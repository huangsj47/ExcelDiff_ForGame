#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""比较口径配置（表头行数 / 名称行 / 关键列）：解析校验 + 改了之后清 diff 缓存。

## 为什么需要清缓存

diff 缓存的键是 `(repository_id, commit_id, file_path, previous_commit_id, diff_version)`
（`services/excel_diff_cache_service.py::_build_cache_query`），里面**没有**仓库的比较配置。
于是 `表头行数` / `关键列` 这两项改完之后，旧缓存照样命中 —— 用户在表单上把「表头行数」
从 1 改成 3，页面上的表现一点不变（还是把第 2、3 行当数据行报出来），他会以为这个配置
没用。`关键列` 从它上线起就有这个问题（改完要吃旧缓存），这里一并修掉；
`名称行` 上线时就带着这条（`DIFF_SETTING_FIELDS` 里三项齐了才算齐）。

改动**不动提交记录、不动同步状态**：那是 `clear_repository_state_for_switch` 干的事
（分支/版本号切换，必须重新同步）。比较配置只是「怎么读同一批文件」变了，文件本身没变，
所以只清缓存、让下次访问重算即可 —— 用前者会让「改一个表头行数」变成「整仓重新同步」。

## 为什么解析与校验也放在这里

这三项是**同一件事**（怎么读同一批文件），而「哪些项影响口径」这份清单必须与
`DIFF_SETTING_FIELDS` 一起维护：新增一项却忘了加进那份清单，表现是「改完看不到变化」，
不会报错。`parse_header_name_row` 就是「名称行」这一项的解析与校验，
三个表单入口（git 创建 / svn 创建 / 编辑）共用同一份，避免三处各写一遍口径。
"""
from __future__ import annotations

from sqlalchemy import or_

from models import (
    DiffCache,
    ExcelHtmlCache,
    MergedDiffCache,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    WeeklyVersionExcelCache,
)

# 只有「影响比较口径」的项才在这里：改了它，已算出的 diff 就不再有意义。
DIFF_SETTING_FIELDS = ("header_rows", "header_name_row", "key_columns")


def parse_header_rows(value) -> tuple:
    """表单里的「表头行数」→ `(库值, 错误信息)`。

    空等价于「不配置」，存 `None`；别的必须是正整数。

    **这里必须自己给提示，不能让它抛。** 三个表单入口原先写的是
    `int(header_rows) if header_rows else None` —— 裸转换。`resource_type == "table"`
    那条「表头行数为必填项」的校验只挡**空**（非空字符串一律 truthy），填了「三」
    或者被脚本改成 `1e9`/`abc` 时一路走到 `int()` 才炸，用户看到的是 500 而不是
    「表头行数必须是数字」。这也是为什么它和 `parse_header_name_row` 放在一起：
    两者用的是同一个值，本来就该由同一处解析。
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return None, ""
    try:
        count = int(text)
    except (TypeError, ValueError):
        return None, f"表头行数「{text}」不是数字"
    if count < 1:
        return None, f"表头行数（{count}）必须大于 0"
    return count, ""


def parse_header_name_row(submitted, header_rows) -> tuple:
    """表单里的「名称行」→ `(库值, 错误信息)`。

    口径：空 / 1 等价于「不配置」，存 `None`（= 第 1 行就是字段名行，与历史上一致）；
    `2 <= 名称行 <= 表头行数` 才接受，其余给一句能照着改的提示。

    「名称行必须在表头块里」这条**不能放宽**：名称行落到数据行上意味着把某一行数据当成
    列名，那一行的取值从此只在列头出现、再也不会被比到（静默漏审），而它下面的数据行
    又整体错位一格。引擎那一层也会夹一次（`DiffService._header_name_row`），但错误要
    在表单这一层说清楚 —— 静默夹到别的行上等于替用户猜他想要哪一行。
    """
    try:
        raw = submitted.get("header_name_row")
    except (AttributeError, TypeError):     # 不是映射语义的容器
        return None, ""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None, ""
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None, f"名称行「{text}」不是数字"
    if value <= 1:
        return None, ""
    # 表头行数走同一处解析：填了「三」的时候，先说「表头行数不是数字」，
    # 而不是拿 `count = 1` 兜底后回一句「名称行（2）不能超过表头行数（1）」——
    # 那是在用一个猜出来的数字教用户改另一个字段。
    count, rows_error = parse_header_rows(header_rows)
    if rows_error:
        return None, rows_error
    if value > (count or 1):
        return None, f"名称行（{value}）不能超过表头行数（{count or 1}）"
    return value, ""


