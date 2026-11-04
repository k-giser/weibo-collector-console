# -*- coding: utf-8 -*-
"""双表 SQLite 存储测试；只使用内存和临时目录，不访问已有业务库。"""
import hashlib
import logging
import os
import sqlite3
from types import SimpleNamespace

import pytest
from scrapy.exceptions import DropItem
from scrapy.settings import Settings
from scrapy.utils.project import get_project_settings

from weibo.pipelines import SQLitePipeline, normalize_pics
from weibo.utils.sqlite_schema import SCHEMA_VERSION


class SpiderStub:
    logger = logging.getLogger('test_spider')


EXPECTED_FIELDS = {
    'id', 'bid', 'user_id', 'screen_name', 'text', 'article_url', 'topics',
    'at_users', 'pics', 'video_url', 'location', 'created_at', 'source',
    'attitudes_count', 'comments_count', 'reposts_count', 'retweet_id', 'ip',
    'user_authentication', 'vip_type', 'vip_level',
}
IMAGE_FIELDS = {'weibo_id', 'seq', 'image_key', 'url', 'image'}


def make_item(**overrides):
    base = {
        'id': '100001', 'bid': 'abc123', 'user_id': 'u1', 'screen_name': '测试',
        'text': '正文', 'pics': ['https://wx1.sinaimg.cn/large/a.jpg'],
        'video_url': '', 'created_at': '2026-08-01 12:00:00',
    }
    base.update(overrides)
    return {'weibo': base, 'keyword': 'test'}


def _mock_sqlite_connect(monkeypatch, conn):
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: conn)


def _columns(pipeline, table='weibo'):
    return {row[1] for row in pipeline.cursor.execute('PRAGMA table_info(%s)' % table)}


def _images(pipeline, weibo_id='100001'):
    return dict(pipeline.cursor.execute(
        'SELECT seq,image FROM weibo_image WHERE weibo_id=? ORDER BY seq', (weibo_id,)))


def _image_records(pipeline, weibo_id='100001'):
    url = 'url' if 'url' in _columns(pipeline, 'weibo_image') else 'NULL'
    return pipeline.cursor.execute(
        'SELECT seq,%s,image_key,image FROM weibo_image WHERE weibo_id=? ORDER BY seq' % url,
        (weibo_id,)).fetchall()


def _new_storage(conn, monkeypatch, drop_fields=()):
    _mock_sqlite_connect(monkeypatch, conn)
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=Settings({
        'SQLITE_DATABASE': 'test.db', 'SQLITE_DROP_FIELDS': list(drop_fields),
    })))
    storage.open_spider(SpiderStub())
    return storage


@pytest.fixture
def pipeline(monkeypatch):
    conn = sqlite3.connect(':memory:')
    result = _new_storage(conn, monkeypatch)
    yield result
    conn.close()


def test_new_database_has_separate_text_and_raw_image_tables(pipeline):
    tables = {row[0] for row in pipeline.cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {'weibo', 'weibo_image'}
    assert _columns(pipeline) == EXPECTED_FIELDS
    assert 'images' not in _columns(pipeline)
    assert _columns(pipeline, 'weibo_image') == IMAGE_FIELDS
    image_info = {row[1]: row for row in pipeline.cursor.execute('PRAGMA table_info(weibo_image)')}
    assert image_info['image'][2] == 'BLOB'
    assert image_info['image_key'][2] == 'BLOB'
    assert image_info['weibo_id'][5] == 1
    assert image_info['seq'][5] == 2
    assert pipeline.cursor.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_all_original_text_fields_remain_without_drop_configuration(pipeline):
    pipeline.process_item(make_item(ip='北京', retweet_id='parent'), SpiderStub())
    assert pipeline.cursor.execute('SELECT ip,retweet_id FROM weibo').fetchone() == ('北京', 'parent')


def test_created_at_id_index_exists(pipeline):
    indexes = {row[1] for row in pipeline.cursor.execute('PRAGMA index_list(weibo)')}
    assert 'idx_weibo_created_at_id' in indexes


@pytest.mark.parametrize('layout', ['legacy_text', 'legacy_dual', 'packed_single'])
def test_legacy_database_is_rejected_without_modifying_file(tmp_path, layout):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE weibo (id TEXT PRIMARY KEY,bid TEXT,pics TEXT,images BLOB)'
                     if layout == 'packed_single' else
                     'CREATE TABLE weibo (id TEXT PRIMARY KEY,bid TEXT,pics TEXT,pics_path TEXT)')
        conn.execute('INSERT INTO weibo VALUES (?,?,?,?)',
                     ('legacy', 'old', 'https://x/old.jpg', b'old-archive' if layout == 'packed_single' else 'old/path.jpg'))
        if layout == 'legacy_dual':
            conn.execute('CREATE TABLE weibo_image (weibo_id TEXT,seq INTEGER,url TEXT,image BLOB)')
            conn.execute('INSERT INTO weibo_image VALUES (?,?,?,?)',
                         ('legacy', 0, 'https://x/old.jpg', b'old-image'))
    before, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=Settings({'SQLITE_DATABASE': str(path)})))
    with pytest.raises(RuntimeError, match='SQLite数据库初始化失败'):
        storage.open_spider(SpiderStub())
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_matching_version_two_database_can_be_reopened(tmp_path):
    path = tmp_path / 'current.db'
    crawler = SimpleNamespace(settings=Settings({'SQLITE_DATABASE': str(path), 'SQLITE_DROP_FIELDS': ['ip']}))
    storage = SQLitePipeline.from_crawler(crawler)
    storage.open_spider(SpiderStub())
    storage.process_item(make_item(ip='not-persisted', images=[('https://x/a.jpg', b'image')]), SpiderStub())
    storage.close_spider(SpiderStub())
    storage = SQLitePipeline.from_crawler(crawler)
    storage.open_spider(SpiderStub())
    assert 'ip' not in _columns(storage)
    assert _images(storage) == {0: b'image'}
    assert storage.cursor.execute('SELECT count(*) FROM weibo').fetchone()[0] == 1
    storage.close_spider(SpiderStub())


