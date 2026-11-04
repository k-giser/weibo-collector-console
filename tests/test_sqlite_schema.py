"""仅在内存中校验新库 DDL，不连接任何已有数据库。"""
import hashlib
import sqlite3

import pytest

from weibo.utils.sqlite_schema import (
    FIELD_NAMES, IMAGE_COLUMNS, SCHEMA_VERSION, create_image_table_sql,
    create_table_sql, image_field_names,
)


def build_schema(drop_fields=()):
    conn = sqlite3.connect(':memory:')
    conn.execute(create_table_sql(drop_fields))
    conn.execute(create_image_table_sql(drop_fields))
    return conn


def columns(conn, table):
    return {row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)}


def test_new_schema_uses_two_tables_and_keeps_original_fields_by_default():
    with build_schema() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {'weibo', 'weibo_image'}
        assert columns(conn, 'weibo') == FIELD_NAMES
        assert len(FIELD_NAMES) == 21
        assert 'images' not in columns(conn, 'weibo')
        assert columns(conn, 'weibo_image') == {name for name, _ in IMAGE_COLUMNS}
        assert SCHEMA_VERSION == 2


def test_ip_exclusion_keeps_every_other_field():
    with build_schema(['ip']) as conn:
        assert columns(conn, 'weibo') == FIELD_NAMES - {'ip'}
        assert 'url' in columns(conn, 'weibo_image')


def test_url_exclusions_are_independent_of_image_bytes_and_identifiers():
    excluded = ['ip', 'pics', 'article_url', 'video_url', 'weibo_image.url']
    with build_schema(excluded) as conn:
        assert columns(conn, 'weibo') == FIELD_NAMES - set(excluded)
        assert columns(conn, 'weibo_image') == {'weibo_id', 'seq', 'image_key', 'image'}
        assert {'id', 'bid', 'user_id'} <= columns(conn, 'weibo')


@pytest.mark.parametrize('excluded', [[], ['ip'], ['pics', 'weibo_image.url']])
def test_image_field_names_match_generated_schema(excluded):
    with build_schema(excluded) as conn:
        assert columns(conn, 'weibo_image') == image_field_names(excluded)


def test_image_field_names_validate_configuration():
    with pytest.raises(ValueError):
        image_field_names(['not-a-field'])


@pytest.mark.parametrize('field', [
    'unrecognized', 'images', 'url', 'weibo_image.unknown',
    'id', 'weibo_image.weibo_id', 'weibo_image.seq',
    'weibo_image.image_key', 'weibo_image.image',
])
@pytest.mark.parametrize('factory', [create_table_sql, create_image_table_sql])
def test_invalid_or_required_field_cannot_be_excluded(field, factory):
    with pytest.raises(ValueError):
        factory([field])


def test_multiple_images_are_individually_addressable_and_binary():
    key_a = hashlib.sha256(b'url-a').digest()
    key_b = hashlib.sha256(b'url-b').digest()
    with build_schema(['weibo_image.url']) as conn:
        conn.execute('INSERT INTO weibo (id,bid) VALUES (?,?)', ('1', 'one'))
        conn.executemany('INSERT INTO weibo_image '
                         '(weibo_id,seq,image_key,image) VALUES (?,?,?,?)', [
            ('1', 0, key_a, b'first-image'),
            ('1', 2, key_b, b'second-image'),
        ])
        assert conn.execute('SELECT image FROM weibo_image '
                            'WHERE weibo_id=? AND seq=?', ('1', 2)).fetchone() == (b'second-image',)
        assert conn.execute('SELECT seq,typeof(image) FROM weibo_image '
                            'ORDER BY seq').fetchall() == [(0, 'blob'), (2, 'blob')]
        assert conn.execute('SELECT count(*) FROM weibo').fetchone()[0] == 1


@pytest.mark.parametrize('seq,key,data', [
    (-1, b'x' * 32, b'image'),
    (None, b'x' * 32, b'image'),
    (0, None, b'image'),
    (0, b'too-short', b'image'),
    (0, b'x' * 32, None),
])
def test_required_image_coordinates_and_payload_are_enforced(seq, key, data):
    with build_schema() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO weibo_image '
                         '(weibo_id,seq,image_key,image) VALUES (?,?,?,?)',
                         ('1', seq, key, data))


def test_duplicate_sequence_is_rejected_for_same_post():
    with build_schema() as conn:
        row = ('1', 0, b'x' * 32, b'image')
        conn.execute('INSERT INTO weibo_image '
                     '(weibo_id,seq,image_key,image) VALUES (?,?,?,?)', row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO weibo_image '
                         '(weibo_id,seq,image_key,image) VALUES (?,?,?,?)', row)
        # 不同微博可以使用相同的图片序号。
        conn.execute('INSERT INTO weibo_image '
                     '(weibo_id,seq,image_key,image) VALUES (?,?,?,?)',
                     ('2', 0, b'x' * 32, b'image'))
