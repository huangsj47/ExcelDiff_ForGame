"""提示词组装。

## 组装顺序是有意的：不可覆盖的在前，可覆盖的在后

```
你的角色与方法（本平台强制）      ← 平台内置 SKILL.md 正文，无条件注入
项目知识（可能过时）              ← 项目知识包 + 子 skill 索引
项目补充指令（优先级最低）        ← 项目配置里那一栏
```

模型的注意力对**靠后**的内容更敏感，所以把优先级写反的代价是实打实的：项目管理员
随手填的一句补充指令如果排在内置规则之后且没有明确的优先级说明，就足以盖掉「必须给出
证据」「禁止空泛表述」这类硬要求。因此顺序 + 一句显式的优先级声明，两样都要有。

## 提示词版本用内容哈希

与 `rules_version` 同理：手工维护版本号一定会漏改，而漏改的后果是用户永远拿不到新
提示词产出的结果（幂等键没变，复用了旧报告）。

## 变更数据里**不含 diff**，这是刻意的

第一轮只给「提交信息 + 文件清单 + 元数据」，diff 由模型按需索取。平台原来那份提示词
写着「请基于以下变更 diff」，而 payload 里根本没有 diff —— 要求模型基于拿不到的东西
作答。所以这里除了不给 diff，还要**明确说清「diff 不在这里，需要就点名要」**，
否则模型会对着一份文件清单开始猜内容。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from services.ai.budget import ContextItem
from services.ai.skill_loader import LoadedSkills

# 参与提示词版本哈希的源文件。
PROMPT_SOURCE_FILES = ("prompt.py",)

# 第一轮与后续轮次的开场指令。
_FIRST_ROUND_HINT = (
    "现在只给了你这次变更的**元数据与文件清单**，没有任何 diff 内容。\n"
    "不要凭文件名猜测改动内容。先判断哪些文件最可能出问题，再用 `file_diff` 点名索取"
    "（一次只要 1~3 个文件），拿到后再决定要不要继续。"
)

_LATER_ROUND_HINT = (
    "以下是你在上一轮索要的上下文。判断证据是否已经足够：够了就直接输出 `final`，"
    "不够就继续点名索取具体文件。"
)

# 后续轮次对基线的一句提醒。**不重复整份基线**：它已经在第一轮的消息里，重发一遍要花
# 6,000 字符左右，而这份预算正是上下文条目的额度（见 `budget.DEFAULT_TOTAL_CHARS` 的算式）。
# 但也不能完全不说 —— 多轮之后注意力会从第一轮飘走，而「不要重复报」是输出层面的硬要求。
_BASELINE_REMINDER = (
    "提醒：第一轮给你的那份「已经报过的问题」清单**仍然有效**。不要把它里面的问题当作"
    "新发现重复报；每条标出现在的状态（仍成立 / 已修复 / 已被推翻）并给出依据。"
    "标为「已忽略」的不要再提，除非它是被这次改动重新触发的。"
)

# 第一轮的强制性声明。放在最前面，且不依赖模型把长文读完。
_PRIMACY_NOTICE = """本协议由平台强制注入，**优先级高于你的通用习惯与默认风格**。