def test_missing_version_two_table_is_rejected_without_repair(tmp_path):
    path = tmp_path / 'missing.db'
    crawler = SimpleNamespace(settings=Settings({'SQLITE_DATABASE': str(path)}))
    storage = SQLitePipeline.from_crawler(crawler)
    storage.open_spider(SpiderStub())
    storage.close_spider(SpiderStub())
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE weibo_image')
    before, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(RuntimeError):
        SQLitePipeline.from_crawler(crawler).open_spider(SpiderStub())
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_insert_item_stores_text_once_and_independent_raw_images(pipeline):
    urls = ['https://x/1.jpg', 'https://x/2.jpg']
    pipeline.process_item(make_item(pics=urls, images=[(urls[0], b'data1'), (urls[1], b'data2')]), SpiderStub())
    assert pipeline.cursor.execute('SELECT pics,text FROM weibo').fetchone() == (','.join(urls), '正文')
    assert _images(pipeline) == {0: b'data1', 1: b'data2'}
    assert _image_records(pipeline) == [
        (0, urls[0], hashlib.sha256(urls[0].encode()).digest(), b'data1'),
        (1, urls[1], hashlib.sha256(urls[1].encode()).digest(), b'data2'),
    ]


def test_insert_without_images_keeps_text_without_image_rows(pipeline):
    pipeline.process_item(make_item(), SpiderStub())
    assert _images(pipeline) == {}
    assert pipeline.cursor.execute('SELECT count(*) FROM weibo').fetchone()[0] == 1


def test_insert_partial_failure_preserves_image_sequence(pipeline):
    urls = ['https://x/1.jpg', 'https://x/2.jpg']
    pipeline.process_item(make_item(pics=urls, images=[None, (urls[1], b'data2')]), SpiderStub())
    assert _images(pipeline) == {1: b'data2'}


def test_repeated_capture_updates_one_text_row_and_existing_image(pipeline):
    url = 'https://x/1.jpg'
    pipeline.process_item(make_item(pics=[url], images=[(url, b'old')]), SpiderStub())
    pipeline.process_item(make_item(pics=[url], images=[(url, b'new')], text='更新正文'), SpiderStub())
    assert pipeline.cursor.execute('SELECT count(*),text FROM weibo').fetchone() == (1, '更新正文')
    assert _images(pipeline) == {0: b'new'}


@pytest.mark.parametrize('retry_images', [None, [], [None], [None, None]])
def test_no_successful_download_does_not_clear_existing_images(pipeline, retry_images):
    url = 'https://x/1.jpg'
    pipeline.process_item(make_item(pics=[url], images=[(url, b'old')]), SpiderStub())
    old = _image_records(pipeline)
    item = make_item(pics=[url], text='新正文')
    if retry_images is not None:
        item['weibo']['images'] = retry_images
    pipeline.process_item(item, SpiderStub())
    assert _image_records(pipeline) == old
    assert pipeline.cursor.execute('SELECT text FROM weibo').fetchone() == ('新正文',)


