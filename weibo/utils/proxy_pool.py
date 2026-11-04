# -*- coding: utf-8 -*-
"""免费代理池:拉取、并发验证、轮换、失效剔除、后台刷新。

代理列表参考 https://github.com/proxifly/free-proxy-list —— 每 5 分钟更新
的免费代理列表,可通过 jsdelivr CDN 直接下载文本(每行一个
protocol://ip:port,混合 http/https/socks4/socks5)。

第一版仅支持 http/https 协议代理(Scrapy 原生支持),socks 代理从列表
过滤掉。代理经并发验证(默认访问 httpbin.org/ip)后进入可用池,供
weibo.middlewares.ProxyMiddleware 轮换使用。

安全提示:免费 http 代理是中间人,能看到明文流量(含微博 Cookie),
仅供低风险测试使用;大批量爬取建议配置可信的付费代理源
(替换 WEIBO_PROXY_LIST_URL 即可)。
"""
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

# crawls/ 目录已被 .gitignore 忽略,用作代理列表本地缓存
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_PATH = os.path.join(_PROJECT_ROOT, 'crawls', 'proxy_cache.txt')

_DEFAULT_LIST_URL = ('https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list'
                     '@main/proxies/{}/data.txt')
_HTTP_PREFIXES = ('http://', 'https://')
# 纯 ip:port 或 host:port(部分代理 API 的返回格式,不含协议和账号密码)
_HOST_PORT_RE = re.compile(r'^[\w.-]+:\d+$')


