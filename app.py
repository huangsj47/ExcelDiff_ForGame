import os
import sys
import json
import math
import threading
import queue
import time
import atexit
import signal
import schedule
import logging
import secrets
import hmac
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import Index, func, case
from services.git_service import GitService
from services.enhanced_git_service import EnhancedGitService
from services.threaded_git_service import ThreadedGitService
# ThreadedGitService配置说明:
# - 多线程优化前一次提交查找，显著提升大仓库性能
# - 默认使用CPU核心数+4个工作线程
# - 包含超时机制和异常降级处理
# - 与原GitService完全兼容

from services.svn_service import SVNService
from services.diff_service import DiffService
from services.excel_html_cache_service import ExcelHtmlCacheService
from utils.url_helpers import generate_commit_diff_url, generate_excel_diff_data_url, generate_refresh_diff_url
import threading
import queue
import logging
from utils.db_retry import db_retry
from utils.sqlite_config import set_sqlite_pragma  # 导入SQLite优化配置

    
from urllib.parse import urlparse
from os import system
from utils.security_utils import (
    decrypt_credential,
    encrypt_credential,
    sanitize_text,
    validate_repository_name,
)
from utils.path_security import build_repository_local_path

system("title SEOTool - diff-confirmation-platform")

# 设置控制台输出编码为UTF-8
if sys.platform == 'win32':
    import codecs
    import io
    # 设置UTF-8编码并启用错误处理
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    # 设置控制台代码页为UTF-8
    os.system('chcp 65001 >nul 2>&1')

# Diff逻辑版本号 - 当diff算法或逻辑发生变化时需要更新此版本号
DIFF_LOGIC_VERSION = "1.8.0"

LOG_LEVEL = {
    'APP_VERBOSE': True,      # 应用主要日志
    'GIT_VERBOSE': True,     # Git操作详细日志
    'CACHE_VERBOSE': True,   # 缓存操作详细日志
    'DIFF_VERBOSE': True,    # Diff计算详细日志
    'SVN_VERBOSE': True,     # SVN操作详细日志
    'EXCEL_VERBOSE': True,   # Excel处理详细日志
    'LOGGING_VERBOSE': True   # 通用日志输出（重载print函数使用）
}

def clean_json_data(data):
    """
    清理数据中的不可JSON序列化的值（如nan, inf等）
    
    Args:
        data: 待清理的数据
        
    Returns:
        清理后的数据
    """
    import math
    import json
    
    if isinstance(data, dict):
        return {k: clean_json_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [clean_json_data(item) for item in data]
    elif isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return None
        return data
    else:
        return data

def validate_excel_diff_data(diff_data):
    """
    验证Excel差异数据的完整性
    
    Args:
        diff_data: 待验证的差异数据
        
    Returns:
        tuple: (is_valid, message)
    """
    if not diff_data:
        return False, "diff_data为空"
    
    if not isinstance(diff_data, dict):
        return False, f"diff_data不是字典类型: {type(diff_data)}"
    
    # 检查必需字段
    required_fields = ['type', 'sheets']
    for field in required_fields:
        if field not in diff_data:
            return False, f"缺少必需字段: {field}"
    
    # 检查type字段
    if diff_data.get('type') != 'excel':
        return False, f"type字段不正确: {diff_data.get('type')}"
    
    # 检查sheets字段
    sheets = diff_data.get('sheets')
    if not isinstance(sheets, dict):
        return False, f"sheets字段不是字典类型: {type(sheets)}"
    
    # 检查是否有有效的工作表数据
    valid_sheets_count = 0
    total_rows = 0
    
    for sheet_name, sheet_data in sheets.items():
        if not isinstance(sheet_data, dict):
            continue
            
        rows = sheet_data.get('rows', [])
        if isinstance(rows, list) and len(rows) > 0:
            valid_sheets_count += 1
            total_rows += len(rows)
    
    # 如果所有工作表都没有数据，可能是有问题的
    if total_rows == 0:
        return False, f"所有工作表都没有差异数据 (共{len(sheets)}个工作表)"
    return True, f"验证通过: {valid_sheets_count}个有效工作表, 共{total_rows}行差异"


def log_print(message, log_type='INFO', force=False):
    """统一的日志输出函数，支持日志级别控制，自动添加时间戳，同时输出到控制台和文件

    优化版本：确保任何错误都不会影响日志输出的连续性
    """
    import sys
    import threading
    from datetime import datetime
    import os

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
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
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
                import sys
                sys.stderr.write(f"[LOG_ERROR]{safe_message}\n")
                sys.stderr.flush()
            except Exception:
                # 完全静默处理
                pass

    except Exception as e:
        # 如果主要逻辑都失败了，尝试最基本的错误输出
        try:
            import sys
            sys.stderr.write(f"[LOG_CRITICAL_ERROR] 日志系统异常: {str(e)}\n")
            sys.stderr.flush()
        except Exception:
            # 完全静默处理
            pass

# 保存原始print函数
_original_print = print

def clear_log_file():
    """清空运行日志文件"""
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        log_file = os.path.join(log_dir, 'runlog.log')
        # 清空文件内容
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write('')
        _original_print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]日志文件已清空: {log_file}")
    except Exception as e:
        _original_print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]清空日志文件失败: {e}")

def safe_json_serialize(obj):
    """安全的JSON序列化函数，处理NaN、Infinity等特殊值"""
    def clean_value(value):
        """递归清理数据中的特殊值"""
        if isinstance(value, dict):
            return {k: clean_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [clean_value(item) for item in value]
        elif isinstance(value, float):
            if math.isnan(value):
                return None  # 将NaN转换为null
            elif math.isinf(value):
                return None  # 将Infinity转换为null
            else:
                return value
        else:
            return value

    # 清理数据
    cleaned_obj = clean_value(obj)
    return cleaned_obj

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

# 重载内置print函数
import builtins
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

# 安装全局异常处理器
sys.excepthook = global_exception_handler

# 设置线程异常处理器
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

# 安装线程异常处理器（Python 3.8+）
try:
    threading.excepthook = thread_exception_handler
except AttributeError:
    # Python 3.7及以下版本不支持threading.excepthook
    pass

def get_excel_column_letter(index):
    """Convert column index to Excel column letter (A, B, C, ..., Z, AA, AB, ...)"""
    result = ""
    while index >= 0:
        result = chr(65 + (index % 26)) + result
        index = index // 26 - 1
    return result

def format_cell_value(value):
    """格式化单元格值，处理null、NaN等特殊值"""
    # 处理空值、null值、undefined值和NaN值
    if value is None or value == 'null' or value == 'None':
        return ''

    # 处理NaN值
    if isinstance(value, float) and math.isnan(value):
        return ''

    # 转换为字符串并去除多余空格
    str_value = str(value).strip()

    # 检查是否为字符串形式的NaN、null等
    if str_value.lower() in ['nan', 'null', 'undefined', '']:
        return ''

    return str_value

def get_unified_diff_data(commit, previous_commit=None):
    """使用新的统一差异服务获取差异数据（优化版本，优先使用缓存）"""
    repository = commit.repository
    start_time = time.time()

    try:
        log_print(f"🔧 统一差异服务开始处理: {commit.path}", 'DIFF', force=True)
        log_print(f"📂 当前提交: {commit.commit_id[:8]} | 前一提交: {previous_commit.commit_id[:8] if previous_commit else 'None'}", 'DIFF', force=True)

        # 如果是Excel文件，优先检查缓存
        is_excel = excel_cache_service.is_excel_file(commit.path)
        if is_excel:
            log_print(f"🔍 Excel文件，检查缓存: {commit.path}", 'CACHE')

            # 检查Excel diff缓存
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path
            )

            if cached_diff:
                cache_time = time.time() - start_time
                log_print(f"✅ 缓存命中，跳过实时计算: {commit.path} | 耗时: {cache_time:.2f}秒", 'CACHE')
                return json.loads(cached_diff.diff_data)
            else:
                log_print(f"❌ 缓存未命中，开始实时计算: {commit.path}", 'CACHE')

        # 如果没有前一提交，这可能是问题所在
        if previous_commit is None:
            log_print(f"⚠️ 警告: 没有前一提交，将与空版本比较 - 这可能导致显示为初始版本", 'DIFF', force=True)

        # 根据仓库类型获取文件内容
        if repository.type == 'git':
            # 获取当前版本文件内容
            current_content = get_file_content_from_git(repository, commit.commit_id, commit.path)
            # 获取前一版本文件内容
            previous_content = None
            if previous_commit:
                previous_content = get_file_content_from_git(repository, previous_commit.commit_id, commit.path)
        elif repository.type == 'svn':
            # 获取SVN文件内容
            current_content = get_file_content_from_svn(repository, commit.commit_id, commit.path)
            # 获取前一版本文件内容
            previous_content = None
            if previous_commit:
                previous_content = get_file_content_from_svn(repository, previous_commit.commit_id, commit.path)
        else:
            log_print(f"❌ 不支持的仓库类型: {repository.type}", 'DIFF', force=True)
            return {
                'type': 'error',
                'file_path': commit.path,
                'error': f'不支持的仓库类型: {repository.type}',
                'message': f'不支持的仓库类型: {repository.type}'
            }

        # 处理差异
        diff_service = DiffService()
        calc_start_time = time.time()
        diff_data = diff_service.process_diff(commit.path, current_content, previous_content)
        processing_time = time.time() - calc_start_time
        
        if diff_data:
            total_time = time.time() - start_time
            log_print(f"✅ 实时diff计算完成: {commit.path} | 类型: {diff_data.get('type', 'unknown')} | 计算耗时: {processing_time:.2f}秒 | 总耗时: {total_time:.2f}秒", 'DIFF')

            # 如果是Excel文件且没有缓存，保存到缓存
            if is_excel and diff_data.get('type') == 'excel':
                try:
                    excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,  # 传递原始对象，不要预先JSON编码
                        processing_time=processing_time,
                        file_size=0,
                        previous_commit_id=previous_commit.commit_id if previous_commit else None,
                        commit_time=commit.commit_time
                    )
                    log_print(f"💾 Excel diff结果已保存到缓存: {commit.path}", 'CACHE')
                except Exception as cache_error:
                    log_print(f"⚠️ 保存缓存失败: {cache_error}", 'CACHE')
        else:
            total_time = time.time() - start_time
            log_print(f"❌ 实时diff计算失败: {commit.path} | 耗时: {total_time:.2f}秒", 'DIFF', force=True)

        return diff_data

    except Exception as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 统一差异服务错误: {e} | 耗时: {total_time:.2f}秒", 'DIFF', force=True)
        return None

# 全局Git服务缓存，避免重复创建实例
_git_service_cache = {}
_git_service_lock = threading.Lock()

def get_git_service(repository):
    """获取Git服务实例（使用缓存避免重复创建）"""
    cache_key = f"{repository.id}_{repository.url}"

    with _git_service_lock:
        if cache_key not in _git_service_cache:
            from services.threaded_git_service import ThreadedGitService
            _git_service_cache[cache_key] = ThreadedGitService(
                repository.url, repository.root_directory,
                repository.username, repository.token,
                repository, active_git_processes
            )
            log_print(f"🔧 创建新的Git服务实例: {repository.name}", 'GIT')
        return _git_service_cache[cache_key]

# 全局SVN服务缓存，避免重复创建实例
_svn_service_cache = {}
_svn_service_lock = threading.Lock()

def get_svn_service(repository):
    """获取SVN服务实例（使用缓存避免重复创建）"""
    cache_key = f"{repository.id}_{repository.url}"

    with _svn_service_lock:
        if cache_key not in _svn_service_cache:
            from services.svn_service import SVNService
            _svn_service_cache[cache_key] = SVNService(repository)
            log_print(f"🔧 创建新的SVN服务实例: {repository.name}", 'SVN')
        return _svn_service_cache[cache_key]

def get_file_content_from_svn(repository, commit_id, file_path):
    """从SVN仓库获取指定提交的文件内容"""
    try:
        svn_service = get_svn_service(repository)

        # SVN的commit_id格式为r12345，需要提取数字部分
        revision = commit_id
        if revision.startswith('r'):
            revision = revision[1:]

        log_print(f"获取SVN文件内容: {file_path}@{revision}", 'SVN')

        # 确保本地仓库存在
        import os
        if not os.path.exists(svn_service.local_path):
            success, message = svn_service.checkout_or_update_repository()
            if not success:
                log_print(f"SVN仓库检出失败: {message}", 'SVN', force=True)
                return None

        # 使用本地工作目录的相对路径，与SVN服务的现有方法保持一致
        # 将绝对路径转换为相对路径
        relative_path = file_path
        if file_path.startswith('/trunk/ProjectMecury/RawData/'):
            # 去掉SVN路径前缀，只保留实际的文件路径
            relative_path = file_path[len('/trunk/ProjectMecury/RawData/'):]
        elif file_path.startswith('/trunk/'):
            # 去掉开头的/trunk/部分，因为本地工作目录已经是trunk
            relative_path = file_path[7:]  # 去掉'/trunk/'
        elif file_path.startswith('/'):
            # 去掉开头的/
            relative_path = file_path[1:]

        log_print(f"原始路径: {file_path}", 'SVN')
        log_print(f"转换后相对路径: {relative_path}", 'SVN')

        # 使用SVN cat命令获取文件内容
        import subprocess

        # 构建正确的SVN URL，避免路径重复
        # repository.url 格式: svn://svn-yy67.gz.netease.com/svn/trunk/ProjectMecury/RawData
        # file_path 格式: /trunk/ProjectMecury/RawData/City_Base/装备.xlsx
        # 需要去掉file_path中与repository.url重复的部分

        from urllib.parse import urlparse
        parsed_url = urlparse(repository.url)
        repo_path = parsed_url.path  # /svn/trunk/ProjectMecury/RawData

        # 从file_path中去掉与repo_path重复的部分
        if file_path.startswith('/trunk/ProjectMecury/RawData/'):
            # 只保留相对于仓库根目录的路径
            relative_file_path = file_path[len('/trunk/ProjectMecury/RawData/'):]
            # 对中文文件名进行URL编码
            from urllib.parse import quote
            encoded_file_path = quote(relative_file_path, safe='/')
            svn_url = f"{repository.url}/{encoded_file_path}@{revision}"
        else:
            # 如果路径格式不符合预期，直接拼接
            from urllib.parse import quote
            encoded_file_path = quote(file_path, safe='/')
            svn_url = f"{repository.url}{encoded_file_path}@{revision}"

        cmd = [svn_service.svn_executable, 'cat', svn_url]

        # 安全获取认证信息，避免SQLAlchemy会话问题
        try:
            username = getattr(repository, 'username', None)
            password = getattr(repository, 'password', None)
            if username and password:
                cmd.extend(['--username', username, '--password', password])
        except Exception as session_error:
            log_print(f"✗ 获取SVN认证信息失败: {session_error}", 'SVN', force=True)
            log_print(f"🔄 SVN操作因会话问题退出，不影响后续操作", 'SVN')
            return None

        # 添加非交互模式参数
        cmd.extend(['--non-interactive', '--trust-server-cert'])

        log_print(f"SVN cat命令: {' '.join(cmd[:2])} [URL和认证信息已隐藏]", 'SVN')
        log_print(f"SVN URL: {svn_url}", 'SVN')
        log_print(f"完整命令参数: {len(cmd)} 个参数", 'SVN')
        log_print(f"调试 - 完整命令: {cmd[:3] + ['[认证信息已隐藏]'] + cmd[7:]}", 'SVN')

        try:
            # SVN cat命令不需要工作目录，直接使用完整URL
            # 设置环境变量确保使用UTF-8编码
            import os
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['LC_ALL'] = 'en_US.UTF-8'

            result = subprocess.run(cmd, capture_output=True, text=False, timeout=30, cwd=None, env=env)

            if result.returncode == 0:
                # 直接返回二进制内容，不进行文本解码
                log_print(f"✅ SVN文件内容获取成功: {len(result.stdout)} 字节", 'SVN')
                return result.stdout  # 返回原始bytes格式
            else:
                error_msg = svn_service._decode_subprocess_output(result.stderr)
                log_print(f"❌ SVN文件内容获取失败: {error_msg}", 'SVN', force=True)
                return None

        except subprocess.TimeoutExpired:
            log_print("❌ SVN cat命令超时", 'SVN', force=True)
            return None

    except Exception as e:
        log_print(f"❌ 获取SVN文件内容异常: {str(e)}", 'SVN', force=True)
        return None

def get_file_content_from_git(repository, commit_id, file_path):
    """从Git仓库获取指定提交的文件内容"""
    try:
        import git
        import os

        # 使用缓存的GitService实例
        git_service = get_git_service(repository)

        log_print(f"检查本地路径: {git_service.local_path}", 'GIT')
        log_print(f"路径是否存在: {os.path.exists(git_service.local_path)}", 'GIT')
        if not os.path.exists(git_service.local_path):
            success, message = git_service.clone_or_update_repository()
            if not success:
                log_print(f"仓库克隆失败: {message}", 'GIT', force=True)
                return None
        
        repo = git.Repo(git_service.local_path)
        
        # 尝试获取完整的commit ID
        try:
            # 如果commit_id是短SHA，尝试获取完整SHA
            if len(commit_id) < 40:
                # 使用Git命令直接解析短SHA，避免遍历所有提交
                try:
                    full_sha = repo.git.rev_parse(commit_id)
                    commit_id = full_sha
                    log_print(f"短SHA解析成功: {commit_id[:8]} -> {full_sha[:8]}", 'GIT')
                except Exception as parse_e:
                    log_print(f"短SHA解析失败，尝试有限遍历: {parse_e}", 'GIT')
                    # 只遍历最近1000个提交，避免卡死
                    commits = list(repo.iter_commits(max_count=1000))
                    for c in commits:
                        if c.hexsha.startswith(commit_id):
                            commit_id = c.hexsha
                            log_print(f"在最近1000个提交中找到匹配: {commit_id[:8]}", 'GIT')
                            break
                    else:
                        log_print(f"在最近1000个提交中未找到匹配的短SHA: {commit_id}", 'GIT', force=True)
            
            commit = repo.commit(commit_id)
        except Exception as e:
            log_print(f"无法找到commit {commit_id}: {e}", 'GIT')
            # 尝试fetch最新数据
            try:
                repo.remotes.origin.fetch()
                commit = repo.commit(commit_id)
            except Exception as e2:
                log_print(f"fetch后仍无法找到commit: {e2}", 'GIT', force=True)
                return None
        
        try:
            blob = commit.tree[file_path]
            return blob.data_stream.read()
        except KeyError:
            log_print(f"文件在提交 {commit_id[:8]} 中不存在: {file_path}", 'GIT')
            return None
            
    except Exception as e:
        log_print(f"获取Git文件内容失败: {str(e)}", 'GIT', force=True)
        return None

app = Flask(__name__)

# 启用模板自动重载（开发环境）
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 启用CORS支持，允许跨域请求
secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
if not secret_key:
    secret_key = secrets.token_urlsafe(48)
    log_print("⚠️ FLASK_SECRET_KEY 未配置，已使用运行期随机密钥。生产环境必须显式配置。", "APP", force=True)

cors_allowed_origins = [origin.strip() for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
if cors_allowed_origins:
    CORS(app, resources={
        r"/status-sync/*": {"origins": cors_allowed_origins},
        r"/api/*": {"origins": cors_allowed_origins},
        r"/admin/*": {"origins": cors_allowed_origins},
    })
else:
    log_print("ℹ️ 未配置 CORS_ALLOWED_ORIGINS，默认禁用跨域访问。", "APP", force=True)

CSRF_SESSION_KEY = "_csrf_token"
ENABLE_ADMIN_SECURITY = os.environ.get("ENABLE_ADMIN_SECURITY", "true").lower() != "false"
SENSITIVE_ENDPOINTS = {
    'delete_repository',
    'delete_project',
    'batch_update_credentials',
    'clear_all_confirmation_status',
    'update_repository_order',
    'swap_repository_order',
    'create_git_repository',
    'create_svn_repository',
    'update_repository',
    'retry_clone_repository',
    'sync_repository',
    'reuse_repository_and_update',
    'update_repository_and_cache',
}


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _is_api_request():
    accept = request.headers.get("Accept", "")
    return (
        request.path.startswith("/api/")
        or request.path.startswith("/admin/")
        or request.is_json
        or "application/json" in accept
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _is_valid_admin_token():
    expected = os.environ.get("ADMIN_API_TOKEN", "").strip()
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    return bool(expected and provided and hmac.compare_digest(expected, provided))


def _has_admin_access():
    return bool(session.get("is_admin")) or _is_valid_admin_token()


def _unauthorized_admin_response():
    if _is_api_request():
        return jsonify({"success": False, "message": "Admin authentication required"}), 401
    next_url = request.url if request.url else url_for('index')
    flash('请先使用管理员账号登录。', 'error')
    return redirect(url_for('admin_login', next=next_url))


def _csrf_error_response(message):
    if _is_api_request():
        return jsonify({"success": False, "message": message}), 400
    flash(message, "error")
    return redirect(request.referrer or url_for('index'))


def _csrf_token_from_request():
    header_token = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
    if header_token:
        return header_token
    form_token = request.form.get("_csrf_token")
    if form_token:
        return form_token
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return payload.get("_csrf_token")
    return None


def _is_same_origin_request():
    expected_host = request.host
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        return parsed.netloc == expected_host
    referer = request.headers.get("Referer")
    if referer:
        parsed = urlparse(referer)
        return parsed.netloc == expected_host
    return True


def _is_safe_redirect(target):
    if not target:
        return False
    parsed = urlparse(target)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc and parsed.netloc != request.host:
        return False
    return True


def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not ENABLE_ADMIN_SECURITY:
            return func(*args, **kwargs)
        if not _has_admin_access():
            return _unauthorized_admin_response()
        return func(*args, **kwargs)
    return wrapper


@app.before_request
def log_request_info():
    """Record incoming request info for admin routes."""
    if request.path.startswith('/admin/'):
        log_print(f"[REQUEST] {request.method} {request.path}", 'REQUEST', force=True)


@app.before_request
def enforce_admin_access():
    if not ENABLE_ADMIN_SECURITY:
        return None
    if request.endpoint in {'static', 'admin_login', 'admin_logout'}:
        return None
    if request.path.startswith('/admin/') or request.endpoint in SENSITIVE_ENDPOINTS:
        if not _has_admin_access():
            return _unauthorized_admin_response()
    return None


@app.before_request
def enforce_csrf():
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None
    if request.endpoint in {'static'}:
        return None
    if _is_valid_admin_token():
        return None
    expected = session.get(CSRF_SESSION_KEY)
    provided = _csrf_token_from_request()
    if not (expected and provided and hmac.compare_digest(str(expected), str(provided))):
        return _csrf_error_response("CSRF token invalid or missing.")
    if not _is_same_origin_request():
        return _csrf_error_response("Cross-site request blocked.")
    return None

# Add the function to Jinja2 template globals
app.jinja_env.globals['get_excel_column_letter'] = get_excel_column_letter
app.jinja_env.globals['csrf_token'] = csrf_token
app.config['SECRET_KEY'] = secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.abspath("instance/diff_platform.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = secret_key

db = SQLAlchemy(app)

# 添加Excel列字母转换过滤器
@app.template_filter('excel_column_letter')
def excel_column_letter(index):
    """将数字索引转换为Excel列字母 (0->A, 1->B, ..., 25->Z, 26->AA)"""
    result = ""
    while index >= 0:
        result = chr(65 + (index % 26)) + result
        index = index // 26 - 1
        if index < 0:
            break
    return result

# 添加单元格值格式化过滤器
@app.template_filter('format_cell_value')
def format_cell_value_filter(value):
    """格式化单元格值，处理null、NaN等特殊值"""
    return format_cell_value(value)

# 全局变量存储Git进程
active_git_processes = set()

# Excel diff 状态统一走数据库缓存与任务队列，不再使用进程内字典状态。

# 后台任务队列和状态 - 使用优先级队列
background_task_queue = queue.PriorityQueue()
background_task_running = False
background_task_thread = None

# 任务包装类，避免字典比较问题
class TaskWrapper:
    def __init__(self, priority, counter, task_data):
        self.priority = priority
        self.counter = counter
        self.task_data = task_data
    
    def __lt__(self, other):
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.counter < other.counter
    
    def __eq__(self, other):
        return self.priority == other.priority and self.counter == other.counter

# 清理Git进程的函数
def cleanup_git_processes():
    """清理所有活跃的Git进程"""
    # log_print("正在清理Git进程...", 'INFO')
    for proc in list(active_git_processes):
        try:
            if proc.poll() is None:  # 进程仍在运行
                proc.terminate()
                proc.wait(timeout=5)
            active_git_processes.discard(proc)
        except Exception as e:
            log_print(f"清理Git进程时出错: {e}", 'GIT', force=True)
            try:
                proc.kill()
                active_git_processes.discard(proc)
            except:
                pass
    # log_print("Git进程清理完成", 'INFO')

# 注册清理函数
atexit.register(cleanup_git_processes)

# 处理信号 - 只在主线程中注册
def signal_handler(signum, frame):
    cleanup_git_processes()
    sys.exit(0)

# 只在主线程中注册信号处理器
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

# 数据库模型
class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    repositories = db.relationship('Repository', backref='project', lazy=True, cascade='all, delete-orphan')

# 全局仓库ID计数器表
class GlobalRepositoryCounter(db.Model):
    __tablename__ = 'global_repository_counter'
    id = db.Column(db.Integer, primary_key=True)
    max_repository_id = db.Column(db.Integer, default=0, nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Repository(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'svn' or 'git'
    category = db.Column(db.String(50))
    url = db.Column(db.String(500), nullable=False)
    server_url = db.Column(db.String(500))
    root_directory = db.Column(db.String(500))
    username = db.Column(db.String(100))
    _password = db.Column('password', db.String(512))
    _token = db.Column('token', db.String(512))
    branch = db.Column(db.String(100))
    resource_type = db.Column(db.String(20))  # 'table', 'res', 'code'
    current_version = db.Column(db.String(50))
    path_regex = db.Column(db.Text)
    log_regex = db.Column(db.Text)
    log_filter_regex = db.Column(db.Text)
    commit_filter = db.Column(db.Text)
    important_tables = db.Column(db.Text)
    display_order = db.Column(db.Integer, default=0)
    unconfirmed_history = db.Column(db.Boolean, default=False)
    delete_table_alert = db.Column(db.Boolean, default=False)
    weekly_version_setting = db.Column(db.String(100))
    # Table配置字段
    header_rows = db.Column(db.Integer)
    key_columns = db.Column(db.String(200))
    enable_id_confirmation = db.Column(db.Boolean, default=False)
    show_duplicate_id_warning = db.Column(db.Boolean, default=False)
    tag_selection = db.Column(db.String(500))
    # 克隆状态字段
    clone_status = db.Column(db.String(20), default='pending')  # pending, cloning, completed, failed
    clone_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # 增量缓存同步字段
    last_sync_commit_id = db.Column(db.String(100))
    last_sync_time = db.Column(db.DateTime)
    cache_version = db.Column(db.String(20))
    sync_mode = db.Column(db.String(20), default='full')
    # Git提交日期范围配置
    start_date = db.Column(db.DateTime)  # Git提交获取的起始日期
    commits = db.relationship('Commit', backref='repository', lazy=True, cascade='all, delete-orphan')

    @property
    def password(self):
        return decrypt_credential(self._password)

    @password.setter
    def password(self, value):
        self._password = encrypt_credential(value)

    @property
    def token(self):
        return decrypt_credential(self._token)

    @token.setter
    def token(self, value):
        self._token = encrypt_credential(value)

class Commit(db.Model):
    __tablename__ = 'commits_log'
    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(100), nullable=False)
    path = db.Column(db.String(500))
    version = db.Column(db.String(50))
    operation = db.Column(db.String(10))  # 'A', 'M', 'D'
    author = db.Column(db.String(100))
    commit_time = db.Column(db.DateTime)
    message = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'confirmed', 'rejected'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class DiffCache(db.Model):
    """Excel文件差异缓存表"""
    __tablename__ = 'diff_cache'
    
    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    previous_commit_id = db.Column(db.String(255))
    diff_data = db.Column(db.Text)  # JSON格式存储差异数据
    file_size = db.Column(db.Integer, default=0)
    processing_time = db.Column(db.Float, default=0.0)
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    error_message = db.Column(db.Text)
    diff_version = db.Column(db.String(20), default=DIFF_LOGIC_VERSION)  # Diff逻辑版本号
    commit_time = db.Column(db.DateTime)  # 提交时间（文件实际提交的时间）
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))  # 缓存生成时间
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    is_long_processing = db.Column(db.Boolean, default=False)  # 是否为耗时超过10秒的文件
    expire_at = db.Column(db.DateTime)  # 缓存过期时间
    
    # 添加索引以提高查询性能
    __table_args__ = (
        Index('idx_repo_commit_file', 'repository_id', 'commit_id', 'file_path'),
        Index('idx_created_at', 'created_at'),
        Index('idx_cache_status', 'cache_status'),
        Index('idx_diff_version', 'diff_version'),
        Index('idx_expire_at', 'expire_at'),
        Index('idx_is_long_processing', 'is_long_processing'),
    )
    
    repository = db.relationship('Repository', backref='diff_caches')

class BackgroundTask(db.Model):
    """后台任务队列表 - 持久化任务队列"""
    __tablename__ = 'background_tasks'
    
    id = db.Column(db.Integer, primary_key=True)
    task_type = db.Column(db.String(50), nullable=False)  # 'excel_diff', 'cleanup_cache', etc.
    repository_id = db.Column(db.Integer, nullable=True)
    commit_id = db.Column(db.String(100), nullable=True)
    file_path = db.Column(db.Text, nullable=True)
    priority = db.Column(db.Integer, default=10)  # 优先级，数字越小优先级越高
    status = db.Column(db.String(20), default='pending')  # 'pending', 'processing', 'completed', 'failed'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, default=0)

class ExcelHtmlCache(db.Model):
    """Excel HTML缓存表 - 缓存完整的HTML内容和样式"""
    __tablename__ = 'excel_html_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)
    commit_id = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    cache_key = db.Column(db.String(255), nullable=False, unique=True)  # MD5哈希键

    # HTML内容和样式
    html_content = db.Column(db.Text)  # 渲染好的HTML内容
    css_content = db.Column(db.Text)   # CSS样式
    js_content = db.Column(db.Text)    # JavaScript代码
    cache_metadata = db.Column(db.Text)      # JSON格式的元数据（文件信息、统计等）

    # 缓存状态和版本
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    diff_version = db.Column(db.String(20), default=DIFF_LOGIC_VERSION)  # Diff逻辑版本号

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 添加索引以提高查询性能
    __table_args__ = (
        Index('idx_html_repo_commit_file', 'repository_id', 'commit_id', 'file_path'),
        Index('idx_html_cache_key', 'cache_key'),
        Index('idx_html_cache_status', 'cache_status'),
        Index('idx_html_diff_version', 'diff_version'),
    )

    repository = db.relationship('Repository', backref='excel_html_caches')

class WeeklyVersionConfig(db.Model):
    """周版本diff配置表"""
    __tablename__ = 'weekly_version_config'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)

    # 配置基本信息
    name = db.Column(db.String(100), nullable=False)  # 配置名称，如"第42周版本"
    description = db.Column(db.Text)  # 配置描述
    branch = db.Column(db.String(100), nullable=False)  # 分支名称

    # 时间配置
    start_time = db.Column(db.DateTime, nullable=False)  # 版本开始时间
    end_time = db.Column(db.DateTime, nullable=False)    # 版本结束时间
    cycle_type = db.Column(db.String(20), default='custom')  # 'weekly', 'biweekly', 'custom'

    # 状态和设置
    is_active = db.Column(db.Boolean, default=True)  # 是否启用
    auto_sync = db.Column(db.Boolean, default=True)  # 是否自动同步
    status = db.Column(db.String(20), default='active')  # 'active', 'completed', 'archived'

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 关系
    project = db.relationship('Project', backref='weekly_version_configs')
    repository = db.relationship('Repository', backref='weekly_version_configs')

    # 添加索引
    __table_args__ = (
        Index('idx_weekly_project_repo', 'project_id', 'repository_id'),
        Index('idx_weekly_time_range', 'start_time', 'end_time'),
        Index('idx_weekly_status', 'status'),
    )

class WeeklyVersionDiffCache(db.Model):
    """周版本diff缓存表 - 存储合并后的diff数据"""
    __tablename__ = 'weekly_version_diff_cache'

    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('weekly_version_config.id'), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)

    # 文件信息
    file_path = db.Column(db.String(500), nullable=False)
    file_type = db.Column(db.String(50))  # 文件类型：'code', 'table', 'res', etc.

    # diff数据
    merged_diff_data = db.Column(db.Text)  # JSON格式的合并diff数据
    base_commit_id = db.Column(db.String(100))  # 基准版本的commit_id
    latest_commit_id = db.Column(db.String(100))  # 最新版本的commit_id

    # 提交信息
    commit_authors = db.Column(db.Text)  # JSON格式的提交者列表
    commit_messages = db.Column(db.Text)  # JSON格式的提交消息列表
    commit_times = db.Column(db.Text)    # JSON格式的提交时间列表
    commit_count = db.Column(db.Integer, default=0)  # 涉及的提交数量

    # 确认状态 - 支持多角色确认
    confirmation_status = db.Column(db.Text)  # JSON格式：{"dev": "pending", "qa": "confirmed", "pm": "pending"}
    overall_status = db.Column(db.String(20), default='pending')  # 'pending', 'confirmed', 'rejected'

    # 缓存状态
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    processing_time = db.Column(db.Float)  # 处理时间（秒）
    file_size = db.Column(db.Integer)      # 文件大小（字节）

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_sync_time = db.Column(db.DateTime)  # 最后同步时间

    # 关系
    config = db.relationship('WeeklyVersionConfig', backref='diff_caches')
    repository = db.relationship('Repository', backref='weekly_diff_caches')

    # 添加索引
    __table_args__ = (
        Index('idx_weekly_diff_config_file', 'config_id', 'file_path'),
        Index('idx_weekly_diff_repo', 'repository_id'),
        Index('idx_weekly_diff_status', 'overall_status'),
        Index('idx_weekly_diff_cache_status', 'cache_status'),
        Index('idx_weekly_diff_sync_time', 'last_sync_time'),
    )

class WeeklyVersionExcelCache(db.Model):
    """周版本Excel合并diff缓存表 - 专门存储Excel文件的合并diff HTML缓存"""
    __tablename__ = 'weekly_version_excel_cache'

    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('weekly_version_config.id'), nullable=False)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)

    # 文件信息
    file_path = db.Column(db.String(500), nullable=False)
    cache_key = db.Column(db.String(255), nullable=False, unique=True)  # MD5哈希键

    # 提交信息
    base_commit_id = db.Column(db.String(100))  # 基准版本的commit_id
    latest_commit_id = db.Column(db.String(100))  # 最新版本的commit_id
    commit_count = db.Column(db.Integer, default=0)  # 提交数量

    # HTML内容和样式
    html_content = db.Column(db.Text)  # 渲染好的HTML内容
    css_content = db.Column(db.Text)   # CSS样式
    js_content = db.Column(db.Text)    # JavaScript代码
    cache_metadata = db.Column(db.Text)  # JSON格式的元数据（文件信息、统计等）

    # 缓存状态
    cache_status = db.Column(db.String(20), default='pending')  # 'pending', 'processing', 'completed', 'failed'
    diff_version = db.Column(db.String(20))  # diff逻辑版本号
    processing_time = db.Column(db.Float)  # 处理时间（秒）

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class OperationLog(db.Model):
    """操作日志表 - 持久化存储Excel缓存操作日志"""
    __tablename__ = 'operation_log'

    id = db.Column(db.Integer, primary_key=True)
    log_type = db.Column(db.String(20), nullable=False)  # 'info', 'success', 'error', 'warning'
    message = db.Column(db.Text, nullable=False)  # 日志消息
    source = db.Column(db.String(50), nullable=False)  # 'excel_cache', 'weekly_excel_cache'

    # 可选的关联信息
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=True)
    config_id = db.Column(db.Integer, nullable=True)  # 周版本配置ID
    file_path = db.Column(db.String(500), nullable=True)  # 相关文件路径

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # 关联关系
    repository = db.relationship('Repository', backref='operation_logs', lazy=True)

    # 添加索引
    __table_args__ = (
        Index('idx_operation_log_source', 'source'),
        Index('idx_operation_log_type', 'log_type'),
        Index('idx_operation_log_repo', 'repository_id'),
        Index('idx_operation_log_config', 'config_id'),
        Index('idx_operation_log_created', 'created_at'),
    )

class MergedDiffCache(db.Model):
    """合并diff缓存表 - 用于存储多个提交合并后的diff结果"""
    __tablename__ = 'merged_diff_cache'

    id = db.Column(db.Integer, primary_key=True)
    repository_id = db.Column(db.Integer, db.ForeignKey('repository.id'), nullable=False)

    # 缓存键和文件信息
    cache_key = db.Column(db.String(255), nullable=False, unique=True)  # MD5哈希键
    file_path = db.Column(db.String(500), nullable=False)

    # 版本信息
    base_commit_id = db.Column(db.String(100))  # 基准版本
    target_commit_id = db.Column(db.String(100))  # 目标版本
    commit_id_list = db.Column(db.Text)  # JSON格式的涉及提交ID列表

    # diff数据
    merged_diff_data = db.Column(db.Text)  # JSON格式的合并diff数据
    diff_summary = db.Column(db.Text)     # JSON格式的diff摘要信息

    # 统计信息
    total_commits = db.Column(db.Integer, default=0)  # 涉及的提交总数
    added_lines = db.Column(db.Integer, default=0)    # 新增行数
    deleted_lines = db.Column(db.Integer, default=0)  # 删除行数
    modified_lines = db.Column(db.Integer, default=0) # 修改行数

    # 缓存状态
    cache_status = db.Column(db.String(50), default='pending')  # pending, completed, failed
    processing_time = db.Column(db.Float)  # 处理时间（秒）
    file_size = db.Column(db.Integer)      # 文件大小（字节）
    diff_version = db.Column(db.String(20), default=DIFF_LOGIC_VERSION)  # Diff逻辑版本号

    # 时间戳
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    expire_at = db.Column(db.DateTime)  # 缓存过期时间

    # 关系
    repository = db.relationship('Repository', backref='merged_diff_caches')

    # 添加索引
    __table_args__ = (
        Index('idx_merged_diff_cache_key', 'cache_key'),
        Index('idx_merged_diff_repo_file', 'repository_id', 'file_path'),
        Index('idx_merged_diff_commits', 'base_commit_id', 'target_commit_id'),
        Index('idx_merged_diff_status', 'cache_status'),
        Index('idx_merged_diff_version', 'diff_version'),
        Index('idx_merged_diff_expire', 'expire_at'),
    )

# Excel差异缓存服务
class ExcelDiffCacheService:
    """Excel文件差异缓存服务"""
    
    def __init__(self):
        self.processing_commits = set()  # 正在处理的提交ID集合
        self.operation_logs = []  # 操作日志列表ID集合
        self.max_cache_count = 1000  # 最大缓存数量
        self.long_processing_threshold = 10.0  # 长处理时间阈值（秒）
        self.long_processing_expire_days = 90  # 长处理文件缓存保留天数（3个月）
        
    def is_excel_file(self, file_path):
        """检查文件是否为Excel文件"""
        excel_extensions = ['.xlsx', '.xls', '.xlsm']
        return any(file_path.lower().endswith(ext) for ext in excel_extensions)
    
    def log_cache_operation(self, message, log_type='info', repository_id=None, file_path=None):
        """记录缓存操作日志到数据库"""
        try:
            # 保存到数据库
            log_entry = OperationLog(
                log_type=log_type,
                message=message,
                source='excel_cache',
                repository_id=repository_id,
                file_path=file_path
            )
            db.session.add(log_entry)

            # 清理超过200条的旧日志
            self._cleanup_old_logs()

            db.session.commit()

            # 同时保持内存中的日志（用于向后兼容），使用北京时间
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            # 保持最多100条内存日志
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]

        except Exception as e:
            # 如果数据库操作失败，至少保持内存日志
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]
            log_print(f"记录操作日志到数据库失败: {e}", 'ERROR')

    def _cleanup_old_logs(self):
        """清理超过200条的旧日志"""
        try:
            # 获取当前日志总数
            total_count = OperationLog.query.filter_by(source='excel_cache').count()

            if total_count > 200:
                # 删除最旧的日志，保留最新的200条
                excess_count = total_count - 200
                old_logs = OperationLog.query.filter_by(source='excel_cache').order_by(OperationLog.created_at.asc()).limit(excess_count).all()

                for log in old_logs:
                    db.session.delete(log)

        except Exception as e:
            log_print(f"清理旧操作日志失败: {e}", 'ERROR')
    
    def get_cached_diff(self, repository_id, commit_id, file_path):
        """获取缓存的差异数据，检查版本号匹配"""
        try:
            log_print(f"🔍 查询缓存: repo={repository_id}, commit={commit_id[:8]}, file={file_path}", 'CACHE')
            
            # 强制刷新数据库会话，避免读取过期缓存
            db.session.expire_all()
            
            cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path,
                cache_status='completed',
                diff_version=DIFF_LOGIC_VERSION  # 只返回当前版本的缓存
            ).order_by(DiffCache.updated_at.desc()).first()
            
            if cache:
                log_print(f"✅ 缓存命中: {file_path} | 版本: {cache.diff_version} | 创建时间: {cache.created_at}", 'CACHE')
                log_print(f"📊 缓存数据大小: {len(cache.diff_data)} 字符 | 处理时间: {cache.processing_time:.2f}秒", 'CACHE')
                return cache
            else:
                # 检查是否存在旧版本的缓存
                old_cache = DiffCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    cache_status='completed'
                ).first()
                
                if old_cache and old_cache.diff_version != DIFF_LOGIC_VERSION:
                    log_print(f"⚠️ 发现旧版本缓存: {file_path} | 旧版本: {old_cache.diff_version} → 当前版本: {DIFF_LOGIC_VERSION}", 'CACHE')
                    # 标记旧缓存为过期，稍后清理
                    old_cache.cache_status = 'outdated'
                    db.session.commit()
                    log_print(f"🗑️ 旧版本缓存已标记为过期", 'CACHE')
                else:
                    log_print(f"❌ 缓存未命中: {file_path} | 需要重新计算diff", 'CACHE')
                
            return None
        except Exception as e:
            log_print(f"❌ 获取缓存差异失败: {e}", 'CACHE', force=True)
            return None
    
    def optimize_diff_data(self, diff_data):
        """优化diff数据，只保留有变更的行"""
        if not diff_data or diff_data.get('type') != 'excel':
            return diff_data
        
        try:
            optimized_data = diff_data.copy()
            original_size = 0
            optimized_size = 0
            
            if 'sheets' in optimized_data:
                for sheet_name, sheet_data in optimized_data['sheets'].items():
                    if 'rows' in sheet_data:
                        original_rows = sheet_data['rows']
                        original_size += len(original_rows)
                        
                        # 只保留有变更的行（added, removed, modified）
                        changed_rows = [
                            row for row in original_rows 
                            if row.get('status') in ['added', 'removed', 'modified']
                        ]
                        
                        sheet_data['rows'] = changed_rows
                        optimized_size += len(changed_rows)
                        
                        # 更新统计信息，只统计有变更的行
                        if 'stats' in sheet_data:
                            stats = sheet_data['stats']
                            # 保持原有的统计数据，因为这些是正确的变更统计
            
            log_print(f"🗜️ diff数据优化: {original_size} 行 → {optimized_size} 行 (减少 {original_size - optimized_size} 行)", 'CACHE')
            return optimized_data
            
        except Exception as e:
            log_print(f"❌ diff数据优化失败: {e}", 'CACHE', force=True)
            return diff_data
    
    def save_cached_diff(self, repository_id, commit_id, file_path, diff_data, processing_time=0.0, file_size=0, previous_commit_id=None, commit_time=None):
        """保存差异数据到缓存，支持智能缓存策略"""
        try:
            log_print(f"💾 保存缓存: repo={repository_id}, commit={commit_id[:8]}, file={file_path}", 'CACHE')
            
            # 判断是否为长处理时间文件
            is_long_processing = processing_time > self.long_processing_threshold
            
            # 计算过期时间
            expire_at = None
            if is_long_processing:
                # 长处理文件保存3个月
                expire_at = datetime.now(timezone.utc) + timedelta(days=self.long_processing_expire_days)
                log_print(f"⏱️ 长处理文件({processing_time:.2f}s)，缓存3个月至: {expire_at.strftime('%Y-%m-%d')}", 'CACHE')
            else:
                # 普通文件根据1000条限制管理，不设置过期时间
                log_print(f"⚡ 普通处理文件({processing_time:.2f}s)，按1000条限制管理", 'CACHE')
            
            # 检查是否已存在
            existing_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path
            ).first()
            
            if existing_cache:
                # 更新现有缓存
                existing_cache.diff_data = json.dumps(diff_data)
                existing_cache.processing_time = processing_time
                existing_cache.file_size = file_size
                existing_cache.cache_status = 'completed'
                existing_cache.diff_version = DIFF_LOGIC_VERSION
                existing_cache.is_long_processing = is_long_processing
                existing_cache.expire_at = expire_at
                existing_cache.updated_at = datetime.now(timezone.utc)
                if commit_time:
                    existing_cache.commit_time = commit_time
                log_print(f"🔄 更新现有缓存: {file_path}", 'CACHE')
            else:
                # 创建新缓存
                new_cache = DiffCache(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    previous_commit_id=previous_commit_id,
                    diff_data=json.dumps(diff_data),
                    processing_time=processing_time,
                    file_size=file_size,
                    cache_status='completed',
                    diff_version=DIFF_LOGIC_VERSION,
                    commit_time=commit_time,
                    is_long_processing=is_long_processing,
                    expire_at=expire_at
                )
                db.session.add(new_cache)
                log_print(f"💾 创建新缓存: {file_path}", 'CACHE')
            
            db.session.commit()
            
            # 验证缓存是否真的保存成功
            saved_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path
            ).first()
            
            if saved_cache:
                log_print(f"✅ 缓存保存验证成功: ID={saved_cache.id}, 状态={saved_cache.cache_status}, 版本={saved_cache.diff_version}", 'CACHE')
            else:
                log_print(f"❌ 缓存保存验证失败: 数据库中未找到缓存记录", 'CACHE', force=True)
            
            # 保存后检查是否需要清理旧缓存
            if not is_long_processing:
                self._cleanup_old_cache(repository_id)
            
            log_print(f"✅ 缓存保存成功: {file_path} | 处理时间: {processing_time:.2f}秒", 'CACHE')
            return True
            
        except Exception as e:
            log_print(f"❌ 保存缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return False
    
    def cache_diff_error(self, repository_id, commit_id, file_path, error_message):
        """缓存错误信息"""
        try:
            existing_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path
            ).first()
            
            if existing_cache:
                existing_cache.cache_status = 'failed'
                existing_cache.error_message = error_message
                existing_cache.updated_at = datetime.now(timezone.utc)
            else:
                cache = DiffCache(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_data='{}',
                    cache_status='failed',
                    error_message=error_message
                )
                db.session.add(cache)
            
            db.session.commit()
        except Exception as e:
            log_print(f"缓存错误信息失败: {e}", 'CACHE', force=True)
            db.session.rollback()
    
    def get_recent_excel_commits(self, repository, limit=1000):
        """获取最近的Excel文件提交（从最近1000条提交中筛选）"""
        try:
            # 先获取最近1000条提交记录，应用仓库起始日期过滤
            query = Commit.query.filter(Commit.repository_id == repository.id)
            
            # 应用仓库配置的起始日期过滤
            if repository.start_date:
                query = query.filter(Commit.commit_time >= repository.start_date)
            
            recent_commits = query.order_by(Commit.commit_time.desc()).limit(limit).all()
            
            # 从中筛选出Excel文件
            excel_commits = []
            for commit in recent_commits:
                if (commit.path.endswith('.xlsx') or 
                    commit.path.endswith('.xls') or 
                    commit.path.endswith('.xlsm') or 
                    commit.path.endswith('.xlsb') or 
                    commit.path.endswith('.csv')):
                    excel_commits.append(commit)
            
            commits = excel_commits
            
            # 进一步过滤符合仓库正则条件的文件
            filtered_commits = []
            if repository.path_regex:
                import re
                pattern = re.compile(repository.path_regex)
                for commit in commits:
                    if pattern.search(commit.path):
                        filtered_commits.append(commit)
            else:
                filtered_commits = commits
            
            log_print(f"📊 从最近{limit}条提交中筛选出{len(filtered_commits)}个Excel文件", 'CACHE')
            return filtered_commits
        except Exception as e:
            log_print(f"获取最近Excel提交失败: {e}", 'CACHE', force=True)
            return []
    
    def process_excel_diff_background(self, repository_id, commit_id, file_path):
        """后台处理Excel文件差异"""
        try:
            log_print(f"开始后台处理Excel差异: repo={repository_id}, commit={commit_id}, file={file_path}", 'EXCEL')
            
            # 检查是否已在处理中
            task_key = f"{repository_id}_{commit_id}_{file_path}"
            if task_key in self.processing_commits:
                log_print(f"任务已在处理中，跳过: {task_key}", 'EXCEL')
                return
            
            self.processing_commits.add(task_key)
            
            # 确保在Flask应用上下文中执行
            with app.app_context():
                try:
                    repository = db.session.get(Repository, repository_id)
                    if not repository:
                        log_print(f"仓库不存在: {repository_id}", 'EXCEL', force=True)
                        return
                    
                    # 获取提交信息
                    commit = Commit.query.filter_by(
                        repository_id=repository_id,
                        commit_id=commit_id,
                        path=file_path
                    ).first()
                    
                    if not commit:
                        log_print(f"提交不存在: {commit_id}, {file_path}", 'EXCEL', force=True)
                        return
                    
                    # 获取前一个提交
                    previous_commit = None
                    file_commits = Commit.query.filter(
                        Commit.repository_id == repository_id,
                        Commit.path == file_path,
                        Commit.commit_time < commit.commit_time
                    ).order_by(Commit.commit_time.desc()).first()
                    
                    start_time = time.time()
                    
                    # 使用统一差异服务处理
                    diff_data = get_unified_diff_data(commit, file_commits)
                    
                    processing_time = time.time() - start_time
                    
                    if diff_data and diff_data.get('type') == 'excel':
                        # 缓存成功的差异数据
                        self.save_cached_diff(
                            repository_id=repository_id,
                            commit_id=commit_id,
                            file_path=file_path,
                            diff_data=diff_data,
                            previous_commit_id=file_commits.commit_id if file_commits else None,
                            processing_time=processing_time
                        )
                        log_print(f"💾 Excel差异缓存成功: {file_path} | 版本: {DIFF_LOGIC_VERSION} | 耗时: {processing_time:.2f}秒", 'EXCEL')
                        log_print(f"📈 差异数据大小: {len(str(diff_data))} 字符", 'EXCEL')
                        
                        # 记录到操作日志
                        self.log_cache_operation(f"✅ 缓存生成成功: {file_path}", 'success', repository_id=repository_id, file_path=file_path)
                    else:
                        # 缓存错误信息
                        error_msg = diff_data.get('error', '处理失败') if diff_data else '处理返回空结果'
                        self.cache_diff_error(repository_id, commit_id, file_path, error_msg)
                        log_print(f"❌ Excel差异处理失败: {file_path} | 错误: {error_msg}", 'EXCEL', force=True)
                        
                        # 记录到操作日志
                        self.log_cache_operation(f"❌ 缓存生成失败: {file_path} - {error_msg}", 'error', repository_id=repository_id, file_path=file_path)
                        
                except Exception as inner_e:
                    log_print(f"处理Excel差异时出错: {inner_e}", 'EXCEL', force=True)
                    import traceback
                    traceback.print_exc()
                finally:
                    self.processing_commits.discard(task_key)
                
        except Exception as e:
            log_print(f"后台处理Excel差异异常: {e}", 'EXCEL', force=True)
            import traceback
            traceback.print_exc()
    
    def cleanup_old_cache(self, days=30):
        """清理超过指定天数的缓存数据和旧版本缓存"""
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            # 清理超过指定天数的缓存
            old_caches = DiffCache.query.filter(DiffCache.created_at < cutoff_date).all()
            
            # 清理标记为过期的旧版本缓存
            outdated_caches = DiffCache.query.filter_by(cache_status='outdated').all()
            
            # 清理版本号不匹配的缓存（兼容性清理）
            version_mismatch_caches = DiffCache.query.filter(
                DiffCache.diff_version != DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).all()
            
            all_caches_to_delete = old_caches + outdated_caches + version_mismatch_caches
            
            # 去重
            unique_caches = {cache.id: cache for cache in all_caches_to_delete}
            
            count = len(unique_caches)
            for cache in unique_caches.values():
                db.session.delete(cache)
            
            db.session.commit()
            log_print(f"清理了 {count} 条过期缓存数据（包括 {len(old_caches)} 条超期缓存，{len(outdated_caches)} 条标记过期缓存，{len(version_mismatch_caches)} 条版本不匹配缓存）")
            return count
        except Exception as e:
            log_print(f"清理缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return 0
    
    def cleanup_version_mismatch_cache(self):
        """专门清理版本号不匹配的缓存"""
        try:
            # 查找所有版本号不匹配的缓存
            mismatch_caches = DiffCache.query.filter(
                DiffCache.diff_version != DIFF_LOGIC_VERSION
            ).all()
            
            count = len(mismatch_caches)
            for cache in mismatch_caches:
                log_print(f"清理版本不匹配的缓存: {cache.file_path} (版本: {cache.diff_version} → {DIFF_LOGIC_VERSION})", 'CACHE')
                db.session.delete(cache)
            
            db.session.commit()
            log_print(f"清理了 {count} 条版本不匹配的缓存", 'CACHE')
            return count
        except Exception as e:
            log_print(f"清理版本不匹配缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return 0
    
    def _cleanup_old_cache(self, repository_id=None):
        """清理超过1000条的旧缓存（不包括长处理文件）"""
        try:
            query = DiffCache.query.filter(
                DiffCache.cache_status == 'completed',
                DiffCache.is_long_processing == False  # 不清理长处理文件
            )
            
            if repository_id:
                query = query.filter(DiffCache.repository_id == repository_id)
            
            # 按创建时间倒序，保留最新的1000条
            total_count = query.count()
            if total_count > self.max_cache_count:
                # 获取需要删除的记录
                old_caches = query.order_by(DiffCache.created_at.desc()).offset(self.max_cache_count).all()
                
                deleted_count = len(old_caches)
                for cache in old_caches:
                    db.session.delete(cache)
                
                db.session.commit()
                return deleted_count
            else:
                current_count = query.count()
                log_print(f"📊 当前缓存数量 {current_count}，无需清理", 'CACHE')
                return 0
                
        except Exception as e:
            log_print(f"❌ 清理旧缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return 0
    
    def get_cache_statistics(self, repository_id=None):
        """获取缓存统计信息"""
        try:
            query = DiffCache.query
            if repository_id:
                query = query.filter(DiffCache.repository_id == repository_id)
            
            total_count = query.count()
            completed_count = query.filter(DiffCache.cache_status == 'completed').count()
            processing_count = query.filter(DiffCache.cache_status == 'processing').count()
            failed_count = query.filter(DiffCache.cache_status == 'failed').count()
            outdated_count = query.filter(DiffCache.cache_status == 'outdated').count()
            
            # 获取当前版本的缓存数量
            current_version_count = query.filter(
                DiffCache.diff_version == DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 获取长处理文件数量
            long_processing_count = query.filter(
                DiffCache.is_long_processing == True,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 计算普通处理文件数量
            normal_processing_count = completed_count - long_processing_count
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'outdated_count': outdated_count,
                'current_version_count': current_version_count,
                'long_processing_count': long_processing_count,
                'normal_processing_count': normal_processing_count,
                'version': DIFF_LOGIC_VERSION
            }
        except Exception as e:
            log_print(f"❌ 获取缓存统计失败: {e}", 'CACHE', force=True)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'outdated_count': 0,
                'current_version_count': 0,
                'long_processing_count': 0,
                'normal_processing_count': 0,
                'version': DIFF_LOGIC_VERSION
            }
    
    def get_cache_statistics_by_repositories(self, repository_ids):
        """获取指定仓库列表的缓存统计信息"""
        try:
            log_print(f"🔍 获取仓库缓存统计: repository_ids={repository_ids}", 'CACHE')
            
            if not repository_ids:
                log_print("⚠️ 仓库ID列表为空，返回零值统计", 'CACHE')
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'processing_count': 0,
                    'failed_count': 0,
                    'outdated_count': 0,
                    'current_version_count': 0,
                    'long_processing_count': 0,
                    'normal_processing_count': 0,
                    'version': DIFF_LOGIC_VERSION
                }
            
            query = DiffCache.query.filter(DiffCache.repository_id.in_(repository_ids))
            
            total_count = query.count()
            completed_count = query.filter(DiffCache.cache_status == 'completed').count()
            processing_count = query.filter(DiffCache.cache_status == 'processing').count()
            failed_count = query.filter(DiffCache.cache_status == 'failed').count()
            outdated_count = query.filter(DiffCache.cache_status == 'outdated').count()
            
            # 获取当前版本的缓存数量
            current_version_count = query.filter(
                DiffCache.diff_version == DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 获取长处理文件数量
            long_processing_count = query.filter(
                DiffCache.is_long_processing == True,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 详细调试信息
            log_print(f"📊 缓存统计详细结果:", 'CACHE')
            log_print(f"   - 总缓存数: {total_count}", 'CACHE')
            log_print(f"   - 已完成: {completed_count}", 'CACHE')
            log_print(f"   - 处理中: {processing_count}", 'CACHE')
            log_print(f"   - 失败: {failed_count}", 'CACHE')
            log_print(f"   - 过期: {outdated_count}", 'CACHE')
            log_print(f"   - 当前版本: {current_version_count}", 'CACHE')
            log_print(f"   - 长处理: {long_processing_count}", 'CACHE')
            
            # 查看最近的几条缓存记录
            recent_caches = query.order_by(DiffCache.created_at.desc()).limit(3).all()
            if recent_caches:
                log_print(f"📋 最近的缓存记录:", 'CACHE')
                for cache in recent_caches:
                    log_print(f"   - ID:{cache.id}, 仓库:{cache.repository_id}, 文件:{cache.file_path}, 状态:{cache.cache_status}, 版本:{cache.diff_version}", 'CACHE')
            else:
                log_print(f"❌ 没有找到任何缓存记录", 'CACHE')
            
            # 计算普通处理文件数量
            normal_processing_count = completed_count - long_processing_count
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'outdated_count': outdated_count,
                'current_version_count': current_version_count,
                'long_processing_count': long_processing_count,
                'normal_processing_count': normal_processing_count,
                'version': DIFF_LOGIC_VERSION
            }
        except Exception as e:
            log_print(f"❌ 获取仓库缓存统计失败: {e}", 'CACHE', force=True)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'outdated_count': 0,
                'current_version_count': 0,
                'long_processing_count': 0,
                'normal_processing_count': 0,
                'version': DIFF_LOGIC_VERSION
            }

# 初始化服务实例
excel_cache_service = ExcelDiffCacheService()
excel_html_cache_service = ExcelHtmlCacheService(db, DIFF_LOGIC_VERSION)

# 初始化周版本Excel缓存服务
from services.weekly_excel_cache_service import WeeklyExcelCacheService
weekly_excel_cache_service = WeeklyExcelCacheService(db, DIFF_LOGIC_VERSION)
    
@db_retry(max_retries=5, delay=0.1)
def update_task_status_with_retry(task_id, status, error_message=None):
    """使用重试机制更新任务状态"""
    # 检查task_id是否有效
    if task_id is None:
        log_print(f"⚠️ 跳过任务状态更新，task_id为None", 'TASK')
        return

    try:
        # 创建新的数据库会话
        db_task = db.session.get(BackgroundTask, task_id)
        if db_task:
            db_task.status = status

            if status == 'processing':
                db_task.started_at = datetime.now(timezone.utc)
            elif status in ['completed', 'failed']:
                db_task.completed_at = datetime.now(timezone.utc)
                if status == 'failed':
                    db_task.error_message = error_message
                    db_task.retry_count += 1

            db.session.commit()
            log_print(f"✅ 任务状态更新成功: {task_id} -> {status}", 'TASK')
        else:
            log_print(f"⚠️ 未找到任务: {task_id}", 'TASK')
    except Exception as e:
        log_print(f"❌ 更新任务状态失败: {task_id} -> {status}, 错误: {e}", 'TASK', force=True)
        db.session.rollback()
        raise e

def background_task_worker():
    """后台任务工作线程"""
    global background_task_running
    
    log_print("后台任务工作线程启动", 'APP')
    log_print(f"初始队列大小: {background_task_queue.qsize()}", 'APP')
    
    while background_task_running:
        task_processed = False
        try:
            # 从优先级队列获取任务，超时1秒
            # log_print(f"等待任务中... 当前队列大小: {background_task_queue.qsize(, 'INFO')}")
            task_wrapper = background_task_queue.get(timeout=1)
            task_processed = True  # 标记已获取任务
            priority = task_wrapper.priority
            task = task_wrapper.task_data
            
            log_print(f"🔧 后台任务开始处理: excel_diff (优先级: {priority}) | 队列剩余: {background_task_queue.qsize()}", 'EXCEL')
            
            if task['type'] == 'excel_diff':
                log_print(f"📊 处理Excel差异: repo={task['repository_id']}, commit={task['commit_id'][:8]}, file={task['file_path']}", 'EXCEL')
                
                # 在Flask应用上下文中执行
                with app.app_context():
                    # 更新数据库任务状态为处理中 - 使用重试机制
                    if 'task_id' in task:
                        try:
                            update_task_status_with_retry(task['task_id'], 'processing')
                        except Exception as update_error:
                            log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
                    
                    try:
                        excel_cache_service.process_excel_diff_background(
                            task['repository_id'],
                            task['commit_id'], 
                            task['file_path']
                        )
                        
                        # 标记任务完成 - 使用重试机制
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'completed')
                            except Exception as update_error:
                                log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
                                
                    except Exception as e:
                        log_print(f"❌ Excel差异处理失败: {e}", 'EXCEL', force=True)
                        
                        # 回滚数据库会话
                        try:
                            db.session.rollback()
                        except Exception as rollback_error:
                            log_print(f"会话回滚失败: {rollback_error}", 'DB', force=True)
                        
                        # 标记任务失败 - 使用重试机制
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', str(e))
                            except Exception as update_error:
                                log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)
            elif task['type'] == 'cleanup_cache':
                log_print(f"🧹 清理缓存: {task.get('days', 30)} 天前的数据", 'CACHE')
                excel_cache_service.cleanup_old_cache(task.get('days', 30))
            elif task['type'] == 'regenerate_cache':
                log_print(f"🔄 重新生成缓存: 仓库 {task['repository_id']}", 'CACHE')
                task_count = regenerate_repository_cache(task['repository_id'])
                log_print(f"✅ 缓存重新生成完成，已添加 {task_count} 个任务到队列", 'CACHE')
            elif task['type'] == 'auto_sync':
                log_print(f"🔄 自动数据分析: 仓库 {task['repository_id']}", 'SYNC')
                
                # 在Flask应用上下文中执行
                with app.app_context():
                    # 更新数据库任务状态为处理中 - 使用重试机制
                    if 'task_id' in task:
                        try:
                            update_task_status_with_retry(task['task_id'], 'processing')
                        except Exception as update_error:
                            log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
                    
                    try:
                        # 执行自动数据分析
                        repository = db.session.get(Repository, task['repository_id'])
                        if repository:
                            log_print(f"开始自动分析仓库: {repository.name}", 'SYNC')
                            
                            # 使用Git服务进行数据同步
                            if repository.type == 'git':
                                from services.threaded_git_service import ThreadedGitService
                                git_service = ThreadedGitService(
                                    repository.url,
                                    repository.root_directory,
                                    repository.username,
                                    repository.token,
                                    repository
                                )
                                
                                # 同步仓库提交记录
                                log_print(f"🚀 [BACKGROUND_SYNC] 开始后台同步仓库 ID: {repository.id}", 'SYNC')
                                
                                # 克隆或更新仓库
                                log_print(f"🔧 [BACKGROUND_SYNC] 准备调用 clone_or_update_repository", 'SYNC')
                                log_print(f"🔧 [BACKGROUND_SYNC] Git服务对象: {git_service}", 'SYNC')
                                log_print(f"🔧 [BACKGROUND_SYNC] 仓库URL: {repository.url}", 'SYNC')
                                log_print(f"🔧 [BACKGROUND_SYNC] 本地路径: {git_service.local_path}", 'SYNC')
                                
                                log_print(f"🔧 [BACKGROUND_SYNC] 即将调用 clone_or_update_repository 方法", 'SYNC')
                                try:
                                    success, message = git_service.clone_or_update_repository()
                                    log_print(f"🔧 [BACKGROUND_SYNC] clone_or_update_repository 返回: success={success}, message={message}", 'SYNC')
                                except Exception as e:
                                    log_print(f"❌ [BACKGROUND_SYNC] clone_or_update_repository 异常: {e}", 'SYNC', force=True)
                                    success, message = False, f"调用异常: {e}"
                                
                                if not success:
                                    log_print(f"仓库克隆/更新失败: {message}", 'SYNC', force=True)
                                    continue
                                
                                # 确定同步起始日期
                                since_date = None
                                
                                # 检查仓库配置的起始日期限制
                                if repository.start_date:
                                    since_date = repository.start_date
                                    log_print(f"🔍 [BACKGROUND_SYNC] 应用仓库配置的起始日期限制: {since_date}", 'SYNC')
                                
                                # 检查数据库中最新提交时间，用于增量同步
                                latest_commit = Commit.query.filter_by(repository_id=repository.id)\
                                    .order_by(Commit.commit_time.desc()).first()
                                
                                if latest_commit and latest_commit.commit_time:
                                    # 如果有配置起始日期，取较晚的时间
                                    if since_date is None or latest_commit.commit_time > since_date:
                                        since_date = latest_commit.commit_time
                                        log_print(f"🔍 [BACKGROUND_SYNC] 从最新提交时间开始增量同步: {since_date}", 'SYNC')
                                
                                # 获取提交记录 - 使用多线程优化版本，应用日期过滤
                                import time
                                start_time = time.time()
                                commits = git_service.get_commits_threaded(since_date=since_date, limit=1000)
                                end_time = time.time()
                                log_print(f"⚡ [THREADED_GIT] 多线程获取提交记录耗时: {(end_time - start_time):.2f}秒, 提交数: {len(commits)}", 'GIT')
                                log_print(f"🔍 [BACKGROUND_SYNC] Git服务获取到 {len(commits)} 个提交记录", 'SYNC')
                                
                                commits_added = 0
                                excel_tasks_added = 0
                                for i, commit_data in enumerate(commits):
                                    # 检查提交是否已存在
                                    existing_commit = Commit.query.filter_by(
                                        repository_id=repository.id,
                                        commit_id=commit_data['commit_id']
                                    ).first()
                                    
                                    if not existing_commit:
                                        # 创建新的提交记录
                                        new_commit = Commit(
                                            repository_id=repository.id,
                                            commit_id=commit_data['commit_id'],
                                            author=commit_data.get('author', ''),
                                            message=commit_data.get('message', ''),
                                            commit_time=commit_data.get('commit_time'),
                                            path=commit_data.get('path', ''),
                                            version=commit_data.get('version', commit_data['commit_id'][:8]),
                                            operation=commit_data.get('operation', 'M'),
                                            status='pending'
                                        )
                                        db.session.add(new_commit)
                                        commits_added += 1
                                        log_print(f"➕ [BACKGROUND_SYNC] 添加新提交 {i+1}/{len(commits)}: {commit_data['commit_id'][:8]}", 'SYNC')
                                        
                                        # 检查是否为Excel文件，如果是则添加到diff缓存任务队列
                                        file_path = commit_data.get('path', '')
                                        if file_path.lower().endswith(('.xlsx', '.xls')):
                                            log_print(f"📊 [BACKGROUND_SYNC] 检测到Excel文件: {file_path}", 'SYNC')
                                            try:
                                                # 创建任务数据
                                                task_data = {
                                                    'type': 'excel_diff',
                                                    'repository_id': repository.id,
                                                    'commit_id': commit_data['commit_id'],
                                                    'file_path': file_path
                                                }
                                                # 使用计数器确保任务的唯一性
                                                import time
                                                task_counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
                                                task_wrapper = TaskWrapper(8, task_counter, task_data)  # 低优先级，后台缓存
                                                background_task_queue.put(task_wrapper)
                                                excel_tasks_added += 1
                                                log_print(f"✅ [BACKGROUND_SYNC] Excel缓存任务已添加: {file_path}", 'SYNC')
                                            except Exception as e:
                                                log_print(f"❌ [BACKGROUND_SYNC] 添加Excel缓存任务失败: {e}", 'SYNC', force=True)
                                    else:
                                        log_print(f"⏭️ [BACKGROUND_SYNC] 跳过已存在提交 {i+1}/{len(commits)}: {commit_data['commit_id'][:8]}", 'SYNC')
                                
                                # 提交数据库更改
                                db.session.commit()
                                log_print(f"✅ [BACKGROUND_SYNC] 后台同步完成，添加了 {commits_added} 个新提交，{excel_tasks_added} 个Excel缓存任务", 'SYNC')
                                log_print(f"✅ 自动数据分析完成: {repository.name}, 添加了 {commits_added} 个提交记录，{excel_tasks_added} 个Excel缓存任务", 'SYNC')
                                
                            elif repository.type == 'svn':
                                svn_service = get_svn_service(repository)

                                # 同步SVN仓库提交记录，传入数据库模块避免循环导入
                                commits_added = svn_service.sync_repository_commits(db, Commit)
                                log_print(f"✅ 自动数据分析完成: {repository.name}, 添加了 {commits_added} 个提交记录", 'SYNC')
                            else:
                                raise Exception(f"不支持的仓库类型: {repository.type}")
                        else:
                            raise Exception(f"仓库不存在: {task['repository_id']}")
                        
                        # 标记任务完成 - 使用重试机制
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'completed')
                            except Exception as update_error:
                                log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
                                
                    except Exception as e:
                        log_print(f"❌ 自动数据分析失败: {e}", 'SYNC', force=True)
                        # 标记任务失败
                        if 'task_id' in task:
                            db_task = db.session.get(BackgroundTask, task['task_id'])
                            if db_task:
                                db_task.status = 'failed'
                                db_task.error_message = str(e)
                                db_task.completed_at = datetime.now(timezone.utc)
                                db_task.retry_count += 1
                                db.session.commit()
            elif task['type'] == 'weekly_sync':
                log_print(f"📅 周版本同步: 配置 {task['config_id']}", 'WEEKLY')

                # 在Flask应用上下文中执行
                with app.app_context():
                    # 更新数据库任务状态为处理中
                    if 'task_id' in task:
                        try:
                            update_task_status_with_retry(task['task_id'], 'processing')
                        except Exception as update_error:
                            log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)

                    try:
                        # 执行周版本同步
                        process_weekly_version_sync(task['config_id'])

                        # 标记任务完成
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'completed')
                            except Exception as update_error:
                                log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)

                    except Exception as e:
                        log_print(f"❌ 周版本同步失败: {e}", 'WEEKLY', force=True)
                        # 标记任务失败
                        if 'task_id' in task:
                            try:
                                update_task_status_with_retry(task['task_id'], 'failed', str(e))
                            except Exception as update_error:
                                log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)

            elif task['type'] == 'weekly_excel_cache':
                log_print(f"📊 周版本Excel缓存: 配置 {task['data']['config_id']}, 文件 {task['data']['file_path']}", 'WEEKLY')

                # 在Flask应用上下文中执行
                with app.app_context():
                    # 更新数据库任务状态为处理中
                    if 'id' in task:
                        try:
                            update_task_status_with_retry(task['id'], 'processing')
                        except Exception as update_error:
                            log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)

                    try:
                        # 执行周版本Excel缓存生成
                        process_weekly_excel_cache(task['data']['config_id'], task['data']['file_path'])

                        # 标记任务完成
                        if 'id' in task:
                            try:
                                update_task_status_with_retry(task['id'], 'completed')
                            except Exception as update_error:
                                log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)

                    except Exception as e:
                        log_print(f"❌ 周版本Excel缓存生成失败: {e}", 'WEEKLY', force=True)
                        # 标记任务失败
                        if 'id' in task:
                            try:
                                update_task_status_with_retry(task['id'], 'failed', str(e))
                            except Exception as update_error:
                                log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)

            log_print(f"✅ 后台任务完成: {task['type']} (优先级: {priority}) | 队列剩余: {background_task_queue.qsize()}", 'TASK')
            
        except queue.Empty:
            # log_print("队列为空，等待新任务...", 'INFO')
            continue
        except Exception as e:
            log_print(f"后台任务处理异常: {e}", 'APP', force=True)
            import traceback
            traceback.print_exc()
        finally:
            # 只有成功获取任务后才调用task_done()
            if task_processed:
                try:
                    background_task_queue.task_done()
                except ValueError:
                    # 如果task_done()已经被调用过，忽略错误
                    pass
    
    log_print("后台任务工作线程停止", 'APP')

def create_auto_sync_task(repository_id):
    """为仓库创建自动数据分析任务"""
    try:
        # 检查是否已存在该仓库的同步任务
        existing_task = BackgroundTask.query.filter_by(
            repository_id=repository_id,
            task_type='auto_sync',
            status='pending'
        ).first()
        
        if existing_task:
            log_print(f"仓库 {repository_id} 已存在待处理的自动同步任务", 'SYNC')
            return existing_task.id
        
        # 创建新的自动同步任务
        new_task = BackgroundTask(
            task_type='auto_sync',
            repository_id=repository_id,
            priority=5,  # 中等优先级，低于用户手动请求但高于后台缓存
            status='pending'
        )
        
        db.session.add(new_task)
        db.session.commit()
        
        # 添加到内存队列
        task_data = {
            'type': 'auto_sync',
            'repository_id': repository_id,
            'task_id': new_task.id
        }
        import time
        task_counter = int(time.time() * 1000000)
        task_wrapper = TaskWrapper(5, task_counter, task_data)
        background_task_queue.put(task_wrapper)
        
        log_print(f"✅ 为仓库 {repository_id} 创建自动数据分析任务 (ID: {new_task.id})", 'SYNC')
        return new_task.id
        
    except Exception as e:
        log_print(f"❌ 创建自动同步任务失败: {e}", 'SYNC', force=True)
        return None

def check_and_create_auto_sync_tasks():
    """检查已克隆但未分析的仓库，自动创建数据分析任务"""
    try:
        # 查找克隆完成但没有提交数据的仓库
        repositories = Repository.query.filter_by(clone_status='completed').all()
        created_tasks = 0
        
        for repo in repositories:
            # 检查仓库是否有提交数据
            commit_count = Commit.query.filter_by(repository_id=repo.id).count()
            
            if commit_count == 0:
                log_print(f"🔍 发现已克隆但未分析的仓库: {repo.name} (ID: {repo.id})", 'SYNC')
                task_id = create_auto_sync_task(repo.id)
                if task_id:
                    created_tasks += 1
        
        if created_tasks > 0:
            log_print(f"✅ 为 {created_tasks} 个仓库创建了自动数据分析任务", 'SYNC')
        else:
            log_print("ℹ️ 没有发现需要自动分析的仓库", 'SYNC')
            
    except Exception as e:
        log_print(f"❌ 检查自动同步任务失败: {e}", 'SYNC', force=True)

def load_pending_tasks():
    """从数据库加载待处理的任务到内存队列"""
    try:
        pending_tasks = BackgroundTask.query.filter_by(status='pending').order_by(BackgroundTask.priority.asc(), BackgroundTask.created_at.asc()).all()

        for db_task in pending_tasks:
            # 根据任务类型构造不同的task_data结构
            if db_task.task_type == 'weekly_excel_cache':
                task_data = {
                    'id': db_task.id,
                    'type': 'weekly_excel_cache',
                    'data': {
                        'config_id': db_task.repository_id,  # repository_id字段存储的是config_id
                        'file_path': db_task.file_path
                    }
                }
            else:
                task_data = {
                    'type': db_task.task_type,
                    'repository_id': db_task.repository_id,
                    'commit_id': db_task.commit_id,
                    'file_path': db_task.file_path,
                    'task_id': db_task.id
                }

            import time
            task_counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
            # 确保优先级是整数，避免None或其他类型导致比较错误
            priority = db_task.priority if db_task.priority is not None else 10
            task_wrapper = TaskWrapper(priority, task_counter, task_data)
            background_task_queue.put(task_wrapper)
        
        log_print(f"从数据库加载了 {len(pending_tasks)} 个待处理任务到队列", 'TASK')
        
        # 重置处理中的任务状态为待处理（服务重启时）
        processing_tasks = BackgroundTask.query.filter_by(status='processing').all()
        for task in processing_tasks:
            task.status = 'pending'
            task.started_at = None
        
        if processing_tasks:
            db.session.commit()
            log_print(f"重置了 {len(processing_tasks)} 个处理中的任务状态为待处理", 'TASK')
        
        # 检查并创建自动同步任务
        check_and_create_auto_sync_tasks()
            
    except Exception as e:
        log_print(f"加载待处理任务失败: {e}", 'TASK', force=True)

def start_background_task_worker():
    """启动后台任务工作线程"""
    global background_task_running, background_task_thread
    
    if not background_task_running:
        background_task_running = True
        
        # 启动前先加载数据库中的待处理任务
        load_pending_tasks()
        
        background_task_thread = threading.Thread(target=background_task_worker, daemon=True)
        background_task_thread.start()
        log_print("后台任务工作线程已启动", 'APP')

def stop_background_task_worker():
    """停止后台任务工作线程"""
    global background_task_running, background_task_thread
    
    if background_task_running:
        log_print("正在停止后台任务工作线程...", 'APP')
        background_task_running = False
        if background_task_thread and background_task_thread.is_alive():
            try:
                background_task_thread.join(timeout=3)
                if background_task_thread.is_alive():
                    log_print("后台任务线程未能在3秒内正常停止", 'APP', force=True)
                else:
                    log_print("后台任务工作线程已停止", 'APP')
            except Exception as e:
                log_print(f"停止后台任务线程时出现错误: {e}", 'APP', force=True)
        else:
            log_print("后台任务工作线程已停止", 'APP')

def add_excel_diff_task(repository_id, commit_id, file_path, priority=10, auto_commit=True):
    """添加Excel差异处理任务到优先级队列
    
    Args:
        repository_id: 仓库ID
        commit_id: 提交ID
        file_path: 文件路径
        priority: 优先级 (数字越小优先级越高，1=最高优先级，10=普通优先级)
        auto_commit: 是否自动提交事务，默认True。在批量操作中可设为False
    """
    # 检查是否已存在相同的待处理任务
    existing_task = BackgroundTask.query.filter_by(
        task_type='excel_diff',
        repository_id=repository_id,
        commit_id=commit_id,
        file_path=file_path,
        status='pending'
    ).first()
    
    if existing_task:
        # 如果新任务优先级更高，更新现有任务的优先级
        if priority < existing_task.priority:
            existing_task.priority = priority
            if auto_commit:
                db.session.commit()
            log_print(f"更新任务优先级: {file_path} (优先级: {priority})", 'TASK')
        return existing_task.id if existing_task else None
    
    # 创建新的持久化任务
    task = BackgroundTask(
        task_type='excel_diff',
        repository_id=repository_id,
        commit_id=commit_id,
        file_path=file_path,
        priority=priority
    )
    db.session.add(task)
    if auto_commit:
        db.session.commit()
    
    # 同时添加到内存队列以便立即处理
    task_data = {
        'type': 'excel_diff',
        'repository_id': repository_id,
        'commit_id': commit_id,
        'file_path': file_path,
        'task_id': task.id
    }
    # 使用计数器确保任务的唯一性，避免字典比较问题
    import time
    task_counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
    task_wrapper = TaskWrapper(priority, task_counter, task_data)
    background_task_queue.put(task_wrapper)
    
    priority_text = "高优先级" if priority < 5 else "普通优先级"
    log_print(f"添加Excel差异任务到队列 ({priority_text}): {file_path}", 'EXCEL')

def add_excel_diff_tasks_batch(repository_id, excel_commits, priority=10):
    """批量添加Excel差异处理任务到优先级队列 - 性能优化版本
    
    Args:
        repository_id: 仓库ID
        excel_commits: Excel提交数据列表
        priority: 优先级 (数字越小优先级越高)
    """
    if not excel_commits:
        return
    
    # 批量检查现有任务，避免重复添加
    existing_tasks = set()
    existing_query = BackgroundTask.query.filter_by(
        task_type='excel_diff',
        repository_id=repository_id,
        status='pending'
    ).all()
    
    for task in existing_query:
        existing_tasks.add((task.commit_id, task.file_path))
    
    # 准备批量插入的新任务
    new_tasks = []
    queue_tasks = []
    import time
    base_counter = int(time.time() * 1000000)
    
    for i, commit_data in enumerate(excel_commits):
        commit_id = commit_data['commit_id']
        file_path = commit_data['path']
        
        # 跳过已存在的任务
        if (commit_id, file_path) in existing_tasks:
            continue
        
        # 准备数据库任务数据
        new_tasks.append({
            'task_type': 'excel_diff',
            'repository_id': repository_id,
            'commit_id': commit_id,
            'file_path': file_path,
            'priority': priority
        })
    
    # 批量插入到数据库
    if new_tasks:
        db.session.bulk_insert_mappings(BackgroundTask, new_tasks)
        db.session.commit()
        
        # 获取插入的任务ID并添加到内存队列
        inserted_tasks = BackgroundTask.query.filter_by(
            task_type='excel_diff',
            repository_id=repository_id,
            status='pending'
        ).filter(BackgroundTask.id > (db.session.query(db.func.max(BackgroundTask.id)).scalar() or 0) - len(new_tasks)).all()
        
        # 添加到内存队列
        for i, task in enumerate(inserted_tasks):
            task_data = {
                'type': 'excel_diff',
                'repository_id': repository_id,
                'commit_id': task.commit_id,
                'file_path': task.file_path,
                'task_id': task.id
            }
            task_counter = base_counter + i
            task_wrapper = TaskWrapper(priority, task_counter, task_data)
            background_task_queue.put(task_wrapper)
        
        log_print(f"批量添加了 {len(new_tasks)} 个Excel缓存任务到队列", 'TASK')

def regenerate_repository_cache(repository_id):
    """重新生成仓库的Excel文件缓存"""
    try:
        log_print(f"开始重新生成仓库缓存: {repository_id}", 'CACHE')
        
        repository = db.session.get(Repository, repository_id)
        if not repository:
            log_print(f"仓库不存在: {repository_id}", 'CACHE', force=True)
            return 0
        
        # 1. 首先清理该仓库的所有待处理和处理中的任务
        log_print(f"清理仓库 {repository_id} 的现有队列任务", 'CACHE')
        
        # 删除待处理的任务
        pending_tasks_deleted = BackgroundTask.query.filter(
            BackgroundTask.repository_id == repository_id,
            BackgroundTask.status.in_(['pending', 'processing'])
        ).delete(synchronize_session=False)
        
        log_print(f"删除了 {pending_tasks_deleted} 个现有队列任务", 'CACHE')
        
        # 2. 删除现有缓存数据
        log_print(f"清理仓库 {repository_id} 的现有缓存数据", 'CACHE')
        cache_deleted = DiffCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {cache_deleted} 个缓存记录", 'CACHE')
        
        db.session.commit()
        
        # 3. 获取最近1000条提交中的Excel文件
        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        
        log_print(f"找到 {len(recent_commits)} 个最近的Excel文件提交", 'CACHE')
        
        # 4. 为每个提交添加新的处理任务
        for commit in recent_commits:
            add_excel_diff_task(repository_id, commit.commit_id, commit.path)
        
        log_print(f"已添加 {len(recent_commits)} 个缓存重建任务", 'CACHE')
        return len(recent_commits)
        
    except Exception as e:
        log_print(f"重新生成仓库缓存失败: {e}", 'CACHE', force=True)
        import traceback
        traceback.print_exc()

# 定时任务：每天凌晨4点清理1个月前的缓存数据
def schedule_cleanup_task():
    """调度清理任务"""
    task = {
        'type': 'cleanup_cache',
        'days': 30,
        'task_id': None  # 清理任务不需要数据库记录，设为None
    }
    import time
    task_counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
    task_wrapper = TaskWrapper(20, task_counter, task)  # 清理任务使用低优先级
    background_task_queue.put(task_wrapper)
    log_print("添加缓存清理任务到队列", 'TASK')

# 周版本同步定时任务
def schedule_weekly_sync_tasks():
    """调度周版本同步任务"""
    try:
        with app.app_context():
            # 获取所有启用的周版本配置
            active_configs = WeeklyVersionConfig.query.filter_by(
                is_active=True,
                auto_sync=True
            ).all()

            for config in active_configs:
                # 检查是否在时间范围内或已结束
                now = datetime.now(timezone.utc)

                # 如果配置已结束且状态还是active，更新为completed
                if now > config.end_time and config.status == 'active':
                    config.status = 'completed'
                    db.session.commit()
                    log_print(f"周版本配置已完成: {config.name}", 'WEEKLY')
                    continue

                # 如果配置还在进行中，创建同步任务
                if config.status == 'active':
                    create_weekly_sync_task(config.id)

            log_print(f"检查了 {len(active_configs)} 个周版本配置", 'WEEKLY')

    except Exception as e:
        log_print(f"调度周版本同步任务失败: {e}", 'WEEKLY', force=True)

# 设置定时任务
schedule.every().day.at("04:00").do(schedule_cleanup_task)
schedule.every(2).minutes.do(schedule_weekly_sync_tasks)  # 每2分钟检查一次周版本同步

# 定时任务检查器
def run_scheduled_tasks():
    """运行定时任务检查器"""
    while background_task_running:
        schedule.run_pending()
        time.sleep(60)  # 每分钟检查一次

# 启动定时任务线程
def start_scheduler():
    """启动定时任务调度器"""
    scheduler_thread = threading.Thread(target=run_scheduled_tasks, daemon=True)
    scheduler_thread.start()
    log_print("定时任务调度器已启动", 'APP')

# 测试路由
@app.route('/auth/login', methods=['GET', 'POST'])
def admin_login():
    next_url = request.args.get('next') or request.form.get('next') or url_for('index')
    if request.method == 'POST':
        configured_user = os.environ.get('ADMIN_USERNAME', 'admin').strip()
        configured_password = os.environ.get('ADMIN_PASSWORD', '').strip()
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()

        if not configured_password:
            flash('ADMIN_PASSWORD 未配置，无法登录管理员账号。', 'error')
            return render_template('admin_login.html', next_url=next_url), 500

        if hmac.compare_digest(username, configured_user) and hmac.compare_digest(password, configured_password):
            session['is_admin'] = True
            session['admin_user'] = username
            session.permanent = True
            flash('管理员登录成功。', 'success')
            if not _is_safe_redirect(next_url):
                next_url = url_for('index')
            return redirect(next_url)

        flash('管理员账号或密码错误。', 'error')
    return render_template('admin_login.html', next_url=next_url)


@app.route('/auth/logout', methods=['POST'])
def admin_logout():
    session.pop('is_admin', None)
    session.pop('admin_user', None)
    session.pop(CSRF_SESSION_KEY, None)
    flash('已退出管理员登录。', 'success')
    return redirect(url_for('index'))


@app.route('/test')
def test():
    return "服务器正常工作！"

# 主页路由
@app.route('/')
def index():
    try:
        log_print("访问主页路由", 'APP')
        projects = Project.query.order_by(Project.created_at.desc()).all()
        log_print(f"找到 {len(projects)} 个项目", 'APP')
        return render_template('index.html', projects=projects)
    except Exception as e:
        log_print(f"主页路由错误: {str(e)}", 'APP', force=True)
        import traceback
        traceback.print_exc()
        return f"主页加载错误: {str(e)}", 500

# 项目管理路由
@app.route('/projects', methods=['GET', 'POST'])
def projects():
    if request.method == 'POST':
        code = request.form.get('code')
        name = request.form.get('name')
        department = request.form.get('department')
        
        if not code or not name:
            flash('项目代号和名称不能为空', 'error')
            return redirect(url_for('projects'))
        
        # 检查项目代号是否已存在
        existing_project = Project.query.filter_by(code=code).first()
        if existing_project:
            flash('项目代号已存在', 'error')
            return redirect(url_for('projects'))
        
        project = Project(code=code, name=name, department=department)
        db.session.add(project)
        db.session.commit()
        flash('项目创建成功', 'success')
        return redirect(url_for('projects'))
    
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template('projects.html', projects=projects)

# 项目详情页面 - 重定向到项目概览
@app.route('/projects/<int:project_id>')
def project_detail(project_id):
    # 直接重定向到项目概览页面
    return redirect(url_for('merged_project_view', project_id=project_id))

# 保留原项目详情页面作为备用
@app.route('/projects/<int:project_id>/detail')
def project_detail_original(project_id):
    project = Project.query.get_or_404(project_id)
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()
    return render_template('project_detail.html', project=project, repositories=repositories)

# 周版本配置相关路由
@app.route('/projects/<int:project_id>/weekly-version-config')
def weekly_version_config(project_id):
    """周版本配置页面"""
    project = Project.query.get_or_404(project_id)
    repositories = Repository.query.filter_by(project_id=project_id).all()

    # 获取分页参数
    page = max(1, request.args.get('page', 1, type=int) or 1)
    requested_per_page = request.args.get('per_page', 20, type=int) or 20
    per_page = min(max(requested_per_page, 1), 200)  # 每页最大200，防止大分页拖垮查询

    # 获取所有配置用于分组
    all_configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).order_by(WeeklyVersionConfig.created_at.desc()).all()

    # 按版本名称和时间范围分组配置
    version_groups = {}
    for config in all_configs:
        # 提取版本基础名称（去掉仓库后缀）
        base_name = config.name
        if ' - ' in config.name:
            base_name = config.name.split(' - ')[0]

        # 创建分组键：版本名称 + 时间范围
        start_time = config.start_time.strftime('%Y-%m-%d %H:%M')
        end_time = config.end_time.strftime('%Y-%m-%d %H:%M')
        group_key = f"{base_name}_{start_time}_{end_time}"

        if group_key not in version_groups:
            version_groups[group_key] = {
                'version_name': base_name,
                'start_time': config.start_time,
                'end_time': config.end_time,
                'configs': [],
                'status': 'active',  # 默认状态
                'cycle_type': config.cycle_type,
                'created_at': config.created_at
            }

        version_groups[group_key]['configs'].append(config)

        # 更新组状态（如果有任何一个配置是completed，则整组为completed）
        if config.status == 'completed':
            version_groups[group_key]['status'] = 'completed'
        elif config.status == 'archived' and version_groups[group_key]['status'] != 'completed':
            version_groups[group_key]['status'] = 'archived'

    # 转换为列表并按优先级排序：活跃版本优先，然后按结束时间倒序
    all_grouped_versions = list(version_groups.values())

    # 判断版本是否活跃（当前时间在版本时间范围内）
    from datetime import datetime, timezone
    from utils.timezone_utils import now_beijing
    now = now_beijing()

    # 分类版本：活跃版本、未来版本、已结束版本
    active_versions = []    # 当前时间在版本区间内
    future_versions = []    # 开始时间在未来
    ended_versions = []     # 结束时间已过

    for group in all_grouped_versions:
        try:
            # 将now转换为本地时间（无时区）
            now_local = now.replace(tzinfo=None)

            # 确保数据库时间也是无时区的
            start_time = group['start_time']
            end_time = group['end_time']
            if start_time.tzinfo is not None:
                start_time = start_time.replace(tzinfo=None)
            if end_time.tzinfo is not None:
                end_time = end_time.replace(tzinfo=None)

            # 分类逻辑
            if start_time <= now_local <= end_time:
                # 活跃版本：当前时间在版本区间内
                group['category'] = 'active'
                active_versions.append(group)
            elif start_time > now_local:
                # 未来版本：开始时间在未来
                group['category'] = 'future'
                future_versions.append(group)
            else:
                # 已结束版本：结束时间已过
                group['category'] = 'ended'
                ended_versions.append(group)

        except Exception as e:
            log_print(f"时间比较出错: {str(e)}", 'APP', force=True)
            # 如果时间比较出错，默认归类为已结束版本
            group['category'] = 'ended'
            ended_versions.append(group)

    # 各分类内部排序：按结束时间倒序
    active_versions.sort(key=lambda x: -x['end_time'].timestamp())
    future_versions.sort(key=lambda x: -x['end_time'].timestamp())
    ended_versions.sort(key=lambda x: -x['end_time'].timestamp())

    # 合并所有版本：活跃版本 -> 未来版本 -> 已结束版本
    all_grouped_versions = active_versions + future_versions + ended_versions

    # 计算分页信息
    total_groups = len(all_grouped_versions)
    total_pages = (total_groups + per_page - 1) // per_page
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page

    # 获取当前页的版本组
    grouped_versions = all_grouped_versions[start_idx:end_idx]

    # 分页信息
    pagination = {
        'page': page,
        'per_page': per_page,
        'total': total_groups,
        'total_pages': total_pages,
        'has_prev': page > 1,
        'has_next': page < total_pages,
        'prev_num': page - 1 if page > 1 else None,
        'next_num': page + 1 if page < total_pages else None
    }

    return render_template('weekly_version_config.html',
                         project=project,
                         repositories=repositories,
                         configs=all_configs,  # 保留原始配置用于模态框
                         grouped_versions=grouped_versions,
                         active_versions=active_versions,
                         future_versions=future_versions,
                         ended_versions=ended_versions,
                         pagination=pagination)

@app.route('/projects/<int:project_id>/weekly-version-config/api', methods=['GET', 'POST'])
def weekly_version_config_api(project_id):
    """周版本配置API"""
    project = Project.query.get_or_404(project_id)

    if request.method == 'GET':
        # 获取配置列表
        configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).all()
        return jsonify({
            'success': True,
            'configs': [{
                'id': config.id,
                'name': config.name,
                'description': config.description,
                'repository_id': config.repository_id,
                'repository_name': config.repository.name,
                'branch': config.branch,
                'start_time': config.start_time.isoformat(),
                'end_time': config.end_time.isoformat(),
                'cycle_type': config.cycle_type,
                'is_active': config.is_active,
                'auto_sync': config.auto_sync,
                'status': config.status,
                'created_at': config.created_at.isoformat()
            } for config in configs]
        })

    elif request.method == 'POST':
        # 创建新配置
        try:
            data = request.get_json()

            # 验证必需字段
            required_fields = ['name', 'repository_id', 'branch', 'start_time', 'end_time']
            for field in required_fields:
                if not data.get(field):
                    return jsonify({'success': False, 'message': f'缺少必需字段: {field}'}), 400

            # 解析时间并设置默认秒钟
            start_time = datetime.fromisoformat(data['start_time'].replace('T', ' '))
            end_time = datetime.fromisoformat(data['end_time'].replace('T', ' '))

            # 开始时间的秒钟默认为00
            start_time = start_time.replace(second=0, microsecond=0)
            # 结束时间的秒钟默认为59
            end_time = end_time.replace(second=59, microsecond=999999)

            if start_time >= end_time:
                return jsonify({'success': False, 'message': '开始时间必须早于结束时间'}), 400

            created_configs = []

            # 处理"全部仓库"选项
            if data['repository_id'] == 'all':
                # 获取项目下的所有仓库
                repositories = Repository.query.filter_by(project_id=project_id).all()
                if not repositories:
                    return jsonify({'success': False, 'message': '该项目下没有仓库'}), 400

                # 为每个仓库创建配置
                for repository in repositories:
                    config = WeeklyVersionConfig(
                        project_id=project_id,
                        repository_id=repository.id,
                        name=f"{data['name']} - {repository.name}",  # 添加仓库名称后缀
                        description=data.get('description', ''),
                        branch=data['branch'],
                        start_time=start_time,
                        end_time=end_time,
                        cycle_type=data.get('cycle_type', 'custom'),
                        is_active=data.get('is_active', True),
                        auto_sync=data.get('auto_sync', True),
                        status='active'
                    )

                    db.session.add(config)
                    created_configs.append(config)

                db.session.commit()

                # 如果启用自动同步，为每个配置创建后台同步任务
                for config in created_configs:
                    if config.auto_sync and config.is_active:
                        create_weekly_sync_task(config.id)

                return jsonify({
                    'success': True,
                    'message': f'成功为 {len(created_configs)} 个仓库创建配置',
                    'config_count': len(created_configs)
                })

            else:
                # 单个仓库配置
                repository = Repository.query.filter_by(id=data['repository_id'], project_id=project_id).first()
                if not repository:
                    return jsonify({'success': False, 'message': '仓库不存在或不属于该项目'}), 400

                # 创建配置
                config = WeeklyVersionConfig(
                    project_id=project_id,
                    repository_id=data['repository_id'],
                    name=data['name'],
                    description=data.get('description', ''),
                    branch=data['branch'],
                    start_time=start_time,
                    end_time=end_time,
                    cycle_type=data.get('cycle_type', 'custom'),
                    is_active=data.get('is_active', True),
                    auto_sync=data.get('auto_sync', True),
                    status='active'
                )

                db.session.add(config)
                db.session.commit()

                # 如果启用自动同步，创建后台同步任务
                if config.auto_sync and config.is_active:
                    create_weekly_sync_task(config.id)

                return jsonify({
                    'success': True,
                    'message': '配置创建成功',
                    'config_id': config.id
                })

        except Exception as e:
            db.session.rollback()
            log_print(f"创建周版本配置失败: {e}", 'ERROR', force=True)
            return jsonify({'success': False, 'message': f'创建失败: {str(e)}'}), 500

@app.route('/projects/<int:project_id>/weekly-version-config/api/<int:config_id>', methods=['GET', 'PUT', 'DELETE'])
def weekly_version_config_detail_api(project_id, config_id):
    """周版本配置详情API"""
    project = Project.query.get_or_404(project_id)
    config = WeeklyVersionConfig.query.filter_by(id=config_id, project_id=project_id).first_or_404()

    if request.method == 'GET':
        # 获取配置详情
        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'name': config.name,
                'description': config.description,
                'repository_id': config.repository_id,
                'repository_name': config.repository.name,
                'branch': config.branch,
                'start_time': config.start_time.isoformat(),
                'end_time': config.end_time.isoformat(),
                'cycle_type': config.cycle_type,
                'is_active': config.is_active,
                'auto_sync': config.auto_sync,
                'status': config.status,
                'created_at': config.created_at.isoformat(),
                'updated_at': config.updated_at.isoformat()
            }
        })

    elif request.method == 'PUT':
        # 更新配置
        try:
            data = request.get_json()

            # 检查是否修改了时间范围
            time_changed = data.get('time_changed', False)
            original_start_time = config.start_time
            original_end_time = config.end_time

            # 更新字段
            if 'name' in data:
                config.name = data['name']
            if 'description' in data:
                config.description = data['description']
            if 'branch' in data:
                config.branch = data['branch']
            if 'start_time' in data:
                new_start_time = datetime.fromisoformat(data['start_time'].replace('T', ' '))
                # 开始时间的秒钟默认为00
                new_start_time = new_start_time.replace(second=0, microsecond=0)
                if new_start_time != original_start_time:
                    time_changed = True
                config.start_time = new_start_time
            if 'end_time' in data:
                new_end_time = datetime.fromisoformat(data['end_time'].replace('T', ' '))
                # 结束时间的秒钟默认为59
                new_end_time = new_end_time.replace(second=59, microsecond=999999)
                if new_end_time != original_end_time:
                    time_changed = True
                config.end_time = new_end_time
            if 'cycle_type' in data:
                config.cycle_type = data['cycle_type']
            if 'is_active' in data:
                config.is_active = data['is_active']
            if 'auto_sync' in data:
                config.auto_sync = data['auto_sync']
            if 'status' in data:
                config.status = data['status']

            config.updated_at = datetime.now(timezone.utc)

            # 如果时间范围发生变化，清空所有相关的diff缓存和确认状态
            if time_changed:
                log_print(f"时间范围已变更，清空配置 {config.name} 的所有diff缓存", 'WEEKLY')

                # 删除所有相关的diff缓存
                deleted_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).delete()
                log_print(f"已删除 {deleted_count} 条diff缓存记录", 'WEEKLY')

                # 如果启用了自动同步，创建新的同步任务
                if config.auto_sync and config.is_active:
                    create_weekly_sync_task(config_id)
                    log_print(f"已创建新的同步任务", 'WEEKLY')

            db.session.commit()

            return jsonify({
                'success': True,
                'message': '配置更新成功',
                'time_changed': time_changed
            })

        except Exception as e:
            db.session.rollback()
            log_print(f"更新周版本配置失败: {e}", 'ERROR', force=True)
            return jsonify({'success': False, 'message': f'更新失败: {str(e)}'}), 500

    elif request.method == 'DELETE':
        # 删除配置
        try:
            # 删除相关的Excel缓存
            excel_cache_deleted = WeeklyVersionExcelCache.query.filter_by(config_id=config_id).delete()
            log_print(f"删除了 {excel_cache_deleted} 个Excel缓存记录", 'WEEKLY')

            # 删除相关的diff缓存
            diff_cache_deleted = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).delete()
            log_print(f"删除了 {diff_cache_deleted} 个diff缓存记录", 'WEEKLY')

            # 删除相关的后台任务
            task_deleted = BackgroundTask.query.filter(
                BackgroundTask.repository_id == config_id,
                BackgroundTask.task_type.in_(['weekly_excel_cache', 'weekly_sync'])
            ).delete(synchronize_session=False)
            log_print(f"删除了 {task_deleted} 个后台任务", 'WEEKLY')

            # 删除配置
            db.session.delete(config)
            db.session.commit()

            return jsonify({'success': True, 'message': '配置删除成功'})

        except Exception as e:
            db.session.rollback()
            log_print(f"删除周版本配置失败: {e}", 'ERROR', force=True)
            return jsonify({'success': False, 'message': f'删除失败: {str(e)}'}), 500

@app.route('/projects/<int:project_id>/weekly-version')
def weekly_version_list(project_id):
    """周版本diff列表页面"""
    project = Project.query.get_or_404(project_id)
    repository_id = request.args.get('repository_id', type=int)

    # 获取配置列表
    query = WeeklyVersionConfig.query.filter_by(project_id=project_id)
    if repository_id:
        query = query.filter_by(repository_id=repository_id)

    configs = query.order_by(WeeklyVersionConfig.created_at.desc()).all()

    return render_template('weekly_version_list.html',
                         project=project,
                         configs=configs,
                         selected_repository_id=repository_id)

@app.route('/projects/<int:project_id>/merged-view')
def merged_project_view(project_id):
    """合并的项目视图：左侧周版本列表，右侧仓库列表"""
    project = Project.query.get_or_404(project_id)

    # 获取所有周版本配置
    configs = WeeklyVersionConfig.query.filter_by(project_id=project_id).order_by(WeeklyVersionConfig.created_at.desc()).all()

    # 获取所有仓库
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()

    # 按时间范围和名称分组周版本配置
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    # 分组逻辑：相同版本基础名称+相同时间范围的配置归为一组
    version_groups = {}
    for config in configs:
        # 提取版本基础名称（去掉仓库后缀）
        # 例如："第一周版本 - qz_client_lua" -> "第一周版本"
        base_name = config.name
        if ' - ' in config.name:
            base_name = config.name.split(' - ')[0]

        # 创建分组键：基础名称 + 开始时间 + 结束时间
        group_key = f"{base_name}_{config.start_time.strftime('%Y%m%d%H%M')}_{config.end_time.strftime('%Y%m%d%H%M')}"

        if group_key not in version_groups:
            version_groups[group_key] = {
                'name': base_name,  # 使用基础名称作为显示名称
                'start_time': config.start_time,
                'end_time': config.end_time,
                'configs': [],
                'is_active': False
            }

        version_groups[group_key]['configs'].append(config)

        # 判断是否为活跃版本（当前时间在版本时间范围内）
        # 处理时区问题：统一转换为无时区的本地时间进行比较
        try:
            # 将now转换为本地时间（无时区）
            now_local = now.replace(tzinfo=None)

            # 确保数据库时间也是无时区的
            start_time = config.start_time
            end_time = config.end_time
            if start_time.tzinfo is not None:
                start_time = start_time.replace(tzinfo=None)
            if end_time.tzinfo is not None:
                end_time = end_time.replace(tzinfo=None)

            if start_time <= now_local <= end_time:
                version_groups[group_key]['is_active'] = True
        except Exception as e:
            log_print(f"时间比较出错: {str(e)}", 'APP', force=True)
            # 如果时间比较出错，默认为非活跃状态
            pass

    # 分离活跃和非活跃版本
    active_versions = []
    inactive_versions = []

    for group in version_groups.values():
        if group['is_active']:
            active_versions.append(group)
        else:
            inactive_versions.append(group)

    # 按时间排序
    active_versions.sort(key=lambda x: x['start_time'], reverse=True)
    inactive_versions.sort(key=lambda x: x['start_time'], reverse=True)

    # 为JavaScript准备序列化的非活跃版本数据
    inactive_versions_json = []
    for version in inactive_versions:
        version_data = {
            'name': version['name'],
            'start_time': version['start_time'].isoformat(),
            'end_time': version['end_time'].isoformat(),
            'is_active': version['is_active'],
            'configs': []
        }
        for config in version['configs']:
            config_data = {
                'id': config.id,
                'repository': {
                    'name': config.repository.name,
                    'type': config.repository.type
                }
            }
            version_data['configs'].append(config_data)
        inactive_versions_json.append(version_data)

    return render_template('merged_project_view.html',
                         project=project,
                         active_versions=active_versions,
                         inactive_versions=inactive_versions,
                         inactive_versions_json=inactive_versions_json,
                         repositories=repositories)

@app.route('/weekly-version-config/<int:config_id>/diff')
def weekly_version_diff(config_id):
    """周版本diff详情页面 - 聚合显示同一时间段的不同仓库配置"""
    config = WeeklyVersionConfig.query.get_or_404(config_id)

    # 查找同一项目下相同时间段的其他配置
    related_configs = WeeklyVersionConfig.query.filter(
        WeeklyVersionConfig.project_id == config.project_id,
        WeeklyVersionConfig.start_time == config.start_time,
        WeeklyVersionConfig.end_time == config.end_time,
        WeeklyVersionConfig.id != config_id  # 排除当前配置
    ).order_by(WeeklyVersionConfig.repository_id.asc()).all()

    # 将当前配置和相关配置合并，按仓库名排序
    all_configs = [config] + related_configs
    all_configs.sort(key=lambda c: c.repository.name)

    return render_template('weekly_version_diff.html',
                         config=config,
                         all_configs=all_configs,
                         current_config_id=config_id)

@app.route('/weekly-version-config/<int:config_id>/info')
def weekly_version_config_info_api(config_id):
    """获取周版本配置信息API"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)

        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'name': config.name,
                'repository': {
                    'id': config.repository.id,
                    'name': config.repository.name,
                    'type': config.repository.type,
                    'resource_type': config.repository.resource_type
                }
            }
        })

    except Exception as e:
        log_print(f"获取周版本配置信息失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/weekly-version-config/<int:config_id>/files')
def weekly_version_files_api(config_id):
    """获取周版本文件列表API"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)

        # 获取该配置的所有diff缓存
        diff_caches = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).all()

        files = []
        authors = set()

        for cache in diff_caches:
            # 解析提交者信息
            commit_authors = json.loads(cache.commit_authors) if cache.commit_authors else []
            authors.update(commit_authors)

            # 解析合并diff数据以获取文件操作信息
            file_operations = []
            if cache.merged_diff_data:
                try:
                    merged_data = json.loads(cache.merged_diff_data)
                    file_operations = merged_data.get('operations', [])
                except:
                    pass

            # 确定文件的主要操作类型（用于颜色编码）
            primary_operation = 'M'  # 默认为修改
            if file_operations:
                if 'D' in file_operations:
                    primary_operation = 'D'  # 删除优先级最高
                elif 'A' in file_operations:
                    primary_operation = 'A'  # 新增次之
                else:
                    primary_operation = 'M'  # 修改

            files.append({
                'file_path': cache.file_path,
                'commit_count': cache.commit_count,
                'commit_authors': cache.commit_authors,
                'commit_messages': cache.commit_messages,  # 添加提交日志
                'commit_times': cache.commit_times,        # 添加提交时间
                'overall_status': cache.overall_status,
                'confirmation_status': cache.confirmation_status,
                'last_sync_time': cache.last_sync_time.isoformat() if cache.last_sync_time else None,
                'operations': file_operations,  # 所有操作
                'primary_operation': primary_operation  # 主要操作类型
            })

        return jsonify({
            'success': True,
            'files': files,
            'authors': list(authors),
            'total_files': len(files),
            'repository_name': config.repository.name
        })

    except Exception as e:
        log_print(f"获取周版本文件列表失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/weekly-version-config/<int:config_id>/file-diff')
def weekly_version_file_diff_api(config_id):
    """获取单个文件的diff内容"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get('file_path')

        if not file_path:
            return "缺少文件路径参数", 400

        # 获取该文件的diff缓存
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            return "<div class='alert alert-warning'>未找到该文件的diff数据</div>"

        # 生成真实的Git diff内容
        diff_html = generate_weekly_git_diff_html(config, diff_cache, file_path)

        return diff_html

    except Exception as e:
        log_print(f"获取文件diff失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>加载diff失败: {str(e)}</div>"

@app.route('/weekly-version-config/<int:config_id>/file-full-diff')
def weekly_version_file_full_diff(config_id):
    """周版本文件完整diff页面 - 优化版本，先显示页面框架"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get('file_path')

        if not file_path:
            return "缺少文件路径参数", 400

        # 获取该文件的diff缓存基本信息
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            return render_template('error.html',
                                 error_message="未找到该文件的diff数据",
                                 back_url=url_for('weekly_version_diff', config_id=config_id))

        # 只准备基本的静态数据，不进行耗时的Git操作
        template_data = {
            'config': config,
            'file_path': file_path,
            'diff_cache': diff_cache,
            'base_commit_id': diff_cache.base_commit_id,
            'latest_commit_id': diff_cache.latest_commit_id,
            # 基本的提交信息（从缓存中获取，不需要Git操作）
            'commit_authors': json.loads(diff_cache.commit_authors) if diff_cache.commit_authors else [],
            'commit_messages': json.loads(diff_cache.commit_messages) if diff_cache.commit_messages else [],
            'commit_times': json.loads(diff_cache.commit_times) if diff_cache.commit_times else []
        }

        return render_template('weekly_version_full_diff.html', **template_data)

    except Exception as e:
        log_print(f"显示周版本完整diff失败: {e}", 'ERROR', force=True)
        return render_template('error.html',
                             error_message=f"加载失败: {str(e)}",
                             back_url=url_for('weekly_version_diff', config_id=config_id))

@app.route('/weekly-version-config/<int:config_id>/file-full-diff-data')
def weekly_version_file_full_diff_data(config_id):
    """异步加载周版本文件完整diff数据"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get('file_path')
        nocache = request.args.get('nocache', 'false').lower() == 'true'

        if not file_path:
            return jsonify({'success': False, 'message': '缺少文件路径参数'}), 400

        # 获取该文件的diff缓存
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            return jsonify({'success': False, 'message': '未找到该文件的diff数据'}), 404

        # 获取基准版本的详细信息（这是耗时操作）
        base_commit_info = None
        if diff_cache.base_commit_id:
            try:
                # 重新获取Repository对象，避免SQLAlchemy会话问题
                repository = Repository.query.get(config.repository_id)
                if not repository:
                    log_print(f"异步获取基准版本信息失败: 仓库不存在 {config.repository_id}", 'ERROR', force=True)
                else:
                    # 根据仓库类型选择合适的服务
                    if repository.type == 'git':
                        service = get_git_service(repository)
                    else:  # SVN仓库
                        service = get_svn_service(repository)

                    base_commit_info = service.get_commit_info(diff_cache.base_commit_id)
                    log_print(f"异步获取基准版本信息: {base_commit_info}", 'WEEKLY')
            except Exception as e:
                log_print(f"异步获取基准版本信息失败: {e}", 'ERROR', force=True)

        # 检查文件类型
        from services.diff_service import DiffService
        diff_service = DiffService()
        file_type = diff_service.get_file_type(file_path)

        # 生成diff HTML内容
        if nocache:
            log_print(f"🔄 重新计算周版本diff (绕过缓存): {file_path}", 'WEEKLY')
            # 如果是Excel文件且有周版本Excel缓存，先清理缓存
            if weekly_excel_cache_service.is_excel_file(file_path):
                try:
                    # 清理该文件的周版本Excel缓存
                    WeeklyVersionExcelCache.query.filter_by(
                        config_id=config_id,
                        file_path=file_path
                    ).delete()
                    db.session.commit()
                    log_print(f"已清理周版本Excel缓存: {file_path}", 'WEEKLY')
                except Exception as cache_e:
                    log_print(f"清理周版本Excel缓存失败: {cache_e}", 'WEEKLY', force=True)

            # 强制重新生成diff HTML（绕过所有缓存）
            diff_html = generate_weekly_git_diff_html(config, diff_cache, file_path, force_recalculate=True)
        else:
            # 正常生成diff HTML内容（可能使用缓存）
            diff_html = generate_weekly_git_diff_html(config, diff_cache, file_path)

        return jsonify({
            'success': True,
            'diff_html': diff_html,
            'base_commit_info': base_commit_info,
            'file_type': file_type,
            'recalculated': nocache
        })

    except Exception as e:
        log_print(f"异步加载周版本diff数据失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': f'加载失败: {str(e)}'}), 500

@app.route('/weekly-version-config/<int:config_id>/file-previous-version')
def weekly_version_file_previous_version(config_id):
    """查看周版本文件的上一版本"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get('file_path')
        commit_id = request.args.get('commit_id')

        if not file_path or not commit_id:
            return "缺少文件路径或提交ID参数", 400

        # 使用Git服务获取指定提交的文件内容
        from services.threaded_git_service import ThreadedGitService
        git_service = ThreadedGitService(
            config.repository.url,
            config.repository.root_directory,
            config.repository.username,
            config.repository.token,
            config.repository
        )

        # 获取文件内容
        file_content = git_service.get_file_content(commit_id, file_path)

        if file_content is None:
            return render_template('error.html',
                                 error_message="无法获取文件内容，文件可能不存在",
                                 back_url=url_for('weekly_version_file_full_diff',
                                                config_id=config_id,
                                                file_path=file_path))

        # 获取提交信息
        commit_info = git_service.get_commit_info(commit_id)

        return render_template('weekly_version_previous_file.html',
                             config=config,
                             file_path=file_path,
                             commit_id=commit_id,
                             commit_info=commit_info,
                             file_content=file_content)

    except Exception as e:
        log_print(f"查看上一版本文件失败: {e}", 'ERROR', force=True)
        return render_template('error.html',
                             error_message=f"加载失败: {str(e)}",
                             back_url=url_for('weekly_version_diff', config_id=config_id))

@app.route('/weekly-version-config/<int:config_id>/file-complete-diff')
def weekly_version_file_complete_diff(config_id):
    """周版本文件完整对比页面（类似单文件diff的完整对比）"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get('file_path')

        if not file_path:
            return "缺少文件路径参数", 400

        # 获取该文件的diff缓存
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            return render_template('error.html',
                                 error_message="未找到该文件的diff数据",
                                 back_url=url_for('weekly_version_diff', config_id=config_id))

        # 解析提交信息
        commit_authors = json.loads(diff_cache.commit_authors) if diff_cache.commit_authors else []
        commit_messages = json.loads(diff_cache.commit_messages) if diff_cache.commit_messages else []
        commit_times = json.loads(diff_cache.commit_times) if diff_cache.commit_times else []

        # 获取基准版本和当前版本的文件内容
        repository = config.repository

        # 获取基准版本内容
        previous_file_content = ""
        if diff_cache.base_commit_id:
            try:
                previous_file_content = get_file_content_at_commit(repository, diff_cache.base_commit_id, file_path)
            except Exception as e:
                log_print(f"获取基准版本文件内容失败: {e}", 'ERROR')
                previous_file_content = ""

        # 获取当前版本内容
        current_file_content = ""
        if diff_cache.latest_commit_id:
            try:
                current_file_content = get_file_content_at_commit(repository, diff_cache.latest_commit_id, file_path)
            except Exception as e:
                log_print(f"获取当前版本文件内容失败: {e}", 'ERROR')
                current_file_content = ""

        # 构建基准版本提交信息
        base_commit_info = None
        if diff_cache.base_commit_id:
            base_commit_info = {
                'short_id': diff_cache.base_commit_id[:8],
                'author': '基准版本',
                'commit_time': config.start_time.strftime('%Y-%m-%d %H:%M'),
                'message': '周版本基准'
            }

        # 生成Git风格的并排diff数据（与单个提交保持一致）
        side_by_side_diff = generate_side_by_side_diff(current_file_content, previous_file_content)

        return render_template('weekly_version_complete_diff.html',
                             config=config,
                             diff_cache=diff_cache,
                             file_path=file_path,
                             commit_authors=commit_authors,
                             commit_messages=commit_messages,
                             commit_times=commit_times,
                             base_commit_info=base_commit_info,
                             base_commit_id=diff_cache.base_commit_id,
                             latest_commit_id=diff_cache.latest_commit_id,
                             previous_file_content=previous_file_content,
                             current_file_content=current_file_content,
                             side_by_side_diff=side_by_side_diff)

    except Exception as e:
        log_print(f"获取周版本完整文件对比失败: {e}", 'ERROR', force=True)
        return render_template('error.html',
                             error_message=f"加载完整文件对比失败: {str(e)}",
                             back_url=url_for('weekly_version_diff', config_id=config_id))

@app.route('/weekly-version-config/<int:config_id>/file-status', methods=['POST'])
def weekly_version_file_status_api(config_id):
    """更新文件确认状态"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        data = request.get_json()

        file_path = data.get('file_path')
        status = data.get('status')

        if not file_path or not status:
            return jsonify({'success': False, 'message': '缺少必需参数'}), 400

        # 获取文件的diff缓存
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            return jsonify({'success': False, 'message': '未找到文件记录'}), 404

        # 更新确认状态
        old_status = diff_cache.overall_status
        confirmation_status = json.loads(diff_cache.confirmation_status) if diff_cache.confirmation_status else {}
        confirmation_status['dev'] = status

        diff_cache.confirmation_status = json.dumps(confirmation_status)
        diff_cache.overall_status = status
        diff_cache.updated_at = datetime.now(timezone.utc)

        db.session.commit()

        # 同步状态到提交记录
        if old_status != status:
            from services.status_sync_service import StatusSyncService
            sync_service = StatusSyncService(db)
            sync_result = sync_service.sync_weekly_to_commit(config_id, file_path, status)
            log_print(f"周版本状态同步结果: {sync_result}", 'SYNC')

        return jsonify({'success': True, 'message': '状态更新成功'})

    except Exception as e:
        db.session.rollback()
        log_print(f"更新文件状态失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/weekly-version-config/<int:config_id>/file-status-info')
def weekly_version_file_status_info_api(config_id):
    """获取文件确认状态信息"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)
        file_path = request.args.get('file_path')

        if not file_path:
            return jsonify({'success': False, 'message': '缺少文件路径参数'}), 400

        # 获取文件状态
        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            return jsonify({'success': False, 'message': '未找到文件记录'}), 404

        return jsonify({
            'success': True,
            'status': diff_cache.overall_status or 'pending',
            'file_path': file_path
        })

    except Exception as e:
        log_print(f"获取周版本文件状态失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': f'获取失败: {str(e)}'}), 500

@app.route('/status-sync/clear-all', methods=['POST'])
@require_admin
def clear_all_confirmation_status():
    """清空所有文件的确认状态"""
    try:
        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)

        result = sync_service.clear_all_confirmation_status()

        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 500

    except Exception as e:
        log_print(f"清空确认状态失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/status-sync/mapping-info')
def get_sync_mapping_info():
    """获取状态同步映射信息"""
    try:
        config_id = request.args.get('config_id', type=int)
        repository_id = request.args.get('repository_id', type=int)
        project_id = request.args.get('project_id', type=int)

        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)

        result = sync_service.get_sync_mapping_info(config_id, repository_id, project_id)

        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 500

    except Exception as e:
        log_print(f"获取同步映射信息失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/status-sync/management')
def status_sync_management():
    """状态同步管理页面（全局）"""
    return render_template('status_sync_management.html')

@app.route('/<project_code>/status-sync/management')
def project_status_sync_management(project_code):
    """项目特定的状态同步管理页面"""
    # 根据项目代码查找项目
    project = Project.query.filter_by(code=project_code).first()
    if not project:
        flash(f'项目 {project_code} 不存在', 'error')
        return redirect(url_for('index'))

    return render_template('status_sync_management.html', project=project)

@app.route('/status-sync/test')
def status_sync_test():
    """状态同步测试页面"""
    return render_template('status_sync_test.html')

@app.route('/status-sync/configs')
def get_sync_configs():
    """获取状态同步配置列表，支持按项目过滤"""
    try:
        project_id = request.args.get('project_id', type=int)

        query = WeeklyVersionConfig.query
        if project_id:
            query = query.filter_by(project_id=project_id)

        configs = query.order_by(WeeklyVersionConfig.created_at.desc()).all()

        config_list = []
        for config in configs:
            config_list.append({
                'id': config.id,
                'name': config.name,
                'repository_name': config.repository.name if config.repository else '未知仓库',
                'project_name': config.project.name if config.project else '未知项目'
            })

        return jsonify({'success': True, 'configs': config_list})

    except Exception as e:
        log_print(f"获取同步配置失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/weekly-version-config/<int:config_id>/batch-confirm', methods=['POST'])
def weekly_version_batch_confirm_api(config_id):
    """批量确认待确认的文件"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)

        # 获取请求数据
        data = request.get_json() or {}
        file_paths = data.get('file_paths', [])

        if file_paths:
            # 如果指定了文件路径，只确认这些文件
            log_print(f"批量确认指定的 {len(file_paths)} 个文件", 'WEEKLY')
            pending_caches = WeeklyVersionDiffCache.query.filter(
                WeeklyVersionDiffCache.config_id == config_id,
                WeeklyVersionDiffCache.overall_status == 'pending',
                WeeklyVersionDiffCache.file_path.in_(file_paths)
            ).all()
        else:
            # 如果没有指定文件路径，确认所有待确认的文件（向后兼容）
            log_print(f"批量确认所有待确认文件", 'WEEKLY')
            pending_caches = WeeklyVersionDiffCache.query.filter_by(
                config_id=config_id,
                overall_status='pending'
            ).all()

        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)

        updated_count = 0
        sync_results = []

        for cache in pending_caches:
            old_status = cache.overall_status
            confirmation_status = json.loads(cache.confirmation_status) if cache.confirmation_status else {}
            confirmation_status['dev'] = 'confirmed'

            cache.confirmation_status = json.dumps(confirmation_status)
            cache.overall_status = 'confirmed'
            cache.updated_at = datetime.now(timezone.utc)
            updated_count += 1

            log_print(f"确认文件: {cache.file_path}", 'WEEKLY')

            # 同步状态到提交记录
            if old_status != 'confirmed':
                sync_result = sync_service.sync_weekly_to_commit(config_id, cache.file_path, 'confirmed')
                sync_results.append(sync_result)

        db.session.commit()

        # 统计同步结果
        total_commits_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))

        log_print(f"批量确认完成，共确认 {updated_count} 个文件，同步更新了 {total_commits_updated} 个提交记录", 'WEEKLY')

        return jsonify({
            'success': True,
            'message': f'成功确认了 {updated_count} 个文件，同步更新了 {total_commits_updated} 个提交记录',
            'updated_count': updated_count
        })

    except Exception as e:
        db.session.rollback()
        log_print(f"批量确认失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

def generate_weekly_git_diff_html(config, diff_cache, file_path, force_recalculate=False):
    """生成周版本的真实Git diff HTML内容"""
    try:
        repository = config.repository

        # 获取基准commit和最新commit
        base_commit_id = diff_cache.base_commit_id
        latest_commit_id = diff_cache.latest_commit_id

        if not latest_commit_id:
            return "<div class='alert alert-warning'>未找到最新提交记录</div>"

        # 检查是否为Excel文件
        from services.diff_service import DiffService
        diff_service = DiffService()
        file_type = diff_service.get_file_type(file_path)

        if file_type == 'excel':
            # Excel文件使用合并diff逻辑
            return generate_weekly_excel_merged_diff_html(config, diff_cache, file_path, force_recalculate=force_recalculate)

        # 解析提交信息
        commit_authors = json.loads(diff_cache.commit_authors) if diff_cache.commit_authors else []
        commit_messages = json.loads(diff_cache.commit_messages) if diff_cache.commit_messages else []
        commit_times = json.loads(diff_cache.commit_times) if diff_cache.commit_times else []

        # 不再生成重复的版本信息头部，因为完整diff页面已经有了
        header_html = ""

        # 使用现有的Git服务获取真实的diff内容
        try:
            from services.threaded_git_service import ThreadedGitService
            git_service = ThreadedGitService(
                repository.url,
                repository.root_directory,
                repository.username,
                repository.token,
                repository
            )

            # 获取两个commit之间的diff
            if base_commit_id:
                log_print(f"获取周版本diff: {base_commit_id[:8]} -> {latest_commit_id[:8]}, 文件: {file_path}", 'WEEKLY')
                diff_result = git_service.get_commit_range_diff(base_commit_id, latest_commit_id, file_path)

                if diff_result and 'patch' in diff_result:
                    diff_content = diff_result['patch']
                    log_print(f"获取到diff内容，长度: {len(diff_content)} 字符", 'WEEKLY')
                    log_print(f"diff内容预览: {diff_content[:200]}...", 'WEEKLY')
                    # 使用现有的diff渲染函数
                    diff_html = render_git_diff_content(diff_content, file_path, base_commit_id, latest_commit_id, config, diff_cache)
                else:
                    log_print(f"未获取到diff内容，diff_result: {diff_result}", 'WEEKLY')
                    diff_html = "<div class='alert alert-warning'>文件在此期间无变更</div>"
            else:
                # 如果没有基准commit，获取最新commit的文件内容作为全新文件显示
                log_print(f"获取周版本初始文件内容: {latest_commit_id[:8]}, 文件: {file_path}", 'WEEKLY')
                file_content = git_service.get_file_content(latest_commit_id, file_path)

                if file_content:
                    # 将内容格式化为全新文件的diff格式
                    diff_html = render_new_file_content(file_content, file_path, latest_commit_id)
                else:
                    diff_html = "<div class='alert alert-warning'>无法获取文件内容</div>"

        except Exception as e:
            log_print(f"获取Git diff失败: {e}", 'ERROR', force=True)
            diff_html = f"<div class='alert alert-danger'>获取diff内容失败: {str(e)}</div>"

        # 只返回纯粹的diff内容，不包含重复的版本信息
        return diff_html

    except Exception as e:
        log_print(f"生成周版本Git diff HTML失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>生成diff内容失败: {str(e)}</div>"

def generate_weekly_excel_merged_diff_html(config, diff_cache, file_path, force_recalculate=False):
    """生成周版本Excel文件的合并diff HTML内容"""
    try:
        repository = config.repository

        # 如果强制重新计算，先检查并清理缓存
        if force_recalculate:
            log_print(f"🔄 强制重新计算周版本Excel diff: {file_path}", 'WEEKLY')
            try:
                # 清理该文件的周版本Excel缓存
                deleted_count = WeeklyVersionExcelCache.query.filter_by(
                    config_id=config.id,
                    file_path=file_path
                ).delete()
                db.session.commit()
                if deleted_count > 0:
                    log_print(f"已清理 {deleted_count} 条周版本Excel缓存: {file_path}", 'WEEKLY')
            except Exception as cache_e:
                log_print(f"清理周版本Excel缓存失败: {cache_e}", 'WEEKLY', force=True)

        # 解析提交信息
        commit_authors = json.loads(diff_cache.commit_authors) if diff_cache.commit_authors else []
        commit_messages = json.loads(diff_cache.commit_messages) if diff_cache.commit_messages else []
        commit_times = json.loads(diff_cache.commit_times) if diff_cache.commit_times else []

        # 获取所有相关的提交ID
        all_commit_ids = []
        if diff_cache.base_commit_id:
            all_commit_ids.append(diff_cache.base_commit_id)
        if diff_cache.latest_commit_id:
            all_commit_ids.append(diff_cache.latest_commit_id)

        # 从commit_messages中提取更多的提交ID（如果有的话）
        # 这里需要根据实际的数据结构来获取所有相关的提交

        log_print(f"生成Excel合并diff: {file_path}, 提交数量: {len(all_commit_ids)}", 'WEEKLY')

        # 查找该文件的所有相关提交记录
        from sqlalchemy import and_, or_
        commits = Commit.query.filter(
            and_(
                Commit.repository_id == repository.id,
                Commit.path == file_path,
                or_(
                    Commit.commit_id == diff_cache.base_commit_id,
                    Commit.commit_id == diff_cache.latest_commit_id
                )
            )
        ).order_by(Commit.commit_time.asc()).all()

        if not commits:
            return "<div class='alert alert-warning'>未找到相关的Excel提交记录</div>"

        log_print(f"找到 {len(commits)} 个相关提交", 'WEEKLY')

        # 使用现有的Excel diff处理逻辑
        if len(commits) == 1:
            # 单个提交，直接获取其Excel diff数据
            commit = commits[0]
            merged_diff_data = get_real_diff_data_for_merge(commit)
        else:
            # 多个提交，需要合并处理
            # 暂时使用第一个和最后一个提交进行对比
            first_commit = commits[0]
            last_commit = commits[-1]
            merged_diff_data = get_commit_pair_diff_internal(last_commit, first_commit)

        if not merged_diff_data or merged_diff_data.get('type') != 'excel':
            log_print(f"❌ Excel合并diff数据检查失败:", 'WEEKLY', force=True)
            log_print(f"  - merged_diff_data存在: {merged_diff_data is not None}", 'WEEKLY', force=True)
            if merged_diff_data:
                log_print(f"  - merged_diff_data类型: {merged_diff_data.get('type', 'None')}", 'WEEKLY', force=True)
                log_print(f"  - merged_diff_data键: {list(merged_diff_data.keys())}", 'WEEKLY', force=True)
                if 'error' in merged_diff_data:
                    log_print(f"  - 错误信息: {merged_diff_data.get('error')}", 'WEEKLY', force=True)
                if 'message' in merged_diff_data:
                    log_print(f"  - 消息: {merged_diff_data.get('message')}", 'WEEKLY', force=True)
            return "<div class='alert alert-warning'>无法生成Excel合并diff数据</div>"

        # 清理NaN值
        import math
        def clean_nan(obj):
            if isinstance(obj, dict):
                return {k: clean_nan(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clean_nan(item) for item in obj]
            elif isinstance(obj, float) and math.isnan(obj):
                return None
            else:
                return obj

        cleaned_diff_data = clean_nan(merged_diff_data)

        # 生成Excel diff HTML
        excel_diff_html = render_excel_diff_html(cleaned_diff_data, file_path)

        return excel_diff_html

    except Exception as e:
        log_print(f"生成周版本Excel合并diff失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>生成Excel diff失败: {str(e)}</div>"

def render_excel_diff_html(merged_diff_data, file_path):
    """渲染Excel diff数据为HTML - 完全使用合并diff的样式和结构"""
    try:
        if not merged_diff_data or not merged_diff_data.get('sheets'):
            return "<div class='alert alert-warning'>Excel文件无变更数据</div>"

        sheets = merged_diff_data['sheets']

        # 使用与合并diff完全相同的HTML结构
        excel_html = f"""
        <!-- 引入合并diff的CSS文件 -->
        <link rel="stylesheet" href="/static/css/excel-diff-new.css?v=2.0">
        <link rel="stylesheet" href="/static/css/excel-scroll-fix.css?v=2.0">

        <!-- Excel合并diff显示 - 使用与单文件diff相同的结构 -->
        <div class="excel-diff-wrapper">
            <!-- Excel工作表标签容器 -->
            <div class="excel-sheet-tabs-container">
                <div id="excel-sheet-tabs" class="excel-sheet-tabs"></div>
            </div>

            <!-- Excel表格内容容器 -->
            <div id="excel-content" class="excel-content-area">
                <div class="excel-sheet-content active">
                    <div class="excel-table-container">
                        <!-- 表格内容将通过JavaScript动态生成 -->
                    </div>
                </div>
            </div>
        </div>

        <script>
        // 存储Excel diff数据到全局变量
        window.weeklyExcelDiffData = """ + json.dumps(merged_diff_data) + """;

        // 标记数据已准备好
        window.weeklyExcelDiffDataReady = true;

        console.log('📊 Excel数据已设置到window.weeklyExcelDiffData');
        console.log('📊 数据内容:', window.weeklyExcelDiffData);
        </script>

        <!-- 将初始化逻辑移到单独的script标签，确保在DOM插入后执行 -->
        <script>
        // 通知父页面数据已准备好，可以开始初始化
        if (typeof window.initWeeklyExcelDiffWhenReady === 'function') {
            window.initWeeklyExcelDiffWhenReady();
        }
        </script>
        """

        return excel_html

    except Exception as e:
        log_print(f"渲染Excel diff HTML失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染Excel diff失败: {str(e)}</div>"

# render_excel_sheet_html函数已删除，现在使用JavaScript动态生成

def render_git_diff_content(diff_content, file_path, base_commit_id, latest_commit_id, config=None, diff_cache=None):
    """渲染Git diff内容为HTML，与现有单文件diff界面保持一致"""
    try:
        if not diff_content:
            return "<div class='alert alert-info'>文件无变更</div>"

        # 检查是否为删除文件（所有行都是删除行）
        is_deleted = is_deleted_file(diff_content)

        if is_deleted:
            # 渲染删除文件内容
            github_diff_html = render_deleted_file_content(diff_content, file_path, config, diff_cache)
        else:
            # 生成GitHub风格的diff内容
            github_diff_html = render_github_style_diff(diff_content)

        diff_html = f"""
        <div class="weekly-diff-content">
            <div class="file-diff-container">
                <div class="file-header">
                    <i class="fas fa-file-code me-2"></i>{file_path}
                </div>
                <div class="diff-content-wrapper">
                    {github_diff_html}
                </div>
            </div>
        </div>

        <style>
        .weekly-diff-content {{
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}

        .file-diff-container {{
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
        }}

        .file-header {{
            background-color: #f6f8fa;
            padding: 8px 16px;
            border-bottom: 1px solid #d0d7de;
            font-weight: 600;
            font-size: 14px;
        }}

        .diff-content-wrapper {{
            background-color: #ffffff;
            max-height: 70vh;
            overflow-y: auto;
        }}

        /* 确保diff内容使用标准字体大小 */
        .weekly-diff-content .diff-container {{
            font-size: 12px;
        }}

        .weekly-diff-content .diff-line-content {{
            font-size: 12px;
            line-height: 20px;
        }}

        .weekly-diff-content .diff-line-number {{
            font-size: 12px;
        }}

        .weekly-diff-content .text-diff-container {{
            font-size: 12px;
        }}

        .weekly-diff-content .text-diff-line {{
            font-size: 12px;
            line-height: 20px;
        }}

        .weekly-diff-content .text-diff-line-content {{
            font-size: 12px;
        }}

        .weekly-diff-content .text-diff-line-number {{
            font-size: 11px;
        }}
        </style>
        """

        return diff_html

    except Exception as e:
        log_print(f"渲染Git diff内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染diff失败: {str(e)}</div>"

def render_github_style_diff(diff_content):
    """渲染GitHub风格的diff内容"""
    try:
        if not diff_content:
            return "<div class='alert alert-info'>文件无变更</div>"

        lines = diff_content.split('\n')
        html_content = []

        old_line_num = 0
        new_line_num = 0

        for line in lines:
            if line.startswith('@@'):
                # 解析hunk头部信息
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                    new_line_num = int(match.group(2)) - 1

                # 渲染hunk头部
                html_content.append(f"""
                    <tr class="diff-line diff-hunk-header">
                        <td class="diff-line-number diff-line-number-old"></td>
                        <td class="diff-line-number diff-line-number-new"></td>
                        <td class="diff-line-content">{line}</td>
                    </tr>
                """)
            elif line.startswith('-'):
                old_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_content.append(f"""
                    <tr class="diff-line diff-line-removed">
                        <td class="diff-line-number diff-line-number-old">{old_line_num}</td>
                        <td class="diff-line-number diff-line-number-new"></td>
                        <td class="diff-line-content">-{line_content}</td>
                    </tr>
                """)
            elif line.startswith('+'):
                new_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_content.append(f"""
                    <tr class="diff-line diff-line-added">
                        <td class="diff-line-number diff-line-number-old"></td>
                        <td class="diff-line-number diff-line-number-new">{new_line_num}</td>
                        <td class="diff-line-content">+{line_content}</td>
                    </tr>
                """)
            elif line.startswith(' ') or (not line.startswith(('@@', '+', '-', '\\'))):
                old_line_num += 1
                new_line_num += 1
                line_content = line[1:] if line.startswith(' ') else line
                line_content = line_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_content.append(f"""
                    <tr class="diff-line">
                        <td class="diff-line-number diff-line-number-old">{old_line_num}</td>
                        <td class="diff-line-number diff-line-number-new">{new_line_num}</td>
                        <td class="diff-line-content"> {line_content}</td>
                    </tr>
                """)

        return f"""
        <div class="diff-container">
            <table class="diff-table">
                <tbody>
                    {''.join(html_content)}
                </tbody>
            </table>
        </div>

        <style>
        .diff-container {{
            background: #fff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}

        .diff-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}

        .diff-line-number {{
            width: 1%;
            min-width: 50px;
            padding: 0 8px;
            text-align: right;
            vertical-align: top;
            color: #656d76;
            background: #f6f8fa;
            border-right: 1px solid #d0d7de;
            user-select: none;
            font-size: 12px;
            line-height: 20px;
        }}

        .diff-line-content {{
            padding: 0 8px;
            vertical-align: top;
            white-space: pre;
            word-wrap: break-word;
            overflow-wrap: break-word;
            line-height: 20px;
            font-size: 12px;
        }}

        .diff-line-added {{
            background-color: #dafbe1 !important;
        }}

        .diff-line-added .diff-line-number {{
            background-color: #ccf2d4 !important;
            color: #24292f !important;
        }}

        .diff-line-removed {{
            background-color: #ffebe9 !important;
        }}

        .diff-line-removed .diff-line-number {{
            background-color: #ffd7d5 !important;
            color: #24292f !important;
        }}

        .diff-hunk-header {{
            background-color: #f1f8ff !important;
        }}

        .diff-hunk-header .diff-line-content {{
            color: #0969da;
            font-weight: 600;
        }}
        </style>
        """

    except Exception as e:
        log_print(f"渲染GitHub风格diff失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染GitHub风格diff失败: {str(e)}</div>"

def is_deleted_file(diff_content):
    """检查是否为删除文件（所有内容行都是删除行）"""
    if not diff_content:
        return False

    lines = diff_content.split('\n')
    content_lines = []

    for line in lines:
        # 跳过hunk头部和其他元数据
        if line.startswith('@@') or line.startswith('\\') or not line.strip():
            continue
        # 收集内容行
        if line.startswith('+') or line.startswith('-') or line.startswith(' '):
            content_lines.append(line)

    # 如果没有内容行，不是删除文件
    if not content_lines:
        return False

    # 检查是否所有内容行都是删除行
    non_deleted_lines = []
    for line in content_lines:
        if not line.startswith('-'):
            non_deleted_lines.append(line)

    return len(non_deleted_lines) == 0

def render_deleted_file_content(diff_content, file_path, config=None, diff_cache=None):
    """渲染删除文件提示为HTML，显示文件已删除的信息"""
    try:
        # 解析diff内容获取基本信息
        lines = diff_content.split('\n') if diff_content else []
        deleted_lines_count = 0

        # 统计删除的行数
        for line in lines:
            if line.startswith('-') and not line.startswith('---'):
                deleted_lines_count += 1

        # 构建查看上一版本的URL
        previous_version_url = ""
        if config and diff_cache and diff_cache.base_commit_id:
            # 构建查看基准版本文件的URL
            previous_version_url = f"/weekly-version-config/{config.id}/file-previous-version?file_path={file_path}&commit_id={diff_cache.base_commit_id}"

        # 获取文件扩展名用于显示合适的图标
        file_extension = file_path.split('.')[-1].lower() if '.' in file_path else ''
        file_icon = get_file_icon(file_extension)

        # 获取删除的内容预览（前几行）
        deleted_content_preview = []
        for line in lines:
            if line.startswith('-') and not line.startswith('---'):
                deleted_content_preview.append(line[1:])  # 去掉'-'前缀
                if len(deleted_content_preview) >= 15:  # 显示前15行
                    break

        return f"""
        <div class="deleted-file-container">
            <!-- 主要删除提示区域 -->
            <div class="deleted-file-main">
                <div class="deleted-file-icon-wrapper">
                    <div class="deleted-file-icon">
                        <i class="fas fa-trash-alt"></i>
                    </div>
                    <div class="file-type-icon">
                        <i class="{file_icon}"></i>
                    </div>
                </div>

                <div class="deleted-file-info">
                    <h3 class="deleted-title">文件已删除</h3>
                    <p class="deleted-subtitle">该文件在此版本中被完全删除</p>
                    <div class="deleted-stats">
                        <div class="stat-item">
                            <i class="fas fa-file-alt me-2"></i>
                            <span class="stat-label">文件名：</span>
                            <code class="stat-value">{file_path.split('/')[-1]}</code>
                        </div>
                        <div class="stat-item">
                            <i class="fas fa-minus-circle me-2"></i>
                            <span class="stat-label">删除行数：</span>
                            <span class="stat-value text-danger">{deleted_lines_count} 行</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 操作按钮区域 -->
            <div class="deleted-file-actions">
                <div class="action-buttons">
                    {f'''
                    <a href="{previous_version_url}" class="btn btn-primary btn-lg" target="_blank">
                        <i class="fas fa-history me-2"></i>查看上一版本
                    </a>
                    ''' if previous_version_url else '''
                    <button type="button" class="btn btn-primary btn-lg" disabled title="无法获取上一版本信息">
                        <i class="fas fa-history me-2"></i>查看上一版本
                    </button>
                    '''}
                    <button type="button" class="btn btn-outline-secondary btn-lg" onclick="showDeletedContent()">
                        <i class="fas fa-eye me-2"></i>显示删除内容
                    </button>
                </div>

                <div class="action-hint">
                    <i class="fas fa-info-circle me-2"></i>
                    点击"查看上一版本"可以查看删除前的完整文件内容
                </div>
            </div>

            <!-- 删除内容详情（默认隐藏） -->
            <div id="deletedContentDetails" style="display: none;" class="deleted-content-details">
                <div class="content-header">
                    <h5><i class="fas fa-code me-2"></i>删除的内容预览</h5>
                    <small class="text-muted">显示前 {min(len(deleted_content_preview), 15)} 行删除的内容</small>
                </div>
                <div class="deleted-content-preview">
                    <div class="code-container">
                        {''.join([f'<div class="code-line deleted-line"><div class="line-number">{i+1}</div><div class="line-text">{line if line.strip() else " "}</div></div>' for i, line in enumerate(deleted_content_preview)])}
                    </div>
                    {f'<div class="more-content-hint"><i class="fas fa-ellipsis-h me-2"></i>还有 {deleted_lines_count - len(deleted_content_preview)} 行内容被删除</div>' if deleted_lines_count > len(deleted_content_preview) else ''}
                </div>
            </div>
        </div>

        <style>
        .deleted-file-container {{
            background: linear-gradient(135deg, #fff9e6 0%, #fef7e0 100%);
            border: 1px solid #f0d000;
            border-radius: 8px;
            padding: 0;
            margin: 15px 0;
            box-shadow: 0 2px 8px rgba(240, 208, 0, 0.1);
            overflow: hidden;
            width: 100%;
        }}

        .deleted-file-main {{
            padding: 20px 15px;
            text-align: center;
            border-bottom: 1px solid rgba(240, 208, 0, 0.3);
        }}

        .deleted-file-icon-wrapper {{
            position: relative;
            display: inline-block;
            margin-bottom: 15px;
        }}

        .deleted-file-icon {{
            font-size: 1.5rem;
            color: #dc3545;
            margin-bottom: 8px;
            animation: pulse 2s infinite;
        }}

        .file-type-icon {{
            position: absolute;
            bottom: -3px;
            right: -6px;
            font-size: 0.72rem;
            color: #6c757d;
            background: white;
            border-radius: 50%;
            padding: 4px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.1);
        }}

        @keyframes pulse {{
            0% {{ transform: scale(1); }}
            50% {{ transform: scale(1.05); }}
            100% {{ transform: scale(1); }}
        }}

        .deleted-file-info {{
            max-width: 500px;
            margin: 0 auto;
        }}

        .deleted-title {{
            color: #dc3545;
            font-weight: 700;
            font-size: 1.4rem;
            margin-bottom: 8px;
        }}

        .deleted-subtitle {{
            color: #6c757d;
            font-size: 0.95rem;
            margin-bottom: 15px;
        }}

        .deleted-stats {{
            display: flex;
            justify-content: center;
            gap: 15px;
            flex-wrap: wrap;
        }}

        .stat-item {{
            display: flex;
            align-items: center;
            background: rgba(255, 255, 255, 0.7);
            padding: 8px 12px;
            border-radius: 6px;
            border: 1px solid rgba(240, 208, 0, 0.3);
            font-size: 0.9rem;
        }}

        .stat-item i {{
            color: #f0d000;
        }}

        .stat-label {{
            font-weight: 600;
            color: #495057;
            margin-right: 8px;
        }}

        .stat-value {{
            font-weight: 700;
        }}

        .deleted-file-actions {{
            padding: 15px 20px;
            text-align: center;
            background: rgba(255, 255, 255, 0.5);
        }}

        .action-buttons {{
            margin-bottom: 15px;
        }}

        .action-buttons .btn {{
            margin: 0 8px;
            padding: 8px 16px;
            font-weight: 600;
            border-radius: 6px;
            transition: all 0.3s ease;
            font-size: 0.9rem;
        }}

        .action-buttons .btn:hover:not(:disabled) {{
            transform: translateY(-1px);
            box-shadow: 0 3px 8px rgba(0,0,0,0.15);
        }}

        .action-hint {{
            color: #6c757d;
            font-size: 0.9rem;
            font-style: italic;
        }}

        .deleted-content-details {{
            background: rgba(255, 255, 255, 0.9);
            border-top: 1px solid rgba(240, 208, 0, 0.3);
            padding: 25px 30px;
        }}

        .content-header {{
            margin-bottom: 15px;
            text-align: left;
        }}

        .content-header h5 {{
            color: #495057;
            margin-bottom: 5px;
        }}

        .deleted-content-preview {{
            background: #fff;
            border: 1px solid #dee2e6;
            border-radius: 6px;
            padding: 0;
            max-height: 400px;
            overflow-y: auto;
            text-align: left;
        }}

        .deleted-content-preview .code-container {{
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 13px;
            line-height: 18.2px;
        }}

        .deleted-content-preview .code-line {{
            display: flex;
            align-items: stretch;
            min-height: 18.2px;
            background: #ffeef0;
            border-left: 3px solid #dc3545;
        }}

        .deleted-content-preview .code-line:hover {{
            background: #ffdddf;
        }}

        .deleted-content-preview .line-number {{
            background: #f8f9fa;
            color: #6c757d;
            padding: 0 8px;
            text-align: right;
            min-width: 40px;
            border-right: 1px solid #dee2e6;
            user-select: none;
            flex-shrink: 0;
        }}

        .deleted-content-preview .line-text {{
            padding: 0 8px;
            flex: 1;
            white-space: pre;
            color: #dc3545;
            overflow-x: auto;
        }}

        .more-content-hint {{
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid #dee2e6;
            color: #6c757d;
            font-style: italic;
            text-align: center;
        }}

        /* 响应式设计 */
        @media (max-width: 768px) {{
            .deleted-file-main {{
                padding: 30px 20px;
            }}

            .deleted-stats {{
                flex-direction: column;
                gap: 15px;
            }}

            .action-buttons .btn {{
                display: block;
                width: 100%;
                margin: 5px 0;
            }}
        }}
        </style>


        """

    except Exception as e:
        log_print(f"渲染删除文件内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染删除文件内容失败: {str(e)}</div>"

def get_file_icon(file_extension):
    """根据文件扩展名返回合适的图标"""
    icon_map = {
        'lua': 'fas fa-code',
        'py': 'fab fa-python',
        'js': 'fab fa-js-square',
        'html': 'fab fa-html5',
        'css': 'fab fa-css3-alt',
        'json': 'fas fa-brackets-curly',
        'xml': 'fas fa-code',
        'txt': 'fas fa-file-alt',
        'md': 'fab fa-markdown',
        'yml': 'fas fa-cog',
        'yaml': 'fas fa-cog',
        'sql': 'fas fa-database',
        'sh': 'fas fa-terminal',
        'bat': 'fas fa-terminal',
        'exe': 'fas fa-cog',
        'dll': 'fas fa-cog',
        'png': 'fas fa-image',
        'jpg': 'fas fa-image',
        'jpeg': 'fas fa-image',
        'gif': 'fas fa-image',
        'pdf': 'fas fa-file-pdf',
        'doc': 'fas fa-file-word',
        'docx': 'fas fa-file-word',
        'xls': 'fas fa-file-excel',
        'xlsx': 'fas fa-file-excel',
    }
    return icon_map.get(file_extension, 'fas fa-file')

def render_deleted_content_details(diff_content):
    """渲染删除文件的详细内容，用于在点击时显示"""
    try:
        if not diff_content:
            return "<div class='alert alert-info'>无删除内容</div>"

        lines = diff_content.split('\n')
        html_content = []
        old_line_num = 0

        for line in lines:
            if line.startswith('@@'):
                # 解析hunk头部信息
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1

                # 渲染hunk头部
                html_content.append(f"""
                    <tr class="diff-line diff-hunk-header">
                        <td class="diff-line-number diff-line-number-old"></td>
                        <td class="diff-line-number diff-line-number-new"></td>
                        <td class="diff-line-content">{line}</td>
                    </tr>
                """)
            elif line.startswith('-'):
                old_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_content.append(f"""
                    <tr class="diff-line diff-line-removed">
                        <td class="diff-line-number diff-line-number-old">{old_line_num}</td>
                        <td class="diff-line-number diff-line-number-new"></td>
                        <td class="diff-line-content">-{line_content}</td>
                    </tr>
                """)

        return f"""
        <div class="diff-container">
            <table class="diff-table">
                <tbody>
                    {''.join(html_content)}
                </tbody>
            </table>
        </div>

        <style>
        .diff-container {{
            background: #fff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}

        .diff-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}

        .diff-line-number {{
            width: 1%;
            min-width: 50px;
            padding: 0 8px;
            text-align: right;
            vertical-align: top;
            color: #656d76;
            background: #f6f8fa;
            border-right: 1px solid #d0d7de;
            user-select: none;
            font-size: 12px;
            line-height: 20px;
        }}

        .diff-line-content {{
            padding: 0 8px;
            vertical-align: top;
            white-space: pre;
            word-wrap: break-word;
            overflow-wrap: break-word;
            line-height: 20px;
            font-size: 12px;
        }}

        .diff-line-removed {{
            background-color: #ffebe9 !important;
        }}

        .diff-line-removed .diff-line-number {{
            background-color: #ffd7d5 !important;
            color: #24292f !important;
        }}

        .diff-hunk-header {{
            background-color: #f1f8ff !important;
        }}

        .diff-hunk-header .diff-line-content {{
            color: #0969da;
            font-weight: 600;
        }}
        </style>
        """

    except Exception as e:
        return f"<div class='alert alert-danger'>渲染删除内容详情失败: {str(e)}</div>"

def render_new_file_content(file_content, file_path, commit_id):
    """渲染新文件内容为HTML，使用GitHub风格"""
    try:
        if not file_content:
            return "<div class='alert alert-info'>文件为空</div>"

        lines = file_content.split('\n')
        html_content = []

        for i, line in enumerate(lines, 1):
            # HTML转义
            line_content = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            html_content.append(f"""
                <tr class="diff-line diff-line-added">
                    <td class="diff-line-number diff-line-number-old"></td>
                    <td class="diff-line-number diff-line-number-new">{i}</td>
                    <td class="diff-line-content">{line_content}</td>
                </tr>
            """)

        diff_html = f"""
        <div class="weekly-diff-content">
            <div class="file-diff-container">
                <div class="file-header">
                    <i class="fas fa-file-plus me-2"></i>{file_path}
                </div>
                <div class="diff-content-wrapper">
                    <div class="diff-container">
                        <table class="diff-table">
                            <tbody>
                                {''.join(html_content)}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <style>
        .weekly-diff-content {{
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}

        .file-diff-container {{
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
        }}

        .file-header {{
            background-color: #f6f8fa;
            padding: 8px 16px;
            border-bottom: 1px solid #d0d7de;
            font-weight: 600;
            font-size: 14px;
        }}

        .diff-content-wrapper {{
            background-color: #ffffff;
            max-height: 70vh;
            overflow-y: auto;
        }}

        .diff-container {{
            background: #fff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}

        .diff-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}

        .diff-line-number {{
            width: 1%;
            min-width: 50px;
            padding: 0 8px;
            text-align: right;
            vertical-align: top;
            color: #656d76;
            background: #f6f8fa;
            border-right: 1px solid #d0d7de;
            user-select: none;
            font-size: 12px;
            line-height: 20px;
        }}

        .diff-line-content {{
            padding: 0 8px;
            vertical-align: top;
            white-space: pre;
            word-wrap: break-word;
            overflow-wrap: break-word;
            line-height: 20px;
            font-size: 12px;
        }}

        .diff-line-added {{
            background-color: #dafbe1 !important;
        }}

        .diff-line-added .diff-line-number {{
            background-color: #ccf2d4 !important;
            color: #24292f !important;
        }}

        .diff-line-removed {{
            background-color: #ffebe9 !important;
        }}

        .diff-line-removed .diff-line-number {{
            background-color: #ffd7d5 !important;
            color: #24292f !important;
        }}
        </style>
        """

        return diff_html

    except Exception as e:
        log_print(f"渲染新文件内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染失败: {str(e)}</div>"

def parse_and_render_diff(diff_content):
    """解析并渲染diff内容"""
    try:
        lines = diff_content.split('\n')
        html_lines = []

        old_line_num = 0
        new_line_num = 0

        for line in lines:
            if line.startswith('@@'):
                # 解析hunk头部信息
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                    new_line_num = int(match.group(2)) - 1

                html_lines.append(f"""
                    <div class="diff-line diff-hunk-header">
                        <span class="line-number"></span>
                        <span class="line-number"></span>
                        <span class="line-content">{line}</span>
                    </div>
                """)
            elif line.startswith('-'):
                old_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_lines.append(f"""
                    <div class="diff-line diff-removed">
                        <span class="line-number">{old_line_num}</span>
                        <span class="line-number"></span>
                        <span class="line-content">-{line_content}</span>
                    </div>
                """)
            elif line.startswith('+'):
                new_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_lines.append(f"""
                    <div class="diff-line diff-added">
                        <span class="line-number"></span>
                        <span class="line-number">{new_line_num}</span>
                        <span class="line-content">+{line_content}</span>
                    </div>
                """)
            elif line.startswith(' ') or (not line.startswith(('@@', '+', '-', '\\'))):
                old_line_num += 1
                new_line_num += 1
                line_content = line[1:] if line.startswith(' ') else line
                line_content = line_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_lines.append(f"""
                    <div class="diff-line diff-context">
                        <span class="line-number">{old_line_num}</span>
                        <span class="line-number">{new_line_num}</span>
                        <span class="line-content"> {line_content}</span>
                    </div>
                """)

        return f"""
        <div class="diff-content">
            {''.join(html_lines)}
        </div>

        <style>
        .diff-content {{
            font-size: 13px;
        }}

        .diff-line {{
            display: flex;
            line-height: 20px;
            min-height: 20px;
        }}

        .diff-line:hover {{
            background-color: rgba(255, 255, 0, 0.1);
        }}

        .line-number {{
            background-color: #f6f8fa;
            color: #656d76;
            padding: 0 8px;
            text-align: right;
            min-width: 50px;
            border-right: 1px solid #d1d9e0;
            user-select: none;
            font-size: 12px;
        }}

        .line-content {{
            padding: 0 8px;
            flex: 1;
            white-space: pre;
        }}

        .diff-added {{
            background-color: #e6ffed;
        }}

        .diff-removed {{
            background-color: #ffeef0;
        }}

        .diff-context {{
            background-color: #ffffff;
        }}

        .diff-hunk-header {{
            background-color: #f1f8ff;
            color: #0366d6;
            font-weight: bold;
        }}
        </style>
        """

    except Exception as e:
        log_print(f"解析diff内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>解析diff失败: {str(e)}</div>"

@app.route('/weekly-version-config/<int:config_id>/stats')
def weekly_version_stats_api(config_id):
    """获取周版本配置统计信息"""
    try:
        config = WeeklyVersionConfig.query.get_or_404(config_id)

        # 统计各状态的文件数量
        total_files = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).count()
        pending_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, overall_status='pending').count()
        confirmed_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, overall_status='confirmed').count()
        rejected_count = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, overall_status='rejected').count()

        return jsonify({
            'success': True,
            'stats': {
                'total_files': total_files,
                'pending_count': pending_count,
                'confirmed_count': confirmed_count,
                'rejected_count': rejected_count
            }
        })

    except Exception as e:
        log_print(f"获取周版本统计信息失败: {e}", 'ERROR', force=True)
        return jsonify({'success': False, 'message': str(e)}), 500

def get_file_content_at_commit(repository, commit_id, file_path):
    """获取指定commit的文件内容"""
    try:
        git_service = GitService(
            repo_url=repository.url,
            root_directory=repository.root_directory,
            username=repository.username,
            token=repository.token,
            repository=repository
        )
        return git_service.get_file_content(commit_id, file_path)
    except Exception as e:
        log_print(f"获取文件内容失败: {e}", 'ERROR')
        return ""

def get_status_text(status):
    """获取状态文本"""
    status_map = {
        'pending': '待确认',
        'confirmed': '已确认',
        'rejected': '已拒绝'
    }
    return status_map.get(status, '未知')

def get_status_badge_class(status):
    """获取状态徽章样式类"""
    class_map = {
        'pending': 'warning',
        'confirmed': 'success',
        'rejected': 'danger'
    }
    return class_map.get(status, 'secondary')

def create_weekly_sync_task(config_id):
    """为周版本配置创建同步任务"""
    try:
        # 检查是否已存在该配置的同步任务
        existing_task = BackgroundTask.query.filter_by(
            task_type='weekly_sync',
            commit_id=str(config_id),  # 使用commit_id字段存储config_id
            status='pending'
        ).first()

        if existing_task:
            log_print(f"周版本配置 {config_id} 已存在待处理的同步任务", 'SYNC')
            return existing_task.id

        # 创建新的同步任务
        new_task = BackgroundTask(
            task_type='weekly_sync',
            repository_id=None,  # 周版本任务不绑定特定仓库
            commit_id=str(config_id),  # 使用commit_id字段存储config_id
            priority=3,  # 高优先级
            status='pending'
        )

        db.session.add(new_task)
        db.session.commit()

        # 添加到内存队列
        task_data = {
            'type': 'weekly_sync',
            'config_id': config_id,
            'task_id': new_task.id
        }
        import time
        task_counter = int(time.time() * 1000000)
        task_wrapper = TaskWrapper(3, task_counter, task_data)
        background_task_queue.put(task_wrapper)

        log_print(f"创建周版本同步任务: config_id={config_id}, task_id={new_task.id}", 'SYNC')
        return new_task.id

    except Exception as e:
        db.session.rollback()
        log_print(f"创建周版本同步任务失败: {e}", 'ERROR', force=True)
        return None

def process_weekly_version_sync(config_id):
    """处理周版本同步任务"""
    try:
        config = WeeklyVersionConfig.query.get(config_id)
        if not config:
            log_print(f"周版本配置不存在: {config_id}", 'WEEKLY', force=True)
            return

        if not config.is_active:
            log_print(f"周版本配置已禁用: {config_id}", 'WEEKLY')
            return

        repository = config.repository
        log_print(f"开始处理周版本同步: {config.name} (仓库: {repository.name})", 'WEEKLY')

        # 获取时间范围内的提交记录
        commits_in_range = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.commit_time >= config.start_time,
            Commit.commit_time <= config.end_time
        ).order_by(Commit.commit_time.asc()).all()

        log_print(f"找到 {len(commits_in_range)} 个时间范围内的提交", 'WEEKLY')

        if not commits_in_range:
            log_print(f"时间范围内无提交记录，跳过同步", 'WEEKLY')
            return

        # 按文件路径分组提交
        files_commits = {}
        for commit in commits_in_range:
            if commit.path not in files_commits:
                files_commits[commit.path] = []
            files_commits[commit.path].append(commit)

        log_print(f"涉及 {len(files_commits)} 个文件", 'WEEKLY')

        # 为每个文件生成合并diff缓存
        for file_path, file_commits in files_commits.items():
            try:
                generate_weekly_merged_diff(config, file_path, file_commits)
            except Exception as e:
                log_print(f"生成文件 {file_path} 的合并diff失败: {e}", 'WEEKLY', force=True)
                continue

        log_print(f"周版本同步完成: {config.name}", 'WEEKLY')

        # 记录到操作日志
        weekly_excel_cache_service.log_cache_operation(f"✅ 周版本同步完成: {config.name} - 处理了 {len(files_commits)} 个文件", 'success', repository_id=config.repository_id, config_id=config.id)

    except Exception as e:
        log_print(f"周版本同步处理失败: {e}", 'WEEKLY', force=True)
        raise e

def generate_weekly_merged_diff(config, file_path, commits):
    """为单个文件生成周版本合并diff"""
    try:
        if not commits:
            return

        repository = config.repository

        # 获取基准版本（时间范围开始前的最后一个提交）
        base_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == file_path,
            Commit.commit_time < config.start_time
        ).order_by(Commit.commit_time.desc()).first()

        # 优化策略：如果数据库中没有找到基准版本，直接查询Git/SVN获取真实的提交历史
        if not base_commit:
            log_print(f"🔍 数据库中未找到基准版本，查询Git/SVN获取 {file_path} 的完整提交历史", 'WEEKLY', force=True)
            base_commit = get_real_base_commit_from_vcs(config, file_path)
            if base_commit:
                log_print(f"✅ 从Git/SVN获取到真实基准版本: {base_commit.commit_id[:8]} ({base_commit.commit_time})", 'WEEKLY', force=True)
            else:
                log_print(f"ℹ️ Git/SVN中也未找到更早的提交，确认为新文件", 'WEEKLY', force=True)

        # 获取最新版本（时间范围内的最后一个提交）
        latest_commit = commits[-1]

        # 检查是否已存在缓存
        existing_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config.id,
            file_path=file_path
        ).first()

        # 准备提交信息
        commit_authors = [commit.author for commit in commits]
        commit_messages = [commit.message.strip() for commit in commits]
        commit_times = [commit.commit_time.isoformat() for commit in commits]

        # 生成合并diff数据
        merged_diff_data = generate_merged_diff_data(
            repository, file_path, base_commit, latest_commit, commits
        )

        if existing_cache:
            # 更新现有缓存
            existing_cache.merged_diff_data = json.dumps(merged_diff_data)
            existing_cache.base_commit_id = base_commit.commit_id if base_commit else None
            existing_cache.latest_commit_id = latest_commit.commit_id
            existing_cache.commit_authors = json.dumps(commit_authors)
            existing_cache.commit_messages = json.dumps(commit_messages)
            existing_cache.commit_times = json.dumps(commit_times)
            existing_cache.commit_count = len(commits)
            existing_cache.cache_status = 'completed'
            existing_cache.last_sync_time = datetime.now(timezone.utc)
            existing_cache.updated_at = datetime.now(timezone.utc)

            # 如果有新的提交，重置确认状态
            if existing_cache.latest_commit_id != latest_commit.commit_id:
                existing_cache.confirmation_status = json.dumps({"dev": "pending"})
                existing_cache.overall_status = 'pending'

            log_print(f"更新周版本diff缓存: {file_path}", 'WEEKLY')
        else:
            # 创建新缓存
            new_cache = WeeklyVersionDiffCache(
                config_id=config.id,
                repository_id=repository.id,
                file_path=file_path,
                merged_diff_data=json.dumps(merged_diff_data),
                base_commit_id=base_commit.commit_id if base_commit else None,
                latest_commit_id=latest_commit.commit_id,
                commit_authors=json.dumps(commit_authors),
                commit_messages=json.dumps(commit_messages),
                commit_times=json.dumps(commit_times),
                commit_count=len(commits),
                confirmation_status=json.dumps({"dev": "pending"}),
                overall_status='pending',
                cache_status='completed',
                last_sync_time=datetime.now(timezone.utc)
            )

            db.session.add(new_cache)
            log_print(f"创建周版本diff缓存: {file_path}", 'WEEKLY')

            # 如果基准版本为空，应用优化策略
            if not base_commit:
                log_print(f"🔄 应用基准版本优化策略: {file_path}", 'WEEKLY')
                db.session.commit()  # 先提交新缓存

                # 尝试从Git/SVN获取真实基准版本
                real_base_commit = get_real_base_commit_from_vcs(config, file_path)
                if real_base_commit:
                    new_cache.base_commit_id = real_base_commit.commit_id
                    log_print(f"✅ 基准版本优化成功: {file_path} -> {real_base_commit.commit_id[:8]}", 'WEEKLY')

        db.session.commit()

        # 检查是否需要生成Excel合并diff缓存
        if weekly_excel_cache_service.needs_merged_diff_cache(config.id, file_path):
            log_print(f"触发Excel合并diff缓存生成: {file_path}", 'WEEKLY')
            try:
                # 异步生成Excel HTML缓存
                create_weekly_excel_cache_task(config.id, file_path)
                log_print(f"✅ Excel缓存任务创建成功: {file_path}", 'WEEKLY')
            except Exception as cache_e:
                log_print(f"创建Excel缓存任务失败: {cache_e}", 'WEEKLY', force=True)
        else:
            log_print(f"跳过Excel缓存生成: {file_path} (不是Excel文件或不需要缓存)", 'WEEKLY')

    except Exception as e:
        db.session.rollback()
        log_print(f"生成周版本合并diff失败: {file_path}, 错误: {e}", 'WEEKLY', force=True)
        raise e

def process_weekly_excel_cache(config_id, file_path):
    """处理周版本Excel缓存生成"""
    try:
        start_time = time.time()
        log_print(f"开始生成周版本Excel缓存: 配置 {config_id}, 文件 {file_path}", 'WEEKLY')

        # 获取配置和diff缓存
        config = WeeklyVersionConfig.query.get(config_id)
        if not config:
            raise Exception(f"周版本配置不存在: {config_id}")

        diff_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config_id,
            file_path=file_path
        ).first()

        if not diff_cache:
            raise Exception(f"周版本diff缓存不存在: {file_path}")

        # 检查是否已存在缓存
        existing_cache = weekly_excel_cache_service.get_cached_html(
            config_id, file_path,
            diff_cache.base_commit_id or '',
            diff_cache.latest_commit_id
        )

        if existing_cache:
            log_print(f"周版本Excel缓存已存在，跳过生成: {file_path}", 'WEEKLY')
            return

        # 生成Excel合并diff HTML
        html_content = generate_weekly_excel_merged_diff_html(config, diff_cache, file_path)

        if not html_content:
            raise Exception("生成Excel合并diff HTML失败")

        # 保存到缓存
        processing_time = time.time() - start_time
        success = weekly_excel_cache_service.save_html_cache(
            config_id=config_id,
            repository_id=config.repository_id,
            file_path=file_path,
            base_commit_id=diff_cache.base_commit_id or '',
            latest_commit_id=diff_cache.latest_commit_id,
            commit_count=diff_cache.commit_count,
            html_content=html_content,
            css_content="",  # CSS已包含在HTML中
            js_content="",   # JS已包含在HTML中
            metadata={
                'file_type': 'excel',
                'commit_count': diff_cache.commit_count,
                'generated_at': datetime.now(timezone.utc).isoformat()
            },
            processing_time=processing_time
        )

        if success:
            log_print(f"✅ 周版本Excel缓存生成完成: {file_path}, 耗时: {processing_time:.2f}秒", 'WEEKLY')
            # 记录到操作日志
            weekly_excel_cache_service.log_cache_operation(f"✅ 周版本Excel缓存生成成功: {file_path} (耗时: {processing_time:.2f}秒)", 'success', repository_id=config.repository_id, config_id=config_id, file_path=file_path)
        else:
            raise Exception("保存缓存失败")

    except Exception as e:
        log_print(f"❌ 周版本Excel缓存生成失败: {file_path}, 错误: {e}", 'WEEKLY', force=True)
        # 记录到操作日志
        weekly_excel_cache_service.log_cache_operation(f"❌ 周版本Excel缓存生成失败: {file_path} - {str(e)}", 'error', config_id=config_id, file_path=file_path)
        raise e

def create_weekly_excel_cache_task(config_id, file_path):
    """创建周版本Excel缓存任务"""
    log_print(f"📝 开始创建周版本Excel缓存任务: config_id={config_id}, file_path={file_path}", 'WEEKLY', force=True)

    try:
        # 创建后台任务来生成Excel HTML缓存
        # 使用repository_id字段存储config_id
        log_print(f"🗃️ 创建数据库任务记录...", 'WEEKLY', force=True)
        new_task = BackgroundTask(
            task_type='weekly_excel_cache',
            repository_id=config_id,  # 存储config_id
            file_path=file_path,
            status='pending',
            priority=5  # 中等优先级
        )

        db.session.add(new_task)
        db.session.commit()
        log_print(f"✅ 数据库任务记录创建成功，任务ID: {new_task.id}", 'WEEKLY', force=True)

        # 添加到任务队列
        import time
        task_counter = int(time.time() * 1000000)  # 微秒级时间戳作为计数器
        log_print(f"📋 添加任务到队列，计数器: {task_counter}", 'WEEKLY', force=True)

        task_wrapper = TaskWrapper(
            5,  # 中等优先级
            task_counter,
            {
                'id': new_task.id,
                'type': 'weekly_excel_cache',
                'data': {
                    'config_id': config_id,
                    'file_path': file_path
                }
            }
        )
        background_task_queue.put(task_wrapper)
        log_print(f"✅ 任务已添加到队列，当前队列大小: {background_task_queue.qsize()}", 'WEEKLY', force=True)

        log_print(f"🎉 周版本Excel缓存任务创建完成: {file_path}", 'WEEKLY', force=True)

    except Exception as e:
        log_print(f"❌ 创建周版本Excel缓存任务失败: {e}", 'WEEKLY', force=True)
        log_print(f"错误详情: {type(e).__name__}: {str(e)}", 'WEEKLY', force=True)
        db.session.rollback()
        raise e

def get_real_base_commit_from_vcs(config, file_path):
    """从Git/SVN获取文件的真实基准版本提交"""
    try:
        repository = config.repository

        # 根据仓库类型选择相应的服务
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            vcs_service = ThreadedGitService(
                repository.url,
                repository.root_directory,
                repository.username,
                repository.token,
                repository
            )
        elif repository.type == 'svn':
            vcs_service = get_svn_service(repository)
        else:
            log_print(f"不支持的仓库类型: {repository.type}", 'WEEKLY', force=True)
            return None

        # 获取文件的完整提交历史
        log_print(f"🔍 从{repository.type.upper()}获取文件提交历史: {file_path}", 'WEEKLY')

        if repository.type == 'git':
            # Git: 获取文件的提交历史
            commits_data = vcs_service.get_file_commit_history(file_path, limit=100)
        else:
            # SVN: 获取文件的提交历史
            commits_data = vcs_service.get_file_history(file_path, limit=100)

        if not commits_data:
            log_print(f"📭 {repository.type.upper()}中未找到文件 {file_path} 的提交历史", 'WEEKLY')
            return None

        # 查找周版本开始时间之前的最后一个提交
        from datetime import timezone
        base_commit_data = None
        for commit_data in commits_data:
            commit_time = commit_data.get('commit_time')
            if commit_time:
                # 确保时间比较的时区一致性
                if commit_time.tzinfo is None:
                    # 如果commit_time没有时区信息，假设为UTC
                    commit_time = commit_time.replace(tzinfo=timezone.utc)

                config_start_time = config.start_time
                if config_start_time.tzinfo is None:
                    # 如果config.start_time没有时区信息，假设为UTC
                    config_start_time = config_start_time.replace(tzinfo=timezone.utc)

                if commit_time < config_start_time:
                    base_commit_data = commit_data
                    break

        if not base_commit_data:
            log_print(f"📭 {repository.type.upper()}中未找到周版本开始前的提交", 'WEEKLY')
            return None

        # 检查数据库中是否已存在这个提交记录
        existing_commit = Commit.query.filter_by(
            repository_id=repository.id,
            commit_id=base_commit_data['commit_id'],
            path=file_path
        ).first()

        if existing_commit:
            log_print(f"✅ 数据库中已存在基准提交: {existing_commit.commit_id[:8]}", 'WEEKLY')
            return existing_commit

        # 如果数据库中不存在，创建新的提交记录
        log_print(f"📝 创建新的基准提交记录: {base_commit_data['commit_id'][:8]}", 'WEEKLY')
        new_commit = Commit(
            repository_id=repository.id,
            commit_id=base_commit_data['commit_id'],
            path=file_path,
            author=base_commit_data.get('author', 'Unknown'),
            commit_time=base_commit_data['commit_time'],
            message=base_commit_data.get('message', ''),
            operation=base_commit_data.get('operation', 'M')
        )

        db.session.add(new_commit)
        db.session.commit()

        log_print(f"✅ 成功创建基准提交记录: {new_commit.commit_id[:8]} ({new_commit.commit_time})", 'WEEKLY')
        return new_commit

    except Exception as e:
        log_print(f"❌ 从{repository.type.upper()}获取基准版本失败: {e}", 'WEEKLY', force=True)
        import traceback
        traceback.print_exc()
        return None

def _normalize_commit_operation(operation):
    """Normalize commit operation to A/M/D/R style."""
    if operation is None:
        return 'M'

    normalized = str(operation).strip().upper()
    if not normalized:
        return 'M'

    mapping = {
        'ADD': 'A',
        'ADDED': 'A',
        'CREATE': 'A',
        'CREATED': 'A',
        'MOD': 'M',
        'MODIFIED': 'M',
        'UPDATE': 'M',
        'UPDATED': 'M',
        'DEL': 'D',
        'DELETE': 'D',
        'DELETED': 'D',
        'REMOVE': 'D',
        'REMOVED': 'D',
        'RENAME': 'R',
        'RENAMED': 'R',
    }
    return mapping.get(normalized, normalized[:1])


def _commit_sort_key_for_merge(commit):
    """Stable sort key for commit merge ordering."""
    commit_time = getattr(commit, 'commit_time', None)
    commit_ts = float('-inf')
    if isinstance(commit_time, datetime):
        try:
            if commit_time.tzinfo is None:
                commit_time = commit_time.replace(tzinfo=timezone.utc)
            commit_ts = commit_time.timestamp()
        except Exception:
            commit_ts = float('-inf')

    commit_db_id = getattr(commit, 'id', 0) or 0
    return commit_ts, commit_db_id


def _commit_time_to_iso(commit_time):
    if isinstance(commit_time, datetime):
        return commit_time.isoformat()
    return None


def generate_merged_diff_data(repository, file_path, base_commit, latest_commit, commits):
    """Generate merged diff data with real merge strategy and compatible metadata."""
    try:
        ordered_commits = sorted((commits or []), key=_commit_sort_key_for_merge)
        if not ordered_commits:
            return {
                'file_path': file_path,
                'file_type': DiffService().get_file_type(file_path),
                'base_commit': base_commit.commit_id if base_commit else None,
                'latest_commit': latest_commit.commit_id if latest_commit else None,
                'commits_count': 0,
                'commit_ids': [],
                'authors': [],
                'operations': [],
                'time_range': {'start': None, 'end': None},
                'merge_strategy': 'empty',
                'has_conflict_risk': False,
                'is_rename_suspected': False,
                'contains_added': False,
                'contains_deleted': False,
                'contains_modified': False,
                'diff_data': None,
                'merged_diff': None,
            }

        operations = [_normalize_commit_operation(getattr(commit, 'operation', None)) for commit in ordered_commits]
        operation_set = set(operations)
        commit_ids = [getattr(commit, 'commit_id', None) for commit in ordered_commits if getattr(commit, 'commit_id', None)]
        authors = sorted({(getattr(commit, 'author', None) or 'Unknown') for commit in ordered_commits})

        merge_strategy = 'single'
        merged_diff = None

        if len(ordered_commits) == 1:
            current_commit = ordered_commits[0]
            previous_commit = base_commit if (base_commit and base_commit.commit_id != current_commit.commit_id) else None
            if previous_commit:
                merged_diff = get_commit_pair_diff_internal(current_commit, previous_commit)
            else:
                merged_diff = get_unified_diff_data(current_commit, None)
        else:
            if are_commits_consecutive_internal(ordered_commits):
                merge_strategy = 'consecutive'
                merged_diff = handle_consecutive_commits_merge_internal(ordered_commits)
            else:
                merge_strategy = 'segmented'
                merged_diff = handle_non_consecutive_commits_merge_internal(ordered_commits)

        # Fallback path: use latest commit against nearest baseline to avoid empty payload.
        if not merged_diff:
            latest_for_fallback = ordered_commits[-1]
            previous_for_fallback = None
            if base_commit and base_commit.commit_id != latest_for_fallback.commit_id:
                previous_for_fallback = base_commit
            elif len(ordered_commits) > 1:
                previous_for_fallback = ordered_commits[-2]

            if previous_for_fallback:
                merged_diff = get_commit_pair_diff_internal(latest_for_fallback, previous_for_fallback)
            else:
                merged_diff = get_unified_diff_data(latest_for_fallback, None)

        merged_diff = clean_json_data(merged_diff) if merged_diff else {
            'type': 'summary',
            'file_path': file_path,
            'message': 'No diff payload generated'
        }

        segment_summaries = []
        if isinstance(merged_diff, dict) and merged_diff.get('type') == 'segmented_diff':
            for index, segment in enumerate(merged_diff.get('segments', []), start=1):
                segment_info = {}
                if isinstance(segment, dict):
                    segment_info = segment.get('segment_info') or {}
                segment_summaries.append({
                    'segment_index': segment_info.get('segment_index', index),
                    'current': segment_info.get('current'),
                    'previous': segment_info.get('previous'),
                })

        known_authors = [author for author in authors if author != 'Unknown']
        has_conflict_risk = (merge_strategy == 'segmented') or (len(known_authors) > 1 and len(ordered_commits) > 1)
        is_rename_suspected = ('R' in operation_set) or ('A' in operation_set and 'D' in operation_set)

        final_data = {
            'file_path': file_path,
            'file_type': DiffService().get_file_type(file_path),
            'base_commit': base_commit.commit_id if base_commit else None,
            'latest_commit': (latest_commit.commit_id if latest_commit else ordered_commits[-1].commit_id),
            'commits_count': len(ordered_commits),
            'commit_ids': commit_ids,
            'authors': authors,
            'operations': operations,
            'time_range': {
                'start': _commit_time_to_iso(getattr(ordered_commits[0], 'commit_time', None)),
                'end': _commit_time_to_iso(getattr(ordered_commits[-1], 'commit_time', None)),
            },
            'merge_strategy': merge_strategy,
            'has_conflict_risk': has_conflict_risk,
            'is_rename_suspected': is_rename_suspected,
            'contains_added': 'A' in operation_set,
            'contains_deleted': 'D' in operation_set,
            'contains_modified': 'M' in operation_set,
            'diff_data': merged_diff,
            'merged_diff': merged_diff,  # backward-friendly alias
        }

        if segment_summaries:
            final_data['segments'] = segment_summaries
            final_data['total_segments'] = len(segment_summaries)

        return clean_json_data(final_data)

    except Exception as e:
        log_print(f"鐢熸垚鍚堝苟diff鏁版嵁澶辫触: {e}", 'WEEKLY', force=True)
        return {}

@app.route('/projects/<int:project_id>/repositories')
def repository_config(project_id):
    project = Project.query.get_or_404(project_id)
    repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order).all()
    return render_template('repository_config.html', project=project, repositories=repositories)

# 新增Git仓库页面
@app.route('/projects/<int:project_id>/repositories/add-git')
def add_git_repository(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template('add_git_repository.html', project=project)

# 新增SVN仓库页面
@app.route('/projects/<int:project_id>/repositories/add-svn')
def add_svn_repository(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template('add_svn_repository.html', project=project)

# 创建Git仓库
@app.route('/repositories/git', methods=['POST'])
@require_admin
def create_git_repository():
    project_id = request.form.get('project_id')
    name = (request.form.get('name') or '').strip()
    category = request.form.get('category')
    url = request.form.get('url')
    server_url = request.form.get('server_url')
    token = (request.form.get('token') or '').strip()
    branch = request.form.get('branch')
    resource_type = request.form.get('resource_type')
    path_regex = request.form.get('file_type_filter') or request.form.get('path_regex')
    log_regex = request.form.get('log_regex')
    log_filter_regex = request.form.get('log_filter_regex')
    commit_filter = request.form.get('commit_filter')
    important_tables = request.form.get('important_tables')
    unconfirmed_history = bool(request.form.get('unconfirmed_history'))
    delete_table_alert = bool(request.form.get('delete_table_alert'))
    weekly_version_setting = request.form.get('weekly_version_setting')
    
    # Table配置项
    header_rows = request.form.get('header_rows')
    key_columns = request.form.get('key_columns')
    enable_id_confirmation = bool(request.form.get('enable_id_confirmation'))
    show_duplicate_id_warning = bool(request.form.get('show_duplicate_id_warning'))
    tag_selection = request.form.get('tag_selection')
    
    # Git日期范围配置
    current_date = request.form.get('current_date')
    start_date = None
    if current_date:
        try:
            from datetime import datetime
            start_date = datetime.strptime(current_date, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                start_date = datetime.strptime(current_date, '%Y-%m-%d')
            except ValueError:
                flash('日期格式错误，请使用 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD 格式', 'error')
                return redirect(url_for('add_git_repository', project_id=project_id))

    if not validate_repository_name(name):
        flash('仓库名称仅允许字母、数字、点、下划线和短横线', 'error')
        return redirect(url_for('add_git_repository', project_id=project_id))
    
    # 验证必填字段
    required_fields = [name, url, server_url, token, branch, resource_type]
    if resource_type == 'table' and not header_rows:
        flash('选择table类型时，表头行数为必填项', 'error')
        return redirect(url_for('add_git_repository', project_id=project_id))
    
    if not all(required_fields):
        flash('必填字段不能为空', 'error')
        return redirect(url_for('add_git_repository', project_id=project_id))
    
    # 获取并更新全局仓库ID计数器
    counter = GlobalRepositoryCounter.query.first()
    if not counter:
        # 如果计数器不存在，创建一个并初始化为当前最大仓库ID
        max_existing_id = db.session.query(db.func.max(Repository.id)).scalar() or 0
        counter = GlobalRepositoryCounter(max_repository_id=max_existing_id)
        db.session.add(counter)
        db.session.flush()  # 确保计数器被保存
    
    # 使用全局计数器分配新的仓库ID
    new_repository_id = counter.max_repository_id + 1
    counter.max_repository_id = new_repository_id
    counter.updated_at = datetime.now(timezone.utc)
    
    repository = Repository(
        id=new_repository_id,  # 手动设置ID
        project_id=project_id,
        name=name,
        type='git',
        category=category,
        url=url,
        server_url=server_url,
        token=token,
        branch=branch,
        resource_type=resource_type,
        path_regex=path_regex,
        log_regex=log_regex,
        log_filter_regex=log_filter_regex,
        commit_filter=commit_filter,
        important_tables=important_tables,
        unconfirmed_history=unconfirmed_history,
        delete_table_alert=delete_table_alert,
        weekly_version_setting=weekly_version_setting,
        header_rows=int(header_rows) if header_rows else None,
        key_columns=key_columns,
        enable_id_confirmation=enable_id_confirmation,
        show_duplicate_id_warning=show_duplicate_id_warning,
        tag_selection=tag_selection,
        start_date=start_date
    )
    
    db.session.add(repository)
    db.session.commit()
    
    # 异步克隆仓库，不阻塞页面响应
    import threading
    
    # 在主线程中获取repository的所有必要数据，避免跨线程访问SQLAlchemy对象
    repository_id = repository.id
    repository_name = repository.name
    
    def async_clone():
        """异步克隆函数"""
        enhanced_async_clone_with_status_update(repository_id, repository_name)
    
    # 启动后台线程进行克隆
    clone_thread = threading.Thread(target=async_clone)
    clone_thread.daemon = True
    clone_thread.start()
    
    flash('Git仓库创建成功，正在后台克隆仓库，请稍后查看仓库状态', 'success')
    return redirect(url_for('repository_config', project_id=project_id))

def clone_repository_to_local(repository):
    """使用增强Git服务克隆仓库到本地，支持重试和大型仓库优化"""
    log_print(f"🚀 开始使用增强Git服务克隆仓库: {repository.name}", 'GIT')
    
    try:
        # 创建增强Git服务实例
        enhanced_git_service = EnhancedGitService(
            repository_url=repository.url,
            root_directory=repository.root_directory,
            username=repository.username,
            token=repository.token,
            repository=repository
        )
        
        # 执行增强克隆（包含重试机制和大型仓库优化）
        success, message = enhanced_git_service.clone_or_update_repository_with_retry()
        stats = enhanced_git_service.get_repository_size_info() if success else None
        
        if success:
            log_print(f"✅ 增强Git克隆成功: {repository.name}", 'GIT')
            if stats:
                log_print(f"📊 仓库统计: {stats}", 'GIT')
            
            # 不在这里更新状态，由调用方处理
                
        else:
            log_print(f"❌ 增强Git克隆失败: {repository.name} | 错误: {message}", 'GIT', force=True)
            raise Exception(f"增强Git克隆失败: {message}")
            
    except Exception as e:
        error_msg = f"增强Git克隆过程出错: {str(e)}"
        log_print(f"❌ {error_msg}", 'GIT', force=True)
        raise Exception(error_msg)

def enhanced_async_clone_with_status_update(repository_id, repository_name):
    """增强的异步克隆函数，带状态更新和应用上下文管理"""
    try:
        # 使用单一应用上下文管理所有数据库操作
        with app.app_context():
            # 重新查询repository对象，确保在当前线程和上下文中有效
            repo = db.session.get(Repository, repository_id)
            if not repo:
                log_print(f"❌ 无法找到仓库ID: {repository_id}", 'REPO', force=True)
                return
                
            # 更新克隆状态为进行中
            repo.clone_status = 'cloning'
            repo.clone_error = None
            db.session.commit()
            log_print(f"🔄 开始异步克隆仓库: {repository_name}", 'GIT')
            
            # 执行克隆操作
            clone_repository_to_local(repo)
            
            # 更新克隆状态为完成
            repo.clone_status = 'completed'
            repo.clone_error = None
            db.session.commit()
            log_print(f"✅ 异步克隆完成: {repository_name}", 'GIT')
            
            # 自动创建数据分析任务
            task_id = create_auto_sync_task(repository_id)
            if task_id:
                log_print(f"🚀 已为仓库 {repository_name} 自动创建数据分析任务", 'INFO')
            
    except Exception as e:
        error_msg = str(e)
        log_print(f"❌ 增强异步克隆失败: {error_msg}", 'INFO')
        
        # 在异常处理中也需要应用上下文
        try:
            with app.app_context():
                repo = db.session.get(Repository, repository_id)
                if repo:
                    repo.clone_status = 'failed'
                    repo.clone_error = error_msg
                    db.session.commit()
        except Exception as db_error:
            log_print(f"❌ 更新克隆状态失败: {str(db_error)}")
    
    log_print(f"🏁 异步克隆任务结束: {repository_name} (ID: {repository_id}, 'INFO')")

def enhanced_retry_clone_repository(repository_id):
    """增强的重试克隆函数（线程安全：仅传repository_id）"""
    repository_name = f"repo-{repository_id}"
    try:
        with app.app_context():
            repository = db.session.get(Repository, repository_id)
            if not repository:
                log_print(f"❌ 重试克隆失败：仓库不存在 {repository_id}", 'GIT', force=True)
                return

            repository_name = repository.name
            log_print(f"🔄 开始增强重试克隆: {repository_name}", 'INFO')

            # 重置克隆状态
            repository.clone_status = 'cloning'
            repository.clone_error = None
            db.session.commit()

            # 执行增强克隆（包含内置重试机制）
            clone_repository_to_local(repository)

            repository.clone_status = 'completed'
            repository.clone_error = None
            db.session.commit()
            log_print(f"✅ 增强重试克隆完成: {repository_name}", 'INFO')

    except Exception as e:
        error_msg = f"增强重试克隆失败: {str(e)}"
        log_print(f"❌ {error_msg}", 'GIT', force=True)
        try:
            with app.app_context():
                repository = db.session.get(Repository, repository_id)
                if repository:
                    repository.clone_status = 'failed'
                    repository.clone_error = error_msg
                    db.session.commit()
        except Exception as db_error:
            log_print(f"❌ 更新重试克隆状态失败: {db_error}", 'GIT', force=True)

def enhanced_async_svn_clone_with_status_update(repository_id, repository_name):
    """增强的异步SVN克隆函数，包含状态更新"""
    try:
        log_print(f"🚀 开始异步SVN克隆任务: {repository_name} (ID: {repository_id})", 'INFO')

        # 在Flask应用上下文中执行
        with app.app_context():
            repo = db.session.get(Repository, repository_id)
            if not repo:
                log_print(f"❌ 仓库不存在: {repository_id}", 'SVN')
                return

            # 更新克隆状态为进行中
            repo.clone_status = 'cloning'
            repo.clone_error = None
            db.session.commit()
            log_print(f"🔄 开始异步克隆SVN仓库: {repository_name}", 'SVN')

            # 执行SVN克隆操作
            clone_svn_repository_to_local(repo)

            # 更新克隆状态为完成
            repo.clone_status = 'completed'
            repo.clone_error = None
            db.session.commit()
            log_print(f"✅ 异步SVN克隆完成: {repository_name}", 'SVN')

            # 自动创建数据分析任务
            task_id = create_auto_sync_task(repository_id)
            if task_id:
                log_print(f"🚀 已为SVN仓库 {repository_name} 自动创建数据分析任务", 'INFO')

    except Exception as e:
        error_msg = str(e)
        log_print(f"❌ 增强异步SVN克隆失败: {error_msg}", 'INFO')

        # 在异常处理中也需要应用上下文
        try:
            with app.app_context():
                repo = db.session.get(Repository, repository_id)
                if repo:
                    repo.clone_status = 'failed'
                    repo.clone_error = error_msg
                    db.session.commit()
        except Exception as db_error:
            log_print(f"❌ 更新SVN克隆状态失败: {str(db_error)}")

    log_print(f"🏁 异步SVN克隆任务结束: {repository_name} (ID: {repository_id})", 'INFO')

def clone_svn_repository_to_local(repository):
    """使用SVN服务克隆仓库到本地"""
    log_print(f"🚀 开始使用SVN服务克隆仓库: {repository.name}", 'SVN')

    try:
        svn_service = get_svn_service(repository)

        # 执行SVN检出或更新
        success, message = svn_service.checkout_or_update_repository()

        if success:
            log_print(f"✅ SVN仓库克隆成功: {repository.name} - {message}", 'SVN')
        else:
            log_print(f"❌ SVN仓库克隆失败: {repository.name} - {message}", 'SVN')
            raise Exception(message)

    except Exception as e:
        error_msg = f"SVN仓库克隆失败: {str(e)}"
        log_print(f"❌ {error_msg}", 'SVN', force=True)
        raise Exception(error_msg)

# 创建SVN仓库
@app.route('/repositories/svn', methods=['POST'])
@require_admin
def create_svn_repository():
    project_id = request.form.get('project_id')
    name = (request.form.get('name') or '').strip()
    category = request.form.get('category')
    url = request.form.get('url')
    root_directory = request.form.get('root_directory')
    username = (request.form.get('username') or '').strip()
    password = (request.form.get('password') or '').strip()
    current_version = request.form.get('current_version')
    resource_type = request.form.get('resource_type')
    path_regex = request.form.get('path_regex')
    log_regex = request.form.get('log_regex')
    log_filter_regex = request.form.get('log_filter_regex')
    commit_filter = request.form.get('commit_filter')
    important_tables = request.form.get('important_tables')
    unconfirmed_history = bool(request.form.get('unconfirmed_history'))
    delete_table_alert = bool(request.form.get('delete_table_alert'))
    weekly_version_setting = request.form.get('weekly_version_setting')

    # Table配置字段
    header_rows = request.form.get('header_rows')
    key_columns = request.form.get('key_columns')
    enable_id_confirmation = bool(request.form.get('enable_id_confirmation'))
    show_duplicate_id_warning = bool(request.form.get('show_duplicate_id_warning'))
    tag_selection = request.form.get('tag_selection')

    if not validate_repository_name(name):
        flash('仓库名称仅允许字母、数字、点、下划线和短横线', 'error')
        return redirect(url_for('add_svn_repository', project_id=project_id))
    
    if not all([name, url, root_directory, username, password, current_version, resource_type]):
        flash('必填字段不能为空', 'error')
        return redirect(url_for('add_svn_repository', project_id=project_id))
    
    # 获取并更新全局仓库ID计数器
    counter = GlobalRepositoryCounter.query.first()
    if not counter:
        # 如果计数器不存在，创建一个并初始化为当前最大仓库ID
        max_existing_id = db.session.query(db.func.max(Repository.id)).scalar() or 0
        counter = GlobalRepositoryCounter(max_repository_id=max_existing_id)
        db.session.add(counter)
        db.session.flush()  # 确保计数器被保存
    
    # 使用全局计数器分配新的仓库ID
    new_repository_id = counter.max_repository_id + 1
    counter.max_repository_id = new_repository_id
    counter.updated_at = datetime.now(timezone.utc)
    
    repository = Repository(
        id=new_repository_id,  # 手动设置ID
        project_id=project_id,
        name=name,
        type='svn',
        category=category,
        url=url,
        root_directory=root_directory,
        username=username,
        password=password,
        current_version=current_version,
        resource_type=resource_type,
        path_regex=path_regex,
        log_regex=log_regex,
        log_filter_regex=log_filter_regex,
        commit_filter=commit_filter,
        important_tables=important_tables,
        unconfirmed_history=unconfirmed_history,
        delete_table_alert=delete_table_alert,
        weekly_version_setting=weekly_version_setting,
        clone_status='pending',  # 设置初始克隆状态
        # Table配置字段
        header_rows=int(header_rows) if header_rows else None,
        key_columns=key_columns,
        enable_id_confirmation=enable_id_confirmation,
        show_duplicate_id_warning=show_duplicate_id_warning,
        tag_selection=tag_selection
    )

    db.session.add(repository)
    db.session.commit()

    # 异步克隆SVN仓库，不阻塞页面响应
    import threading

    # 在主线程中获取repository的所有必要数据，避免跨线程访问SQLAlchemy对象
    repository_id = repository.id
    repository_name = repository.name

    def async_svn_clone():
        """异步SVN克隆函数"""
        enhanced_async_svn_clone_with_status_update(repository_id, repository_name)

    # 启动后台线程进行克隆
    clone_thread = threading.Thread(target=async_svn_clone)
    clone_thread.daemon = True
    clone_thread.start()

    flash('SVN仓库创建成功，正在后台克隆仓库，请稍后查看仓库状态', 'success')
    return redirect(url_for('repository_config', project_id=project_id))

# 提交记录列表页面
@app.route('/repositories/<int:repository_id>/commits')
def commit_list(repository_id):
    log_print(f"=== 访问提交列表页面 ===", 'APP')
    log_print(f"Repository ID: {repository_id}", 'APP')
    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    
    # 获取同一项目下的所有仓库，按名称分组
    all_repositories = project.repositories
    repository_groups = {}
    
    # 为每个仓库获取实际分支信息
    for repo in all_repositories:
        # 如果没有分支信息，尝试从git服务获取
        if not repo.branch and repo.type == 'git':
            try:
                from services.threaded_git_service import ThreadedGitService
                git_service = ThreadedGitService(repo.url, repo.root_directory, repo.username, repo.token, repo)
                branches = git_service.get_branches()
                if branches:
                    # 使用第一个分支作为默认分支，通常是master或main
                    repo.branch = branches[0]
                    db.session.commit()
            except:
                # 如果获取失败，使用默认值
                repo.branch = 'master'
        elif not repo.branch:
            # SVN或其他类型仓库的默认分支
            repo.branch = 'master'
        
        # 移除 _git 和 _svn 后缀来分组
        base_name = repo.name
        if base_name.endswith('_git') or base_name.endswith('_svn'):
            base_name = base_name.rsplit('_', 1)[0]
        
        if base_name not in repository_groups:
            repository_groups[base_name] = {
                'name': base_name,
                'repositories': [],
                'earliest_repo': repo
            }
        
        repository_groups[base_name]['repositories'].append(repo)
        
        # 找到最早创建的仓库作为代表
        if repo.id < repository_groups[base_name]['earliest_repo'].id:
            repository_groups[base_name]['earliest_repo'] = repo
    
    # 转换为列表格式，用于模板显示
    grouped_repositories = []
    for group_name, group_data in repository_groups.items():
        grouped_repositories.append({
            'name': group_name,
            'repositories': group_data['repositories'],
            'current_repo': repository if repository in group_data['repositories'] else group_data['earliest_repo']
        })
    
    repositories = all_repositories  # 保持向后兼容
    
    # 获取筛选参数
    filters = {
        'author': request.args.get('author', ''),
        'path': request.args.get('path', ''),
        'version': request.args.get('version', ''),
        'operation': request.args.get('operation', ''),
        'status': request.args.get('status', ''),
        'status_list': [s for s in request.args.getlist('status') if s]  # 过滤空字符串
    }
    
    # 获取分页参数
    page = max(1, request.args.get('page', 1, type=int) or 1)
    requested_per_page = request.args.get('per_page', 50, type=int) or 50
    per_page = min(max(requested_per_page, 1), 200)  # 限制每页大小，避免大页拖垮查询
    
    # 构建查询
    query = Commit.query.filter_by(repository_id=repository_id)
    
    # 应用仓库配置的起始日期过滤
    if repository.start_date:
        query = query.filter(Commit.commit_time >= repository.start_date)
        log_print(f"应用仓库起始日期过滤: {repository.start_date}", 'APP')
    
    # 检查仓库克隆状态和数据准备情况
    repository_status = {
        'clone_status': repository.clone_status,
        'clone_error': repository.clone_error,
        'is_data_ready': False,
        'status_message': ''
    }
    
    # 先检查基础查询结果
    base_count = query.count()
    log_print(f"基础查询结果数量: {base_count}", 'APP')
    
    # 判断数据是否准备好
    if repository.clone_status == 'cloning':
        repository_status['status_message'] = '仓库正在克隆中，请稍后刷新页面查看数据...'
    elif repository.clone_status == 'failed':
        repository_status['status_message'] = f'仓库克隆失败：{repository.clone_error or "未知错误"}'
    elif repository.clone_status == 'completed' and base_count == 0:
        repository_status['status_message'] = '仓库克隆完成，正在分析提交数据，请稍后刷新页面或点击"手动获取数据"按钮...'
    elif base_count > 0:
        repository_status['is_data_ready'] = True
    
    if filters['author']:
        query = query.filter(Commit.author.contains(filters['author']))
    if filters['path']:
        query = query.filter(Commit.path.contains(filters['path']))
    if filters['version']:
        query = query.filter(Commit.version.contains(filters['version']))
    if filters['operation']:
        query = query.filter_by(operation=filters['operation'])
    # 处理状态筛选 - 支持逗号分隔的多状态
    status_param = request.args.get('status', '')
    if status_param and ',' in status_param:
        # 如果status参数包含逗号，说明是多选状态
        status_list = [s.strip() for s in status_param.split(',') if s.strip()]
        if status_list:
            query = query.filter(Commit.status.in_(status_list))
    elif filters['status_list']:
        query = query.filter(Commit.status.in_(filters['status_list']))
    elif filters['status']:
        query = query.filter_by(status=filters['status'])
    
    # 分页查询
    pagination = query.order_by(Commit.commit_time.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    commits = pagination.items
    
    # 调试信息
    log_print(f"=== 分页调试信息 ===", 'APP')
    log_print(f"Repository ID: {repository_id}", 'APP')
    log_print(f"Page: {page}, Per page: {per_page}", 'APP')
    log_print(f"Pagination total: {pagination.total}", 'APP')
    log_print(f"Pagination pages: {pagination.pages}", 'APP')
    log_print(f"Current page items: {len(commits)}", 'APP')
    log_print(f"Filters: {filters}", 'APP')
    log_print(f"=====================", 'APP')
    
    return render_template('commit_list.html',
                         commits=commits,
                         pagination=pagination,
                         repository=repository,
                         project=project,
                         repositories=repositories,
                         grouped_repositories=grouped_repositories,
                         filters=filters,
                         repository_status=repository_status)

# diff确认页面

# 新的带项目代号和仓库名的Excel diff数据路由
@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/excel-diff-data')
def get_excel_diff_data_with_path(project_code, repository_name, commit_id):
    return get_excel_diff_data(commit_id)

# 保持向后兼容的原路由
@app.route('/commits/<int:commit_id>/excel-diff-data')
def get_excel_diff_data(commit_id):
    """异步获取Excel diff数据的API端点（支持HTML缓存优先）"""
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    
    # 检查是否为Excel文件
    is_excel = excel_cache_service.is_excel_file(commit.path)
    
    if not is_excel:
        return jsonify({'error': True, 'message': '不是Excel文件'})
    
    try:
        # 首先检查HTML缓存（优先级最高）
        cached_html = excel_html_cache_service.get_cached_html(
            repository.id, commit.commit_id, commit.path
        )
        
        if cached_html:
            log_print(f"✅ 从HTML缓存获取Excel差异: {commit.path}", 'EXCEL')
            return jsonify({
                'success': True, 
                'html_content': cached_html['html_content'],
                'css_content': cached_html['css_content'],
                'js_content': cached_html['js_content'],
                'metadata': cached_html['metadata'],
                'from_html_cache': True,
                'created_at': cached_html['created_at'].isoformat() if cached_html['created_at'] else None
            })
        
        # HTML缓存未命中，检查原始数据缓存
        cached_diff = excel_cache_service.get_cached_diff(
            repository.id, commit.commit_id, commit.path
        )
        
        if cached_diff:
            log_print(f"📊 从数据缓存获取Excel差异，生成HTML: {commit.path}", 'EXCEL')
            try:
                # 解析缓存的diff数据
                import json
                diff_data = json.loads(cached_diff.diff_data)
                
                # 生成HTML缓存
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
                
                # 保存HTML缓存
                metadata = {
                    'file_path': commit.path,
                    'commit_id': commit.commit_id,
                    'repository_name': repository.name,
                    'processing_time': cached_diff.processing_time
                }
                
                excel_html_cache_service.save_html_cache(
                    repository.id, commit.commit_id, commit.path,
                    html_content, css_content, js_content, metadata
                )
                
                return jsonify({
                    'success': True, 
                    'html_content': html_content,
                    'css_content': css_content,
                    'js_content': js_content,
                    'metadata': metadata,
                    'from_html_cache': False,
                    'from_data_cache': True
                })
                
            except Exception as e:
                log_print(f"⚠️ HTML生成失败，返回原始数据: {e}", 'INFO')
                return jsonify({'success': True, 'diff_data': json.loads(cached_diff.diff_data), 'from_cache': True})
        
        # 所有缓存都未命中，实时处理
        log_print(f"🔄 缓存未命中，开始实时处理Excel文件: {commit.path}", 'INFO')
        
        # 获取前一个提交
        previous_commit = None
        file_commits = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()
        
        # 使用统一差异服务处理
        diff_data = get_unified_diff_data(commit, file_commits)
        
        if diff_data and diff_data.get('type') == 'excel':
            try:
                # 生成HTML内容
                html_content, css_content, js_content = excel_html_cache_service.generate_excel_html(diff_data)
                
                # 保存HTML缓存
                metadata = {
                    'file_path': commit.path,
                    'commit_id': commit.commit_id,
                    'repository_name': repository.name,
                    'real_time_processing': True
                }
                
                excel_html_cache_service.save_html_cache(
                    repository.id, commit.commit_id, commit.path,
                    html_content, css_content, js_content, metadata
                )
                
                # 异步缓存原始数据，用户主动请求使用高优先级
                add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                
                log_print(f"✅ Excel差异实时处理完成，HTML缓存已保存: {commit.path}", 'EXCEL')
                return jsonify({
                    'success': True, 
                    'html_content': html_content,
                    'css_content': css_content,
                    'js_content': js_content,
                    'metadata': metadata,
                    'from_html_cache': False,
                    'real_time': True
                })
                
            except Exception as e:
                log_print(f"⚠️ HTML生成失败，返回原始数据: {e}", 'INFO')
                # 如果HTML生成失败，仍然返回原始数据，用户主动请求使用高优先级
                add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                return jsonify({'success': True, 'diff_data': diff_data, 'from_cache': False})
        else:
            error_msg = diff_data.get('error', '处理失败') if diff_data else 'Excel文件处理返回空结果'
            return jsonify({'error': True, 'message': error_msg})
            
    except Exception as e:
        log_print(f"❌ Excel diff处理失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': True, 'message': f'Excel文件处理失败: {str(e)}'})

# 新的统一差异显示路由
# 新的带项目代号和仓库名的新diff路由
@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/diff/new')
def commit_diff_new_with_path(project_code, repository_name, commit_id):
    return commit_diff_new(commit_id)

# 保持向后兼容的原路由
@app.route('/commits/<int:commit_id>/diff/new')
def commit_diff_new(commit_id):
    """使用新的差异服务显示文件差异"""
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    
    # 获取该文件的所有提交历史
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc()).all()
    
    # 获取上一个版本的提交信息
    previous_commit = None
    current_index = None
    for i, fc in enumerate(file_commits):
        if fc.id == commit.id:
            current_index = i
            break
    
    if current_index is not None and current_index + 1 < len(file_commits):
        previous_commit = file_commits[current_index + 1]
    
    # 如果按索引未找到，尝试按时间查找
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()
    
    # 使用新的差异服务处理文件
    diff_data = get_unified_diff_data(commit, previous_commit)
    
    return render_template('commit_diff_new.html', 
                         commit=commit, 
                         repository=repository,
                         project=project,
                         diff_data=diff_data,
                         file_commits=file_commits,
                         previous_commit=previous_commit)

# 完整文件diff路由
@app.route('/commits/<int:commit_id>/full-diff')
def commit_full_diff(commit_id):
    """显示完整文件的diff，类似Git工具的并排显示"""
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project

    # 获取上一个版本的提交信息
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc()).all()

    previous_commit = None
    current_index = None
    for i, fc in enumerate(file_commits):
        if fc.id == commit.id:
            current_index = i
            break

    if current_index is not None and current_index + 1 < len(file_commits):
        previous_commit = file_commits[current_index + 1]
    
    # 如果按索引未找到，尝试按时间查找
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()
    
    # 获取完整文件内容
    try:
        import subprocess
        import os
        
        # 获取仓库的本地路径
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            git_service = ThreadedGitService(repository.url, repository.root_directory, 
                                   repository.username, repository.token, repository, set())
            local_path = git_service.local_path
            
            # 如果本地路径不存在，尝试克隆
            if not os.path.exists(local_path):
                success, message = git_service.clone_or_update_repository()
                if not success:
                    current_file_content = f"仓库克隆失败: {message}"
                    previous_file_content = f"仓库克隆失败: {message}"
                    return render_template('full_file_diff.html',
                                         commit=commit,
                                         repository=repository,
                                         project=project,
                                         previous_commit=previous_commit,
                                         current_file_content=current_file_content,
                                         previous_file_content=previous_file_content)
        else:
            svn_service = get_svn_service(repository)
            local_path = svn_service.local_path
        
        # 获取当前版本文件内容
        try:
            if repository.type == 'git':
                result = subprocess.run([
                    'git', 'show', f'{commit.commit_id}:{commit.path}'
                ], cwd=local_path, capture_output=True, text=True, encoding='utf-8')
            else:
                # SVN处理
                result = subprocess.run([
                    'svn', 'cat', f'{repository.url}/{commit.path}@{commit.commit_id}'
                ], capture_output=True, text=True, encoding='utf-8')
            
            if result.returncode == 0:
                current_file_content = result.stdout
                log_print(f"Current file content length: {len(current_file_content)}")
            else:
                current_file_content = f"无法获取文件内容: {result.stderr}"
                log_print(f"Git show error: {result.stderr}", 'INFO')
        except Exception as e:
            current_file_content = f"获取当前版本失败: {str(e)}"
        
        # 获取前一版本文件内容
        previous_file_content = ""
        if previous_commit:
            try:
                if repository.type == 'git':
                    result = subprocess.run([
                        'git', 'show', f'{previous_commit.commit_id}:{commit.path}'
                    ], cwd=local_path, capture_output=True, text=True, encoding='utf-8')
                else:
                    # SVN处理
                    result = subprocess.run([
                        'svn', 'cat', f'{repository.url}/{commit.path}@{previous_commit.commit_id}'
                    ], capture_output=True, text=True, encoding='utf-8')
                
                if result.returncode == 0:
                    previous_file_content = result.stdout
                else:
                    previous_file_content = f"无法获取文件内容: {result.stderr}"
            except Exception as e:
                previous_file_content = f"获取前一版本失败: {str(e)}"
        else:
            previous_file_content = ""
            
    except Exception as e:
        log_print(f"获取文件内容失败: {e}", 'INFO')
        current_file_content = "无法获取文件内容"
        previous_file_content = "无法获取文件内容"
    
    # 生成Git风格的并排diff数据
    side_by_side_diff = generate_side_by_side_diff(current_file_content, previous_file_content)

    return render_template('git_style_diff.html',
                         commit=commit,
                         repository=repository,
                         project=project,
                         previous_commit=previous_commit,
                         current_file_content=current_file_content,
                         previous_file_content=previous_file_content,
                         side_by_side_diff=side_by_side_diff)

def generate_side_by_side_diff(current_content, previous_content):
    """生成Git风格的并排diff数据"""
    import difflib

    if not current_content:
        current_content = ""
    if not previous_content:
        previous_content = ""

    current_lines = current_content.splitlines()
    previous_lines = previous_content.splitlines()

    # 使用SequenceMatcher进行更精确的diff
    matcher = difflib.SequenceMatcher(None, previous_lines, current_lines)

    left_lines = []  # 左侧（前一版本）
    right_lines = []  # 右侧（当前版本）

    left_line_num = 1
    right_line_num = 1

    # 处理所有的操作块
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            # 相同的行
            for i in range(i1, i2):
                left_lines.append({
                    'line_num': left_line_num,
                    'content': previous_lines[i],
                    'type': 'context'
                })
                right_lines.append({
                    'line_num': right_line_num,
                    'content': current_lines[j1 + (i - i1)],
                    'type': 'context'
                })
                left_line_num += 1
                right_line_num += 1

        elif tag == 'delete':
            # 删除的行（只在左侧显示）
            for i in range(i1, i2):
                left_lines.append({
                    'line_num': left_line_num,
                    'content': previous_lines[i],
                    'type': 'removed'
                })
                right_lines.append({
                    'line_num': None,
                    'content': '',
                    'type': 'empty'
                })
                left_line_num += 1

        elif tag == 'insert':
            # 插入的行（只在右侧显示）
            for j in range(j1, j2):
                left_lines.append({
                    'line_num': None,
                    'content': '',
                    'type': 'empty'
                })
                right_lines.append({
                    'line_num': right_line_num,
                    'content': current_lines[j],
                    'type': 'added'
                })
                right_line_num += 1

        elif tag == 'replace':
            # 替换的行
            max_lines = max(i2 - i1, j2 - j1)

            for k in range(max_lines):
                # 左侧（删除的行）
                if k < (i2 - i1):
                    left_lines.append({
                        'line_num': left_line_num,
                        'content': previous_lines[i1 + k],
                        'type': 'removed'
                    })
                    left_line_num += 1
                else:
                    left_lines.append({
                        'line_num': None,
                        'content': '',
                        'type': 'empty'
                    })

                # 右侧（添加的行）
                if k < (j2 - j1):
                    right_lines.append({
                        'line_num': right_line_num,
                        'content': current_lines[j1 + k],
                        'type': 'added'
                    })
                    right_line_num += 1
                else:
                    right_lines.append({
                        'line_num': None,
                        'content': '',
                        'type': 'empty'
                    })

    return {
        'left_lines': left_lines,
        'right_lines': right_lines
    }

# 重新计算差异API
# 新的带项目代号和仓库名的刷新diff路由
@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/refresh-diff', methods=['POST'])
def refresh_commit_diff_with_path(project_code, repository_name, commit_id):
    return refresh_commit_diff(commit_id)

# 保持向后兼容的原路由
@app.route('/commits/<int:commit_id>/refresh-diff', methods=['POST'])
def refresh_commit_diff(commit_id):
    """重新计算提交的差异数据，绕过缓存 - 优化版本"""
    try:
        commit = Commit.query.get_or_404(commit_id)
        repository = commit.repository

        log_print(f"🔄 开始重新计算差异: commit={commit_id}, file={commit.path}", 'APP')

        # 记录开始时间
        start_time = time.time()

        # 如果是Excel文件，先清除相关缓存
        if excel_cache_service.is_excel_file(commit.path):
            log_print(f"🗑️ 清除Excel缓存: {commit.path}", 'EXCEL')

            # 优化的批量删除缓存，减少数据库操作和锁定时间
            try:
                cache_delete_start = time.time()

                # 删除diff缓存
                deleted_count = DiffCache.query.filter_by(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path
                ).delete()

                # 删除HTML缓存 - 直接在这里执行，避免额外的函数调用开销
                html_deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repository.id,
                    commit_id=commit.commit_id,
                    file_path=commit.path
                ).delete()

                # 一次性提交所有删除操作
                if deleted_count > 0 or html_deleted_count > 0:
                    db.session.commit()
                    cache_delete_time = time.time() - cache_delete_start
                    log_print(f"✅ 已删除缓存记录: diff={deleted_count}, html={html_deleted_count} | 耗时: {cache_delete_time:.2f}秒", 'EXCEL')
                else:
                    log_print(f"ℹ️ 没有找到需要删除的缓存记录", 'EXCEL')

            except Exception as cache_error:
                log_print(f"⚠️ 清除缓存时出错: {cache_error}", 'EXCEL', force=True)
                db.session.rollback()

        # 获取上一个版本的提交信息 - 优化查询
        file_commits = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()

        # 强制重新计算差异
        diff_calculation_start = time.time()
        diff_data = get_unified_diff_data(commit, file_commits)
        diff_calculation_time = time.time() - diff_calculation_start

        if diff_data:
            # 如果是Excel文件，重新缓存结果
            if excel_cache_service.is_excel_file(commit.path) and diff_data.get('type') == 'excel':
                cache_start = time.time()
                log_print(f"💾 重新缓存Excel差异数据", 'EXCEL')

                try:
                    excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,
                        previous_commit_id=file_commits.commit_id if file_commits else None,
                        processing_time=diff_calculation_time,
                        commit_time=commit.commit_time
                    )
                    cache_time = time.time() - cache_start
                    log_print(f"💾 缓存保存完成，耗时: {cache_time:.2f}秒", 'EXCEL')

                except Exception as cache_error:
                    log_print(f"⚠️ 保存缓存时出错: {cache_error}", 'EXCEL')

            total_time = time.time() - start_time
            log_print(f"✅ 差异重新计算完成: {commit.path} | 计算耗时: {diff_calculation_time:.2f}秒 | 总耗时: {total_time:.2f}秒", 'APP')

            # 使用安全的JSON序列化处理diff_data中的NaN值
            safe_diff_data = safe_json_serialize(diff_data)

            return jsonify({
                'success': True,
                'message': f'差异重新计算完成，计算耗时 {diff_calculation_time:.2f} 秒',
                'processing_time': diff_calculation_time,
                'total_time': total_time,
                'diff_data': safe_diff_data  # 使用清理后的数据，避免NaN导致JSON解析错误
            })
        else:
            total_time = time.time() - start_time
            log_print(f"❌ 差异重新计算失败: {commit.path} | 耗时: {total_time:.2f}秒", 'APP', force=True)
            return jsonify({
                'success': False,
                'message': '差异重新计算失败，请检查文件内容',
                'total_time': total_time
            })

    except Exception as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 重新计算差异异常: {e} | 耗时: {total_time:.2f}秒", 'APP', force=True)
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'重新计算差异失败: {str(e)}',
            'total_time': total_time
        }), 500

# 新的带项目代号和仓库名的路由
@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/diff')
def commit_diff_with_path(project_code, repository_name, commit_id):
    return commit_diff(commit_id)

# 保持向后兼容的原路由
@app.route('/commits/<int:commit_id>/diff')
def commit_diff(commit_id):
    commit = Commit.query.get_or_404(commit_id)
    repository = commit.repository
    project = repository.project
    
    # 检查是否为删除操作
    is_deleted = commit.operation == 'D'
    
    # 检查是否为Excel文件
    is_excel = excel_cache_service.is_excel_file(commit.path)
    
    # 获取该文件的所有提交历史 - 使用更严格的排序
    file_commits = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path
    ).order_by(Commit.commit_time.desc(), Commit.id.desc()).all()
    
    # 获取上一个版本的提交信息 - 改进的查找逻辑
    previous_commit = None
    
    # 方法1: 直接按时间查找前一提交（最可靠的方法）
    previous_commit = Commit.query.filter(
        Commit.repository_id == repository.id,
        Commit.path == commit.path,
        Commit.commit_time < commit.commit_time
    ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()
    
    # 方法2: 如果按时间未找到，尝试按ID查找（处理时间相同的情况）
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.id < commit.id
        ).order_by(Commit.id.desc()).first()
    
    # 方法3: 如果还是未找到，使用索引方法作为最后备选
    if previous_commit is None:
        current_index = None
        for i, fc in enumerate(file_commits):
            if fc.id == commit.id:
                current_index = i
                break
        
        if current_index is not None and current_index + 1 < len(file_commits):
            previous_commit = file_commits[current_index + 1]
    
    # 调试日志
    log_print(f"🔍 查找前一提交 - 文件: {commit.path}", 'DIFF', force=True)
    log_print(f"🔍 该文件总提交数: {len(file_commits)}", 'DIFF', force=True)
    
    if previous_commit:
        log_print(f"✅ 找到前一提交: ID:{previous_commit.id} {previous_commit.commit_id[:8]} {previous_commit.commit_time}", 'DIFF', force=True)
    else:
        log_print(f"❌ 未找到前一提交 - 这是初始提交", 'DIFF', force=True)
    
    # 如果是删除操作，返回删除信息页面
    if is_deleted:
        return render_template('commit_diff.html', 
                             commit=commit, 
                             repository=repository,
                             project=project,
                             diff_data={'type': 'deleted', 'message': '该文件已被删除'},
                             file_commits=file_commits,
                             previous_commit=previous_commit,
                             is_excel=is_excel,
                             is_deleted=True)
    
    if is_excel:
        # Excel文件处理 - 优先使用缓存
        try:
            log_print(f"处理Excel文件差异: {commit.path}", 'EXCEL')
            log_print(f"Commit ID: {commit.commit_id}", 'EXCEL')
            log_print(f"Repository: {repository.name}", 'EXCEL')
            
            # 首先检查缓存
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path
            )
            
            diff_data = None
            cache_is_valid = False
            
            if cached_diff:
                log_print(f"📦 从缓存获取Excel差异数据: {commit.path}", 'EXCEL')
                log_print(f"🏷️ 缓存版本: {cached_diff.diff_version} | 缓存时间: {cached_diff.created_at}", 'EXCEL')
                
                try:
                    # 从缓存对象中提取实际的diff数据
                    import json
                    cached_data = json.loads(cached_diff.diff_data)
                    
                    # 验证缓存数据的完整性
                    is_valid, validation_message = validate_excel_diff_data(cached_data)
                    log_print(f"🔍 缓存数据验证: {validation_message}", 'EXCEL')
                    
                    if is_valid:
                        diff_data = cached_data
                        cache_is_valid = True
                        log_print(f"✅ 缓存数据验证通过，使用缓存数据", 'EXCEL')
                    else:
                        log_print(f"❌ 缓存数据验证失败: {validation_message}", 'EXCEL', force=True)
                        log_print(f"🔄 将删除无效缓存并重新生成", 'EXCEL')
                        
                        # 删除无效的缓存记录
                        try:
                            db.session.delete(cached_diff)
                            db.session.commit()
                            log_print(f"🗑️ 已删除无效缓存记录 ID: {cached_diff.id}", 'EXCEL')
                        except Exception as delete_error:
                            log_print(f"❌ 删除缓存记录失败: {delete_error}", 'EXCEL', force=True)
                            db.session.rollback()
                            
                except json.JSONDecodeError as e:
                    log_print(f"❌ 缓存数据JSON解析失败: {e}", 'EXCEL', force=True)
                    cache_is_valid = False
                except Exception as e:
                    log_print(f"❌ 缓存数据处理异常: {e}", 'EXCEL', force=True)
                    cache_is_valid = False
            
            # 如果缓存无效或不存在，重新生成
            if not cache_is_valid:
                log_print(f"🔄 缓存未命中或无效，开始实时处理Excel文件: {commit.path}", 'EXCEL')
                diff_data = get_unified_diff_data(commit, previous_commit)
                
                # 验证新生成的数据
                if diff_data:
                    is_valid, validation_message = validate_excel_diff_data(diff_data)
                    log_print(f"🔍 新生成数据验证: {validation_message}", 'EXCEL')
                    
                    if is_valid:
                        cache_is_valid = True
                    else:
                        log_print(f"❌ 新生成的数据也无效: {validation_message}", 'EXCEL', force=True)
                else:
                    log_print(f"❌ 新数据生成失败", 'EXCEL', force=True)
                
                # 如果处理成功且验证通过，优化并立即缓存结果
                if diff_data and cache_is_valid:
                    log_print(f"💾 立即缓存Excel差异结果: {commit.path}", 'EXCEL')
                    
                    # 优化diff数据：只保留有变更的行
                    optimized_diff_data = excel_cache_service.optimize_diff_data(diff_data)
                    
                    cache_success = excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=optimized_diff_data,
                        previous_commit_id=previous_commit.commit_id if previous_commit else None,
                        processing_time=0,  # 这里可以记录实际处理时间
                        file_size=0,  # 这里可以记录文件大小
                        commit_time=commit.commit_time  # 传递提交时间
                    )
                    if cache_success:
                        log_print(f"✅ Excel差异缓存成功: {commit.path}", 'EXCEL')
                    else:
                        log_print(f"❌ Excel差异缓存失败: {commit.path}", 'EXCEL', force=True)
                        # 缓存失败时，添加到后台任务队列重试，用户点击的条目使用高优先级
                        add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                        log_print(f"已添加Excel差异缓存任务到后台队列 (高优先级): {commit.path}", 'EXCEL')
                else:
                    log_print(f"❌ 缓存条件不满足，跳过缓存", 'CACHE', force=True)
                
                # 如果新服务失败，尝试使用旧的Excel处理逻辑作为备用
                # 只有在diff_data为None或空时才使用备用逻辑，不要检查type字段
                if not diff_data:
                    log_print("使用旧的Excel处理逻辑作为备用", 'EXCEL')
                    git_service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, active_git_processes)
                    diff_data = git_service.parse_excel_diff(commit.commit_id, commit.path)
                    log_print(f"旧Excel处理逻辑返回: {type(diff_data)}", 'EXCEL')
                
        except Exception as e:
            log_print(f"Excel diff generation failed: {e}", 'EXCEL', force=True)
            import traceback
            traceback.print_exc()
            diff_data = None
            
        # 清理diff_data中的不可序列化值
        if diff_data:
            diff_data = clean_json_data(diff_data)
        
        # 调试日志：确认模板变量
        log_print(f"🔍 模板变量调试: is_excel=True, diff_data存在={diff_data is not None}", 'EXCEL', force=True)
        if diff_data:
            log_print(f"🔍 diff_data类型: {type(diff_data)}, 键: {list(diff_data.keys()) if isinstance(diff_data, dict) else 'N/A'}", 'EXCEL', force=True)
        
        # 构建模板上下文
        template_context = {
            'commit': commit,
            'repository': repository,
            'project': project,
            'diff_data': diff_data,
            'file_commits': file_commits,
            'previous_commit': previous_commit,
            'is_excel': True,
            'is_deleted': False
        }
        
        log_print(f"🔍 模板上下文键: {list(template_context.keys())}", 'EXCEL', force=True)
        log_print(f"🔍 is_excel值: {template_context['is_excel']}, 类型: {type(template_context['is_excel'])}", 'EXCEL', force=True)
        
        return render_template('commit_diff.html', **template_context)
    else:
        # 非Excel文件，正常同步处理
        diff_data = get_diff_data(commit)
        
        return render_template('commit_diff.html', 
                             commit=commit, 
                             repository=repository,
                             project=project,
                             diff_data=diff_data,
                             file_commits=file_commits,
                             previous_commit=previous_commit,
                             is_excel=False,
                             is_deleted=False)


# 确认/拒绝提交（旧版本，已被新的API替代）

# 重新生成Diff缓存
@app.route('/repositories/<int:repository_id>/regenerate-cache', methods=['POST'])
def regenerate_cache(repository_id):
    """重新生成指定仓库的Excel文件差异缓存"""
    try:
        repository = Repository.query.get_or_404(repository_id)
        
        # 直接在这里获取Excel提交数量并添加任务
        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        task_count = len(recent_commits)
        
        if task_count > 0:
            # 删除现有的所有缓存数据
            DiffCache.query.filter_by(repository_id=repository_id).delete()
            
            # 删除HTML缓存数据
            ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete()
            
            db.session.commit()
            log_print(f"已清理仓库 {repository_id} 的所有缓存数据", 'INFO')
            
            # 为每个提交添加处理任务
            for commit in recent_commits:
                add_excel_diff_task(repository_id, commit.commit_id, commit.path, priority=15)  # 批量重建使用低优先级
            
            message = f'已将 {task_count} 个Excel文件差异放入缓存队列，正在后台处理中...'
            
            # 记录重新生成缓存操作
            excel_cache_service.log_cache_operation(f"🔄 重新生成缓存: 仓库 {repository.name}, 任务数量 {task_count}", 'info')
        else:
            message = f'仓库 {repository.name} 最近2周内没有Excel文件提交，无需重新生成缓存。'
        
        return jsonify({
            'success': True, 
            'message': message,
            'task_count': task_count
        })
        
    except Exception as e:
        log_print(f"重新生成缓存失败: {e}", 'INFO')
        return jsonify({
            'success': False, 
            'message': f'重新生成缓存失败: {str(e)}'
        }), 500

# 获取缓存状态
@app.route('/repositories/<int:repository_id>/cache-status')
def get_cache_status(repository_id):
    """获取仓库的缓存状态"""
    try:
        repository = Repository.query.get_or_404(repository_id)
        
        # 统计缓存数据
        total_cache = DiffCache.query.filter_by(repository_id=repository_id).count()
        completed_cache = DiffCache.query.filter_by(
            repository_id=repository_id, 
            cache_status='completed'
        ).count()
        failed_cache = DiffCache.query.filter_by(
            repository_id=repository_id, 
            cache_status='failed'
        ).count()
        processing_cache = DiffCache.query.filter_by(
            repository_id=repository_id, 
            cache_status='processing'
        ).count()
        
        # 获取最近1000条提交中的Excel文件数量
        recent_commits = excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        total_excel_commits = len(recent_commits)
        
        return jsonify({
            'success': True,
            'repository_name': repository.name,
            'total_cache': total_cache,
            'completed_cache': completed_cache,
            'failed_cache': failed_cache,
            'processing_cache': processing_cache,
            'total_excel_commits': total_excel_commits,
            'cache_coverage': f"{completed_cache}/{total_excel_commits}" if total_excel_commits > 0 else "0/0"
        })
        
    except Exception as e:
        log_print(f"获取缓存状态失败: {e}", 'INFO')
        return jsonify({
            'success': False, 
            'message': f'获取缓存状态失败: {str(e)}'
        }), 500

# 重试克隆仓库
@app.route('/repositories/<int:repository_id>/retry-clone', methods=['POST'])
def retry_clone_repository(repository_id):
    repository = Repository.query.get_or_404(repository_id)
    
    if repository.type != 'git':
        flash('只支持Git仓库的克隆重试', 'error')
        return redirect(url_for('repository_config', project_id=repository.project_id))

    project_id = repository.project_id
    
    # 启动后台线程进行重试克隆
    retry_thread = threading.Thread(target=enhanced_retry_clone_repository, args=(repository_id,), daemon=True)
    retry_thread.start()
    
    flash('已启动仓库克隆重试，请稍后查看状态', 'success')
    return redirect(url_for('repository_config', project_id=project_id))

# 同步仓库提交记录
@app.route('/repositories/<int:repository_id>/sync', methods=['POST'])
def sync_repository(repository_id):
    """手动获取数据 - 立即执行git pull和分析"""
    try:
        log_print(f"🚀 [MANUAL_SYNC] 手动同步开始 - 仓库ID: {repository_id}", 'INFO')
        
        # 获取仓库信息
        repository = db.session.get(Repository, repository_id)
        if not repository:
            return jsonify({'status': 'error', 'message': '仓库不存在'}), 404
        
        log_print(f"📂 [MANUAL_SYNC] 仓库信息: {repository.name} ({repository.type})")
        
        if repository.type == 'git':
            git_service = get_git_service(repository)
            
            # 立即执行git pull操作
            log_print(f"🔄 [MANUAL_SYNC] 开始git pull操作...", 'INFO')
            success, message = git_service.clone_or_update_repository()
            if not success:
                log_print(f"❌ [MANUAL_SYNC] Git操作失败: {message}", 'INFO')
                return jsonify({'status': 'error', 'message': f'Git操作失败: {message}'}), 500
            
            log_print(f"✅ [MANUAL_SYNC] Git操作成功: {message}", 'INFO')
            
            # 获取数据库中最新的提交时间，用于增量同步
            latest_commit = Commit.query.filter_by(repository_id=repository_id)\
                .order_by(Commit.commit_time.desc()).first()
            
            since_date = None
            if latest_commit and latest_commit.commit_time:
                since_date = latest_commit.commit_time
                log_print(f"🔍 [MANUAL_SYNC] 从最新提交时间开始增量同步: {since_date}", 'INFO')
            else:
                log_print(f"🔍 [MANUAL_SYNC] 首次同步，获取最近800个提交", 'INFO')
            
            # 检查仓库配置的起始日期限制
            repository = Repository.query.get(repository_id)
            if repository and repository.start_date:
                if since_date is None or since_date < repository.start_date:
                    since_date = repository.start_date
                    log_print(f"🔍 [MANUAL_SYNC] 应用仓库配置的起始日期限制: {since_date}", 'INFO')
            
            # 获取提交记录 - 增量或限制数量，使用多线程优化版本
            limit = 800 if not since_date else 1000  # 首次同步限制800个，增量同步最多1000个
            import time
            start_time = time.time()
            commits = git_service.get_commits_threaded(since_date=since_date, limit=limit)
            end_time = time.time()
            log_print(f"⚡ [THREADED_GIT] 多线程获取提交记录耗时: {(end_time - start_time):.2f}秒, 提交数: {len(commits)}", 'GIT')
            log_print(f"🔍 [MANUAL_SYNC] 获取到 {len(commits)} 个提交记录")
            
            commits_added = 0
            excel_tasks_added = 0
            for i, commit_data in enumerate(commits):
                # 检查提交是否已存在
                existing_commit = Commit.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_data['commit_id']
                ).first()
                
                if not existing_commit:
                    # 创建新的提交记录
                    new_commit = Commit(
                        repository_id=repository_id,
                        commit_id=commit_data['commit_id'],
                        author=commit_data.get('author', ''),
                        message=commit_data.get('message', ''),
                        commit_time=commit_data.get('commit_time'),
                        path=commit_data.get('path', ''),
                        version=commit_data.get('version', commit_data['commit_id'][:8]),
                        operation=commit_data.get('operation', 'M'),
                        status='pending'
                    )
                    db.session.add(new_commit)
                    commits_added += 1
                    log_print(f"➕ [MANUAL_SYNC] 添加新提交 {i+1}/{len(commits)}: {commit_data['commit_id'][:8]}")
                    
                    # 检查是否为Excel文件，如果是则添加到diff缓存任务队列
                    if excel_cache_service.is_excel_file(commit_data.get('path', '')):
                        add_excel_diff_task(
                            repository_id, 
                            commit_data['commit_id'], 
                            commit_data.get('path', ''), 
                            priority=10,
                            auto_commit=False  # 不自动提交，避免会话冲突
                        )
                        excel_tasks_added += 1
                        log_print(f"📊 [MANUAL_SYNC] 添加Excel缓存任务: {commit_data.get('path', '')}")
                else:
                    log_print(f"⏭️ [MANUAL_SYNC] 跳过已存在提交 {i+1}/{len(commits)}: {commit_data['commit_id'][:8]}")
            
            # 提交数据库更改
            db.session.commit()
            log_print(f"✅ [MANUAL_SYNC] 手动同步完成，添加了 {commits_added} 个新提交，{excel_tasks_added} 个Excel缓存任务", 'INFO')
            
            return jsonify({
                'status': 'success', 
                'message': f'同步成功，添加了 {commits_added} 个新提交',
                'commits_added': commits_added
            }), 200
            
        elif repository.type == 'svn':
            svn_service = get_svn_service(repository)
            # 传入数据库模块避免循环导入
            commits_added = svn_service.sync_repository_commits(db, Commit)

            return jsonify({
                'status': 'success',
                'message': f'同步成功，添加了 {commits_added} 个新提交',
                'commits_added': commits_added
            }), 200
        else:
            return jsonify({'status': 'error', 'message': f'不支持的仓库类型: {repository.type}'}), 400
            
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        log_print(f"❌ [MANUAL_SYNC] 手动同步失败: {str(e)}")
        log_print(f"错误详情: {error_details}", 'INFO')
        return jsonify({'status': 'error', 'message': f'同步失败: {str(e)}'}), 500

def run_repository_update_and_cache(repository_id):
    """异步执行仓库更新和缓存（线程安全：按ID重新查询对象）"""
    try:
        with app.app_context():
            repository = db.session.get(Repository, repository_id)
            if not repository:
                log_print(f"❌ 异步更新失败：仓库不存在 {repository_id}", 'API', force=True)
                return

            if repository.type == 'git':
                service = get_git_service(repository)
                success, message = service.clone_or_update_repository()
                log_print(f"Git更新结果: {success}, {message}", 'GIT')
            elif repository.type == 'svn':
                service = get_svn_service(repository)
                success, message = service.checkout_or_update_repository()
                log_print(f"SVN更新结果: {success}, {message}", 'SVN')
            else:
                log_print(f"不支持的仓库类型: {repository.type}", 'API', force=True)
                return

            if success:
                log_print("仓库更新成功，开始触发缓存操作...", 'CACHE')
                commits_added = service.sync_repository_commits(db, Commit)
                log_print(f"{repository.type.upper()} 同步完成，添加了 {commits_added} 个新提交", 'SYNC')
                log_print(f"✅ 仓库 {repository.name} 更新和缓存完成", 'CACHE')
            else:
                log_print(f"❌ 仓库 {repository.name} 更新失败: {message}", 'API', force=True)
    except Exception as e:
        log_print(f"❌ 异步更新和缓存操作异常: {e}", 'API', force=True)
        import traceback
        traceback.print_exc()


@app.route('/api/repositories/<int:repository_id>/reuse-and-update', methods=['POST'])
def reuse_repository_and_update(repository_id):
    """复用仓库并触发更新和缓存操作的API接口"""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', 'pull_and_cache')

        repository = Repository.query.get_or_404(repository_id)
        log_print(f"🔄 收到仓库复用更新请求: {repository.name} (ID: {repository_id})", 'API')

        update_thread = threading.Thread(target=run_repository_update_and_cache, args=(repository_id,), daemon=True)
        update_thread.start()

        return jsonify({
            'success': True,
            'message': f'仓库 {repository.name} 更新和缓存任务已启动',
            'repository_id': repository_id,
            'action': action
        })

    except Exception as e:
        log_print(f"❌ 仓库更新API异常: {e}", 'API', force=True)
        return jsonify({
            'success': False,
            'message': f'更新失败: {str(e)}'
        }), 500

def check_local_repository_exists(project_code, repository_name, repository_id):
    """检查本地仓库是否存在"""
    try:
        local_path = build_repository_local_path(project_code, repository_name, repository_id, strict=False)
    except (TypeError, ValueError):
        return False
    return os.path.exists(local_path)

@app.route('/commits/<int:commit_id>/status', methods=['POST'])
def update_commit_status(commit_id):
    """更新提交状态"""
    try:
        data = request.get_json(silent=True) or {}
        status = data.get('status')

        # 兼容历史前端：action=confirm/reject
        if not status:
            action = (data.get('action') or request.form.get('action') or request.form.get('status') or '').strip()
            action_to_status = {
                'confirm': 'confirmed',
                'confirmed': 'confirmed',
                'approve': 'confirmed',
                'reject': 'rejected',
                'rejected': 'rejected',
                'pending': 'pending',
                'reviewed': 'reviewed',
            }
            status = action_to_status.get(action, action)

        if status not in ['pending', 'reviewed', 'confirmed', 'rejected']:
            return jsonify({'status': 'error', 'message': '无效的状态值'}), 400

        commit = Commit.query.get_or_404(commit_id)
        old_status = commit.status
        commit.status = status
        db.session.commit()

        # 同步状态到周版本diff
        if old_status != status:
            from services.status_sync_service import StatusSyncService
            sync_service = StatusSyncService(db)
            sync_result = sync_service.sync_commit_to_weekly(commit_id, status)
            log_print(f"提交状态同步结果: {sync_result}", 'SYNC')

        return jsonify({'success': True, 'message': '状态更新成功'})

    except Exception as e:
        app.logger.error(f"更新提交状态失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/commits/batch-update', methods=['POST'])
def batch_update_commits_compat():
    """兼容历史前端的批量更新接口（batch-approve/batch-reject 的统一入口）"""
    try:
        data = request.get_json(silent=True) or {}
        commit_ids = data.get('commit_ids') or data.get('ids') or request.form.getlist('ids')
        action = (data.get('action') or request.form.get('action') or '').strip().lower()

        if not commit_ids:
            return jsonify({'status': 'error', 'message': '未选择任何提交'}), 400

        # 标准化commit_ids为int列表
        normalized_ids = []
        for raw_id in commit_ids:
            try:
                normalized_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        if not normalized_ids:
            return jsonify({'status': 'error', 'message': '提交ID无效'}), 400

        if action in {'confirm', 'confirmed', 'approve'}:
            target_status = 'confirmed'
        elif action in {'reject', 'rejected'}:
            target_status = 'rejected'
        else:
            return jsonify({'status': 'error', 'message': '不支持的批量操作'}), 400

        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)
        updated_count = 0
        sync_results = []

        for commit_id in normalized_ids:
            commit = db.session.get(Commit, commit_id)
            if commit and commit.status != target_status:
                commit.status = target_status
                updated_count += 1
                sync_results.append(sync_service.sync_commit_to_weekly(commit_id, target_status))

        db.session.commit()
        total_weekly_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))
        return jsonify({
            'status': 'success',
            'message': f'已更新 {updated_count} 个提交，同步更新 {total_weekly_updated} 个周版本记录',
            'updated_count': updated_count
        })
    except Exception as e:
        db.session.rollback()
        log_print(f"批量更新提交失败: {str(e)}", 'APP', force=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/commits/<int:commit_id>/approve-all', methods=['POST'])
def approve_all_files(commit_id):
    """批量确认提交的所有文件"""
    try:
        commit = Commit.query.get_or_404(commit_id)
        log_print(f"批量确认: 当前提交ID={commit.id}, commit_id={commit.commit_id}, repository_id={commit.repository_id}", 'INFO')
        
        # 获取同一次提交的所有文件（通过commit_id匹配）
        related_commits = Commit.query.filter_by(
            repository_id=commit.repository_id,
            commit_id=commit.commit_id
        ).all()
        
        log_print(f"找到 {len(related_commits)} 个相关提交:")
        for rc in related_commits:
            log_print(f"  - ID={rc.id}, path={rc.path}, 当前状态={rc.status}", 'INFO')
        
        # 将所有相关提交状态设为已确认
        updated_count = 0
        for related_commit in related_commits:
            if related_commit.status != 'confirmed':
                related_commit.status = 'confirmed'
                updated_count += 1
                log_print(f"  更新提交 {related_commit.id} 状态为 confirmed", 'INFO')
        
        db.session.commit()
        log_print(f"批量确认完成，更新了 {updated_count} 个文件", 'INFO')
        
        return jsonify({
            'status': 'success', 
            'message': f'已确认 {len(related_commits)} 个文件 (更新了 {updated_count} 个)'
        })
        
    except Exception as e:
        log_print(f"批量确认失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/commits/batch-approve', methods=['POST'])
def batch_approve_commits():
    """批量通过选中的提交"""
    try:
        data = request.get_json()
        commit_ids = data.get('commit_ids', [])

        if not commit_ids:
            return jsonify({'status': 'error', 'message': '未选择任何提交'}), 400

        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)

        updated_count = 0
        sync_results = []

        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if commit and commit.status != 'confirmed':
                old_status = commit.status
                commit.status = 'confirmed'
                updated_count += 1

                # 同步状态到周版本diff
                sync_result = sync_service.sync_commit_to_weekly(commit_id, 'confirmed')
                sync_results.append(sync_result)

        db.session.commit()

        # 统计同步结果
        total_weekly_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))

        return jsonify({
            'status': 'success',
            'message': f'已通过 {updated_count} 个提交，同步更新了 {total_weekly_updated} 个周版本记录'
        })

    except Exception as e:
        log_print(f"批量通过失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/commits/batch-reject', methods=['POST'])
def batch_reject_commits():
    """批量拒绝选中的提交"""
    try:
        data = request.get_json()
        commit_ids = data.get('commit_ids', [])
        
        if not commit_ids:
            return jsonify({'status': 'error', 'message': '未选择任何提交'}), 400
        
        from services.status_sync_service import StatusSyncService
        sync_service = StatusSyncService(db)

        updated_count = 0
        sync_results = []

        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if commit and commit.status != 'rejected':
                old_status = commit.status
                commit.status = 'rejected'
                updated_count += 1

                # 同步状态到周版本diff
                sync_result = sync_service.sync_commit_to_weekly(commit_id, 'rejected')
                sync_results.append(sync_result)

        db.session.commit()

        # 统计同步结果
        total_weekly_updated = sum(r.get('updated_count', 0) for r in sync_results if r.get('success'))

        return jsonify({
            'status': 'success',
            'message': f'已拒绝 {updated_count} 个提交，同步更新了 {total_weekly_updated} 个周版本记录'
        })
        
    except Exception as e:
        log_print(f"批量拒绝失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/commits/reject', methods=['POST'])
def reject_commit():
    """拒绝单个提交"""
    try:
        data = request.get_json()
        commit_id = data.get('commit_id')
        
        if not commit_id:
            return jsonify({'status': 'error', 'message': '未指定提交ID'}), 400
        
        commit = db.session.get(Commit, commit_id)
        if not commit:
            return jsonify({'status': 'error', 'message': '提交不存在'}), 404
        
        if commit.status != 'rejected':
            commit.status = 'rejected'
            db.session.commit()
            
            return jsonify({
                'status': 'success',
                'message': '提交已拒绝'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': '提交已经是拒绝状态'
            })
        
    except Exception as e:
        log_print(f"拒绝提交失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/excel-cache/logs')
def get_excel_cache_logs():
    """获取Excel缓存操作日志"""
    try:
        # 获取分页参数
        page = max(1, request.args.get('page', 1, type=int) or 1)
        per_page = request.args.get('per_page', 10, type=int) or 10

        # 限制每页最大数量和总页数，确保最多返回200条
        per_page = min(max(per_page, 1), 50)  # 每页1~50条
        max_total = 200

        # 只允许访问最近max_total条日志，避免全量all()造成内存和查询压力
        total_logs_raw = OperationLog.query.count()
        total_logs = min(total_logs_raw, max_total)
        offset = (page - 1) * per_page

        if offset >= total_logs:
            paginated_logs_db = []
        else:
            fetch_size = min(per_page, total_logs - offset)
            paginated_logs_db = (
                OperationLog.query
                .order_by(OperationLog.created_at.desc())
                .offset(offset)
                .limit(fetch_size)
                .all()
            )

        # 转换为前端需要的格式，使用北京时间
        from utils.timezone_utils import format_beijing_time
        logs = []
        for log in paginated_logs_db:
            logs.append({
                'time': format_beijing_time(log.created_at, '%Y/%m/%d %H:%M:%S'),
                'message': log.message,
                'type': log.log_type
            })
       
        # 计算总页数
        total_pages = (total_logs + per_page - 1) // per_page
        
        return jsonify({
            'success': True,
            'logs': logs,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total_logs,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_prev': page > 1
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'获取日志失败: {str(e)}'
        }), 500

@app.route('/api/excel-html-cache/clear')
def clear_excel_html_cache():
    """清理Excel HTML缓存（版本检查）"""
    try:
        repository_id = request.args.get('repository_id', type=int)
        force_all = request.args.get('force_all', 'false').lower() == 'true'
        
        if force_all:
            # 强制清理所有HTML缓存
            from sqlalchemy import text
            result = db.session.execute(text('DELETE FROM excel_html_cache'))
            count = result.rowcount
            db.session.commit()
            log_print(f"🧹 强制清理了所有 {count} 个HTML缓存", 'INFO')
        else:
            # 只清理旧版本缓存
            count = excel_html_cache_service.cleanup_old_version_cache()
        
        return jsonify({
            'success': True,
            'message': f'清理了 {count} 个{"所有" if force_all else "旧版本"}HTML缓存',
            'cleared_count': count
        })
        
    except Exception as e:
        log_print(f"❌ 清理HTML缓存失败: {e}", 'INFO')
        return jsonify({'success': False, 'message': f'清理失败: {str(e)}'})

# 删除重复的路由定义

@app.route('/api/excel-html-cache/regenerate')
def regenerate_excel_html_cache():
    """重新生成Excel HTML缓存"""
    try:
        repository_id = request.args.get('repository_id', type=int)
        commit_id = request.args.get('commit_id')
        file_path = request.args.get('file_path')
        
        if not all([repository_id, commit_id, file_path]):
            return jsonify({'success': False, 'message': '缺少必要参数'})
        
        # 删除现有HTML缓存
        existing_cache = ExcelHtmlCache.query.filter_by(
            repository_id=repository_id,
            commit_id=commit_id,
            file_path=file_path
        ).first()
        
        if existing_cache:
            db.session.delete(existing_cache)
            db.session.commit()
            log_print(f"🗑️ 删除现有HTML缓存: {file_path}", 'INFO')
        
        # 触发重新生成（通过调用Excel diff数据接口）
        commit = Commit.query.filter_by(
            repository_id=repository_id,
            commit_id=commit_id,
            path=file_path
        ).first()
        
        if commit:
            # 这会触发HTML缓存的重新生成
            result = get_excel_diff_data(commit.id)
            return jsonify({
                'success': True,
                'message': f'HTML缓存重新生成完成: {file_path}',
                'regenerated': True
            })
        else:
            return jsonify({'success': False, 'message': '找不到对应的提交记录'})
            
    except Exception as e:
        log_print(f"❌ 重新生成HTML缓存失败: {e}", 'INFO')
        return jsonify({'success': False, 'message': f'重新生成失败: {str(e)}'})

@app.route('/commits/<int:commit_id>/priority-diff', methods=['POST'])
def request_priority_diff(commit_id):
    """请求优先处理指定提交的diff"""
    try:
        commit = Commit.query.get_or_404(commit_id)
        repository = commit.repository
        
        # 检查是否为Excel文件
        if not excel_cache_service.is_excel_file(commit.path):
            return jsonify({
                'success': False, 
                'message': '该文件不是Excel文件，无需优先处理'
            })
        
        # 检查是否已有缓存
        cached_diff = excel_cache_service.get_cached_diff(
            repository.id, commit.commit_id, commit.path
        )
        
        if cached_diff:
            return jsonify({
                'success': True, 
                'message': '该文件已有缓存，无需重新处理',
                'cached': True
            })
        
        # 添加到高优先级队列
        add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
        
        return jsonify({
            'success': True, 
            'message': f'已将 {commit.path} 添加到高优先级处理队列',
            'cached': False,
            'queue_size': background_task_queue.qsize()
        })
        
    except Exception as e:
        log_print(f"请求优先处理失败: {e}", 'INFO')
        return jsonify({
            'success': False, 
            'message': f'请求失败: {str(e)}'
        })

@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/priority-diff', methods=['POST'])
def request_priority_diff_with_path(project_code, repository_name, commit_id):
    """请求优先处理指定提交的diff (带路径版本)"""
    return request_priority_diff(commit_id)

@app.route('/api/excel-diff-status/<cache_key>')
def excel_diff_status(cache_key):
    """旧版状态接口（已废弃）"""
    try:
        return jsonify({
            'status': 'deprecated',
            'message': '该接口已废弃，请改用 /commits/<commit_id>/diff-data 与缓存管理接口查询状态。',
            'cache_key': cache_key
        }), 410

    except Exception as e:
        log_print(f"检查Excel diff状态失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

# 合并diff重新计算路由
@app.route('/commits/<int:commit_id>/diff-data', methods=['GET'])
def get_commit_diff_data(commit_id):
    """异步获取单个提交的diff数据（优化版本，优先使用缓存）"""
    try:
        start_time = time.time()
        commit = db.session.get(Commit, commit_id)
        if not commit:
            return jsonify({'success': False, 'message': '提交不存在'})

        repository = commit.repository
        is_excel = excel_cache_service.is_excel_file(commit.path)

        # 获取前一个提交用于diff对比
        previous_commit = Commit.query.filter(
            Commit.repository_id == commit.repository_id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()

        diff_data = None

        # 如果是Excel文件，优先检查缓存
        if is_excel:
            log_print(f"🔍 合并diff异步请求Excel文件: {commit.path}", 'CACHE')

            # 检查Excel diff缓存
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path
            )

            if cached_diff:
                log_print(f"✅ 缓存命中，避免重复计算: {commit.path} | 耗时: {time.time() - start_time:.2f}秒", 'CACHE')
                try:
                    diff_data = json.loads(cached_diff.diff_data)
                    log_print(f"🔍 缓存数据解析成功: type={diff_data.get('type')}, sheets={len(diff_data.get('sheets', {}))}", 'CACHE')
                except Exception as parse_error:
                    log_print(f"❌ 缓存数据解析失败: {parse_error}", 'CACHE')
                    log_print(f"🔍 原始缓存数据前100字符: {cached_diff.diff_data[:100]}", 'CACHE')
                    diff_data = None
            else:
                log_print(f"❌ 缓存未命中，开始实时计算: {commit.path}", 'CACHE')
                # 使用统一的diff数据获取方法
                diff_data = get_unified_diff_data(commit, previous_commit)
        else:
            # 非Excel文件，直接计算
            diff_data = get_unified_diff_data(commit, previous_commit)

        if diff_data:
            # 清理diff数据中的NaN和Infinity值
            import math
            def sanitize_data(obj):
                if isinstance(obj, dict):
                    return {k: sanitize_data(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [sanitize_data(item) for item in obj]
                elif isinstance(obj, float):
                    if math.isnan(obj) or math.isinf(obj):
                        return None
                    return obj
                else:
                    return obj

            diff_data = sanitize_data(diff_data)

            total_time = time.time() - start_time
            log_print(f"✅ 合并diff异步请求完成: {commit.path} | 总耗时: {total_time:.2f}秒", 'PERF')

            return jsonify({
                'success': True,
                'commit_id': commit_id,
                'diff_data': diff_data,
                'previous_commit': {
                    'commit_id': previous_commit.commit_id[:8] if previous_commit else 'N/A',
                    'commit_time': previous_commit.commit_time.strftime('%Y-%m-%d %H:%M:%S') if previous_commit and previous_commit.commit_time else 'N/A',
                    'author': previous_commit.author if previous_commit else 'N/A',
                    'message': previous_commit.message if previous_commit else 'N/A'
                } if previous_commit else None
            })
        else:
            total_time = time.time() - start_time
            log_print(f"❌ 合并diff异步请求失败: {commit.path} | 耗时: {total_time:.2f}秒", 'PERF')
            return jsonify({
                'success': False,
                'commit_id': commit_id,
                'message': '无法获取diff数据'
            })

    except Exception as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 获取提交 {commit_id} 的diff数据失败: {e} | 耗时: {total_time:.2f}秒", 'ERROR')
        return jsonify({
            'success': False,
            'commit_id': commit_id,
            'message': f'获取diff数据失败: {str(e)}'
        })

@app.route('/commits/merge-diff/refresh', methods=['POST'])
def refresh_merge_diff():
    """重新计算合并diff数据，绕过缓存"""
    try:
        log_print("🔄 开始处理合并diff重新计算请求", 'APP')
        commit_ids = request.json.get('commit_ids', [])
        log_print(f"📋 收到提交ID: {commit_ids}", 'APP')

        if not commit_ids:
            log_print("❌ 未提供提交ID", 'INFO')
            return jsonify({'success': False, 'message': '未提供提交ID'})

        commits = []
        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if commit:
                commits.append(commit)
                log_print(f"✅ 找到提交: {commit_id} - {commit.path}", 'INFO')

        if not commits:
            log_print("❌ 未找到有效的提交记录", 'INFO')
            return jsonify({'success': False, 'message': '未找到有效的提交记录'})
        
        # 临时暂停后台缓存任务，避免冲突
        log_print("🔄 临时暂停后台缓存任务处理...", 'INFO')
        from services.background_task_service import pause_background_tasks
        pause_background_tasks()
        
        # 优化的批量缓存清理逻辑
        cleared_count = 0
        cache_clear_start = time.time()
        cache_clear_time = 0.0

        # 批量收集所有需要删除的缓存条件
        diff_cache_conditions = []
        html_cache_conditions = []

        for commit in commits:
            if excel_cache_service.is_excel_file(commit.path):
                log_print(f"🔄 准备清除缓存: {commit.path}", 'INFO')
                diff_cache_conditions.append((commit.repository_id, commit.commit_id, commit.path))
                html_cache_conditions.append((commit.repository_id, commit.commit_id, commit.path))
                cleared_count += 1

        if diff_cache_conditions:
            # 批量删除diff缓存
            total_diff_deleted = 0
            for repo_id, commit_id, file_path in diff_cache_conditions:
                deleted_count = DiffCache.query.filter(
                    DiffCache.repository_id == repo_id,
                    DiffCache.commit_id == commit_id,
                    DiffCache.file_path == file_path
                ).delete(synchronize_session=False)
                total_diff_deleted += deleted_count

            # 批量删除HTML缓存 - 直接在这里执行，避免函数调用开销
            total_html_deleted = 0
            for repo_id, commit_id, file_path in html_cache_conditions:
                html_deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repo_id,
                    commit_id=commit_id,
                    file_path=file_path
                ).delete(synchronize_session=False)
                total_html_deleted += html_deleted_count

            # 一次性提交所有删除操作
            db.session.commit()
            cache_clear_time = time.time() - cache_clear_start
            log_print(f"✅ 批量清除缓存完成: diff={total_diff_deleted}, html={total_html_deleted} | 耗时: {cache_clear_time:.2f}秒", 'INFO')
        else:
            cache_clear_time = time.time() - cache_clear_start
            log_print(f"ℹ️ 没有找到需要清除的Excel文件缓存", 'INFO')
        
        # 恢复后台缓存任务处理
        log_print("🔄 恢复后台缓存任务处理...", 'INFO')
        from services.background_task_service import resume_background_tasks
        resume_background_tasks()
        
        return jsonify({
            'success': True,
            'message': f'已清除 {cleared_count} 个文件的缓存，缓存清理耗时 {cache_clear_time:.2f} 秒，请刷新页面查看重新计算的结果',
            'cleared_count': cleared_count,
            'cache_clear_time': cache_clear_time
        })
        
    except Exception as e:
        log_print(f"重新计算合并diff失败: {e}", 'INFO')
        return jsonify({'success': False, 'message': f'重新计算失败: {str(e)}'})

@app.route('/commits/merge-diff')
def merge_diff():
    """合并选中条目的diff显示页面"""
    log_print("🚨🚨🚨 ROUTE CALLED! /commits/merge-diff 🚨🚨🚨", 'APP')
    try:
        commit_ids = request.args.getlist('ids')
        
        if not commit_ids:
            flash('未选择任何提交', 'error')
            return redirect(request.referrer or url_for('index'))
        
        commits = []
        for commit_id in commit_ids:
            commit = db.session.get(Commit, commit_id)
            if commit:
                commits.append(commit)
        
        if not commits:
            flash('未找到有效的提交记录', 'error')
            return redirect(request.referrer or url_for('index'))
        
        # 按提交时间排序
        commits.sort(key=lambda x: x.commit_time or datetime.min, reverse=False)  # 升序排列，最早的在前
        
        # 获取项目和仓库信息
        repository = commits[0].repository
        project = repository.project
        
        # 检查是否为同一文件的连续提交
        log_print(f"=== 开始调用get_merged_diff_data ===", 'APP')
        log_print(f"📋 提交ID列表: {commit_ids}", 'APP')
        log_print(f"📊 提交数量: {len(commits)}", force=True)
        for i, commit in enumerate(commits):
            log_print(f"  {i+1}. {commit.commit_id[:8]} - {commit.path}", 'APP')
        
        # 添加异常处理来防止Socket错误中断处理
        try:
            merged_diff_data = get_merged_diff_data(commits)
            log_print(f"=== get_merged_diff_data调用完成 ===", 'APP')
            log_print(f"merged_diff_data结果: {merged_diff_data is not None}", 'INFO')
            if merged_diff_data:
                log_print(f"merged_diff_data类型: {merged_diff_data.get('type', 'INFO')}")
                log_print(f"merged_diff_data键: {list(merged_diff_data.keys())}", 'INFO')
            log_print(f"=== 结束get_merged_diff_data调试 ===", 'APP')
        except Exception as merge_error:
            log_print(f"❌ get_merged_diff_data处理失败: {str(merge_error)}", 'INFO', force=True)
            import traceback
            traceback.print_exc()
            flash(f'合并diff处理失败: {str(merge_error)}', 'error')
            return redirect(request.referrer or url_for('index'))
        
    except Exception as route_error:
        log_print(f"❌ 合并diff路由处理失败: {str(route_error)}", force=True)
        import traceback
        traceback.print_exc()
        flash(f'页面处理失败: {str(route_error)}', 'error')
        return redirect(request.referrer or url_for('index'))
    
    log_print(f"合并diff页面调试信息:", 'INFO')
    log_print(f"- 提交数量: {len(commits)}")
    log_print(f"- 提交ID: {[c.id for c in commits]}", 'INFO')
    log_print(f"- 文件路径: {[c.path for c in commits]}", 'INFO')
    commit_times = []
    for c in commits:
        try:
            if c.commit_time:
                commit_times.append(c.commit_time.strftime('%Y-%m-%d %H:%M:%S'))
            else:
                commit_times.append('None')
        except Exception as e:
            commit_times.append(f'Error: {str(e)}')
    log_print(f"- 提交时间: {commit_times}", 'INFO')
    log_print(f"- 合并diff数据: {merged_diff_data is not None}", 'INFO')
    if merged_diff_data:
        log_print(f"- 合并diff类型: {merged_diff_data.get('type', 'INFO')}")
        log_print(f"- 合并diff键: {list(merged_diff_data.keys())}", 'INFO')
        if merged_diff_data.get('type') == 'excel':
            log_print(f"- Excel工作表数量: {len(merged_diff_data.get('sheets', {}))}", 'INFO')
            if merged_diff_data.get('sheets'):
                for sheet_name, sheet_data in merged_diff_data.get('sheets', {}).items():
                    log_print(f"  - 工作表 '{sheet_name}': {sheet_data.get('status', 'unknown')}, 行数: {len(sheet_data.get('rows', []))}")
                    if sheet_data.get('rows'):
                        log_print(f"    - 第一行示例: {sheet_data['rows'][0] if sheet_data['rows'] else 'None'}", 'INFO')
        else:
            log_print(f"- hunks数量: {len(merged_diff_data.get('hunks', []))}", 'INFO')
    else:
        log_print("- 合并diff数据为None，将使用传统逐个显示方式", 'INFO')
    
    # 智能构建显示列表：合并连续提交，分离不同文件
    commits_with_diff = build_smart_display_list(commits)

    # 计算缓存状态
    cache_status_summary = {'cached': 0, 'uncached': 0, 'total': len(commits_with_diff)}
    for item in commits_with_diff:
        if item.get('cache_available', False):
            cache_status_summary['cached'] += 1
        else:
            cache_status_summary['uncached'] += 1

    log_print(f"commits_with_diff 数量: {len(commits_with_diff)} (异步加载模式)")
    log_print(f"缓存状态: {cache_status_summary['cached']}/{cache_status_summary['total']} 已缓存", 'CACHE')
    return render_template('merge_diff.html',
                         commits=commits,
                         commits_with_diff=commits_with_diff,
                         merged_diff_data=merged_diff_data,
                         project=project,
                         repository=repository,
                         commit_ids=commit_ids)

# 编辑仓库页面

@app.route('/update_commit_fields')
def update_commit_fields_route():
    """更新现有提交记录中缺失的version和operation字段"""
    try:
        # 查找version或operation为None的记录
        commits_to_update = Commit.query.filter(
            (Commit.version.is_(None)) | (Commit.operation.is_(None))
        ).all()
        
        updated_count = 0
        for commit in commits_to_update:
            # 更新version字段（使用commit_id的前8位）
            if commit.version is None:
                commit.version = commit.commit_id[:8] if commit.commit_id else 'unknown'
            
            # 更新operation字段（默认为修改）
            if commit.operation is None:
                commit.operation = 'M'  # 默认为修改
            
            updated_count += 1
        
        # 提交更改
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'成功更新 {updated_count} 条提交记录',
            'updated_count': updated_count
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'更新失败: {str(e)}'
        }), 500

@app.route('/repositories/<int:repository_id>/edit')
def edit_repository(repository_id):
    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    
    if repository.type == 'git':
        return render_template('add_git_repository.html', 
                             project=project, 
                             repository=repository, 
                             is_edit=True)
    else:
        return render_template('add_svn_repository.html', 
                             project=project, 
                             repository=repository, 
                             is_edit=True)

# 更新仓库配置 - 表单提交处理
@app.route('/repositories/<int:repository_id>/update', methods=['POST'])
@require_admin
def update_repository(repository_id):
    """处理仓库编辑表单提交"""
    repository = Repository.query.get_or_404(repository_id)
    project_id = repository.project_id

    try:
        # 保存旧的仓库名称，用于重命名目录
        old_name = repository.name
        new_name = (request.form.get('name') or '').strip()

        if not validate_repository_name(new_name):
            flash('仓库名称仅允许字母、数字、点、下划线和短横线', 'error')
            return redirect(url_for('edit_repository', repository_id=repository_id))

        # 保存旧的文件类型过滤器，用于检测是否需要重新筛选
        old_file_type_filter = repository.path_regex if repository.type == 'git' else None

        # 更新仓库信息
        repository.name = new_name
        repository.category = request.form.get('category')
        repository.resource_type = request.form.get('resource_type')
        repository.display_order = int(request.form.get('display_order', 0))

        # 通用字段更新 - 注意：模板中使用的是file_type_filter字段名
        repository.path_regex = request.form.get('file_type_filter') or request.form.get('path_regex')  # 修正字段名
        repository.log_regex = request.form.get('log_regex')
        repository.log_filter_regex = request.form.get('log_filter_regex')
        repository.commit_filter = request.form.get('commit_filter')
        repository.important_tables = request.form.get('important_tables')
        repository.unconfirmed_history = bool(request.form.get('unconfirmed_history'))
        repository.delete_table_alert = bool(request.form.get('delete_table_alert'))
        repository.weekly_version_setting = request.form.get('weekly_version_setting')

        # Table配置字段
        header_rows = request.form.get('header_rows')
        repository.header_rows = int(header_rows) if header_rows else None
        repository.key_columns = request.form.get('key_columns')
        repository.enable_id_confirmation = bool(request.form.get('enable_id_confirmation'))
        repository.show_duplicate_id_warning = bool(request.form.get('show_duplicate_id_warning'))
        repository.tag_selection = request.form.get('tag_selection')

        # 根据仓库类型更新特定字段
        if repository.type == 'git':
            repository.url = request.form.get('url')
            repository.server_url = request.form.get('server_url')
            new_token = (request.form.get('token') or '').strip()
            if new_token:
                repository.token = new_token
            repository.branch = request.form.get('branch')
            repository.enable_webhook = 'enable_webhook' in request.form
            repository.show_latest_id = 'show_latest_id' in request.form
            repository.table_name_column = request.form.get('table_name_column')

            # Git日期范围配置
            current_date = request.form.get('current_date')
            if current_date:
                try:
                    from datetime import datetime
                    repository.start_date = datetime.strptime(current_date, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    try:
                        repository.start_date = datetime.strptime(current_date, '%Y-%m-%d')
                    except ValueError:
                        flash('日期格式错误，请使用 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD 格式', 'error')
                        return redirect(url_for('edit_repository', repository_id=repository_id))

        elif repository.type == 'svn':
            repository.url = request.form.get('url')
            repository.username = request.form.get('username')
            new_password = (request.form.get('password') or '').strip()
            if new_password:
                repository.password = new_password

        # 提交数据库更改
        db.session.commit()

        # 检查是否需要异步触发重新筛选
        need_refilter = repository.type == 'git' and old_file_type_filter != repository.path_regex

        if need_refilter:
            log_print(f"文件类型过滤器已更新: '{old_file_type_filter}' -> '{repository.path_regex}'", 'APP')

            # 在主线程中获取必要的数据，避免跨线程访问SQLAlchemy对象
            repository_id = repository.id
            new_path_regex = repository.path_regex

            # 异步触发重新筛选
            import threading
            def async_refilter():
                try:
                    log_print("开始异步重新筛选仓库内容...", 'APP')
                    with app.app_context():
                        # 重新获取repository对象（在新的应用上下文中）
                        repo = Repository.query.get(repository_id)
                        if not repo:
                            log_print(f"❌ 未找到仓库ID: {repository_id}", 'APP', force=True)
                            return

                        # 先清理不符合新过滤规则的数据库记录
                        if new_path_regex:
                            import re
                            try:
                                pattern = re.compile(new_path_regex)
                                log_print(f"开始清理不符合过滤规则的记录: {new_path_regex}", 'APP')

                                # 查找不符合新规则的提交记录
                                all_commits = Commit.query.filter_by(repository_id=repository_id).all()
                                commits_to_delete = []

                                for commit in all_commits:
                                    if commit.path and not pattern.match(commit.path):
                                        commits_to_delete.append(commit)

                                if commits_to_delete:
                                    log_print(f"找到 {len(commits_to_delete)} 个不符合规则的提交记录，开始清理...", 'APP')

                                    # 删除相关的diff缓存
                                    for commit in commits_to_delete:
                                        DiffCache.query.filter_by(
                                            repository_id=repository_id,
                                            commit_id=commit.commit_id,
                                            file_path=commit.path
                                        ).delete()

                                    # 删除提交记录
                                    for commit in commits_to_delete:
                                        db.session.delete(commit)

                                    db.session.commit()
                                    log_print(f"已清理 {len(commits_to_delete)} 个不符合规则的记录", 'APP')
                                else:
                                    log_print("没有找到需要清理的记录", 'APP')

                            except re.error as e:
                                log_print(f"正则表达式编译失败: {e}", 'APP', force=True)

                        # 然后重新同步符合新规则的内容
                        try:
                            from incremental_cache_system import IncrementalCacheManager
                            cache_system = IncrementalCacheManager()
                            success, message = cache_system.force_full_sync(repository_id)
                            if not success:
                                log_print(f"❌ 全量同步失败: {message}", 'APP', force=True)
                            else:
                                log_print("✅ 全量同步成功", 'APP')
                        except Exception as sync_e:
                            log_print(f"❌ 全量同步异常: {str(sync_e)}", 'APP', force=True)

                    log_print("仓库内容重新筛选完成", 'APP')
                except Exception as e:
                    log_print(f"重新筛选仓库内容时出错: {str(e)}", 'APP', force=True)
                    import traceback
                    log_print(f"详细错误信息: {traceback.format_exc()}", 'APP', force=True)

            # 启动后台线程执行重新筛选
            thread = threading.Thread(target=async_refilter, daemon=True)
            thread.start()

            # 立即返回，并在session中设置提示消息
            flash('仓库设置已保存，正在后台重新筛选文件，请稍后查看提交列表。', 'info')
        else:
            flash(f'仓库 "{repository.name}" 更新成功', 'success')

        return redirect(url_for('repository_config', project_id=project_id))

    except Exception as e:
        db.session.rollback()
        flash(f'更新仓库失败: {str(e)}', 'error')
        return redirect(url_for('edit_repository', repository_id=repository_id))

# 更新仓库配置 - API接口
@app.route('/repositories/<int:repository_id>/update-api', methods=['POST'])
def update_repository_and_cache(repository_id):
    """更新仓库并触发缓存操作的API接口"""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', 'pull_and_cache')
        
        repository = Repository.query.get_or_404(repository_id)
        log_print(f"🔄 收到仓库更新请求: {repository.name} (ID: {repository_id})", 'API')

        # 启动后台线程执行更新和缓存
        update_thread = threading.Thread(target=run_repository_update_and_cache, args=(repository_id,), daemon=True)
        update_thread.start()
        
        return jsonify({
            'success': True,
            'message': f'仓库 {repository.name} 更新和缓存任务已启动',
            'repository_id': repository_id,
            'action': action
        })
        
    except Exception as e:
        log_print(f"❌ 仓库更新API异常: {e}", 'API', force=True)
        return jsonify({
            'success': False,
            'message': f'更新失败: {str(e)}'
        }), 500

# 批量更新仓库凭据
@app.route('/repositories/batch-update-credentials', methods=['POST'])
@require_admin
def batch_update_credentials():
    """批量更新项目下的仓库凭据"""
    try:
        data = request.get_json()
        project_id = data.get('project_id')
        repo_type = data.get('repo_type')
        
        if not project_id or not repo_type:
            return jsonify({'status': 'error', 'message': '缺少必要参数'}), 400
        
        # 查询项目下指定类型的所有仓库
        repositories = Repository.query.filter_by(project_id=project_id, type=repo_type).all()
        
        if not repositories:
            return jsonify({'status': 'error', 'message': f'项目下没有找到{repo_type.upper()}仓库'}), 404
        
        updated_count = 0
        
        if repo_type == 'git':
            git_token = data.get('git_token')
            if not git_token:
                return jsonify({'status': 'error', 'message': '缺少Git Token'}), 400
            
            # 更新所有Git仓库的token
            for repo in repositories:
                repo.token = git_token
                updated_count += 1
                
        elif repo_type == 'svn':
            svn_username = data.get('svn_username')
            svn_password = data.get('svn_password')
            if not svn_username or not svn_password:
                return jsonify({'status': 'error', 'message': '缺少SVN用户名或密码'}), 400
            
            # 更新所有SVN仓库的用户名和密码
            for repo in repositories:
                repo.username = svn_username
                repo.password = svn_password
                updated_count += 1
        
        else:
            return jsonify({'status': 'error', 'message': '不支持的仓库类型'}), 400
        
        # 提交数据库更改
        db.session.commit()
        
        return jsonify({
            'status': 'success', 
            'message': f'成功更新{updated_count}个{repo_type.upper()}仓库',
            'updated_count': updated_count
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"批量更新仓库凭据失败: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# 更新仓库排序
@app.route('/repositories/update-order', methods=['POST'])
def update_repository_order():
    try:
        data = request.get_json()
        repo_id = data.get('repo_id')
        new_order = data.get('new_order')
        project_id = data.get('project_id')
        
        if not repo_id or new_order is None or not project_id:
            return jsonify({'status': 'error', 'message': '缺少必要参数'}), 400
        
        # 获取项目下的所有仓库，按当前顺序排列
        repositories = Repository.query.filter_by(project_id=project_id).order_by(Repository.display_order.asc()).all()
        
        # 找到要移动的仓库
        target_repo = None
        for repo in repositories:
            if repo.id == repo_id:
                target_repo = repo
                break
        
        if not target_repo:
            return jsonify({'status': 'error', 'message': '仓库不存在'}), 404
        
        # 重新排列仓库顺序
        repositories.remove(target_repo)
        repositories.insert(new_order, target_repo)
        
        # 更新所有仓库的display_order
        for index, repo in enumerate(repositories):
            repo.display_order = index
        
        db.session.commit()
        return jsonify({'status': 'success', 'message': '仓库排序更新成功'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/repositories/swap-order', methods=['POST'])
def swap_repository_order():
    try:
        data = request.get_json()
        first_repo_id = data.get('first_repo_id')
        second_repo_id = data.get('second_repo_id')
        project_id = data.get('project_id')
        
        if not first_repo_id or not second_repo_id or not project_id:
            return jsonify({'status': 'error', 'message': '缺少必要参数'}), 400
        
        if first_repo_id == second_repo_id:
            return jsonify({'status': 'error', 'message': '不能选择相同的仓库'}), 400
        
        # 获取两个仓库
        first_repo = Repository.query.filter_by(id=first_repo_id, project_id=project_id).first()
        second_repo = Repository.query.filter_by(id=second_repo_id, project_id=project_id).first()
        
        if not first_repo or not second_repo:
            return jsonify({'status': 'error', 'message': '仓库不存在或不属于该项目'}), 404
        
        # 交换display_order
        first_order = first_repo.display_order
        second_order = second_repo.display_order
        
        first_repo.display_order = second_order
        second_repo.display_order = first_order
        
        # 提交数据库更改
        db.session.commit()
        
        return jsonify({
            'status': 'success', 
            'message': f'成功交换仓库 {first_repo.name} 和 {second_repo.name} 的顺序'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

# 删除仓库
@app.route('/repositories/<int:repository_id>/delete', methods=['POST'])
@require_admin
def delete_repository(repository_id):
    repository = Repository.query.get_or_404(repository_id)
    project_id = repository.project_id
    repo_name = repository.name
    
    # 获取本地仓库路径（Git和SVN都使用相同的命名规则）
    local_path = build_repository_local_path(
        repository.project.code,
        repository.name,
        repository.id,
        strict=False
    )
    
    log_print(f"开始删除仓库 {repo_name} (ID: {repository_id})", 'DELETE')
    
    # 完整清理所有相关数据，避免外键约束错误
    try:
        # 1. 删除后台任务队列中的相关任务
        background_tasks_deleted = BackgroundTask.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {background_tasks_deleted} 个BackgroundTask记录", 'DELETE')
        
        # 2. 删除DiffCache记录
        diff_cache_deleted = DiffCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {diff_cache_deleted} 个DiffCache记录", 'DELETE')
        
        # 3. 删除ExcelHtmlCache记录
        excel_cache_deleted = ExcelHtmlCache.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {excel_cache_deleted} 个ExcelHtmlCache记录", 'DELETE')

        # 4. 删除MergedDiffCache记录
        try:
            merged_cache_deleted = MergedDiffCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {merged_cache_deleted} 个MergedDiffCache记录", 'DELETE')
        except Exception as e:
            log_print(f"删除MergedDiffCache记录时出错（可能是表结构问题）: {e}", 'DELETE')
            # 如果表结构有问题，尝试直接执行SQL删除
            try:
                db.session.execute("DELETE FROM merged_diff_cache WHERE repository_id = :repo_id", {"repo_id": repository_id})
                log_print(f"通过SQL成功删除MergedDiffCache记录", 'DELETE')
            except Exception as sql_e:
                log_print(f"SQL删除MergedDiffCache记录也失败: {sql_e}", 'DELETE')

        # 5. 删除WeeklyVersionDiffCache记录
        try:
            weekly_cache_deleted = WeeklyVersionDiffCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {weekly_cache_deleted} 个WeeklyVersionDiffCache记录", 'DELETE')
        except Exception as e:
            log_print(f"删除WeeklyVersionDiffCache记录时出错: {e}", 'DELETE')

        # 6. 删除WeeklyVersionExcelCache记录
        try:
            weekly_excel_cache_deleted = WeeklyVersionExcelCache.query.filter_by(repository_id=repository_id).delete()
            log_print(f"删除了 {weekly_excel_cache_deleted} 个WeeklyVersionExcelCache记录", 'DELETE')
        except Exception as e:
            log_print(f"删除WeeklyVersionExcelCache记录时出错: {e}", 'DELETE')

        # 7. 删除Commit记录
        commit_deleted = Commit.query.filter_by(repository_id=repository_id).delete()
        log_print(f"删除了 {commit_deleted} 个Commit记录", 'DELETE')
        
        # 8. 清空仓库的增量缓存同步字段
        repository.last_sync_commit_id = None
        repository.last_sync_time = None
        repository.cache_version = None
        repository.sync_mode = 'full'
        log_print(f"清空了仓库 {repo_name} 的增量缓存同步字段", 'DELETE')

        # 9. 删除仓库记录
        db.session.delete(repository)
        db.session.commit()
        log_print(f"成功删除仓库 {repo_name} 的所有数据库记录", 'DELETE')
        
        flash(f'仓库 {repo_name} 及其所有关联数据已成功删除', 'success')
        
    except Exception as e:
        db.session.rollback()
        log_print(f"删除仓库失败: {str(e)}", 'ERROR')
        flash(f'删除仓库失败: {str(e)}', 'error')
        return redirect(url_for('repository_config', project_id=project_id))
    
    # 10. 尝试删除本地仓库目录
    delete_local_repository_directory(local_path, repo_name)
    
    return redirect(url_for('repository_config', project_id=project_id))

def delete_local_repository_directory(local_path, repo_name):
    """删除本地仓库目录，使用多种策略确保删除成功"""
    def delete_directory():
        try:
            if not os.path.exists(local_path):
                log_print(f"ℹ️ 目录不存在，无需删除: {local_path}", 'DELETE')
                return
            
            # 策略1: 标准shutil.rmtree删除
            success = try_standard_delete(local_path, repo_name)
            if success:
                return
            
            # 策略2: 移除只读属性后删除
            success = try_remove_readonly_and_delete(local_path, repo_name)
            if success:
                return
            
            # 策略3: 使用Windows命令删除
            success = try_windows_command_delete(local_path, repo_name)
            if success:
                return
            
            # 策略4: 强制删除（PowerShell）
            success = try_powershell_force_delete(local_path, repo_name)
            if success:
                return
            
            # 所有策略都失败，记录到待删除列表
            log_print(f"❌ 所有删除策略都失败: {local_path}", 'DELETE')
            record_pending_deletion(local_path, repo_name)
            
        except Exception as e:
            log_print(f"❌ 删除目录过程中发生异常: {local_path} | 错误: {str(e)}", 'DELETE')
            record_pending_deletion(local_path, repo_name)
    
    # 在后台线程中执行删除操作，避免阻塞主线程
    delete_thread = threading.Thread(target=delete_directory)
    delete_thread.daemon = True
    delete_thread.start()

def try_standard_delete(local_path, repo_name):
    """策略1: 标准shutil.rmtree删除"""
    try:
        import shutil
        shutil.rmtree(local_path, ignore_errors=False)
        if not os.path.exists(local_path):
            log_print(f"✅ 标准删除成功: {local_path} (仓库: {repo_name})", 'DELETE')
            return True
        else:
            log_print(f"⚠️ 标准删除不完整: {local_path}", 'DELETE')
            return False
    except Exception as e:
        log_print(f"⚠️ 标准删除失败: {local_path} | 错误: {str(e)}", 'DELETE')
        return False

def try_remove_readonly_and_delete(local_path, repo_name):
    """策略2: 移除只读属性后删除"""
    try:
        import subprocess
        import shutil
        # 移除只读属性
        subprocess.run(['attrib', '-R', f'{local_path}\\*.*', '/S', '/D'], 
                      capture_output=True, check=False)
        
        # 再次尝试删除
        shutil.rmtree(local_path, ignore_errors=False)
        if not os.path.exists(local_path):
            log_print(f"✅ 移除只读属性后删除成功: {local_path} (仓库: {repo_name})", 'DELETE')
            return True
        else:
            log_print(f"⚠️ 移除只读属性后删除不完整: {local_path}", 'DELETE')
            return False
    except Exception as e:
        log_print(f"⚠️ 移除只读属性后删除失败: {local_path} | 错误: {str(e)}", 'DELETE')
        return False

def try_windows_command_delete(local_path, repo_name):
    """策略3: 使用Windows rmdir命令删除"""
    try:
        import subprocess
        result = subprocess.run(['rmdir', '/s', '/q', local_path], 
                              capture_output=True, text=True, check=False)
        
        if not os.path.exists(local_path):
            log_print(f"✅ Windows命令删除成功: {local_path} (仓库: {repo_name})", 'DELETE')
            return True
        else:
            log_print(f"⚠️ Windows命令删除失败: {local_path} | 错误: {result.stderr}", 'DELETE')
            return False
    except Exception as e:
        log_print(f"⚠️ Windows命令删除异常: {local_path} | 错误: {str(e)}", 'DELETE')
        return False

def try_powershell_force_delete(local_path, repo_name):
    """策略4: 使用PowerShell强制删除"""
    try:
        import subprocess
        ps_command = f'Remove-Item -Path "{local_path}" -Recurse -Force -ErrorAction SilentlyContinue'
        result = subprocess.run(['powershell', '-Command', ps_command], 
                              capture_output=True, text=True, check=False)
        
        if not os.path.exists(local_path):
            log_print(f"✅ PowerShell强制删除成功: {local_path} (仓库: {repo_name})", 'DELETE')
            return True
        else:
            log_print(f"⚠️ PowerShell强制删除失败: {local_path}", 'DELETE')
            return False
    except Exception as e:
        log_print(f"⚠️ PowerShell强制删除异常: {local_path} | 错误: {str(e)}", 'DELETE')
        return False

def record_pending_deletion(local_path, repo_name):
    """记录待删除的目录到文件"""
    try:
        pending_file = 'pending_deletions.txt'
        with open(pending_file, 'a', encoding='utf-8') as f:
            f.write(f"{local_path}|{repo_name}|{datetime.now().isoformat()}\n")
        log_print(f"已记录待删除目录: {local_path}", 'REPO')
    except Exception as e:
        log_print(f"记录待删除目录失败: {e}", 'REPO', force=True)

def cleanup_pending_deletions():
    """清理待删除的仓库目录"""
    import shutil
    import threading
    
    def cleanup_directories():
        try:
            pending_file = 'pending_deletions.txt'
            if not os.path.exists(pending_file):
                return
            
            # 读取待删除目录列表
            with open(pending_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            if not lines:
                return
            
            log_print(f"发现 {len(lines)} 个待删除目录，开始清理...", 'REPO')
            
            remaining_lines = []
            deleted_count = 0
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                try:
                    parts = line.split('|')
                    if len(parts) < 3:
                        continue
                    
                    local_path = parts[0]
                    repo_name = parts[1]
                    timestamp = parts[2]
                    
                    if os.path.exists(local_path):
                        try:
                            shutil.rmtree(local_path, ignore_errors=False)
                            if not os.path.exists(local_path):
                                log_print(f"成功删除目录: {local_path} (仓库: {repo_name})", 'REPO')
                                deleted_count += 1
                            else:
                                # 删除失败，保留到文件中
                                remaining_lines.append(line)
                                log_print(f"目录删除不完整，保留待下次处理: {local_path}", 'REPO')
                        except PermissionError:
                            # 权限错误，保留到文件中
                            remaining_lines.append(line)
                            log_print(f"权限不足，无法删除目录: {local_path}", 'REPO')
                        except Exception as e:
                            # 其他错误，保留到文件中
                            remaining_lines.append(line)
                            log_print(f"删除目录失败: {local_path}, 错误: {e}", 'REPO')
                    else:
                        # 目录已不存在，无需处理
                        log_print(f"目录已不存在，跳过: {local_path}", 'REPO')
                        deleted_count += 1
                        
                except Exception as e:
                    # 解析行失败，保留原行
                    remaining_lines.append(line)
                    log_print(f"解析待删除记录失败: {line}, 错误: {e}", 'REPO')
            
            # 更新待删除文件
            if remaining_lines:
                with open(pending_file, 'w', encoding='utf-8') as f:
                    for line in remaining_lines:
                        f.write(line + '\n')
                log_print(f"清理完成，成功删除 {deleted_count} 个目录，剩余 {len(remaining_lines)} 个待处理", 'REPO')
            else:
                # 删除待删除文件
                os.remove(pending_file)
                log_print(f"清理完成，成功删除所有 {deleted_count} 个目录", 'REPO')
                
        except Exception as e:
            log_print(f"清理待删除目录过程失败: {e}", 'REPO', force=True)
    
    # 异步执行清理，避免阻塞应用启动
    cleanup_thread = threading.Thread(target=cleanup_directories, daemon=True)
    cleanup_thread.start()

# 测试仓库连接
@app.route('/repositories/<int:repository_id>/test', methods=['POST'])
def test_repository(repository_id):
    repository = Repository.query.get_or_404(repository_id)
    
    try:
        log_print(f"测试仓库连接: {repository.name}", 'TEST')
        log_print(f"仓库类型: {repository.type}", 'TEST')
        log_print(f"仓库URL: {repository.url}", 'TEST')
        log_print(f"分支: {repository.branch}", 'TEST')
        log_print(f"Token: {'已设置' if repository.token else '未设置'}", 'TEST')
        
        if repository.type == 'git':
            # 为合并diff使用缓存的Git服务实例
            service = get_git_service(repository)
            log_print(f"本地路径: {service.local_path}", 'TEST')
            
            # 先测试SSH连接（如果是SSH URL）
            ssh_test_result = service.test_ssh_connection()
            log_print(f"SSH连接测试结果: {ssh_test_result}", 'TEST')
            
            if not ssh_test_result:
                flash('SSH连接测试失败，请检查网络连接和SSH配置', 'warning')
                # SSH连接失败时不再进行克隆测试，避免重复提示
            else:
                # 只有SSH连接成功时才进行克隆测试
                success, message = service.clone_or_update_repository()
                if success:
                    flash(f'仓库连接测试成功: {message}', 'success')
                else:
                    flash(f'仓库连接测试失败: {message}', 'error')
        else:
            flash('暂时只支持Git仓库测试', 'warning')
            
    except Exception as e:
        log_print(f"测试过程中发生错误: {str(e)}", 'TEST', force=True)
        import traceback
        traceback.print_exc()
        flash(f'测试失败: {str(e)}', 'error')
    
    return redirect(url_for('repository_config', project_id=repository.project_id))

# 删除项目
@app.route('/projects/<int:project_id>/delete', methods=['POST'])
@require_admin
def delete_project(project_id):
    project = Project.query.get_or_404(project_id)
    
    db.session.delete(project)
    db.session.commit()
    flash('项目删除成功', 'success')
    return redirect(url_for('index'))

@app.context_processor
def inject_template_functions():
    """注入模板函数"""
    return dict(
        get_diff_data=get_diff_data,
        generate_commit_diff_url=generate_commit_diff_url,
        generate_excel_diff_data_url=generate_excel_diff_data_url,
        generate_refresh_diff_url=generate_refresh_diff_url
    )

def get_diff_data(commit):
    """获取真实的diff数据 - 返回数据结构而非JSON响应"""
    try:
        repository = commit.repository

        # 查找前一个相同文件路径的提交，与 commit_full_diff 路由保持一致
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc()).first()

        # 如果按时间未找到，尝试按ID查找
        if previous_commit is None:
            previous_commit = Commit.query.filter(
                Commit.repository_id == repository.id,
                Commit.path == commit.path,
                Commit.id < commit.id
            ).order_by(Commit.id.desc()).first()

        log_print(f"🔍 get_diff_data - 当前提交: {commit.commit_id[:8]} ({commit.commit_time})", 'DIFF')
        if previous_commit:
            log_print(f"🔍 get_diff_data - 前一提交: {previous_commit.commit_id[:8]} ({previous_commit.commit_time})", 'DIFF')
        else:
            log_print(f"🔍 get_diff_data - 无前一提交，这是初始提交", 'DIFF')

        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            # 为合并diff使用独立的线程池，避免与后台任务冲突
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, active_git_processes)

            if commit.path and (commit.path.endswith('.xlsx') or commit.path.endswith('.xls')):
                try:
                    log_print(f"开始处理commit {commit.commit_id}的Excel diff数据...", 'EXCEL')
                    # 对于Excel文件，暂时保持原有逻辑，后续可以优化
                    # TODO: 实现Excel文件的前一提交比较逻辑
                    diff_data = service.parse_excel_diff(commit.commit_id, commit.path)

                    # 打印性能统计
                    if hasattr(service, 'performance_stats'):
                        log_print(f"Excel处理性能统计: {service.performance_stats}", 'EXCEL')

                    return diff_data
                except Exception as e:
                    log_print(f"获取commit {commit.commit_id} Excel diff数据时出错: {str(e)}", 'EXCEL', force=True)
                    import traceback
                    traceback.print_exc()
                    return {'error': str(e)}
            else:
                log_print(f"开始处理commit {commit.commit_id}的代码文件diff数据: {commit.path}", 'INFO')
                # 使用相同文件路径的前一提交进行比较
                if previous_commit:
                    diff_data = service.get_commit_range_diff(previous_commit.commit_id, commit.commit_id, commit.path)
                else:
                    # 初始提交，显示整个文件作为新增
                    diff_data = service.get_file_diff(commit.commit_id, commit.path)

                # log_print(f"Git服务返回的diff数据类型: {type(diff_data)}, 内容: {diff_data}")

                if diff_data and diff_data.get('hunks'):
                    # 保持原始hunks格式，添加file_path信息
                    diff_data['file_path'] = commit.path
                    # 输出性能统计
                    stats = service.get_performance_stats()
                    log_print(f"Git diff处理性能统计: {stats}", 'INFO')
                    log_print(f"成功获取diff数据，hunks数量: {len(diff_data.get('hunks', []))}", 'INFO')
                    return diff_data
                else:
                    log_print(f"未能获取到有效的diff数据，返回模拟数据", 'INFO')
                    mock_data = get_mock_diff_data(commit)
                    log_print(f"模拟数据结构: {mock_data}", 'INFO')
                    return mock_data
        
        elif repository.type == 'svn':
            service = get_svn_service(repository)

            if commit.path and (commit.path.endswith('.xlsx') or commit.path.endswith('.xls')):
                # 使用统一的Excel diff处理逻辑
                return get_unified_diff_data(commit, None)
            else:
                diff_data = service.get_file_diff(commit.version, commit.path)
                if diff_data and diff_data.get('hunks'):
                    # 保持原始hunks格式，添加file_path信息
                    diff_data['file_path'] = commit.path
                    return diff_data
        
        # 如果无法获取真实数据，返回模拟数据
        log_print(f"无法获取真实diff数据，返回模拟数据", 'INFO')
        return get_mock_diff_data(commit)
        
    except Exception as e:
        log_print(f"获取真实diff数据失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return get_mock_diff_data(commit)

def get_real_diff_data_for_merge(commit):
    """获取用于合并显示的diff数据（lines格式）"""
    try:
        log_print(f"开始获取提交{commit.id}的diff数据: {commit.path}", 'INFO')
        repository = commit.repository
        
        # 检查是否为Excel文件
        is_excel = excel_cache_service.is_excel_file(commit.path)
        log_print(f"- 是否Excel文件: {is_excel}", 'INFO')
        
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            # 为合并diff使用独立的线程池，避免与后台任务冲突
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, active_git_processes)
            
            if is_excel:
                log_print(f"- 处理Excel文件，优先检查缓存", 'INFO')
                
                # 强制刷新数据库会话以确保读取最新数据
                db.session.expire_all()
                cached_diff = excel_cache_service.get_cached_diff(
                    repository.id, commit.commit_id, commit.path
                )
                
                if cached_diff:
                    log_print(f"- 从缓存获取Excel差异数据", 'INFO')
                    log_print(f"- 缓存版本: {cached_diff.diff_version} | 缓存时间: {cached_diff.created_at}", 'INFO')
                    log_print(f"- 缓存更新时间: {cached_diff.updated_at}", 'INFO')
                    log_print(f"- 缓存ID: {cached_diff.id}", 'INFO')
                    # 从缓存对象中提取实际的diff数据
                    import json
                    excel_diff = json.loads(cached_diff.diff_data)
                    log_print(f"- 解析后的Excel diff数据类型: {excel_diff.get('type', 'INFO') if excel_diff else 'None'}")
                    if excel_diff and excel_diff.get('sheets'):
                        log_print(f"- 解析后的工作表数量: {len(excel_diff['sheets'])}")
                        first_sheet_name = list(excel_diff['sheets'].keys())[0]
                        first_sheet_data = list(excel_diff['sheets'].values())[0]
                        log_print(f"  - 工作表 '{first_sheet_name}': {first_sheet_data.get('status', 'unknown')}, 行数: {len(first_sheet_data.get('rows', []))}")
                    else:
                        log_print(f"- ❌ 解析后的Excel diff数据无工作表", 'INFO')
                    
                    # 合并diff用户主动请求，添加高优先级缓存任务确保数据最新
                    add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                    log_print(f"✅ 合并diff添加高优先级缓存任务: {commit.path}", 'CACHE')
                else:
                    log_print(f"- 缓存未命中，调用Git Excel diff解析", 'INFO')
                    excel_diff = service.parse_excel_diff(commit.commit_id, commit.path)
                    
                    # 缓存未命中时，立即添加高优先级缓存任务
                    add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                    log_print(f"✅ 合并diff缓存未命中，添加高优先级缓存任务: {commit.path}", 'CACHE')
                    log_print(f"- Excel工作表列表: {list(excel_diff.get('sheets', {}).keys())}")
                    # 打印第一个工作表的结构用于调试
                    if excel_diff.get('sheets'):
                        first_sheet_name = list(excel_diff['sheets'].keys())[0]
                        first_sheet = excel_diff['sheets'][first_sheet_name]
                        log_print(f"- 第一个工作表 '{first_sheet_name}' 结构: {list(first_sheet.keys())}", 'INFO')
                        if 'rows' in first_sheet:
                            log_print(f"- 工作表行数: {len(first_sheet['rows'])}")
                
                # 在返回前清理数据中的NaN值
                if excel_diff:
                    log_print(f"- 开始清理Excel diff数据中的NaN值...", 'APP')
                    try:
                        excel_diff = clean_json_data(excel_diff)
                        log_print(f"- Excel diff数据清理完成", 'APP')
                    except Exception as clean_error:
                        log_print(f"- ❌ Excel diff数据清理失败: {str(clean_error)}", force=True)
                        import traceback
                        traceback.print_exc()
                
                log_print(f"- 准备返回Excel diff数据，类型: {type(excel_diff)}", force=True)
                log_print(f"- 即将执行return语句...", 'APP')
                result = excel_diff
                log_print(f"- return语句执行完成，返回值: {result is not None}", 'APP')
                
                # 延迟清理线程池，避免中断后台任务
                import threading
                def delayed_cleanup():
                    import time
                    time.sleep(2)  # 延迟2秒清理
                    if hasattr(service, 'cleanup_thread_pool'):
                        service.cleanup_thread_pool()
                        log_print(f"- 延迟清理合并diff线程池完成", 'APP')
                
                cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
                cleanup_thread.start()
                
                return result
            else:
                log_print(f"- 调用Git文本diff解析", 'INFO')
                diff_data = service.get_file_diff(commit.commit_id, commit.path)
                if diff_data and diff_data.get('hunks'):
                    # 将hunks格式转换为模板期望的lines格式
                    return convert_hunks_to_lines(diff_data)
        
        elif repository.type == 'svn':
            service = get_svn_service(repository)

            if is_excel:
                log_print(f"- 处理SVN Excel文件，使用统一diff处理逻辑", 'INFO')
                # 使用统一的Excel diff处理逻辑
                excel_diff = get_unified_diff_data(commit, None)

                # 清理数据中的NaN值
                if excel_diff:
                    try:
                        excel_diff = clean_json_data(excel_diff)
                        log_print(f"- SVN Excel diff数据清理完成", 'APP')
                    except Exception as clean_error:
                        log_print(f"- ❌ SVN Excel diff数据清理失败: {str(clean_error)}", force=True)

                return excel_diff
            else:
                log_print(f"- 调用SVN文本diff解析", 'INFO')
                diff_data = service.get_file_diff(commit.version, commit.path)
                if diff_data and diff_data.get('hunks'):
                    # 将hunks格式转换为模板期望的lines格式
                    return convert_hunks_to_lines(diff_data)
        
        # 如果无法获取真实数据，返回模拟数据
        log_print(f"无法获取真实diff数据，返回模拟数据", 'INFO')
        return get_mock_diff_data(commit)
        
    except Exception as e:
        log_print(f"获取合并diff数据失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return get_mock_diff_data(commit)

def get_merged_diff_data(commits):
    """增强的智能合并diff数据处理"""
    if not commits:
        return None
    
    log_print(f"=== 增强合并diff处理开始 ===", 'INFO')
    log_print(f"提交数量: {len(commits)}")
    
    # 按文件路径分组提交
    from collections import defaultdict
    file_groups = defaultdict(list)
    for commit in commits:
        file_groups[commit.path].append(commit)
    
    log_print(f"文件分组数量: {len(file_groups)}")
    for file_path, file_commits in file_groups.items():
        log_print(f"  - {file_path}: {len(file_commits)}个提交")
    
    # 情况1&4: 不同文件的合并diff（包括混合情况）
    if len(file_groups) > 1:
        log_print("✓ 检测到情况1/4: 不同文件的合并diff（包括混合情况）", 'INFO')
        return handle_different_files_merge(file_groups)
    
    # 情况2和3: 同一文件的合并diff
    file_path = list(file_groups.keys())[0]
    file_commits = file_groups[file_path]
    
    # 按时间排序提交
    file_commits.sort(key=lambda x: x.commit_time)
    
    log_print(f"同一文件 {file_path} 的提交处理:", 'INFO')
    for i, commit in enumerate(file_commits):
        log_print(f"  {i+1}. {commit.commit_id[:8]} - {commit.commit_time}", 'INFO')
    
    # 检查是否为连续提交
    if are_commits_consecutive_internal(file_commits):
        log_print("✓ 检测到情况2: 相同文件连续commit的合并diff", 'INFO')
        return handle_consecutive_commits_merge_internal(file_commits)
    else:
        log_print("✓ 检测到情况3: 相同文件非连续commit的合并diff", 'INFO')
        return handle_non_consecutive_commits_merge_internal(file_commits)

def handle_different_files_merge(file_groups):
    """情况1&4: 处理不同文件的合并diff（包括混合情况）"""
    log_print("处理多文件的合并diff...", 'INFO')

    # 分析每个文件的提交情况
    merged_sections = []

    for file_path, file_commits in file_groups.items():
        log_print(f"分析文件: {file_path} ({len(file_commits)}个提交)", 'INFO')

        # 按时间排序
        file_commits.sort(key=lambda x: x.commit_time)

        if len(file_commits) == 1:
            # 单个提交，直接添加
            log_print(f"  - 单个提交: {file_commits[0].commit_id[:8]}", 'INFO')
            merged_sections.append({
                'type': 'single_commit',
                'file_path': file_path,
                'commit': file_commits[0]
            })
        else:
            # 多个提交，检查是否连续
            if are_commits_consecutive_internal(file_commits):
                # 连续提交，合并为单个diff
                log_print(f"  - 连续提交合并: {file_commits[0].commit_id[:8]}..{file_commits[-1].commit_id[:8]}", 'INFO')
                merged_sections.append({
                    'type': 'consecutive_merge',
                    'file_path': file_path,
                    'commits': file_commits,
                    'start_commit': file_commits[0],
                    'end_commit': file_commits[-1]
                })
            else:
                # 非连续提交，分别显示
                log_print(f"  - 非连续提交分别显示: {len(file_commits)}个diff", 'INFO')
                for i, commit in enumerate(file_commits):
                    merged_sections.append({
                        'type': 'individual_commit',
                        'file_path': file_path,
                        'commit': commit,
                        'sequence': i + 1
                    })

    # 返回None让系统使用传统显示模式，但记录分析结果
    log_print(f"✓ 多文件合并分析完成，共{len(merged_sections)}个显示单元，使用传统显示模式", 'INFO')
    return None
    
    diff_sections = []
    
    for file_path, file_commits in file_groups.items():
        log_print(f"处理文件: {file_path} ({len(file_commits)}个提交)", force=True)
        
        try:
            # 对每个文件的提交按时间排序
            file_commits.sort(key=lambda x: x.commit_time)
            
            # 为每个文件智能生成diff
            if len(file_commits) == 1:
                # 单个提交，与前一版本diff
                log_print(f"  - 单个提交处理: {file_commits[0].commit_id[:8]}", 'APP')
                try:
                    log_print(f"  - 调用get_unified_diff_data函数...", 'APP')
                    # 获取前一个提交用于diff对比
                    previous_commit = None
                    if len(file_commits) > 1:
                        previous_commit = file_commits[1]  # 第二个提交作为前一个提交
                    diff_data = get_unified_diff_data(file_commits[0], previous_commit)
                    log_print(f"  - 函数调用完成，返回值类型: {type(diff_data)}", force=True)
                    log_print(f"  - diff_data获取结果: {diff_data is not None}", 'APP')
                except Exception as get_error:
                    log_print(f"  - ❌ 获取diff_data时出错: {str(get_error)}", force=True)
                    import traceback
                    traceback.print_exc()
                    diff_data = None
                    continue  # 跳过这个文件，继续处理下一个
                if diff_data:
                    log_print(f"  - diff_data类型: {diff_data.get('type', 'unknown')}", force=True)
                    log_print(f"  - diff_data键: {list(diff_data.keys())}", 'INFO', force=True)
                    
                    # 检查是否为空的Excel数据
                    if diff_data.get('type') == 'excel':
                        sheets = diff_data.get('sheets', {})
                        if not sheets:
                            log_print(f"  - ⚠️ Excel文件无工作表数据，跳过: {file_path}", 'APP')
                        else:
                            has_content = False
                            for sheet_name, sheet_data in sheets.items():
                                if sheet_data.get('rows') and len(sheet_data['rows']) > 0:
                                    has_content = True
                                    break
                            if not has_content:
                                log_print(f"  - ⚠️ Excel文件所有工作表都为空，但仍添加到结果中: {file_path}", 'APP')
                    
                    # 数据已经在get_real_diff_data_for_merge中清理过了，不需要再次清理
                    log_print(f"  - 跳过JSON数据清理（已在函数内部清理）", 'APP')
                    
                    try:
                        # 安全地获取时间字符串
                        commit_time_str = None
                        try:
                            commit_time_str = file_commits[0].commit_time.isoformat()
                        except Exception as time_error:
                            log_print(f"  - ⚠️ 获取提交时间失败: {str(time_error)}", force=True)
                            commit_time_str = str(file_commits[0].commit_time)
                        
                        diff_sections.append({
                            'file_path': file_path,
                            'diff_type': 'single_commit',
                            'diff_data': diff_data,
                            'commits': [{'id': file_commits[0].commit_id, 'time': commit_time_str}],
                            'description': f"单个提交 {file_commits[0].commit_id[:8]}"
                        })
                        log_print(f"  - ✅ 成功添加diff段: {file_path}", 'APP')
                    except Exception as append_error:
                        log_print(f"  - ❌ 添加diff段时出错: {file_path} - {str(append_error)}", force=True)
                        import traceback
                        traceback.print_exc()
                else:
                    log_print(f"  - ❌ 未获取到diff数据: {file_path}", 'APP')
            else:
                # 多个提交，检查是否连续
                if are_commits_consecutive_internal(file_commits):
                    # 连续提交，合并为单个diff
                    log_print(f"  - 连续提交合并: {file_commits[0].commit_id[:8]}..{file_commits[-1].commit_id[:8]}", 'APP')
                    diff_data = handle_consecutive_commits_merge_internal(file_commits)
                    if diff_data:
                        # 清理diff_data中的NaN值
                        diff_data = clean_json_data(diff_data)
                        diff_sections.append({
                            'file_path': file_path,
                            'diff_type': 'consecutive_merge',
                            'diff_data': diff_data,
                            'commits': [{'id': c.commit_id, 'time': c.commit_time.isoformat()} for c in file_commits],
                            'description': f"连续提交合并 {file_commits[0].commit_id[:8]}..{file_commits[-1].commit_id[:8]}"
                        })
                        log_print(f"  - ✅ 成功添加连续合并diff段: {file_path}", 'APP')
                else:
                    # 非连续提交，分段显示
                    log_print(f"  - 非连续提交分段处理: {len(file_commits)}个提交", force=True)
                    diff_data = handle_non_consecutive_commits_merge_internal(file_commits)
                    if diff_data:
                        # 清理diff_data中的NaN值
                        diff_data = clean_json_data(diff_data)
                        diff_sections.append({
                            'file_path': file_path,
                            'diff_type': 'segmented',
                            'diff_data': diff_data,
                            'commits': [{'id': c.commit_id, 'time': c.commit_time.isoformat()} for c in file_commits],
                            'description': f"非连续提交分段 ({diff_data.get('total_segments', 0)}段)"
                        })
                        log_print(f"  - ✅ 成功添加分段diff段: {file_path}", 'APP')
        except Exception as e:
            log_print(f"  - ❌ 处理文件时出错: {file_path} - {str(e)}", force=True)
            import traceback
            traceback.print_exc()
    
    log_print(f"生成了 {len(diff_sections)} 个diff段", force=True)
    for i, section in enumerate(diff_sections):
        log_print(f"  段{i+1}: {section['file_path']} - {section['diff_type']} - 有数据: {section['diff_data'] is not None}", 'APP')
    
    return {
        'type': 'multiple_files',
        'sections': diff_sections,
        'total_files': len(file_groups),
        'total_sections': len(diff_sections)
    }

def handle_consecutive_commits_merge_internal(file_commits):
    """情况2: 处理相同文件连续commit的合并diff"""
    log_print("处理连续提交的合并diff...", 'INFO')
    
    earliest_commit = file_commits[0]
    latest_commit = file_commits[-1]
    
    log_print(f"最早提交: {earliest_commit.commit_id[:8]}", 'INFO')
    log_print(f"最新提交: {latest_commit.commit_id[:8]}", 'INFO')
    
    repository = earliest_commit.repository
    
    try:
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            service = ThreadedGitService(repository.url, repository.root_directory, 
                               repository.username, repository.token, repository, active_git_processes)
            
            # 检查是否为Excel文件
            is_excel = excel_cache_service.is_excel_file(earliest_commit.path)
            
            if is_excel:
                log_print(f"🔍 处理Excel连续提交合并diff", 'APP')
                log_print(f"📊 最早提交: {earliest_commit.commit_id[:8]} ({earliest_commit.path})", force=True)
                log_print(f"📊 最新提交: {latest_commit.commit_id[:8]} ({latest_commit.path})", force=True)
                
                # Excel文件需要计算从最早提交前一版本到最新提交的范围diff
                parent_commit_id = service.get_parent_commit(earliest_commit.commit_id)
                if parent_commit_id:
                    log_print(f"🎯 计算Excel范围diff: {parent_commit_id[:8]}..{latest_commit.commit_id[:8]}", 'APP')
                    # 对于Excel文件，需要创建一个虚拟的前一提交对象
                    try:
                        # 创建虚拟的前一提交对象用于范围diff计算
                        virtual_previous_commit = Commit()
                        virtual_previous_commit.commit_id = parent_commit_id
                        virtual_previous_commit.repository = repository
                        virtual_previous_commit.path = earliest_commit.path
                        
                        log_print(f"✨ 创建虚拟前一提交: {parent_commit_id[:8]} -> {latest_commit.commit_id[:8]}", 'APP')
                        diff_data = get_unified_diff_data(latest_commit, virtual_previous_commit)
                        if diff_data:
                            log_print(f"✅ Excel范围diff计算成功，数据类型: {diff_data.get('type', 'unknown')}", force=True)
                            diff_data['commit_range'] = f"{earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]}"
                            diff_data['is_merged'] = True
                            return clean_json_data(diff_data)
                        else:
                            log_print("❌ Excel范围diff计算返回空数据", 'APP')
                    except Exception as e:
                        log_print(f"❌ Excel范围diff计算异常: {e}", 'APP')
                        import traceback
                        traceback.print_exc()
                else:
                    log_print(f"❌ 无法获取最早提交的父提交: {earliest_commit.commit_id[:8]}", 'APP')
                
                # 如果范围diff失败，回退到使用最新提交的单个diff
                log_print("⚠️ 范围diff失败，回退到单个提交diff", 'APP')
                # 获取前一个提交
                previous_commit = None
                if len(file_commits) > 1:
                    previous_commit = file_commits[1]
                diff_data = get_unified_diff_data(latest_commit, previous_commit)
                return clean_json_data(diff_data) if diff_data else None
            else:
                # 文本文件获取范围diff
                parent_commit_id = service.get_parent_commit(earliest_commit.commit_id)
                if parent_commit_id:
                    diff_data = service.get_commit_range_diff(
                        parent_commit_id, latest_commit.commit_id, earliest_commit.path
                    )
                    if diff_data:
                        diff_data['commit_range'] = f"{earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]}"
                        diff_data['is_merged'] = True
                        return diff_data
        
        elif repository.type == 'svn':
            service = get_svn_service(repository)
            
            # SVN使用版本号
            parent_version = str(int(earliest_commit.version) - 1)
            diff_data = service.get_version_range_diff(
                parent_version, latest_commit.version, earliest_commit.path
            )
            if diff_data:
                diff_data['version_range'] = f"{parent_version}..{latest_commit.version}"
                diff_data['is_merged'] = True
                return diff_data
                
    except Exception as e:
        log_print(f"获取连续提交diff失败: {str(e)}")
    
    return None

def handle_non_consecutive_commits_merge_internal(file_commits):
    """情况3: 处理相同文件非连续commit的合并diff"""
    log_print("处理非连续提交的合并diff...", 'INFO')
    
    # 获取文件的完整提交历史
    repository = file_commits[0].repository
    file_path = file_commits[0].path
    
    # 查询该文件的所有提交，按时间排序
    all_file_commits = db.session.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.path == file_path
    ).order_by(Commit.commit_time.desc()).all()
    
    log_print(f"文件 {file_path} 的完整提交历史: {len(all_file_commits)}个")
    
    # 找到选中提交在历史中的位置
    selected_commit_ids = {commit.commit_id for commit in file_commits}
    selected_positions = []
    
    for i, commit in enumerate(all_file_commits):
        if commit.commit_id in selected_commit_ids:
            selected_positions.append((i, commit))
    
    log_print(f"选中提交的位置: {[pos[0] for pos in selected_positions]}", 'INFO')
    
    # 生成diff段
    diff_segments = []
    
    for i, (pos, commit) in enumerate(selected_positions):
        log_print(f"处理提交段 {i+1}: {commit.commit_id[:8]} (位置: {pos}, 'INFO')")
        
        # 找到前一个提交
        if pos + 1 < len(all_file_commits):
            previous_commit = all_file_commits[pos + 1]
            log_print(f"  前一提交: {previous_commit.commit_id[:8]}", 'INFO')
            
            # 生成这个提交与前一提交的diff
            diff_data = get_commit_pair_diff_internal(commit, previous_commit)
            if diff_data:
                diff_data['segment_info'] = {
                    'current': commit.commit_id[:8],
                    'previous': previous_commit.commit_id[:8],
                    'segment_index': i + 1,
                    'total_segments': len(selected_positions)
                }
                diff_segments.append(diff_data)
        else:
            # 第一个提交，与初始版本比较
            log_print("  这是最早的提交，与初始版本比较", 'INFO')
            diff_data = get_unified_diff_data(commit, None)
            if diff_data:
                diff_data['segment_info'] = {
                    'current': commit.commit_id[:8],
                    'previous': 'initial',
                    'segment_index': i + 1,
                    'total_segments': len(selected_positions)
                }
                diff_segments.append(diff_data)
    
    if diff_segments:
        return {
            'type': 'segmented_diff',
            'segments': diff_segments,
            'file_path': file_path,
            'total_segments': len(diff_segments)
        }
    
    return None

def build_smart_display_list(commits):
    """构建智能显示列表：合并连续提交，分离不同文件"""
    from collections import defaultdict

    # 按文件路径分组
    file_groups = defaultdict(list)
    for commit in commits:
        file_groups[commit.path].append(commit)

    display_list = []

    for file_path, file_commits in file_groups.items():
        log_print(f"处理文件显示: {file_path} ({len(file_commits)}个提交)", 'INFO')

        # 按时间排序
        file_commits.sort(key=lambda x: x.commit_time)

        if len(file_commits) == 1:
            # 单个提交，直接显示
            commit = file_commits[0]
            cache_available = check_commit_cache_available(commit)
            display_list.append({
                'type': 'single_commit',
                'commit': commit,
                'commit_id': commit.id,
                'diff_data': None,
                'cache_available': cache_available,
                'display_title': f"📄 {commit.path}",
                'display_subtitle': f"提交 {commit.commit_id[:8]}"
            })
            log_print(f"  - 单个提交显示: {commit.commit_id[:8]}", 'INFO')
        else:
            # 多个提交，检查是否连续
            if are_commits_consecutive_internal(file_commits):
                # 连续提交，合并显示（显示最新的提交，但diff是从最早到最新的合并结果）
                latest_commit = file_commits[-1]  # 最新的提交
                earliest_commit = file_commits[0]  # 最早的提交

                # 创建一个虚拟的合并提交对象
                merged_commit = create_merged_commit_display(file_commits)
                cache_available = check_commit_cache_available(latest_commit)

                display_list.append({
                    'type': 'consecutive_merge',
                    'commit': merged_commit,
                    'commit_id': latest_commit.id,  # 使用最新提交的ID进行异步加载
                    'diff_data': None,
                    'cache_available': cache_available,
                    'display_title': f"📄 {file_path}",
                    'display_subtitle': f"合并提交 {earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]} ({len(file_commits)}个连续提交)",
                    'merged_commits': file_commits,
                    'start_commit': earliest_commit,
                    'end_commit': latest_commit
                })
                log_print(f"  - 连续提交合并显示: {earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]}", 'INFO')
            else:
                # 非连续提交，分别显示
                for i, commit in enumerate(file_commits):
                    cache_available = check_commit_cache_available(commit)
                    display_list.append({
                        'type': 'individual_commit',
                        'commit': commit,
                        'commit_id': commit.id,
                        'diff_data': None,
                        'cache_available': cache_available,
                        'display_title': f"📄 {commit.path}",
                        'display_subtitle': f"提交 {commit.commit_id[:8]} (第{i+1}个)",
                        'sequence': i + 1
                    })
                log_print(f"  - 非连续提交分别显示: {len(file_commits)}个", 'INFO')

    log_print(f"智能显示列表构建完成: {len(display_list)}个显示单元", 'INFO')
    return display_list

def check_commit_cache_available(commit):
    """检查提交的缓存是否可用"""
    if excel_cache_service.is_excel_file(commit.path):
        cached_diff = excel_cache_service.get_cached_diff(
            commit.repository_id, commit.commit_id, commit.path
        )
        return cached_diff is not None
    return False

def create_merged_commit_display(commits):
    """创建合并提交的显示对象"""
    if not commits:
        return None

    # 使用最新的提交作为基础
    latest_commit = commits[-1]
    earliest_commit = commits[0]

    # 创建一个包含合并信息的显示对象
    class MergedCommitDisplay:
        def __init__(self, commits):
            self.commits = commits
            self.latest = commits[-1]
            self.earliest = commits[0]

        @property
        def id(self):
            return self.latest.id

        @property
        def commit_id(self):
            return self.latest.commit_id

        @property
        def path(self):
            return self.latest.path

        @property
        def message(self):
            return f"合并了{len(self.commits)}个连续提交: {self.earliest.commit_id[:8]}..{self.latest.commit_id[:8]}"

        @property
        def author(self):
            authors = list(set(c.author for c in self.commits if c.author))
            if len(authors) == 1:
                return authors[0]
            elif len(authors) > 1:
                return f"{authors[0]} 等{len(authors)}人"
            else:
                return "未知"

        @property
        def commit_time(self):
            return self.latest.commit_time

        @property
        def version(self):
            return f"{self.earliest.version}..{self.latest.version}"

        @property
        def status(self):
            return self.latest.status

        @property
        def repository(self):
            return self.latest.repository

        @property
        def repository_id(self):
            return self.latest.repository_id

    return MergedCommitDisplay(commits)

def are_commits_consecutive_internal(commits):
    """检查提交是否在文件历史中连续"""
    if len(commits) <= 1:
        return True
    
    repository = commits[0].repository
    file_path = commits[0].path
    
    # 获取该文件的完整提交历史
    all_commits = db.session.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.path == file_path
    ).order_by(Commit.commit_time.desc()).all()
    
    # 创建提交ID到位置的映射
    commit_positions = {commit.commit_id: i for i, commit in enumerate(all_commits)}
    
    # 获取选中提交的位置
    selected_positions = []
    for commit in commits:
        if commit.commit_id in commit_positions:
            selected_positions.append(commit_positions[commit.commit_id])
    
    selected_positions.sort()
    
    # 检查位置是否连续
    for i in range(1, len(selected_positions)):
        if selected_positions[i] - selected_positions[i-1] != 1:
            return False
    
    return True

def get_commit_pair_diff_internal(current_commit, previous_commit):
    """获取两个提交之间的diff"""
    try:
        repository = current_commit.repository
        
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            # 为合并diff使用独立的线程池，避免与后台任务冲突
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, active_git_processes)
            
            if excel_cache_service.is_excel_file(current_commit.path):
                # Excel文件比较 - 使用统一的差异处理逻辑
                return get_unified_diff_data(current_commit, previous_commit)
            else:
                # 文本文件比较
                diff_data = service.get_commit_range_diff(
                    previous_commit.commit_id, current_commit.commit_id, current_commit.path
                )
                return diff_data
                
        elif repository.type == 'svn':
            service = get_svn_service(repository)

            if excel_cache_service.is_excel_file(current_commit.path):
                # Excel文件比较 - 使用统一的差异处理逻辑
                log_print(f"SVN Excel文件比较: {current_commit.path}", 'WEEKLY', force=True)
                return get_unified_diff_data(current_commit, previous_commit)
            else:
                # 文本文件比较
                diff_data = service.get_version_range_diff(
                    previous_commit.version, current_commit.version, current_commit.path
                )
                return diff_data
            
    except Exception as e:
        log_print(f"获取提交对diff失败: {str(e)}")
        return None

def convert_hunks_to_lines(diff_data):
    """将hunks格式转换为模板期望的lines格式"""
    all_lines = []
    old_line_num = 1
    new_line_num = 1
    
    for hunk in diff_data.get('hunks', []):
        # 添加hunk头部
        all_lines.append({
            'type': 'header',
            'content': hunk.get('header', ''),
            'old_line_number': None,
            'new_line_number': None
        })
        
        # 重置行号为hunk的起始行号
        old_line_num = hunk.get('old_start', 1)
        new_line_num = hunk.get('new_start', 1)
        
        for line in hunk.get('lines', []):
            line_type = line.get('type', 'context')
            
            # 确保类型名称匹配CSS类名 (diff-line-added, diff-line-removed, diff-line-context)
            # 不需要转换，保持原有的 added/removed/context
            
            # 计算行号
            old_num = None
            new_num = None
            
            if line_type == 'removed':
                old_num = old_line_num
                old_line_num += 1
            elif line_type == 'added':
                new_num = new_line_num
                new_line_num += 1
            elif line_type == 'context':
                old_num = old_line_num
                new_num = new_line_num
                old_line_num += 1
                new_line_num += 1
            
            all_lines.append({
                'type': line_type,
                'content': line.get('content', ''),
                'old_line_number': old_num,
                'new_line_number': new_num
            })
    
    return {
        'type': 'code',
        'file_path': diff_data.get('file_path', ''),
        'lines': all_lines
    }

def get_mock_diff_data(commit):
    """获取模拟的diff数据"""
    if commit.path and (commit.path.endswith('.xlsx') or commit.path.endswith('.xls')):
        # Excel文件的模拟数据
        return {
            'type': 'table',
            'sheet_name': 'Sheet1',
            'changes': [
                {
                    'type': 'added',
                    'row': 5,
                    'data': {'A': 'ID5', 'B': 'New Item', 'C': '新增项目', 'D': '描述', 'E': '备注'}
                },
                {
                    'type': 'modified',
                    'row': 3,
                    'data': {'A': 'ID3', 'B': 'Modified Item', 'C': '修改项目', 'D': '新描述', 'E': '更新'}
                }
            ]
        }
    else:
        # 代码文件的模拟数据 - 兼容模板格式
        return {
            'type': 'code',
            'file_path': commit.path,
            'lines': [
                {'type': 'header', 'content': '@@ -1,3 +1,3 @@', 'old_line_number': None, 'new_line_number': None},
                {'type': 'removed', 'content': 'function oldFunction() {', 'old_line_number': 1, 'new_line_number': None},
                {'type': 'added', 'content': 'function newFunction() {', 'old_line_number': None, 'new_line_number': 1},
                {'type': 'context', 'content': '    // 函数体', 'old_line_number': 2, 'new_line_number': 2},
                {'type': 'removed', 'content': '    return "old";', 'old_line_number': 3, 'new_line_number': None},
                {'type': 'added', 'content': '    return "new";', 'old_line_number': None, 'new_line_number': 3},
                {'type': 'context', 'content': '}', 'old_line_number': 4, 'new_line_number': 4}
            ]
        }

@app.route('/repositories/compare')
def repository_compare():
    """仓库间提交对比页面"""
    source_repo_id = request.args.get('source')
    target_repo_id = request.args.get('target')
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    interval_minutes = int(request.args.get('interval', 5))
    
    if not source_repo_id or not target_repo_id:
        flash('请选择要对比的仓库', 'error')
        return redirect(url_for('index'))
    
    source_repo = Repository.query.get_or_404(source_repo_id)
    target_repo = Repository.query.get_or_404(target_repo_id)
    
    # 解析时间范围
    from datetime import datetime
    try:
        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
    except:
        flash('时间格式错误', 'error')
        return redirect(url_for('index'))
    
    # 获取两个仓库在指定时间范围内的提交
    source_commits = Commit.query.filter(
        Commit.repository_id == source_repo_id,
        Commit.commit_time >= start_dt,
        Commit.commit_time <= end_dt
    ).order_by(Commit.commit_time.desc()).all()
    
    target_commits = Commit.query.filter(
        Commit.repository_id == target_repo_id,
        Commit.commit_time >= start_dt,
        Commit.commit_time <= end_dt
    ).order_by(Commit.commit_time.desc()).all()
    
    # 分析差异
    comparison_result = analyze_repository_differences(
        source_commits, target_commits, source_repo, target_repo, interval_minutes
    )
    
    return render_template('repository_compare.html',
                         source_repo=source_repo,
                         target_repo=target_repo,
                         start_time=start_dt,
                         end_time=end_dt,
                         interval_minutes=interval_minutes,
                         comparison_result=comparison_result)

def analyze_repository_differences(source_commits, target_commits, source_repo, target_repo, interval_minutes):
    """分析两个仓库之间的差异"""
    from datetime import timedelta
    
    # 按文件路径分组提交
    source_files = {}
    target_files = {}
    
    for commit in source_commits:
        if commit.path not in source_files:
            source_files[commit.path] = []
        source_files[commit.path].append(commit)
    
    for commit in target_commits:
        if commit.path not in target_files:
            target_files[commit.path] = []
        target_files[commit.path].append(commit)
    
    # 找出差异
    differences = []
    all_files = set(source_files.keys()) | set(target_files.keys())
    
    for file_path in all_files:
        source_file_commits = source_files.get(file_path, [])
        target_file_commits = target_files.get(file_path, [])
        
        if not source_file_commits and target_file_commits:
            # 只在目标仓库存在
            differences.append({
                'type': 'target_only',
                'file_path': file_path,
                'target_commits': target_file_commits,
                'description': f'文件只在{target_repo.name}中存在'
            })
        elif source_file_commits and not target_file_commits:
            # 只在源仓库存在
            differences.append({
                'type': 'source_only',
                'file_path': file_path,
                'source_commits': source_file_commits,
                'description': f'文件只在{source_repo.name}中存在'
            })
        else:
            # 两个仓库都存在，分析时间差异
            source_latest = max(source_file_commits, key=lambda c: c.commit_time)
            target_latest = max(target_file_commits, key=lambda c: c.commit_time)
            
            time_diff = abs((source_latest.commit_time - target_latest.commit_time).total_seconds() / 60)
            
            if time_diff > interval_minutes:
                # 提交时间差异超过阈值
                if source_latest.commit_time > target_latest.commit_time:
                    differences.append({
                        'type': 'source_newer',
                        'file_path': file_path,
                        'source_commit': source_latest,
                        'target_commit': target_latest,
                        'time_diff_minutes': int(time_diff),
                        'description': f'{source_repo.name}中的版本更新（相差{int(time_diff)}分钟）'
                    })
                else:
                    differences.append({
                        'type': 'target_newer',
                        'file_path': file_path,
                        'source_commit': source_latest,
                        'target_commit': target_latest,
                        'time_diff_minutes': int(time_diff),
                        'description': f'{target_repo.name}中的版本更新（相差{int(time_diff)}分钟）'
                    })
    
    return {
        'total_differences': len(differences),
        'differences': differences,
        'source_files_count': len(source_files),
        'target_files_count': len(target_files),
        'common_files_count': len(set(source_files.keys()) & set(target_files.keys()))
    }

@app.route('/repositories/<int:repository_id>/commits/by-file')
def get_commits_by_file(repository_id):
    """获取指定文件的所有提交记录"""
    file_path = request.args.get('path')
    if not file_path:
        return jsonify({'error': '文件路径不能为空'}), 400
    
    commits = Commit.query.filter(
        Commit.repository_id == repository_id,
        Commit.path == file_path
    ).order_by(Commit.commit_time.desc()).all()
    
    commits_data = []
    for commit in commits:
        commits_data.append({
            'id': commit.id,
            'version': commit.version,
            'author': commit.author,
            'commit_time': commit.commit_time.strftime('%Y-%m-%d %H:%M:%S') if commit.commit_time else '',
            'status': commit.status,
            'operation': commit.operation
        })
    
    return jsonify({'commits': commits_data})

@app.route('/commits/compare')
def commits_compare():
    """提交对比页面"""
    from_commit_id = request.args.get('from')
    to_commit_id = request.args.get('to')
    
    if not from_commit_id or not to_commit_id:
        flash('请指定要对比的提交', 'error')
        return redirect(url_for('index'))
    
    from_commit = Commit.query.get_or_404(from_commit_id)
    to_commit = Commit.query.get_or_404(to_commit_id)
    
    # 确保两个提交是同一个文件
    if from_commit.path != to_commit.path:
        flash('只能对比同一文件的不同版本', 'error')
        return redirect(url_for('commit_diff', commit_id=from_commit_id))
    
    # 获取两个提交的diff数据
    from_diff_data = get_diff_data(from_commit)
    to_diff_data = get_diff_data(to_commit)
    
    # 生成对比diff
    compare_diff = generate_compare_diff(from_commit, to_commit, from_diff_data, to_diff_data)
    
    return render_template('commits_compare.html',
                         from_commit=from_commit,
                         to_commit=to_commit,
                         compare_diff=compare_diff,
                         repository=from_commit.repository,
                         project=from_commit.repository.project)

def generate_compare_diff(from_commit, to_commit, from_diff_data, to_diff_data):
    """生成两个提交之间的对比diff"""
    try:
        repository = from_commit.repository
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            # 为合并diff使用独立的线程池，避免与后台任务冲突
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, active_git_processes)
            
            # 使用GitService获取两个提交之间的diff
            diff_data = service.get_commit_range_diff(from_commit.commit_id, to_commit.commit_id, from_commit.path)
            
            if diff_data and diff_data.get('hunks'):
                diff_data['file_path'] = from_commit.path
                diff_data['from_commit'] = from_commit.version
                diff_data['to_commit'] = to_commit.version
                return diff_data
        
        # 如果无法获取真实diff，返回基本信息
        return {
            'type': 'code',
            'file_path': from_commit.path,
            'from_commit': from_commit.version,
            'to_commit': to_commit.version,
            'lines': [
                {'type': 'header', 'content': f'对比 {from_commit.version} 和 {to_commit.version}', 'old_line_number': None, 'new_line_number': None},
                {'type': 'context', 'content': '无法获取详细diff信息', 'old_line_number': 1, 'new_line_number': 1}
            ]
        }
        
    except Exception as e:
        log_print(f"生成对比diff失败: {str(e)}")
        return {
            'type': 'code',
            'file_path': from_commit.path,
            'from_commit': from_commit.version,
            'to_commit': to_commit.version,
            'lines': [
                {'type': 'header', 'content': f'对比 {from_commit.version} 和 {to_commit.version}', 'old_line_number': None, 'new_line_number': None},
                {'type': 'context', 'content': f'diff生成失败: {str(e)}', 'old_line_number': 1, 'new_line_number': 1}
            ]
        }

# 应用启动时的初始化
def create_tables():
    """创建数据库表"""
    import sqlite3
    import os

    with app.app_context():
        # 确保instance目录存在
        instance_dir = 'instance'
        if not os.path.exists(instance_dir):
            try:
                os.makedirs(instance_dir)
                log_print(f"✅ 创建instance目录: {os.path.abspath(instance_dir)}", 'DB')
            except Exception as e:
                log_print(f"❌ 创建instance目录失败: {e}", 'DB', force=True)
                return
        else:
            log_print(f"ℹ️ instance目录已存在: {os.path.abspath(instance_dir)}", 'DB')

        # 检查创建前的表状态
        db_path = 'instance/diff_platform.db'
        existing_tables = []
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                existing_tables = [table[0] for table in cursor.fetchall()]
                conn.close()
            except Exception as e:
                log_print(f"检查现有表失败: {e}", 'DB', force=True)
        else:
            log_print(f"ℹ️ 数据库文件不存在，将创建新数据库: {os.path.abspath(db_path)}", 'DB')

        log_print(f"创建前的数据库表: {existing_tables}", 'DB')
        
        # 创建所有表
        try:
            db.create_all()
            log_print("✅ db.create_all() 执行完成", 'DB')
        except Exception as e:
            log_print(f"❌ 创建表失败: {e}", 'DB', force=True)
            return
        
        # 检查创建后的表状态
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            final_tables = [table[0] for table in cursor.fetchall()]
            conn.close()
            
            log_print(f"创建后的数据库表: {final_tables}", 'DB')
            
            # 验证必需的表
            expected_tables = [
                'project', 'repository', 'commits_log',
                'background_tasks', 'global_repository_counter',
                'diff_cache', 'excel_html_cache', 'weekly_version_config',
                'weekly_version_diff_cache', 'weekly_version_excel_cache',
                'merged_diff_cache', 'operation_log'
            ]
            
            missing_tables = [t for t in expected_tables if t not in final_tables]
            if missing_tables:
                log_print(f"⚠️ 仍然缺失的表: {missing_tables}", 'DB', force=True)
            else:
                log_print("✅ 所有必需的表都已创建", 'DB')
                
        except Exception as e:
            log_print(f"检查最终表状态失败: {e}", 'DB', force=True)

def clear_version_mismatch_cache():
    """清理版本不匹配的缓存（自动模式）"""
    import time
    try:
        log_print(f"检查并清理版本不匹配的缓存 (当前版本: {DIFF_LOGIC_VERSION})", 'CACHE')
        
        # 分批清理，避免长时间锁定数据库
        batch_size = 100
        total_diff_cleaned = 0
        total_html_cleaned = 0
        
        # 清理DiffCache表中版本不匹配的记录（分批处理）
        while True:
            try:
                diff_cache_batch = DiffCache.query.filter(
                    DiffCache.diff_version != DIFF_LOGIC_VERSION
                ).limit(batch_size).all()
                
                if not diff_cache_batch:
                    break
                
                for cache in diff_cache_batch:
                    log_print(f"清理版本不匹配的缓存: {cache.file_path} (版本: {cache.diff_version} → {DIFF_LOGIC_VERSION})", 'CACHE')
                    db.session.delete(cache)
                
                db.session.commit()
                total_diff_cleaned += len(diff_cache_batch)
                
                # 短暂休息，释放数据库锁
                time.sleep(0.1)
                
            except Exception as e:
                log_print(f"清理DiffCache批次失败: {e}", 'CACHE', force=True)
                db.session.rollback()
                time.sleep(1)  # 等待后重试
                continue
        
        # 清理ExcelHtmlCache表中版本不匹配的记录（分批处理）
        while True:
            try:
                html_cache_batch = ExcelHtmlCache.query.filter(
                    ExcelHtmlCache.diff_version != DIFF_LOGIC_VERSION
                ).limit(batch_size).all()
                
                if not html_cache_batch:
                    break
                
                for cache in html_cache_batch:
                    log_print(f"清理版本不匹配的HTML缓存: {cache.file_path} (版本: {cache.diff_version} → {DIFF_LOGIC_VERSION})", 'CACHE')
                    db.session.delete(cache)
                
                db.session.commit()
                total_html_cleaned += len(html_cache_batch)
                
                # 短暂休息，释放数据库锁
                time.sleep(0.1)
                
            except Exception as e:
                log_print(f"清理ExcelHtmlCache批次失败: {e}", 'CACHE', force=True)
                db.session.rollback()
                time.sleep(1)  # 等待后重试
                continue
        
        if total_diff_cleaned > 0 or total_html_cleaned > 0:
            log_print(f"清理完成：{total_diff_cleaned} 条数据缓存，{total_html_cleaned} 条HTML缓存", 'CACHE')
        else:
            log_print("无需清理版本不匹配的缓存", 'CACHE')
            log_print("启动成功！", 'APP')
            
    except Exception as e:
        log_print(f"清理版本不匹配缓存失败: {e}", 'CACHE', force=True)
        try:
            db.session.rollback()
        except:
            pass

def initialize_app():
    """初始化应用，包括数据库和后台任务"""
    global _app_initialized
    if _app_initialized:
        log_print("应用已经初始化过，跳过重复初始化", 'APP')
        return

    try:
        # 创建数据库表
        create_tables()
        log_print("数据库表创建完成", 'APP')
        
        # 在应用上下文中启动后台任务工作线程
        with app.app_context():
            start_background_task_worker()
        
        # 异步清理版本不匹配的缓存（避免阻塞启动）
        import threading
        def async_cache_cleanup():
            try:
                with app.app_context():
                    clear_version_mismatch_cache()
            except Exception as e:
                log_print(f"异步缓存清理失败: {e}", 'APP', force=True)
        
        cleanup_thread = threading.Thread(target=async_cache_cleanup, daemon=True)
        cleanup_thread.start()
        log_print("异步缓存清理已启动", 'APP')
        
        # 清理待删除的仓库目录
        cleanup_pending_deletions()
        
        log_print("应用初始化完成", 'APP')
        _app_initialized = True

    except Exception as e:
        log_print(f"应用初始化失败: {e}", 'APP', force=True)
        raise

def cleanup_app():
    """应用关闭时的清理工作"""
    try:
        # log_print("开始清理应用资源...", 'APP')
        stop_background_task_worker()
        cleanup_git_processes()
        # log_print("应用清理完成", 'APP')
    except Exception as e:
        log_print(f"应用清理过程中出现错误: {e}", 'APP', force=True)
        # 忽略清理过程中的错误，避免阻塞退出

@app.route('/admin/excel-cache/cleanup-expired', methods=['POST'])
@require_admin
def cleanup_expired_cache():
    """手动清理过期缓存"""
    try:
        # 清理过期的长处理文件缓存
        expired_count = excel_cache_service.cleanup_expired_cache()

        # 清理超过1000条的普通缓存
        old_count = excel_cache_service._cleanup_old_cache()

        # 清理HTML缓存中的过期项
        html_expired_count = excel_html_cache_service.cleanup_expired_cache()

        # 清理周版本Excel缓存中的过期项
        weekly_excel_expired_count = weekly_excel_cache_service.cleanup_expired_cache()
        weekly_excel_old_count = weekly_excel_cache_service.cleanup_old_cache()

        return jsonify({
            'success': True,
            'message': f'清理完成',
            'expired_count': expired_count,
            'old_count': old_count,
            'html_expired_count': html_expired_count,
            'weekly_excel_expired_count': weekly_excel_expired_count,
            'weekly_excel_old_count': weekly_excel_old_count
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'清理失败: {str(e)}'
        }), 500

@app.route('/admin/excel-cache/clear-all-diff-cache', methods=['POST'])
@require_admin
def clear_all_diff_cache():
    """清理所有Excel差异数据缓存"""
    try:
        log_print("🧹 开始清理Excel差异数据缓存...", 'INFO')
        # 使用数据库原生批量删除，避免循环小批删除 + sleep 的低吞吐
        total_diff_cache_count = DiffCache.query.delete(synchronize_session=False)
        task_count = BackgroundTask.query.filter_by(task_type='excel_diff').delete(synchronize_session=False)
        db.session.commit()

        log_print(f"🧹 清理完成：{total_diff_cache_count} 个Excel差异数据缓存，{task_count} 个相关后台任务", 'INFO')

        return jsonify({
            'success': True,
            'message': f'清理了 {total_diff_cache_count} 个Excel差异数据缓存和 {task_count} 个相关任务',
            'diff_cache_count': total_diff_cache_count,
            'task_count': task_count
        })

    except Exception as e:
        db.session.rollback()
        log_print(f"清理Excel差异数据缓存失败: {e}", 'INFO', force=True)
        return jsonify({
            'success': False,
            'message': f'清理失败: {str(e)}'
        }), 500

@app.route('/admin/excel-cache/strategy-info', methods=['GET'])
def get_cache_strategy_info():
    """获取缓存策略信息"""
    try:
        strategy_info = {
            'max_cache_count': excel_cache_service.max_cache_count,
            'long_processing_threshold': excel_cache_service.long_processing_threshold,
            'long_processing_expire_days': excel_cache_service.long_processing_expire_days,
            'html_cache_expire_days': 30,  # HTML缓存保留30天
            'weekly_excel_max_cache_count': weekly_excel_cache_service.max_cache_count,
            'weekly_excel_expire_days': weekly_excel_cache_service.expire_days,
            'current_diff_version': DIFF_LOGIC_VERSION
        }
        
        return jsonify({
            'success': True,
            'strategy': strategy_info
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'获取策略信息失败: {str(e)}'
        }), 500

@app.route('/api/excel-html-cache/stats', methods=['GET'])
def get_excel_html_cache_stats():
    """获取Excel HTML缓存统计信息"""
    try:
        with app.app_context():
            # 获取数据缓存统计
            data_cache_stats = excel_cache_service.get_cache_statistics()
            
            # 获取HTML缓存统计
            html_cache_stats = excel_html_cache_service.get_cache_statistics()
            
            return jsonify({
                'success': True,
                'data_cache': data_cache_stats,
                'html_cache': html_cache_stats,
                'strategy': {
                    'current_version': DIFF_LOGIC_VERSION,
                    'cache_limit': 1000,
                    'long_processing_threshold': 10.0,
                    'long_processing_expire_days': 90,
                    'html_cache_expire_days': 30
                }
            })
    except Exception as e:
        log_print(f"❌ 获取HTML缓存统计失败: {e}", 'CACHE', force=True)
        return jsonify({
            'success': False,
            'message': f'获取统计失败: {str(e)}'
        }), 500

@app.route('/api/excel-cache/stats-by-project', methods=['GET'])
def get_excel_cache_stats_by_project():
    """获取按项目分组的Excel缓存统计信息"""
    try:
        with app.app_context():
            projects = Project.query.order_by(Project.id.asc()).all()
            project_stats = []

            # 1) 仓库数量（按项目聚合）
            repo_counts = db.session.query(
                Repository.project_id,
                func.count(Repository.id).label('repository_count')
            ).group_by(Repository.project_id).all()
            repo_count_map = {pid: int(cnt or 0) for pid, cnt in repo_counts}

            # 2) DiffCache统计（按项目聚合）
            diff_rows = db.session.query(
                Repository.project_id.label('project_id'),
                func.count(DiffCache.id).label('total_count'),
                func.sum(case((DiffCache.cache_status == 'completed', 1), else_=0)).label('completed_count'),
                func.sum(case((DiffCache.cache_status == 'processing', 1), else_=0)).label('processing_count'),
                func.sum(case((DiffCache.cache_status == 'failed', 1), else_=0)).label('failed_count'),
                func.sum(case((DiffCache.cache_status == 'outdated', 1), else_=0)).label('outdated_count'),
                func.sum(case(((DiffCache.cache_status == 'completed') & (DiffCache.diff_version == DIFF_LOGIC_VERSION), 1), else_=0)).label('current_version_count'),
                func.sum(case(((DiffCache.cache_status == 'completed') & (DiffCache.is_long_processing.is_(True)), 1), else_=0)).label('long_processing_count'),
            ).join(
                Repository, Repository.id == DiffCache.repository_id
            ).group_by(
                Repository.project_id
            ).all()
            diff_map = {}
            for row in diff_rows:
                total_count = int(row.total_count or 0)
                completed_count = int(row.completed_count or 0)
                long_processing_count = int(row.long_processing_count or 0)
                diff_map[row.project_id] = {
                    'total_count': total_count,
                    'completed_count': completed_count,
                    'processing_count': int(row.processing_count or 0),
                    'failed_count': int(row.failed_count or 0),
                    'outdated_count': int(row.outdated_count or 0),
                    'current_version_count': int(row.current_version_count or 0),
                    'long_processing_count': long_processing_count,
                    'normal_processing_count': max(completed_count - long_processing_count, 0),
                    'version': DIFF_LOGIC_VERSION
                }

            # 3) ExcelHtmlCache统计（按项目聚合）
            html_rows = db.session.query(
                Repository.project_id.label('project_id'),
                func.count(ExcelHtmlCache.id).label('total_count'),
                func.sum(case((ExcelHtmlCache.cache_status == 'completed', 1), else_=0)).label('completed_count'),
                func.sum(case((ExcelHtmlCache.diff_version == excel_html_cache_service.current_version, 1), else_=0)).label('current_version_count'),
                func.sum(
                    func.length(func.coalesce(ExcelHtmlCache.html_content, '')) +
                    func.length(func.coalesce(ExcelHtmlCache.css_content, '')) +
                    func.length(func.coalesce(ExcelHtmlCache.js_content, ''))
                ).label('total_size_bytes')
            ).join(
                Repository, Repository.id == ExcelHtmlCache.repository_id
            ).group_by(
                Repository.project_id
            ).all()
            html_map = {}
            for row in html_rows:
                total_count = int(row.total_count or 0)
                current_version_count = int(row.current_version_count or 0)
                total_size_bytes = int(row.total_size_bytes or 0)
                html_map[row.project_id] = {
                    'total_count': total_count,
                    'completed_count': int(row.completed_count or 0),
                    'current_version_count': current_version_count,
                    'old_version_count': max(total_count - current_version_count, 0),
                    'total_size_mb': round(total_size_bytes / (1024 * 1024), 2),
                    'current_version': excel_html_cache_service.current_version
                }

            # 4) WeeklyVersionExcelCache统计（按项目聚合）
            weekly_rows = db.session.query(
                Repository.project_id.label('project_id'),
                func.count(WeeklyVersionExcelCache.id).label('total_count'),
                func.sum(case((WeeklyVersionExcelCache.cache_status == 'completed', 1), else_=0)).label('completed_count'),
                func.sum(case((WeeklyVersionExcelCache.cache_status == 'processing', 1), else_=0)).label('processing_count'),
                func.sum(case((WeeklyVersionExcelCache.cache_status == 'failed', 1), else_=0)).label('failed_count'),
                func.sum(func.length(func.coalesce(WeeklyVersionExcelCache.html_content, ''))).label('total_size')
            ).join(
                Repository, Repository.id == WeeklyVersionExcelCache.repository_id
            ).group_by(
                Repository.project_id
            ).all()
            weekly_map = {}
            for row in weekly_rows:
                weekly_map[row.project_id] = {
                    'total_count': int(row.total_count or 0),
                    'completed_count': int(row.completed_count or 0),
                    'processing_count': int(row.processing_count or 0),
                    'failed_count': int(row.failed_count or 0),
                    'total_size': int(row.total_size or 0),
                    'max_cache_count': weekly_excel_cache_service.max_cache_count,
                    'expire_days': weekly_excel_cache_service.expire_days
                }

            for project in projects:
                pid = project.id
                data_cache_stats = diff_map.get(pid, {
                    'total_count': 0,
                    'completed_count': 0,
                    'processing_count': 0,
                    'failed_count': 0,
                    'outdated_count': 0,
                    'current_version_count': 0,
                    'long_processing_count': 0,
                    'normal_processing_count': 0,
                    'version': DIFF_LOGIC_VERSION
                })
                html_cache_stats = html_map.get(pid, {
                    'total_count': 0,
                    'completed_count': 0,
                    'current_version_count': 0,
                    'old_version_count': 0,
                    'total_size_mb': 0.0,
                    'current_version': excel_html_cache_service.current_version
                })
                weekly_excel_cache_stats = weekly_map.get(pid, {
                    'total_count': 0,
                    'completed_count': 0,
                    'processing_count': 0,
                    'failed_count': 0,
                    'total_size': 0,
                    'max_cache_count': weekly_excel_cache_service.max_cache_count,
                    'expire_days': weekly_excel_cache_service.expire_days
                })

                project_stats.append({
                    'project': {
                        'id': pid,
                        'code': project.code,
                        'name': project.name,
                        'repository_count': repo_count_map.get(pid, 0)
                    },
                    'data_cache': data_cache_stats,
                    'html_cache': html_cache_stats,
                    'weekly_excel_cache': weekly_excel_cache_stats
                })

            return jsonify({
                'success': True,
                'projects': project_stats,
                'total_projects': len(projects)
            })
            
    except Exception as e:
        log_print(f"❌ 获取项目缓存统计失败: {e}", 'CACHE', force=True)
        return jsonify({
            'success': False,
            'message': f'获取项目统计失败: {str(e)}'
        }), 500

@app.route('/admin/weekly-excel-cache/stats', methods=['GET'])
def get_weekly_excel_cache_stats():
    """获取周版本Excel缓存统计信息"""
    try:
        stats = weekly_excel_cache_service.get_cache_stats()
        return jsonify({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        log_print(f"❌ 获取统计失败: {e}", 'CACHE', force=True)
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'获取统计信息失败: {str(e)}'
        }), 500

@app.route('/admin/weekly-excel-cache/cleanup', methods=['POST'])
@require_admin
def cleanup_weekly_excel_cache():
    """清理周版本Excel缓存"""
    try:
        expired_count = weekly_excel_cache_service.cleanup_expired_cache()
        old_count = weekly_excel_cache_service.cleanup_old_cache()

        return jsonify({
            'success': True,
            'message': f'清理完成',
            'expired_count': expired_count,
            'old_count': old_count
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'清理失败: {str(e)}'
        }), 500

@app.route('/admin/weekly-excel-cache/clear-all', methods=['POST'])
@require_admin
def clear_all_weekly_excel_cache():
    """清理所有周版本Excel缓存"""
    try:
        count = weekly_excel_cache_service.clear_all_cache()

        return jsonify({
            'success': True,
            'message': f'已清理 {count} 条周版本Excel缓存',
            'count': count
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'清理失败: {str(e)}'
        }), 500

@app.route('/admin/weekly-excel-cache/rebuild/<int:config_id>', methods=['POST'])
@require_admin
def rebuild_weekly_excel_cache(config_id):
    """重建指定周版本配置的Excel缓存"""
    log_print(f"🔄 开始重建周版本Excel缓存，配置ID: {config_id}", 'WEEKLY', force=True)

    try:
        # 获取周版本配置
        config = db.session.get(WeeklyVersionConfig, config_id)
        if not config:
            log_print(f"❌ 周版本配置不存在: {config_id}", 'WEEKLY', force=True)
            return jsonify({
                'success': False,
                'message': f'周版本配置不存在: {config_id}'
            }), 404

        log_print(f"✅ 找到周版本配置: {config.name} (仓库: {config.repository.name})", 'WEEKLY', force=True)

        # 记录到操作日志
        log_print(f"🔍 准备调用log_cache_operation", 'DEBUG', force=True)
        try:
            weekly_excel_cache_service.log_cache_operation(f"🔄 开始重建周版本Excel缓存: {config.name} (仓库: {config.repository.name})", 'info', repository_id=config.repository_id, config_id=config_id)
            log_print(f"🔍 log_cache_operation调用成功", 'DEBUG', force=True)
        except Exception as e:
            log_print(f"🔍 log_cache_operation调用失败: {e}", 'ERROR', force=True)

        # 1. 首先清理该配置下的所有待处理和处理中的任务
        log_print(f"🧹 清理配置ID {config_id} 的现有队列任务", 'WEEKLY', force=True)

        # 删除待处理的周版本Excel缓存任务（使用repository_id字段存储config_id）
        pending_tasks_deleted = BackgroundTask.query.filter(
            BackgroundTask.repository_id == config_id,
            BackgroundTask.task_type == 'weekly_excel_cache',
            BackgroundTask.status.in_(['pending', 'processing'])
        ).delete(synchronize_session=False)

        log_print(f"✅ 删除了 {pending_tasks_deleted} 个现有队列任务", 'WEEKLY', force=True)

        # 2. 删除现有缓存数据
        log_print(f"🧹 清理配置ID {config_id} 下的现有Excel缓存数据", 'WEEKLY', force=True)
        cache_deleted = WeeklyVersionExcelCache.query.filter_by(config_id=config_id).delete()
        log_print(f"✅ 删除了 {cache_deleted} 个缓存记录", 'WEEKLY', force=True)

        db.session.commit()

        # 3. 获取该配置下所有的Excel文件diff缓存
        log_print(f"🔍 查询配置ID {config_id} 下的diff缓存...", 'WEEKLY', force=True)
        excel_diff_caches = WeeklyVersionDiffCache.query.filter_by(config_id=config_id).all()
        log_print(f"📊 找到 {len(excel_diff_caches)} 个diff缓存记录", 'WEEKLY', force=True)

        # 4. 筛选出Excel文件（简化逻辑，所有Excel文件都重建）
        excel_files = []
        for diff_cache in excel_diff_caches:
            if diff_cache.file_path.lower().endswith(('.xlsx', '.xls', '.csv')):
                excel_files.append(diff_cache.file_path)
                log_print(f"📋 添加Excel文件: {diff_cache.file_path}", 'WEEKLY', force=True)
            else:
                log_print(f"⏭️ 跳过非Excel文件: {diff_cache.file_path}", 'WEEKLY', force=True)

        log_print(f"📈 总计需要重建缓存的Excel文件: {len(excel_files)} 个", 'WEEKLY', force=True)

        if not excel_files:
            log_print(f"ℹ️ 没有需要重建缓存的Excel文件", 'WEEKLY', force=True)
            return jsonify({
                'success': True,
                'message': f'周版本配置 "{config.name}" 中没有需要重建缓存的Excel文件',
                'task_count': 0,
                'deleted_count': cache_deleted
            })

        # 5. 为每个Excel文件创建缓存任务
        log_print(f"🚀 开始创建缓存重建任务...", 'WEEKLY', force=True)
        task_count = 0
        for file_path in excel_files:
            try:
                log_print(f"📝 创建任务: {file_path}", 'WEEKLY', force=True)
                create_weekly_excel_cache_task(config_id, file_path)
                task_count += 1
                log_print(f"✅ 任务创建成功: {file_path}", 'WEEKLY', force=True)
            except Exception as task_e:
                log_print(f"❌ 创建Excel缓存任务失败: {file_path}, 错误: {task_e}", 'WEEKLY', force=True)

        message = f'已清理 {cache_deleted} 条旧缓存，创建 {task_count} 个重建任务，正在后台处理中...'
        log_print(f"🎉 重建缓存请求处理完成: {message}", 'WEEKLY', force=True)

        # 记录到操作日志
        weekly_excel_cache_service.log_cache_operation(f"✅ 周版本Excel缓存重建完成: {config.name} - {message}", 'success', repository_id=config.repository_id, config_id=config_id)

        return jsonify({
            'success': True,
            'message': message,
            'task_count': task_count,
            'deleted_count': cache_deleted,
            'excel_files': excel_files  # 添加文件列表用于前端显示
        })

    except Exception as e:
        log_print(f"❌ 重建周版本Excel缓存失败: {e}", 'WEEKLY', force=True)
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'重建缓存失败: {str(e)}'
        }), 500


@app.route('/admin/excel-cache')
def excel_cache_management():
    """Excel缓存管理页面"""
    # 获取所有项目信息
    projects = Project.query.all()
    return render_template('excel_cache_management.html', 
                         current_version=DIFF_LOGIC_VERSION,
                         projects=projects)

# 注册清理函数
atexit.register(cleanup_app)

# 防止重复初始化的标志
_app_initialized = False

if __name__ == '__main__':
    import sys
    import os
    import signal
    import threading
    
    # 禁用Python输出缓冲，确保日志立即显示
    os.environ['PYTHONUNBUFFERED'] = '1'
    
    # 禁用Werkzeug的访问日志以避免日志输出错误
    # import logging
    # log = logging.getLogger('werkzeug')
    # log.setLevel(logging.ERROR)
    
    # 设置环境变量避免Windows控制台I/O问题
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    # 强制设置标准输出为无缓冲模式
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    
    # 全局标志，用于优雅关闭
    shutdown_flag = threading.Event()
    
    def signal_handler(signum, frame):
        """信号处理器，用于优雅关闭应用"""
        log_print("\n接收到中断信号，正在关闭应用...", 'APP')
        shutdown_flag.set()
        cleanup_app()
        # 强制退出，避免线程异常
        os._exit(0)
    
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # 清空日志文件
        clear_log_file()

        # 初始化应用
        initialize_app()

        # 启动Flask应用
        log_print("正在启动服务器...", 'APP')
        log_print("按 Ctrl+C 停止服务器", 'APP')
        
        # 禁用debug模式和reloader来避免多进程问题
        app.run(debug=False, host='0.0.0.0', port=8002, use_reloader=False, threaded=True)
        
    except KeyboardInterrupt:
        log_print("\n接收到键盘中断，正在关闭应用...", 'APP')
        cleanup_app()
    except SystemExit:
        # 正常退出，不显示异常
        pass
    except Exception as e:
        log_print(f"应用运行异常: {e}", 'APP', force=True)
        cleanup_app()
        sys.exit(1)
    finally:
        # 确保清理工作完成
        if not shutdown_flag.is_set():
            cleanup_app()

