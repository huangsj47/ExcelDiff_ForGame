#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口提交账：**三个互不相等的数**，以及把它们分开说清楚的那一段话。

## 为什么单独一个模块

模型可见的摘要里原先只有**一个**「N 个提交」，而它来自
`change_set.from_weekly_payload` 按每个文件的 `latest_commit_id` 分组 —— 那是
「有多少个不同的『文件最后一次改动所在的提交』」，**不是**「这个窗口改了多少条提交」。
2026-09-23 实测的报告里因此留下了这条自相矛盾的记录：

    清单说「3 个提交」，而差异的出处（`stored_diff_source._batch_provenance`）说
    覆盖「5/6 条提交」

两边描述的根本不是同一个集合，读者只能猜哪个是真的。修法不是把某一边改小，而是
**让三个数各自有名有姓地出现**：

| 数 | 回答的问题 | 事实来源 |
|---|---|---|
| 实际提交数 | 这个窗口（当前 tip 可达的）一共改了多少条提交 | `commits_log` 窗口内 distinct `commit_id` |
| 文件最新提交数 | 本次输入里的文件，各自「最后一次改动」落在多少个不同提交上 | 每条 delta 的 `latest_commit_id` 去重 |
| 合并差异覆盖的提交数 | 每个文件的合并差异覆盖了几条提交（按文件累加；同一提交被多个文件覆盖会重复计数） | 每条 delta 的 `commit_count` |

第三个数的**累加口径**是刻意的：它正是 `_batch_provenance` 里那个「5/6」的来源
（那一行说的是**这一个文件**的合并差异覆盖了几条提交）。把它写成「去重后的提交数」
会与出处那一行再次对不上 —— 而「对不上」正是本次要修的东西。所以这里如实写明
「按文件累加」，不让读者以为它等于实际提交数。

放新模块而不放进 `change_set.py`：那边的职责是「渲染」，而这里是「数的来源与口径」，
且它要读库（`commits_log`）—— 让 `change_set` 保持纯函数（它现在的调用方与测试都
在无 db 的环境里跑）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence, Tuple

from utils.logger import log_print


@dataclass(frozen=True)
class WindowCommitFacts:
    """窗口提交账。三个数**互不相等是常态**，不是异常。"""

    #: 实际提交数：窗口内 distinct `commit_id` 的个数。取不到时是 `None`（**不是 0**）。
    actual: int | None = None
    #: 文件最新提交数：本次输入的 delta 文件里 distinct `latest_commit_id` 的个数。
    latest: int = 0
    #: 合并差异覆盖的提交数：按文件累加（同一提交被多个文件覆盖会重复计数）。
    merged_total: int = 0
    #: 单个文件里最大的那一个 —— 它对应差异出处那句「覆盖 N 条提交」。
    merged_max: int = 0
    #: 合并差异覆盖**不止一条**提交的文件 `(路径, 条数)`，按条数降序。用于在清单里
    #: 点名「这几个文件的差异不是单独某一条提交的改动」。
    multi: Tuple[Tuple[str, int], ...] = ()
    #: 参与统计的 delta 文件数。
    files: int = 0

    @property
    def meaningful(self) -> bool:
        """三个数至少有一个不为零时才算「有一份账」。全零时一个字都不说。"""
        return bool(self.files or self.latest or self.merged_total or self.actual)


def _commit_count(entry: Mapping[str, Any]) -> int:
    try:
        return max(0, int(entry.get("commit_count") or 0))
    except (TypeError, ValueError):
        return 0


def facts_from_payload(
    payload: Mapping[str, Any], *, window_commit_ids: Sequence[str] = ()
) -> WindowCommitFacts:
    """从 payload 算出这份账。**纯函数** —— 不读库、不猜。

    `window_commit_ids` 由写侧（`scope_sampling.build_weekly_payload`）带下来：它要读
    `commits_log`，而那是这一层不该做的事。缺它时 `actual` 保持 `None`（未知），
    渲染那一段会只说另外两个数 —— 「没记录」不许写成 0。
    """
    rows = list(payload.get("delta_files") or ())
    latest: list[str] = []
    merged_total = 0
    merged_max = 0
    multi: list[tuple[str, int]] = []
    files = 0
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        files += 1
        commit = str(item.get("latest_commit_id") or "").strip()
        if commit and commit not in latest:
            latest.append(commit)
        count = _commit_count(item)
        merged_total += count
        merged_max = max(merged_max, count)
        path = str(item.get("file_path") or "").strip()
        if path and count > 1:
            multi.append((path, count))

    ids = [str(item).strip() for item in (window_commit_ids or ()) if str(item).strip()]
    actual = len(set(ids)) if ids else None
    return WindowCommitFacts(
        actual=actual,
        latest=len(latest),
        merged_total=merged_total,
        merged_max=merged_max,
        multi=tuple(sorted(multi, key=lambda pair: (-pair[1], pair[0]))),
        files=files,
    )


