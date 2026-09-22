#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台**已经算好并落库**的那份 diff：取出来，并如实交代它的出处。

## 为什么单独一个文件

`services/ai/platform_provider.py` 贴着仓库的长度闸门（`scripts/check_file_length.py --strict`
在 2000 行报错）。这一块是那个文件里**唯一读数据库**的一段（`WeeklyVersionDiffCache`），
与渲染、与 `ContextProvider` 的状态机都没有耦合：入参是 `(repository_id, path, commit)`，
出参是 `(载荷, 出处说明)`。单独一层之后，「平台本地有没有这份数据」这件事只需要看这一个
文件。命名与 `baseline_source.py`（已落库**结论**的取数来源）同构。

## 为什么 diff 要读「平台已经算好的那一份」

取数还有第三种失败方式：**平台本地根本没有这份数据**。diff 原先一律现场重算（读本地
工作副本），而 platform/agent 模式下平台被显式禁止 clone，代码文件又没有单文件 diff
缓存 —— 于是代码仓库的 diff 永远取不到，而「取不到」被渲染成「取到了记录但没有补丁
内容」。这里读的是平台**已经算好并落库**的周版本合并 diff（周版本页面读的同一个 payload）。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from utils.logger import log_print


def _weekly_stored_diff(
    repository_id: Optional[int],
    path: str,
    commit: str,
    *,
    row_id: Optional[int] = None,
):
    """这个文件在这个提交上的、**平台已经算好并落库**的周版本合并 diff。

    返回 `(载荷, 出处说明)`：载荷是渲染层能直接吃的
    `{'type': 'code'|'text'|'excel'|'segmented_diff', …}`，没有就是 `(None, "")`；
    出处说明是给模型看的一行「这是谁的 diff」，只在合并了多条提交时才有内容
    （见下面的「不许张冠李戴」）。

    ## 为什么 AI 要读这一份，而不是自己再算一遍

    1. **它就是评审者在页面上看的那一份。** `WeeklyVersionDiffCache.merged_diff_data`
       是周版本同步（`weekly_version_logic.generate_weekly_merged_diff`）算出来落库的，
       周版本页面的取数（`weekly_version_file_diff_api` → `generate_weekly_git_diff_html`
       / `generate_weekly_excel_merged_diff_html`）读的就是它。让模型读别的来源，
       它给出的结论就没法与人看到的页面对账。

    2. **自己算的那一份是「单提交 vs 前一提交」，在周版本里只是其中一段。**
       把这一份给它，等于把一个文件一周里的改动只看最后一次。

    3. **平台本地往往根本没有工作副本。** platform/agent 模式下平台被显式禁止 clone
       （`get_file_content_from_git` 直接返回 None），而**代码文件的 diff 没有单文件缓存**
       （`DiffCache` / `ExcelDiffCacheService` 都是配表专用的），于是实时路径必然读不到
       任何内容 —— 那时旧渲染层给出的「取到了记录但没有补丁内容」是一句假话，
       模型读到的是「这里没什么可看的」。配表之所以没这个问题，正是因为它的 diff 有缓存。

    ## 为什么按 (仓库, 路径, latest_commit_id) 精确匹配

    变更清单里给模型的每个文件都带着自己的 `latest_commit_id`（就是这一列），模型
    索取时原样传回来 —— 于是「它问的那个提交」与「这条缓存行」是同一个东西，
    不需要再猜「哪一次周版本」。

    **但这个键不唯一**（REV-AI-003）：缓存行是**按 `config_id` 写的**，而同一个仓库、
    同一个文件、同一个 `latest_commit_id` 在两个周窗口里都可能出现（回填日期的提交、
    同一时刻的两条提交都会让两个窗口的终点撞上）。这时旧实现 `order_by(id.desc())`
    就是**在猜**，而且会猜中后建的那个窗口。

    所以现在的口径是：

    * 传了 `row_id`（写侧把这条 delta 的来源行带下来了）→ **按主键取**，不猜；
    * 没传 → 旧查询照跑，但**命中多行时拒绝猜测**，返回 `(None, "")` 让调用方按既有
      链路降级（现场重算 / 问 Agent），而不是悄悄给一份别的窗口的差异。

    ## 不许张冠李戴

    这条缓存覆盖的是**一个窗口**（可能好几条提交），而模型的索取长成
    `file_diff(commit=X, path=p)` —— 一个问「提交 X 改了什么」的形状。窗口里不止一条
    提交时，模型会把整段窗口的改动都算到 X 头上（而它没有任何办法发现）。所以合并了
    多条提交时，出处说明会明写「这是覆盖 N 条提交的合并差异」。

    `diff_version` **不在这里校验**：口径版本决定的是「周版本同步要不要重算」
    （`needs_merged_diff_cache` → `is_merged_diff_cache_current`），读取侧
    （含页面的 Excel 分支 `load_weekly_excel_diff_from_cache`）本来就不看它。
    """
    if not repository_id or not path or not commit:
        return None, ""
    from models.weekly_version import WeeklyVersionDiffCache

    try:
        if row_id is not None:
            row = WeeklyVersionDiffCache.query.filter_by(id=row_id).first()
        else:
            # 旧路径（老 payload 没带行主键）。**命中多行时拒绝猜测** —— 见 docstring：
            # 猜中的是另一个周窗口那一份，而它看起来完全正常。
            candidates = (
                WeeklyVersionDiffCache.query.filter_by(
                    repository_id=repository_id, file_path=path, latest_commit_id=commit
                )
                .order_by(WeeklyVersionDiffCache.id.desc())
                .limit(2)
                .all()
            )
            if len(candidates) > 1:
                log_print(
                    f"⚠️ AI 取数：{path} 在提交 {str(commit)[:8]} 上有 {len(candidates)} 条"
                    "周版本缓存（两个窗口的终点撞上了），这一份没有带来源行，不猜是哪一条",
                    'AI',
                )
                return None, ""
            row = candidates[0] if candidates else None
    except Exception as exc:  # noqa: BLE001 —— 取不到只是少一条来源，不该让整次索取失败
        log_print(f"⚠️ AI 取数：查周版本合并 diff 失败 {path}: {type(exc).__name__}: {exc}")
        return None, ""
    if row is None or not row.merged_diff_data:
        return None, ""
    try:
        envelope = json.loads(row.merged_diff_data)
    except (TypeError, ValueError) as exc:
        log_print(f"⚠️ AI 取数：周版本合并 diff 解析失败 {path}: {exc}")
        return None, ""
    if not isinstance(envelope, Mapping):
        return None, ""
    # 外壳是 `generate_merged_diff_data` 的元数据（commit_ids / authors / merge_strategy…），
    # 真正的载荷在 `diff_data` 与 `merged_diff` 里（同一个对象，历史原因各留了一份）。
    for key in ("diff_data", "merged_diff"):
        candidate = envelope.get(key)
        if isinstance(candidate, Mapping) and candidate:
            return candidate, _batch_provenance(envelope)
    return None, ""


