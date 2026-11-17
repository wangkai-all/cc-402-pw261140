"""SQLite 元数据库：只存元数据与引用台账，绝不存放业务文件本体。

真实的分区文件、聚合结果、报告、副本都在 var/data 下以普通文件存在；
回收动作必须真的 unlink 文件，而不是把这里的状态改成 DELETED 就算完成。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 策略版本（只追加新版本，旧版本留痕，便于追溯当时依据哪条规则判定）
CREATE TABLE IF NOT EXISTS policies (
    id              INTEGER PRIMARY KEY,
    zone            TEXT NOT NULL,
    retention_days  INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    note            TEXT,
    created_vt      REAL NOT NULL,   -- 生效时的虚拟时间
    superseded_vt   REAL,            -- 被新版本取代的虚拟时间
    UNIQUE(zone, version)
);

-- 分区（一个 zone 下按天的数据分片，对应真实文件）
CREATE TABLE IF NOT EXISTS partitions (
    id              INTEGER PRIMARY KEY,
    zone            TEXT NOT NULL,
    business_day    TEXT NOT NULL,   -- YYYY-MM-DD，业务日
    file_path       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    created_vt      REAL NOT NULL,
    -- RETAINED 正常在保 / EXPIRED 已过留存期但被引用 / PENDING_RECLAIM 可回收 / DELETED 已真实回收
    status          TEXT NOT NULL DEFAULT 'RETAINED',
    policy_version  INTEGER NOT NULL,
    UNIQUE(zone, business_day)
);

-- 加工任务
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- PIPELINE
    status          TEXT NOT NULL,   -- RUNNING/COMPLETED/LOST/RESUMED/FAILED
    input_ids       TEXT NOT NULL,   -- JSON: 声明要读取的分区 id 清单（鉴权边界）
    result_path     TEXT,
    last_heartbeat  REAL,            -- 真实墙钟
    lease_token     TEXT,            -- 当前租约令牌，恢复后换新令牌，旧令牌作废
    lease_expires   REAL,            -- 真实墙钟
    created_vt      REAL NOT NULL,
    completed_vt    REAL,
    fail_reason     TEXT
);

-- 加工进度（每个输入分区一行，崩溃恢复后可断点续跑）
CREATE TABLE IF NOT EXISTS task_progress (
    task_id         INTEGER NOT NULL,
    partition_id    INTEGER NOT NULL,
    state           TEXT NOT NULL,   -- PENDING/DONE/FAILED
    attempts        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(task_id, partition_id)
);

-- 封存报告
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER,
    title           TEXT NOT NULL,
    mode            TEXT NOT NULL,   -- HOLD 延迟回收 / COPY 必要副本
    file_path       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    created_vt      REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE'  -- ACTIVE / SUPERSEDED
);

-- 引用授权台账（可追溯的引用关系，只追加状态流转）
CREATE TABLE IF NOT EXISTS grants (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,   -- TASK_LEASE / REPORT_HOLD / COPY_BINDING
    partition_id    INTEGER,         -- TASK_LEASE / REPORT_HOLD 指向原件分区
    copy_id         INTEGER,         -- COPY_BINDING 指向副本
    task_id         INTEGER,
    report_id       INTEGER,
    status          TEXT NOT NULL,   -- ACTIVE/RELEASED/REVOKED/EXPIRED/RENEWED
    issued_vt       REAL NOT NULL,
    expire_vt       REAL NOT NULL,   -- 虚拟时间：引用的过期规则
    renew_count     INTEGER NOT NULL DEFAULT 0,
    renewed_by_grant INTEGER,        -- 续用产生的新 grant id
    note            TEXT
);

-- 存储单元（分区原件 + 必要副本，回收器统一处理的对象）
CREATE TABLE IF NOT EXISTS storage_units (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,   -- PARTITION / COPY
    partition_id    INTEGER,
    file_path       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    created_vt      REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'LIVE'  -- LIVE / DELETED
);

-- append-only 审计事件
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    ts_virtual      REAL NOT NULL,
    ts_wall         REAL NOT NULL,
    actor           TEXT NOT NULL,   -- policy/executor/reaper/supervisor/api
    action          TEXT NOT NULL,
    target          TEXT,
    detail          TEXT NOT NULL    -- JSON
);
"""


class Database:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
