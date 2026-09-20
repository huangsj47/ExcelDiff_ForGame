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

## 三处裁剪，越靠后越狠

| 裁剪 | 对象 | 触发 |
|---|---|---|
| `shrink_item` / `enforce_budget` | **本轮**取回来的上下文 | 单条超长、条数超限、总量超预算 |
| `compact_history` | **中间那几轮的对话** | 整份提示词（含历史）超出窗口水位 |
| `looks_like_context_overflow` + 引擎里的收尾请求 | 上游已经拒过一次 | 估算失准、发出去被拒 |

前两处是「先手」：在发出去之前把体积压下来。第三处是兜底 —— 估算终究是估算，
上游真的拒了也不能让一次已经跑了几轮的分析连结论一起作废。

## 纯函数

本模块不读文件、不发请求、不碰数据库，全部输入输出都是值。这样它可以被完整单测，
而压缩逻辑恰恰是「看起来对但边界一堆」的那类代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

# 截断标记。模型认识这个写法，且它对人类读者也是明确的。
TRUNCATION_SUFFIX = "\n\n... [truncated by local tool]"

# 分级压缩的每级上限（字符）。逐级减半再减半，最后一级直接换成一句说明。
SHRINK_LEVEL_LIMITS = (4000, 1200)
# 到这一级时整个内容被替换成说明文字，只保留「这里原本有什么」。
MAX_SHRINK_LEVEL = len(SHRINK_LEVEL_LIMITS) + 1

# 条数上限。**必须不小于 `context_tools.DEFAULT_MAX_TOOL_REQUESTS`**，否则会出现
# 「付了 N 次索取、只带走 max_items 条」的浪费（`test_the_context_item_cap_never_wastes_a_paid_request`）。
DEFAULT_MAX_ITEMS = 40
# 必须与 `models.ai_analysis.project_config.DEFAULT_PROMPT_CHAR_BUDGET` 相等
# （`test_model_defaults_agree_with_the_budget_layer` 会拦住漂移）。取值依据见那边的注释：
# 它要装得下「系统提示词 + 变更清单（现在是全量列出的，上限 MAX_LIST_CHARS）+ 索取次数 × 单条上限」，
# 同时**留在默认窗口的水位（600,000 字）以下**。
DEFAULT_TOTAL_CHARS = 560_000


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


