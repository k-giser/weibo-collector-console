# -*- coding: utf-8 -*-
"""新版 s.weibo.com 页面解析测试(基于 2026-08-21 抓取的真实页面 fixture)。

背景:2026-08 起 s.weibo.com 对同一查询会返回多种页面变体,旧逻辑把
节流/骨架页(200 但无有效卡片)当正常页静默跳过导致漏抓。本测试覆盖:
- 真实页面解析产量(fixtures/v*.html 为真实响应)
- 空结果页识别(card-no-result)
- 异常页检测与有限重试(dont_filter、重试计数、耗尽放弃)
- 中间件对异常页的连续计数与自动停止

运行: pytest tests/test_page_parsing.py
"""
import os
from datetime import datetime

import pytest
import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import TextResponse
from scrapy.settings import Settings

from weibo.middlewares import BlockedSpiderMiddleware
from weibo.utils.blocked_counter import BlockedCounter
from weibo.utils.util import standardize_date

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')

# 真实页面 fixture(v*:正常页,throttle_*:正常页,empty_genuine:空结果页)
REAL_PAGE_FIXTURES = [
    'v1_0731_p1.html', 'v2_0731_p2.html', 'v3_0803_p1.html',
    'v4_0729_p1.html', 'v5_0803_p5.html', 'v6_flood_0731_p1.html',
    'v7_odd_scope.html', 'v8_empty_20230101.html',
    'throttle_1.html', 'throttle_2.html', 'throttle_3.html',
]


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
        return f.read()


def make_spider():
    """最小配置实例化真实 SearchSpider(不连网)。"""
    from weibo.spiders.search import SearchSpider
    settings = Settings({
        'KEYWORD_LIST': ['北京暴雨'],
        'WEIBO_TYPE': 1,
        'CONTAIN_TYPE': 0,
        'FURTHER_THRESHOLD': 40,
        'LIMIT_RESULT': 0,
        'START_DATE': '2023-07-29',
        'END_DATE': '2023-08-03',
        'DEFAULT_REQUEST_HEADERS': {'cookie': 'test'},
        'WEIBO_ANOMALY_MAX_RETRIES': 2,
    })
    return SearchSpider(settings=settings)


def make_resp(html, url='https://s.weibo.com/weibo?q=北京暴雨&type=1&page=1',
              meta=None):
    """构造带 xpath 的 TextResponse,携带 request.meta。"""
    req = scrapy.Request(url, meta={'keyword': '北京暴雨', **(meta or {})})
    return TextResponse(url=url, body=html.encode('utf-8'),
                        encoding='utf-8', request=req)


SKELETON_HTML = """
<html><body>
<div class="card-wrap"><div class="card">骨架卡片,无 mid</div></div>
<div class="card-wrap"><div class="card">骨架卡片,无 mid</div></div>
<ul class="s-scroll"><li>1</li><li>2</li></ul>
</body></html>
"""


# ---------- 真实页面解析 ----------

@pytest.mark.parametrize('name', REAL_PAGE_FIXTURES)
def test_real_pages_yield_items(name):
    """真实页面(新版布局)能被 parse_weibo 正常解析出微博条目。"""
    spider = make_spider()
    resp = make_resp(load_fixture(name))
    items = list(spider.parse_weibo(resp))
    assert len(items) > 0
    for it in items:
        assert it['weibo']['id']  # mid 已解析
        assert it['weibo']['bid']
        assert it['weibo']['user_id']
        assert it['weibo']['text']


def test_first_card_created_at_new_format():
    """新版中文时间格式 '2023年07月31日 23:59' 被标准化为 'YYYY-MM-DD HH:MM'。"""
    spider = make_spider()
    resp = make_resp(load_fixture('v1_0731_p1.html'))
    items = list(spider.parse_weibo(resp))
    assert items[0]['weibo']['created_at'] == '2023-07-31 23:59'


def test_subdivision_triggers_on_big_page():
    """s-scroll 页数 >= 阈值(40)时触发按天细分,不再解析当前页。"""
    spider = make_spider()
    resp = make_resp(load_fixture('v1_0731_p1.html'),  # s-scroll 50 页
                     meta={'base_url': 'https://s.weibo.com/weibo?q=北京暴雨'})
    out = list(spider.parse(resp))
    assert all(isinstance(r, scrapy.Request) for r in out)
    assert len(out) == 6  # 6 天细分请求(07-29 ~ 08-03)
    assert 'timescope' in out[0].url


