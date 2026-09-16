#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库信息查询工具。

## 这个模块的范围（曾经不是这样）

这里原先还有 `ensure_instance_directory()` 与 `create_tables(db)` 两个函数，
它们**从未被任何生产代码调用**（全仓只有 `get_database_info` 被
`routes/main_routes.py` 的 `/api/system/info` 引用），却各自是一份**会走偏的
第二实现**：

* `create_tables(db)` 用 `sqlite3.connect(DATABASE_CONFIG['db_path'])` 自己连一个
  文件来「验证」表是否建好 —— 完全绕过 SQLAlchemy 引擎。后端是 MySQL、或
  `DATABASE_URL` 指向别处时，它看的和 `db.create_all()` 写的是**两个不同的库**：
  要么把好库报成「仍然缺失的表: [全部 21 张]」，要么在别的文件上报「✅ 所有必需的
  表都已创建」。
* 它的必需表清单只有 12 张，而真正启动链路
  （`services/app_bootstrap_db_service.py`）检查 21 张 —— 两份清单会各自漂移。
* 它的外层 `except Exception` 只打日志不抛出，调用方无法察觉失败。
* 名字 `create_tables` 还与 `app.create_tables`（真正的启动建表入口）重名，
  `from app import create_tables` 与 `from utils.database import create_tables`
  拿到的是两个行为不同的函数。

所以这两个函数已删除。建表与目录创建**只有**一条路径：
`services/app_bootstrap_db_service.py::create_tables_with_runtime_checks`
（由 `app.create_tables` 调用）。

## 路径来源

本模块**不再**读 `config.DATABASE_CONFIG` 作为「真实库位置」—— 那只是静态默认值，
运行时会 `DATABASE_URL` / `DB_BACKEND` 覆盖。`get_database_info()` 改为优先取
Flask app 上已解析好的 `SQLALCHEMY_DATABASE_URI`，这样 `/api/system/info` 报的
就是**当前真正在用**的那个库（此前它可能报一个 CWD 推导出来的无关路径）。
"""

import os
import sqlite3

from utils.db_config import get_sqlite_path_from_uri, sanitize_database_uri
from utils.safe_print import log_print


def _runtime_database_uri():
    """取运行期真正生效的数据库 URI；取不到时返回空串。

    刻意**不**在函数签名里要求 app/current_app：本模块被 `/api/system/info` 调用，
    也允许在没有应用上下文的脚本里被调用；拿不到就退回静态默认值。
    """
    try:
        from flask import current_app

        if current_app:
            uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
            if uri:
                return str(uri)
    except Exception:  # pragma: no cover - 无应用上下文 / 无 Flask
        pass
    try:
        from config import DATABASE_CONFIG

        return f"sqlite:///{DATABASE_CONFIG['db_path']}"
    except Exception:  # pragma: no cover
        return ""


def get_database_info():
    """获取数据库信息（报告**运行期真正在用**的那个库）。"""
    database_uri = _runtime_database_uri()

    if not database_uri:
        return {
            "exists": False,
            "path": "",
            "size": 0,
            "tables": [],
            "error": "无法确定运行期数据库 URI",
        }

    if not database_uri.lower().startswith("sqlite"):
        # MySQL 后端没有「库文件」概念。这里明确回报后端类型，而不是假装
        # 文件不存在（此前会返回 exists=False + 一个无关的 sqlite 路径，
        # 让排查的人以为数据库丢了）。
        backend = getattr(_flask_db_backend(), "upper", lambda: "MYSQL")()
        return {
            "exists": True,
            "path": "",
            "size": 0,
            "tables": [],
            "backend": backend,
            "uri": sanitize_database_uri(database_uri),
            "note": "非 SQLite 后端：没有库文件，库信息请查数据库服务端",
        }

    db_path = get_sqlite_path_from_uri(database_uri) or ""
    if not db_path or not os.path.exists(db_path):
        return {
            "exists": False,
            "path": db_path,
            "size": 0,
            "tables": [],
            "backend": "SQLITE",
            "uri": sanitize_database_uri(database_uri),
        }

    try:
        size = os.path.getsize(db_path)
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [table[0] for table in cursor.fetchall()]
        finally:
            conn.close()

        return {
            "exists": True,
            "path": db_path,
            "size": size,
            "tables": tables,
            "backend": "SQLITE",
            "uri": sanitize_database_uri(database_uri),
        }
    except Exception as e:
        log_print(f"获取数据库信息失败: {e}", "DB", force=True)
        return {
            "exists": False,
            "path": db_path,
            "size": 0,
            "tables": [],
            "backend": "SQLITE",
            "error": str(e),
        }


def _flask_db_backend():
    try:
        from flask import current_app

        if current_app:
            return str(current_app.config.get("DB_BACKEND") or "")
    except Exception:  # pragma: no cover
        pass
    return ""


def backup_database(backup_path=None):
    """备份 SQLite 库文件。非 SQLite 后端返回 False（并说明原因）。"""
    try:
        db_path = get_sqlite_path_from_uri(_runtime_database_uri())
        if not db_path:
            log_print("当前后端不是 SQLite，无法用文件复制方式备份", "DB", force=True)
            return False
        if not os.path.exists(db_path):
            log_print(f"数据库文件不存在，无法备份: {db_path}", "DB", force=True)
            return False

        if not backup_path:
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{db_path}.backup_{timestamp}"

        import shutil

        shutil.copy2(db_path, backup_path)
        log_print(f"✅ 数据库备份成功: {backup_path}", "DB")
        return True

    except Exception as e:
        log_print(f"数据库备份失败: {e}", "DB", force=True)
        return False
