"""
安全的日志输出工具，避免I/O operation on closed file错误
优先使用已加载的 app.log_print，其次 utils.logger，未加载时使用本地回退输出。
"""
import sys
import os
import threading
from datetime import datetime


def _get_logger_log_print():
    """从 utils.logger 模块获取 log_print，避免循环导入。"""
    # 优先尝试 app 模块（兼容旧代码路径与单测桩）
    app_module = sys.modules.get("app")
    if app_module:
        app_log_print = getattr(app_module, "log_print", None)
        log_level = getattr(app_module, "LOG_LEVEL", {})
        if not isinstance(log_level, dict):
            log_level = {}
        if callable(app_log_print):
            return app_log_print, log_level

    # 回退：尝试 utils.logger（拆分后的日志模块）
    logger_module = sys.modules.get("utils.logger")
    if logger_module:
        lp = getattr(logger_module, "log_print", None)
        ll = getattr(logger_module, "LOG_LEVEL", {})
        if callable(lp):
            return lp, ll if isinstance(ll, dict) else {}

    return None, {}


def safe_print(message, log_type='INFO', force=False):
    """
    安全的日志输出函数，支持日志级别控制

    Args:
        message: 要输出的消息
        log_type: 日志类型 ('INFO', 'ERROR', 'DEBUG', etc.)
        force: 是否强制输出
    """
    logger_func, log_level = _get_logger_log_print()
    if logger_func is not None:
        logger_func(message, log_type, force)
        return

    # 回退方案：logger 尚未加载时使用原始输出
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