def test_small_page_parsed_and_paginated():
    """s-scroll 页数 < 阈值时解析当前页并继续翻页(a.next)。"""
    spider = make_spider()
    resp = make_resp(load_fixture('v7_odd_scope.html'))  # s-scroll 13 页
    out = list(spider.parse_by_hour(resp))
    items = [r for r in out if isinstance(r, dict)]
    requests = [r for r in out if isinstance(r, scrapy.Request)]
    assert len(items) > 0
    assert len(requests) == 1
    assert 'page=2' in requests[0].url


PAGE_CALLBACKS = ['parse', 'parse_by_day', 'parse_by_hour', 'parse_page']


@pytest.mark.parametrize('callback_name', PAGE_CALLBACKS)
def test_page_callbacks_retry_anomaly_with_same_callback(callback_name):
    spider = make_spider()
    callback = getattr(spider, callback_name)
    out = list(callback(make_resp(SKELETON_HTML)))
    assert len(out) == 1
    assert out[0].callback == callback
    assert out[0].dont_filter
    assert out[0].meta['keyword'] == '北京暴雨'


@pytest.mark.parametrize('callback_name', PAGE_CALLBACKS)
def test_page_callbacks_stop_on_genuine_empty_page(callback_name):
    spider = make_spider()
    response = make_resp(load_fixture('empty_genuine.html'))
    assert list(getattr(spider, callback_name)(response)) == []


@pytest.mark.parametrize('callback_name', PAGE_CALLBACKS)
def test_page_callbacks_parse_and_preserve_pagination_metadata(callback_name):
    spider = make_spider()
    response = make_resp(load_fixture('v7_odd_scope.html'),
                         meta={'date': '2023-07-31'})
    out = list(getattr(spider, callback_name)(response))
    assert any(isinstance(result, dict) for result in out)
    requests = [result for result in out if isinstance(result, scrapy.Request)]
    assert len(requests) == 1
    assert requests[0].callback == spider.parse_page
    assert requests[0].meta['keyword'] == '北京暴雨'
    assert requests[0].meta['date'] == '2023-07-31'
    assert 'page=2' in requests[0].url


@pytest.mark.parametrize('callback_name', PAGE_CALLBACKS)
def test_next_page_has_independent_retry_and_download_state(callback_name):
    spider = make_spider()
    search_meta = {
        'base_url': 'https://s.weibo.com/weibo?q=北京暴雨',
        'keyword': '北京暴雨',
        'date': '2023-07-31',
        'start_time': '2023-07-31-23',
        'end_time': '2023-08-01-0',
    }
    previous_request_state = {
        'anomaly_retries': 2,
        '_cookie_retry_count': 1,
        'proxy_retry_count': 3,
        'retry_times': 2,
        'proxy': 'http://old-proxy.example:8080',
        '_proxy_request': True,
        '_proxy_fallback_direct': True,
        'redirect_times': 2,
        'redirect_urls': ['https://login.sina.com.cn/'],
        'download_latency': 1.5,
    }
    response = make_resp(load_fixture('v7_odd_scope.html'),
                         meta={**search_meta, **previous_request_state})
    requests = [result for result in getattr(spider, callback_name)(response)
                if isinstance(result, scrapy.Request)]
    assert len(requests) == 1
    next_request = requests[0]
    assert next_request.meta == search_meta
    assert next_request.dont_filter is False

    # 上一页耗尽异常重试后，下一页首次异常仍获得完整预算。
    next_response = TextResponse(url=next_request.url,
                                 body=SKELETON_HTML.encode('utf-8'),
                                 encoding='utf-8', request=next_request)
    retries = list(next_request.callback(next_response))
    assert len(retries) == 1
    assert retries[0].meta['anomaly_retries'] == 1
    assert retries[0].callback == spider.parse_page


