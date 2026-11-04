"""每个任务独立的 Scrapy 进程；控制文件用于跨平台优雅暂停。"""
import json
import sys
from pathlib import Path

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from weibo.admin.progress import atomic_json


def main():
    folder = Path(sys.argv[1]).resolve()
    config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
    settings = get_project_settings()
    settings.setdict(config, priority='cmdline')
    settings.set('ADMIN_JOB_DIR', str(folder), priority='cmdline')
    settings.set('EXTENSIONS', {'weibo.admin.progress.Progress': 500}, priority='cmdline')
    process = CrawlerProcess(settings)
    def failed(failure):
        try:
            progress = json.loads((folder / 'progress.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            progress = {}
        message = failure.getErrorMessage()
        progress['reason'] = 'sqlite_error' if 'SQLite' in message else 'startup_error'
        atomic_json(folder / 'progress.json', progress)
    process.crawl('search').addErrback(failed)
    process.start()


if __name__ == '__main__':
    main()
