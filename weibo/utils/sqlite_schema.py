"""后续采集使用的双表结构；字段排除列表只用于创建新库。"""

SCHEMA_VERSION = 2

WEIBO_COLUMNS = (
    ('id', 'TEXT NOT NULL PRIMARY KEY'),
    ('bid', 'TEXT NOT NULL'),
    ('user_id', 'TEXT'),
    ('screen_name', 'TEXT'),
    ('text', 'TEXT'),
    ('article_url', 'TEXT'),
    ('topics', 'TEXT'),
    ('at_users', 'TEXT'),
    ('pics', 'TEXT'),
    ('video_url', 'TEXT'),
    ('location', 'TEXT'),
    ('created_at', 'TEXT'),
    ('source', 'TEXT'),
    ('attitudes_count', 'INTEGER'),
    ('comments_count', 'INTEGER'),
    ('reposts_count', 'INTEGER'),
    ('retweet_id', 'TEXT'),
    ('ip', 'TEXT'),
    ('user_authentication', 'TEXT'),
    ('vip_type', 'TEXT'),
    ('vip_level', 'INTEGER'),
)

FIELD_NAMES = frozenset(name for name, _ in WEIBO_COLUMNS)
IMAGE_COLUMNS = (
    ('weibo_id', 'TEXT NOT NULL'),
    ('seq', 'INTEGER NOT NULL CHECK (seq >= 0)'),
    ('image_key', 'BLOB NOT NULL CHECK (length(image_key) = 32)'),
    ('url', 'TEXT'),
    ('image', 'BLOB NOT NULL'),
)

_OPTIONAL_IMAGE_FIELDS = frozenset({'weibo_image.url'})
_REQUIRED_FIELDS = frozenset({
    'id', 'weibo_image.weibo_id', 'weibo_image.seq',
    'weibo_image.image_key', 'weibo_image.image',
})


def _validate_drop_fields(drop_fields):
    excluded = set(drop_fields)
    required = excluded & _REQUIRED_FIELDS
    if required:
        raise ValueError('双表存储必须保留字段: %s' % ', '.join(sorted(required)))
    unknown = excluded - FIELD_NAMES - _OPTIONAL_IMAGE_FIELDS
    if unknown:
        raise ValueError('未知SQLite字段: %s' % ', '.join(sorted(unknown)))
    return excluded


def create_table_sql(drop_fields=()):
    excluded = _validate_drop_fields(drop_fields)
    definitions = ['"%s" %s' % (name, kind)
                   for name, kind in WEIBO_COLUMNS if name not in excluded]
    return 'CREATE TABLE weibo (%s)' % ', '.join(definitions)


def create_image_table_sql(drop_fields=()):
    excluded = _validate_drop_fields(drop_fields)
    definitions = ['"%s" %s' % (name, kind) for name, kind in IMAGE_COLUMNS
                   if 'weibo_image.' + name not in excluded]
    definitions.append('PRIMARY KEY (weibo_id, seq)')
    return 'CREATE TABLE weibo_image (%s)' % ', '.join(definitions)


def image_field_names(drop_fields=()):
    """返回当前配置的新图片表列名，同时校验字段排除配置。"""
    excluded = _validate_drop_fields(drop_fields)
    return frozenset(name for name, _ in IMAGE_COLUMNS
                     if 'weibo_image.' + name not in excluded)
