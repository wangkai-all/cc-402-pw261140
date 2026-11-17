"""双时钟：业务判定用可快进的虚拟时钟，进程活性（心跳/失联）用真实墙钟。

分区是否到期、报告续用是否过期，全部走虚拟时钟 —— 这样网页上可以快进时间
演示完整生命周期；任务是否失联走真实时间，因为"进程还活着"是物理事实，
不能被业务时钟的快进所伪造。
"""
from __future__ import annotations

import threading
import time


class Clock:
    def __init__(self, epoch: float | None = None) -> None:
        # 虚拟时钟的起点锚定在当前墙钟；offset 为累计快进秒数
        self._anchor = time.time() if epoch is None else epoch
        self._offset = 0.0
        self._lock = threading.RLock()

    # ---- 虚拟业务时钟 ----
    def now(self) -> float:
        with self._lock:
            return self._anchor + self._offset

    def advance(self, seconds: float) -> float:
        with self._lock:
            if seconds < 0:
                raise ValueError("时钟只能向前")
            self._offset += seconds
            return self.now()

    def iso(self, ts: float | None = None) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or self.now()))

    # ---- 真实墙钟（仅用于进程活性）----
    @staticmethod
    def wall() -> float:
        return time.time()
