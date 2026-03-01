
# SQLite优化配置
import sqlite3
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """设置SQLite优化参数"""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        # 启用WAL模式，提高并发性能
        cursor.execute("PRAGMA journal_mode=WAL")
        # 设置同步模式为NORMAL，平衡性能和安全性
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 设置缓存大小
        cursor.execute("PRAGMA cache_size=10000")
        # 设置超时时间
        cursor.execute("PRAGMA busy_timeout=30000")
        # 启用外键约束
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        # 移除日志输出，避免日志污染
        # print("✅ SQLite优化配置已应用")
