# -*- coding: utf-8 -*-

"""微博请求的代理、Cookie 轮换及连续风控处理。"""

import logging
from urllib.parse import urlparse

from scrapy import signals
from scrapy.exceptions import CloseSpider

from weibo.utils.blocked_counter import BlockedCounter
from weibo.utils.cookie_manager import CookieManager

logger = logging.getLogger(__name__)


def _is_weibo_url(url):
    host = (urlparse(url).hostname or '').lower()
    return host == 'weibo.com' or host.endswith('.weibo.com')


class ProxyMiddleware(object):
    """代理下载中间件,支持三种代理来源(由 WEIBO_PROXY_SOURCE 决定):

    - free(默认):从免费代理列表拉取并验证,爬取时轮换使用;代理连接失败
      或触发微博风控(403、跳转 passport.weibo.com、验证码)时自动换代理
      重试,代理池耗尽时回退直连,保证爬取不中断。
    - paid + WEIBO_PROXY_API_URL:付费代理"提取 API",复用免费模式相同的
      下载-验证-轮换机制,仅列表地址不同。
    - paid + WEIBO_PROXY_GATEWAY:固定代理网关/隧道,所有请求走同一入口,
      出口 IP 由代理供应商轮换;被风控时换网关无意义,交给连续风控中间件处理,连接失败交给 RetryMiddleware 处理。

    仅对 weibo.com / s.weibo.com 页面请求走代理,图片等 CDN 域名
    (sinaimg.cn)请求不走代理。
    """

    def __init__(self):
        self.enabled = False
        self.max_retries = 3
        self.gateway = ''
        self.pool = None

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        mw.enabled = crawler.settings.getbool('WEIBO_PROXY_ENABLED', False)
        mw.max_retries = crawler.settings.getint('WEIBO_PROXY_MAX_RETRIES',
                                                  3)
        if not mw.enabled:
            return mw
        source = crawler.settings.get('WEIBO_PROXY_SOURCE', 'free')
        gateway = (crawler.settings.get('WEIBO_PROXY_GATEWAY', '') or
                   '').strip()
        if gateway:
            # 方式二:固定网关/隧道代理,不建池不验证
            mw.gateway = gateway if '://' in gateway else 'http://' + gateway
            logger.info('使用付费代理网关: %s(所有微博请求走该网关)',
                        mw.gateway)
            return mw
        from weibo.utils.proxy_pool import ProxyPool
        api_url = (crawler.settings.get('WEIBO_PROXY_API_URL', '') or
                   '').strip()
        if source == 'paid' and api_url:
            # 方式一:付费代理提取 API,复用免费列表的下载-验证-轮换机制
            logger.info('使用付费代理提取 API: %s', api_url)
            mw.pool = ProxyPool(crawler.settings, list_url=api_url)
        else:
            mw.pool = ProxyPool(crawler.settings)
        mw.pool.refresh()  # 启动时同步验证一批代理
        logger.info('代理池已初始化,可用代理 %d 个', mw.pool.size())
        mw.pool.start_refresh_thread()
        crawler.signals.connect(mw.engine_stopped,
                                signal=signals.engine_stopped)
        return mw

    def engine_stopped(self):
        if self.pool:
            self.pool.stop()

    def process_request(self, request, spider):
        if not self.enabled or (not self.pool and not self.gateway):
            return None
        if not _is_weibo_url(request.url):
            return None  # 图片/CDN 请求不走代理
        if request.meta.get('_proxy_fallback_direct'):
            # 已回退直连的请求,直接放行(直连成功后会清除该标记)
            request.meta.pop('proxy', None)
            return None
        if self.gateway:
            request.meta['proxy'] = self.gateway
            request.meta['_proxy_request'] = True
            spider.logger.debug('使用代理网关: %s -> %s', self.gateway,
                                request.url)
            return None
        proxy = self.pool.get_proxy()
        if not proxy:
            # 池空:清除可能从上级请求继承的代理,直连
            request.meta.pop('proxy', None)
            request.meta.pop('_proxy_request', None)
            return None
        request.meta['proxy'] = proxy
        request.meta['_proxy_request'] = True
        spider.logger.debug('使用代理: %s -> %s', proxy, request.url)
        return None

    def process_response(self, request, response, spider):
        if not self.enabled or (not self.pool and not self.gateway):
            return response
        if request.meta.get('_proxy_fallback_direct'):
            # 直连请求:成功或失败都清除标记,让后续子请求可重新走代理;
            # 若直连也被风控,交给连续风控中间件处理,不再重试
            request.meta.pop('_proxy_fallback_direct', None)
            return response
        if not request.meta.get('_proxy_request'):
            return response
        if self.gateway:
            # 网关出口 IP 由供应商轮换,被风控时换网关无意义,直接放行
            if self._is_blocked(response):
                spider.logger.warning(
                    '代理网关 %s 被微博风控(status=%s,url=%s),本次放行',
                    self.gateway, response.status, response.url[:100])
            return response
        if self._is_blocked(response):
            proxy = request.meta.get('proxy', '未知')
            self.pool.mark_failed(proxy)
            spider.logger.info('代理 %s 被风控(status=%s,url=%s),换代理重试',
                               proxy, response.status, response.url[:100])
            return self._retry_with_proxy(request, spider)
        return response

    def process_exception(self, request, exception, spider):
        if (not self.enabled or (not self.pool and not self.gateway)
                or not request.meta.get('_proxy_request')
                or request.meta.get('_proxy_fallback_direct')):
            return None  # 非代理请求交给其他中间件处理
        if self.gateway:
            # 网关连接失败可能是瞬时的,交给 RetryMiddleware 默认重试
            return None
        proxy = request.meta.get('proxy', '未知')
        self.pool.mark_failed(proxy)
        spider.logger.debug('代理 %s 连接失败(%s),换代理重试', proxy, exception)
        return self._retry_with_proxy(request, spider)

    @staticmethod
    def _is_blocked(response):
        """判断响应是否命中微博风控。

        302 登录跳转由 CookieMiddleware(610,先于 RedirectMiddleware 600)
        拦截;放行后的 302 会被跟随,response.url 是最终地址,据此判断
        是否落在 passport.weibo.com 或 login.sina.com.cn(登录墙)。
        """
        if response.status == 403:
            return True
        host = (urlparse(response.url).hostname or '').lower()
        if host in ('passport.weibo.com', 'login.sina.com.cn'):
            return True  # cookie 失效的典型形态:302 跟随到 sina 登录页
        try:
            text = response.text[:3000]
        except AttributeError:
            return False  # 图片等二进制响应没有 text 属性
        if 'wconfirm' in text or 'Sina Visitor System' in text:
            return True
        return False

    def _retry_with_proxy(self, request, spider):
        """换一个代理重试该请求;达到上限或池空时回退直连。"""
        retry_count = request.meta.get('proxy_retry_count', 0)
        if retry_count >= self.max_retries:
            spider.logger.warning('代理重试已达上限(%s次),回退直连: %s',
                                  self.max_retries, request.url)
            return self._fallback_direct(request)
        proxy = self.pool.get_proxy()
        if not proxy:
            spider.logger.warning('代理池已耗尽,回退直连: %s', request.url)
            return self._fallback_direct(request)
        new_req = request.replace()  # meta 浅拷贝,保留 keyword 等业务字段
        new_req.meta['proxy'] = proxy
        new_req.meta['proxy_retry_count'] = retry_count + 1
        new_req.dont_filter = True  # URL 未变,必须跳过 DupeFilter
        return new_req

    @staticmethod
    def _fallback_direct(request):
        """清除代理标记,让请求以直连方式继续。"""
        new_req = request.replace()
        new_req.meta.pop('proxy', None)
        new_req.meta.pop('_proxy_request', None)
        new_req.meta['_proxy_fallback_direct'] = True
        new_req.dont_filter = True
        return new_req


