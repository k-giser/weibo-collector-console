# 微博关键词搜索爬虫

基于 Scrapy，按关键词和时间范围采集微博搜索结果，保存微博文本、用户信息及图片。支持多关键词、自动细分时间范围、Cookie 轮换、代理和断点续爬。

已增加单人采集管理台，支持登录、任务队列、进度日志及 Cookie 热更新。Ubuntu 22.04 使用 uv 安装及后台运行方法见 [管理台使用说明](docs/admin.md)，无需 Docker。

任务上的“查看内容”支持正文、作者和日期筛选、分页、微博详情、多图放大及单张原图下载。仅查看最新版任务数据库，采集中也可手动刷新结果。

## 项目结构

```text
weibo/
  settings.py              # 采集配置，支持环境变量覆盖
  items.py                 # 微博字段
  spiders/search.py        # 搜索、时间细分、分页和内容解析
  middlewares.py           # Cookie、代理和连续风控处理
  pipelines.py             # 去重、CSV、数据库与媒体下载
  utils/
    util.py                # 搜索参数转换、关键词文件和日期处理
    cookie_manager.py      # Cookie 文件加载和轮换
    proxy_pool.py          # 代理获取、验证、轮换和刷新
    blocked_counter.py     # 连续异常计数
    sqlite_schema.py       # 后续采集使用的双表字段定义
tests/                     # 采集相关测试及离线页面样本
scrapy.cfg                 # Scrapy 项目入口
pyproject.toml             # 运行依赖与开发依赖
uv.lock                    # 固定依赖版本
.python-version            # Python 3.10
requirements.txt           # 从 uv.lock 导出的兼容依赖清单
结果文件/                  # CSV、SQLite 和可选视频输出
crawls/                    # 断点进度和代理缓存
```

## 安装与启动

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在项目目录执行（以下示例使用 Windows PowerShell）。Ubuntu 管理台部署见 [部署说明](docs/admin.md)。

```powershell
uv python install 3.10
uv sync --locked

$env:WEIBO_COOKIE = '填入已登录账号的完整 Cookie'
$env:WEIBO_KEYWORDS = '北京暴雨,北京积水'
$env:WEIBO_START_DATE = '2023-07-29'
$env:WEIBO_END_DATE = '2023-08-03'

uv run --locked python -m scrapy crawl search -s JOBDIR=crawls/search
```

未提供环境变量时，使用 `weibo/settings.py` 中的本地配置。当前默认关键词为北京洪涝相关的十个关键词，时间范围为 2023-07-29 至 2023-08-03，微博类型为原创，不限制是否含图片。

起止日期均包含当天。搜索结果页数达到 `FURTHER_THRESHOLD` 时，先拆分为逐日搜索，再拆分为逐小时搜索；小时是最小粒度。微博搜索自身的展示上限、页面异常等仍可能造成遗漏。

当前版本不提供地区筛选或 IP 属地查询；经确认，后续新建 SQLite 数据库不再保存 `ip` 字段，不会修改已有数据库。

## 关键词与时间配置

所有采集配置集中在 `weibo/settings.py`。

```python
# 分别搜索多个关键词
KEYWORD_LIST = ['北京暴雨', '北京积水']
# 搜索同时包含两个词的微博
KEYWORD_LIST = ['北京 暴雨']
# 搜索话题
KEYWORD_LIST = ['#北京暴雨#']
# 从 UTF-8 文件读取，每行一个关键词
KEYWORD_LIST = 'keyword_list.txt'

START_DATE = '2023-07-29'
END_DATE = '2023-08-03'
DOWNLOAD_DELAY = 10
FURTHER_THRESHOLD = 40
LIMIT_RESULT = 0
```

`WEIBO_KEYWORDS` 环境变量接受逗号分隔的文本或 JSON 数组。关键词文件路径请通过 `KEYWORD_LIST` 配置。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `WEIBO_KEYWORDS` | settings 中的关键词 | 分别搜索的关键词列表 |
| `WEIBO_START_DATE` | `2023-07-29` | 开始日期，`YYYY-MM-DD` |
| `WEIBO_END_DATE` | `2023-08-03` | 结束日期，`YYYY-MM-DD` |
| `WEIBO_TYPE` | `1` | 微博类型，见下表 |
| `WEIBO_CONTAIN_TYPE` | `0` | 必须包含的内容，见下表 |
| `WEIBO_FURTHER_THRESHOLD` | `40` | 触发时间细分的结果页数；越小通常请求越多 |
| `WEIBO_LIMIT_RESULT` | `0` | 解析条数上限，`0` 不限制；按去重前计数 |
| `WEIBO_LOG_LEVEL` | `INFO` | 日志级别 |
| `LOGSTATS_INTERVAL` | `10` | Scrapy 统计日志间隔，单位秒 |

