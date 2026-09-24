#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""必读清单的**进度**：这一片分到的文件里，还有几条没取到证据。

## 为什么需要它（P1b，实测 run 58）

分工是**确定性**的：平台按 `(仓库, 路径, 提交)` 把 120 个文件分给若干分片
（`manifest` / `subagent.apply_manifest`）。取证却是**模型自主**的：它想要什么就要什么，
平台只把清单念给它听一次（任务书）。于是「分配 120 个、取到证据 63 个」这件事在**运行
过程中谁都不知道** —— 模型不知道（它没有这个数），编排层也不知道（它只有事后才从
`ai_analysis_trace` 算出来的覆盖账）。

这一层把它变成**每轮都能问一次**的数：清单进来，取到的数出去；`note()` 渲染成一句能
照做的话（「还剩哪几条、先把它们取一次」）。它同时是「逐轮进度」的一行 —— 模型原先
看得到的进度只有「还能索取 N 次」，没有「你手上这活干到哪了」。

## 判据：**同一份**「取到证据」口径，不在这里另立一套

「算不算取到证据」由 `trace_evidence.FILE_EVIDENCE_KINDS` 与
`trace_evidence.failure_notice` 定（失败说明长得像内容，只能按前缀认）。这一层**复用**
那两个判据，不重写一遍：两处各写一份必然漂移，而漂移的表现是「面板说取到了 63 个、
进度行说还有 40 条没取」这种自相矛盾。

## 它不记「碰过」，只记「取到」

`file_diff` 命中本地缓存时交给模型的是一句「见上文那一节」的指针 —— 那**不是**证据
（`context_tools` 模块 docstring 第 4 条）。这类条目在这里不算数：算进去会让进度行在
模型真正需要它的时候显示「已经读完了」。跨成员共享的那一份**算数**（给出的是全文）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, Tuple

from services.ai.scope import normalize_path
from services.ai.trace_evidence import FILE_EVIDENCE_KINDS, failure_notice

#: 进度行里最多列出几条还没取证据的文件。**8 条是「看得见、能照做」与「每轮都付这份
#: 提示词钱」之间的折中**：这几行每轮都要重发一次（一轮一次模型调用），列 60 条等于
#: 每轮多花几千字符，而模型真正需要的是「还剩几条 + 最该先看的那几个」。
MANDATORY_NOTE_MAX_ITEMS = 8

#: 提交号允许的最短前缀。模型可以只写短号（仓库里通常 7~12 位），平台那边存的是全号；
#: 两边按前缀认。低于这个长度不当成匹配 —— 4 个字符的号在两个仓库里撞车的概率不低，
#: 而「张冠李戴地记成已读」正是这一层最贵的错（比漏记一条贵得多）。
MIN_COMMIT_PREFIX = 7


def _commit_matches(left: str, right: str) -> bool:
    """两个提交号是不是同一条：相等，或者一个是另一个的前缀（≥ `MIN_COMMIT_PREFIX`）。"""
    first = str(left or "").strip().lower()
    second = str(right or "").strip().lower()
    if not first or not second:
        return False
    if first == second:
        return True
    short, long = (first, second) if len(first) < len(second) else (second, first)
    if len(short) < MIN_COMMIT_PREFIX:
        return False
    return long.startswith(short)


def _repository_matches(request_repo: Any, entry_repo: Any) -> bool:
    """仓库这一维：**只在两边都写得出时收窄**（与覆盖账同一口径）。

    模型没点名仓库（`None` / 空串）时不因此判不匹配 —— 那是「它没写」，不是「它写的是
    另一个」。反过来，点了名就必须对上：同一条 `(提交, 路径)` 落在两个仓库里是两份不同的
    内容（P1a），把它记成「已读」会让进度行提前归零。
    """
    left = str(request_repo or "").strip()
    right = str(entry_repo if entry_repo is not None else "").strip()
    if not left or not right:
        return True
    return left == right


