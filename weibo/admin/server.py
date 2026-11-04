"""标准库实现的单管理员服务，不向网页返回采集凭据。"""
import argparse
from contextlib import closing
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from datetime import date

from weibo.admin.progress import atomic_json
from weibo.admin import viewer

ROOT = Path(__file__).resolve().parents[2]


class Manager:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.process = None
        self.active = None
        self.running = False
        self.shutdown = False
        try:
            self.state = json.loads((self.folder / 'state.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            self.state = {'jobs': [], 'cookies': []}
        for job in self.state['jobs']:
            if job['status'] in ('running', 'pausing', 'cancelling'):
                job['status'] = 'interrupted'
        self.save()

    def save(self):
        atomic_json(self.folder / 'state.json', self.state)

    def pool(self):
        for entry in self.state['cookies']:
            entry['revision'] = secrets.token_hex(8)
        atomic_json(self.folder / 'pool.json', self.state['cookies'])

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self.state))
            try:
                status = json.loads((self.folder / 'pool.status.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                status = {}
            for cookie in result['cookies']:
                cookie.pop('value', None)
                cookie['active'] = self.process is not None and status.get('active') == cookie['id']
                cookie['runtime_error'] = '本轮请求受限' if cookie['id'] in status.get('rejected', []) else ''
                if status.get('active') == cookie['id']:
                    cookie['last_used'] = status.get('updated')
            for job in result['jobs']:
                folder = self.folder / job['id']
                job['path'] = str(folder / 'weibo.db')
                try:
                    job['progress'] = json.loads((folder / 'progress.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    job['progress'] = {}
                db = folder / 'weibo.db'
                if db.exists():
                    try:
                        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
                            job['progress']['saved'] = conn.execute('SELECT count(*) FROM weibo').fetchone()[0]
                            job['progress']['images'] = conn.execute('SELECT count(*) FROM weibo_image').fetchone()[0]
                    except sqlite3.Error:
                        pass
            result['queue_running'] = self.running
            return result

    @staticmethod
    def validate(data):
        keywords = [v.strip() for v in str(data.get('keywords', '')).splitlines() if v.strip()]
        if not keywords or len(keywords) > 100 or any(len(v) > 200 for v in keywords):
            raise ValueError('请输入 1—100 个关键词，每行一个，每个不超过 200 字')
        start, end = date.fromisoformat(data['start']), date.fromisoformat(data['end'])
        if start > end:
            raise ValueError('开始日期不能晚于结束日期')
        kind = int(data.get('kind', 1))
        delay, limit = float(data.get('delay', 10)), int(data.get('limit', 0))
        if kind not in range(7) or not 1 <= delay <= 3600 or not 0 <= limit <= 10000000:
            raise ValueError('任务参数超出范围')
        return dict(keywords='\n'.join(keywords), start=str(start), end=str(end), kind=kind,
                    delay=delay, limit=limit, images=bool(data.get('images', True)),
                    proxy=str(data.get('proxy', '')).strip()[:2000])

    def action(self, data):
        with self.lock:
            op = data['op']
            if op in ('create', 'edit', 'copy'):
                config = self.validate(data)
                if op == 'edit':
                    job = next(j for j in self.state['jobs'] if j['id'] == data['id'])
                    if job['status'] != 'queued':
                        raise ValueError('仅待运行任务可以编辑')
                    job['config'] = config
                else:
                    self.state['jobs'].append(dict(id=secrets.token_hex(8), status='queued', config=config))
            elif op == 'start':
                if not any(c['enabled'] for c in self.state['cookies']):
                    raise ValueError('请先添加并启用 Cookie')
                self.running = True
            elif op == 'queue_pause':
                self.running = False
            elif op in ('pause', 'cancel', 'resume'):
                job = next(j for j in self.state['jobs'] if j['id'] == data['id'])
                if op == 'resume':
                    if job['status'] not in ('paused', 'interrupted', 'failed'):
                        raise ValueError('此任务不能恢复')
                    job['status'] = 'queued'
                elif job['id'] == self.active:
                    job['status'] = 'pausing' if op == 'pause' else 'cancelling'
                    (self.folder / job['id'] / 'stop').touch()
                    self.running = False
                elif job['status'] != 'completed':
                    job['status'] = 'paused' if op == 'pause' else 'cancelled'
            elif op == 'cookie':
                value = str(data.get('value', '')).strip()
                if not value or '\n' in value or '\r' in value or len(value) > 30000:
                    raise ValueError('请输入一行完整 Cookie')
                entry = next((c for c in self.state['cookies'] if c['id'] == data.get('id')), None)
                if entry:
                    entry.update(value=value)
                else:
                    self.state['cookies'].append(dict(id=secrets.token_hex(8), label=str(data.get('label', 'Cookie'))[:80], value=value, enabled=True))
                self.pool()
            elif op == 'verify':
                import requests
                entry = next(c for c in self.state['cookies'] if c['id'] == data['id'])
                try:
                    response = requests.get('https://weibo.com/ajax/config/get',
                        headers={'Cookie': entry['value'], 'User-Agent': 'Mozilla/5.0'},
                        timeout=12, allow_redirects=False)
                    payload = response.json() if response.status_code == 200 else {}
                    entry['verification'] = '登录有效' if payload.get('data', {}).get('login') is True else '未确认登录，请更换或稍后再试'
                except (requests.RequestException, ValueError):
                    entry['verification'] = '验证失败，网络或接口异常'
                entry['verified_at'] = int(time.time())
            elif op in ('toggle', 'select'):
                entry = next(c for c in self.state['cookies'] if c['id'] == data['id'])
                if op == 'toggle':
                    entry['enabled'] = not entry['enabled']
                else:
                    entry['enabled'] = True
                    self.state['cookies'].remove(entry)
                    self.state['cookies'].insert(0, entry)
                self.pool()
            else:
                raise ValueError('未知操作')
            self.save()

    def loop(self):
        while not self.shutdown:
            with self.lock:
                if self.process and self.process.poll() is not None:
                    job = next(j for j in self.state['jobs'] if j['id'] == self.active)
                    try:
                        progress = json.loads((self.folder / self.active / 'progress.json').read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        progress = {}
                    reason = progress.get('reason')
                    job['status'] = ('cancelled' if job['status'] == 'cancelling' else
                                     'paused' if reason == 'admin_pause' else
                                     'completed' if reason == 'finished' or (reason and reason.startswith('已达到爬取结果数量限制')) else 'failed')
                    job['reason'] = reason or '进程异常退出，请查看日志'
                    if reason in ('sqlite_error', 'disk_full') or (reason and any(v in reason for v in ('blocked', 'anomaly', 'cookie', '风控', '异常页'))):
                        self.running = False
                    self.process = None
                    self.active = None
                    self.save()
                if not self.process and self.running:
                    job = next((j for j in self.state['jobs'] if j['status'] == 'queued'), None)
                    if job:
                        try:
                            self.launch(job)
                        except Exception:
                            job['status'] = 'failed'
                            job['reason'] = '无法启动任务，请检查目录权限及剩余空间'
                            self.running = False
                            self.save()
            time.sleep(.5)

    def launch(self, job):
        if not any(c['enabled'] for c in self.state['cookies']):
            self.running = False
            return
        folder = self.folder / job['id']
        folder.mkdir(exist_ok=True)
        (folder / 'stop').unlink(missing_ok=True)
        progress_path = folder / 'progress.json'
        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding='utf-8'))
            progress['reason'] = None
            atomic_json(progress_path, progress)
        config = job['config']
        pipelines = {'weibo.pipelines.SQLitePipeline': 306}
        if config['images']:
            pipelines['weibo.pipelines.MyImagesPipeline'] = 300
        settings = dict(KEYWORD_LIST=config['keywords'].splitlines(), START_DATE=config['start'],
                        END_DATE=config['end'], WEIBO_TYPE=config['kind'], DOWNLOAD_DELAY=config['delay'],
                        LIMIT_RESULT=config['limit'], SQLITE_DATABASE=str(folder / 'weibo.db'),
                        JOBDIR=str(folder / 'checkpoint'), ITEM_PIPELINES=pipelines,
                        WEIBO_ADMIN_POOL=str(self.folder / 'pool.json'),
                        WEIBO_COOKIE_MAX_RETRIES=max(1, len(self.state['cookies']) - 1),
                        LOG_FILE=str(folder / 'crawl.log'), LOG_LEVEL='INFO',
                        WEIBO_COOKIE_FILE='',
                        WEIBO_PROXY_ENABLED=False,
                        DEFAULT_REQUEST_HEADERS={'Accept-Language': 'zh-CN,zh;q=0.9'})
        if config.get('proxy'):
            settings.update(WEIBO_PROXY_ENABLED=True, WEIBO_PROXY_SOURCE='paid',
                            WEIBO_PROXY_GATEWAY=config['proxy'])
        atomic_json(folder / 'config.json', settings)
        self.pool()
        # 日志交给 Scrapy；不将环境变量中的凭据写入命令行。
        with (folder / 'process.log').open('ab') as output:
            self.process = subprocess.Popen([sys.executable, '-m', 'weibo.admin.worker', str(folder)],
                                            cwd=ROOT, stdout=output, stderr=output)
        self.active = job['id']
        job['status'] = 'running'
        self.save()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200, mime='application/json; charset=utf-8', cookie=None, download=None):
        body = value if isinstance(value, bytes) else value.encode('utf-8') if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % download)
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            session = cookies['session'].value
            return self.server.sessions.get(session, 0) > time.time()
        except (KeyError, ValueError):
            return False

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path in ('/', '/app.js', '/style.css', '/viewer.js'):
            name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/viewer.js':'viewer.js'}[path.path]
            mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css', '/viewer.js':'text/javascript'}[path.path]
            return self.reply((Path(__file__).parent / 'static' / name).read_text(encoding='utf-8'), mime=mime + '; charset=utf-8')
        if not self.authenticated():
            return self.reply({'error': '请先登录'}, 401)
        if path.path in ('/api/posts', '/api/post', '/api/image'):
            params = {k:v[0] for k,v in parse_qs(path.query).items()}
            try:
                with viewer.database(self.server.manager, params.get('job','')) as conn:
                    if path.path == '/api/posts':
                        result = viewer.list_posts(conn, params)
                    elif path.path == '/api/post':
                        result = viewer.detail(conn, params.get('id',''))
                    else:
                        body, mime, ext = viewer.picture(conn, params.get('id',''), params.get('seq',''), params.get('thumb') == '1')
                        result = None
                if result is not None:
                    return self.reply(result)
                return self.reply(body, mime=mime, download='weibo-image.' + ext if params.get('download') == '1' else None)
            except viewer.ViewError as exc:
                return self.reply({'error': str(exc)}, exc.status)
        if path.path == '/api/state':
            return self.reply(self.server.manager.snapshot())
        if path.path == '/api/log':
            ident = parse_qs(path.query).get('id', [''])[0]
            if ident not in [j['id'] for j in self.server.manager.state['jobs']]:
                return self.reply({'error': '任务不存在'}, 404)
            folder = self.server.manager.folder / ident
            text = ''
            for name in ('crawl.log', 'process.log'):
                try:
                    with (folder / name).open('rb') as stream:
                        stream.seek(max(0, stream.seek(0, 2) - 128000))
                        text += stream.read().decode('utf-8', errors='replace')
                except FileNotFoundError:
                    pass
            for cookie in self.server.manager.state['cookies']:
                text = text.replace(cookie['value'], '[已隐藏]')
            return self.reply(text, mime='text/plain; charset=utf-8')
        return self.reply({'error': '不存在'}, 404)

    def do_POST(self):
        # 自定义请求头禁止跨站表单调用；服务不开放跨域访问。
        if self.headers.get('X-Admin-Request') != '1':
            return self.reply({'error': '请求无效'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 65536:
                raise ValueError('请求过大或为空')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/login':
                now = time.time()
                attempts = self.server.attempts
                address = self.client_address[0]
                history = [v for v in attempts.get(address, []) if now - v < 300]
                attempts[address] = history
                if len(history) >= 10:
                    return self.reply({'error': '尝试次数过多，请五分钟后再试'}, 429)
                history.append(now)
                auth = self.server.auth
                digest = hashlib.pbkdf2_hmac('sha256', str(data.get('password', '')).encode(), bytes.fromhex(auth['salt']), 600000).hex()
                if not hmac.compare_digest(digest, auth['hash']):
                    return self.reply({'error': '密码错误'}, 401)
                attempts.pop(address, None)
                token = secrets.token_urlsafe(32)
                self.server.sessions[token] = now + 43200
                return self.reply({'ok': True}, cookie=f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
            if not self.authenticated():
                return self.reply({'error': '请先登录'}, 401)
            if self.path == '/api/logout':
                cookies = SimpleCookie(self.headers.get('Cookie', ''))
                self.server.sessions.pop(cookies['session'].value, None)
                return self.reply({'ok': True}, cookie='session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if self.path != '/api/action':
                return self.reply({'error': '不存在'}, 404)
            self.server.manager.action(data)
            self.reply({'ok': True})
        except (ValueError, KeyError, StopIteration, TypeError):
            self.reply({'error': '操作失败，请检查输入及任务状态'}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='admin-data')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--set-password', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    folder = Path(args.data).resolve()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 防止两个服务同时控制同一队列（Ubuntu 使用内核文件锁）。
    lock_file = (folder / 'service.lock').open('a')
    if sys.platform != 'win32':
        import fcntl
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    auth_path = folder / 'auth.json'
    if args.set_password or not auth_path.exists():
        password = getpass.getpass('设置管理密码（至少12位）: ')
        if len(password) < 12 or password != getpass.getpass('再次输入密码: '):
            raise SystemExit('两次密码必须一致，且至少12位')
        salt = secrets.token_bytes(16)
        atomic_json(auth_path, dict(salt=salt.hex(), hash=hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000).hex()))
        if args.set_password:
            return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.auth = json.loads(auth_path.read_text(encoding='utf-8'))
    server.sessions, server.attempts = {}, {}
    manager = server.manager = Manager(folder)
    worker = threading.Thread(target=manager.loop, daemon=True)
    worker.start()
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f'管理台已启动：http://{args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        manager.shutdown = True
        worker.join(3)
        if manager.process:
            (folder / manager.active / 'stop').touch()
            manager.process.wait()  # 保持主进程直到采集完成优雅收尾。
        server.server_close()


if __name__ == '__main__':
    main()
