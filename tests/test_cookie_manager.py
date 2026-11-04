# -*- coding: utf-8 -*-
"""CookieManager 单元测试(使用临时文件,不依赖网络)。

运行: pytest tests/test_cookie_manager.py
"""
import threading

import pytest
from scrapy.settings import Settings

from weibo.utils.cookie_manager import CookieManager

DEFAULT_COOKIE = 'SUB=default; ALF=0'


@pytest.fixture
def cookie_file(tmp_path):
    p = tmp_path / 'cookies.txt'
    p.write_text(
        '# 注释行\n'
        '\n'
        'SUB=cookie1; ALF=1\n'
        'SUB=cookie2; ALF=2\n'
        'SUB=cookie3; ALF=3\n',
        encoding='utf-8')
    return str(p)


def make_settings(cookie_file=None):
    return Settings({
        'WEIBO_COOKIE_FILE': cookie_file or '',
        'DEFAULT_REQUEST_HEADERS': {'cookie': DEFAULT_COOKIE},
    })


def test_load_from_file_multiple(cookie_file):
    m = CookieManager(make_settings(cookie_file))
    assert m.count == 3
    assert m.current == 'SUB=cookie1; ALF=1'


def test_load_skips_comments_and_blank_lines(cookie_file):
    """注释行和空行不参与计数。"""
    m = CookieManager(make_settings(cookie_file))
    assert [m.current] + [m.switch() for _ in range(2)] == [
        'SUB=cookie1; ALF=1', 'SUB=cookie2; ALF=2', 'SUB=cookie3; ALF=3']


def test_fallback_when_file_missing(tmp_path):
    m = CookieManager(make_settings(str(tmp_path / 'nope.txt')))
    assert m.count == 1
    assert m.current == DEFAULT_COOKIE


def test_fallback_when_file_empty(tmp_path):
    p = tmp_path / 'empty.txt'
    p.write_text('# 只有注释\n\n', encoding='utf-8')
    m = CookieManager(make_settings(str(p)))
    assert m.count == 1
    assert m.current == DEFAULT_COOKIE


def test_no_cookie_anywhere(tmp_path):
    """文件不存在且无默认 cookie 时,current 返回空字符串。"""
    m = CookieManager(Settings({'WEIBO_COOKIE_FILE': str(tmp_path / 'x')}))
    assert m.count == 0
    assert m.current == ''


def test_switch_cycles_through_and_wraps(cookie_file):
    m = CookieManager(make_settings(cookie_file))
    assert m.switch() == 'SUB=cookie2; ALF=2'
    assert m.switch() == 'SUB=cookie3; ALF=3'
    assert m.switch() == 'SUB=cookie1; ALF=1'  # 循环回到第一个


def test_switch_single_cookie_returns_same():
    m = CookieManager(make_settings())
    for _ in range(3):
        assert m.switch() == DEFAULT_COOKIE


def test_thread_safety(cookie_file):
    """并发 current/switch 不抛异常、不丢 cookie。"""
    m = CookieManager(make_settings(cookie_file))
    errors = []

    def worker():
        try:
            for _ in range(200):
                m.current
                m.switch()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
