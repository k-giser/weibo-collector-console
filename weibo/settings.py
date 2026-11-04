# -*- coding: utf-8 -*-
import json
import os


def env_int(name, default):
    value = os.getenv(name)
    if value is None or value == '':
        return default
    return int(value)


def env_list(name, default):
    value = os.getenv(name)
    if value is None or value == '':
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in value.split(',')]
    if isinstance(parsed, str):
        parsed = [parsed]
    return [item for item in parsed if item]

BOT_NAME = 'weibo'
SPIDER_MODULES = ['weibo.spiders']
NEWSPIDER_MODULE = 'weibo.spiders'
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False
# 默认 INFO 便于实时看到爬取进度(WEIBO_LOG_LEVEL=ERROR 可回到只报错)
LOG_LEVEL = os.getenv('WEIBO_LOG_LEVEL', 'INFO')
# Scrapy 内置进度统计输出间隔(秒):每 10 秒打印一次已爬页数/已抓条数
LOGSTATS_INTERVAL = env_int('LOGSTATS_INTERVAL', 10)
# 访问完一个页面再访问下一个时需要等待的时间，默认为10秒
DOWNLOAD_DELAY = 10
# --- 代理池配置(默认关闭,设置 WEIBO_PROXY_ENABLED=1 启用)---
# 代理来源:free = 免费代理池(默认);paid = 付费代理(大批量爬取推荐)。
# 免费代理列表参考 https://github.com/proxifly/free-proxy-list(jsdelivr CDN,
# 每 5 分钟更新);免费代理存活率低且可能被微博风控,且免费 http 代理能看到
# 明文流量(含微博 Cookie),大批量爬取建议使用可信的付费代理源。
WEIBO_PROXY_ENABLED = os.getenv('WEIBO_PROXY_ENABLED', '0') == '1'
WEIBO_PROXY_SOURCE = os.getenv('WEIBO_PROXY_SOURCE', 'free')
# 免费代理列表地址(仅 WEIBO_PROXY_SOURCE=free 时使用),每行一个代理:
# protocol://ip:port 或纯 ip:port 均可,自动补 http://
WEIBO_PROXY_LIST_URL = os.getenv(
    'WEIBO_PROXY_LIST_URL',
    'https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt')
# --- 付费代理接入(WEIBO_PROXY_SOURCE=paid 时生效,两种方式任选其一)---
# 方式一:代理供应商"提取 API"地址,返回每行一个代理的文本(格式同免费列表,
# 认证信息一般由供应商放在 URL 参数里)。复用下载-验证-轮换机制。
WEIBO_PROXY_API_URL = os.getenv('WEIBO_PROXY_API_URL', '')
# 方式二:固定代理网关/隧道,如 http://user:pass@host:port 或 host:port(自动补
# http://)。所有微博请求走该网关,由供应商自动轮换出口 IP;不验证、不轮换。
WEIBO_PROXY_GATEWAY = os.getenv('WEIBO_PROXY_GATEWAY', '')
# 代理池最大容量,防止大量无效代理拖慢切换
WEIBO_PROXY_MAX_PROXIES = env_int('WEIBO_PROXY_MAX_PROXIES', 200)
# 代理验证目标 URL,验证请求经代理发出;若返回 JSON 且含 origin 字段(如 httpbin.org/ip)
# 还会比对返回 IP 与代理 IP,自动剔除透明代理
WEIBO_PROXY_VERIFY_URL = os.getenv('WEIBO_PROXY_VERIFY_URL',
                                   'https://httpbin.org/ip')
# 代理验证超时(秒)
WEIBO_PROXY_VERIFY_TIMEOUT = env_int('WEIBO_PROXY_VERIFY_TIMEOUT', 8)
# 单个请求因代理失效/风控最多换代理的次数,超过后回退直连
WEIBO_PROXY_MAX_RETRIES = env_int('WEIBO_PROXY_MAX_RETRIES', 3)
# 代理池后台刷新间隔(分钟),0 表示不刷新
WEIBO_PROXY_REFRESH_MINUTES = env_int('WEIBO_PROXY_REFRESH_MINUTES', 5)
# 按国家过滤代理(ISO 国家代码,如 US),留空不过滤
WEIBO_PROXY_COUNTRY = os.getenv('WEIBO_PROXY_COUNTRY', '')

