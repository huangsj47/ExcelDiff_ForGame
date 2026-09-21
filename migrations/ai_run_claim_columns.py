#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`ai_analysis_run` 的两列一索引：活动运行认领与降级原因。

## 为什么单独一个迁移

模型层（`models/ai_analysis/analysis_run.py`）加的东西，**只对新建的表生效** ——
`db.create_all()` 对已存在的表什么都不做。这一版加的是：

    ai_analysis_run.active_key   VARCHAR(120)   活动运行认领（UNIQUE）
    ai_analysis_run.degradation  VARCHAR(40)    降级原因（DEGRADE_* 取值）
    uq_ai_run_active_key                         上面那一列的 UNIQUE 索引

其中 `active_key` 上的唯一约束是**幂等的地基**：手工与定时同时触发、或同页连按两次，
「同一目标 + 同一输入同时只允许一条活动运行」这件事必须由数据库裁决，而不是由
「先查再插」在应用层近似（那条路在查与插之间有窗口，而连按两次正好落在窗口里）。

## 两种落地方式

1. **重建库（推荐，平台尚未正式部署）**：`python scripts/recreate_db.py`。
   `create_all` 会带着新列与新索引把表建出来，本文件不用跑。
2. **保留数据的库**：跑本文件。它是幂等的 —— 列已存在就跳过，索引已存在就跳过；
   SQLite / MySQL 两种后端都支持。

    python -m migrations.ai_run_claim_columns          # 打一份现状
    python -m migrations.ai_run_claim_columns --apply  # 真的执行

**默认只打不动**：这是唯一一条会改线上库结构的入口，默认必须是安全的那个方向。

## 谁来调它