@pytest.mark.parametrize('without_urls', [False, True])
def test_partial_retry_matches_hash_and_image_order_without_needing_raw_urls(monkeypatch, without_urls):
    conn = sqlite3.connect(':memory:')
    excluded = ['pics', 'article_url', 'video_url', 'weibo_image.url'] if without_urls else []
    storage = _new_storage(conn, monkeypatch, excluded)
    a, b, c = ['https://x/%s.jpg' % value for value in 'abc']
    storage.process_item(make_item(pics=[a, b], images=[(a, b'old-a'), (b, b'old-b')]), SpiderStub())
    storage.process_item(make_item(pics=[b, a, c], images=[None, (a, b'new-a'), None]), SpiderStub())
    assert _images(storage) == {0: b'old-b', 1: b'new-a'}
    rows = _image_records(storage)
    assert rows[0][2] == hashlib.sha256(b.encode()).digest()
    assert rows[1][2] == hashlib.sha256(a.encode()).digest()
    if without_urls:
        assert not {'pics', 'article_url', 'video_url'} & _columns(storage)
        assert 'url' not in _columns(storage, 'weibo_image')
        assert all(row[1] is None for row in rows)
    storage.close_spider(SpiderStub())


def test_partial_retry_does_not_reuse_another_image_at_same_sequence(pipeline):
    a, b, c = ['https://x/%s.jpg' % value for value in 'abc']
    pipeline.process_item(make_item(pics=[a, b], images=[(a, b'old-a'), (b, b'old-b')]), SpiderStub())
    pipeline.process_item(make_item(pics=[c, b], images=[None, (b, b'new-b')]), SpiderStub())
    assert _images(pipeline) == {1: b'new-b'}


@pytest.mark.parametrize('pics,expected', [
    ([], ''), (None, ''), (['a'], 'a'), (['a', 'b'], 'a,b'), ('already,string', 'already,string'),
])
def test_normalize_pics(pics, expected):
    assert normalize_pics(pics) == expected


def test_successful_save_releases_item_image_bytes(pipeline):
    item = make_item(images=[('https://x/a.jpg', b'data')])
    assert pipeline.process_item(item, SpiderStub()) is item
    assert 'images' not in item['weibo']
    assert _images(pipeline) == {0: b'data'}


def test_last_item_failure_requests_shutdown_and_is_not_reported_saved(pipeline):
    closed = []
    pipeline.crawler = SimpleNamespace(engine=SimpleNamespace(close_spider=lambda spider, reason: closed.append(reason)))
    spider = SpiderStub()
    with pytest.raises(DropItem, match='SQLite保存失败'):
        pipeline.process_item(make_item(bid=None, images=[('https://x/a.jpg', b'data')]), spider)
    assert closed == ['sqlite_error']
    assert spider.sqlite3_error
    assert pipeline.cursor.execute('SELECT count(*) FROM weibo').fetchone()[0] == 0
    assert _images(pipeline) == {}
    with pytest.raises(DropItem, match='SQLite已停止写入'):
        pipeline.process_item(make_item(id='100002'), spider)
    assert closed == ['sqlite_error']


def test_bad_image_cannot_corrupt_existing_text_or_images(pipeline):
    url = 'https://x/1.jpg'
    pipeline.process_item(make_item(pics=[url], images=[(url, b'old')]), SpiderStub())
    before = (pipeline.cursor.execute('SELECT text FROM weibo').fetchone(), _image_records(pipeline))
    with pytest.raises(DropItem):
        pipeline.process_item(make_item(pics=[url], text='不应保存', images=[(url, object())]), SpiderStub())
    assert (pipeline.cursor.execute('SELECT text FROM weibo').fetchone(), _image_records(pipeline)) == before


def test_invalid_new_image_cannot_leave_text_only_row(pipeline):
    with pytest.raises(DropItem):
        pipeline.process_item(make_item(images=[('https://x/bad.jpg', object())]), SpiderStub())
    assert pipeline.cursor.execute('SELECT count(*) FROM weibo').fetchone()[0] == 0
    assert _images(pipeline) == {}


def test_image_sql_failure_rolls_back_text_and_any_image_rows(pipeline):
    original = pipeline.cursor

    class FailSecondImage:
        def __init__(self):
            self.count = 0

        def execute(self, sql, parameters=()):
            if 'insert into weibo_image' in sql.replace('"', '').lower():
                self.count += 1
                if self.count == 2:
                    raise sqlite3.OperationalError('simulated image write failure')
            return original.execute(sql, parameters)

        def executemany(self, sql, rows):
            for row in rows:
                self.execute(sql, row)
            return self

        def __getattr__(self, name):
            return getattr(original, name)

    pipeline.cursor = FailSecondImage()
    with pytest.raises(DropItem):
        pipeline.process_item(make_item(pics=['https://x/a', 'https://x/b'],
                                       images=[('https://x/a', b'a'), ('https://x/b', b'b')]), SpiderStub())
    assert original.execute('SELECT count(*) FROM weibo').fetchone()[0] == 0
    assert original.execute('SELECT count(*) FROM weibo_image').fetchone()[0] == 0


