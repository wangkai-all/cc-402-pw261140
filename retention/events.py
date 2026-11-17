"""append-only 审计日志：所有关键动作都留痕，事件永不更新、永不删除。"""
from __future__ import annotations

import json

from .clock import Clock
from .db import Database


class EventLog:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def emit(self, actor: str, action: str, detail: dict, target: str | None = None) -> int:
        with self.db.lock:
            cur = self.db.execute(
                """INSERT INTO events (ts_virtual, ts_wall, actor, action, target, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.clock.now(), Clock.wall(), actor, action, target, json.dumps(detail, ensure_ascii=False)),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out
