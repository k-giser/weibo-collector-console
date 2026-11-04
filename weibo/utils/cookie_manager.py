# -*- coding: utf-8 -*-
"""多 Cookie 管理器:从文件加载多个微博 Cookie,风控时自动循环切换。

Cookie 加载优先级:
1. WEIBO_COOKIE_FILE 指定的文件(每行一个完整 cookie,# 注释行与空行跳过)
2. WEIBO_COOKIE 环境变量(即 settings 的 DEFAULT_REQUEST_HEADERS['cookie'])
3. settings 中的默认值

切换策略:switch() 循环轮换(用尽后回到第一个),单请求的切换次数上限
由中间件(WEIBO_COOKIE_MAX_RETRIES)控制,避免无 cookie 可用的状态。
"""
import logging
import threading
import json
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class CookieManager(object):
    """线程安全的 Cookie 轮换器。"""

    def __init__(self, settings):
        self._cookies = []
        self._index = 0
        self._lock = threading.Lock()
        self._managed_path = settings.get('WEIBO_ADMIN_POOL', '')
        self._managed_revision = None
        self._failed = set()
        self._managed_entries = []
        self._last_report = 0
        file_path = (settings.get('WEIBO_COOKIE_FILE', '') or '').strip()
        if file_path:
            self._cookies = self._load_from_file(file_path)
            if self._cookies:
                logger.info('从文件加载 %d 个 cookie: %s', len(self._cookies),
                            file_path)
        if not self._cookies:
            # 回退到环境变量 / settings 默认值
            default_cookie = (settings.get('DEFAULT_REQUEST_HEADERS', {}) or
                              {}).get('cookie', '')
            if default_cookie:
                self._cookies = [default_cookie]
        if not self._cookies:
            logger.error('未配置任何微博 Cookie')

    @staticmethod
    def _load_from_file(file_path):
        """读取 cookie 文件:# 注释行与空行跳过,每行一个完整 cookie。"""
        cookies = []
        try:
            with open(file_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    cookies.append(line)
        except OSError as exc:
            logger.warning('读取 cookie 文件失败(%s),回退到环境变量配置',
                           exc)
            return []
        return cookies

    @property
    def current(self):
        """当前使用的 cookie。无 cookie 时返回空字符串。"""
        with self._lock:
            self._reload_managed()
            self._report()
            if not self._cookies:
                return ''
            return self._cookies[self._index]

    def switch(self):
        """循环切换到下一个 cookie,返回新 cookie。

        只有 1 个 cookie 时仍返回同一个(接口一致,切换次数由中间件控制)。
        """
        with self._lock:
            self._reload_managed()
            if len(self._cookies) <= 1:
                return self._cookies[0] if self._cookies else ''
            self._index = (self._index + 1) % len(self._cookies)
            return self._cookies[self._index]

    @property
    def count(self):
        with self._lock:
            self._reload_managed()
            return len(self._cookies)

    def _reload_managed(self):
        if not self._managed_path:
            return
        try:
            with open(self._managed_path, encoding='utf-8') as stream:
                entries = json.load(stream)
            revision = [(c['id'], c['value'], c.get('revision')) for c in entries if c['enabled']]
            if revision != self._managed_revision:
                self._failed.clear()
                self._managed_entries = [c for c in entries if c['enabled']]
                self._cookies = [c['value'] for c in self._managed_entries]
                self._index = 0
                self._managed_revision = revision
        except (OSError, ValueError, KeyError):
            self._cookies = []  # 管理模式禁止退回代码中旧 Cookie。

    def reject_current(self, rejected=None):
        """管理台模式下，本轮受限凭据退出候选，避免无限循环。"""
        with self._lock:
            self._reload_managed()
            if not self._cookies:
                return ''
            self._failed.add(rejected or self._cookies[self._index])
            self._cookies = [c['value'] for c in self._managed_entries
                             if c['value'] not in self._failed]
            self._index = 0
            self._report(force=True)
            return self._cookies[0] if self._cookies else ''

    def _report(self, force=False):
        if not self._managed_path or (not force and time.time() - self._last_report < 5):
            return
        current = self._cookies[self._index] if self._cookies else None
        status = {'updated': int(time.time()), 'active': next((c['id'] for c in self._managed_entries if c['value'] == current), None),
                  'rejected': [c['id'] for c in self._managed_entries if c['value'] in self._failed]}
        try:
            target = Path(self._managed_path).with_suffix('.status.json')
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps(status), encoding='utf-8')
            temp.replace(target)
            self._last_report = time.time()
        except OSError:
            pass
