"""策略服务：留存策略版本管理、到期计算、引用授权台账（grants）。

回收判定不看文件日期，只看两件事：
1. 按"分区创建时适用的策略版本"算出的虚拟到期点是否已过（虚拟时钟）；
2. grants 台账里是否还有 ACTIVE 且未过期的引用。
"""
from __future__ import annotations

import json
import uuid

from .clock import Clock
from .db import Database
from .events import EventLog

DAY = 86400.0


class PolicyService:
    def __init__(self, db: Database, clock: Clock, events: EventLog) -> None:
        self.db = db
        self.clock = clock
        self.events = events

    # ---------- 策略版本 ----------
    def current_policy(self, zone: str) -> dict | None:
        r = self.db.query_one(
            "SELECT * FROM policies WHERE zone=? AND superseded_vt IS NULL ORDER BY version DESC",
            (zone,),
        )
        return dict(r) if r else None

    def set_retention(self, zone: str, retention_days: int, note: str = "") -> dict:
        """留存期限调整：旧版本标记 superseded（留痕），追加新版本。"""
        if retention_days <= 0:
            raise ValueError("留存天数必须为正")
        with self.db.lock:
            cur = self.db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM policies WHERE zone=?", (zone,)
            )
            version = cur.fetchone()["v"]
            self.db.execute(
                "UPDATE policies SET superseded_vt=? WHERE zone=? AND superseded_vt IS NULL",
                (self.clock.now(), zone),
            )
            self.db.execute(
                """INSERT INTO policies (zone, retention_days, version, note, created_vt)
                   VALUES (?, ?, ?, ?, ?)""",
                (zone, retention_days, version, note, self.clock.now()),
            )
            self.db.commit()
        self.events.emit("policy", "RETENTION_CHANGED",
                         {"zone": zone, "new_retention_days": retention_days,
                          "version": version, "note": note},
                         target=f"policy:{zone}")
        return self.current_policy(zone)

    def expiry_vt(self, partition: dict) -> float:
        """按分区创建时适用的策略版本计算虚拟到期点（不是文件 mtime）。"""
        p = self.db.query_one(
            "SELECT * FROM policies WHERE zone=? AND version=?",
            (partition["zone"], partition["policy_version"]),
        )
        days = p["retention_days"] if p else self.current_policy(partition["zone"])["retention_days"]
        return partition["created_vt"] + days * DAY

    def is_expired(self, partition: dict) -> bool:
        return self.clock.now() >= self.expiry_vt(partition)

    # ---------- grants 台账 ----------
    def issue_grant(self, kind: str, *, partition_id: int | None = None, copy_id: int | None = None,
                    task_id: int | None = None, report_id: int | None = None,
                    expire_vt: float, note: str = "") -> int:
        with self.db.lock:
            cur = self.db.execute(
                """INSERT INTO grants (kind, partition_id, copy_id, task_id, report_id,
                                       status, issued_vt, expire_vt, note)
                   VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)""",
                (kind, partition_id, copy_id, task_id, report_id, self.clock.now(), expire_vt, note),
            )
            gid = int(cur.lastrowid)
            self.db.commit()
        self.events.emit("policy", "GRANT_ISSUED",
                         {"grant_id": gid, "kind": kind, "partition_id": partition_id,
                          "copy_id": copy_id, "task_id": task_id, "report_id": report_id,
                          "expire_vt": expire_vt, "note": note},
                         target=f"grant:{gid}")
        return gid

    def _set_status(self, grant_id: int, status: str, extra: dict | None = None) -> None:
        with self.db.lock:
            self.db.execute("UPDATE grants SET status=? WHERE id=?", (status, grant_id))
            self.db.commit()
        detail = {"grant_id": grant_id, "status": status}
        if extra:
            detail.update(extra)
        self.events.emit("policy", f"GRANT_{status}", detail, target=f"grant:{grant_id}")

    def release_grant(self, grant_id: int) -> None:
        self._set_status(grant_id, "RELEASED")

    def revoke_grant(self, grant_id: int, reason: str) -> None:
        self._set_status(grant_id, "REVOKED", {"reason": reason})

    def expire_due_grants(self) -> list[int]:
        """虚拟时钟到期的 ACTIVE grant 标记 EXPIRED（惰性 + 回收前统一执行）。"""
        rows = self.db.query(
            "SELECT id, kind FROM grants WHERE status='ACTIVE' AND expire_vt <= ?",
            (self.clock.now(),),
        )
        out = []
        for r in rows:
            self._set_status(r["id"], "EXPIRED")
            out.append(r["id"])
        return out

    def active_grants_for_partition(self, partition_id: int) -> list[dict]:
        """当前真正有效的原件引用：ACTIVE 且虚拟时间未到期。"""
        rows = self.db.query(
            """SELECT * FROM grants
               WHERE partition_id=? AND status='ACTIVE' AND expire_vt > ?
               ORDER BY id""",
            (partition_id, self.clock.now()),
        )
        return [dict(r) for r in rows]

    def active_grants_for_copy(self, copy_id: int) -> list[dict]:
        rows = self.db.query(
            """SELECT * FROM grants
               WHERE copy_id=? AND status='ACTIVE' AND expire_vt > ?
               ORDER BY id""",
            (copy_id, self.clock.now()),
        )
        return [dict(r) for r in rows]

    def renew_report_hold(self, report_id: int, extra_days: int) -> dict:
        """续用报告：旧 grant 留痕为 RENEWED，签发新的 ACTIVE grant（可追溯链）。"""
        if extra_days <= 0:
            raise ValueError("续用天数必须为正")
        old = self.db.query_one(
            "SELECT * FROM grants WHERE report_id=? AND kind='REPORT_HOLD' AND status='ACTIVE' "
            "ORDER BY id DESC", (report_id,))
        report = self.db.query_one("SELECT * FROM reports WHERE id=?", (report_id,))
        if old is None:
            raise RuntimeError("该报告没有有效的延迟回收授权，无法续用")
        new_expire = max(self.clock.now(), old["expire_vt"]) + extra_days * DAY
        with self.db.lock:
            self.db.execute("UPDATE grants SET status='RENEWED' WHERE id=?", (old["id"],))
            cur = self.db.execute(
                """INSERT INTO grants (kind, partition_id, copy_id, task_id, report_id,
                                       status, issued_vt, expire_vt, renew_count, renewed_by_grant, note)
                   VALUES ('REPORT_HOLD', ?, NULL, ?, ?, 'ACTIVE', ?, ?, ?, NULL, '报告续用')""",
                (old["partition_id"], old["task_id"], report_id, self.clock.now(),
                 new_expire, old["renew_count"] + 1),
            )
            new_id = int(cur.lastrowid)
            self.db.execute("UPDATE grants SET renewed_by_grant=? WHERE id=?", (new_id, old["id"]))
            self.db.commit()
        self.events.emit("policy", "REPORT_RENEWED",
                         {"report_id": report_id, "old_grant": old["id"], "new_grant": new_id,
                          "extra_days": extra_days, "new_expire_vt": new_expire,
                          "mode": report["mode"]},
                         target=f"report:{report_id}")
        return {"new_grant_id": new_id, "expire_vt": new_expire}

    def renew_copy_binding(self, report_id: int, extra_days: int) -> dict:
        """COPY 模式续用：延长 COPY_BINDING（原件早已可回收，护的是副本）。"""
        if extra_days <= 0:
            raise ValueError("续用天数必须为正")
        rows = self.db.query(
            "SELECT * FROM grants WHERE report_id=? AND kind='COPY_BINDING' AND status='ACTIVE'",
            (report_id,),
        )
        if not rows:
            raise RuntimeError("该报告没有有效的副本绑定，无法续用")
        results = []
        for old in rows:
            new_expire = max(self.clock.now(), old["expire_vt"]) + extra_days * DAY
            with self.db.lock:
                self.db.execute("UPDATE grants SET status='RENEWED' WHERE id=?", (old["id"],))
                cur = self.db.execute(
                    """INSERT INTO grants (kind, partition_id, copy_id, task_id, report_id,
                                           status, issued_vt, expire_vt, renew_count, renewed_by_grant, note)
                       VALUES ('COPY_BINDING', NULL, ?, ?, ?, 'ACTIVE', ?, ?, ?, NULL, '副本续用')""",
                    (old["copy_id"], old["task_id"], report_id, self.clock.now(),
                     new_expire, old["renew_count"] + 1),
                )
                new_id = int(cur.lastrowid)
                self.db.execute("UPDATE grants SET renewed_by_grant=? WHERE id=?", (new_id, old["id"]))
                self.db.commit()
            self.events.emit("policy", "COPY_RENEWED",
                             {"report_id": report_id, "old_grant": old["id"], "new_grant": new_id,
                              "copy_id": old["copy_id"], "new_expire_vt": new_expire},
                             target=f"grant:{new_id}")
            results.append({"copy_id": old["copy_id"], "new_grant_id": new_id, "expire_vt": new_expire})
        return {"renewed": results}

    # ---------- 保留原因（供预览 / 网页展示）----------
    def retention_reasons_partition(self, partition_id: int) -> list[str]:
        reasons = []
        for g in self.active_grants_for_partition(partition_id):
            if g["kind"] == "TASK_LEASE":
                t = self.db.query_one("SELECT name FROM tasks WHERE id=?", (g["task_id"],))
                reasons.append(f"未完成加工引用：任务#{g['task_id']}「{t['name'] if t else ''}」"
                               f"租约有效至虚拟 {self.clock.iso(g['expire_vt'])}")
            elif g["kind"] == "REPORT_HOLD":
                reasons.append(f"封存报告延迟回收：报告#{g['report_id']} 续用至虚拟 "
                               f"{self.clock.iso(g['expire_vt'])}（已续 {g['renew_count']} 次）")
        return reasons
