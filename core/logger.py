# coding: utf-8

import os
import re
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from rich.logging import RichHandler


# 按天滚动出来的日志文件名形如 client_2026-10-18.log
_DATED_LOG_RE = re.compile(r'^(?P<stem>.+)_(?P<date>\d{4}-\d{2}-\d{2})\.log$')


class TruncatingFileHandler(RotatingFileHandler):
    """
    超过 maxBytes 后 truncate 文件，不保留任何备份文件

    另外在跨天时把当天日志重命名归档为 {name}_{YYYY-MM-DD}.log，
    这样"只保留最近 N 天"才有可依据的按天文件。
    """

    _TAIL_LINES = 10  # truncate 前保留的末尾行数

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_day = None

    def emit(self, record):
        # 跨天检查：同一天内不会重复调用 date 计算，几乎零开销
        day = date.fromtimestamp(record.created)
        if day != self._current_day:
            self._current_day = day
            self._rollover_by_date(day)
        super().emit(record)

    def _rollover_by_date(self, today: date) -> None:
        """把昨天及更早的日志内容归档成带日期的文件，新的一天从头写"""
        try:
            if not os.path.exists(self.baseFilename):
                return
            last_day = date.fromtimestamp(os.path.getmtime(self.baseFilename))
            if last_day >= today:
                return  # 已经是今天的文件，无需归档

            stem = os.path.splitext(self.baseFilename)[0]
            # 归档名要与 prune_logs 的 {name}_{YYYY-MM-DD}.log 规则对齐，
            # 否则归档出来的文件永远匹配不上、也就永远不会被清理
            if stem.endswith('_latest'):
                stem = stem[:-len('_latest')]
            target = f'{stem}_{last_day.isoformat()}.log'

            if self.stream:
                self.stream.close()
                self.stream = None
            os.replace(self.baseFilename, target)

            self.stream = open(self.baseFilename, 'w', encoding=self.encoding)
            self.stream.write(f'--- Log started {datetime.now()}\n')
            self.stream.flush()
        except Exception:
            # 归档失败不能影响日志写入，退化为清空重开
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self.stream = open(self.baseFilename, 'w', encoding=self.encoding)
                self.stream.flush()
            except Exception:
                pass

    def doRollover(self):
        # truncate 前先读旧文件末尾几行
        tail = ''
        try:
            with open(self.baseFilename, 'r', encoding=self.encoding) as f:
                lines = f.readlines()
                tail = ''.join(lines[-self._TAIL_LINES:]).rstrip()
        except Exception:
            pass

        if self.stream:
            self.stream.close()
            self.stream = None
        # 用 'w' 模式重开，直接清空文件从头写
        self.stream = open(self.baseFilename, 'w', encoding=self.encoding)
        self.stream.write(f'--- Log truncated at {datetime.now()}\n')
        if tail:
            self.stream.write(f'--- Last {self._TAIL_LINES} lines of previous entries:\n{tail}\n\n')
        self.stream.flush()