def test_day_subdivision_covers_all_hours_and_next_day_boundary():
    spider = make_spider()
    response = make_resp(load_fixture('v1_0731_p1.html'), meta={
        'base_url': 'https://s.weibo.com/weibo?q=北京暴雨',
        'date': '2023-07-31',
    })
    requests = list(spider.parse_by_day(response))
    assert len(requests) == 24
    assert all(request.callback == spider.parse_by_hour for request in requests)
    assert requests[0].meta['start_time'] == '2023-07-31-0'
    assert requests[-1].meta['end_time'] == '2023-08-01-0'
    for previous, current in zip(requests, requests[1:]):
        assert previous.meta['end_time'] == current.meta['start_time']


def test_hour_is_final_subdivision_even_when_result_pages_exceed_threshold():
    spider = make_spider()
    response = make_resp(load_fixture('v1_0731_p1.html'))
    out = list(spider.parse_by_hour(response))
    assert any(isinstance(result, dict) for result in out)
    requests = [result for result in out if isinstance(result, scrapy.Request)]
    assert len(requests) == 1
    assert requests[0].callback == spider.parse_page


@pytest.mark.parametrize('callback_name', PAGE_CALLBACKS)
def test_page_callbacks_yield_last_allowed_item_before_stopping(callback_name):
    spider = make_spider()
    spider.limit_result = 2
    response = make_resp(load_fixture('v7_odd_scope.html'))
    items = []
    with pytest.raises(CloseSpider):
        for item in getattr(spider, callback_name)(response):
            items.append(item)
    assert len(items) == 2
    assert all(isinstance(item, dict) for item in items)


# ---------- 空结果页识别 ----------

def test_genuine_empty_page_detected():
    """真实空结果页(带 card-no-result)判为 empty,不解析不翻页。"""
    spider = make_spider()
    resp = make_resp(load_fixture('empty_genuine.html'))
    assert spider._page_state(resp)[0] == 'empty'
    assert list(spider.parse(resp)) == []


def test_empty_marker_not_required_for_anomaly():
    """无卡片且无 no-result 标记的骨架页判为 anomaly,而非 empty。"""
    spider = make_spider()
    resp = make_resp(SKELETON_HTML)
    assert spider._page_state(resp)[0] == 'anomaly'


# ---------- 异常页有限重试 ----------

def test_anomaly_retry_request_created():
    spider = make_spider()
    resp = make_resp(SKELETON_HTML)
    retry = spider._anomaly_retry(resp, spider.parse)
    assert retry is not None
    assert retry.dont_filter is True  # URL 未变,必须跳过去重
    assert retry.meta['anomaly_retries'] == 1
    assert retry.callback == spider.parse


def test_anomaly_retry_exhausted_after_max():
    spider = make_spider()  # WEIBO_ANOMALY_MAX_RETRIES=2
    retry = None
    resp = make_resp(SKELETON_HTML)
    for _ in range(3):
        if retry is not None:
            resp = make_resp(SKELETON_HTML, meta={'anomaly_retries':
                                                  retry.meta['anomaly_retries']})
        retry = spider._anomaly_retry(resp, spider.parse)
    assert retry is None  # 第 3 次重试耗尽


def test_parse_yields_retry_request_on_skeleton():
    """parse() 遇到骨架页产出重试请求而非静默跳过。"""
    spider = make_spider()
    resp = make_resp(SKELETON_HTML)
    out = list(spider.parse(resp))
    assert len(out) == 1
    assert isinstance(out[0], scrapy.Request)
    assert out[0].dont_filter is True


# ---------- 中间件:骨架页计入连续异常 ----------

def make_text_resp(html, status=200):
    return TextResponse(url='https://s.weibo.com/weibo?q=x',
                        body=html.encode('utf-8'), encoding='utf-8',
                        status=status)


class SpiderStub:
    import logging
    logger = logging.getLogger('test_spider')


def test_middleware_skeleton_counted():
    mw = BlockedSpiderMiddleware()
    mw.counter = BlockedCounter(limit=2)
    mw.process_spider_input(make_text_resp(SKELETON_HTML), SpiderStub())
    assert mw.counter.count == 1
    with pytest.raises(CloseSpider):
        mw.process_spider_input(make_text_resp(SKELETON_HTML), SpiderStub())


