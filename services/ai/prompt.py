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
from typing import Iterable, Mapping, Optional, Sequence

from services.ai.budget import ContextItem
from services.ai.protocol import build_budget_exhausted_hint
from services.ai.skill_loader import LoadedSkills

# 参与提示词版本哈希的源文件。
PROMPT_SOURCE_FILES = ("prompt.py",)

# 第一轮与后续轮次的开场指令。
#
# 第一轮为什么要求「先分诊、再点名」：文件清单是**名字**，信息量很低
# （`SeasonRankCfgMod12.lua` 能说明什么？），而索取次数是按次计的稀缺资源。让模型先把
# 假设摆出来、再据此挑文件，比让它凭名字盲选一批要值钱得多 —— `reason` 里那段分诊
# 同时是给人看的：出问题时能看出它当时在想什么，而不是只看到「它要了这 8 个文件」。
#
# 4~8 这个量级也是算过的：单条上限 11,000 字符，8 条约 88,000，装得进 360,000 的预算；
# 而一次只要 1~3 个会把 20 次额度摊到很多轮里，每轮都要重发一遍上下文（多轮的成本）。
_FIRST_ROUND_HINT = (
    "现在只给了你这次变更的**元数据与文件清单**，没有任何 diff 内容。不要凭文件名猜测"
    "改动内容。第一轮按这个顺序走：\n"
    "\n"
    "1. **先分诊**：把这次变更按「最可能出事」排序，说出依据（改了哪些业务行为、涉及"
    "哪条业务链、清单里哪些文件互相关联）。**同时标出这次改动跨了哪几个模块，以及"
    "有没有本该成对出现、却只看到一边的改动**（配表与它的生成物、客户端与服务端、"
    "协议定义与打包解包）——只改一半的改动是最值钱的信号，优先索取它们另一端的 diff。"
    "这段判断写进 `reason`。\n"
    "2. **配表类改动按「数据本身是否说得通」看**（`config_data` 维度）：改了描述/备注的，"
    "同行里被它描述的列有没有跟着改；新增或改动的行有没有漏填必填列；类型、枚举、日期"
    "格式是否与同表其它行一致；有没有复制粘贴出来只改了一半的重复行。**只改了一列文案、"
    "它描述的字段没动**是这类改动里最常见的问题，而它看起来最像「只是改了句文案」，"
    "最容易被放过去。\n"
    "3. **数值改动按「放在这个系统里合不合理」看**（`value_sanity` 维度）：先认数量和"
    "量级 —— 一个数值改了几个数量级（道具价值 1000 → 1000000、奖励 10 → 100000、"
    "价格 100 → 1）是本轮最值得索取上下文的信号之一；再认**经济闭环**：同一件东西的"
    "买入价与卖出/回收价关系反了（售价 100 卖给商店能卖 10000）、合成或分解的产出大于"
    "投入 —— 这类配置会被玩家刷爆。**这类改动要连带索取同一张表的内容**："
    "`file_content` 会在正文之前附一段**整表统计**（数值列的最小/中位/P90/最大、文本列的"
    "取值分布，按整表算、不受截断影响），那才是「这个值合不合理」的比较基准；"
    "本批次里若有这张表的更早提交，也可以读它做历史对照（比**分布**，不要只比一个值）。"
    "**疑似就要报**，但报的时候必须写明基准来自哪里；确实拿不到基准的也要报，"
    "并在证据里显式写「缺基准：<原因>」、置信度只给 `high`（不许 `very_high`）。\n"
    "4. **再点名**：按这个顺序一次索取 4~8 个最关键的 `file_diff`，在 `reason` 里说清"
    "为什么是它们。拿到之后先对照第 1 步的假设，再决定要不要继续。\n"
    "5. **说清边界**：`reason` 里写明这一轮决定先看什么、暂时没看什么。\n"
    "\n"
    "额度是按**次数**计的，分散在多轮里不会变多 —— 一次能要完最关键的几个，就一次要完。"
)

_LATER_ROUND_HINT = (
    "以下是你在上一轮索要的上下文。判断证据是否已经足够：够了就直接输出 `final`，"
    "不够就继续点名索取具体文件。"
)

