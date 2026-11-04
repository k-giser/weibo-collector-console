# -*- coding: utf-8 -*-
"""BlockedCounter 单元测试。

运行: pytest tests/test_blocked_counter.py
"""
import threading

from weibo.utils.blocked_counter import BlockedCounter


def test_hit_increments():
    c = BlockedCounter(limit=5)
    assert c.hit() == 1
    assert c.hit() == 2
    assert c.count == 2


def test_reset_clears_and_returns_previous():
    c = BlockedCounter(limit=5)
    c.hit()
    c.hit()
    assert c.reset() == 2
    assert c.count == 0


def test_limit_zero_disabled():
    """limit=0 时仍可调用,由调用方判断是否启用。"""
    c = BlockedCounter(limit=0)
    assert c.hit() == 1
    assert c.limit == 0


def test_thread_safety():
    c = BlockedCounter(limit=100)
    errors = []

    def worker():
        try:
            for _ in range(1000):
                c.hit()
                c.count
                c.reset()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