class Logger:
    """日志系统管理器"""

    _loggers = {}

    @classmethod
    def setup(cls, name: str, log_dir: str = None, level: str = 'INFO', max_bytes: int = 10 * 1024 * 1024, log_filename: str = None):
        """
        设置并返回一个日志记录器

        Args:
            name: 日志记录器名称（通常是 'server' 或 'client'）
            log_dir: 日志文件目录，默认为项目根目录下的 logs 文件夹
            level: 日志级别，可选值：'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
            max_bytes: 单个日志文件最大大小，默认 10MB
            backup_count: 保留的日志文件数量，默认 5 个
            log_filename: 自定义日志文件名前缀，如果不填则默认使用 name 或 'root'

        Returns:
            logging.Logger: 配置好的日志记录器
        """
        # 设置日志级别
        file_log_level = getattr(logging, level.upper(), logging.INFO)
        console_log_level = logging.WARNING  # 控制台强制只打印 WARNING 及以上

        # 如果已经初始化过，更新级别并返回
        if name in cls._loggers:
            logger = cls._loggers[name]
            logger.setLevel(min(file_log_level, console_log_level))
            for handler in logger.handlers:
                if isinstance(handler, RotatingFileHandler):
                    handler.setLevel(file_log_level)
                elif isinstance(handler, logging.StreamHandler):
                    handler.setLevel(console_log_level)
            return logger

        # 创建日志记录器
        logger = logging.getLogger(name if name else None)
        logger.setLevel(min(file_log_level, console_log_level))

        # 确保不会传播到 root logger
        if name:
            logger.propagate = False

        # 确定日志目录
        if log_dir is None:
            from config_client import BASE_DIR
            log_dir = os.path.join(BASE_DIR, 'logs')

        # 创建日志目录
        Path(log_dir).mkdir(parents=True, exist_ok=True)


        # 1. 文件处理器（根据传入 level 记录）
        file_name_prefix = log_filename or name or 'root'
        log_file = os.path.join(log_dir, f'{file_name_prefix}_latest.log')
        formatter = logging.Formatter(
            fmt='%(asctime)s.%(msecs)03d %(levelname)-5s [%(filename)20s:%(lineno)-3d] %(message)s',
            datefmt='%H:%M:%S'
        )
        file_handler = TruncatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            encoding='utf-8'
        )
        file_handler.setLevel(file_log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 2. 控制台处理器（固定 WARNING 及以上，使用 rich 渲染）
        stream_handler = RichHandler(
            level=console_log_level,
            rich_tracebacks=True,
            markup=True,
            show_path=False
        )
        logger.addHandler(stream_handler)

        # 缓存日志记录器
        cls._loggers[name] = logger

        return logger

    @classmethod
    def get_logger(cls, name: str):
        """
        获取已创建的日志记录器，如果不存在则创建一个默认的

        Args:
            name: 日志记录器名称

        Returns:
            logging.Logger: 日志记录器
        """
        if name not in cls._loggers:
            # 如果 logger 还没有被初始化，先创建一个默认的（INFO 级别）
            # 之后 core_client.py/core_server.py 会用正确的级别重新初始化
            return cls.setup(name, level='INFO')
        return cls._loggers[name]

    @classmethod
    def prune_logs(cls, keep_days: int = 1, log_dir: str = None) -> int:
        """
        删除过期日志，只保留最近 keep_days 天的

        处理两类文件：
        - {name}_{YYYY-MM-DD}.log  按天归档的，直接按文件名日期判断
        - {name}_latest.log        当天正在写的，保留不动

        Args:
            keep_days: 保留天数，1 表示只留今天
            log_dir: 日志目录，默认为项目根目录下的 logs

        Returns:
            删除的文件数量
        """
        keep_days = max(1, int(keep_days))
        cutoff = date.today() - timedelta(days=keep_days - 1)

        if log_dir is None:
            from config_client import BASE_DIR
            log_dir = os.path.join(BASE_DIR, 'logs')

        log_path = Path(log_dir)
        if not log_path.is_dir():
            return 0

        removed = 0
        for log_file in log_path.glob('*.log'):
            m = _DATED_LOG_RE.match(log_file.name)
            if not m:
                continue  # _latest.log 等非日期文件不处理
            try:
                file_day = date.fromisoformat(m.group('date'))
            except ValueError:
                continue
            if file_day < cutoff:
                try:
                    log_file.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


# 便捷函数
def setup_logger(name: str, log_dir: str = None, level: str = 'INFO', **kwargs):
    """设置日志记录器的便捷函数"""
    return Logger.setup(name, log_dir, level, **kwargs)


def get_logger(name: str):
    """获取日志记录器的便捷函数"""
    return Logger.get_logger(name)


def prune_logs(keep_days: int = 1, log_dir: str = None) -> int:
    """清理过期日志的便捷函数"""
    return Logger.prune_logs(keep_days, log_dir)