class ProxyPool(object):
    """线程安全的免费代理池。"""

    def __init__(self, settings, list_url=None):
        """list_url 可由调用方显式传入(如付费代理提取 API),否则读配置。"""
        self.list_url = list_url or settings.get('WEIBO_PROXY_LIST_URL', '')
        # 按国家过滤时优先使用 free-proxy-list 的国家分类列表
        self.country = (settings.get('WEIBO_PROXY_COUNTRY', '') or
                        '').strip().upper()
        self.max_proxies = settings.getint('WEIBO_PROXY_MAX_PROXIES', 200)
        self.verify_url = settings.get(
            'WEIBO_PROXY_VERIFY_URL', 'https://httpbin.org/ip')
        self.verify_timeout = settings.getint('WEIBO_PROXY_VERIFY_TIMEOUT', 8)
        self.refresh_minutes = settings.getint('WEIBO_PROXY_REFRESH_MINUTES',
                                               5)
        self.concurrency = 80  # 验证并发数
        self.max_verify_candidates = 800  # 单次最多验证的候选数,控制启动耗时
        self._proxies = []  # 已验证可用的代理列表
        self._index = 0  # 轮换指针
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        })
        self._stop_event = None
        self._refresh_thread = None

    # ---------- 对外接口 ----------

    def refresh(self):
        """同步刷新一次:下载列表 -> 并发验证 -> 替换可用池。"""
        try:
            raw = self._fetch_proxy_list()
            valid = self.validate_proxies(raw)
            self._replace_pool(valid)
        except Exception as exc:
            logger.error('代理池刷新失败: %s', exc)

    def start_refresh_thread(self):
        """启动后台刷新 daemon 线程,每 refresh_minutes 分钟刷新一次。"""
        if self.refresh_minutes <= 0:
            return
        self._stop_event = threading.Event()
        self._refresh_thread = threading.Thread(target=self._refresh_loop,
                                                daemon=True)
        self._refresh_thread.start()
        logger.info('代理池后台刷新线程已启动,每 %d 分钟刷新一次',
                    self.refresh_minutes)

    def stop(self):
        """停止后台刷新线程(daemon 线程进程退出时也会自动结束)。"""
        if self._stop_event:
            self._stop_event.set()

    def get_proxy(self):
        """轮换取一个可用代理,池空返回 None。"""
        with self._lock:
            if not self._proxies:
                return None
            proxy = self._proxies[self._index]
            self._index = (self._index + 1) % len(self._proxies)
            return proxy

    def mark_failed(self, proxy_url):
        """标记代理失效并从池中移除。"""
        with self._lock:
            try:
                self._proxies.remove(proxy_url)
            except ValueError:
                pass  # 可能已被刷新线程替换
            if self._proxies:
                self._index %= len(self._proxies)
            else:
                self._index = 0
        logger.debug('代理失效移除: %s(剩余 %d)', proxy_url,
                     len(self._proxies))

    def size(self):
        with self._lock:
            return len(self._proxies)

    # ---------- 内部实现 ----------

    def _refresh_loop(self):
        while not self._stop_event.wait(self.refresh_minutes * 60):
            self.refresh()

    def _list_url(self):
        """代理列表下载地址:自定义地址(含付费 API)优先;
        未配置时使用免费列表,配置了国家则用国家分类列表。"""
        if self.list_url:
            return self.list_url
        if self.country:
            return _DEFAULT_LIST_URL.format('countries/' + self.country)
        return _DEFAULT_LIST_URL.format('all')

    def _fetch_proxy_list(self):
        """下载代理列表并过滤 http/https 协议,下载失败时读本地缓存兜底。"""
        url = self._list_url()
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            lines = resp.text.splitlines()
            self._write_cache('\n'.join(lines))
            logger.info('代理列表下载成功: %s(%d 条)', url, len(lines))
        except Exception as exc:
            logger.warning('代理列表下载失败(%s),读取本地缓存', exc)
            lines = self._read_cache()
            if not lines:
                logger.error('本地缓存无代理列表,代理池为空')
                return []
        proxies = self._filter_http(lines)
        logger.info('过滤后 http/https 代理 %d 条', len(proxies))
        return proxies

    @staticmethod
    def _filter_http(lines):
        """保留 http/https 代理并去重。

        兼容两种格式:带协议的 protocol://ip:port(免费列表),以及不带协议
        的纯 ip:port / host:port(部分代理 API 的返回格式,自动补 http://)。
        socks 等其他协议与含账号密码的行(user:pass@host:port)丢弃。
        """
        result = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(_HTTP_PREFIXES):
                proxy = line
            elif _HOST_PORT_RE.match(line):
                proxy = 'http://' + line
            else:
                continue
            if proxy not in result:
                result.append(proxy)
        return result

    def validate_proxies(self, proxy_list):
        """并发验证代理可用性,返回可用列表(不超过 max_proxies 条)。

        为避免启动耗时过长,单次最多验证 max_verify_candidates 个候选
        (免费代理存活率低,验证全部 2000+ 条收益有限)。
        """
        if not proxy_list:
            return []
        candidates = proxy_list[:self.max_verify_candidates]
        if len(candidates) < len(proxy_list):
            logger.info('候选代理过多,本次只验证前 %d 条', len(candidates))
        valid = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = [executor.submit(self._validate_one, p)
                       for p in candidates]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    valid.append(result)
        logger.info('代理验证完成: %d/%d 可用', len(valid), len(candidates))
        return valid[:self.max_proxies]

    def _validate_one(self, proxy_url):
        """验证单个代理:请求验证 URL 返回 200 即可用。

        若验证目标返回 JSON 且含 origin 字段(如 httpbin.org/ip),还会比对
        返回 IP 与代理 IP:不一致说明是透明代理(微博仍能看到真实 IP,
        换 IP 无效),直接剔除。
        """
        try:
            resp = self._session.get(
                self.verify_url,
                proxies={'http': proxy_url, 'https': proxy_url},
                timeout=self.verify_timeout,
            )
            if resp.status_code != 200:
                return None
            proxy_ip = proxy_url.split('://')[-1].split(':')[0]
            try:
                origin = resp.json().get('origin', '')
            except Exception:
                origin = ''
            if origin and proxy_ip and proxy_ip not in origin.split(','):
                return None  # 透明代理
            return proxy_url
        except Exception:
            return None

    def _replace_pool(self, proxies):
        with self._lock:
            self._proxies = list(proxies)
            self._index = 0

    def _write_cache(self, text):
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                f.write(text)
        except OSError as exc:
            logger.warning('代理列表缓存写入失败: %s', exc)

    def _read_cache(self):
        try:
            with open(CACHE_PATH, encoding='utf-8') as f:
                return f.read().splitlines()
        except OSError:
            return []
