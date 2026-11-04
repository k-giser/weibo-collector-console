# -*- coding: utf-8 -*-
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import scrapy

import weibo.utils.util as util
from scrapy.exceptions import CloseSpider
from scrapy.utils.project import get_project_settings
from weibo.items import WeiboItem
from weibo.utils.cookie_manager import CookieManager


class SearchSpider(scrapy.Spider):
    name = 'search'
    allowed_domains = ['weibo.com']
    # 翻页仅保留搜索条件，重试、代理、重定向等状态属于单个请求。
    _SEARCH_META_KEYS = ('base_url', 'keyword', 'date', 'start_time', 'end_time')

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.configure(crawler.settings)
        return spider

    def __init__(self, *args, settings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure(settings or get_project_settings())

    def configure(self, settings):
        self.project_settings = settings
        self.keyword_list = self.load_keyword_list(
            self.project_settings.get('KEYWORD_LIST'))
        self.weibo_type = util.convert_weibo_type(
            self.project_settings.get('WEIBO_TYPE'))
        self.contain_type = util.convert_contain_type(
            self.project_settings.get('CONTAIN_TYPE'))
        self.start_date = self.project_settings.get(
            'START_DATE', datetime.now().strftime('%Y-%m-%d'))
        self.end_date = self.project_settings.get(
            'END_DATE', datetime.now().strftime('%Y-%m-%d'))
        self.further_threshold = int(
            self.project_settings.get('FURTHER_THRESHOLD', 46))
        self.limit_result = int(self.project_settings.get('LIMIT_RESULT', 0))
        # 异常页(200 但无有效卡片)单页重试次数,连续异常由中间件计数自动停止
        self.anomaly_max_retries = int(
            self.project_settings.get('WEIBO_ANOMALY_MAX_RETRIES', 2))
        self.result_count = 0
        # 进度展示:已请求页数与当前细分阶段
        self.page_count = 0
        self.current_stage = '初始化'
        self._last_progress_time = 0
        self.mongo_error = False
        self.pymongo_error = False
        self.mysql_error = False
        self.pymysql_error = False
        self.sqlite3_error = False

    def load_keyword_list(self, keyword_list):
        """Load keywords from settings or a UTF-8 text file."""
        if not keyword_list:
            raise CloseSpider('KEYWORD_LIST不能为空，请在settings.py中配置关键词')
        if not isinstance(keyword_list, list):
            if not os.path.isabs(keyword_list):
                keyword_list = os.path.join(os.getcwd(), keyword_list)
            if not os.path.isfile(keyword_list):
                raise CloseSpider('不存在%s文件' % keyword_list)
            try:
                keyword_list = util.get_keyword_list(keyword_list)
            except ValueError as exc:
                raise CloseSpider(str(exc)) from exc
        keywords = []
        for keyword in keyword_list:
            if not keyword:
                continue
            if len(keyword) > 2 and keyword[0] == '#' and keyword[-1] == '#':
                keyword = '%23' + keyword[1:-1] + '%23'
            keywords.append(keyword)
        if not keywords:
            raise CloseSpider('KEYWORD_LIST不能为空，请至少配置一个关键词')
        return keywords

    def validate_runtime_settings(self):
        """Validate settings that are only required when the crawl starts."""
        cookie = CookieManager(self.project_settings).current.strip()
        if not cookie or cookie == 'your_cookie_here':
            raise CloseSpider(
                '未配置微博Cookie。请配置WEIBO_COOKIE_FILE、环境变量WEIBO_COOKIE，或本地settings.py中的DEFAULT_REQUEST_HEADERS["cookie"]')
        if util.str_to_time(self.start_date) > util.str_to_time(self.end_date):
            raise CloseSpider(
                'settings.py配置错误，START_DATE值应早于或等于END_DATE值，请重新配置settings.py')

    def check_limit(self):
        """检查是否达到爬取结果数量限制"""
        if self.limit_result > 0 and self.result_count >= self.limit_result:
            self.logger.info('已达到爬取结果数量限制：%s条，停止爬取', self.limit_result)
            raise CloseSpider('已达到爬取结果数量限制')
        return False

    def _page_state(self, response):
        """统一页面健康判断,返回 (state, page_count, next_url)。

        s.weibo.com(2026-08 新版)对同一查询会返回多种页面变体:
        - 正常页:卡片带 mid 属性,可正常解析
        - 空结果页:带 card-no-result 标记(该时间段确实无微博)
        - 节流/异常页:200 但只有无 mid 的骨架卡片,解析必为 0 条,
          旧逻辑会把它当正常页静默跳过,造成漏抓;这里标记为
          'anomaly',由调用方有限重试
        """
        if response.xpath('//div[@class="card card-no-result s-pt20b40"]'):
            return 'empty', 0, None
        real_cards = len(response.xpath('//div[@class="card-wrap"][@mid]'))
        if not real_cards:
            return 'anomaly', 0, None
        page_count = len(response.xpath('//ul[@class="s-scroll"]/li'))
        next_url = response.xpath('//a[@class="next"]/@href').extract_first()
        return 'ok', page_count, next_url

    def _anomaly_retry(self, response, callback):
        """异常页(节流/布局变体)有限重试。

        重试必须 dont_filter=True 跳过 DupeFilter(URL 未变);
        连续异常会由 BlockedSpiderMiddleware 按风控计数自动停止,
        这里只负责瞬时抖动的自愈。
        """
        retries = response.meta.get('anomaly_retries', 0) + 1
        if retries <= self.anomaly_max_retries:
            self.logger.warning('页面异常(无有效微博卡片),重试 %d/%d: %s',
                                retries, self.anomaly_max_retries,
                                response.url[:120])
            return response.request.replace(
                callback=callback,
                dont_filter=True,
                meta={**response.meta, 'anomaly_retries': retries})
        self.logger.error('页面异常且重试耗尽(疑似持续节流),跳过该页: %s',
                          response.url[:120])
        return None

    def _log_progress(self, response):
        """实时输出爬取进度(5 秒节流,配合 LOGSTATS_INTERVAL 一起看)。

        大时间范围时程序会递归细分(满 50 页 -> 按天 -> 按小时),细分阶段
        可能长时间没有数据入库,进度行让用户知道程序仍在工作而不是卡死。
        """
        self.page_count += 1
        now = time.time()
        if now - self._last_progress_time < 5:
            return
        self._last_progress_time = now
        self.logger.info(
            '[进度] 阶段=%s 关键词=%s 已请求页=%d 已解析=%d 结果页数=%s | %s',
            self.current_stage, response.meta.get('keyword', '?'),
            self.page_count, self.result_count,
            len(response.xpath('//ul[@class="s-scroll"]/li')),
            response.url[:120])

    async def start(self):
        """Scrapy 2.13+ 引擎消费异步 start() 生成器。

        新版本 Scrapy 不再调用同步的 start_requests()，默认 start() 只读取
        start_urls，导致仅定义了 start_requests() 的爬虫不产生任何请求。
        此处包装 start_requests() 以兼容新旧版本 Scrapy。
        """
        for request in self.start_requests():
            yield request

    def start_requests(self):
        self.validate_runtime_settings()
        start_date = datetime.strptime(self.start_date, '%Y-%m-%d')
        end_date = datetime.strptime(self.end_date,
                                     '%Y-%m-%d') + timedelta(days=1)
        start_str = start_date.strftime('%Y-%m-%d') + '-0'
        end_str = end_date.strftime('%Y-%m-%d') + '-0'
        for keyword in self.keyword_list:
            base_url = 'https://s.weibo.com/weibo?q=%s' % keyword
            url = base_url + self.weibo_type
            url += self.contain_type
            url += '&timescope=custom:{}:{}'.format(start_str, end_str)
            yield scrapy.Request(url=url,
                                 callback=self.parse,
                                 meta={
                                     'base_url': base_url,
                                     'keyword': keyword
                                 })

    def check_environment(self):
        """判断配置要求的软件是否已安装"""
        if self.pymongo_error:
            self.logger.error('系统中可能没有安装pymongo库，请先运行 pip install pymongo ，再运行程序')
            raise CloseSpider()
        if self.mongo_error:
            self.logger.error('系统中可能没有安装或启动MongoDB数据库，请先根据系统环境安装或启动MongoDB，再运行程序')
            raise CloseSpider()
        if self.pymysql_error:
            self.logger.error('系统中可能没有安装pymysql库，请先运行 pip install pymysql ，再运行程序')
            raise CloseSpider()
        if self.mysql_error:
            self.logger.error('系统中可能没有安装或正确配置MySQL数据库，请先根据系统环境安装或配置MySQL，再运行程序')
            raise CloseSpider()
        if self.sqlite3_error:
            self.logger.error(
                '系统中可能没有安装或正确配置SQLite3数据库，请检查SQLITE_DATABASE配置后再运行程序')
            raise CloseSpider()

    def parse(self, response):
        self.current_stage = '日粒度'
        yield from self._parse_search_page(
            response, self.parse, subdivide=self._requests_by_day)

    def parse_by_day(self, response):
        """以天为单位搜索，结果过多时继续拆分到小时。"""
        self.current_stage = '按天细分'
        yield from self._parse_search_page(
            response, self.parse_by_day, subdivide=self._requests_by_hour)

    def parse_by_hour(self, response):
        """小时为最小搜索粒度，直接解析并翻页。"""
        self.current_stage = '按小时解析'
        yield from self._parse_search_page(response, self.parse_by_hour)

    def parse_page(self, response):
        """继续解析当前搜索条件的后续页面。"""
        yield from self._parse_search_page(response, self.parse_page)

    def _parse_search_page(self, response, callback, subdivide=None):
        """统一处理页面状态、细分、微博解析与翻页。"""
        self._log_progress(response)
        state, page_count, next_url = self._page_state(response)
        if state == 'empty':
            self.logger.info('当前页面搜索结果为空: %s', response.url)
            return
        if state == 'anomaly':
            req = self._anomaly_retry(response, callback)
            if req:
                yield req
            return
        if subdivide is not None and page_count >= self.further_threshold:
            yield from subdivide(response)
            return
        for weibo in self.parse_weibo(response):
            self.check_environment()
            yield weibo
        if next_url:
            self.check_limit()
            yield response.follow(next_url, callback=self.parse_page,
                                  meta={key: response.meta[key]
                                        for key in self._SEARCH_META_KEYS
                                        if key in response.meta})

    def _requests_by_day(self, response):
        base_url = response.meta.get('base_url')
        keyword = response.meta.get('keyword')
        start_date = datetime.strptime(self.start_date, '%Y-%m-%d')
        end_date = datetime.strptime(self.end_date, '%Y-%m-%d')
        while start_date <= end_date:
            date = start_date.strftime('%Y-%m-%d')
            start_str = date + '-0'
            start_date += timedelta(days=1)
            end_str = start_date.strftime('%Y-%m-%d') + '-0'
            url = base_url + self.weibo_type + self.contain_type
            url += '&timescope=custom:{}:{}&page=1'.format(start_str, end_str)
            yield scrapy.Request(url=url, callback=self.parse_by_day,
                                 meta={'base_url': base_url, 'keyword': keyword,
                                       'date': date})

    def _requests_by_hour(self, response):
        base_url = response.meta.get('base_url')
        keyword = response.meta.get('keyword')
        start_date = datetime.strptime(response.meta['date'], '%Y-%m-%d')
        for _ in range(24):
            start_str = '{}-{}'.format(start_date.strftime('%Y-%m-%d'),
                                       start_date.hour)
            start_date += timedelta(hours=1)
            end_str = '{}-{}'.format(start_date.strftime('%Y-%m-%d'),
                                     start_date.hour)
            url = base_url + self.weibo_type + self.contain_type
            url += '&timescope=custom:{}:{}&page=1'.format(start_str, end_str)
            yield scrapy.Request(url=url, callback=self.parse_by_hour,
                                 meta={'base_url': base_url, 'keyword': keyword,
                                       'start_time': start_str,
                                       'end_time': end_str})

    def get_article_url(self, selector):
        """获取微博头条文章url"""
        article_url = ''
        text = (selector.xpath('string(.)').extract_first() or '').replace(
            '\u200b', '').replace('\ue627', '').replace('\n',
                                                        '').replace(' ', '')
        if text.startswith('发布了头条文章'):
            urls = selector.xpath('.//a')
            for url in urls:
                if url.xpath(
                        'i[@class="wbicon"]/text()').extract_first() == 'O':
                    if url.xpath('@href').extract_first() and url.xpath(
                            '@href').extract_first().startswith('http://t.cn'):
                        article_url = url.xpath('@href').extract_first()
                    break
        return article_url

    def get_location(self, selector):
        """获取微博发布位置"""
        a_list = selector.xpath('.//a')
        location = ''
        for a in a_list:
            if a.xpath('./i[@class="wbicon"]') and a.xpath(
                    './i[@class="wbicon"]/text()').extract_first() == '2':
                location = a.xpath('string(.)').extract_first()[1:]
                break
        return location

    def get_at_users(self, selector):
        """获取微博中@的用户昵称"""
        a_list = selector.xpath('.//a')
        at_users = ''
        at_list = []
        for a in a_list:
            href = a.xpath('@href').extract_first() or ''
            text = a.xpath('string(.)').extract_first() or ''
            if len(unquote(href)) > 14 and len(text) > 1:
                if unquote(href)[14:] == text[1:]:
                    at_user = text[1:]
                    if at_user not in at_list:
                        at_list.append(at_user)
        if at_list:
            at_users = ','.join(at_list)
        return at_users

    def get_topics(self, selector):
        """获取参与的微博话题"""
        a_list = selector.xpath('.//a')
        topics = ''
        topic_list = []
        for a in a_list:
            text = a.xpath('string(.)').extract_first() or ''
            if len(text) > 2 and text[0] == '#' and text[-1] == '#':
                if text[1:-1] not in topic_list:
                    topic_list.append(text[1:-1])
        if topic_list:
            topics = ','.join(topic_list)
        return topics

    def get_vip(self, selector):
        """获取用户的VIP类型和等级信息"""
        vip_type = "非会员"
        vip_level = 0

        vip_container = selector.xpath('.//div[@class="user_vip_icon_container"]')
        if vip_container:
            svvip_img = vip_container.xpath('.//img[contains(@src, "svvip_")]')
            if svvip_img:
                vip_type = "超级会员"
                src = svvip_img.xpath('@src').extract_first() or ''
                level_match = re.search(r'svvip_(\d+)\.png', src)
                if level_match:
                    vip_level = int(level_match.group(1))
            else:
                vip_img = vip_container.xpath('.//img[contains(@src, "vip_")]')
                if vip_img:
                    vip_type = "会员"
                    src = vip_img.xpath('@src').extract_first() or ''
                    level_match = re.search(r'vip_(\d+)\.png', src)
                    if level_match:
                        vip_level = int(level_match.group(1))

        return vip_type, vip_level

    def extract_count(self, text):
        """Extract Weibo count text; missing or non-numeric labels become 0."""
        matches = re.findall(r'\d+.*', text or '')
        return matches[0] if matches else '0'

    def clean_weibo_text(self, selector, is_long=False):
        text = (selector.xpath('string(.)').extract_first() or '').replace(
            '\u200b', '').replace('\ue627', '')
        location = self.get_location(selector)
        if location:
            text = text.replace('2' + location, '')
        text = text[2:].replace(' ', '') if len(text) >= 2 else text.strip()
        if is_long and len(text) >= 4:
            text = text[:-4]
        return text, location

    def parse_weibo(self, response):
        """解析网页中的微博信息"""
        keyword = response.meta.get('keyword')
        for sel in response.xpath("//div[@class='card-wrap']"):
            # 检查是否达到爬取结果数量限制
            if self.check_limit():
                return

            info = sel.xpath(
                "div[@class='card']/div[@class='card-feed']/div[@class='content']/div[@class='info']"
            )
            if info:
                weibo = WeiboItem()
                weibo['id'] = sel.xpath('@mid').extract_first()
                from_href = sel.xpath(
                    './/div[@class="from"]/a[1]/@href').extract_first()
                user_href = info[0].xpath('div[2]/a/@href').extract_first()
                txt_nodes = sel.xpath('.//p[@class="txt"]')
                if not weibo['id'] or not from_href or not user_href or not txt_nodes:
                    self.logger.warning('跳过无法解析的微博卡片: %s', response.url)
                    continue
                bid = from_href.split('/')[-1].split('?')[0]
                weibo['bid'] = bid
                weibo['user_id'] = user_href.split('?')[0].split('/')[-1]
                weibo['screen_name'] = info[0].xpath(
                    'div[2]/a/@nick-name').extract_first() or ''
                # 获取VIP信息
                weibo['vip_type'], weibo['vip_level'] = self.get_vip(info[0])
                txt_sel = txt_nodes[0]
                retweet_sel = sel.xpath('.//div[@class="card-comment"]')
                retweet_txt_sel = ''
                if retweet_sel and retweet_sel[0].xpath('.//p[@class="txt"]'):
                    retweet_txt_sel = retweet_sel[0].xpath(
                        './/p[@class="txt"]')[0]
                content_full = sel.xpath(
                    './/p[@node-type="feed_list_content_full"]')

                is_long_weibo = False
                is_long_retweet = False
                if content_full:
                    if not retweet_sel:
                        txt_sel = content_full[0]
                        is_long_weibo = True
                    elif len(content_full) == 2:
                        txt_sel = content_full[0]
                        retweet_txt_sel = content_full[1]
                        is_long_weibo = True
                        is_long_retweet = True
                    elif retweet_sel[0].xpath(
                            './/p[@node-type="feed_list_content_full"]'):
                        retweet_txt_sel = retweet_sel[0].xpath(
                            './/p[@node-type="feed_list_content_full"]')[0]
                        is_long_retweet = True
                    else:
                        txt_sel = content_full[0]
                        is_long_weibo = True
                weibo['article_url'] = self.get_article_url(txt_sel)
                weibo['text'], weibo['location'] = self.clean_weibo_text(
                    txt_sel, is_long_weibo)
                weibo['at_users'] = self.get_at_users(txt_sel)
                weibo['topics'] = self.get_topics(txt_sel)
                reposts_count = sel.xpath(
                    './/a[@action-type="feed_list_forward"]/text()').extract()
                reposts_count = "".join(reposts_count)
                weibo['reposts_count'] = self.extract_count(reposts_count)
                comments_count = sel.xpath(
                    './/a[@action-type="feed_list_comment"]/text()'
                ).extract_first()
                weibo['comments_count'] = self.extract_count(comments_count)
                attitudes_count = sel.xpath(
                    './/a[@action-type="feed_list_like"]/button/span[2]/text()').extract_first()
                weibo['attitudes_count'] = self.extract_count(attitudes_count)
                created_at = sel.xpath(
                    './/div[@class="from"]/a[1]/text()').extract_first()
                created_at = re.sub(r'\s+', ' ', created_at or '').strip()
                created_at = created_at.split('前')[0]
                weibo['created_at'] = util.standardize_date(
                    created_at) if created_at else ''
                source = sel.xpath('.//div[@class="from"]/a[2]/text()'
                                   ).extract_first()
                weibo['source'] = source if source else ''
                pics = []
                is_exist_pic = sel.xpath(
                    './/div[@class="media media-piclist"]')
                if is_exist_pic:
                    pics = is_exist_pic[0].xpath('ul[1]/li/img/@src').extract()
                    pics = [pic[8:] for pic in pics]
                    pics = [
                        re.sub(r'/.*?/', '/large/', pic, 1) for pic in pics
                    ]
                    pics = ['https://' + pic for pic in pics]
                video_url = ''
                is_exist_video = sel.xpath(
                    './/div[@class="thumbnail"]//video-player').extract_first()
                if is_exist_video:
                    video_matches = re.findall(r'src:\'(.*?)\'', is_exist_video)
                    if video_matches:
                        video_url = video_matches[0].replace('&amp;', '&')
                        video_url = 'http:' + video_url
                if not retweet_sel:
                    weibo['pics'] = pics
                    weibo['video_url'] = video_url
                else:
                    weibo['pics'] = []
                    weibo['video_url'] = ''
                weibo['retweet_id'] = ''
                if retweet_sel and retweet_sel[0].xpath(
                        './/div[@node-type="feed_list_forwardContent"]/a[1]'):
                    retweet_id_data = retweet_sel[0].xpath(
                        './/a[@action-type="feed_list_like"]/@action-data'
                    ).extract_first() or ''
                    retweet_from_href = retweet_sel[0].xpath(
                        './/p[@class="from"]/a/@href').extract_first() or ''
                    retweet_info = retweet_sel[0].xpath(
                        './/div[@node-type="feed_list_forwardContent"]/a[1]'
                    )
                    if not retweet_id_data.startswith('mid=') or not retweet_from_href or not retweet_info or not retweet_txt_sel:
                        self.logger.warning('跳过无法解析的转发微博: %s', response.url)
                    else:
                        retweet = WeiboItem()
                        retweet['id'] = retweet_id_data[4:]
                        retweet['bid'] = retweet_from_href.split(
                            '/')[-1].split('?')[0]
                        info = retweet_info[0]
                        retweet_user_href = info.xpath(
                            '@href').extract_first() or ''
                        retweet['user_id'] = retweet_user_href.split('/')[-1]
                        retweet['screen_name'] = info.xpath(
                            '@nick-name').extract_first() or ''
                        retweet['vip_type'], retweet['vip_level'] = self.get_vip(
                            info)
                        retweet['article_url'] = self.get_article_url(
                            retweet_txt_sel)
                        retweet['text'], retweet['location'] = self.clean_weibo_text(
                            retweet_txt_sel, is_long_retweet)
                        retweet['at_users'] = self.get_at_users(retweet_txt_sel)
                        retweet['topics'] = self.get_topics(retweet_txt_sel)
                        reposts_count = retweet_sel[0].xpath(
                            './/ul[@class="act s-fr"]/li[1]/a[1]/text()'
                        ).extract_first()
                        retweet['reposts_count'] = self.extract_count(
                            reposts_count)
                        comments_count = retweet_sel[0].xpath(
                            './/ul[@class="act s-fr"]/li[2]/a[1]/text()'
                        ).extract_first()
                        retweet['comments_count'] = self.extract_count(
                            comments_count)
                        attitudes_count = retweet_sel[0].xpath(
                            './/a[@class="woo-box-flex woo-box-alignCenter woo-box-justifyCenter"]//span[@class="woo-like-count"]/text()'
                        ).extract_first()
                        retweet['attitudes_count'] = self.extract_count(
                            attitudes_count)
                        created_at = retweet_sel[0].xpath(
                            './/p[@class="from"]/a[1]/text()').extract_first()
                        created_at = re.sub(r'\s+', ' ', created_at or '').strip()
                        created_at = created_at.split('前')[0]
                        retweet['created_at'] = util.standardize_date(
                            created_at) if created_at else ''
                        source = retweet_sel[0].xpath(
                            './/p[@class="from"]/a[2]/text()').extract_first()
                        retweet['source'] = source if source else ''
                        retweet['pics'] = pics
                        retweet['video_url'] = video_url
                        retweet['retweet_id'] = ''
                        retweet['ip'] = ''
                        retweet['user_authentication'] = ''
                        self.result_count += 1

                        yield {'weibo': retweet, 'keyword': keyword}

                        if self.check_limit():
                            return

                        weibo['retweet_id'] = retweet['id']
                weibo['ip'] = ''

                avator = sel.xpath(
                    "div[@class='card']/div[@class='card-feed']/div[@class='avator']"
                )
                if avator:
                    user_auth = avator.xpath('.//svg/@id').extract_first()
                    if user_auth == 'woo_svg_vblue':
                        weibo['user_authentication'] = '蓝V'
                    elif user_auth == 'woo_svg_vyellow':
                        weibo['user_authentication'] = '黄V'
                    elif user_auth == 'woo_svg_vorange':
                        weibo['user_authentication'] = '红V'
                    elif user_auth == 'woo_svg_vgold':
                        weibo['user_authentication'] = '金V'
                    else:
                        weibo['user_authentication'] = '普通用户'
                else:
                    weibo['user_authentication'] = '普通用户'

                # 增加结果计数（主微博）
                self.result_count += 1

                yield {'weibo': weibo, 'keyword': keyword}

                # 检查是否达到爬取结果数量限制
                if self.check_limit():
                    return
