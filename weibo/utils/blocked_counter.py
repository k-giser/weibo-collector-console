# -*- coding: utf-8 -*-
"""线程安全的连续风控计数器。

用于统计"连续多少次风控响应最终到达爬虫"(所有换 cookie / 换代理
重试都用尽后),达到阈值时由 spider 中间件触发自动停止。
"""
import threading


class BlockedCounter(object):
    """连续风控计数器。limit 为 0 表示禁用(不计数)。"""

    def __init__(self, limit=0):
        self.limit = limit
        self._count = 0
        self._lock = threading.Lock()

    def hit(self):
        """记录一次风控,返回当前连续次数。"""
        with self._lock:
            self._count += 1
            return self._count

    def reset(self):
        """收到正常响应时清零,返回清零前的连续次数。"""
        with self._lock:
            prev = self._count
            self._count = 0
            return prev

    @property
    def count(self):
        with self._lock:
            return self._count
