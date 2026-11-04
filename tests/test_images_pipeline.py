# -*- coding: utf-8 -*-
"""MyImagesPipeline 单元测试(纯逻辑,不依赖网络下载)。

运行: pytest tests/test_images_pipeline.py
"""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from scrapy import Request
from scrapy.pipelines.files import FileException
from twisted.internet.defer import Deferred

from weibo.items import WeiboItem
from weibo.pipelines import MyImagesPipeline


class CrawlerStub:
    def __init__(self):
        from scrapy.settings import Settings
        self.settings = Settings({'IMAGES_STORE': './'})
        self.request_fingerprinter = object()  # MediaPipeline.__init__ 断言非空


@pytest.fixture
def pipeline():
    """Scrapy 2.17 的 ImagesPipeline 需要 store_uri + crawler(真实流程由
    from_crawler 传入);测试只调用纯逻辑方法,不触发实际下载。"""
    return MyImagesPipeline('file:///tmp', crawler=CrawlerStub())


class FakeResponse:
    def __init__(self, status=200, body=b'img',
                 url='https://wx1.sinaimg.cn/large/a.jpg'):
        self.status = status
        self.body = body
        self.url = url
        self.flags = []


def make_item(pics, weibo_id='123456789', keyword='test'):
    weibo = WeiboItem(id=weibo_id, pics=pics, text='正文')
    return {'weibo': weibo, 'keyword': keyword}


def test_get_media_requests_single_pic(pipeline):
    item = make_item(['https://wx1.sinaimg.cn/large/a.jpg'])
    requests = list(pipeline.get_media_requests(item, None))
    assert len(requests) == 1
    assert requests[0].url == 'https://wx1.sinaimg.cn/large/a.jpg'
    assert requests[0].meta['item'] is item


def test_get_media_requests_multiple_pics(pipeline):
    urls = [f'https://wx1.sinaimg.cn/large/a{i}.jpg' for i in range(3)]
    requests = list(pipeline.get_media_requests(make_item(urls), None))
    assert [r.url for r in requests] == urls


def test_get_media_requests_no_pics(pipeline):
    assert list(pipeline.get_media_requests(make_item([]), None)) == []


def test_media_downloaded_returns_bytes(pipeline):
    """下载结果直接携带图片字节,不落盘。"""
    resp = FakeResponse(body=b'image-bytes')
    req = Request('https://wx1.sinaimg.cn/large/a.jpg')
    result = asyncio.run(pipeline.media_downloaded(resp, req, None))
    assert result['data'] == b'image-bytes'
    assert result['url'] == req.url
    assert result['checksum'] == hashlib.md5(b'image-bytes').hexdigest()


def test_media_downloaded_non_200_raises(pipeline):
    resp = FakeResponse(status=403, body=b'')
    with pytest.raises(FileException):
        asyncio.run(
            pipeline.media_downloaded(resp, Request('https://x/y'), None))


def test_media_downloaded_empty_body_raises(pipeline):
    resp = FakeResponse(status=200, body=b'')
    with pytest.raises(FileException):
        asyncio.run(
            pipeline.media_downloaded(resp, Request('https://x/y'), None))


def test_item_completed_all_success(pipeline):
    item = make_item(['https://wx1.sinaimg.cn/large/a.jpg',
                      'https://wx1.sinaimg.cn/large/b.jpg'])
    results = [
        (True, {
            'url': item['weibo']['pics'][0],
            'data': b'data1',
            'checksum': 'x',
            'status': 'downloaded'
        }),
        (True, {
            'url': item['weibo']['pics'][1],
            'data': b'data2',
            'checksum': 'y',
            'status': 'downloaded'
        }),
    ]
    ret = pipeline.item_completed(results, item, None)
    assert ret is item  # 不 DropItem,返回原 item 继续传后续 pipeline
    assert item['weibo']['images'] == [
        (item['weibo']['pics'][0], b'data1'),
        (item['weibo']['pics'][1], b'data2'),
    ]


def test_item_completed_partial_failure(pipeline):
    """部分图片失败:失败位置 None 占位,不 DropItem。"""
    item = make_item(['a', 'b', 'c'])
    results = [
        (True, {
            'url': 'a',
            'data': b'data1',
            'checksum': 'x'
        }),
        (False, Exception('boom')),
        (True, {
            'url': 'c',
            'data': b'data3',
            'checksum': 'z'
        }),
    ]
    ret = pipeline.item_completed(results, item, None)
    assert ret is item
    assert item['weibo']['images'] == [('a', b'data1'), None, ('c', b'data3')]


def test_item_completed_all_failure(pipeline):
    """全部图片失败也不 DropItem,文本记录照常入库。"""
    item = make_item(['a', 'b'])
    results = [(False, Exception('boom')), (False, Exception('boom'))]
    ret = pipeline.item_completed(results, item, None)
    assert ret is item
    assert item['weibo']['images'] == [None, None]


def test_item_completed_empty_pics(pipeline):
    item = make_item([])
    ret = pipeline.item_completed([], item, None)
    assert ret is item
    assert item['weibo']['images'] == []


def test_completed_download_does_not_retain_binary_data_in_cache(pipeline):
    """连续下载不同图片时，成功缓存不随图片总量增长。"""
    info = SimpleNamespace(downloading=set(), downloaded={}, waiting={})
    for index in range(50):
        fp = str(index).encode()
        info.downloading.add(fp)
        info.waiting[fp] = []
        result = {'url': str(index), 'data': b'x' * 100_000}
        pipeline._cache_result_and_execute_waiters(result, fp, info)
        assert not info.downloaded
    assert not info.downloading
    assert not info.waiting


def test_shared_inflight_download_delivers_bytes_to_every_waiter(pipeline,
                                                               monkeypatch):
    """移除长期缓存不会让同一图片的并发等待者拿不到内容。"""
    monkeypatch.setattr('scrapy.pipelines.media.call_later',
                        lambda delay, callback, result: callback(result))
    fp = b'shared-image'
    first, second = Deferred(), Deferred()
    received = []
    first.addCallback(lambda result: received.append(result))
    second.addCallback(lambda result: received.append(result))
    info = SimpleNamespace(downloading={fp}, downloaded={},
                           waiting={fp: [first, second]})
    result = {'url': 'https://x/shared.jpg', 'data': b'shared-bytes'}
    pipeline._cache_result_and_execute_waiters(result, fp, info)
    assert received == [result, result]
    assert not info.downloaded
    for index, download in enumerate(received):
        item = make_item([result['url']], weibo_id=str(index))
        pipeline.item_completed([(True, download)], item, info)
        assert item['weibo']['images'] == [(result['url'], b'shared-bytes')]
