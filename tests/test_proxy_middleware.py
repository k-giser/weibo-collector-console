# -*- coding: utf-8 -*-
"""ProxyMiddleware 逻辑单元测试(使用 fake 代理池,不依赖网络)。

运行: pytest tests/test_proxy_middleware.py
"""
import logging

import pytest
from scrapy import Request

from weibo.middlewares import ProxyMiddleware


class SpiderStub:
    logger = logging.getLogger('test_spider')


class FakeResponse:
    def __init__(self, status=200, url='https://s.weibo.com/weibo?q=x',
                 text='<html>ok</html>'):
        self.status = status
        self.url = url
        self.text = text


class FakePool:
    def __init__(self, proxies):
        self._proxies = list(proxies)
        self.failed = []

    def get_proxy(self):
        return self._proxies.pop(0) if self._proxies else None

    def mark_failed(self, p):
        self.failed.append(p)

    def size(self):
        return len(self._proxies)


@pytest.fixture
def mw():
    mw = ProxyMiddleware()
    mw.enabled = True
    mw.max_retries = 3
    mw.pool = FakePool(['http://p1:1111', 'http://p2:2222'])
    return mw


def test_process_request_assigns_proxy(mw):
    req = Request('https://s.weibo.com/weibo?q=%E6%B5%8B%E8%AF%95')
    mw.process_request(req, SpiderStub())
    assert req.meta.get('proxy') == 'http://p1:1111'
    assert req.meta.get('_proxy_request') is True


def test_image_domain_not_proxied(mw):
    img = Request('https://wx1.sinaimg.cn/large/abc.jpg')
    mw.process_request(img, SpiderStub())
    assert 'proxy' not in img.meta


def test_weibo_name_inside_external_hostname_not_proxied(mw):
    request = Request('https://weibo.com.example.test/image.jpg')
    mw.process_request(request, SpiderStub())
    assert 'proxy' not in request.meta


def test_empty_pool_clears_inherited_proxy(mw):
    mw.pool = FakePool([])
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://inherited:9'
    mw.process_request(req, SpiderStub())
    assert 'proxy' not in req.meta
    assert '_proxy_request' not in req.meta


def test_403_switches_proxy(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    mw.process_request(req, SpiderStub())  # 先分配池中第一个代理 p1
    assert req.meta['proxy'] == 'http://p1:1111'
    ret = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert isinstance(ret, Request)
    assert ret.meta['proxy'] == 'http://p2:2222'  # 换到下一个代理
    assert ret.meta['proxy_retry_count'] == 1
    assert ret.dont_filter is True
    assert mw.pool.failed == ['http://p1:1111']


def test_passport_redirect_switches_proxy(mw):
    mw.pool = FakePool(['http://p3:3333'])
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://p2:2222'
    req.meta['_proxy_request'] = True
    ret = mw.process_response(
        req,
        FakeResponse(status=200,
                     url='https://passport.weibo.com/visitor/visitor?entry=miniblog'),
        SpiderStub())
    assert isinstance(ret, Request)


def test_wconfirm_switches_proxy(mw):
    mw.pool = FakePool(['http://p4:4444'])
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://p3:3333'
    req.meta['_proxy_request'] = True
    ret = mw.process_response(req, FakeResponse(text='wconfirm'), SpiderStub())
    assert isinstance(ret, Request)


def test_normal_response_passes_through(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://p4:4444'
    req.meta['_proxy_request'] = True
    resp = FakeResponse()
    assert mw.process_response(req, resp, SpiderStub()) is resp


def test_retry_limit_falls_back_direct(mw):
    mw.pool = FakePool(['http://p5:5555'])
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://p4:4444'
    req.meta['_proxy_request'] = True
    req.meta['proxy_retry_count'] = 3  # 达到 max_retries
    ret = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert isinstance(ret, Request)
    assert 'proxy' not in ret.meta
    assert ret.meta.get('_proxy_fallback_direct') is True


def test_direct_request_blocked_no_retry(mw):
    """直连请求被风控不再重试,且清除标记让子请求可重新走代理。"""
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['_proxy_fallback_direct'] = True
    resp = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert resp.status == 403
    assert '_proxy_fallback_direct' not in req.meta


def test_exception_switches_proxy(mw):
    mw.pool = FakePool(['http://p6:6666'])
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://p5:5555'
    req.meta['_proxy_request'] = True
    ret = mw.process_exception(req, Exception('Connection refused'),
                               SpiderStub())
    assert isinstance(ret, Request)
    assert ret.meta['proxy'] == 'http://p6:6666'
    assert 'http://p5:5555' in mw.pool.failed


def test_disabled_middleware_bypasses():
    mw = ProxyMiddleware()
    mw.enabled = False
    mw.pool = None
    req = Request('https://s.weibo.com/weibo?q=x')
    assert mw.process_request(req, SpiderStub()) is None
    assert 'proxy' not in req.meta


@pytest.fixture
def gw_mw():
    """固定网关(付费)模式的中间件,不建代理池。"""
    mw = ProxyMiddleware()
    mw.enabled = True
    mw.max_retries = 3
    mw.gateway = 'http://tps.gateway.example:8888'
    mw.pool = None
    return mw


def test_gateway_assigns_proxy(gw_mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    gw_mw.process_request(req, SpiderStub())
    assert req.meta.get('proxy') == 'http://tps.gateway.example:8888'
    assert req.meta.get('_proxy_request') is True


def test_gateway_image_domain_bypassed(gw_mw):
    img = Request('https://wx1.sinaimg.cn/large/abc.jpg')
    gw_mw.process_request(img, SpiderStub())
    assert 'proxy' not in img.meta


def test_gateway_blocked_passes_through(gw_mw):
    """网关模式被风控时不换代理重试,直接放行给爬虫。"""
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://tps.gateway.example:8888'
    req.meta['_proxy_request'] = True
    resp = gw_mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert resp.status == 403
    assert 'proxy_retry_count' not in req.meta


def test_gateway_exception_goes_to_retry_middleware(gw_mw):
    """网关连接失败不换代理,交给 RetryMiddleware 默认重试。"""
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://tps.gateway.example:8888'
    req.meta['_proxy_request'] = True
    ret = gw_mw.process_exception(req, Exception('Connection refused'),
                                  SpiderStub())
    assert ret is None
