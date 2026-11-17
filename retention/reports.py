"""封存报告：把加工结果封存为报告，并二选一建立旧数据引用。

- HOLD（延迟回收）：原件分区不复制，签发 REPORT_HOLD grant 护住原件，可续用；
- COPY（保留必要副本）：真实复制一份到副本区，签发 COPY_BINDING，原件随即
  失去这层保护（但仍受其它有效 grant 约束），副本有独立过期点。
"""
from __future__ import annotations

import json

from .clock import Clock
from .db import Database
from .events import EventLog
from .policy import DAY, PolicyService
from .storage import Storage


class ReportArchive:
    def __init__(self, db: Database, clock: Clock, events: EventLog,
                 policy: PolicyService, storage: Storage) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.policy = policy
        self.storage = storage

    def seal_report(self, task_id: int, title: str, mode: str,
                    partition_ids: list[int], hold_days: int) -> dict:
        if mode not in ("HOLD", "COPY"):
            raise ValueError("mode 必须是 HOLD 或 COPY")
        if hold_days <= 0:
            raise ValueError("保留天数必须为正")
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None:
            raise KeyError("任务不存在")
        if not partition_ids:
            raise ValueError("至少选择一个被引用分区")
        declared = json.loads(task["input_ids"])
        for pid in partition_ids:
            if pid not in declared:
                raise PermissionError(f"分区 {pid} 不在任务声明的输入清单内，无权引用")
            p = self.db.query_one("SELECT * FROM partitions WHERE id=?", (pid,))
            if p is None or p["status"] == "DELETED":
                raise FileNotFoundError(f"分区 {pid} 已被回收，无法封存引用")

        with self.db.lock:
            cur = self.db.execute(
                """INSERT INTO reports (task_id, title, mode, file_path, size_bytes, created_vt, status)
                   VALUES (?, ?, ?, '', 0, ?, 'ACTIVE')""",
                (task_id, title, mode, self.clock.now()),
            )
            report_id = int(cur.lastrowid)
            self.db.commit()

        # 真实报告文件
        body = [
            f"封存报告 #{report_id}",
            f"标题: {title}",
            f"来源任务: #{task_id}（{task['name']}）",
            f"引用模式: {mode}",
            f"引用分区: {partition_ids}",
            f"封存虚拟时间: {self.clock.iso()}",
            "=" * 48,
        ]
        for pid in partition_ids:
            p = self.db.query_one("SELECT * FROM partitions WHERE id=?", (pid,))
            body.append(f"[分区 {pid}] {p['zone']}/{p['business_day']} sha256={p['sha256'][:16]}…")
        path, size = self.storage.write_report(report_id, "\n".join(body) + "\n")
        with self.db.lock:
            self.db.execute("UPDATE reports SET file_path=?, size_bytes=? WHERE id=?",
                            (path, size, report_id))
            self.db.commit()

        expire_vt = self.clock.now() + hold_days * DAY
        details = []
        if mode == "HOLD":
            for pid in partition_ids:
                gid = self.policy.issue_grant(
                    "REPORT_HOLD", partition_id=pid, task_id=task_id, report_id=report_id,
                    expire_vt=expire_vt, note=f"报告#{report_id} 延迟回收",
                )
                details.append({"partition_id": pid, "grant_id": gid})
        else:
            for pid in partition_ids:
                copy = self.storage.copy_for_report(pid, report_id)
                gid = self.policy.issue_grant(
                    "COPY_BINDING", copy_id=copy["copy_id"], task_id=task_id, report_id=report_id,
                    expire_vt=expire_vt, note=f"报告#{report_id} 必要副本绑定",
                )
                details.append({"partition_id": pid, "copy_id": copy["copy_id"], "grant_id": gid})

        self.events.emit("archive", "REPORT_SEALED",
                         {"report_id": report_id, "task_id": task_id, "mode": mode,
                          "partition_ids": partition_ids, "hold_days": hold_days,
                          "report_file": path, "report_size": size, "details": details},
                         target=f"report:{report_id}")
        return {"report_id": report_id, "mode": mode, "file_path": path, "details": details,
                "expire_vt": expire_vt}

    def renew(self, report_id: int, extra_days: int) -> dict:
        report = self.db.query_one("SELECT * FROM reports WHERE id=?", (report_id,))
        if report is None:
            raise KeyError("报告不存在")
        if report["mode"] == "HOLD":
            return self.policy.renew_report_hold(report_id, extra_days)
        return self.policy.renew_copy_binding(report_id, extra_days)