# 后续轮次对变更清单的**指针**。它在第一轮已经完整给出过一次（就在上文），这里不再重发。
#
# 为什么不再每轮重发：它是整份提示词里最大的一段（全量列出时上限约 87,000 字）。每轮重发
# 一遍的代价是双份的 ——
#   * **上下文窗口**：8 轮下来单这一段就 700,000 字，比整个预算还大，而窗口是硬约束，
#     撑爆的结果是整次分析被上游拒绝、连结论一起作废（见 `budget.compact_history`）；
#   * **钱**：它是每一轮**新追加**的内容，落在缓存断点之后，所以每一轮都按**未命中**价
#     计费，而不是命中价。
#
# 但也不能只字不提：多轮之后模型对最早那条的注意力最弱，而变更清单正是判断的基准。
# 所以这段指针要同时说清三件事 —— 清单在哪、它仍然有效、以及**被压缩掉时怎么办**。
_CHANGE_SUMMARY_POINTER = (
    "## 本次变更\n\n"
    "变更清单**没有变**，已经在第 1 轮的对话里完整给出过（就在上文），这里不再重发 ——"
    "它是整份提示词里最大的一段，每轮重发一遍会占掉本该留给上下文的位置。\n\n"
    "**它仍然是你的判断基准**：结论里「本版本改了什么」必须与那份清单一致，不要凭"
    "印象改写文件数、提交数或改动范围。如果上文中那份清单因长度控制被压缩或省略掉了，"
    "请用 `commit_detail` / `file_diff` 按需重新索取，不要把「没看到」当成「没改」。"
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


def change_block(change_summary: str, *, round_index: int) -> str:
    """本轮消息里那段「本次变更」的**实际文本**。

    第一轮是清单全文；后续轮次是一段指针（见 `_CHANGE_SUMMARY_POINTER`）。

    **为什么要有这个函数**：组装消息与算提示词预算必须用同一份文本。预算按清单全文算、
    消息里只放指针，会让平台白白少用几十万字符的额度（那是上下文条目的额度）；反过来
    的组合则是算着够、发出去超。所以两边都调它，而不是各自判断一次轮次。
    """
    if round_index <= 1:
        return str(change_summary or "")
    return _CHANGE_SUMMARY_POINTER


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
    total_files: Optional[int] = None,
) -> str:
    """把变更批次渲染成给模型看的清单。

    「共 N 个文件」这类计数必须准确 —— 模型会用它们判断自己看到的是不是全部。

    `total_files` 是**截断前的真实文件数**。清单是按优先级取样出来的，不给这个值时
    首行会把「清单里的文件数」说成「本版本的文件数」：线上那个周版本真实变更 767 个文件、
    清单只列了 200 个，模型于是写出「本版本共 67 个提交、200 个文件」，读者与它自己
    都以为这就是全量 —— 后面「结论强度受限」的免责声明显得没来由，因为没人知道
    还有 567 个文件根本没进清单。

    ## 「没列出来」不等于「读不到」

    这两件事以前被绑在一起：被取样的 200 个也是白名单的全部，所以没列出来的文件连
    `file_diff` 都会被拒。现在白名单是**本批次全部改动过的文件**，只有「名字没列全」
    这一件事 —— 所以截断说明里必须写清「你可以按路径索取」，并给出发现路径的办法
    （对该提交用 `commit_detail`）。否则模型会老老实实地把「看不到名字」当成「读不到
    内容」，那份「信息缺口」的免责声明就白写了。
    """
    listed_files = sum(len(commit.files) for commit in commits)
    actual_total = listed_files if total_files is None else max(total_files, listed_files)

    if actual_total > listed_files:
        missing = actual_total - listed_files
        lines = [
            f"本次变更的文件共 {actual_total} 个，下面列出其中的 {listed_files} 个"
            f"（涉及 {len(commits)} 个提交）。",
            f"**还有 {missing} 个文件的名字没有列出来 —— 但它们同样是本批次改动过的文件，"
            "而且你可以读到它们的 diff。** 想知道某个提交到底改了哪些文件，就对那个提交"
            "用 `commit_detail`（它会给出该提交的完整文件清单），再用 `file_diff` 点名索取"
            "具体文件。**不要因为名字没列出来就当成「读不到」。**",
            "另外，不要写成「本版本共改了 N 个文件」这类把清单当成全量的说法，也不要把"
            "结论强度说得比手上的证据更高 —— 需要时明确写出「本次只看到 M/N 个文件」。",
            "",
        ]
    else:
        lines = [
            f"本次变更共 {len(commits)} 个提交、{listed_files} 个文件。",
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
    history_recap: str = "",
) -> str:
    """组装某一轮的 user 消息。

    轮次、剩余预算、以及**被省略了什么**，都要写进来。模型看不到这些就会以为自己
    已经掌握全部信息 —— 或者反过来，无休止地索要下去。

    `baseline_digest` 是上一轮为止的结论（`baseline.build_baseline_digest` 的输出），
    **只在第一轮整份给出**，后续轮次只带一句提醒：它每轮重发要花掉约 6,000 字符，而那
    正是上下文条目的额度。第一轮也是模型决定整体策略的一轮，那时看到它最有效。

    `change_summary` 同理只发一次（第一轮），后续轮次给一段指针 —— 理由见
    `_CHANGE_SUMMARY_POINTER`：它是最大的一段，而且每轮重发都按未命中价计费。
    走不走指针由 `change_block` 决定（预算那边用的是同一个函数，不能两处各判一次）。

    `history_recap` 是「中间几轮被压掉了」时补的那段记录（`budget.compact_history` 的
    产出）。它必须紧挨着本轮上下文之前：那一句「需要就重新索取」要贴着模型的下一步动作，
    写在最前面会被后面几段冲淡。
    """
    is_first_round = round_index <= 1
    blocks: list[str] = []

    blocks.append(f"# 本次变更（第 {round_index}/{max_rounds} 轮）")
    blocks.append(change_block(change_summary, round_index=round_index))

    if is_first_round:
        if baseline_digest.strip():
            # 放在变更清单之后：先看「改了什么」，再看「其中哪些已经有人看过了」。
            blocks.append(baseline_digest.strip())
        blocks.append(_FIRST_ROUND_HINT)
    else:
        blocks.append(_LATER_ROUND_HINT)
        if baseline_digest.strip():
            blocks.append(_BASELINE_REMINDER)
        if history_recap.strip():
            blocks.append(history_recap.strip())
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
        # 这段文案**只有一份**（在 `protocol` 里，与纠正提示放在一起）。以前这里另写了
        # 一段同义的，两句话不一样：这里只说「请立刻输出 final」，没有明说「禁止继续
        # 请求上下文」—— 而那句正是要模型别把最后一轮浪费在又一次索取上。两份文案的
        # 后果是改一处漏一处，所以合到一处。
        blocks.append(build_budget_exhausted_hint())

    if correction_hint.strip():
        blocks.append("## 上一轮的问题\n\n" + correction_hint.strip())

    return "\n\n".join(blocks).rstrip() + "\n"