def truncate_bytes(text: str, max_bytes: int, *, encoding: str = "utf-8") -> tuple[str, bool]:
    """按**字节**上限截断，返回 (结果, 是否真的截断了)。

    ## 为什么还需要一把按字节量的刀

    `truncate_text` 量的是**字符数**，而跨节点回传那条路上的天花板是**字节数**：
    `AgentTask.result_summary` 是 `db.Column(db.Text)`，MySQL 下 `TEXT` 是 65,535 **字节**。
    字符数是个会随内容语言漂移的代理指标 —— 同样 11,000 字，一段中文配表差异是 33,000
    字节（**一半额度没用上就停了**），而一段 ASCII 代码补丁只有 11,000 字节（另一半同样
    空着）。而「代码 diff 看不到」正是最需要这个额度的地方，所以该按字节收敛。

    ## 不切在字符中间

    按字节数硬切会把一个多字节字符劈成两半，`decode` 出来是个替换符（U+FFFD）—— 模型读到
    一串乱码，而且**它不会知道那是切坏的**（看起来就是原文里有怪字符）。所以从头切下来的
    字节串按 `errors="ignore"` 解码：丢掉被劈开的那个残字节，剩下的仍是干净文本。

    标记本身也算在预算里（与 `truncate_text` 同一条口径），否则结果会超出调用方给的上限。
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须为正数")
    content = str(text or "")
    if len(content.encode(encoding)) <= max_bytes:
        return content, False
    room = max(0, max_bytes - len(TRUNCATION_SUFFIX.encode(encoding)))
    head = content.encode(encoding)[:room].decode(encoding, errors="ignore")
    return head + TRUNCATION_SUFFIX, True


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

    调用方是 `compact_history`：压掉中间几轮历史时，把那些轮次取过的内容目录留下来。
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


# ==========================================================================
# 上下文窗口：水位、压历史、以及「上游说装不下」的兜底
# ==========================================================================

# 窗口未知时的默认值。**这是一个口径，不是猜某个具体模型**：问不到端点声明的窗口时
# 按 1M token 处理，而不是不设防（见 `effective_prompt_budget`）。
#
# 取 1M 还有一层刻意：当前默认预算是 560,000 字，小于 1M 的 60%（600,000 字），
# 所以「默认窗口」不会悄悄改变已有项目的行为 —— 只有把预算配到 600,000 字以上的
# 项目才会吃到这层水位。
DEFAULT_CONTEXT_TOKENS = 1_000_000

# 水位：窗口用到这个比例就开始压历史。留 40% 是给「模型的回复 + 估算误差」的。
COMPACT_AT_RATIO = 0.6

# 1 token 按多少字符算。取 **1.0**，也就是假设「一个字符至少一个 token」——
# 中英混排下这是最保守的方向（英文约 4 字 1 token、中文约 1 字 1 token），于是
# 「字符数 ≤ 窗口 × 60%」能推出「token 数 ≤ 窗口 × 60%」。
#
# 这个假设**偏保守**：真实占用只会更小，代价是少塞了一些本来装得下的内容。这个方向的
# 错可以接受；反过来的错（以为装得下、发出去被上游拒绝）会把一次已经跑了几轮的分析
# 连结论一起作废 —— 那正是要避免的。
CHARS_PER_TOKEN = 1.0

# 压历史时**至少**保留最近几轮原文。1 轮是下限（模型当下的推理是从最近一轮继续的）；
# 溢出兜底路径会传 0，意思是「连最近一轮也可以丢」。
MIN_KEEP_TURNS = 1


def resolve_context_window(declared: object) -> tuple[int, bool]:
    """把端点声明的窗口收敛成一个可用的值。返回 `(窗口 token, 是否用了默认值)`。

    第二个返回值必须一路带到界面上去：**「按默认 1M 压的」与「端点声明了 1M」是两件事**，
    不说清的话读的人会以为平台问到了窗口。
    """
    try:
        value = int(declared or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return DEFAULT_CONTEXT_TOKENS, True
    return value, False


def context_watermark_chars(window_tokens: int) -> int:
    """窗口对应的字符水位（窗口 × 60%，按 `CHARS_PER_TOKEN` 折算）。"""
    window, _ = resolve_context_window(window_tokens)
    return max(1, int(window * COMPACT_AT_RATIO * CHARS_PER_TOKEN))


def effective_prompt_budget(configured_chars: object, window_tokens: int) -> tuple[int, str]:
    """配置的字符预算与窗口水位取小，返回 `(生效预算, 说明)`。说明为空表示没压。

    ## 与以前那条规则的关系（为什么改了）

    以前是 `clamp_to_model_window`：只在「预算字符数 **大于** 窗口 token 数」时才压，
    理由是字符与 token 的换算比例平台无从知道，没有依据就不该砍分析质量。顾虑是对的，
    但它留下了一个更糟的口子 —— 窗口**问不到**时它什么都不做（`window` 为空直接返回），
    于是把预算配到 2,000,000 字的项目会拿着一份 2M 字符的提示词去撞一个 128k 的模型，
    **必然被拒**，而拒绝的后果是整次分析连结论一起作废。

    现在按「窗口 × 60%」压，并且把字符当 token 算（见 `CHARS_PER_TOKEN`）：这个方向是
    保守的，压出来的提示词在 token 意义上一定装得下。少塞的那部分内容由「压历史」承担
    （`compact_history`），而它压掉的是**重复**，不是信息。
    """
    configured = max(0, int(configured_chars or 0))
    window, defaulted = resolve_context_window(window_tokens)
    watermark = context_watermark_chars(window)
    if configured <= watermark:
        return configured, ""
    source = "端点未声明窗口，按默认值" if defaulted else "端点声明的窗口"
    return watermark, (
        f"上下文窗口 {window:,} token（{source}），按 {int(COMPACT_AT_RATIO * 100)}% 水位"
        f"压到 {watermark:,} 字执行；配置的 {configured:,} 字预算只作为上限。"
        "提示词超出水位时，平台会压掉最早几轮的历史（保留最近几轮原文），"
        "被压掉的内容可以重新索取。"
    )


def looks_like_context_overflow(text: object) -> bool:
    """上游的报错像不像「提示词装不下」。

    ## 为什么敢用文本匹配

    各家网关的报错文案完全不同（OpenAI 系是 `context_length_exceeded`，vLLM 会说
    `maximum context length`，自建网关什么写法都有），没有统一的错误码可认。而两种
    判错的代价是**不对称**的：

    * **认错（把别的错当成超窗）**：多花一次压缩后的调用 —— 压缩后还是失败，末尾仍然
      报失败，用户看到的失败原因不变。代价是一次请求。
    * **漏认（超窗了却没认出来）**：这次分析直接作废，前面几轮的钱白花 —— 也就是
      这个函数存在的理由。

    所以这里宁可放宽一点。匹配到的文本同时会写进失败原因里，用户能自己核对。
    """
    haystack = str(text or "").lower()
    if not haystack:
        return False
    for needle in (
        "context_length_exceeded",
        "context length",
        "maximum context",
        "max context",
        "context window",
        "reduce the length",
        "too many tokens",
        "exceeds the maximum",
        "prompt is too long",
        "input is too long",
        # 中文网关的常见写法（有的会把上游文案翻一遍再回吐）
        "上下文长度",
        "上下文超",
        "超出最大上下文",
        "too long",
    ):
        if needle in haystack:
            return True
    return False


@dataclass(frozen=True)
class TurnMemo:
    """一轮的「事后来看值得留一句」的记录，给压历史用。

    **只记标签，不记正文**：正文本来就在对话里，压历史时才需要一句「这一轮索取了什么」。
    条数按 `labels` 截断是为了让摘要本身足够小 —— 摘要要是也很大，压历史就白压了。
    """

    index: int
    status: str = ""
    items: tuple[ContextItem, ...] = ()
    max_labels: int = 6

    def describe(self) -> str:
        label = _ROUND_STATUS_LABELS.get(self.status, self.status or "无记录")
        if not self.items:
            return label
        labels = "、".join(str(item.label) for item in self.items[: self.max_labels])
        if len(self.items) > self.max_labels:
            labels += f" 等 {len(self.items)} 条"
        return f"{label}：{labels}"


_ROUND_STATUS_LABELS = {
    "requests": "索取了上下文",
    "final": "给出了结论",
    "unparsable": "输出无法解析",
}


@dataclass(frozen=True)
class CompactionResult:
    """压历史的结果。`dropped_turns == 0` 表示什么都没做。"""

    messages: tuple[dict, ...] = ()
    # 要并进「本轮」用户消息的那段摘要。为空表示没有历史要交代。
    recap: str = ""
    dropped_turns: int = 0
    dropped_chars: int = 0
    notes: tuple[str, ...] = ()

    @property
    def compacted(self) -> bool:
        return self.dropped_turns > 0


def _group_turns(messages: Iterable[Mapping[str, Any]]) -> list[list[dict]]:
    """把消息按「轮」分组：遇到 user 就开新的一轮。

    不假设严格交替：模型回了个空的 assistant、或者某个上游一次给了两条 assistant，
    都只会把几条并进同一轮，不会丢消息。
    """
    turns: list[list[dict]] = []
    for message in messages:
        entry = dict(message)
        if not turns or entry.get("role") == "user":
            turns.append([entry])
        else:
            turns[-1].append(entry)
    return turns


def compact_history(
    messages: Sequence[Mapping[str, Any]],
    *,
    target_chars: int,
    keep_recent_turns: int = MIN_KEEP_TURNS,
    memos: Sequence[TurnMemo] = (),
    protect_head: int = 2,
) -> CompactionResult:
    """把**中间那些轮次**压成一段摘要：只留 system、第一轮、以及最近几轮原文。

    ## 为什么只动中间

    * `messages[0]`（system）与 `messages[1]`（第一轮的 user 消息）是**跨运行复用的缓存
      前缀** —— 两个 `cache_control` 断点都挂在那里，动一下就把命中让给了别人；
    * 第一轮里是**变更清单与历史结论基线**，也就是这次分析要回答的问题本身。

    能压的只有「要过什么、拿到过什么」的那几轮，而它们恰好最占体积、信息密度最低：
    每一轮都把整份上下文重发一遍。压掉它们换来的是「分析能跑到底」，而不是「少花点钱」。

    ## `protect_head`：子代理模式下必须多钉一条

    子代理（见 `services/ai/subagent.py`）的第一轮消息形状是
    `[system, 共享消息, **任务书**]` —— 任务书排在**第 3 条**。它写的是「你负责哪几个
    维度」，被压掉之后子代理会跑到一半忘记自己的分工，然后开始自由发挥；而这件事**不会
    报错**，只表现为一份浅一点的报告。所以那条调用要传 `protect_head=3`。

    默认值 2 就是这条改动之前的行为（钉住 system 与第一轮），不传时逐字节不变。
    「前 k 条」是按**位置**数，不是按轮次：调用方必须在「上一轮的 assistant 已经进了
    messages、本轮的 user 还没进」这个时刻调用它，位置才对得上。

    ## 压的是重复，不是信息

    被丢掉的轮次不会消失：`recap` 里有一行一行的记录（第几轮、索取了什么），并且会带上
    一句「需要就重新索取」。这是本模块一贯的口径（见模块文档第 1 条）——**绝不静默裁剪**。

    ## `memos` 与轮次是按位置一一对应的

    第 i 个 memo 描述 tail 里的第 i 轮。
    """
    ordered = [dict(message) for message in messages]
    head_size = max(1, int(protect_head))
    if len(ordered) <= head_size:
        return CompactionResult(messages=tuple(ordered))

    head, tail = ordered[:head_size], ordered[head_size:]
    turns = _group_turns(tail)
    keep = max(0, int(keep_recent_turns))

    dropped = 0
    while dropped < len(turns) - keep:
        kept = head + [item for turn in turns[dropped:] for item in turn]
        if estimate_chars(kept) <= target_chars:
            break
        dropped += 1
    if dropped == 0:
        return CompactionResult(messages=tuple(ordered))

    kept_turns = turns[dropped:]
    dropped_messages = [item for turn in turns[:dropped] for item in turn]
    dropped_chars = estimate_chars(dropped_messages)
    recap = _recap_for(dropped, turns[:dropped], memos)
    return CompactionResult(
        messages=tuple(head + [item for turn in kept_turns for item in turn]),
        recap=recap,
        dropped_turns=dropped,
        dropped_chars=dropped_chars,
        notes=(
            f"为控制上下文体积，最早的 {dropped} 轮历史已压成一段摘要"
            f"（省下约 {dropped_chars:,} 字，最近 {len(kept_turns)} 轮原文保留）。"
            "被压掉的轮次里取过的内容若仍需要，可以重新索取。",
        ),
    )


def _recap_for(
    dropped: int, turns: Sequence[Sequence[Mapping[str, Any]]], memos: Sequence[TurnMemo]
) -> str:
    """把被压掉的轮次写成一段给模型看的记录。"""
    lines = [
        "## 已压缩的历史（为控制上下文体积）",
        "",
        f"下面是最早 {dropped} 轮里发生过的事。**这几轮的正文已从对话里移除**，只剩这份"
        "记录 —— 如果某份内容对你的结论是必需的，请重新索取（索取额度仍然有效）。",
    ]
    for offset, _turn in enumerate(turns, start=1):
        memo = memos[offset - 1] if offset - 1 < len(memos) else None
        if memo is None:
            lines.append(f"- 第 {offset} 轮")
        else:
            lines.append(f"- 第 {offset} 轮：{memo.describe()}")
    items = [item for memo in memos[:dropped] for item in memo.items]
    if items:
        lines.append("")
        lines.append(build_continuation_summary(items))
    return "\n".join(lines)
