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


def header_scope_fingerprint(config_ids: Sequence[int]) -> str:
    """这批配置所属仓库的比较口径指纹（sha1 前 8 位；查不到就返回空串）。

    取的是 `Repository` 上那四项**影响读法**的配置：

    * `header_rows` / `header_name_row` / `key_columns` —— 仓库级的坐标标量；
    * `header_profiles` —— 「一个仓库并存多种表头格式」时的按文件选用规则。

    **原值直接参与哈希，不做规范化**：这里的用途是「变了没有」，不是「等价类相同」
    （要规范化的是另一件事，见 `agent_file_content_dispatch.header_config_fingerprint`）。
    填 `3` 与填 `"3"` 在库里都是 int，所以不会因为写法不同而抖动。

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
            header_profiles or "",
        )
        for config_id, repository_id, header_rows, header_name_row, key_columns, header_profiles in rows
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:8]
