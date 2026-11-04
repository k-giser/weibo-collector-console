# -*- coding: utf-8 -*-
"""BlockedSpiderMiddleware 单元测试(不依赖网络)。

运行: pytest tests/test_blocked_spider_middleware.py
"""
import logging
from types import SimpleNamespace

import pytest
from scrapy import Request
from scrapy.core.spidermw import SpiderMiddlewareManager
from scrapy.exceptions import CloseSpider
from scrapy.http import Response
from scrapy.spidermiddlewares.httperror import HttpError, HttpErrorMiddleware
from scrapy.utils.project import get_project_settings

from weibo.middlewares import BlockedSpiderMiddleware
from weibo.utils.blocked_counter import BlockedCounter


class SpiderStub:
    logger = logging.getLogger('test_spider')


class FakeResponse:
    def __init__(self, status=200, url='https://s.weibo.com/weibo?q=x',
                 text='<html>ok</html>'):
        self.status = status
        self.url = url
        self.text = text


@pytest.fixture
def mw():
    mw = BlockedSpiderMiddleware()
    mw.counter = BlockedCounter(limit=2)
    return mw


def test_blocked_below_limit_passes(mw):
    mw.process_spider_input(FakeResponse(status=403), SpiderStub())
    assert mw.counter.count == 1


def test_blocked_reaches_limit_raises_close_spider(mw):
    mw.process_spider_input(FakeResponse(status=403), SpiderStub())
    with pytest.raises(CloseSpider):
        mw.process_spider_input(FakeResponse(status=403), SpiderStub())


def test_normal_response_resets_counter(mw):
    mw.process_spider_input(FakeResponse(status=403), SpiderStub())
    mw.process_spider_input(FakeResponse(status=200), SpiderStub())
    assert mw.counter.count == 0


def test_limit_zero_disabled():
    mw = BlockedSpiderMiddleware()
    mw.counter = BlockedCounter(limit=0)
    mw.process_spider_input(FakeResponse(status=403), SpiderStub())
    assert mw.counter.count == 0


def test_empty_result_page_not_counted(mw):
    """正常空结果页(200 + card-no-result)不含风控特征,不计数。"""
    mw.process_spider_input(
        FakeResponse(text='<div class="card card-no-result s-pt20b40">无结果'),
        SpiderStub())
    assert mw.counter.count == 0


def test_visitor_system_page_counted(mw):
    """登录失效页(Sina Visitor System)属于风控,计数。"""
    mw.process_spider_input(
        FakeResponse(text='Sina Visitor System 请先登录'), SpiderStub())
    assert mw.counter.count == 1


def test_login_wall_url_counted(mw):
    """302 跟随后的 sina 登录页(cookie 失效)属于风控,计数。"""
    mw.process_spider_input(
        FakeResponse(
            status=200,
            url='https://login.sina.com.cn/sso/login.php?url=...'),
        SpiderStub())
    assert mw.counter.count == 1


def test_configured_middleware_order_counts_403_before_http_error_filter():
    settings = get_project_settings()
    settings.set('WEIBO_CONSECUTIVE_BLOCKED_LIMIT', 2)
    crawler = SimpleNamespace(settings=settings, spider=SpiderStub())
    blocked = BlockedSpiderMiddleware.from_crawler(crawler)
    http_error = HttpErrorMiddleware.from_crawler(crawler)
    priorities = settings.getwithbase('SPIDER_MIDDLEWARES')
    ordered = sorted([
        (priorities['weibo.middlewares.BlockedSpiderMiddleware'], blocked),
        (priorities['scrapy.spidermiddlewares.httperror.HttpErrorMiddleware'], http_error),
    ], key=lambda entry: entry[0])
    manager = SpiderMiddlewareManager(*(mw for _, mw in ordered), crawler=crawler)
    request = Request('https://s.weibo.com/weibo?q=test')
    response = Response(request.url, status=403, request=request)

    def process_input():
        # 使用框架实际注册后的输入顺序；未达到阈值时仍让内置过滤器处理。
        for method in manager.methods['process_spider_input']:
            if method.__self__ is blocked:
                method(response, crawler.spider)
            else:
                method(response)

    with pytest.raises(HttpError):
        process_input()
    assert blocked.counter.count == 1
    with pytest.raises(CloseSpider):
        process_input()
    assert blocked.counter.count == 2
