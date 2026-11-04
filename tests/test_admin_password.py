import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from weibo.admin.server import Handler, Manager, initialize_auth, password_matches, password_record


def test_default_account_and_existing_password_are_preserved(tmp_path):
    path = tmp_path / 'auth.json'
    auth = initialize_auth(path)
    assert auth['username'] == 'admin'
    assert password_matches(auth, 'admin')
    legacy = password_record('x')
    legacy.pop('username')
    path.write_text(json.dumps(legacy))
    assert initialize_auth(path) == legacy
    assert password_matches(initialize_auth(path), 'x')
    assert not password_matches(initialize_auth(path), 'admin')
    assert '"admin"' not in path.read_text()  # 旧配置未被自动重置。


@pytest.fixture
def service(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.manager = Manager(tmp_path)
    server.auth = initialize_auth(tmp_path / 'auth.json')
    server.sessions, server.attempts = {}, {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join(3)


def client():
    session = requests.Session()
    session.headers['X-Admin-Request'] = '1'
    return session


def test_password_change_requires_login_current_password_and_confirmation(service):
    server, base = service
    first, second = client(), client()
    def login(session, password='admin', username='admin'):
        return session.post(base + '/api/login', json=dict(username=username, password=password), timeout=5)
    data = dict(current_password='admin', new_password='x', confirm_password='x')
    assert first.post(base + '/api/password', json=data, timeout=5).status_code == 401
    assert login(first, username='other').status_code == 401
    assert login(first).status_code == login(second).status_code == 200
    for changed in (dict(current_password='wrong'), dict(confirm_password='different'),
                    dict(new_password='', confirm_password='')):
        assert first.post(base + '/api/password', json={**data, **changed}, timeout=5).status_code == 400
    response = first.post(base + '/api/password', json=data, timeout=5)
    assert response.status_code == 200  # 一字符新密码，已移除原长度限制。
    for session in (first, second):
        assert session.get(base + '/api/state', timeout=5).status_code == 401
    saved = json.loads((server.manager.folder / 'auth.json').read_text())
    assert saved == server.auth
    assert password_matches(saved, 'x')
    assert not password_matches(saved, 'admin')
    assert login(first).status_code == 401
    assert login(first, password='x').status_code == 200
    assert first.get(base + '/api/state', timeout=5).status_code == 200


def test_failed_password_write_keeps_existing_password_and_session(service, monkeypatch):
    server, base = service
    session = client()
    assert session.post(base + '/api/login', json=dict(username='admin', password='admin'), timeout=5).status_code == 200
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr('weibo.admin.server.atomic_json', fail)
    response = session.post(base + '/api/password', json=dict(
        current_password='admin', new_password='new', confirm_password='new'), timeout=5)
    assert response.status_code == 500
    assert password_matches(server.auth, 'admin')
    assert session.get(base + '/api/state', timeout=5).status_code == 200
