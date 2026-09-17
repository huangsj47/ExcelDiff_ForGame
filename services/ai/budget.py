"""提示词的字符预算与上下文分级压缩。

## 为什么所有裁剪都必须「带回省略了多少」

静默截断会让模型以为自己看到了全貌，于是基于残缺信息给出**看起来很确定**的结论。
这是最坏的一种失败：报告读起来没问题，但它漏掉的正是被截掉的那部分。所以这里的
每一条裁剪路径都会：

* 在文本上留下可读的痕迹（`... [truncated by local tool]`）；
* 在 `meta` 里记下原始长度 / 被丢掉的条数；
* 在 `notes` 里产出一句要写进提示词的话，明确告诉模型「你没看到全部，需要就再要」。

## 为什么按字符而不是 token

要做准确估算就得引入 tokenizer 依赖，而本仓库的依赖很克制；中文场景下字符数与
「上下文还剩多少」的相关性已经够用。代价是估算偏保守，所以对外的预算值会留出余量
（由调用方决定留多少），这里只负责「给定预算，把内容压到预算内」。

## 纯函数

本模块不读文件、不发请求、不碰数据库，全部输入输出都是值。这样它可以被完整单测，
而压缩逻辑恰恰是「看起来对但边界一堆」的那类代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# 截断标记。模型认识这个写法，且它对人类读者也是明确的。
TRUNCATION_SUFFIX = "\n\n... [truncated by local tool]"

# 分级压缩的每级上限（字符）。逐级减半再减半，最后一级直接换成一句说明。
SHRINK_LEVEL_LIMITS = (4000, 1200)
# 到这一级时整个内容被替换成说明文字，只保留「这里原本有什么」。
MAX_SHRINK_LEVEL = len(SHRINK_LEVEL_LIMITS) + 1

DEFAULT_MAX_ITEMS = 20
# 必须与 `models.ai_analysis.project_config.DEFAULT_PROMPT_CHAR_BUDGET` 相等
# （`test_model_defaults_agree_with_the_budget_layer` 会拦住漂移）。取值依据见那边的注释：
# 它要装得下「系统提示词 + 变更清单（现在是全量列出的，上限 MAX_LIST_CHARS）+ 索取次数 × 单条上限」。
DEFAULT_TOTAL_CHARS = 360_000


@dataclass(frozen=True)
class ContextItem:
    """一条要带进后续轮次的上下文。

    `meta` 是**记账字段**，例如「这个提交共有 30 个文件，这里只列了 10 个」。它会随
    内容一起给模型看，所以模型能知道自己看到的不是全部。
    """

    kind: str
    label: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class BudgetResult:
    """压缩结果。"""

    items: tuple[ContextItem, ...]
    # 因为条数上限被丢掉的条数。
    omitted_by_count: int = 0
    # 因为总字符上限被丢掉的条数。
    omitted_by_chars: int = 0
    # 达到过的最高压缩级别（0 表示没压过）。
    shrink_level: int = 0
    # 要写进提示词的说明（明确告知被省略了什么）。
    notes: tuple[str, ...] = ()

    @property
    def total_chars(self) -> int:
        return sum(item.char_count for item in self.items)

    @property
    def omitted_total(self) -> int:
        return self.omitted_by_count + self.omitted_by_chars


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """按字符上限截断，返回 (结果, 是否真的截断了)。

    上限必须为正；否则调用方传 0 或负数时会得到一段「加了三字标记但内容为空」的
    怪文本，那种输入应当由调用方挡住。
    """
    if limit <= 0:
        raise ValueError("limit 必须为正数")
    content = str(text or "")
    if len(content) <= limit:
        return content, False
    # 标记本身也算在预算里，否则结果会超出调用方给的上限。
    room = max(0, limit - len(TRUNCATION_SUFFIX))
    return content[:room] + TRUNCATION_SUFFIX, True


def elision_marker(omitted: int) -> str:
    """中间省略的标记，带上**被省略了多少字**。"""
    return f"\n\n... [中间省略 {omitted} 字，内容未完整展示] ...\n\n"


def truncate_text_middle(text: str, limit: int) -> tuple[str, bool]:
    """保留**开头和结尾**、省略中间。

    为什么不是只砍尾巴：本平台要分析的是 Excel 配表，而结构化 diff 是按「表 → 行 → 单元格」
    顺序线性排列的。只砍尾巴意味着**后面的表整张都看不到**——模型会以为自己看到的就是
    全部变更，而那些没露面的表恰恰可能是最危险的（比如「道具表改了、商城表跟着要改」）。

    保留首尾之后，模型至少能看到全部被改动的表名（它们分布在序列两端与中间），缺的只是
    中间某几张表的逐行细节。相比之下这是个小得多的损失，而且省略标记会把「缺了多少」
    说清楚。
    """
    if limit <= 0:
        raise ValueError("limit 必须为正数")
    content = str(text or "")
    if len(content) <= limit:
        return content, False

    # 标记的长度依赖被省略的字数，所以先按「标记大约 40 字」预留，再回填准确值。
    # 一次回填足够：预估值偏大时结果只会比上限短，不会超。
    reserved = len(elision_marker(len(content)))
    room = max(0, limit - reserved)
    head = room // 2
    tail = room - head
    omitted = len(content) - head - tail
    marker = elision_marker(omitted)
    result = content[:head] + marker + (content[-tail:] if tail > 0 else "")
    while len(result) > limit and head > 0:
        # 预估值偏小时（标记随位数变长）逐步收紧，保证结果不超上限。
        head -= 1
        tail = max(0, room - head)
        omitted = len(content) - head - tail
        result = content[:head] + elision_marker(omitted) + (content[-tail:] if tail else "")
    return result, True


def _summary_item(item: ContextItem, level: int) -> ContextItem:
    """把整条内容换成一句说明，只保留「这里原本有什么」。"""
    summary = (
        f"[{item.kind}] {item.label} 的内容因预算控制被省略"
        f"（原本 {item.char_count} 字）。如果结论仍然需要它，请重新索取。"
    )
    meta = dict(item.meta)
    meta["omitted_for_budget"] = True
    meta["original_chars"] = item.char_count
    meta["shrink_level"] = level
    return replace(item, text=summary, meta=meta)


def shrink_item(item: ContextItem, level: int) -> ContextItem:
    """把一条上下文压到第 `level` 级。

    1 级与 2 级是截断（4000 / 1200 字符），3 级直接换成说明文字。逐级递进而不是
    一步到位，是因为「截断到 4000」通常已经够用，直接换成说明会白丢信息。
    """
    if level <= 0:
        return item
    if level >= MAX_SHRINK_LEVEL:
        return _summary_item(item, level)

    limit = SHRINK_LEVEL_LIMITS[level - 1]
    text, truncated = truncate_text(item.text, limit)
    if not truncated:
        return item
    meta = dict(item.meta)
    meta["truncated_from"] = item.char_count
    meta["shrink_level"] = level
    return replace(item, text=text, meta=meta)


def _omission_note(kind: str, dropped: int, reason: str) -> str:
    return (
        f"有 {dropped} 条 {kind} 上下文因{reason}未提供给你。"
        "你看到的内容**不是全部**——如果结论依赖被省略的部分，请重新索取。"
    )


def limit_item_count(
    items: Iterable[ContextItem], max_items: int
) -> tuple[tuple[ContextItem, ...], int, tuple[str, ...]]:
    """按条数上限保留**最近的**若干条。

    保留最近的而不是最早的：多轮分析里后要到的上下文通常是围绕当前疑点的，更相关。
    被丢掉的条数会写进提示词，而不是静默消失。
    """
    ordered = tuple(items)
    if max_items <= 0:
        # 注意别写成 `return (), n, (note if ordered else (),)` —— 那个尾逗号会让返回值
        # 变成「装着一个空元组的元组」`((),)`，于是「没有内容可丢」时会凭空多出一条
        # 空说明。测试里 `test_count_limit_of_zero_on_empty_input_is_quiet` 就是抓它的。
        notes = (_omission_note("全部", len(ordered), "条数上限为 0"),) if ordered else ()
        return (), len(ordered), notes
    if len(ordered) <= max_items:
        return ordered, 0, ()

    dropped = ordered[: len(ordered) - max_items]
    kinds = "、".join(sorted({item.kind for item in dropped}))
    kept = ordered[len(ordered) - max_items :]
    return kept, len(dropped), (_omission_note(kinds, len(dropped), "条数上限"),)


def trim_total_chars(
    items: Iterable[ContextItem], total_chars: int
) -> tuple[tuple[ContextItem, ...], int, tuple[str, ...]]:
    """按总字符上限从**最旧的**开始丢。

    丢最旧的与 `limit_item_count` 的方向一致：越新的上下文越贴当前疑点。
    """
    ordered = list(items)
    if total_chars <= 0:
        notes = (_omission_note("全部", len(ordered), "字符预算为 0"),) if ordered else ()
        return (), len(ordered), notes

    total = sum(item.char_count for item in ordered)
    if total <= total_chars:
        return tuple(ordered), 0, ()

    dropped = 0
    while ordered and total > total_chars:
        removed = ordered.pop(0)
        total -= removed.char_count
        dropped += 1
    if not dropped:
        return tuple(ordered), 0, ()
    kinds = "、".join(sorted({item.kind for item in ordered})) or "全部"
    return tuple(ordered), dropped, (
        _omission_note(kinds, dropped, "总字符预算不足"),
    )


def enforce_budget(
    items: Iterable[ContextItem],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    total_chars: int = DEFAULT_TOTAL_CHARS,
) -> BudgetResult:
    """把上下文压进预算。

    顺序是**先压单条、再限条数、最后限总量**，而不是反过来：

    * 先压单条，能在不丢任何一条的前提下把体积降下来（丢条目损失的信息更多）；
    * 压完仍超条数/总量，才丢条目。

    逐级加压：从 1 级开始试，每次重算总量，仍在预算外就升一级。这样「轻微超预算」
    只需截断一个长文件，而不会一上来就把内容换成说明文字。

    **某一级什么都没改就停止升级**。否则会出现这种情况：200 条各 100 字的短上下文
    超了条数上限，1 级与 2 级都无事可做（本来就不长），却一路升到 3 级把所有内容换成
    说明文字——信息被毁掉了，而真正该做的是丢条目。升级的前提是上一级确实压出了东西。

    **体积判断只看「最终会留下的那些条目」。** 超出条数上限的那些反正会被
    `limit_item_count` 丢掉，把它们算进体积，循环就永远满足不了条件（条数是丢条目才能
    解决的），于是一路升到 3 级、把所有内容换成说明文字 —— 又一次毁掉信息。
    真实触发路径：模型一次索要 12 个文件（预算本来就允许 12 次），而条数上限是 8；
    12 条各 14,000 字的情况下，修之前的结果是 8 条共 682 字的说明文字。
    """
    current = tuple(items)
    notes: list[str] = []
    level = 0

    def _survivors_over_size(entries: tuple[ContextItem, ...]) -> bool:
        kept, _, _ = limit_item_count(entries, max_items)
        return sum(item.char_count for item in kept) > total_chars

    if _survivors_over_size(current):
        for candidate_level in range(1, MAX_SHRINK_LEVEL + 1):
            shrunk = tuple(shrink_item(item, candidate_level) for item in current)
            if shrunk == current and candidate_level < MAX_SHRINK_LEVEL:
                break
            current = shrunk
            level = candidate_level
            if not _survivors_over_size(current):
                break

        if level:
            notes.append(
                f"为控制预算，部分上下文已被压缩到第 {level} 级"
                "（截断或省略）。被省略的内容可以重新索取。"
            )

    current, omitted_by_count, count_notes = limit_item_count(current, max_items)
    notes.extend(count_notes)

    current, omitted_by_chars, char_notes = trim_total_chars(current, total_chars)
    notes.extend(char_notes)

    return BudgetResult(
        items=current,
        omitted_by_count=omitted_by_count,
        omitted_by_chars=omitted_by_chars,
        shrink_level=level,
        notes=tuple(notes),
    )


def build_continuation_summary(items: Iterable[ContextItem], *, keep: int = 3) -> str:
    """生成「续跑摘要」：只保留最近几条的标题，体积极小。

    用于「上下文怎么压都还是超预算」时的兜底——此时不能再把内容带过去了，但至少要
    让模型知道上一轮进行到哪、拿到过什么，否则它会从头再来一遍，白烧预算。
    """
    ordered = tuple(items)
    recent = ordered[-keep:] if keep > 0 else ()
    lines = [
        f"已获取过 {len(ordered)} 条上下文。最近 {len(recent)} 条是：",
    ]
    for item in recent:
        lines.append(f"- [{item.kind}] {item.label}")
    lines.append(
        "这些内容本轮未随消息附上（体积超限）。如仍需要，请重新索取具体的文件或提交。"
    )
    return "\n".join(lines)


def estimate_chars(messages: Iterable[dict[str, str]]) -> int:
    """粗估一组消息的字符总量，用于判断是否接近预算。"""
    return sum(len(str(message.get("content") or "")) for message in messages)


def clamp_to_model_window(budget_chars: int, context_tokens: int) -> tuple[int, str]:
    """预算**明显**超出模型上下文窗口时压回窗口大小；否则原样返回。

    返回 `(生效预算, 说明)`；说明为空表示没有压缩。

    ## 为什么只压「明显」的那一档

    本模块按**字符**估算（见模块文档），而模型窗口是按 **token** 计的。中英混排下一个
    字符大致对应 0.3~1 个 token，这个比例取决于提示词里中文占多少，**平台无从知道**。

    于是只有一种情形是不需要换算比例就能断定的：预算字符数 **大于** 窗口 token 数。
    此时就算按最乐观的 1 字 1 token 算也已经装不下，超窗是必然的，压回窗口只会砍掉
    装不下的部分。反过来的情形（预算字符数 ≤ 窗口 token 数）是否装得下要看语言构成，
    压它就是在没有依据的情况下砍掉分析质量，而且用户看不出发生了什么 —— 所以不压。

    这也是为什么这里**没有**留「安全系数」：留系数等于假装知道那个换算比例。
    """
    if context_tokens <= 0 or budget_chars <= context_tokens:
        return budget_chars, ""
    note = (
        f"提示词字符预算 {budget_chars:,} 字超过模型上下文窗口 {context_tokens:,} token，"
        f"本次按 {context_tokens:,} 字执行（字与 token 最乐观按 1:1 算也已超窗）。"
    )
    return context_tokens, note