| 类型配置 | 可选值 |
|---|---|
| `WEIBO_TYPE` | `0` 全部，`1` 原创，`2` 热门，`3` 关注人，`4` 认证用户，`5` 媒体，`6` 观点 |
| `CONTAIN_TYPE` | `0` 不限制，`1` 图片，`2` 视频，`3` 音乐，`4` 短链接 |

## Cookie 与异常处理

在浏览器登录微博后，打开开发者工具的 Network 面板，刷新页面，从微博请求的 Request Headers 中复制完整的 Cookie。通过管理台凭据池、`WEIBO_COOKIE` 环境变量或本地忽略文件 `cookies.local.txt` 配置，不要把真实 Cookie 写进代码或提交到 Git。

多个账号的 Cookie 可以写入 UTF-8 文本文件，每行一个完整 Cookie，空行和 `#` 开头的注释行会跳过：

```text
# cookies.txt
SUB=第一个账号的Cookie...
SUB=第二个账号的Cookie...
```

```powershell
$env:WEIBO_COOKIE_FILE = 'cookies.txt'
uv run --locked python -m scrapy crawl search -s JOBDIR=crawls/search
```

读取顺序为 Cookie 文件、`WEIBO_COOKIE` 环境变量、本地 settings 默认值。有效的 Cookie 文件可以独立使用，无需同时填写默认 Cookie。请求触发风控时，先尝试切换 Cookie，再由已启用的代理中间件尝试换代理及回退直连。持续异常达到阈值时自动停止。每个新页面独立计算重试次数，翻页不会继承上一页耗尽的重试预算。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `WEIBO_COOKIE` | 本地配置 | 单账号完整 Cookie |
| `WEIBO_COOKIE_FILE` | 项目根目录 `cookies.local.txt` | 本地忽略的多账号 Cookie 文件 |
| `WEIBO_COOKIE_MAX_RETRIES` | `1` | 单请求最多切换 Cookie 的次数 |
| `WEIBO_ANOMALY_MAX_RETRIES` | `2` | 无有效微博卡片的异常页重试次数 |
| `WEIBO_CONSECUTIVE_BLOCKED_LIMIT` | `5` | 连续风控或异常页的停止阈值，`0` 禁用 |

带明确空结果标记的页面正常结束该搜索分支；无有效卡片且没有空结果标记的页面视为异常页。收到正常响应后，连续异常计数清零。

## 代理配置

默认直连。代理仅用于微博页面请求，图片 CDN 请求直连。

```powershell
# 免费代理池
$env:WEIBO_PROXY_ENABLED = '1'

# 付费代理提取 API：返回每行一个代理的文本
$env:WEIBO_PROXY_SOURCE = 'paid'
$env:WEIBO_PROXY_API_URL = 'https://供应商的提取接口'

# 或固定网关：由供应商轮换出口，不建立本地代理池
$env:WEIBO_PROXY_GATEWAY = 'http://用户名:密码@代理域名:端口'
```

提取 API 与固定网关任选一种。配置了网关时优先使用网关。代理池支持 `http://host:port`、`https://host:port` 和纯 `host:port`，验证可用后轮换；失效时剔除，池空或重试用尽后回退直连。固定网关模式不做本地验证和轮换。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `WEIBO_PROXY_ENABLED` | `0` | `1` 启用代理 |
| `WEIBO_PROXY_SOURCE` | `free` | `free` 或 `paid` |
| `WEIBO_PROXY_LIST_URL` | free-proxy-list CDN | 代理列表地址 |
| `WEIBO_PROXY_API_URL` | 空 | 付费提取 API |
| `WEIBO_PROXY_GATEWAY` | 空 | 固定代理网关，可省略 `http://` |
| `WEIBO_PROXY_COUNTRY` | 空 | 未指定列表地址时使用的国家分类，ISO 国家代码 |
| `WEIBO_PROXY_MAX_PROXIES` | `200` | 可用池容量 |
| `WEIBO_PROXY_VERIFY_URL` | `https://httpbin.org/ip` | 验证目标 |
| `WEIBO_PROXY_VERIFY_TIMEOUT` | `8` | 验证超时，单位秒 |
| `WEIBO_PROXY_MAX_RETRIES` | `3` | 单请求最多换代理次数 |
| `WEIBO_PROXY_REFRESH_MINUTES` | `5` | 列表刷新间隔，`0` 不刷新 |

## 数据输出

默认启用的管线依次为：

```text
微博 ID 去重 → CSV → 图片下载 → SQLite
```