class CookieMiddleware(object):
    """多 Cookie 下载中间件(优先级 610)。

    配置 WEIBO_COOKIE_FILE(每行一个 cookie)后,所有微博请求统一携带当前
    cookie;响应命中微博风控时自动切换到下一个 cookie 重试(保留当前代理,
    同一出口 IP 试新 cookie),切换次数用完后再由 ProxyMiddleware(543)换
    代理。未配置 cookie 文件时为单 cookie(环境变量/默认值),直接放行,
    行为与默认 Scrapy 一致。

    优先级 610 的取舍:
    - process_request 升序:在 ProxyMiddleware(543)之后执行,最后写入
      cookie 头,覆盖 DefaultHeadersMiddleware(400)写入的静态默认值
    - process_response 降序:先于 RedirectMiddleware(600)拦截 302 登录
      跳转(cookie 失效的典型形态),在跟随跳转前切换 cookie 重试原 URL;
      也先于 ProxyMiddleware(543)处理风控,先切 cookie 再换代理,
      避免 cookie 失效时白白消耗代理
    """

    def __init__(self):
        self.manager = None
        self.max_retries = 1

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        mw.manager = CookieManager(crawler.settings)
        mw.max_retries = crawler.settings.getint('WEIBO_COOKIE_MAX_RETRIES',
                                                  1)
        if mw.manager.count > 1:
            logger.info('多 cookie 模式已启用(共 %d 个),风控时自动切换',
                        mw.manager.count)
        return mw

    def process_request(self, request, spider):
        if not _is_weibo_url(request.url):
            # DefaultHeadersMiddleware 也可能注入静态 Cookie，或重定向继承
            # 原请求头；图片/CDN 等外域请求必须主动清除微博凭据。
            request.headers.pop('cookie', None)
            return None
        if not self.manager:
            return None
        # 微博请求统一覆盖 cookie 头,切换后新请求自动带新 cookie
        request.headers['cookie'] = self.manager.current
        return None

    @staticmethod
    def _is_login_redirect(response):
        """302 跳转到 sina 登录墙 = cookie 失效的典型形态。

        必须在 RedirectMiddleware(600)之前拦截,否则跳转后 URL 变成
        登录页地址,重试便失去了"回到原搜索页"的意义。
        """
        location = response.headers.get('Location', '')  # Scrapy 返回 bytes
        if isinstance(location, bytes):
            location = location.decode('utf-8', 'ignore')
        return (response.status == 302 and
                (urlparse(location).hostname or '').lower() == 'login.sina.com.cn')

    def process_response(self, request, response, spider):
        if not _is_weibo_url(request.url):
            return response  # CDN 的失败不代表微博账号失效，不切换 Cookie
        if self.manager and self.manager._managed_path:
            if self._is_login_redirect(response) or ProxyMiddleware._is_blocked(response):
                value = self.manager.reject_current(request.headers.get('cookie', b'').decode('utf-8'))
                if not value:
                    from scrapy.utils.defer import deferred_from_coro
                    deferred_from_coro(spider.crawler.engine.close_spider_async(reason='cookies_exhausted'))
                    return response
                retry = request.replace(dont_filter=True)
                retry.headers['cookie'] = value
                return retry
        if not self.manager or self.manager.count <= 1:
            return response  # 单 cookie 或未配置,不做切换
        if not (self._is_login_redirect(response)
                or ProxyMiddleware._is_blocked(response)):
            return response
        retry_count = request.meta.get('_cookie_retry_count', 0)
        if retry_count >= self.max_retries:
            return response  # 切换次数用尽,放行给代理中间件/爬虫
        new_cookie = self.manager.switch()
        spider.logger.warning(
            'cookie 触发风控(status=%s,url=%s),切换 cookie 重试(第%d次)',
            response.status, response.url[:100], retry_count + 1)
        new_req = request.replace()  # meta 浅拷贝,保留 proxy 等字段
        new_req.headers['cookie'] = new_cookie
        new_req.meta['_cookie_retry_count'] = retry_count + 1
        new_req.dont_filter = True  # URL 未变,必须跳过 DupeFilter
        return new_req

    def process_exception(self, request, exception, spider):
        # 连接错误与 cookie 无关,交给其他中间件处理
        return None


