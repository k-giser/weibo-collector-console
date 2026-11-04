"""运行真实 Scrapy 引擎，下载中间件使用离线响应，不访问网络。"""
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize('mode', ['shared_image', 'without_urls', 'database_failure'])
def test_image_storage_and_failure_shutdown_in_real_engine(tmp_path, mode):
    cookie_file = tmp_path / 'cookies.txt'
    cookie_file.write_text('SUB=test-one\nSUB=test-two\n', encoding='utf-8')
    database = tmp_path / 'probe.db'
    script = textwrap.dedent('''
        import json
        import sqlite3
        import sys
        from pathlib import Path
        import scrapy
        from scrapy.crawler import CrawlerProcess
        from scrapy.http import HtmlResponse, Response
        from scrapy import signals
        import hashlib

        mode, database, cookie_file = sys.argv[1:]
        image_url = 'https://wx1.sinaimg.cn/large/offline-test.jpg'
        image_bytes = b'\\xff\\xd8\\xffoffline-image'

        class OfflineDownloader:
            downloads = 0

            def process_request(self, request, spider):
                # 所有下载都在这里截断，意外 URL 也不会发出网络请求。
                if request.url == 'https://s.weibo.com/weibo?q=offline':
                    return HtmlResponse(request.url, request=request,
                                        body=b'<html>offline</html>')
                assert request.url == image_url
                assert not request.headers.get('cookie')
                type(self).downloads += 1
                return Response(request.url, request=request, body=image_bytes,
                                headers={'Content-Type': 'image/jpeg'})

        class ProbeSpider(scrapy.Spider):
            name = 'offline-pipeline-probe'
            start_urls = ['https://s.weibo.com/weibo?q=offline']

            def parse(self, response):
                for index in range(1 if mode == 'database_failure' else 2):
                    yield {'keyword': 'offline', 'weibo': {
                        'id': str(index),
                        'bid': None if mode == 'database_failure' else str(index),
                        'text': 'offline text', 'pics': [image_url],
                        'ip': 'offline',
                        'article_url': 'https://example.test/article',
                        'video_url': 'https://example.test/video',
                    }}

        process = CrawlerProcess({
            'LOG_ENABLED': True,
            'LOG_LEVEL': 'ERROR',
            'WARN_ON_GENERATOR_RETURN_VALUE': False,
            'TELNETCONSOLE_ENABLED': False,
            'COOKIES_ENABLED': False,
            'DEFAULT_REQUEST_HEADERS': {'cookie': 'must-not-reach-images'},
            'WEIBO_COOKIE_FILE': cookie_file,
            'IMAGES_STORE': str(Path(database).parent / 'media'),
            'SQLITE_DATABASE': database,
            'SQLITE_DROP_FIELDS': (['ip', 'pics', 'article_url', 'video_url',
                                    'weibo_image.url']
                                   if mode == 'without_urls' else ['ip']),
            'ITEM_PIPELINES': {
                'weibo.pipelines.DuplicatesPipeline': 300,
                'weibo.pipelines.MyImagesPipeline': 304,
                'weibo.pipelines.SQLitePipeline': 306,
            },
            'DOWNLOADER_MIDDLEWARES': {
                'weibo.middlewares.CookieMiddleware': 610,
                '__main__.OfflineDownloader': 900,
            },
        })
        crawler = process.create_crawler(ProbeSpider)
        saved = []

        def record_saved(item, **kwargs):
            assert 'images' not in item['weibo']
            saved.append(item['weibo']['id'])

        crawler.signals.connect(record_saved, signal=signals.item_scraped)
        process.crawl(crawler)
        process.start()
        stats = crawler.stats.get_stats()
        with sqlite3.connect(database) as conn:
            posts = conn.execute('SELECT count(*) FROM weibo').fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in conn.execute('PRAGMA table_info(weibo)')}
            image_columns = {row[1] for row in conn.execute(
                'PRAGMA table_info(weibo_image)')}
            images = conn.execute('SELECT image FROM weibo_image').fetchall()
            image_keys = conn.execute('SELECT image_key FROM weibo_image').fetchall()
            associated_images = conn.execute(
                'SELECT w.id,i.seq,i.image FROM weibo w JOIN weibo_image i '
                'ON i.weibo_id=w.id ORDER BY w.id,i.seq').fetchall()
        assert tables == {'weibo', 'weibo_image'}
        assert 'ip' not in columns
        assert all(row[0] == image_bytes for row in images)
        assert len(associated_images) == len(images)
        assert all(row[0] == hashlib.sha256(image_url.encode('utf-8')).digest()
                   for row in image_keys)
        if mode == 'without_urls':
            assert not {'pics', 'article_url', 'video_url'} & columns
            assert 'url' not in image_columns
        print(json.dumps({
            'reason': stats.get('finish_reason'),
            'saved': len(saved),
            'scraped': stats.get('item_scraped_count', 0),
            'dropped': stats.get('item_dropped_count', 0),
            'posts': posts, 'images': len(images),
            'downloads': OfflineDownloader.downloads,
        }))
    ''')
    completed = subprocess.run(
        [sys.executable, '-X', 'utf8', '-c', script,
         mode, str(database), str(cookie_file)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding='utf-8', timeout=25)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    if mode != 'database_failure':
        assert result == {'reason': 'finished', 'saved': 2, 'scraped': 2,
                          'dropped': 0, 'posts': 2, 'images': 2, 'downloads': 1}, completed.stderr
    else:
        assert result == {'reason': 'sqlite_error', 'saved': 0, 'scraped': 0,
                          'dropped': 1, 'posts': 0, 'images': 0, 'downloads': 1}, completed.stderr