- CSV：`结果文件/<关键词>/<关键词>.csv`，使用 UTF-8 BOM 编码。
- SQLite：后续采集默认写入 `结果文件/weibo_new.db`。`weibo` 表每条微博一行，`weibo_image` 表每张图片一行，通过 `weibo_id` 关联，以 `seq` 保留图片顺序。
- 图片：下载后直接入库，不生成图片文件；下载失败时保留微博文本，跳过对应图片。
- 视频：可选启用 `MyVideoPipeline`，保存到 `结果文件/<关键词>/videos/`。

字段包括微博 ID、bid、用户 ID、昵称、正文、文章链接、位置、话题、@用户、转评赞、发布时间、来源、图片和视频链接、转发原文 ID、认证及会员信息。转发微博对应的原文会作为独立条目保存。

图片字节只随正在处理的条目传递，入库后释放，不在下载管线中保留全量成功图片缓存。同一图片的并发请求仍共享下载结果；完成后再次遇到该 URL 时可能重新下载。

图片表的 `image` 列直接保存原始图片字节，不打包、不转换为 Base64，也不改变图片质量。无图时不写图片行；部分下载失败时保留对应序号空缺。重复采集时，未下载成功不会清空已有图片；部分失败使用 `image_key` 来源摘要补齐，避免重排后关联错误。该摘要是临时图片 URL 的 SHA256 二进制值，不是可以打开的 URL。

微博表增加 `(created_at, id)` 索引，方便按时间查询。图片表以 `(weibo_id, seq)` 为主键，可直接读取、导出或更新单张图片。

SQLite 初始化失败会中止启动；写入失败会回滚该条微博及其图片，明确计为未保存，并立即请求关闭采集。CSV 在 SQLite 之前写入，因此两者可能存在差异；修复存储问题后应核对失败记录，断点队列不会自动补回已处理失败的条目。

去重管线在单次运行内按微博 ID 去重；SQLite 以微博 ID 为主键更新记录。CSV 以追加方式写入，跨次运行可能重复。

在 `ITEM_PIPELINES` 中启用或关闭输出类型。SQLite 文件名可通过环境变量 `SQLITE_DATABASE` 或 `settings.py` 中的同名配置修改，默认 `weibo_new.db`。已有数据库不迁移、不改表；程序只读检查目标库的版本和字段，旧版库或字段配置不一致时拒绝写入并提示使用新文件。

经确认，`SQLITE_DROP_FIELDS` 当前为 `['ip', 'pics', 'article_url', 'video_url', 'weibo_image.url']`，其余原有字段保留。它只影响新建数据库，不会删除已有表的列。调整保留字段后，应同时选择新的 `SQLITE_DATABASE` 文件名。CSV 和可选的 MySQL、MongoDB 输出保持各自原有结构。

图片可以用 `SELECT image FROM weibo_image WHERE weibo_id=? AND seq=?` 单独读取，返回的原始字节可直接显示或导出。无需读取或解包同条微博的其它图片。

URL 字段的排除标记分别为 `pics`、`article_url`、`video_url`、`weibo_image.url`，均已确认从后续新库排除。爬虫仍会在内存中临时使用 URL 下载图片或视频，不将这些原始 URL 写入 SQLite。微博 ID、bid、用户 ID、图片原始字节及图片序号均保留。

## 停止、续爬与进度

使用 `JOBDIR` 时，按一次 Ctrl+C，等待正常收尾并保存待处理队列；再次运行相同命令继续。更换搜索条件时使用新的 `JOBDIR`，避免混用旧的请求队列和去重记录。

自动停止后可更新 Cookie 或代理，再使用相同命令续爬。断点机制保存尚未处理的请求队列，不保证补抓此前已跳过的异常页面。

每隔约 5 秒输出采集进度：

```text
[进度] 阶段=按天细分 关键词=北京暴雨 已请求页=37 已解析=152 结果页数=12 | https://s.weibo.com/...
```

阶段包括初始范围、按天细分和按小时解析。已解析为去重和存储前的计数，实际保存条数以 Scrapy 统计或数据库为准；结果页数为当前搜索条件的分页数量。细分过程中可能暂时没有新条目保存。

## 测试

```powershell
uv run --locked python -m pytest -q
```

测试覆盖离线页面解析、时间细分、每页独立重试、Cookie 文件校验、连续异常停止、图片处理及 SQLite 存储，并使用真实 Scrapy 引擎验证共享图片下载和存储失败关闭。下载由离线响应替代，不访问微博。

## 项目来源

本项目基于 [dataabc/weibo-search](https://github.com/dataabc/weibo-search) 二次开发，增加了采集管理台、内容查看、图片数据库保存和 uv 部署配置。
