"""服务容器 + 演示数据生成：把各组件装配起来，并提供一键造数。"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .clock import Clock
from .db import Database
from .events import EventLog
from .executor import Executor
from .policy import DAY, PolicyService
from .reaper import Reaper
from .reports import ReportArchive
from .storage import Storage


class App:
    def __init__(self, var_dir: Path, heartbeat_timeout: float = 15.0) -> None:
        self.var_dir = var_dir
        self.data_dir = var_dir / "data"
        self.clock = Clock()
        self.db = Database(var_dir / "meta.db")
        self.events = EventLog(self.db, self.clock)
        self.policy = PolicyService(self.db, self.clock, self.events)
        self.storage = Storage(self.data_dir, self.db, self.clock, self.events)
        self.executor = Executor(self.db, self.clock, self.events, self.policy,
                                 self.storage, heartbeat_timeout=heartbeat_timeout)
        self.archive = ReportArchive(self.db, self.clock, self.events, self.policy, self.storage)
        self.reaper = Reaper(self.db, self.clock, self.events, self.policy, self.storage)

    def start_background(self) -> None:
        self.executor.start()

    def shutdown(self) -> None:
        self.executor.shutdown()
        self.db.close()

    # ---------- 一键造数：两个 zone，多个业务日分区 ----------
    def seed_demo(self) -> dict:
        # 初始策略（后续网页上可缩短留存期）
        if self.policy.current_policy("orders") is None:
            self.policy.set_retention("orders", 30, note="订单区初始留存 30 天")
        if self.policy.current_policy("events") is None:
            self.policy.set_retention("events", 14, note="事件区初始留存 14 天")

        rng = random.Random(402)
        created = []
        # orders: 业务日从虚拟时间 40 天前开始，共 6 天；events: 20 天前开始，共 4 天
        specs = [
            ("orders", 40, 6),
            ("events", 20, 4),
        ]
        for zone, age_start, days in specs:
            for i in range(days):
                age = age_start - i * 2  # 每隔一天一个分区
                day_vt = self.clock.now() - age * DAY
                business_day = self.clock.iso(day_vt)[:10]
                exists = self.db.query_one(
                    "SELECT id FROM partitions WHERE zone=? AND business_day=?", (zone, business_day))
                if exists:
                    continue
                records = [
                    {"order_id": f"{zone[:1].upper()}{rng.randint(1000, 9999)}",
                     "amount": round(rng.uniform(10, 500), 2),
                     "customer": f"cust_{rng.randint(1, 50)}"}
                    for _ in range(8)
                ]
                info = self.storage.create_partition(zone, business_day, records)
                created.append(info)
        self.events.emit("api", "DEMO_SEEDED", {"partitions": len(created)})
        return {"created_partitions": len(created), "items": created}

    # ---------- 状态汇总（网页轮询）----------
    def snapshot(self) -> dict:
        def rows(sql, params=()):
            return [dict(r) for r in self.db.query(sql, params)]

        partitions = []
        for p in rows("SELECT * FROM partitions ORDER BY zone, business_day"):
            p["expiry_vt"] = self.policy.expiry_vt(p)
            p["expiry_iso"] = self.clock.iso(p["expiry_vt"])
            p["expired"] = self.policy.is_expired(p)
            p["reasons"] = self.policy.retention_reasons_partition(p["id"]) if p["status"] != "DELETED" else []
            p["file_exists"] = Path(p["file_path"]).exists()
            partitions.append(p)

        units = rows("SELECT * FROM storage_units ORDER BY id")
        for u in units:
            u["file_exists"] = Path(u["file_path"]).exists()

        tasks = rows("SELECT * FROM tasks ORDER BY id")
        for t in tasks:
            t["input_ids"] = json.loads(t["input_ids"])
            t["progress"] = rows("SELECT * FROM task_progress WHERE task_id=? ORDER BY partition_id", (t["id"],))
            t["heartbeat_age_s"] = round(self.clock.wall() - (t["last_heartbeat"] or 0), 1)

        reports = rows("SELECT * FROM reports ORDER BY id")
        for r in reports:
            r["file_exists"] = Path(r["file_path"]).exists()
            r["grants"] = rows("SELECT * FROM grants WHERE report_id=? ORDER BY id", (r["id"],))

        grants = rows("SELECT * FROM grants ORDER BY id DESC")
        for g in grants:
            g["expire_iso"] = self.clock.iso(g["expire_vt"])
            g["active_now"] = g["status"] == "ACTIVE" and g["expire_vt"] > self.clock.now()

        policies = rows("SELECT * FROM policies ORDER BY zone, version")
        for p in policies:
            p["created_iso"] = self.clock.iso(p["created_vt"])

        freed = self.db.query_one(
            "SELECT COALESCE(SUM(size_bytes),0) AS s, COUNT(*) AS c FROM storage_units WHERE status='DELETED'"
        )
        return {
            "virtual_now": self.clock.now(),
            "virtual_now_iso": self.clock.iso(),
            "wall_now": self.clock.wall(),
            "policies": policies,
            "partitions": partitions,
            "storage_units": units,
            "tasks": tasks,
            "reports": reports,
            "grants": grants,
            "totals": {
                "live_units": sum(1 for u in units if u["status"] == "LIVE"),
                "deleted_units": freed["c"],
                "freed_bytes": freed["s"],
                "live_bytes": sum(u["size_bytes"] for u in units if u["status"] == "LIVE"),
            },
            "events": self.events.list(80),
        }