class BlockedSpiderMiddleware(object):
    """连续风控 spider 中间件(优先级 40，先于 HttpErrorMiddleware)。

    下载器中间件(Cookie/Proxy)无法修复的风控响应最终会到达这里,此时
    统计连续次数;达到 WEIBO_CONSECUTIVE_BLOCKED_LIMIT(0 禁用)时抛出
    CloseSpider,由引擎走正常关闭流程(保存 JOBDIR 进度),修复 cookie /
    代理后重启即可断点续爬。收到正常响应时计数清零。

    正常空结果页(200 + card-no-result,即该时间段无微博)不含风控特征,
    不会被误判;新版 s.weibo.com(2026-08)的节流/布局变体页(200 但
    无有效微博卡片)解析必为 0 条,同样计入连续异常,避免长时间静默漏抓。
    """

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        mw.counter = BlockedCounter(
            crawler.settings.getint('WEIBO_CONSECUTIVE_BLOCKED_LIMIT', 5))
        return mw

    @staticmethod
    def _is_skeleton(response):
        """节流/布局变体页:200 但页面没有带 mid 的有效微博卡片,
        且不是正常空结果页(card-no-result)。这类页面解析必为 0 条,
        与风控响应一样计入连续异常计数。
        """
        if response.status != 200:
            return False
        if not getattr(response, 'xpath', None):
            return False
        if response.xpath('//div[@class="card card-no-result s-pt20b40"]'):
            return False
        return not response.xpath('//div[@class="card-wrap"][@mid]')

    def process_spider_input(self, response, spider):
        if self.counter.limit <= 0:
            return None
        blocked = ProxyMiddleware._is_blocked(response)
        skeleton = self._is_skeleton(response)
        if blocked or skeleton:
            count = self.counter.hit()
            spider.logger.warning('连续第 %d 次%s响应(status=%s,url=%s)',
                                  count,
                                  '风控' if blocked else '异常页(无有效卡片)',
                                  response.status, response.url[:100])
            if count >= self.counter.limit:
                spider.logger.error(
                    '连续 %d 次%s,达到上限(%d),自动停止爬取并保存进度;'
                    '请更新 cookie/代理后重启续爬', count,
                    '风控响应' if blocked else '异常页',
                    self.counter.limit)
                raise CloseSpider(
                    '连续%d次%s,自动停止并保存进度' % (
                        count, '风控响应' if blocked else '异常页'))
        else:
            previous = self.counter.reset()
            if previous:
                spider.logger.info('收到正常响应,连续风控计数(%d)已重置',
                                   previous)
        return None
