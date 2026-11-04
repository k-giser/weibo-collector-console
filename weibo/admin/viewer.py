"""最新版任务数据库的只读查询；列表不读取图片 BLOB。"""
import base64
from contextlib import contextmanager
from datetime import date, timedelta
import io
import json
import sqlite3
import time
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


class ViewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextmanager
def database(manager, job_id):
    with manager.lock:
        if not any(j['id'] == job_id for j in manager.state['jobs']):
            raise ViewError('任务不存在', 404)
        path = (manager.folder / job_id / 'weibo.db').resolve()
        if not path.is_relative_to(manager.folder):
            raise ViewError('任务路径无效', 404)
    if not path.is_file():
        raise ViewError('此任务尚未保存采集数据', 404)
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
        if conn.execute('PRAGMA user_version').fetchone()[0] != 2:
            raise ViewError('仅支持最新版任务数据库，不支持旧库和实验库')
        yield conn
    except sqlite3.Error:
        raise ViewError('数据正在写入或查询耗时较长，请缩小条件后重试', 503) from None
    finally:
        if conn is not None:
            conn.close()


def encode_cursor(row):
    return base64.urlsafe_b64encode(json.dumps([row['created_at'], row['id']]).encode()).decode()


def list_posts(conn, params):
    where, args = [], []
    for key, column in [('text', 'text'), ('author', 'screen_name')]:
        value = params.get(key, '')
        if len(value) > 200:
            raise ViewError('搜索词不能超过 200 字')
        if value:
            if key == 'author':
                where.append('(instr(screen_name, ?) > 0 OR user_id = ?)')
                args.extend([value, value])
            else:
                where.append('instr(text, ?) > 0')
                args.append(value)
    try:
        start = date.fromisoformat(params['start']) if params.get('start') else None
        end = date.fromisoformat(params['end']) if params.get('end') else None
        if start and end and start > end:
            raise ValueError
        if start:
            where.append('created_at >= ?')
            args.append(str(start))
        if end:
            where.append('created_at < ?')
            args.append(str(end + timedelta(days=1)))
    except (ValueError, OverflowError):
        raise ViewError('日期范围无效') from None
    if params.get('images') == '1':
        where.append('EXISTS (SELECT 1 FROM weibo_image i WHERE i.weibo_id = w.id)')
    if params.get('cursor'):
        try:
            stamp, ident = json.loads(base64.urlsafe_b64decode(params['cursor'].encode()))
            if not isinstance(ident, str) or (stamp is not None and not isinstance(stamp, str)):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ViewError('翻页参数无效') from None
        if stamp is None:
            where.append('(created_at IS NULL AND id < ?)')
            args.append(ident)
        else:
            where.append('(created_at < ? OR (created_at = ? AND id < ?) OR created_at IS NULL)')
            args.extend([stamp, stamp, ident])
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    rows = conn.execute('SELECT id, screen_name, user_id, created_at, substr(text,1,220) AS text, '
        '(SELECT count(*) FROM weibo_image i WHERE i.weibo_id=w.id) AS image_count, '
        '(SELECT min(seq) FROM weibo_image i WHERE i.weibo_id=w.id) AS first_image '
        'FROM weibo w' + clause + ' ORDER BY created_at DESC, id DESC LIMIT 21', args).fetchall()
    items = [dict(row) for row in rows[:20]]
    return {'items': items, 'next': encode_cursor(rows[19]) if len(rows) > 20 else None}


def detail(conn, ident):
    row = conn.execute('SELECT id,bid,user_id,screen_name,text,created_at,source,location,topics,at_users,'
        'attitudes_count,comments_count,reposts_count,retweet_id,user_authentication,vip_type,vip_level '
        'FROM weibo WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ViewError('微博不存在', 404)
    result = dict(row)
    result['images'] = [r[0] for r in conn.execute(
        'SELECT seq FROM weibo_image WHERE weibo_id=? ORDER BY seq', (ident,))]
    return result


def picture(conn, ident, seq, thumbnail=False):
    try:
        seq = int(seq)
        if not 0 <= seq <= 2147483647:
            raise ValueError
    except ValueError:
        raise ViewError('图片编号无效') from None
    row = conn.execute('SELECT image FROM weibo_image WHERE weibo_id=? AND seq=? '
                       'AND length(image)<=67108864', (ident, seq)).fetchone()
    if not row:
        raise ViewError('图片不存在或过大', 404)
    raw = bytes(row[0])
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                kind = image.format
                if kind not in ('JPEG', 'PNG', 'GIF', 'WEBP', 'BMP'):
                    raise ValueError
                if thumbnail:
                    if image.width * image.height > 25000000:
                        raise ValueError
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((480, 480))
                    output = io.BytesIO()
                    image.convert('RGB').save(output, format='JPEG', quality=80)
                    return output.getvalue(), 'image/jpeg', 'jpg'
                return raw, {'JPEG':'image/jpeg','PNG':'image/png','GIF':'image/gif',
                             'WEBP':'image/webp','BMP':'image/bmp'}[kind], kind.lower().replace('jpeg','jpg')
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ViewError('图片数据损坏或尺寸过大，无法预览', 422) from None