def test_unknown_item_field_is_not_silently_discarded(pipeline):
    with pytest.raises(DropItem, match='未知微博字段'):
        pipeline.process_item(make_item(unknown_field='unsupported'), SpiderStub())
    assert pipeline.cursor.execute('SELECT count(*) FROM weibo').fetchone()[0] == 0


def test_open_failure_aborts_instead_of_leaving_unusable_pipeline(monkeypatch):
    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError('cannot open database')
    monkeypatch.setattr(sqlite3, 'connect', fail_connect)
    spider = SpiderStub()
    with pytest.raises(RuntimeError, match='SQLite数据库初始化失败'):
        SQLitePipeline().open_spider(spider)
    assert spider.sqlite3_error


def test_database_path_uses_crawler_settings(monkeypatch):
    conn = sqlite3.connect(':memory:')
    paths = []
    monkeypatch.setattr(sqlite3, 'connect', lambda path: paths.append(path) or conn)
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=Settings({'SQLITE_DATABASE': 'custom.db'})))
    storage.open_spider(SpiderStub())
    assert paths == [os.path.abspath(os.path.join('结果文件', 'custom.db'))]
    storage.close_spider(SpiderStub())


def test_default_database_path_is_new_two_table_file(monkeypatch):
    conn = sqlite3.connect(':memory:')
    paths = []
    monkeypatch.setattr(sqlite3, 'connect', lambda path: paths.append(path) or conn)
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=Settings({})))
    storage.open_spider(SpiderStub())
    assert paths == [os.path.abspath(os.path.join('结果文件', 'weibo_new.db'))]
    storage.close_spider(SpiderStub())


def test_approved_ip_exclusion_does_not_remove_other_fields(monkeypatch):
    conn = sqlite3.connect(':memory:')
    storage = _new_storage(conn, monkeypatch, ['ip'])
    assert _columns(storage) == EXPECTED_FIELDS - {'ip'}
    assert _columns(storage, 'weibo_image') == IMAGE_FIELDS
    storage.process_item(make_item(ip='北京', retweet_id='parent'), SpiderStub())
    assert storage.cursor.execute('SELECT retweet_id FROM weibo').fetchone() == ('parent',)
    storage.close_spider(SpiderStub())


def test_confirmed_project_defaults_exclude_urls_and_preserve_images(tmp_path):
    settings = get_project_settings()
    excluded = {'ip', 'pics', 'article_url', 'video_url', 'weibo_image.url'}
    assert set(settings.getlist('SQLITE_DROP_FIELDS')) == excluded
    settings.set('SQLITE_DATABASE', str(tmp_path / 'confirmed-fields.db'),
                 priority='cmdline')
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=settings))
    storage.open_spider(SpiderStub())
    assert _columns(storage) == EXPECTED_FIELDS - (excluded - {'weibo_image.url'})
    assert _columns(storage, 'weibo_image') == IMAGE_FIELDS - {'url'}
    url = 'https://x/download-only.jpg'
    storage.process_item(make_item(
        ip='北京', pics=[url], article_url='https://x/article',
        video_url='https://x/video', images=[(url, b'original-image')]), SpiderStub())
    assert storage.cursor.execute('SELECT id,bid,user_id FROM weibo').fetchone() == (
        '100001', 'abc123', 'u1')
    assert storage.cursor.execute('SELECT image FROM weibo_image').fetchone() == (
        b'original-image',)
    storage.close_spider(SpiderStub())


def test_changed_drop_configuration_rejects_existing_file_without_altering_columns(tmp_path):
    path = tmp_path / 'existing.db'
    original_settings = Settings({'SQLITE_DATABASE': str(path)})
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=original_settings))
    storage.open_spider(SpiderStub())
    storage.process_item(make_item(ip='北京', retweet_id='parent'), SpiderStub())
    storage.close_spider(SpiderStub())
    before, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    changed = Settings({'SQLITE_DATABASE': str(path), 'SQLITE_DROP_FIELDS': ['ip']})
    with pytest.raises(RuntimeError):
        SQLitePipeline.from_crawler(SimpleNamespace(settings=changed)).open_spider(SpiderStub())
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


@pytest.mark.parametrize('field', ['unsupported_field', 'id', 'images', 'weibo_image.seq', 'weibo_image.image'])
def test_new_database_rejects_unknown_or_required_drop_fields(monkeypatch, field):
    conn = sqlite3.connect(':memory:')
    _mock_sqlite_connect(monkeypatch, conn)
    storage = SQLitePipeline.from_crawler(SimpleNamespace(settings=Settings({
        'SQLITE_DATABASE': 'invalid-config-test.db', 'SQLITE_DROP_FIELDS': [field],
    })))
    with pytest.raises(RuntimeError, match='SQLite数据库初始化失败') as exc:
        storage.open_spider(SpiderStub())
    assert isinstance(exc.value.__cause__, ValueError)