def _is_failed_payload(payload: Any) -> bool:
    """这份载荷是「取不到」还是「真的差异」。

    `type == 'error'`（`get_unified_diff_data` 读不到内容时给的）与带 `error` 键的载荷
    （配表侧的读取失败）都是**失败说明**，渲染出来是一句完整的话 —— 它长得像内容，
    所以调用方必须能分辨它：否则「本地算不出来」会悄悄变成「给模型一句取数失败」就结束了，
    而真正能算出来的那台机器（业务节点的 Agent）根本没被问过。
    """
    if not isinstance(payload, Mapping):
        return True
    return bool(str(payload.get("type") or "").strip() == "error" or payload.get("error"))


def _batch_provenance(envelope: Mapping[str, Any]) -> str:
    """这份合并 diff 覆盖了哪些提交 —— 只在**不止一条**时给说明。

    一条提交时它逐字等于「这条提交的差异」，说明只是噪音；两条以上时不说清楚，
    模型会把整段窗口算到它问的那一条提交头上。
    """
    commit_ids = envelope.get("commit_ids")
    ids = [str(item) for item in commit_ids if item] if isinstance(commit_ids, Sequence) \
        and not isinstance(commit_ids, (str, bytes)) else []
    count = len(ids) or int(envelope.get("commits_count") or 0)
    if count <= 1:
        return ""
    if ids:
        scope = f"（{ids[0][:8]} … {ids[-1][:8]}）"
    else:
        scope = ""
    return (
        f"（出处：平台已落库的**合并差异**，覆盖本批次的 {count} 条提交{scope} ——"
        "它是这个文件在本批次里的全部改动，**不是单独某一条提交的改动**。）"
    )