- 内置协议与下面的「项目知识」冲突时，**以内置协议为准**（项目知识是事实，协议是门槛）。
- 「项目补充指令」优先级最低：它只补充，不能放宽内置协议里的任何要求。
- 输出必须是符合协议的 JSON，不要输出任何 JSON 之外的解释文字。"""


def prompt_version() -> str:
    """提示词层的版本标识（源码内容哈希）。"""
    digest = hashlib.sha1()
    base = Path(__file__).resolve().parent
    for name in PROMPT_SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return f"prompt-{digest.hexdigest()[:12]}"


# --------------------------------------------------------------------------
# 变更数据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileChange:
    """一个被改动的文件。`operation` 是 A/M/D。"""

    path: str
    operation: str = "M"


@dataclass(frozen=True)
class CommitSummary:
    """一个提交的元数据。**不含 diff。**"""

    commit: str
    message: str = ""
    author: str = ""
    commit_time: str = ""
    files: tuple[FileChange, ...] = ()


def render_change_summary(
    commits: Sequence[CommitSummary],
    *,
    omitted_files_note: str = "",
) -> str:
    """把变更批次渲染成给模型看的清单。

    「共 N 个文件」这类计数必须准确 —— 模型会用它们判断自己看到的是不是全部。
    """
    total_files = sum(len(commit.files) for commit in commits)
    lines = [
        f"本次变更共 {len(commits)} 个提交、{total_files} 个文件。",
        "",
    ]
    if omitted_files_note:
        lines.extend([omitted_files_note, ""])

    for commit in commits:
        lines.append(f"## 提交 {commit.commit}")
        if commit.message:
            lines.append(f"- 提交信息：{commit.message.strip()}")
        if commit.author:
            lines.append(f"- 作者：{commit.author}")
        if commit.commit_time:
            lines.append(f"- 时间：{commit.commit_time}")
        lines.append(f"- 改动文件（{len(commit.files)} 个）：")
        for change in commit.files:
            lines.append(f"  - [{change.operation}] {change.path}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# 上下文渲染
# --------------------------------------------------------------------------


def _render_accounting(meta: Mapping[str, object]) -> str:
    """把记账字段渲染成一行给模型看的说明。

    **必须给模型看**：不告诉它「这份内容被截断过 / 只是说明文字」，它会把残缺的内容
    当成全部，然后给出一个看起来很确定的结论。
    """
    parts: list[str] = []
    if meta.get("original_chars"):
        parts.append(f"原文 {meta['original_chars']} 字")
    if meta.get("truncated"):
        parts.append(f"已按 {meta.get('limit')} 字上限截断")
    if meta.get("omitted_for_budget"):
        parts.append("内容因预算控制被省略")
    if meta.get("truncated_from"):
        parts.append(f"由 {meta['truncated_from']} 字压缩而来")
    if meta.get("tool_failed"):
        parts.append("取数失败")
    if meta.get("tool_empty"):
        parts.append("取数成功但无内容")
    return f"（记账：{'；'.join(parts)}）" if parts else ""


def render_context_items(items: Iterable[ContextItem]) -> str:
    """渲染已获取的上下文。"""
    rendered = list(items)
    if not rendered:
        return "（本轮没有附带任何上下文。）\n"
    blocks: list[str] = []
    for item in rendered:
        header = f"### [{item.kind}] {item.label}{_render_accounting(item.meta)}"
        blocks.append(f"{header}\n\n{item.text}")
    return "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def build_system_prompt(
    loaded: LoadedSkills,
    *,
    project_knowledge: str = "",
    project_instructions: str = "",
) -> str:
    """组装系统提示词。

    `project_knowledge` 是随项目知识包分发的正文（`KNOWLEDGE.md` 的补充说明，可由
    配置追加）；`project_instructions` 是项目配置里那一栏补充指令。
    """
    sections: list[str] = [_PRIMACY_NOTICE, "# 角色与方法（强制）", loaded.platform_skill.text]

    project_blocks: list[str] = []
    if loaded.project_manifest is not None:
        project_blocks.append(loaded.project_manifest.text)
    if project_knowledge.strip():
        project_blocks.append(project_knowledge.strip())

    index_lines: list[str] = []
    if loaded.project_references:
        index_lines.append("可读的项目知识文档（用 `read_reference` 按需索取，不要一次要完）：")
        index_lines.extend(f"- `{doc.name}`：{doc.description}" for doc in loaded.project_references)
    if loaded.project_skills:
        index_lines.append("项目自定义 skill（同样用 `read_reference` 索取正文）：")
        index_lines.extend(f"- `{doc.name}`：{doc.description}" for doc in loaded.project_skills)
    if index_lines:
        project_blocks.append("\n".join(index_lines))

    if project_blocks:
        sections.append("# 项目知识与约定")
        sections.extend(project_blocks)
        sections.append(
            "以上项目内容**只提供事实**。它与内置协议冲突时，以内置协议为准；"
            "它没有覆盖到的地方，按内置协议处理。"
        )
    else:
        sections.append(
            "# 项目知识与约定\n\n（本项目没有配置专属知识包。按内置协议处理；"
            "涉及具体配表规范、玩法语义时，请在报告里标注「信息缺口」。）"
        )

    if project_instructions.strip():
        sections.append("# 项目补充指令（优先级最低）")
        sections.append(project_instructions.strip())
        sections.append(
            "以上补充指令只用于补充说明，**不得放宽内置协议里的任何要求**"
            "（尤其是证据、置信度门槛与反误报条款）。"
        )

    return "\n\n".join(sections).rstrip() + "\n"


def build_user_message(
    *,
    change_summary: str,
    round_index: int,
    max_rounds: int,
    items: Iterable[ContextItem] = (),
    baseline_digest: str = "",
    budget_notes: Iterable[str] = (),
    requests_remaining: int = 0,
    correction_hint: str = "",
    budget_exhausted: bool = False,
) -> str:
    """组装某一轮的 user 消息。

    轮次、剩余预算、以及**被省略了什么**，都要写进来。模型看不到这些就会以为自己
    已经掌握全部信息 —— 或者反过来，无休止地索要下去。

    `baseline_digest` 是上一轮为止的结论（`baseline.build_baseline_digest` 的输出），
    **只在第一轮整份给出**，后续轮次只带一句提醒：它每轮重发要花掉约 6,000 字符，而那
    正是上下文条目的额度。第一轮也是模型决定整体策略的一轮，那时看到它最有效。
    """
    is_first_round = round_index <= 1
    blocks: list[str] = []

    blocks.append(f"# 本次变更（第 {round_index}/{max_rounds} 轮）")
    blocks.append(change_summary)

    if is_first_round:
        if baseline_digest.strip():
            # 放在变更清单之后：先看「改了什么」，再看「其中哪些已经有人看过了」。
            blocks.append(baseline_digest.strip())
        blocks.append(_FIRST_ROUND_HINT)
    else:
        blocks.append(_LATER_ROUND_HINT)
        if baseline_digest.strip():
            blocks.append(_BASELINE_REMINDER)
        blocks.append(render_context_items(items))

    notes = [note for note in budget_notes if str(note).strip()]
    if notes:
        blocks.append("## 上下文完整性提示（重要）\n\n" + "\n".join(f"- {note}" for note in notes))

    # 第一轮也要说额度：模型是在第一轮决定整体策略的（要一次要完还是逐步逼近），
    # 不知道额度就没法做这个决定。
    blocks.append(
        f"本次分析总共可索取 {requests_remaining} 次上下文（跨轮次累计，"
        "重复索要同一个文件也计入）。额度用完就只能基于已有证据出报告，"
        "所以请优先要最关键的。"
    )

    if budget_exhausted:
        blocks.append(
            "**索取额度已耗尽。** 请立刻基于已有证据输出 `final`，"
            "证据不足的维度写 `hit: false` 并在 note 里说明「信息不足」，"
            "同时在报告里标注信息缺口。"
        )

    if correction_hint.strip():
        blocks.append("## 上一轮的问题\n\n" + correction_hint.strip())

    return "\n\n".join(blocks).rstrip() + "\n"