平台启动时的轻量迁移在 `services/db_migration_service.py::apply_schema_migrations`
（那里有 `_migrate_ai_analysis_columns` 与 `REQUIRED_INDEXES`）。把 `apply(db, log_print)`
接进去即可；本文件**没有**自己接，因为它不在那次改动的归属范围内。没接上也不影响
新库（新库走 1），只有「保留数据的老库」需要手动跑一次。
"""
from __future__ import annotations

import re
import sys

from sqlalchemy import inspect
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError

TABLE = "ai_analysis_run"

# 要补的列：(列名, DDL 里的类型)。**没有 DEFAULT 子句** —— 老行在新列上是 NULL，
# 而那正是我们要的语义：`active_key` 为 NULL = 不占着任何输入（老行本来也不认识
# 这个机制）；`degradation` 为 NULL = 「不知道降级原因」（老行没有这一列）。
REQUIRED_COLUMNS = (
    ("active_key", "VARCHAR(120)"),
    ("degradation", "VARCHAR(40)"),
)

# 要补的唯一索引：(索引名, 是否唯一, 列名)。唯一性只对 active_key 有要求。
REQUIRED_INDEXES = (
    ("uq_ai_run_active_key", True, ("active_key",)),
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# MySQL 1061 = ER_DUP_KEYNAME（索引已存在）。
_MYSQL_DUPLICATE_KEY_NAME_ERRNO = 1061


def _existing_columns(db) -> set:
    try:
        return {col["name"] for col in inspect(db.engine).get_columns(TABLE)}
    except SQLAlchemyError:
        return set()


def _existing_indexes(db) -> set:
    """已有的索引名。

    **只用来把账记准**（「新建了」还是「本来就有」），不用来「先查后建」：那句
    `IF NOT EXISTS`（SQLite/PG）与「照建 + 吞 1061」（MySQL）本身就是幂等的，
    不需要在它前面再加一次有竞态的检查。
    """
    try:
        return {item["name"] for item in inspect(db.engine).get_indexes(TABLE)}
    except SQLAlchemyError:
        return set()


def _table_exists(db) -> bool:
    try:
        return TABLE in set(inspect(db.engine).get_table_names())
    except SQLAlchemyError:
        return False


def _create_index_sql(dialect: str, name: str, unique: bool, columns: tuple) -> str:
    """按后端拼 CREATE INDEX 语句（入参只来自本文件的常量，且逐个校验过标识符）。"""
    head = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    body = f"{name} ON {TABLE} ({', '.join(columns)})"
    if dialect == "mysql":
        # MySQL **不支持** `CREATE INDEX IF NOT EXISTS`（8.0 也不支持，写了直接语法
        # 错误）。选「照建 + 捕获 1061 忽略」而不是「先查后建」：后者是 check-then-act，
        # 两个进程同时迁移会双双查到「不存在」然后一起建，其中一个必然报错。
        return f"{head} {body}"
    return f"{head} IF NOT EXISTS {body}"


def _is_already_exists_error(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None) or ()
    if args and args[0] == _MYSQL_DUPLICATE_KEY_NAME_ERRNO:
        return True
    message = str(exc).lower()
    return "duplicate key name" in message or "already exists" in message


def apply(db, log_print) -> dict:
    """执行迁移。返回 `{added_columns, added_indexes, skipped}` 的账。

    **幂等**：列 / 索引已存在就跳过，重复跑不报错。失败不抛（只记日志）—— 它是启动
    链路的一部分，一条 DDL 失败不该把平台带下水；但**必须留下日志**，静默跳过的表现是
    「代码在读一个库上不存在的列」。
    """
    report = {"added_columns": [], "added_indexes": [], "skipped": []}
    if not _table_exists(db):
        # 表还没建出来（全新库 / 精简测试库）：交给 `create_all`，这里不造表。
        report["skipped"].append(f"{TABLE} 不存在")
        return report

    columns = _existing_columns(db)
    for name, ddl in REQUIRED_COLUMNS:
        if name in columns:
            report["skipped"].append(name)
            continue
        if not _IDENTIFIER_RE.match(name):
            report["skipped"].append(name)
            continue
        try:
            db.session.execute(sa_text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl}"))
            db.session.commit()
            report["added_columns"].append(name)
        except SQLAlchemyError as exc:
            db.session.rollback()
            log_print(f"⚠️ 迁移跳过：无法给 {TABLE} 加列 {name}: {exc}", "DB", force=True)

    dialect = db.engine.dialect.name
    for name, unique, cols in REQUIRED_INDEXES:
        if not all(_IDENTIFIER_RE.match(part) for part in (name, *cols)):
            report["skipped"].append(name)
            continue
        # 「本来就有」要在执行**之前**量：`IF NOT EXISTS` / 吞 1061 都不会告诉你
        # 这次到底建了没有，执行完再看是看不出来的。
        existed = name in _existing_indexes(db)
        try:
            db.session.execute(sa_text(_create_index_sql(dialect, name, unique, cols)))
            db.session.commit()
        except SQLAlchemyError as exc:
            try:
                db.session.rollback()
            except SQLAlchemyError:
                pass
            if _is_already_exists_error(exc):
                report["skipped"].append(name)
                continue
            log_print(f"⚠️ 迁移失败：无法创建索引 {name}: {exc}", "DB", force=True)
            continue
        (report["skipped"] if existed else report["added_indexes"]).append(name)

    if report["added_columns"] or report["added_indexes"]:
        log_print(
            f"✅ ai_analysis_run 迁移完成：列 {report['added_columns']}、"
            f"索引 {report['added_indexes']}",
            "DB",
            force=True,
        )
    return report


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply_it = "--apply" in argv

    from app import app, db
    from utils.logger import log_print

    with app.app_context():
        print(f"目标库：{db.engine.url}")
        print(f"模式：{'执行' if apply_it else '只打不动（加 --apply 才改库结构）'}")
        if not apply_it:
            print(f"待补列：{[name for name, _ in REQUIRED_COLUMNS]}")
            print(f"待补唯一索引：{[name for name, _, _ in REQUIRED_INDEXES]}")
            print("现有列：", sorted(_existing_columns(db)))
            return 0
        report = apply(db, log_print)
        print(f"新增列：{report['added_columns']}")
        print(f"新增索引：{report['added_indexes']}")
        print(f"已存在跳过：{report['skipped']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
