#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一日志系统 - 从 app.py 拆分
包含 log_print、safe_log_print、全局异常处理器等
"""

import os
import sys
import threading
import builtins
from datetime import datetime
from glob import glob

# ---------------------------------------------------------------------------
#  日志级别控制 — 从 .env 读取，默认全部开启
#  在 .env 中设置 LOG_<类型>=false 即可关闭对应类型的日志
#  例如: LOG_GIT=false  LOG_REQUEST=false  LOG_CACHE=false
# ---------------------------------------------------------------------------
_LOG_CATEGORIES = [
    # ---- 核心 ----
    'APP',       # 应用主要日志（启动/关闭/路由访问）
    'ERROR',     # 错误级别日志（始终建议开启）
    'INFO',      # 通用信息日志
    'DEBUG',     # 调试日志
    'LOGGING',   # 重载 print 产生的日志
    # ---- 版本控制 ----
    'GIT',       # Git 操作详细日志
    'SVN',       # SVN 操作详细日志
    # ---- 业务 ----
    'DIFF',      # Diff 计算详细日志
    'EXCEL',     # Excel 处理详细日志
    'CACHE',     # 缓存操作详细日志
    'TASK',      # 后台任务日志
    'WEEKLY',    # 周版本同步日志
    'SYNC',      # 状态同步日志
    'SCHEDULER', # 定时调度器日志
    # ---- HTTP / 安全 ----
    'REQUEST',   # HTTP 请求日志
    'API',       # API 调用日志
    'REPO',      # 仓库管理日志
    # ---- 基础设施 ----
    'DB',        # 数据库操作日志
    'CLEANUP',   # 清理任务日志
    'PERF',      # 性能计数日志
    'DELETE',    # 删除操作日志
    'TEST',      # 测试相关日志
]


def _build_log_level() -> dict:
    """根据 .env 中的 LOG_<TYPE> 环境变量构建日志开关字典
    - 默认所有日志类型开启 (True)
    - 在 .env 中设置 LOG_GIT=false 即可关闭 GIT 类型日志
    - LOG_ALL=false 可一次性关闭全部普通日志（ERROR 除外）
    """
    result = {}
    # 全局开关
    log_all = os.environ.get('LOG_ALL', 'true').lower() != 'false'
    for cat in _LOG_CATEGORIES:
        env_val = os.environ.get(f'LOG_{cat}', '').strip().lower()
        if env_val == 'false':
            result[f'{cat}_VERBOSE'] = False
        elif env_val == 'true':
            result[f'{cat}_VERBOSE'] = True
        else:
            # 未显式配置时取决于全局开关；ERROR 始终开启
            result[f'{cat}_VERBOSE'] = True if cat == 'ERROR' else log_all
    return result


LOG_LEVEL = _build_log_level()


def _get_log_dir() -> str:
    """Resolve log directory path.

    LOG_DIR env var can be used by tests to isolate side effects.
    """
    override = os.environ.get("LOG_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')


# 保存原始print函数 — 必须使用 sys.stdout.write 来确保不递归
# 在模块被 Flask 二次 import 时, builtins.print 可能已被重载,
# 这里直接用底层 IO 避免重复打印
_sys_for_log = sys


def _original_print(msg, **kwargs):
    """安全的底层输出, 直接写 sys.stdout, 不经过 builtins.print"""
    try:
        _sys_for_log.stdout.write(str(msg) + '\n')
    except Exception:
        pass


def log_print(message, log_type='INFO', force=False):
    """统一的日志输出函数，支持日志级别控制，自动添加时间戳，同时输出到控制台和文件
    优化版本：确保任何错误都不会影响日志输出的连续性
    """
    # 调试模式：检查环境变量
    DEBUG_LOG = os.environ.get('DEBUG_LOG', 'false').lower() == 'true'
    if not (force or LOG_LEVEL.get(f'{log_type}_VERBOSE', True)):
        return

    # 安全的消息处理函数

    def safe_str(obj):
        """安全地将对象转换为字符串"""
        try:
            if isinstance(obj, str):
                return obj

            return str(obj)

        except Exception:
            return '<无法转换的对象>'

    # 安全的时间戳生成

    def safe_timestamp():
        """安全地生成时间戳"""
        try:
            return datetime.now().strftime('[%Y-%m-%d %H:%M:%S]')

        except Exception:
            return '[时间戳错误]'

    # 安全的进程信息获取

    def safe_process_info():
        """安全地获取进程信息"""
        try:
            process_id = os.getpid()
            thread_id = threading.get_ident()
            return f"[PID:{process_id}][TID:{thread_id}]"

        except Exception:
            return '[进程信息错误]'

    # 安全的控制台输出

    def safe_console_print(msg):
        """安全地输出到控制台，完全避免flush操作"""
        try:
            # 检查标准输出是否可用
            if hasattr(sys.stdout, 'closed') and sys.stdout.closed:
                return False

            # 直接输出，完全不使用flush，让系统自动处理缓冲
            _original_print(msg)
            # 不执行任何flush操作，这是导致阻塞的根本原因
            # 让操作系统和Python解释器自动管理输出缓冲
            return True

        except (UnicodeEncodeError, UnicodeDecodeError):
            # 编码错误，尝试安全编码
            try:
                safe_msg = msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                _original_print(safe_msg)
                return True

            except Exception:
                try:
                    # 最后的尝试：ASCII安全模式
                    ascii_msg = msg.encode('ascii', errors='replace').decode('ascii')
                    _original_print(ascii_msg)
                    return True

                except Exception:
                    return False

        except Exception:
            # 其他所有错误都静默处理
            return False

    # 安全的文件输出

    def safe_file_print(msg):
        """安全地输出到文件，避免阻塞操作"""
        try:
            # 确保日志目录存在
            log_dir = _get_log_dir()
            if not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, 'runlog.log')
            # 使用行缓冲模式，避免手动flush造成的阻塞
            with open(log_file, 'a', encoding='utf-8', errors='replace', buffering=1) as f:
                f.write(msg + '\n')
                # 移除f.flush()调用，让系统自动处理缓冲，避免I/O阻塞
            return True

        except Exception:
            # 文件输出失败时静默处理
            return False

    # 主要处理逻辑
    try:
        # 安全地处理消息
        safe_message = safe_str(message)
        timestamp = safe_timestamp()
        process_info = safe_process_info()
        # 构建完整消息
        full_message = f"{timestamp}{process_info}{safe_message}"
        # 尝试输出到控制台
        console_success = safe_console_print(full_message)
        # 尝试输出到文件
        file_success = safe_file_print(full_message)
        # 调试信息：如果控制台输出失败，记录到文件
        if not console_success:
            try:
                debug_msg = f"[DEBUG] 控制台输出失败: {safe_message[:50]}..."
                safe_file_print(debug_msg)
            except Exception:
                pass

        # 如果两者都失败，尝试最基本的输出
        if not console_success and not file_success:
            try:
                # 最后的尝试：使用最基本的print，不经过safe_console_print
                sys.stderr.write(f"[LOG_ERROR]{safe_message}\n")
                sys.stderr.flush()
            except Exception:
                # 完全静默处理
                pass

    except Exception as e:
        # 如果主要逻辑都失败了，尝试最基本的错误输出
        try:
            sys.stderr.write(f"[LOG_CRITICAL_ERROR] 日志系统异常: {str(e)}\n")
            sys.stderr.flush()
        except Exception:
            # 完全静默处理
            pass


def _rotate_log_backups(log_file: str, max_backups: int = 10) -> None:
    """Rotate current log file to timestamped backup and trim old backups."""
    if not os.path.exists(log_file):
        return

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_base = f"{log_file}.bak.{timestamp}"
    backup_path = backup_base
    serial = 1
    while os.path.exists(backup_path):
        backup_path = f"{backup_base}_{serial}"
        serial += 1

    os.replace(log_file, backup_path)

    backup_pattern = f"{log_file}.bak.*"
    backup_files = sorted(glob(backup_pattern))
    if len(backup_files) > max_backups:
        for old_file in backup_files[: len(backup_files) - max_backups]:
            try:
                os.remove(old_file)
            except OSError:
                pass


def clear_log_file():
    """启动时轮转运行日志，并保留最多10个历史备份。"""
    try:
        log_dir = _get_log_dir()
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_file = os.path.join(log_dir, 'runlog.log')
        _rotate_log_backups(log_file, max_backups=10)
        with open(log_file, 'w', encoding='utf-8'):
            pass
        _original_print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]日志文件已轮转并初始化: {log_file}")
    except Exception as e:
        _original_print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]轮转日志文件失败: {e}")


def safe_log_print(*args, **kwargs):
    """重载的print函数，使用log_print进行安全输出"""
    try:
        # 将所有参数转换为字符串并连接
        message = ' '.join(str(arg) for arg in args)
        # 使用LOGGING类型的log_print输出（log_print会自动添加时间戳）
        log_print(message, 'LOGGING', force=False)
    except Exception:
        # 如果log_print失败，回退到原始print函数
        try:
            _original_print(*args, **kwargs)
        except Exception:
            # 如果原始print也失败，静默处理
            pass


def install_print_override(is_testing=False):
    """重载内置print函数（测试环境中跳过，避免干扰 pytest 输出）"""
    if not is_testing:
        builtins.print = safe_log_print


# 设置全局异常处理器，防止未捕获的异常中断日志输出


def global_exception_handler(exc_type, exc_value, exc_traceback):
    """全局异常处理器，确保异常不会中断日志输出"""
    try:
        import traceback
        error_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        log_print(f"❌ 未捕获的异常: {error_msg}", 'ERROR', force=True)
    except Exception:
        # 如果连异常处理都失败了，尝试最基本的输出
        try:
            _original_print(f"CRITICAL: 全局异常处理器失败")
        except Exception:
            pass

    # 调用原始的异常处理器
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def thread_exception_handler(args):
    """线程异常处理器，处理线程中的未捕获异常"""
    try:
        import traceback
        error_msg = ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log_print(f"❌ 线程异常 [{args.thread.name}]: {error_msg}", 'ERROR', force=True)
    except Exception:
        # 如果连异常处理都失败了，尝试最基本的输出
        try:
            _original_print(f"CRITICAL: 线程异常处理器失败")
        except Exception:
            pass


def install_exception_handlers():
    """安装全局和线程异常处理器"""
    sys.excepthook = global_exception_handler
    # 安装线程异常处理器（Python 3.8+）
    try:
        threading.excepthook = thread_exception_handler
    except AttributeError:
        # Python 3.7及以下版本不支持threading.excepthook
        pass