def _normalize_setting(value) -> str:
    """空与 None 等价；其余按去空格后的字符串比。

    `header_rows` 表单里是 `'3'`、库里存的是 `int 3`，直接比会永远不等 ——
    那会导致每次保存都清一遍缓存（能用，但白算）。
    """
    if value is None:
        return ""
    return str(value).strip()


def diff_settings_changed(repository, submitted) -> list:
    """表单里哪些比较配置真的变了。

    `submitted` 是本次提交的原始映射（`request.form`）。表单里**没有**这个字段时跳过：
    局部表单（某些入口只提交一部分字段）不该被误判成「改成了空」而白清一遍缓存。
    """
    changed = []
    for field in DIFF_SETTING_FIELDS:
        try:
            present = field in submitted
        except TypeError:  # 不是映射/集合语义的容器：当作没提交
            present = False
        if not present:
            continue
        if _normalize_setting(getattr(repository, field, None)) != _normalize_setting(
            submitted.get(field)
        ):
            changed.append(field)
    return changed


def _delete(query, name, repository_id, log_print):
    try:
        return query.delete(synchronize_session=False)
    except Exception as exc:  # noqa: BLE001 —— 清缓存失败不该让「保存配置」这件事失败
        if log_print:
            log_print(
                f"⚠️ 清理 {name} 缓存失败: repo_id={repository_id} | {type(exc).__name__}: {exc}",
                "CACHE",
                force=True,
            )
        return 0


def reset_repository_diff_caches(repository_id, *, changed_fields=(), log_print=None) -> dict:
    """清掉该仓库的 diff 缓存（提交页 / 快照 HTML / 合并 / 周版本），返回各类删除条数。

    **必须在调用方 `db.session.commit()` 之前调用**：这里是同一个事务里的 `delete()`，
    调用方 commit 才算数；反过来（先 commit 再删且不再 commit）会在请求结束时被回滚，
    缓存一条没删而日志里写着删了 —— 静默失败。

    周版本两张表按 `repository_id` **或** `config_id` 匹配，与
    `clear_repository_state_for_switch` 一致：老数据里可能有只带 config_id 的行。
    """
    config_ids = [
        config.id for config in WeeklyVersionConfig.query.filter_by(repository_id=repository_id).all()
    ]

    deleted = {}
    for name, model in (("diff", DiffCache), ("html", ExcelHtmlCache),
                        ("merged", MergedDiffCache)):
        deleted[name] = _delete(
            model.query.filter_by(repository_id=repository_id), name, repository_id, log_print)

    for name, model in (("weekly_diff", WeeklyVersionDiffCache),
                        ("weekly_excel", WeeklyVersionExcelCache)):
        if config_ids:
            query = model.query.filter(
                or_(model.repository_id == repository_id, model.config_id.in_(config_ids))
            )
        else:
            query = model.query.filter_by(repository_id=repository_id)
        deleted[name] = _delete(query, name, repository_id, log_print)

    if log_print:
        log_print(
            f"比较配置变更（{', '.join(changed_fields) or '未指明'}），已清理该仓库 diff 缓存: "
            f"repo_id={repository_id} | " + ", ".join(f"{k}={v}" for k, v in deleted.items()),
            "CACHE",
            force=True,
        )
    return deleted
