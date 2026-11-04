import hashlib
import io
import sqlite3
import threading
from http.server import ThreadingHTTPServer

from PIL import Image
import pytest
import requests

from weibo.admin import viewer
from weibo.admin.server import Manager, Handler
from weibo.utils.sqlite_schema import create_table_sql, create_image_table_sql


@pytest.fixture
def sample(tmp_path):
    manager = Manager(tmp_path)
    manager.action(dict(op='create', keywords='测试', start='2023-07-29', end='2023-08-03'))
    job = manager.state['jobs'][0]['id']
    folder = tmp_path / job
    folder.mkdir()
    path = folder / 'weibo.db'
    stream = io.BytesIO()
    Image.new('RGB', (800, 600), 'orange').save(stream, format='PNG')
    raw = stream.getvalue()
    with sqlite3.connect(path) as conn:
        conn.execute(create_table_sql(['ip', 'pics', 'article_url', 'video_url']))
        conn.execute(create_image_table_sql(['weibo_image.url']))
        conn.execute('PRAGMA user_version=2')
        conn.execute('CREATE INDEX idx_weibo_created_at_id ON weibo(created_at DESC,id DESC)')
        for i in range(45):
            conn.execute('INSERT INTO weibo(id,bid,user_id,screen_name,text,created_at) VALUES(?,?,?,?,?,?)',
                (f'{i:03}',str(i),'user1' if i%2 else 'user2','甲作者' if i%2 else '乙作者',
                 '含有百分号%及下划线_' if i==1 else '正文测试', '2023-07-30 12:00' if i<44 else None))
        for seq in (0, 3):
            conn.execute('INSERT INTO weibo_image VALUES(?,?,?,?)',('001',seq,bytes(32),raw))
        conn.execute('INSERT INTO weibo_image VALUES(?,?,?,?)',('002',0,bytes(32),b'broken image'))
    conn.close()
    return manager, job, path, raw


def test_paging_no_duplicates_and_readonly(sample):
    manager, job, path, _ = sample
    digest = hashlib.sha256(path.read_bytes()).digest()
    ids, cursor = [], None
    for _ in range(3):
        with viewer.database(manager, job) as conn:
            result = viewer.list_posts(conn, {'cursor':cursor} if cursor else {})
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM weibo")
        ids.extend(row['id'] for row in result['items'])
        cursor = result['next']
    assert len(ids) == len(set(ids)) == 45
    assert ids[:2] == ['043','042']
    assert ids[-1] == '044'  # 空时间排最后。
    assert cursor is None
    assert hashlib.sha256(path.read_bytes()).digest() == digest


def test_filters_literal_text_and_image_metadata(sample):
    manager, job, _, _ = sample
    with viewer.database(manager, job) as conn:
        result = viewer.list_posts(conn, {'text':'%','author':'user1','images':'1',
                                         'start':'2023-07-30','end':'2023-07-30'})
        assert [p['id'] for p in result['items']] == ['001']
        assert result['items'][0]['image_count'] == 2
        assert result['items'][0]['first_image'] == 0
        assert not viewer.list_posts(conn, {'start':'2023-07-31'})['items']
        assert viewer.list_posts(conn, {'author':'甲作'})['items']
        assert not viewer.list_posts(conn, {'text':"' OR 1=1 --"})['items']
        for params in ({'cursor':'bad'}, {'start':'invalid'}, {'start':'2024-01-01','end':'2023-01-01'}):
            with pytest.raises(viewer.ViewError):
                viewer.list_posts(conn, params)


def test_details_sparse_gallery_original_and_thumbnail(sample):
    manager, job, _, raw = sample
    with viewer.database(manager, job) as conn:
        assert viewer.detail(conn,'001')['images'] == [0,3]
        assert viewer.picture(conn,'001','3') == (raw, 'image/png','png')
        thumbnail, mime, _ = viewer.picture(conn,'001','3',True)
        assert mime == 'image/jpeg'
        with Image.open(io.BytesIO(thumbnail)) as image:
            assert image.size == (480,360)
        for ident, seq, code in [('001','1',404),('002','0',422),('001','../',400),('001','9'*40,400)]:
            with pytest.raises(viewer.ViewError) as error:
                viewer.picture(conn,ident,seq)
            assert error.value.status == code


def test_only_known_latest_task_database(sample):
    manager, job, path, _ = sample
    for ident in ('../elsewhere', 'unknown'):
        with pytest.raises(viewer.ViewError):
            with viewer.database(manager,ident):
                pass
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA user_version=0')
    conn.close()
    with pytest.raises(viewer.ViewError, match='旧库'):
        with viewer.database(manager,job):
            pass
    path.unlink()
    with pytest.raises(viewer.ViewError):
        with viewer.database(manager,job):
            pass
    assert not path.exists()


def test_http_images_and_content_require_login(sample):
    manager, job, _, raw = sample
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.manager, server.sessions = manager, {'test-session':9999999999}
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        session = requests.Session()
        for path in ('/api/posts','/api/post','/api/image'):
            assert session.get(base+path,timeout=5).status_code == 401
        session.cookies.set('session','test-session')
        assert session.get(base+'/api/posts',params={'job':job},timeout=5).json()['items']
        assert session.get(base+'/api/post',params={'job':job,'id':'001'},timeout=5).json()['images']==[0,3]
        response = session.get(base+'/api/image',params={'job':job,'id':'001','seq':'3','download':'1'},timeout=5)
        assert response.content == raw
        assert response.headers['Content-Type'] == 'image/png'
        assert 'attachment;' in response.headers['Content-Disposition']
        assert response.headers['X-Content-Type-Options']=='nosniff'
        assert session.get(base+'/api/posts',params={'job':'../'},timeout=5).status_code==404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
