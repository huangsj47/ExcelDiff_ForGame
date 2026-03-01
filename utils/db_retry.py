
import time
import functools
from sqlalchemy.exc import OperationalError, PendingRollbackError

def db_retry(max_retries=3, delay=0.1):
    """数据库操作重试装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, PendingRollbackError) as e:
                    if "database is locked" in str(e) or "PendingRollbackError" in str(e):
                        if attempt < max_retries - 1:
                            print(f"数据库操作失败，重试 {attempt + 1}/{max_retries}: {str(e)}")
                            # 回滚会话
                            try:
                                from app import db
                                db.session.rollback()
                            except:
                                pass
                            time.sleep(delay * (2 ** attempt))  # 指数退避
                            continue
                    raise e
            return None
        return wrapper
    return decorator