def test_middleware_skeleton_not_blocked_status():
    """非 200 的响应走原有风控判定,骨架判定不误伤。"""
    mw = BlockedSpiderMiddleware()
    mw.counter = BlockedCounter(limit=2)
    assert not mw._is_skeleton(make_text_resp(SKELETON_HTML, status=403))


def test_middleware_empty_page_not_counted():
    """正常空结果页(200 + card-no-result)不计数。"""
    mw = BlockedSpiderMiddleware()
    mw.counter = BlockedCounter(limit=5)
    mw.process_spider_input(
        make_text_resp('<div class="card card-no-result s-pt20b40">无结果'),
        SpiderStub())
    assert mw.counter.count == 0


# ---------- standardize_date 格式兼容 ----------

def test_standardize_date_new_chinese_format():
    assert standardize_date('2023年07月31日 23:59') == '2023-07-31 23:59'
    assert standardize_date('2023年8月1日 9:00') == '2023-08-01 9:00'


def test_standardize_date_old_hyphen_format():
    assert standardize_date('2023-07-31 23:59') == '2023-07-31 23:59'
    assert standardize_date('2023-07-31') == '2023-07-31'


@pytest.mark.parametrize('value', [
    '07-31 23:59', '07-31   23:59', '\n 07-31 23:59 \n',
    '7-31 23:59', '07月31日 23:59',
])
def test_standardize_date_without_year_preserves_hour(value):
    assert standardize_date(value) == '%s-07-31 23:59' % datetime.now().year


def test_real_card_without_year_preserves_time_separator():
    spider = make_spider()
    html = load_fixture('v1_0731_p1.html').replace(
        '2023年07月31日 23:59', '07-31 23:59', 1)
    items = list(spider.parse_weibo(make_resp(html)))
    assert items[0]['weibo']['created_at'] == '%s-07-31 23:59' % datetime.now().year


def test_retweet_without_year_preserves_time_separator():
    html = '''
    <div class="card-wrap" mid="200">
      <div class="card"><div class="card-feed"><div class="content">
        <div class="info"><div></div><div>
          <a href="//weibo.com/123" nick-name="转发者">转发者</a>
        </div></div>
        <div class="from"><a href="//weibo.com/123/abc">07-31 23:59</a></div>
        <p class="txt">  转发正文</p>
        <div class="card-comment">
          <div node-type="feed_list_forwardContent">
            <a href="//weibo.com/456" nick-name="原作者">原作者</a>
          </div>
          <p class="txt">  原文正文</p>
          <p class="from"><a href="//weibo.com/456/def">07-31 22:59</a></p>
          <a action-type="feed_list_like" action-data="mid=100"></a>
        </div>
      </div></div></div>
    </div>
    '''
    items = list(make_spider().parse_weibo(make_resp(html)))
    assert len(items) == 2
    assert items[0]['weibo']['created_at'] == '%s-07-31 22:59' % datetime.now().year
    assert items[1]['weibo']['created_at'] == '%s-07-31 23:59' % datetime.now().year


@pytest.mark.parametrize('contents,default_cookie,allowed', [
    ('# comment\n\nSUB=file-cookie\n', '', True),
    ('SUB=file-cookie\n', 'your_cookie_here', True),
    (None, 'SUB=default-cookie', True),
    ('# only comments\n\n', 'SUB=default-cookie', True),
    (None, '', False),
    ('', '', False),
    ('# only comments\n', '', False),
    (None, 'your_cookie_here', False),
])
def test_startup_validates_effective_cookie_file_and_fallback(
        tmp_path, contents, default_cookie, allowed):
    cookie_file = tmp_path / 'cookies.txt'
    if contents is not None:
        cookie_file.write_text(contents, encoding='utf-8')
    spider = make_spider()
    spider.project_settings.set('WEIBO_COOKIE_FILE', str(cookie_file))
    spider.project_settings.set('DEFAULT_REQUEST_HEADERS', {'cookie': default_cookie})
    if allowed:
        assert len(list(spider.start_requests())) == 1
    else:
        with pytest.raises(CloseSpider) as exc:
            list(spider.start_requests())
        assert '未配置微博Cookie' in exc.value.reason


def test_standardize_date_relative():
    import re
    assert re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$',
                    standardize_date('3小时前'))
    assert re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$',
                    standardize_date('刚刚'))
