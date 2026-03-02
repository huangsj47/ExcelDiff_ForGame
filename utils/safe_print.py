"""
安全的日志输出工具，避免I/O operation on closed file错误
统一委托给 app.log_print，确保所有日志经过统一的日志系统处理
"""
import sys
import os
import threading
from datetime import datetime


def _get_app_log_print():
    """延迟获取 app.log_print，避免循环导入"""
    try:
        from app import log_print as _app_log_print, LOG_LEVEL
        return _app_log_print, LOG_LEVEL
    except ImportError:
        return None, {}


def safe_print(message, log_type='INFO', force=False):
    """
    安全的日志输出函数，支持日志级别控制

    Args:
        message: 要输出的消息
        log_type: 日志类型 ('INFO', 'ERROR', 'DEBUG', etc.)
        force: 是否强制输出
    """
    app_log_print, log_level = _get_app_log_print()
    if app_log_print is not None:
        app_log_print(message, log_type, force)
        return

    # 回退方案：app.py 尚未加载时使用原始输出
    if not force and not log_level.get(f'{log_type}_VERBOSE', True):
        return
    try:
        timestamp = datetime.now().strftime('[%Y-%m-%d %H:%M:%S]')
        pid = os.getpid()
        tid = threading.get_ident()
        full_msg = f"{timestamp}[PID:{pid}][TID:{tid}]{message}"
        sys.stdout.write(full_msg + '\n')
    except Exception:
        pass


# 为了兼容性，提供一个全局的安全打印函数
def log_print(message, log_type='INFO', force=False):
    """统一的日志输出函数，支持日志级别控制"""
    safe_print(message, log_type, force)