# -*- coding: utf-8 -*-

# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html

import copy
import csv
from contextlib import closing
import hashlib
import os
from pathlib import Path

import scrapy
from scrapy.exceptions import DropItem
from scrapy.pipelines.files import FileException, FilesPipeline
from scrapy.pipelines.images import ImagesPipeline
from scrapy.utils.defer import deferred_from_coro
from scrapy.utils.project import get_project_settings

from weibo.utils.sqlite_schema import (
    FIELD_NAMES, SCHEMA_VERSION, WEIBO_COLUMNS, create_image_table_sql,
    create_table_sql, image_field_names,
)

settings = get_project_settings()


def normalize_pics(pics):
    if not pics:
        return ''
    if isinstance(pics, str):
        return pics
    return ','.join(pics)


class CsvPipeline(object):
    def process_item(self, item, spider):
        base_dir = '结果文件' + os.sep + item['keyword']
        if not os.path.isdir(base_dir):
            os.makedirs(base_dir)
        file_path = base_dir + os.sep + item['keyword'] + '.csv'
        if not os.path.isfile(file_path):
            is_first_write = 1
        else:
            is_first_write = 0

        if item:
            with open(file_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                if is_first_write:
                    header = [
                        'id', 'bid', 'user_id', '用户昵称', '微博正文', '头条文章url',
                        '发布位置', '艾特用户', '话题', '转发数', '评论数', '点赞数', '发布时间',
                        '发布工具', '微博图片url', '微博视频url', 'retweet_id', 'ip', 'user_authentication',
                        '会员类型', '会员等级'
                    ]
                    writer.writerow(header)

                writer.writerow([
                    item['weibo'].get('id', ''),
                    item['weibo'].get('bid', ''),
                    item['weibo'].get('user_id', ''),
                    item['weibo'].get('screen_name', ''),
                    item['weibo'].get('text', ''),
                    item['weibo'].get('article_url', ''),
                    item['weibo'].get('location', ''),
                    item['weibo'].get('at_users', ''),
                    item['weibo'].get('topics', ''),
                    item['weibo'].get('reposts_count', ''),
                    item['weibo'].get('comments_count', ''),
                    item['weibo'].get('attitudes_count', ''),
                    item['weibo'].get('created_at', ''),
                    item['weibo'].get('source', ''),
                    normalize_pics(item['weibo'].get('pics', [])),
                    item['weibo'].get('video_url', ''),
                    item['weibo'].get('retweet_id', ''),
                    item['weibo'].get('ip', ''),
                    item['weibo'].get('user_authentication', ''),
                    item['weibo'].get('vip_type', ''),
                    item['weibo'].get('vip_level', 0)
                ])
        return item

class SQLitePipeline(object):
    def __init__(self):
        self.crawler = None
        self.failed = False
        self.closing = False

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    def open_spider(self, spider):
        try:
            import sqlite3
            # 在结果文件目录下创建SQLite数据库
            base_dir = '结果文件'
            if not os.path.isdir(base_dir):
                os.makedirs(base_dir)
            runtime_settings = (self.crawler.settings if self.crawler is not None
                                else getattr(spider, 'settings', settings))
            db_name = runtime_settings.get('SQLITE_DATABASE', 'weibo_new.db')
            drop_fields = runtime_settings.getlist('SQLITE_DROP_FIELDS', [])
            text_sql = create_table_sql(drop_fields)
            image_sql = create_image_table_sql(drop_fields)
            self.columns = {name for name, _ in WEIBO_COLUMNS if name not in drop_fields}
            self.image_columns = image_field_names(drop_fields)
            db_path = Path(os.path.join(base_dir, db_name)).resolve()
            existing = self._check_existing_database(db_path)
            self.conn = sqlite3.connect(str(db_path))
            self.cursor = self.conn.cursor()
            if not existing:
                self.conn.execute('BEGIN')
                self.cursor.execute(text_sql)
                self.cursor.execute(image_sql)
                self.cursor.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
            if {'created_at', 'id'} <= self.columns:
                self.cursor.execute('CREATE INDEX IF NOT EXISTS '
                                    'idx_weibo_created_at_id '
                                    'ON weibo(created_at DESC, id DESC)')
            self.conn.commit()
        except Exception as e:
            spider.logger.error("SQLite数据库创建失败: %s", e)
            spider.sqlite3_error = True
            self.failed = True
            self.close_spider(spider)
            raise RuntimeError('SQLite数据库初始化失败') from e

    def _check_existing_database(self, db_path):
        """先以只读方式核对版本与列，旧库不打开写入、不迁移。"""
        if not db_path.exists():
            return False
        import sqlite3
        with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as probe:
            tables = {row[0] for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                return False
            version = probe.execute('PRAGMA user_version').fetchone()[0]
            text_columns = {row[1] for row in probe.execute('PRAGMA table_info(weibo)')}
            image_columns = {row[1] for row in probe.execute('PRAGMA table_info(weibo_image)')}
            if (version != SCHEMA_VERSION or tables != {'weibo', 'weibo_image'}
                    or text_columns != self.columns or image_columns != self.image_columns):
                raise ValueError('检测到旧版或字段配置不同的数据库，请通过SQLITE_DATABASE指定新的文件；现有数据库不会被修改')
        return True

    def _stop_crawl(self, spider):
        """存储失败时立即请求正常关闭，不等待下一条微博触发检查。"""
        crawler = self.crawler or getattr(spider, 'crawler', None)
        engine = getattr(crawler, 'engine', None)
        if engine is None or self.closing:
            return
        self.closing = True
        if hasattr(engine, 'close_spider_async'):
            deferred_from_coro(engine.close_spider_async(reason='sqlite_error'))
        else:
            engine.close_spider(spider, reason='sqlite_error')

    def process_item(self, item, spider):
        if self.failed:
            item['weibo'].pop('images', None)
            raise DropItem('SQLite已停止写入，请检查数据库错误后重新采集')
        try:
            data = dict(item['weibo'])
            images = data.pop('images', None) or []
            unknown = set(data) - FIELD_NAMES
            if unknown:
                raise ValueError('未知微博字段: %s' % ', '.join(sorted(unknown)))
            pics = data.get('pics', [])
            data['pics'] = normalize_pics(data.get('pics', []))
            # 只写入新库实际保留的列；不会对已有数据库增加或删除字段。
            data = {key: value for key, value in data.items() if key in self.columns}
            keys = ', '.join('"%s"' % key for key in data)
            placeholders = ', '.join(['?'] * len(data))
            updates = ', '.join('"%s"=excluded."%s"' % (key, key)
                                for key in data if key != 'id')
            conflict = ('DO UPDATE SET ' + updates) if updates else 'DO NOTHING'
            sql = ('INSERT INTO weibo (%s) VALUES (%s) '
                   'ON CONFLICT(id) %s' % (keys, placeholders, conflict))
            self.cursor.execute(sql, tuple(data.values()))
            self._save_images(data['id'], pics, images)
            self.conn.commit()
        except Exception as e:
            spider.logger.error("SQLite保存出错: %s", e)
            spider.sqlite3_error = True
            self.failed = True
            try:
                self.conn.rollback()
            except Exception as rollback_error:
                spider.logger.error('SQLite回滚失败: %s', rollback_error)
            finally:
                item['weibo'].pop('images', None)
                self._stop_crawl(spider)
            raise DropItem('SQLite保存失败: %s' % e) from e
        # 图片已经持久化，不再随 item 留在后续信号、日志或其它引用中。
        item['weibo'].pop('images', None)
        return item

    def _save_images(self, weibo_id, pics, images):
        """逐图保存原始字节；来源摘要用于重排、补图，不依赖持久化URL。"""
        if not isinstance(images, (list, tuple)):
            raise ValueError('图片下载结果必须是列表')
        downloaded = {}
        for seq, image in enumerate(images):
            if image is None:
                continue
            if not isinstance(image, (list, tuple)) or len(image) != 2:
                raise ValueError('图片下载结果必须包含URL和二进制数据')
            url, content = image
            if not isinstance(url, str) or not isinstance(content, (bytes, bytearray, memoryview)) or not content:
                raise ValueError('图片URL或二进制数据无效')
            downloaded[seq] = {'weibo_id': weibo_id, 'seq': seq, 'url': url,
                               'image_key': hashlib.sha256(url.encode('utf-8')).digest(),
                               'image': bytes(content)}
        if not downloaded:
            return  # 全部失败或未启用下载时，保留此前成功保存的图片。
        fields = ['weibo_id', 'seq', 'image_key', 'image']
        if 'url' in self.image_columns:
            fields.insert(3, 'url')
        old_rows = self.cursor.execute(
            'SELECT %s FROM weibo_image WHERE weibo_id=?' % ','.join(fields),
            (weibo_id,)).fetchall()
        old_by_key = {}
        for row in old_rows:
            record = dict(zip(fields, row))
            old_by_key[record['image_key']] = record
        urls = normalize_pics(pics).split(',') if pics else []
        records = []
        for seq in range(max(len(images), len(urls))):
            record = downloaded.get(seq)
            if record is None and seq < len(urls):
                key = hashlib.sha256(urls[seq].encode('utf-8')).digest()
                previous = old_by_key.get(key)
                if previous:
                    record = {**previous, 'seq': seq}
            if record is not None:
                records.append(tuple(record[field] for field in fields))
        self.cursor.execute('DELETE FROM weibo_image WHERE weibo_id=?', (weibo_id,))
        self.cursor.executemany(
            'INSERT INTO weibo_image (%s) VALUES (%s)' % (
                ','.join(fields), ','.join('?' for _ in fields)), records)

    def close_spider(self, spider):
        if getattr(self, 'conn', None) is not None:
            self.conn.close()
            self.conn = None

class MyImagesPipeline(ImagesPipeline):
    def _cache_result_and_execute_waiters(self, result, fp, info):
        """保留并发下载共享机制，但不长期缓存包含图片字节的结果。

        父类会将下载结果一直缓存到爬虫关闭；这里分发给所有等待者后，
        立即移除成功结果。已完成的 URL 再次出现时可以重新下载，避免
        图片总量决定整个爬取过程的内存占用。
        """
        super()._cache_result_and_execute_waiters(result, fp, info)
        if isinstance(result, dict) and 'data' in result:
            info.downloaded.pop(fp, None)

    def get_media_requests(self, item, info):
        for image_url in item['weibo']['pics']:
            yield scrapy.Request(image_url, meta={'item': item})

    async def media_downloaded(self, response, request, info, *, item=None):
        """下载后直接返回图片字节,不写磁盘。

        完全覆写父类:父类会调用 file_path 把文件落盘并返回
        {'url','path','checksum'};这里直接拿 response.body 作为字节
        数据随 item 传给 SQLitePipeline 入库。
        """
        if response.status != 200:
            raise FileException('download-error: status=%s' %
                                response.status)
        if not response.body:
            raise FileException('empty-content')
        return {
            'url': request.url,
            'data': response.body,
            'checksum': hashlib.md5(response.body).hexdigest(),
            'status': 'downloaded',
        }

    def item_completed(self, results, item, info):
        """把下载的图片字节写回 item,绝不 DropItem 以保证文本入库。

        不调用 super().item_completed:父类在图片全部下载失败时会
        raise DropItem,导致文本记录也一并丢失。
        """
        pics = item['weibo'].get('pics', [])
        # 元素为 (url, bytes),下载失败的位置 None 占位,与 pics 一一对应
        images = [None] * len(pics)
        # results 顺序与 get_media_requests 的请求顺序一致
        for i, (success, result_info) in enumerate(results):
            if success and i < len(images):
                data = result_info.get('data')
                if data:
                    images[i] = (pics[i], data)
        item['weibo']['images'] = images
        return item


class MyVideoPipeline(FilesPipeline):
    def get_media_requests(self, item, info):
        if item['weibo']['video_url']:
            yield scrapy.Request(item['weibo']['video_url'],
                                 meta={'item': item})

    def file_path(self, request, response=None, info=None, *, item=None):
        # Scrapy 2.17 起 file_path 会传入 item 参数
        item = item if item is not None else request.meta.get('item')
        base_dir = '结果文件' + os.sep + item['keyword'] + os.sep + 'videos'
        if not os.path.isdir(base_dir):
            os.makedirs(base_dir)
        file_path = base_dir + os.sep + item['weibo']['id'] + '.mp4'
        return file_path


class MongoPipeline(object):
    def open_spider(self, spider):
        try:
            from pymongo import MongoClient
            self.client = MongoClient(settings.get('MONGO_URI'))
            self.db = self.client['weibo']
            self.collection = self.db['weibo']
        except ModuleNotFoundError:
            spider.pymongo_error = True

    def process_item(self, item, spider):
        try:
            import pymongo

            new_item = copy.deepcopy(item)
            if not self.collection.find_one({'id': new_item['weibo']['id']}):
                self.collection.insert_one(dict(new_item['weibo']))
            else:
                self.collection.update_one({'id': new_item['weibo']['id']},
                                           {'$set': dict(new_item['weibo'])})
        except pymongo.errors.ServerSelectionTimeoutError:
            spider.mongo_error = True

    def close_spider(self, spider):
        try:
            self.client.close()
        except AttributeError:
            pass


class MysqlPipeline(object):
    def create_database(self, mysql_config):
        """创建MySQL数据库"""
        import pymysql
        sql = """CREATE DATABASE IF NOT EXISTS %s DEFAULT
            CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""" % settings.get(
            'MYSQL_DATABASE', 'weibo')
        db = pymysql.connect(**mysql_config)
        cursor = db.cursor()
        cursor.execute(sql)
        db.close()

    def create_table(self):
        """创建MySQL表"""
        sql = """
                CREATE TABLE IF NOT EXISTS weibo (
                id varchar(20) NOT NULL,
                bid varchar(12) NOT NULL,
                user_id varchar(20),
                screen_name varchar(30),
                text varchar(2000),
                article_url varchar(100),
                topics varchar(200),
                at_users varchar(1000),
                pics varchar(3000),
                video_url varchar(1000),
                location varchar(100),
                created_at DATETIME,
                source varchar(30),
                attitudes_count INT,
                comments_count INT,
                reposts_count INT,
                retweet_id varchar(20),
                PRIMARY KEY (id),
                ip varchar(100),
                user_authentication varchar(100),
                vip_type varchar(50),
                vip_level INT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        self.cursor.execute(sql)

    def open_spider(self, spider):
        try:
            import pymysql
            mysql_config = {
                'host': settings.get('MYSQL_HOST', 'localhost'),
                'port': settings.get('MYSQL_PORT', 3306),
                'user': settings.get('MYSQL_USER', 'root'),
                'password': settings.get('MYSQL_PASSWORD', '123456'),
                'charset': 'utf8mb4'
            }
            self.create_database(mysql_config)
            mysql_config['db'] = settings.get('MYSQL_DATABASE', 'weibo')
            self.db = pymysql.connect(**mysql_config)
            self.cursor = self.db.cursor()
            self.create_table()
        except ImportError:
            spider.pymysql_error = True
        except pymysql.OperationalError:
            spider.mysql_error = True

    def process_item(self, item, spider):
        data = dict(item['weibo'])
        data['pics'] = normalize_pics(data.get('pics', []))
        keys = ', '.join(data.keys())
        values = ', '.join(['%s'] * len(data))
        sql = """INSERT INTO {table}({keys}) VALUES ({values}) ON
                     DUPLICATE KEY UPDATE""".format(table='weibo',
                                                    keys=keys,
                                                    values=values)
        update = ','.join([" {key} = {key}".format(key=key) for key in data])
        sql += update
        try:
            self.cursor.execute(sql, tuple(data.values()))
            self.db.commit()
        except Exception:
            self.db.rollback()
        return item

    def close_spider(self, spider):
        try:
            self.db.close()
        except Exception:
            pass


class DuplicatesPipeline(object):
    def __init__(self):
        self.ids_seen = set()

    def process_item(self, item, spider):
        if item['weibo']['id'] in self.ids_seen:
            raise DropItem("过滤重复微博: %s" % item)
        else:
            self.ids_seen.add(item['weibo']['id'])
            return item
