#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这批周版本配置用**哪一套表头口径** —— 进 AI 内容身份的那一维。

## 它补的是一个静默的漏洞

AI 分析的「这份输入已经分析过了，别再跑一遍」判据是**内容身份**：

    (file_path, base_commit_id, latest_commit_id, diff_version)

`services/ai/scope_sampling.weekly_snapshot_digest` 与
`services/ai/snapshot_store._digest_of` 都用它。这四项全部在说「**文件变了没有**」——
**没有一项说「这些文件该怎么读」**。

于是改了仓库的比较口径（表头行数 / 名称行 / 关键列 / 多套表头方案）之后：

1. `reset_repository_diff_caches` 把 `WeeklyVersionDiffCache` 清了（那四项进了
   `DIFF_SETTING_FIELDS`）—— 这一步是对的；
2. 下次同步重算，可**文件确实没变**，算出来的四元组与清之前一模一样；
3. 身份相同 ⇒ 调度器判「输入一字未变」⇒ **自动分析跳过**。

而这次改动真正换掉的是**模型能看到的东西**：列名取自哪一行、哪些行算数据、
A 列算不算数据。用户改了配置，下一次自动分析什么都没发生，面板上也没有任何地方
说得出为什么 —— 这正是本模块要堵的那个洞。

把这个指纹混进内容身份之后，「改了怎么读」就等于「换了一份输入」。

## 为什么单独一个模块

`scope_sampling` 依赖 `snapshot_store`（后者用它的 `weekly_snapshot_digest` 口径），
所以这两个模块里**哪一个都不能**放这个函数 —— 会形成循环 import。
而它确实是两处共用的一件事：只有一处实现，两边的身份才逐字相同。
"""

from __future__ import annotations

import hashlib
from typing import Sequence

from models import Repository, WeeklyVersionConfig, db
from services.excel_header_profiles import parse_config


def scoped_version(version, scope: str) -> str:
    """把「比较口径」并进版本号 —— 内容身份因此能表达「怎么读」，不只是「读了什么」。

    **做差的两边必须用同一个函数、同一个位置**：快照条目在 `seal_snapshot` 里写进去，
    当前缓存条目在 `_select_delta_entries` 里现算。只并一边的后果不是「漏报」而是
    **全量失真** —— 两边永远不相等，改配置之前每一个文件都判「没变」、
    改之后每一个都判「变了」。

    列宽 40（`AiDiffSnapshotItem.diff_version`）；`"1.18.0|ab12cd34"` 是 15 字符。
    """
    if not scope:
        return version
    return f"{version or ''}|{scope}"


def _profiles_digest_text(raw) -> str:
    """把 `header_profiles` 那一列 JSON 压成**语义表示**再进指纹。

    直接拿原文进哈希有两个后果，都是白花钱：

    * 改一句**说明文本**（`note`，纯展示字段，模型一个字都看不到）会让指纹变
      ⇒ 自动分析重跑一遍（约 ¥3.4）；
    * 只是调整了 JSON 的缩进、或调换了键的书写顺序（语义一字未改）也一样。

    所以这里过一遍 `parse_config`，只取**影响读法**的东西：方案的坐标，以及规则的
    匹配方式与目标。`note` 与 `label` 都不取 —— 它们不改变模型看到的内容。

    **两个列表都保持原序、不排序**：`bindings` 的顺序有语义（`path_regex` 是
    「按用户填的顺序取首个命中」，见 `resolve_for_file`），排序会让两套先后不同的
    配置算出同一个指纹。
    """
    config = parse_config(raw)
    parts = [
        "P|%s|%s|%s|%s|%s"
        % (
            profile.key,
            profile.header_rows if profile.header_rows is not None else "",
            profile.header_name_row if profile.header_name_row is not None else "",
            profile.key_columns or "",
            profile.marker_column or "",
        )
        for profile in config.profiles
    ]
    parts += [
        "B|%s|%s|%s" % (binding.match, binding.value, binding.profile_key)
        for binding in config.bindings
    ]
    return ";".join(parts)


def header_scope_fingerprint(config_ids: Sequence[int]) -> str:
    """这批配置所属仓库的比较口径指纹（sha1 前 8 位；查不到就返回空串）。

    取的是 `Repository` 上那四项**影响读法**的配置：

    * `header_rows` / `header_name_row` / `key_columns` —— 仓库级的坐标标量；
    * `header_profiles` —— 「一个仓库并存多种表头格式」时的按文件选用规则。

    **前半段原值直接参与哈希**：`header_rows` / `header_name_row` / `key_columns` 三个
    标量是从 int 列读出来的，填 `3` 与填 `"3"` 在库里都是 int，不会因为写法不同而抖动。
    **`header_profiles` 那一列相反，要先过语义规范化**（见 `_profiles_digest_text`）——
    它是用户手写的 JSON 文本，原文里混着缩进和纯展示字段，直接哈希会让「改了一句说明」
    或「只是重新缩进」这种与读法无关的改动也付出一次重跑的代价。

    查不到（配置刚被删、库不通）时返回空串：空串是个**常量**，不会让指纹无端抖动 ——
    这里宁可在异常时退化成今天的行为，也不要因为一次查询失败就让每轮分析都重跑。
    """
    ids = []
    for raw in config_ids or ():
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not ids:
        return ""
    try:
        rows = (
            db.session.query(
                WeeklyVersionConfig.id,
                WeeklyVersionConfig.repository_id,
                Repository.header_rows,
                Repository.header_name_row,
                Repository.key_columns,
                Repository.header_profiles,
            )
            .outerjoin(Repository, Repository.id == WeeklyVersionConfig.repository_id)
            .filter(WeeklyVersionConfig.id.in_(ids))
            .all()
        )
    except Exception:  # noqa: BLE001 —— 见 docstring：异常时退化成今天的行为
        return ""
    if not rows:
        return ""
    parts = sorted(
        "%s|%s|%s|%s|%s|%s"
        % (
            config_id or 0,
            repository_id or 0,
            header_rows if header_rows is not None else "",
            header_name_row if header_name_row is not None else "",
            key_columns or "",
            _profiles_digest_text(header_profiles),
        )
        for config_id, repository_id, header_rows, header_name_row, key_columns, header_profiles in rows
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:8]
