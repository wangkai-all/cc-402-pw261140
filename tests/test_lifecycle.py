"""端到端测试：虚构数据 → 加工 → 封存 → 留存缩短 → 失联/恢复 → 真实回收。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retention.app import App  # noqa: E402
from retention.policy import DAY  # noqa: E402


def make_records(n=5):
    return [{"order_id": f"O{i}", "amount": 10.0 + i, "customer": f"c{i % 3}"} for i in range(n)]


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.var = Path(self.tmp.name) / "var"
        self.app = App(self.var, heartbeat_timeout=0.6)
        self.app.start_background()
        self.app.policy.set_retention("orders", 30, "初始 30 天")

    def tearDown(self):
        self.app.shutdown()
        self.tmp.cleanup()

    def _partition(self, pid):
        return dict(self.app.db.query_one("SELECT * FROM partitions WHERE id=?", (pid,)))

    def _unit_of_partition(self, pid):
        return dict(self.app.db.query_one(
            "SELECT * FROM storage_units WHERE kind='PARTITION' AND partition_id=?", (pid,)))

    def _wait_status(self, task_id, status, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.app.db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
            if t["status"] == status:
                return
            time.sleep(0.05)
        raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达 {status}")

    def _wait_grants_released(self, task_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            c = self.app.db.query(
                "SELECT COUNT(*) c FROM grants WHERE task_id=? AND kind='TASK_LEASE' AND status='ACTIVE'",
                (task_id,))[0]["c"]
            if c == 0:
                return
            time.sleep(0.05)
        raise AssertionError(f"任务 {task_id} 的租约未在 {timeout}s 内释放")

    def test_01_seed_and_real_files(self):
        """分区是真实文件，元数据只记账。"""
        info = self.app.storage.create_partition("orders", "2026-10-01", make_records())
        p = Path(info["path"])
        self.assertTrue(p.exists())
        self.assertGreater(p.stat().st_size, 0)
        records = self.app.storage.read_partition(info["path"])
        self.assertEqual(len(records), 5)
        self.assertEqual(float(records[0]["amount"]), 10.0)

    def test_02_retention_shortening_keeps_version_history(self):
        """缩短留存期产生新版本，旧版本留痕；到期按分区适用版本计算。"""
        self.app.storage.create_partition("orders", "2026-10-02", make_records())
        p1 = self._partition(1)
        self.app.policy.set_retention("orders", 3, "缩短为 3 天")
        p2_row = self.app.storage.create_partition("orders", "2026-10-03", make_records())
        p2 = self._partition(p2_row["partition_id"])
        self.assertEqual(p1["policy_version"], 1)
        self.assertEqual(p2["policy_version"], 2)
        pols = self.app.db.query("SELECT * FROM policies WHERE zone='orders' ORDER BY version")
        self.assertEqual([r["retention_days"] for r in pols], [30, 3])
        self.assertIsNotNone(dict(pols[0])["superseded_vt"])
        # 快进 4 天：v2 分区到期，v1 分区（30 天）未到期
        self.app.clock.advance(4 * DAY)
        self.assertTrue(self.app.policy.is_expired(p2))
        self.assertFalse(self.app.policy.is_expired(p1))

    def test_03_task_completes_and_releases_lease(self):
        """加工真实读文件产出结果，完成后释放 TASK_LEASE。"""
        a = self.app.storage.create_partition("orders", "2026-10-04", make_records())
        b = self.app.storage.create_partition("orders", "2026-10-05", make_records())
        t = self.app.executor.create_task("汇总", [a["partition_id"], b["partition_id"]])
        self._wait_status(t["task_id"], "COMPLETED")
        task = self.app.db.query_one("SELECT * FROM tasks WHERE id=?", (t["task_id"],))
        self.assertTrue(Path(task["result_path"]).exists())
        self._wait_grants_released(t["task_id"])

    def test_04_lost_task_does_not_block_reclaim(self):
        """失联任务的租约被 supervisor 撤销，到期分区可被真实回收。"""
        info = self.app.storage.create_partition("orders", "2026-10-06", make_records())
        pid = info["partition_id"]
        path = Path(info["path"])
        t = self.app.executor.create_task("长任务", [pid])
        self.app.executor.trigger_lost(t["task_id"])
        self._wait_status(t["task_id"], "LOST")
        # trigger_lost 已把心跳置为远古时刻；确保 supervisor 完成一轮扫描并撤销租约
        deadline = time.time() + 5.0
        while time.time() < deadline:
            g = self.app.db.query_one(
                "SELECT status FROM grants WHERE task_id=? AND kind='TASK_LEASE'", (t["task_id"],))
            if g["status"] == "REVOKED":
                break
            time.sleep(0.05)
        g = self.app.db.query_one(
            "SELECT status FROM grants WHERE task_id=? AND kind='TASK_LEASE'", (t["task_id"],))
        self.assertEqual(g["status"], "REVOKED")
        # 快进到留存期后
        self.app.clock.advance(31 * DAY)
        plan = self.app.reaper.evaluate()
        ids = [x["unit_id"] for x in plan["reclaimable"]]
        self.assertIn(self._unit_of_partition(pid)["id"], ids)
        result = self.app.reaper.run()
        self.assertFalse(path.exists(), "文件必须被真实 unlink")
        self.assertEqual(self._partition(pid)["status"], "DELETED")
        self.assertGreater(result["bytes_freed_this_run"], 0)

    def test_05_resume_rechecks_access_and_succeeds(self):
        """LOST 后恢复：重新确认读取权、换新令牌、任务跑完。"""
        info = self.app.storage.create_partition("orders", "2026-10-07", make_records())
        pid = info["partition_id"]
        t = self.app.executor.create_task("可恢复任务", [pid])
        self.app.executor.trigger_lost(t["task_id"])
        self._wait_status(t["task_id"], "LOST")
        r = self.app.executor.resume_task(t["task_id"])
        self.assertTrue(all(c["allowed"] for c in r["checks"]))
        self._wait_status(t["task_id"], "COMPLETED")
        # 新令牌与旧令牌不同，旧 worker 已自行退出
        task = self.app.db.query_one("SELECT * FROM tasks WHERE id=?", (t["task_id"],))
        self.assertEqual(task["lease_token"], r["lease_token"])

    def test_06_resume_denied_when_partition_reclaimed(self):
        """恢复期间分区已被回收 → 读取权确认失败 → 任务 FAILED 且留 ACCESS_DENIED 事件。"""
        info = self.app.storage.create_partition("orders", "2026-10-08", make_records())
        pid = info["partition_id"]
        t = self.app.executor.create_task("注定失败", [pid])
        self.app.executor.trigger_lost(t["task_id"])
        self._wait_status(t["task_id"], "LOST")
        # 租约已撤销，快进后回收分区
        self.app.clock.advance(31 * DAY)
        self.app.reaper.run()
        with self.assertRaises(PermissionError):
            self.app.executor.resume_task(t["task_id"])
        task = self.app.db.query_one("SELECT status FROM tasks WHERE id=?", (t["task_id"],))
        self.assertEqual(task["status"], "FAILED")
        ev = self.app.db.query_one(
            "SELECT action FROM events WHERE action='RESUME_ACCESS_DENIED' ORDER BY id DESC")
        self.assertIsNotNone(ev)

    def test_07_hold_report_blocks_reclaim_then_renew_then_expires(self):
        """HOLD 模式：到期分区被报告护住；续用延长；授权过期后可回收。"""
        info = self.app.storage.create_partition("orders", "2026-10-09", make_records())
        pid = info["partition_id"]
        t = self.app.executor.create_task("hold 任务", [pid])
        self._wait_status(t["task_id"], "COMPLETED")
        r = self.app.archive.seal_report(t["task_id"], "月报", "HOLD", [pid], hold_days=40)
        self.app.clock.advance(31 * DAY)  # 留存期已过，但 HOLD（40 天）仍有效
        plan = self.app.reaper.evaluate()
        self.assertNotIn(self._unit_of_partition(pid)["id"],
                         [x["unit_id"] for x in plan["reclaimable"]])
        retained = next(x for x in plan["retained"] if x["partition_id"] == pid)
        self.assertTrue(any("封存报告" in rs for rs in retained["reasons"]))
        # 续用 10 天
        self.app.archive.renew(r["report_id"], 10)
        old = self.app.db.query_one("SELECT status FROM grants WHERE id=?", (r["details"][0]["grant_id"],))
        self.assertEqual(old["status"], "RENEWED")
        # 再快进 15 天：续用（共 40+10 天保护，已过 31 天）也过期 → 可回收
        self.app.clock.advance(20 * DAY)
        plan = self.app.reaper.evaluate()
        self.assertIn(self._unit_of_partition(pid)["id"],
                      [x["unit_id"] for x in plan["reclaimable"]])
        self.app.reaper.run()
        self.assertFalse(Path(info["path"]).exists())

    def test_08_copy_mode_original_reclaimed_but_copy_lives(self):
        """COPY 模式：原件可被回收，副本独立保留并真实占用空间，副本到期再回收。"""
        info = self.app.storage.create_partition("orders", "2026-10-10", make_records())
        pid = info["partition_id"]
        t = self.app.executor.create_task("copy 任务", [pid])
        self._wait_status(t["task_id"], "COMPLETED")
        r = self.app.archive.seal_report(t["task_id"], "副本报告", "COPY", [pid], hold_days=40)
        copy_id = r["details"][0]["copy_id"]
        copy_unit = self.app.db.query_one("SELECT * FROM storage_units WHERE id=?", (copy_id,))
        self.assertTrue(Path(copy_unit["file_path"]).exists())
        # 副本与原件内容一致（sha256）
        orig = self._partition(pid)
        self.assertEqual(copy_unit["sha256"], orig["sha256"])
        # 快进到原件留存期后（副本 20 天仍有效）
        self.app.clock.advance(31 * DAY)
        self.app.reaper.run()
        self.assertFalse(Path(info["path"]).exists(), "原件应已回收")
        self.assertTrue(Path(copy_unit["file_path"]).exists(), "副本必须仍在盘")
        # 副本到期后回收（封存后累计已快进 31 天，再 15 天超过 40 天绑定）
        self.app.clock.advance(15 * DAY)
        plan = self.app.reaper.evaluate()
        self.assertIn(copy_id, [x["unit_id"] for x in plan["reclaimable"]])
        self.app.reaper.run()
        self.assertFalse(Path(copy_unit["file_path"]).exists())

    def test_09_mtime_ignored_only_expiry_and_grants_matter(self):
        """回收判定不看文件日期：把文件 mtime 改成远古，留存期内依然保留。"""
        info = self.app.storage.create_partition("orders", "2026-10-11", make_records())
        os.utime(info["path"], (1, 1))  # 1970 年的 mtime
        plan = self.app.reaper.evaluate()
        self.assertNotIn(self._unit_of_partition(info["partition_id"])["id"],
                         [x["unit_id"] for x in plan["reclaimable"]])

    def test_10_reap_accounts_real_bytes_and_audit_trail(self):
        """释放空间按真实 stat 字节统计；所有动作有审计事件。"""
        infos = [self.app.storage.create_partition("orders", f"2026-11-{12+i:02d}", make_records())
                 for i in range(3)]
        # 走一遍完整授权链：签发 TASK_LEASE 再释放
        tk = self.app.executor.create_task("审计链任务", [i["partition_id"] for i in infos])
        self._wait_status(tk["task_id"], "COMPLETED")
        sizes = sum(Path(i["path"]).stat().st_size for i in infos)
        self.app.clock.advance(31 * DAY)
        result = self.app.reaper.run()
        self.assertEqual(result["bytes_freed_this_run"], sizes)
        for i in infos:
            self.assertFalse(Path(i["path"]).exists())
        evs = {r["action"] for r in self.app.db.query("SELECT DISTINCT action FROM events")}
        for required in ("PARTITION_CREATED", "GRANT_ISSUED", "FILE_RECLAIMED", "RECLAIM_RUN"):
            self.assertIn(required, evs)
        # 事件详情是合法 JSON
        for r in self.app.db.query("SELECT detail FROM events LIMIT 5"):
            json.loads(r["detail"])

    def test_11_cannot_seal_undeclared_partition(self):
        """报告不能引用任务输入清单之外的分区（鉴权边界）。"""
        a = self.app.storage.create_partition("orders", "2026-10-13", make_records())
        b = self.app.storage.create_partition("orders", "2026-10-14", make_records())
        t = self.app.executor.create_task("边界任务", [a["partition_id"]])
        self._wait_status(t["task_id"], "COMPLETED")
        with self.assertRaises(PermissionError):
            self.app.archive.seal_report(t["task_id"], "越权", "HOLD", [b["partition_id"]], 5)

    def test_12_preview_does_not_delete(self):
        """dry-run / preview 不产生任何删除。"""
        info = self.app.storage.create_partition("orders", "2026-10-15", make_records())
        self.app.clock.advance(31 * DAY)
        dry = self.app.reaper.run(dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertTrue(Path(info["path"]).exists())
        self.assertEqual(self._partition(info["partition_id"])["status"], "RETAINED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
