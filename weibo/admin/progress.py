import json
import shutil
import time
from urllib.parse import urlsplit, parse_qs
from pathlib import Path

from scrapy import signals
from scrapy.utils.defer import deferred_from_coro
from twisted.internet.task import LoopingCall


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


class Progress:
    def __init__(self, crawler):
        self.crawler = crawler
        self.folder = Path(crawler.settings['ADMIN_JOB_DIR'])
        self.started = time.time()
        self.closing = False
        try:
            self.previous = json.loads((self.folder / 'progress.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.previous = {}
        self.loop = LoopingCall(self.tick)

    @classmethod
    def from_crawler(cls, crawler):
        obj = cls(crawler)
        crawler.signals.connect(obj.opened, signal=signals.spider_opened)
        crawler.signals.connect(obj.closed, signal=signals.spider_closed)
        crawler.signals.connect(obj.response, signal=signals.response_received)
        return obj

    def opened(self, spider):
        self.loop.start(2, now=False)

    def response(self, response, request, spider):
        self.current = {key: request.meta.get(key, '') for key in
                        ('keyword', 'date', 'start_time', 'end_time')}
        self.current['url'] = response.url
        self.current['page'] = parse_qs(urlsplit(response.url).query).get('page', ['1'])[0]

    def tick(self, reason=None):
        stats = self.crawler.stats
        spider = self.crawler.spider
        value = dict(getattr(self, 'current', {}))
        for key, current in [('pages', getattr(spider, 'page_count', 0)),
                             ('saved', stats.get_value('item_scraped_count', 0)),
                             ('failed', stats.get_value('item_dropped_count', 0) +
                              stats.get_value('downloader/exception_count', 0))]:
            value[key] = self.previous.get(key, 0) + current
        value['elapsed'] = self.previous.get('elapsed', 0) + int(time.time() - self.started)
        value['reason'] = reason
        atomic_json(self.folder / 'progress.json', value)
        if not reason and not self.closing:
            stop = (self.folder / 'stop').exists()
            low_disk = shutil.disk_usage(self.folder).free < 512 * 1024 * 1024
            pool = Path(self.crawler.settings['WEIBO_ADMIN_POOL'])
            try:
                no_cookies = not any(c['enabled'] for c in json.loads(pool.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                no_cookies = True
            if stop or low_disk or no_cookies:
                self.closing = True
                deferred_from_coro(self.crawler.engine.close_spider_async(
                    reason='disk_full' if low_disk else 'cookies_exhausted' if no_cookies else 'admin_pause'))

    def closed(self, spider, reason):
        if self.loop.running:
            self.loop.stop()
        self.tick(reason)
