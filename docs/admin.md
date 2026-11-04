# Weibo Collector Console：Ubuntu 22.04 部署

在项目根目录操作。使用服务器普通账号运行，数据目录仅该账号可读写。

```bash
# 尚未安装 uv 时，使用官方安装器（安装到当前用户目录）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 在项目根目录安装固定版本的运行依赖
uv python install 3.10
uv sync --locked --no-dev

# 设置密码，然后启动管理台
uv run --frozen --no-sync python -m weibo.admin.server --data ./admin-data --set-password
uv run --frozen --no-sync python -m weibo.admin.server --data ./admin-data --host 0.0.0.0 --port 8080
```

浏览器打开 `http://虚拟机地址:8080`，登录后添加完整 Cookie、新建任务，再启动队列。端口映射指向此端口，限制访问来源为自己的内网。通过不可信网络访问时，应由已有反向代理提供 HTTPS，保护密码和 Cookie 传输。

## 后台运行

将下面内容保存为 `/etc/systemd/system/weibo-admin.service`。把 `YOUR_USER` 和 `/absolute/path/weibo-collector-console` 替换为实际账号及项目绝对路径。先完成上面的依赖同步和设置密码步骤。用 `command -v uv` 查询 uv 的实际路径，并替换下例中的 `/home/YOUR_USER/.local/bin/uv`。

```ini
[Unit]
Description=Weibo collection administration
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/absolute/path/weibo-collector-console
ExecStart=/home/YOUR_USER/.local/bin/uv run --frozen --no-sync python -m weibo.admin.server --data /absolute/path/weibo-collector-console/admin-data --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5
UMask=0077
Environment=UV_PYTHON_DOWNLOADS=never
KillMode=mixed
TimeoutStopSec=300
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now weibo-admin
sudo systemctl status weibo-admin
```

uv、Python 和项目依赖安装在当前用户目录，无需管理员权限；配置系统级 systemd 服务需要管理员协助。无管理员权限时可以先以前台方式运行。服务重启后队列暂停，中断任务显示“待恢复”，不会自动采集。停止服务时等待采集进程收尾，不要直接强制断电。

## 更新与验证

`pyproject.toml` 管理依赖，`uv.lock` 锁定解析结果，`.python-version` 指定 Python 3.10。部署时使用 `uv sync --locked --no-dev`；服务启动使用 `--frozen --no-sync`，不在启动时安装依赖。不要把本机 `.venv` 复制到 Ubuntu，在目标服务器重新同步。

更新项目后，先停止服务，再同步依赖并重新启动：

```bash
sudo systemctl stop weibo-admin
uv sync --locked --no-dev
sudo systemctl start weibo-admin
```

开发或运行离线测试时安装开发依赖：

```bash
uv sync --locked
uv run --locked python -m pytest -q
```

修改依赖时编辑 `pyproject.toml` 并运行 `uv lock`，随代码一起保存 `uv.lock`。`requirements.txt` 仅作为兼容导出，使用 `uv export --locked --no-dev --no-hashes --output-file requirements.txt` 更新，不再手动维护。参考 [uv 项目说明](https://docs.astral.sh/uv/guides/projects/) 和 [锁定及同步说明](https://docs.astral.sh/uv/concepts/projects/sync/)。

## 使用与数据

- 创建任务后先待运行；启动队列后按顺序执行。暂停队列仅阻止下一个任务启动；任务上的暂停按钮才会暂停当前采集。
- 运行任务不能修改关键词或日期，可复制成新任务。恢复任务使用原数据库与断点目录。取消保留结果。
- Cookie 列表不返回完整值。替换、启停、手动切换在下一次微博请求生效。受限凭据退出本轮候选，全部不可用暂停队列；更新凭据后手动恢复任务及启动队列。
- “验证”使用微博登录配置接口，仅反映该接口当时的登录状态，不保证搜索接口没有限流。
- 每个任务独立保存 `weibo.db`、`checkpoint/`、`crawl.log`、`process.log` 和累计进度。原有数据库不会打开或修改。保存微博与图片两张表，不保存已确认删除的 IP 与四个 URL 字段。
- 微博、图片数量来自实际数据库；页数为解析回调累计次数，失败数为丢弃记录及请求异常累计，不等同于永久漏采数量。动态搜索不显示整体百分比和预计完成时间。
- 页面日志下载为最近 128 KB 片段；完整日志及数据库通过服务器文件工具获取。复制数据库前先暂停任务，避免复制到写入中的文件。
- 空闲磁盘不足 512 MiB 时暂停当前任务及队列。磁盘实际耗尽仍会由数据库错误保护收尾。
- Scrapy 断点保留待处理请求，不保证异常跳过的页面或进程突然被杀时的在途请求全部恢复。正常暂停优先等待处理完成。
- 备份整个 `admin-data` 目录；其中包含账号密码摘要及 Cookie 原文，目录不可共享或提交到 Git。密码重设需先停止服务，使用 `--set-password`。登录会话在服务重启后失效。

## 内容查看

在任务上点击“查看内容”，无需暂停采集。仅支持当前管理台任务产生的最新版双表数据库，不导入旧库或实验库，不改变数据库结构。

- 每页 20 条，按发布时间从新到旧；使用上一页、下一页翻页。
- 可以按正文包含、作者昵称或用户 ID、发布日期范围、仅有图片筛选。正文搜索为字面包含，不支持通配符；并不代表原始采集关键词命中关系。
- “搜索 / 刷新”会回到第一页读取最新数据；后台进度刷新不会替换当前内容。采集中数据仍可能更新，因此跨页浏览不是固定快照。
- 列表加载摘要及缩略图，详情展示完整正文和多图。点击图片放大，支持左右切换、键盘方向键及单张原图下载。
- 仅展示实际保存成功的图片，图片缺失不等于原微博没有图片。损坏或超大图片显示读取失败，不影响浏览正文。
- 所有内容和图片接口均要求登录。数据库以只读方式打开，列表不读取图片二进制，不新增索引、字段或表。
- 正文包含搜索可能扫描较多记录；查询限时约两秒，数据库忙或超时会提示缩小筛选范围重试。缩略图在内存中生成，不保存到原数据库。原图读取限制为 64 MiB，缩略图处理上限 2500 万像素。

当前不包含内容编辑删除、批量打包、数据导出、定时调度及外部通知。
