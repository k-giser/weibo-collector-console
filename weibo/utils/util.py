import re
from datetime import datetime, timedelta


def convert_weibo_type(weibo_type):
    """将微博类型转换成字符串"""
    if weibo_type == 0:
        return '&typeall=1'
    elif weibo_type == 1:
        return '&scope=ori'
    elif weibo_type == 2:
        return '&xsort=hot'
    elif weibo_type == 3:
        return '&atten=1'
    elif weibo_type == 4:
        return '&vip=1'
    elif weibo_type == 5:
        return '&category=4'
    elif weibo_type == 6:
        return '&viewpoint=1'
    return '&scope=ori'


def convert_contain_type(contain_type):
    """将包含类型转换成字符串"""
    if contain_type == 0:
        return '&suball=1'
    elif contain_type == 1:
        return '&haspic=1'
    elif contain_type == 2:
        return '&hasvideo=1'
    elif contain_type == 3:
        return '&hasmusic=1'
    elif contain_type == 4:
        return '&haslink=1'
    return '&suball=1'


def get_keyword_list(file_name):
    """获取文件中的关键词列表"""
    with open(file_name, 'rb') as f:
        try:
            lines = f.read().splitlines()
            lines = [line.decode('utf-8-sig') for line in lines]
        except UnicodeDecodeError:
            raise ValueError('%s文件应为utf-8编码，请先将文件编码转为utf-8再运行程序' %
                             file_name)
        keyword_list = []
        for line in lines:
            if line:
                keyword_list.append(line)
    return keyword_list


def standardize_date(created_at):
    """标准化微博发布时间。

    新版 s.weibo.com(2026-08)页面时间为中文格式
    '2023年07月31日 23:59',旧版为 '2023-07-31 23:59' 或当年内的
    'MM-DD HH:MM',这里统一转成 'YYYY-MM-DD HH:MM' 文本(便于
    SQL 字符串比较与按日期切片)。
    """
    created_at = ' '.join(created_at.split())
    if "刚刚" in created_at:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    elif "秒" in created_at:
        second = created_at[:created_at.find(u"秒")]
        second = timedelta(seconds=int(second))
        created_at = (datetime.now() - second).strftime("%Y-%m-%d %H:%M")
    elif "分钟" in created_at:
        minute = created_at[:created_at.find(u"分钟")]
        minute = timedelta(minutes=int(minute))
        created_at = (datetime.now() - minute).strftime("%Y-%m-%d %H:%M")
    elif "小时" in created_at:
        hour = created_at[:created_at.find(u"小时")]
        hour = timedelta(hours=int(hour))
        created_at = (datetime.now() - hour).strftime("%Y-%m-%d %H:%M")
    elif "今天" in created_at:
        today = datetime.now().strftime('%Y-%m-%d')
        created_at = today + ' ' + created_at[2:]
    else:
        # 带年份:'2023年07月31日 23:59' / '2023-07-31 23:59'
        m = re.match(r'(\d{4})[年-](\d{1,2})[月-](\d{1,2})(日)?\s*(.*)', created_at)
        if m:
            year, month, day, _, time = m.groups()
            time = (time or '').strip()
            created_at = '%04d-%02d-%02d' % (
                int(year), int(month), int(day))
            if time:
                created_at += ' ' + time
        else:
            # 当年内省略年份:'MM-DD HH:MM'，兼容中文月日及多余空白。
            m = re.match(r'(\d{1,2})[月-](\d{1,2})日?\s*(.*)', created_at)
            if m:
                month, day, time = m.groups()
                created_at = '%04d-%02d-%02d' % (
                    datetime.now().year, int(month), int(day))
                if time:
                    created_at += ' ' + time.strip()
    return created_at


def str_to_time(text):
    """将字符串转换成时间类型"""
    result = datetime.strptime(text, '%Y-%m-%d')
    return result
