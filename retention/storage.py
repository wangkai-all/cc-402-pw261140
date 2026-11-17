"""真实文件存储层：分区、聚合结果、封存报告、必要副本都落到 var/data 下。

所有读写都是真实文件 IO；回收 = os.remove 真实文件，再回写元数据。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from .clock import Clock
from .db import Database
from .events import EventLog


class Storage:
    def __init__(self, root: Path, db: Database, clock: Clock, events: EventLog) -> None:
        self.root = root
        self.partitions_dir = root / "partitions"
        self.results_dir = root / "results"
        self.reports_dir = root / "reports"
        self.copies_dir = root / "copies"
        for d in (self.partitions_dir, self.results_dir, self.reports_dir, self.copies_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.clock = clock
        self.events = events

    @staticmethod
    def sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---------- 分区 ----------
    def create_partition(self, zone: str, business_day: str, records: list[dict]) -> dict:
        """把虚构记录写成真实 CSV 文件并登记分区 + 存储单元。"""
        day_dir = self.partitions_dir / zone / business_day
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / "data.csv"
        header = ",".join(records[0].keys())
        lines = [header] + [",".join(str(v) for v in r.values()) for r in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        size = path.stat().st_size
        digest = self.sha256_file(path)

        pv = self.db.query_one(
            "SELECT version FROM policies WHERE zone=? AND superseded_vt IS NULL ORDER BY version DESC",
            (zone,),
        )
        policy_version = pv["version"] if pv else 1
        with self.db.lock:
            cur = self.db.execute(
                """INSERT INTO partitions
                   (zone, business_day, file_path, size_bytes, sha256, created_vt, status, policy_version)
                   VALUES (?, ?, ?, ?, ?, ?, 'RETAINED', ?)""",
                (zone, business_day, str(path), size, digest, self.clock.now(), policy_version),
            )
            pid = int(cur.lastrowid)
            self.db.execute(
                """INSERT INTO storage_units (kind, partition_id, file_path, size_bytes, sha256, created_vt, status)
                   VALUES ('PARTITION', ?, ?, ?, ?, ?, 'LIVE')""",
                (pid, str(path), size, digest, self.clock.now()),
            )
            self.db.commit()
        self.events.emit("storage", "PARTITION_CREATED",
                         {"partition_id": pid, "zone": zone, "business_day": business_day,
                          "size_bytes": size, "sha256": digest[:12]},
                         target=f"partition:{pid}")
        return {"partition_id": pid, "path": str(path), "size_bytes": size}

    def read_partition(self, path: str) -> list[dict]:
        """真实读取文件（鉴权通过后才会调到这里）。"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(path)
        if not os.access(p, os.R_OK):
            raise PermissionError(f"不可读: {path}")
        lines = p.read_text(encoding="utf-8").splitlines()
        header = lines[0].split(",")
        return [dict(zip(header, line.split(","))) for line in lines[1:]]

    # ---------- 加工结果 ----------
    def write_result(self, task_id: int, payload: str) -> str:
        path = self.results_dir / f"task_{task_id}_result.csv"
        path.write_text(payload, encoding="utf-8")
        return str(path)

    # ---------- 封存报告 ----------
    def write_report(self, report_id: int, payload: str) -> tuple[str, int]:
        path = self.reports_dir / f"report_{report_id}.txt"
        path.write_text(payload, encoding="utf-8")
        return str(path), path.stat().st_size

    def copy_for_report(self, partition_id: int, report_id: int) -> dict:
        """COPY 模式：真实复制分区文件到副本区，登记 COPY 存储单元。"""
        p = self.db.query_one("SELECT * FROM partitions WHERE id=?", (partition_id,))
        src = Path(p["file_path"])
        dst_dir = self.copies_dir / f"report_{report_id}" / p["zone"] / p["business_day"]
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "data.csv"
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            fdst.write(fsrc.read())
        size = dst.stat().st_size
        digest = self.sha256_file(dst)
        with self.db.lock:
            cur = self.db.execute(
                """INSERT INTO storage_units (kind, partition_id, file_path, size_bytes, sha256, created_vt, status)
                   VALUES ('COPY', ?, ?, ?, ?, ?, 'LIVE')""",
                (partition_id, str(dst), size, digest, self.clock.now()),
            )
            copy_id = int(cur.lastrowid)
            self.db.commit()
        self.events.emit("storage", "COPY_CREATED",
                         {"copy_id": copy_id, "partition_id": partition_id, "report_id": report_id,
                          "size_bytes": size, "sha256_match": digest == p["sha256"]},
                         target=f"copy:{copy_id}")
        return {"copy_id": copy_id, "path": str(dst), "size_bytes": size, "sha256": digest}

    # ---------- 真实回收 ----------
    def reclaim_unit(self, unit_id: int) -> int:
        """物理删除存储单元文件并核对消失，返回真实释放字节数。"""
        u = self.db.query_one("SELECT * FROM storage_units WHERE id=?", (unit_id,))
        if u is None:
            raise KeyError(f"storage_unit {unit_id} 不存在")
        if u["status"] == "DELETED":
            return 0
        path = Path(u["file_path"])
        before = path.stat().st_size if path.exists() else 0
        if path.exists():
            os.remove(path)  # 真实文件回收，而不是只改数据库状态
        assert not path.exists(), f"文件仍存在: {path}"
        after_exists = path.exists()
        with self.db.lock:
            self.db.execute("UPDATE storage_units SET status='DELETED' WHERE id=?", (unit_id,))
            if u["kind"] == "PARTITION":
                self.db.execute("UPDATE partitions SET status='DELETED' WHERE id=?",
                                (u["partition_id"],))
            self.db.commit()
        self.events.emit("storage", "FILE_RECLAIMED",
                         {"unit_id": unit_id, "kind": u["kind"], "path": str(path),
                          "bytes_freed": before, "still_exists_after": after_exists},
                         target=f"unit:{unit_id}")
        return before
