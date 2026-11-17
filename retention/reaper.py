"""回收器：只依据"虚拟到期 + 引用台账"判定，绝不按文件日期删除。

判定顺序（每个 LIVE 存储单元）：
1. 先让到期的 ACTIVE grant 流转为 EXPIRED；
2. 副本（COPY）：没有有效 COPY_BINDING → 可回收；
3. 分区原件（PARTITION）：
   a. 策略留存期未到 → 保留（原因：留存期内）；
   b. 仍有有效 TASK_LEASE / REPORT_HOLD → 保留（原因：被引用）；
   c. 否则 → 可回收，真实 unlink 文件并统计释放字节。

支持 dry_run 预览（网页"可清理范围"）与 run 真实回收。
"""
from __future__ import annotations

from .clock import Clock
from .db import Database
from .events import EventLog
from .policy import PolicyService
from .storage import Storage


class Reaper:
    def __init__(self, db: Database, clock: Clock, events: EventLog,
                 policy: PolicyService, storage: Storage) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.policy = policy
        self.storage = storage

    def evaluate(self) -> dict:
        """返回当前可清理范围 + 每个单元保留/可清理的原因。不做任何修改。"""
        self.policy.expire_due_grants()
        units = self.db.query("SELECT * FROM storage_units WHERE status='LIVE' ORDER BY id")
        reclaimable, retained = [], []
        total_reclaimable = 0
        for u in units:
            base = {"unit_id": u["id"], "kind": u["kind"], "partition_id": u["partition_id"],
                    "path": u["file_path"], "size_bytes": u["size_bytes"]}
            if u["kind"] == "COPY":
                active = self.policy.active_grants_for_copy(u["id"])
                if active:
                    g = active[0]
                    retained.append({**base, "reasons": [
                        f"必要副本被报告#{g['report_id']} 绑定，至虚拟 {self.clock.iso(g['expire_vt'])}"]})
                else:
                    reclaimable.append({**base, "reasons": ["副本绑定已过期/释放，无有效引用"]})
                    total_reclaimable += u["size_bytes"]
                continue

            # PARTITION 原件
            p = self.db.query_one("SELECT * FROM partitions WHERE id=?", (u["partition_id"],))
            if not self.policy.is_expired(p):
                retained.append({**base, "reasons": [
                    f"留存期内：{p['zone']} 策略 {p['policy_version']} 版，"
                    f"虚拟到期点 {self.clock.iso(self.policy.expiry_vt(p))}"]})
                continue
            reasons = self.policy.retention_reasons_partition(p["id"])
            if reasons:
                retained.append({**base, "reasons": reasons})
            else:
                reclaimable.append({**base, "reasons": ["留存期已过且无任何有效引用"]})
                total_reclaimable += u["size_bytes"]

        freed_so_far = self.db.query_one(
            "SELECT COALESCE(SUM(size_bytes),0) AS s FROM storage_units WHERE status='DELETED'"
        )["s"]
        return {
            "virtual_now": self.clock.now(),
            "virtual_now_iso": self.clock.iso(),
            "reclaimable": reclaimable,
            "retained": retained,
            "reclaimable_count": len(reclaimable),
            "reclaimable_bytes": total_reclaimable,
            "total_freed_bytes_historical": freed_so_far,
        }

    def run(self, dry_run: bool = False) -> dict:
        plan = self.evaluate()
        if dry_run:
            return {"dry_run": True, **plan}
        freed = 0
        deleted = []
        for item in plan["reclaimable"]:
            # 回收前再次在锁内确认没有新 grant 抢入（TOCTOU 防护）
            with self.db.lock:
                u = self.db.query_one("SELECT * FROM storage_units WHERE id=?", (item["unit_id"],))
                if u["status"] != "LIVE":
                    continue
                if u["kind"] == "COPY" and self.policy.active_grants_for_copy(u["id"]):
                    continue
                if u["kind"] == "PARTITION" and self.policy.active_grants_for_partition(u["partition_id"]):
                    continue
                bytes_freed = self.storage.reclaim_unit(item["unit_id"])
            freed += bytes_freed
            deleted.append({"unit_id": item["unit_id"], "kind": item["kind"], "bytes_freed": bytes_freed})
        self.events.emit("reaper", "RECLAIM_RUN",
                         {"deleted_count": len(deleted), "bytes_freed": freed,
                          "items": deleted}, target="reaper")
        after = self.evaluate()
        return {"dry_run": False, "deleted": deleted, "bytes_freed_this_run": freed, **after}
