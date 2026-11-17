"""加工执行器：任务租约、真实加工线程、心跳、失联判定、恢复时重新鉴权。

- 租约 TASK_LEASE 走真实墙钟心跳；supervisor 周期检查，超时即判 LOST 并撤销租约，
  因此失联任务不可能永久阻塞回收。
- 恢复（resume）不相信旧状态：逐个分区重新确认读取权（存在 + os.R_OK + 在任务
  声明的输入清单内），通过才签发新令牌新租约；任何一个不通过则任务 FAILED。
"""
from __future__ import annotations

import json
import threading
import time
import uuid

from .clock import Clock
from .db import Database
from .events import EventLog
from .policy import PolicyService
from .storage import Storage


class Executor:
    def __init__(self, db: Database, clock: Clock, events: EventLog,
                 policy: PolicyService, storage: Storage,
                 heartbeat_timeout: float = 15.0, tick: float = 0.8) -> None:
        self.db = db
        self.clock = clock
        self.events = events
        self.policy = policy
        self.storage = storage
        self.heartbeat_timeout = heartbeat_timeout  # 真实秒数
        # 循环睡眠不得超过心跳超时的一半，否则睡眠期内租约会过期被误判失联
        self.tick = min(tick, heartbeat_timeout / 2)
        self._stop = threading.Event()
        self._workers: dict[int, threading.Thread] = {}
        self._supervisor: threading.Thread | None = None

    def start(self) -> None:
        self._supervisor = threading.Thread(target=self._supervise, name="supervisor", daemon=True)
        self._supervisor.start()

    def shutdown(self) -> None:
        self._stop.set()

    # ---------- 创建任务并签发租约 ----------
    def create_task(self, name: str, partition_ids: list[int]) -> dict:
        if not partition_ids:
            raise ValueError("至少选择一个输入分区")
        # 初始鉴权：所有声明输入必须存在且可读
        for pid in partition_ids:
            p = self.db.query_one("SELECT * FROM partitions WHERE id=?", (pid,))
            if p is None:
                raise FileNotFoundError(f"分区 {pid} 不存在")
            if p["status"] == "DELETED" or not __import__("os").path.exists(p["file_path"]):
                raise PermissionError(f"分区 {pid} 文件已不存在，不能开工")
            if not __import__("os").access(p["file_path"], __import__("os").R_OK):
                raise PermissionError(f"分区 {pid} 不可读")
        token = uuid.uuid4().hex
        now_wall = Clock.wall()
        with self.db.lock:
            cur = self.db.execute(
                """INSERT INTO tasks (name, kind, status, input_ids, last_heartbeat,
                                      lease_token, lease_expires, created_vt)
                   VALUES (?, 'PIPELINE', 'RUNNING', ?, ?, ?, ?, ?)""",
                (name, json.dumps(partition_ids), now_wall, token,
                 now_wall + self.heartbeat_timeout, self.clock.now()),
            )
            task_id = int(cur.lastrowid)
            for pid in partition_ids:
                self.db.execute(
                    "INSERT INTO task_progress (task_id, partition_id, state) VALUES (?, ?, 'PENDING')",
                    (task_id, pid),
                )
            self.db.commit()
            # 每个输入分区一条 TASK_LEASE（虚拟过期点给一个很大的窗口：任务完成才释放；
            # 真正的活性约束由墙钟心跳 + supervisor 保证）
            for pid in partition_ids:
                self.policy.issue_grant(
                    "TASK_LEASE", partition_id=pid, task_id=task_id,
                    expire_vt=self.clock.now() + 3650 * 86400.0,
                    note=f"任务#{task_id} 加工租约",
                )
        self.events.emit("executor", "TASK_CREATED",
                         {"task_id": task_id, "name": name, "inputs": partition_ids,
                          "lease_token": token[:8]}, target=f"task:{task_id}")
        t = threading.Thread(target=self._worker, args=(task_id, token),
                             name=f"worker-{task_id}", daemon=True)
        self._workers[task_id] = t
        t.start()
        return {"task_id": task_id, "lease_token": token}

    # ---------- 加工线程：真实读文件、写聚合结果 ----------
    def _worker(self, task_id: int, token: str) -> None:
        # 入场立即打一次心跳，消除"线程刚启动还没轮到心跳就被 supervisor 判失联"的竞态
        self._heartbeat(task_id, token)
        if self._token_stale(task_id, token):
            return
        try:
            while not self._stop.is_set():
                task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if task is None or task["lease_token"] != token:
                    # 令牌已作废（失联撤销 / 恢复换新），旧线程必须退出
                    self.events.emit("executor", "WORKER_TOKEN_STALE",
                                     {"task_id": task_id}, target=f"task:{task_id}")
                    return
                if task["status"] != "RUNNING":
                    return
                self._heartbeat(task_id, token)
                row = self.db.query_one(
                    "SELECT * FROM task_progress WHERE task_id=? AND state='PENDING' "
                    "ORDER BY partition_id LIMIT 1", (task_id,))
                if row is None:
                    self._finish(task_id, token)
                    return
                pid = row["partition_id"]
                # 每次读取前再次核对令牌与租约
                task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if task["lease_token"] != token:
                    return
                try:
                    records = self.storage.read_partition(
                        self.db.query_one("SELECT file_path FROM partitions WHERE id=?", (pid,))["file_path"]
                    )
                    total = sum(float(r["amount"]) for r in records)
                    count = len(records)
                    with self.db.lock:
                        self.db.execute(
                            "UPDATE task_progress SET state='DONE', attempts=attempts+1 WHERE task_id=? AND partition_id=?",
                            (task_id, pid),
                        )
                        self.db.commit()
                    self.events.emit("executor", "PARTITION_PROCESSED",
                                     {"task_id": task_id, "partition_id": pid,
                                      "rows": count, "amount_sum": round(total, 2)},
                                     target=f"task:{task_id}")
                except (FileNotFoundError, PermissionError) as e:
                    with self.db.lock:
                        self.db.execute(
                            "UPDATE task_progress SET state='FAILED', attempts=attempts+1 WHERE task_id=? AND partition_id=?",
                            (task_id, pid),
                        )
                        self.db.execute(
                            "UPDATE tasks SET status='FAILED', fail_reason=? WHERE id=?",
                            (f"读取分区 {pid} 失败: {e}", task_id),
                        )
                        self.db.commit()
                    self._release_task_grants(task_id)
                    self.events.emit("executor", "TASK_FAILED",
                                     {"task_id": task_id, "reason": str(e)}, target=f"task:{task_id}")
                    return
                time.sleep(self.tick)
        except Exception as e:  # 线程兜底
            self.events.emit("executor", "WORKER_CRASHED",
                             {"task_id": task_id, "error": repr(e)}, target=f"task:{task_id}")

    def _token_stale(self, task_id: int, token: str) -> bool:
        task = self.db.query_one("SELECT lease_token FROM tasks WHERE id=?", (task_id,))
        return task is None or task["lease_token"] != token

    def _heartbeat(self, task_id: int, token: str) -> None:
        if self._token_stale(task_id, token):
            return
        with self.db.lock:
            now = Clock.wall()
            self.db.execute(
                "UPDATE tasks SET last_heartbeat=?, lease_expires=? WHERE id=?",
                (now, now + self.heartbeat_timeout, task_id),
            )
            self.db.commit()

    def _finish(self, task_id: int, token: str) -> None:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None or task["lease_token"] != token:
            return
        # 写真实聚合结果文件
        rows = self.db.query(
            """SELECT tp.partition_id, p.zone, p.business_day, tp.state
               FROM task_progress tp JOIN partitions p ON p.id=tp.partition_id
               WHERE tp.task_id=? ORDER BY tp.partition_id""", (task_id,))
        payload = ["partition_id,zone,business_day,state"]
        payload += [f"{r['partition_id']},{r['zone']},{r['business_day']},{r['state']}" for r in rows]
        path = self.storage.write_result(task_id, "\n".join(payload) + "\n")
        with self.db.lock:
            self.db.execute(
                "UPDATE tasks SET status='COMPLETED', result_path=?, completed_vt=? WHERE id=?",
                (path, self.clock.now(), task_id),
            )
            self.db.commit()
        self._release_task_grants(task_id)
        self.events.emit("executor", "TASK_COMPLETED",
                         {"task_id": task_id, "result_path": path}, target=f"task:{task_id}")

    def _release_task_grants(self, task_id: int) -> None:
        rows = self.db.query(
            "SELECT id FROM grants WHERE task_id=? AND kind='TASK_LEASE' AND status='ACTIVE'",
            (task_id,))
        for r in rows:
            self.policy.release_grant(r["id"])

    # ---------- 主动触发失联（网页按钮；模拟进程崩溃/网络分区）----------
    def trigger_lost(self, task_id: int) -> dict:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None:
            raise KeyError("任务不存在")
        if task["status"] != "RUNNING":
            raise RuntimeError(f"任务当前状态 {task['status']}，只有 RUNNING 能被置为失联")
        # 令牌作废 → 旧 worker 下一轮自动退出；心跳时间戳推到很久以前 → supervisor 立刻判定
        with self.db.lock:
            self.db.execute(
                "UPDATE tasks SET lease_token=NULL, last_heartbeat=?, lease_expires=? WHERE id=?",
                (0.0, 0.0, task_id),
            )
            self.db.commit()
        self.events.emit("executor", "TASK_LOST_TRIGGERED",
                         {"task_id": task_id, "reason": "网页手动触发：模拟进程失联"},
                         target=f"task:{task_id}")
        return {"task_id": task_id}

    # ---------- supervisor：真实墙钟判定失联，撤销租约解除阻塞 ----------
    def _supervise(self) -> None:
        # 启动宽限：给 worker 线程留出调度入场时间，避免冷启动误判
        self._stop.wait(self.heartbeat_timeout)
        while not self._stop.is_set():
            try:
                rows = self.db.query("SELECT * FROM tasks WHERE status='RUNNING'")
                now = Clock.wall()
                for t in rows:
                    if t["lease_token"] is None or now > (t["lease_expires"] or 0):
                        self._mark_lost(t["id"])
            except Exception as e:
                self.events.emit("supervisor", "SUPERVISOR_ERROR", {"error": repr(e)})
            time.sleep(1.0)

    def _mark_lost(self, task_id: int) -> None:
        with self.db.lock:
            self.db.execute("UPDATE tasks SET status='LOST', lease_token=NULL WHERE id=?", (task_id,))
            self.db.commit()
        rows = self.db.query(
            "SELECT id FROM grants WHERE task_id=? AND kind='TASK_LEASE' AND status='ACTIVE'",
            (task_id,))
        for r in rows:
            self.policy.revoke_grant(r["id"], reason="任务失联，心跳超时，租约撤销")
        self.events.emit("supervisor", "TASK_MARKED_LOST",
                         {"task_id": task_id, "heartbeat_timeout_s": self.heartbeat_timeout},
                         target=f"task:{task_id}")

    # ---------- 恢复：重新确认读取权，签发新租约新令牌 ----------
    def resume_task(self, task_id: int) -> dict:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if task is None:
            raise KeyError("任务不存在")
        if task["status"] != "LOST":
            raise RuntimeError(f"任务当前状态 {task['status']}，只有 LOST 可以恢复")
        import os
        input_ids = json.loads(task["input_ids"])
        checks = []
        denied = []
        for pid in input_ids:
            p = self.db.query_one("SELECT * FROM partitions WHERE id=?", (pid,))
            ok, reason = True, ""
            if p is None or p["status"] == "DELETED":
                ok, reason = False, "分区已被回收"
            elif not os.path.exists(p["file_path"]):
                ok, reason = False, "物理文件不存在"
            elif not os.access(p["file_path"], os.R_OK):
                ok, reason = False, "进程无读取权限"
            checks.append({"partition_id": pid, "allowed": ok, "reason": reason})
            if not ok:
                denied.append({"partition_id": pid, "reason": reason})

        if denied:
            with self.db.lock:
                self.db.execute(
                    "UPDATE tasks SET status='FAILED', fail_reason=? WHERE id=?",
                    ("恢复时读取权重新确认失败: " + json.dumps(denied, ensure_ascii=False), task_id),
                )
                self.db.commit()
            self.events.emit("executor", "RESUME_ACCESS_DENIED",
                             {"task_id": task_id, "checks": checks}, target=f"task:{task_id}")
            raise PermissionError(f"读取权确认未通过: {denied}")

        # 全部通过：新令牌 + 新租约（旧 TASK_LEASE 已在失联时 REVOKED，重新签发）
        token = uuid.uuid4().hex
        now_wall = Clock.wall()
        with self.db.lock:
            self.db.execute(
                "UPDATE tasks SET status='RESUMED', lease_token=?, last_heartbeat=?, lease_expires=? WHERE id=?",
                (token, now_wall, now_wall + self.heartbeat_timeout, task_id),
            )
            self.db.commit()
            for pid in input_ids:
                self.policy.issue_grant(
                    "TASK_LEASE", partition_id=pid, task_id=task_id,
                    expire_vt=self.clock.now() + 3650 * 86400.0,
                    note=f"任务#{task_id} 恢复后重新签发租约",
                )
        self.events.emit("executor", "TASK_RESUMED",
                         {"task_id": task_id, "checks": checks, "new_token": token[:8]},
                         target=f"task:{task_id}")
        # RESUMED 只是审计语义；worker 主循环以 RUNNING 为运行条件，原子置位避免窗口
        with self.db.lock:
            self.db.execute("UPDATE tasks SET status='RUNNING' WHERE id=?", (task_id,))
            self.db.commit()
        t = threading.Thread(target=self._worker, args=(task_id, token),
                             name=f"worker-{task_id}-r", daemon=True)
        self._workers[task_id] = t
        t.start()
        return {"task_id": task_id, "checks": checks, "lease_token": token}
