# -*- coding: utf-8 -*-
"""SellerSprite MCP 调用限流器：滑动窗口，默认 8 次 / 滚动 60 秒，相邻调用 ≥ 7.5 秒。

需求 1：MCP 调用频率锁定每分钟 8 次。所有 SellerSprite 调用（含连接测试、
临时重试）必须经过本限流器；达到窗口上限时阻塞等待，不得停止任务。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Optional

OnWait = Optional[Callable[[float], None]]


class RateLimiter:
    def __init__(self, max_calls: int = 8, window_seconds: float = 60.0,
                 min_interval: float = 7.5) -> None:
        if max_calls <= 0 or window_seconds <= 0 or min_interval < 0:
            raise ValueError("限流参数必须为正")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.min_interval = min_interval
        self._timestamps: deque[float] = deque()
        self._last_call = 0.0
        self._lock = threading.Lock()

    def next_available_in(self) -> float:
        """距下次允许调用的秒数（0 表示现在可用）。线程安全，不阻塞。"""
        with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                self._timestamps.popleft()
            wait = 0.0
            if len(self._timestamps) >= self.max_calls:
                wait = max(wait, self._timestamps[0] + self.window_seconds - now)
            if self._last_call > 0:
                wait = max(wait, self._last_call + self.min_interval - now)
            return max(0.0, wait)

    def record_call(self) -> None:
        """记录一次已发生的调用（调用成功后立即调用）。"""
        with self._lock:
            now = time.monotonic()
            self._timestamps.append(now)
            self._last_call = now
            # 顺带清理窗口外时间戳，防止 deque 无限增长
            while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                self._timestamps.popleft()

    def wait_until_allowed(self, on_wait: OnWait = None) -> float:
        """阻塞直到允许下一次调用，返回实际等待秒数。

        on_wait(wait_seconds) 在每个等待周期回调一次（用于 GUI 倒计时显示）。
        """
        waited = 0.0
        while True:
            wait = self.next_available_in()
            if wait <= 0:
                return waited
            if on_wait is not None:
                on_wait(wait)
            step = min(wait, 1.0)
            time.sleep(step)
            waited += step

    @property
    def calls_in_window(self) -> int:
        with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                self._timestamps.popleft()
            return len(self._timestamps)
