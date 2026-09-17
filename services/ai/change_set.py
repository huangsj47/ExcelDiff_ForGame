"""把平台的 payload 转成引擎要的三样东西。

## 为什么单独一层

引擎（`engine.run_analysis`）要的是字符串 + `AnalysisScope`，而平台那边的 payload 是
两个**形状不同**的 dict：单提交模式给一个 `commit` 对象（带 message/author/时间），周版本
模式给一列 `delta_files`（每项只有 `latest_commit_id`，**没有 message**）。把这两种形状的
差异挡在这一层，引擎就只需要认识一种输入，两种模式的接线也不用各写一遍。

## 周版本模式的固有信息缺口（不掩饰）

`weekly_version_diff_cache` 只存 `latest_commit_id`，不存提交信息。所以周版本的变更清单
里**没有提交说明**。这不是可以就地补上的东西 —— 它需要回源到仓库，而那正是模型用
`commit_detail` 这项工具去做的事。所以这里如实留空，不编造一份「提交信息」出来。

## 表与生成物

`bundles` 的说明行会被拼进变更清单：同一张表与它的生成物是**一次改动**，分开看每一侧
都正常，「表改了、产物没跟上」只有一起看才看得见。识别规则与项目无关，见 `bundles.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from services.ai.bundles import build_bundles, describe_bundles
from services.ai.prompt import CommitSummary, FileChange, render_change_summary
from services.ai.scope import AnalysisScope, normalize_path

# 变更清单里最多列几组「表 ↔ 生成物」的关联。列太多会把清单本身挤长，而它每一轮都在
# 提示词里；剩下多少组由 `describe_bundles` 自己说明。
DEFAULT_BUNDLE_LIMIT = 12

_SCOPE_LABELS = {
    "full": "全量",
    "incremental": "增量（只含上次分析之后变化的部分）",
}


@dataclass(frozen=True)
class ChangeSet:
    """引擎的输入。"""

    # 给模型看的变更清单（已经渲染好，含省略说明与表的关联说明）。
    summary: str
    scope: AnalysisScope
    # 本次涉及的全部路径，规范化且去重，保持原有顺序。用于配对「表 ↔ 生成物」。
    paths: tuple[str, ...] = ()
    commits: tuple[CommitSummary, ...] = ()
    # 表与生成物配成一组时的说明行（单文件单元不会出现）。
    bundle_lines: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.commits


def from_commit_payload(
    payload: Mapping[str, object],
    *,
    readable_references: Iterable[str] = (),
    bundle_limit: int = DEFAULT_BUNDLE_LIMIT,
) -> ChangeSet:
    """单提交模式：一个提交、一个文件。"""
    commit = dict(payload.get("commit") or {})
    commit_id = str(commit.get("commit_id") or "")
    path = normalize_path(str(commit.get("path") or ""))

    commits: tuple[CommitSummary, ...] = ()
    if commit_id:
        commits = (
            CommitSummary(
                commit=commit_id,
                message=str(commit.get("message") or ""),
                author=str(commit.get("author") or ""),
                commit_time=str(commit.get("commit_time") or ""),
                files=(FileChange(path=path, operation=_operation(commit.get("operation"))),)
                if path
                else (),
            ),
        )

    return build(
        commits,
        readable_references=readable_references,
        scope_note=_scope_note(payload),
        bundle_limit=bundle_limit,
    )


def from_weekly_payload(
    payload: Mapping[str, object],
    *,
    readable_references: Iterable[str] = (),
    bundle_limit: int = DEFAULT_BUNDLE_LIMIT,
) -> ChangeSet:
    """周版本模式：一列 delta 文件，按 `latest_commit_id` 归到各自的提交下。

    同一个提交下可能有多个文件（同一个提交改了多张表），所以这里按提交分组，而不是
    一个文件一个提交 —— 否则变更清单里会出现同一个提交号重复十几次。
    """
    grouped: dict[str, list[FileChange]] = {}
    for item in payload.get("delta_files") or []:
        entry = dict(item or {})
        commit_id = str(entry.get("latest_commit_id") or "")
        path = normalize_path(str(entry.get("file_path") or ""))
        if not commit_id or not path:
            continue
        grouped.setdefault(commit_id, []).append(FileChange(path=path, operation="M"))

    commits = tuple(
        CommitSummary(commit=commit_id, files=tuple(files))
        for commit_id, files in grouped.items()
    )

    scope_note = _scope_note(payload)
    if payload.get("delta_truncated"):
        scope_note += (
            f"（只列出了优先级最高的 {len(payload.get('delta_files') or [])} 个文件，"
            "不是全部改动。）"
        )

    # 截断前的真实文件数（`summary.total_files`）。清单是取样出来的，
    # 不把这个数传下去，模型会把清单长度当成「本版本的文件数」。
    summary = payload.get("summary") or {}
    try:
        total_files = int(summary.get("total_files"))
    except (TypeError, ValueError):
        total_files = None

    return build(
        commits,
        readable_references=readable_references,
        scope_note=scope_note,
        bundle_limit=bundle_limit,
        total_files=total_files,
    )


def build(
    commits: Sequence[CommitSummary],
    *,
    readable_references: Iterable[str] = (),
    scope_note: str = "",
    bundle_limit: int = DEFAULT_BUNDLE_LIMIT,
    total_files: Optional[int] = None,
) -> ChangeSet:
    """渲染清单并算出白名单范围。两种模式共用。"""
    ordered = tuple(commits)
    paths = _collect_paths(ordered)

    bundles = build_bundles(paths)
    bundle_lines = tuple(describe_bundles(bundles, limit=bundle_limit))

    body = render_change_summary(ordered, total_files=total_files)
    if scope_note:
        body = f"{scope_note}\n\n{body}"
    if bundle_lines:
        body += (
            "\n## 这些改动是一件事，请一起看\n\n"
            + "\n".join(bundle_lines)
            + "\n\n配表项目里，**表与它的生成物是同一次改动**：只看表或只看生成物，"
            "「表里加了 ID、生成的代码里有没有对应项」这类问题都看不出来。\n"
        )

    return ChangeSet(
        summary=body,
        scope=AnalysisScope(
            commits=tuple(commit.commit for commit in ordered if commit.commit),
            paths_by_commit={
                commit.commit: frozenset(change.path for change in commit.files if change.path)
                for commit in ordered
                if commit.commit and commit.files
            },
            readable_references=frozenset(str(name) for name in readable_references if name),
        ),
        paths=paths,
        commits=ordered,
        bundle_lines=bundle_lines,
    )


def _collect_paths(commits: Iterable[CommitSummary]) -> tuple[str, ...]:
    """全部路径，规范化 + 去重 + 保持先后顺序。

    顺序稳定是有意为之：`build_bundles` 的输出顺序跟着它走，而这份清单每轮都进提示词，
    顺序飘忽会让「这一轮和上一轮差在哪」变成不可读的。
    """
    seen: set[str] = set()
    result: list[str] = []
    for commit in commits:
        for change in commit.files:
            path = normalize_path(str(change.path or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)
    return tuple(result)


def _operation(value: object) -> str:
    """把操作类型收敛到 A/M/D。认不出来的一律按 M（修改）——**不猜成删除或新增**：
    猜错方向会让模型按错误的前提推理。"""
    text = str(value or "").strip().upper()
    return text if text in ("A", "M", "D") else "M"


def _scope_note(payload: Mapping[str, object]) -> str:
    scope = str(payload.get("scope") or "")
    label = _SCOPE_LABELS.get(scope)
    if not label:
        return ""
    return f"本次分析范围：{label}。"
