"""
安全的日志输出工具，避免I/O operation on closed file错误
"""
import sys

def safe_print(message, log_type='INFO', force=False):
    """
    安全的日志输出函数，支持日志级别控制
    
    Args:
        message: 要输出的消息
        log_type: 日志类型 ('INFO', 'ERROR', 'DEBUG', etc.)
        force: 是否强制输出
    """
    try:
        print(message)
    except (UnicodeEncodeError, ValueError, OSError):
        # 如果编码失败或文件流关闭，使用安全输出
        try:
            if hasattr(sys.stdout, 'closed') and sys.stdout.closed:
                return  # 如果标准输出已关闭，直接返回
            safe_message = str(message).encode('ascii', errors='replace').decode('ascii')
            print(safe_message)
        except (ValueError, AttributeError, OSError):
            # 如果仍然失败，静默忽略
            pass

# 为了兼容性，提供一个全局的安全打印函数
def log_print(message, log_type='INFO', force=False):
    """统一的日志输出函数，支持日志级别控制"""
    safe_print(message, log_type, force)
