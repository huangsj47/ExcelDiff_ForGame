# -*- coding: utf-8 -*-
"""用户选的「分析范围」（focus）：**同一个范围概念，在两个总体上的落法**。

## 为什么单独一个模块

`services/ai_analysis_service.py` 贴着仓库的 2000 行硬上限
（`scripts/check_file_length.py --strict`）。这一组是**纯判据**：只读配置对象与
delta 的 `repository_id`，不碰库、不发请求、不写报告 —— 与「怎么跑一次分析」没有耦合，
搬出来之后服务层只留调用。

## 两个总体，一套取值域

`focus` 的取值域只有四个（`all` / `table` / `code` / 仓库 id），但它要落在两种东西上：

* **文件**（`delta_files`）—— `filter_delta_files_by_focus`，判据落在每条 delta 的
  `repository_id` 上，还要给出「给人看的范围名」（会渲染进提示词）；
* **配置**（`WeeklyVersionConfig`）—— `configs_in_focus`，判据落在 `cfg.repository_id`
  与仓库的 `resource_type` 上，给窗口提交账用（`commit_detail` 的白名单由它收窄）。

两者**不是同一条判据抄两遍**，因为边界情形不同：delta 的 `repository_id` 可能压根不在
本批 configs 里（那时 `repo_by_id.get()` 给 `None`，按「代码」算，于是「只看代码」会
保留它），而配置一定有自己的仓库。但**取值域与「认不出来就不筛」这条口径必须一致**，
改一边要回来看另一边。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

# 与 `services/ai/job_service.FOCUS_ALL` **同一个值**。此处从那边 import 而不是再写一个
# 字面量：仓库里已经有两份（`job_service` 与 `ai_analysis_service`），第三份只会更多。
from services.ai.job_service import FOCUS_ALL

__all__ = [
    "FOCUS_ALL",
    "configs_in_focus",
    "filter_delta_files_by_focus",
    "resource_type_of",
]


def resource_type_of(repo: Any) -> str:
    """仓库的资源类型，**空值按「代码」算**。

    `models/repository.py` 写明取值是 `'table' / 'res' / 'code'`，而这一列**可空**
    （写入侧还有一条裸赋值会写进 NULL）。界面上那一栏的选项是这么分的：

        {% if (cfg.repository.resource_type or 'code') == 'table' %}…配表…{% else %}…代码…

    也就是 **NULL 与 `'res'` 都算代码仓库**。这里原先回的是空串，而调用方拿它去比
    `== "code"` —— `"" != "code"`，于是用户选「只看代码仓库」时，那些 `resource_type`
    为空的仓库的改动**一条都不会进输入**，而报告上写着「仅代码仓库」，模型据此把
    一个缺口说成覆盖完整。两侧必须同一套判据。
    """
    kind = str(getattr(repo, "resource_type", "") or "").strip().lower()
    return kind or "code"


def configs_in_focus(configs: Sequence[Any], focus: Optional[str]) -> List[Any]:
    """落在用户选的范围里的**配置**（给窗口提交账用）。

    ## 为什么要收窄

    `commit_detail` 的白名单由窗口提交账撑起。不收窄的话，用户选「只看仓库 A」时窗口里
    仍带着仓库 B 的提交，于是白名单**放行本批次根本不该看的仓库** —— 那正是
    `AnalysisScope.repository_ids_by_commit` 那条跨仓判据（REV-AI-001）要挡的事。

    识别不出来 / 指向不存在的仓库时**退回全量**（不擅自收窄）：`focus` 是 URL 参数，
    不能让它把一次分析变成空跑，也不能让它悄悄改变权限边界之外的语义。
    """
    items = list(configs)
    text = str(focus or "").strip()
    if not text or text == FOCUS_ALL:
        return items
    if text in ("table", "code"):
        want_table = text == "table"
        return [
            cfg for cfg in items
            if (resource_type_of(getattr(cfg, "repository", None)) == "table") is want_table
        ]
    try:
        repo_id = int(text)
    except (TypeError, ValueError):
        return items
    kept = [cfg for cfg in items if getattr(cfg, "repository_id", None) == repo_id]
    return kept or items


def filter_delta_files_by_focus(
    delta_files: List[dict], focus: Optional[str], configs: Sequence[Any]
) -> Tuple[List[dict], str]:
    """按用户选的「分析范围」筛文件，返回 (筛选后的清单, 给人看的范围名)。

    ## 为什么要有这个

    一个 767 个文件的版本，**人比任何自动策略都清楚这周该看哪一半**：这周改的是配表数值，
    下几周才轮到代码。让用户先选范围，比在服务端猜「哪 200 个更重要」准得多，而且成本是
    线性的（筛选之后清单短了、额度也集中了）。

    取值：`all`（或空）不筛；`table` / `code` 按仓库的 `resource_type` 筛；数字按
    仓库 id 筛。**认不出来的一律不筛**（`focus` 是 URL 参数，不能让它把分析变成空跑）。
    """
    text = str(focus or "").strip().lower()
    if not text or text == FOCUS_ALL:
        return list(delta_files), ""

    repo_by_id = {cfg.repository_id: cfg.repository for cfg in configs}

    if text in ("table", "code"):
        # **只有 `'table'` 算配表**，其余（含 `'res'` 与空值）都算代码 —— 与模板里
        # 「`resource_type or 'code'` 是否等于 `'table'`」那三行是同一套判据。
        # 判据分叉的后果见 `resource_type_of`：选「只看代码仓库」会静默吞掉老仓库。
        want_table = text == "table"
        kept = [
            item for item in delta_files
            if (resource_type_of(repo_by_id.get(item.get("repository_id"))) == "table")
            is want_table
        ]
        label = "仅配表仓库" if want_table else "仅代码仓库"
        return kept, label

    try:
        repo_id = int(text)
    except (TypeError, ValueError):
        return list(delta_files), ""

    repo = repo_by_id.get(repo_id)
    if repo is None:
        return list(delta_files), ""
    kept = [item for item in delta_files if item.get("repository_id") == repo_id]
    return kept, f"仅仓库「{repo.name}」"
