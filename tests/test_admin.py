import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
import hashlib
import subprocess
import sys
import textwrap

import pytest
import requests

from weibo.admin.server import Manager, Handler
from weibo.utils.cookie_manager import CookieManager


def config(**overrides):
    return dict(keywords='测试\n采集', start='2023-07-29', end='2023-08-03', **overrides)


def test_task_isolation_edit_and_recovery(tmp_path):
    manager = Manager(tmp_path)
    manager.action(dict(op='create', **config()))
    manager.action(dict(op='create', **config()))
    a, b = manager.state['jobs']
    assert a['id'] != b['id']
    a['status'] = 'running'
    with pytest.raises(ValueError):
        manager.action(dict(op='edit', id=a['id'], **config()))
    manager.save()
    recovered = Manager(tmp_path)
    assert recovered.state['jobs'][0]['status'] == 'interrupted'
    assert not recovered.running
    assert recovered.snapshot()['jobs'][0]['path'] != recovered.snapshot()['jobs'][1]['path']


@pytest.mark.parametrize('reason,next_runs', [('finished', True), ('startup_error', True),
                                           ('sqlite_error', False), ('cookies_exhausted', False)])
def test_queue_only_advances_after_process_exit(tmp_path, monkeypatch, reason, next_runs):
    manager = Manager(tmp_path)
    for _ in range(2):
        manager.action(dict(op='create', **config()))
    first, second = manager.state['jobs']
    first['status'] = 'running'
    folder = tmp_path / first['id']
    folder.mkdir()
    (folder / 'progress.json').write_text(json.dumps({'reason': reason}))
    manager.active = first['id']
    manager.running = True
    class Process:
        def poll(self):
            return 0
    manager.process = Process()
    launches = []
    monkeypatch.setattr(manager, 'launch', lambda job: launches.append(job['id']))
    monkeypatch.setattr('weibo.admin.server.time.sleep', lambda _: setattr(manager, 'shutdown', True))
    manager.loop()
    assert launches == ([second['id']] if next_runs else [])
    assert first['status'] == ('completed' if reason == 'finished' else 'failed')


def test_cookie_hot_reload_exhaustion_and_redaction(tmp_path):
    manager = Manager(tmp_path)
    for value in ('SUB=first', 'SUB=second'):
        manager.action(dict(op='cookie', value=value))
    cookie = CookieManager({'WEIBO_ADMIN_POOL': str(tmp_path / 'pool.json')})
    assert cookie.current == 'SUB=first'
    assert cookie.reject_current('SUB=first') == 'SUB=second'
    assert cookie.reject_current('SUB=second') == ''
    assert cookie.current == ''
    ident = manager.state['cookies'][0]['id']
    manager.action(dict(op='cookie', id=ident, value='SUB=new'))
    assert cookie.current == 'SUB=new'
    assert 'SUB=new' not in json.dumps(manager.snapshot())
    assert 'SUB=second' not in json.dumps(manager.snapshot())


def test_http_auth_csrf_and_secret_protection(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.manager = Manager(tmp_path)
    server.sessions, server.attempts = {}, {}
    server.auth = dict(salt='00' * 16, hash=hashlib.pbkdf2_hmac('sha256', b'test-password', bytes(16), 600000).hex())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    session = requests.Session()
    try:
        assert session.get(base + '/api/state').status_code == 401
        assert session.post(base + '/api/login', json={}).status_code == 403
        session.headers['X-Admin-Request'] = '1'
        assert session.post(base + '/api/login', json={'username': 'admin', 'password': 'test-password'}).status_code == 200
        assert session.post(base + '/api/action', json=dict(op='cookie', value='SUB=private')).status_code == 200
        response = session.get(base + '/api/state')
        assert response.status_code == 200
        assert 'SUB=private' not in response.text
        assert session.get(base + '/api/log?id=../../settings.py').status_code == 404
        assert session.post(base + '/api/logout', json={}).status_code == 200
        assert session.get(base + '/api/state').status_code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize('pause', [False, True])
def test_real_worker_offline_completion_and_graceful_pause(tmp_path, pause):
    """运行实际 worker/extension/SQLite，下载器截断所有网络。"""
    folder = tmp_path / 'job'
    folder.mkdir()
    pool = tmp_path / 'pool.json'
    pool.write_text('[{"id":"one","value":"SUB=test","enabled":true}]')
    config_data = dict(KEYWORD_LIST=['offline'], START_DATE='2023-07-29', END_DATE='2023-07-29',
        WEIBO_ADMIN_POOL=str(pool), SQLITE_DATABASE=str(folder / 'weibo.db'),
        JOBDIR=str(folder / 'checkpoint'), DOWNLOAD_DELAY=0,
        LOG_ENABLED=False, TELNETCONSOLE_ENABLED=False,
        ITEM_PIPELINES={'weibo.pipelines.SQLitePipeline':306},
        DOWNLOADER_MIDDLEWARES={'__main__.Offline':900},
        DEFAULT_REQUEST_HEADERS={}, SQLITE_DROP_FIELDS=['ip','pics','article_url','video_url','weibo_image.url'])
    (folder / 'config.json').write_text(json.dumps(config_data))
    if pause:
        (folder / 'stop').touch()
    script = textwrap.dedent('''
        import sys
        from scrapy.http import HtmlResponse
        from scrapy.settings import Settings
        from weibo.admin import worker
        class Offline:
            def process_request(self, request, spider):
                response = HtmlResponse(request.url, request=request,
                    body=b'<div class="card card-no-result s-pt20b40"></div>', encoding='utf-8')
                from pathlib import Path
                if (Path(sys.argv[1]) / 'stop').exists():
                    from twisted.internet import reactor
                    from twisted.internet.task import deferLater
                    return deferLater(reactor, 3, lambda: response)
                return response
        worker.get_project_settings = lambda: Settings({'SPIDER_MODULES':['weibo.spiders']})
        worker.main()
    ''')
    completed = subprocess.run([sys.executable, '-c', script, str(folder)], capture_output=True, timeout=25)
    assert completed.returncode == 0, completed.stderr.decode(errors='replace')
    progress = json.loads((folder / 'progress.json').read_text())
    assert progress['reason'] == ('admin_pause' if pause else 'finished')
    assert (folder / 'weibo.db').exists()
    assert progress['keyword'] == 'offline'
