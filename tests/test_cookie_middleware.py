# -*- coding: utf-8 -*-
"""CookieMiddleware 逻辑单元测试(使用临时 cookie 文件,不依赖网络)。

运行: pytest tests/test_cookie_middleware.py
"""
import logging

import pytest
from scrapy import Request
from scrapy.http import Response
from scrapy.settings import Settings

from weibo.middlewares import CookieMiddleware
from weibo.utils.cookie_manager import CookieManager


class SpiderStub:
    logger = logging.getLogger('test_spider')


class FakeResponse:
    def __init__(self, status=200, url='https://s.weibo.com/weibo?q=x',
                 text='<html>ok</html>'):
        self.status = status
        self.url = url
        self.text = text
        self.headers = {}


@pytest.fixture
def cookie_file(tmp_path):
    p = tmp_path / 'cookies.txt'
    p.write_text('SUB=cookie1; ALF=1\nSUB=cookie2; ALF=2\n',
                 encoding='utf-8')
    return str(p)


@pytest.fixture
def mw(cookie_file):
    mw = CookieMiddleware()
    mw.manager = CookieManager(Settings({'WEIBO_COOKIE_FILE': cookie_file}))
    mw.max_retries = 1
    return mw


def test_process_request_sets_cookie_header(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    mw.process_request(req, SpiderStub())
    assert req.headers.get('cookie') == b'SUB=cookie1; ALF=1'


def test_process_request_overrides_existing_cookie(mw):
    req = Request('https://s.weibo.com/weibo?q=x',
                  headers={'cookie': 'old=1'})
    mw.process_request(req, SpiderStub())
    assert req.headers.get('cookie') == b'SUB=cookie1; ALF=1'


def test_blocked_switches_cookie_and_retries(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    req.headers['cookie'] = 'SUB=cookie1; ALF=1'
    ret = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert isinstance(ret, Request)
    assert ret.headers.get('cookie') == b'SUB=cookie2; ALF=2'
    assert ret.meta['_cookie_retry_count'] == 1
    assert ret.dont_filter is True


def test_retry_keeps_proxy_meta(mw):
    """切换 cookie 后保留代理,让同一出口 IP 试新 cookie。"""
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['proxy'] = 'http://p1:1111'
    req.headers['cookie'] = 'SUB=cookie1; ALF=1'
    ret = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert ret.meta['proxy'] == 'http://p1:1111'


def test_retry_limit_passes_through(mw):
    """到达切换上限后放行,交给代理中间件/爬虫处理。"""
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['_cookie_retry_count'] = 1  # 已达到 max_retries
    req.headers['cookie'] = 'SUB=cookie2; ALF=2'
    resp = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert resp.status == 403
    assert 'SUB=cookie2; ALF=2' in str(req.headers.get('cookie'))


def test_single_cookie_no_switch():
    """未配置 cookie 文件(单 cookie)时不做切换,直接放行。"""
    mw = CookieMiddleware()
    mw.manager = CookieManager(
        Settings({'DEFAULT_REQUEST_HEADERS': {'cookie': 'SUB=only'}}))
    mw.max_retries = 1
    req = Request('https://s.weibo.com/weibo?q=x')
    resp = mw.process_response(req, FakeResponse(status=403), SpiderStub())
    assert resp.status == 403


def test_normal_response_passes_through(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    resp = FakeResponse()
    assert mw.process_response(req, resp, SpiderStub()) is resp


def test_exception_passes_through(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    assert mw.process_exception(req, Exception('boom'),
                                SpiderStub()) is None


def test_login_302_switches_cookie(mw):
    """302 跳转到 sina 登录墙(cookie 失效典型形态)触发切换,且重试原 URL。"""
    req = Request('https://s.weibo.com/weibo?q=x')
    req.headers['cookie'] = 'SUB=cookie1; ALF=1'
    resp = FakeResponse(
        status=302,
        url='https://s.weibo.com/weibo?q=x',
        text='')
    resp.headers = {'Location': 'https://login.sina.com.cn/sso/login.php?url=...'}
    ret = mw.process_response(req, resp, SpiderStub())
    assert isinstance(ret, Request)
    assert ret.url == 'https://s.weibo.com/weibo?q=x'  # 原 URL,未跟随跳转
    assert ret.headers.get('cookie') == b'SUB=cookie2; ALF=2'
    assert ret.meta['_cookie_retry_count'] == 1


def test_login_302_retry_limit_passes(mw):
    req = Request('https://s.weibo.com/weibo?q=x')
    req.meta['_cookie_retry_count'] = 1
    resp = FakeResponse(status=302, url='https://s.weibo.com/weibo?q=x',
                        text='')
    resp.headers = {'Location': 'https://login.sina.com.cn/sso/login.php'}
    ret = mw.process_response(req, resp, SpiderStub())
    assert ret.status == 302  # 切换用尽,放行给 RedirectMiddleware 跟随


@pytest.mark.parametrize('url', [
    'https://wx1.sinaimg.cn/large/test.jpg',
    'https://weibo.com.example.test/image.jpg',
    'https://login.sina.com.cn/sso/login.php',
])
def test_external_request_clears_cookie_from_default_headers(mw, url):
    request = Request(url, headers={'Cookie': 'SUB=static-default'})
    mw.process_request(request, SpiderStub())
    assert 'Cookie' not in request.headers


@pytest.mark.parametrize('status', [200, 403])
def test_binary_cdn_response_does_not_switch_cookie(mw, status):
    request = Request('https://wx1.sinaimg.cn/large/test.jpg')
    response = Response(request.url, status=status, body=b'\xff\xd8\xffimage',
                        request=request, headers={'Content-Type': 'image/jpeg'})
    original_cookie = mw.manager.current
    assert mw.process_response(request, response, SpiderStub()) is response
    assert mw.manager.current == original_cookie


def test_binary_weibo_response_does_not_access_text(mw):
    request = Request('https://weibo.com/image.jpg')
    response = Response(request.url, body=b'\xff\xd8\xffimage', request=request)
    assert mw.process_response(request, response, SpiderStub()) is response


def test_login_url_in_redirect_query_does_not_switch_cookie(mw):
    request = Request('https://s.weibo.com/weibo?q=test')
    response = Response(request.url, status=302, request=request, headers={
        'Location': 'https://example.test/?next=https://login.sina.com.cn/'})
    original_cookie = mw.manager.current
    assert mw.process_response(request, response, SpiderStub()) is response
    assert mw.manager.current == original_cookie