def describe_commit_counts(facts: WindowCommitFacts) -> str:
    """把三个数渲染成**给模型看的一段话**（由 `change_set` 拼进变更清单）。

    ## 三句话必须都在，且必须点名各自的口径

    * 少了第一句，模型会把「文件最新提交数」当成窗口的提交数 —— 这就是那条「3 个提交」
      的来源，而实际窗口里可能有别的提交只改了本次没装进输入的文件；
    * 少了第三句，模型读到差异出处那句「覆盖 N 条提交」时会以为平台前后矛盾，进而在
      报告里写一条**不存在**的信息缺口；
    * 口径写反（把累加说成去重）会让三个数在别的批次里再次对不上。
    """
    if not facts.meaningful:
        return ""
    lines = ["## 这次变更的提交账（三个数不是一回事，报告里请分开写）", ""]
    if facts.actual is not None:
        lines.append(
            f"- **本窗口实际提交数：{facts.actual}**"
            "（当前分支 tip 可达、且落在本窗口时间范围内的提交，去重后）"
        )
    else:
        lines.append(
            "- 本窗口实际提交数：**本次没有记录**（写侧未带上窗口提交清单）——"
            "不要把它写成 0，也不要用下面两个数替代它"
        )
    lines.append(
        f"- 文件最新提交数：{facts.latest}"
        f"（本次输入的 {facts.files} 个文件里，`latest_commit_id` 去重后的个数）——"
        "**这是每个文件「最后一次改动」所在的提交，不是本窗口的提交总数**"
    )
    lines.append(
        f"- 每文件合并差异覆盖的提交数：合计 {facts.merged_total}、单个文件最多 {facts.merged_max}"
        "（**按文件累加**：同一条提交改了多个文件就会被计多次）。"
        "取 `file_diff` 时看到的「覆盖 N 条提交」说的就是这个单文件口径"
    )
    lines.append(
        "上面三个数**本来就可以不相等**：一个只改了本次未装进输入的文件的提交只进第一个数；"
        "一个文件被多条提交改过时，它的合并差异覆盖的是那几条。**不要把它们当成互相矛盾**，"
        "也不要凭其中任何一个去断言「本版本一共改了多少」。"
    )
    if facts.multi:
        shown = "、".join(f"`{path}`（{count} 条）" for path, count in facts.multi[:5])
        more = f"，另有 {len(facts.multi) - 5} 个" if len(facts.multi) > 5 else ""
        lines.append(
            f"\n**下面清单里 `## 提交 <id>` 的小标题写的是「这个文件在本窗口的最后一次改动」，"
            f"不是「这一整段差异都是这条提交做的」。** 有 {len(facts.multi)} 个文件的合并差异"
            f"覆盖了不止一条提交（{shown}{more}）：它们的 `file_diff` 给的是**覆盖那几条"
            "提交的合并差异**，索取时回执里也会这么写。要判断「是哪一条提交引入了这一行」，"
            "用 `commit_detail` 逐条比对，不要把它算在小标题那条提交头上。"
        )
    return "\n".join(lines) + "\n"


def window_commit_ids(configs: Iterable[Any]) -> Tuple[str, ...]:
    """窗口内、当前仓库上的 distinct 提交号（**读库**，给写侧用）。

    窗口换算走 `weekly_window_in_utc`（唯一实现，见该函数的说明：config 的
    start/end 是北京墙钟，而 `commit_time` 是 naive-UTC，不换算就差 8 小时）。

    查询失败时返回空元组 = 「没记录」（渲染那一段会说「本次没有记录」，不写成 0）。
    """
    items = [cfg for cfg in (configs or ()) if getattr(cfg, "id", None)]
    if not items:
        return ()
    try:
        from models import Commit
        from services.weekly_version_logic import weekly_window_in_utc
    except Exception as exc:  # noqa: BLE001 —— 拿不到依赖只是少一个数
        log_print(f"⚠️ AI 分析：窗口提交账取不到（{exc}），本次不写「实际提交数」", "AI")
        return ()

    found: list[str] = []
    for cfg in items:
        try:
            start_utc, end_utc = weekly_window_in_utc(cfg)
        except Exception:  # noqa: BLE001
            continue
        if start_utc is None or end_utc is None:
            continue
        try:
            rows = (
                Commit.query
                .filter(
                    Commit.repository_id == cfg.repository_id,
                    Commit.commit_time >= start_utc,
                    Commit.commit_time <= end_utc,
                )
                .with_entities(Commit.commit_id)
                .distinct()
                .all()
            )
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 分析：窗口提交账查询失败（config={cfg.id}）：{exc}", "AI")
            continue
        for row in rows:
            value = row[0] if not isinstance(row, str) else row
            text = str(value or "").strip()
            if text and text not in found:
                found.append(text)
    return tuple(found)