# --- Cookie 管理与风控自动停止 ---
# 从文件加载多个微博 cookie(每行一个完整 cookie,# 注释行与空行跳过),
# 当前 cookie 触发微博风控时自动切换下一个重试,无需人工介入。
# 未配置时回退到 WEIBO_COOKIE 环境变量(即 DEFAULT_REQUEST_HEADERS)。
WEIBO_COOKIE_FILE = os.getenv('WEIBO_COOKIE_FILE', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.local.txt'))
# 单个请求因 cookie 风控最多切换的次数,默认 1(即最多尝试 2 个不同 cookie)
WEIBO_COOKIE_MAX_RETRIES = env_int('WEIBO_COOKIE_MAX_RETRIES', 1)
# 连续风控次数阈值:换 cookie、换代理等手段都用尽后,连续 N 个风控响应
# 到达爬虫时自动停止并保存进度(JOBDIR),0 禁用
WEIBO_CONSECUTIVE_BLOCKED_LIMIT = env_int(
    'WEIBO_CONSECUTIVE_BLOCKED_LIMIT', 5)
# 异常页重试次数:200 但页面无有效微博卡片(新版 s.weibo.com 的节流/
# 布局变体页,解析必为 0 条)时,单页最多重试 N 次,连续异常由上面的
# 风控计数自动停止
WEIBO_ANOMALY_MAX_RETRIES = env_int('WEIBO_ANOMALY_MAX_RETRIES', 2)

# 代理池下载中间件。优先级 543 的取舍:
# - process_request 先于 HttpProxyMiddleware(750)设置 request.meta['proxy']
# - process_response 先于 RetryMiddleware(500)拦截风控响应,返回新 Request 换代理重试
# Cookie 中间件优先级 610:
# - process_request 在 543 之后执行,最后写入 cookie 头覆盖默认值
# - process_response 先于 RedirectMiddleware(600)拦截 302 登录跳转
#   (cookie 失效的典型形态),也先于 543 处理风控,先切 cookie 再换代理
# 禁用 OffsiteMiddleware:cookie 失效时微博 302 到 login.sina.com.cn(不在
# allowed_domains 内),offsite 过滤会吞掉跳转导致请求链断裂、爬虫静默结束;
# 本爬虫所有请求 URL 均来自自身解析,无外域风险
DOWNLOADER_MIDDLEWARES = {
    'weibo.middlewares.ProxyMiddleware': 543,
    'weibo.middlewares.CookieMiddleware': 610,
    'scrapy.downloadermiddlewares.offsite.OffsiteMiddleware': None,
}
# 连续风控自动停止的 spider 中间件:所有重试手段用尽后的风控响应到达
# 爬虫前计数,达阈值抛 CloseSpider(引擎正常收尾保存 JOBDIR 进度)。
# 必须先于 HttpErrorMiddleware(50)，否则 403 会被提前过滤，无法计数。
SPIDER_MIDDLEWARES = {
    'weibo.middlewares.BlockedSpiderMiddleware': 40,
}
DEFAULT_REQUEST_HEADERS = {
    'Accept':
    'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7',
    'cookie': os.getenv('WEIBO_COOKIE', ''),
}
ITEM_PIPELINES = {
    'weibo.pipelines.DuplicatesPipeline': 300,
    'weibo.pipelines.CsvPipeline': 301,
    # 'weibo.pipelines.MysqlPipeline': 302,
    # 'weibo.pipelines.MongoPipeline': 303,
    'weibo.pipelines.MyImagesPipeline': 304,
    # 'weibo.pipelines.MyVideoPipeline': 305,
    'weibo.pipelines.SQLitePipeline': 306
}
# 要搜索的关键词列表，可写多个, 值可以是由关键词或话题组成的列表，也可以是包含关键词的txt文件路径，
# 如'keyword_list.txt'，txt文件中每个关键词占一行
KEYWORD_LIST = env_list('WEIBO_KEYWORDS', [
    "北京洪涝",
    "北京洪水",
    "北京被淹",
    "北京降雨",
    "北京暴雨",
    "北京内涝",
    "北京积水",
    "北京水灾",
    "北京淹没",
    "北京浸水"
])  # 或者 KEYWORD_LIST = 'keyword_list.txt'
# 要搜索的微博类型，0代表搜索全部微博，1代表搜索全部原创微博，2代表热门微博，3代表关注人微博，4代表认证用户微博，5代表媒体微博，6代表观点微博
WEIBO_TYPE = env_int('WEIBO_TYPE', 1)
# 筛选结果微博中必需包含的内容，0代表不筛选，获取全部微博，1代表搜索包含图片的微博，2代表包含视频的微博，3代表包含音乐的微博，4代表包含短链接的微博
CONTAIN_TYPE = env_int('WEIBO_CONTAIN_TYPE', 0)
# 注:微博已删除地区筛选功能,region=custom 参数失效(2026-08 实测返回
# 全国结果),原 REGION / 按省细分机制已移除
# 搜索的起始日期，为yyyy-mm-dd形式，搜索结果包含该日期
START_DATE = os.getenv('WEIBO_START_DATE', '2023-07-29')
# 搜索的终止日期，为yyyy-mm-dd形式，搜索结果包含该日期
END_DATE = os.getenv('WEIBO_END_DATE', '2023-08-03')
# 进一步细分搜索的阈值，若结果页数大于等于该值，则认为结果没有完全展示，细分搜索条件重新搜索以获取更多微博。数值越大速度越快，也越有可能漏掉微博；数值越小速度越慢，获取的微博就越多。
# 建议数值大小设置在40到50之间。
FURTHER_THRESHOLD = env_int('WEIBO_FURTHER_THRESHOLD', 40)
# 爬取结果的数量限制，爬取到该数量的微博后自动停止，设置为0代表不限制
LIMIT_RESULT = env_int('WEIBO_LIMIT_RESULT', 0)
# 图片文件存储路径
IMAGES_STORE = './'
# 视频文件存储路径
FILES_STORE = './'
# 配置MongoDB数据库
# MONGO_URI = 'localhost'
# 配置MySQL数据库，以下为默认配置，可以根据实际情况更改，程序会自动生成一个名为weibo的数据库，如果想换其它名字请更改MYSQL_DATABASE值
# MYSQL_HOST = 'localhost'
# MYSQL_PORT = 3306
# MYSQL_USER = 'root'
# MYSQL_PASSWORD = '123456'
# MYSQL_DATABASE = 'weibo'
# 后续采集使用新的双表库，已有数据库保持原样。
SQLITE_DATABASE = os.getenv('SQLITE_DATABASE', 'weibo_new.db')
# 已确认排除 IP 属地及原始 URL；只影响后续新建数据库，不改现有表。
SQLITE_DROP_FIELDS = ['ip', 'pics', 'article_url', 'video_url', 'weibo_image.url']