@dataclass
class MandatoryProgress:
    """一片的必读清单进度。**每个成员一份**（`ContextTools` 持有一个）。

    `entries` 是 `AssignedFile` 那样的对象（`repository_id` / `commit` / `path` /
    `source`），只按属性读 —— 这一层不 import 计划层的类型，免得编排改动波及它。
    """

    entries: Tuple[Any, ...] = ()
    #: 已经取到证据的那些条目的下标。用下标不用路径：清单里同一个路径可能出现在
    #: 两条提交上（历史 + 本次），按路径去重会把其中一条永远算成没读。
    done: set = field(default_factory=set)

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def missing(self) -> Tuple[Any, ...]:
        """还没取到证据的条目（**保持清单顺序**：先列出来的先被看到）。"""
        return tuple(
            entry for index, entry in enumerate(self.entries) if index not in self.done
        )

    @property
    def remaining(self) -> int:
        return len(self.missing)

    def observe(self, request: Any, item: Any) -> None:
        """一次取数的产出 → 它把哪几条必读文件取到了（`ContextTools` 每个分支都调一次）。

        只认三件事同时成立：请求类型是文件类证据、**内容真在手**（不是失败说明、不是
        本地缓存的那句指针）、三元组对得上。任何一条不成立就什么都不做 —— 这个函数的
        错误方向必须是「少记一条」，因为少记只是让进度行多说一句「还没读」。
        """
        if not self.entries:
            return
        if str(getattr(request, "type", "") or "") not in FILE_EVIDENCE_KINDS:
            return
        if not _has_content(item):
            return
        path = normalize_path(getattr(request, "path", ""))
        if not path:
            return
        commit = getattr(request, "commit", "")
        repository = getattr(request, "repository_id", "")
        for index, entry in enumerate(self.entries):
            if index in self.done:
                continue
            if normalize_path(getattr(entry, "path", "")) != path:
                continue
            if not _commit_matches(commit, getattr(entry, "commit", "")):
                continue
            if not _repository_matches(repository, getattr(entry, "repository_id", None)):
                continue
            self.done.add(index)

    def note(self, *, limit: int = MANDATORY_NOTE_MAX_ITEMS) -> str:
        """每轮那一行进度。**没有清单时返回空串**（单代理、汇总、对账轮）。

        三句话的分工：还剩几条（数）、剩下的是哪几个（能照做）、取不到时怎么写（不许
        把它写成「没问题」）。最后那句不是客套 —— 这一行的目的正是让「没读到」在报告里
        有一条明确的去处，否则模型会用「本文件无风险」把缺口填掉。
        """
        if not self.entries:
            return ""
        missing = self.missing
        if not missing:
            return (
                f"**必读清单进度**：本次分给你的 {self.total} 个文件**已全部取到证据**。"
                "接下来把额度用在跨文件核对与反证上。"
            )
        shown = missing[: max(1, int(limit or 1))]
        lines = "".join(f"\n- {_describe(entry)}" for entry in shown)
        more = (
            f"\n- ……另有 {len(missing) - len(shown)} 个，完整清单见 `change-manifest`"
            if len(missing) > len(shown)
            else ""
        )
        return (
            f"**必读清单进度**：本次分给你的 {self.total} 个文件里，"
            f"**还有 {len(missing)} 个没取到证据** —— 这是本次分析的覆盖底线，"
            "先把下面这些各取一次 `file_diff`，再去做你那几个维度的深挖。"
            f"{lines}{more}\n如果某一条在额度用尽前都没取到，请在报告里如实写成"
            "「信息缺口：该文件未取到证据」，**不要写成「没有风险」**。"
        )


def _describe(entry: Any) -> str:
    """一行地址：三样都写全（模型照着填就能发一条 `file_diff`）。

    `AssignedFile.describe()` 已经这么写了；这里对**任何**带这三个属性的对象都成立 ——
    进度行的调用方不该被迫构造计划层的类型。
    """
    describe = getattr(entry, "describe", None)
    if callable(describe):
        return str(describe())
    path = str(getattr(entry, "path", "") or "")
    commit = str(getattr(entry, "commit", "") or "")[:12]
    repository = getattr(entry, "repository_id", "")
    return f"`{path}`（提交 {commit}、仓库 {repository}）"


def _has_content(item: Any) -> bool:
    """这一条目**真的带着内容**吗（`context_tools` 那三条判据的只读版）。

    失败说明（`[取数失败]`…）与工具自报的失败都不算内容 —— 判据是**同一份**
    （`trace_evidence.failure_notice`），面板上那一列「失败」走的也是它。
    """
    if item is None:
        return False
    meta = dict(getattr(item, "meta", None) or {})
    if meta.get("tool_failed") or meta.get("tool_empty"):
        return False
    # 本地缓存命中时交给模型的那一句指针：内容不在这一条里（见模块 docstring）。
    if meta.get("repeat_pointer"):
        return False
    return not failure_notice(getattr(item, "text", ""))


def progress_of(entries: Iterable[Any] | None, done: Sequence[int] = ()) -> MandatoryProgress:
    """构造入口（给不需要自己攒状态的调用方，如测试与诊断）。"""
    return MandatoryProgress(entries=tuple(entries or ()), done=set(done))


def coverage_precheck(*, assigned: int, pool: int) -> str:
    """**启动前**的覆盖预检：必读集要几次索取、池里有几次。够用时返回空串。

    ## 为什么要在启动前问这一句

    「必读 N 个文件、池里只有 M 次」这件事在实测里是**跑完才知道**的：报告末尾写一句
    「本次有 57 个文件未取到证据」，用户读到时钱已经花了、这一轮也过去了。而它在开跑前
    完全可算 —— 分片是平台分的（`manifest`）、池是配置决定的（`FamilyQuota`），两个数
    都在手里。读一份 diff 要一次索取，所以「把分到的文件各读一次」的代价就是它们的条数。

    ## 口径：去重、且只算**必读**这一笔

    `assigned` 是同一条 `(仓库, 提交, 路径)` 在全家去重后的条数（一个文件落在两个分片里
    只需要被读一次），`pool` 是全家共享池的索取总量。返回一句**能照做**的话（缺口多少、
    两条出路各是什么），够用时返回空串 —— 这一句是告警，不是每轮都要说的进度。

    它**不拦路**：额度紧不等于这一轮不该跑（报告照样能出，只是覆盖缺口要如实写）。
    """
    need = max(0, int(assigned or 0))
    have = max(0, int(pool or 0))
    if not need or need <= have:
        return ""
    return (
        f"⚠️ 覆盖预检：本次分给各分片的必读文件共 {need} 条（去重后），"
        f"而全家共享池只有 {have} 次索取 —— **按「每个文件至少读一次」算，本次读不完**"
        f"（缺口 {need - have} 条）。两条出路：调高「单个 agent 可索取次数」"
        "（池 = 分片数 × 该值），或缩小分析窗口让改动文件少一些。"
        "本轮报告会把没读到的那几条如实写成信息缺口，不会当成「没问题」。"
    )
