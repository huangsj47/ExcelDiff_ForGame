"""把超长的取数结果切成**可点名的段**，而不是从中间砍一刀。

## 它解决什么

单条上下文有字符上限（`context_tools.DEFAULT_TOOL_LIMITS`：`file_diff` 11,000、
`commit_detail` 6,000、`read_reference` 11,000）。超了就得截，而原先的截法让一部分内容
**永远拿不到**：

* `file_diff` 走「保留首尾」的截断 —— 中间那段永远读不到，而「就改了一行关键配置」
  很可能正落在中间；模型却以为自己看全了（首尾都在，中间那行省略号容易被读成「没什么要紧的」）。
* `read_reference` 走普通截断 —— 项目知识文档 26,766 / 15,468 字符，**永远只给得到前半份**，
  而且模型不知道自己看的是半份。
* `commit_detail` 同理：大提交的文件清单**尾部被砍掉**，而尾部正是按路径排序的后半批文件 ——
  模型于是「不知道路」（它连这些文件存在都不知道，自然不会去点名索取）。

## 办法：说清「一共有几段、这是第几段」，并让模型点名要别的段

渲染结果由**抬头 + 正文**组成：

```
[file_diff] 代码差异：scripts/x.lua 共 5 段（每段一个改动块）。这里是第 1-2 段。
要看别的段，在请求里写 "lines": "<第几段>"（例如 "3-4"）。其余 3 段是：
  3. @@ -90,2 +93,3 @@ function c()
  4. @@ -200,4 +204,5 @@ function d()
  5. @@ -320,6 +330,8 @@ function e()

<正文>
```

抬头里四件事缺一不可：**总共多少段**（不然模型不知道还有没有）、**这是第几段**
（不然它把片段当全文）、**其余几段分别是什么**（不然它只能一段段试，而额度按次数计）、
**怎么要别的段**（不然它只能猜或放弃）。

同一套语法（`lines`，就是 `file_content` 那个行窗口的写法）用在三个工具上，只是**单位不同**：
`file_diff` 是改动块、`read_reference` 是小节、`commit_detail` 是一个改动文件。单位写进
抬头，模型在用的地方看到它，不必去记一张表。

## 为什么单位不统一成「字符偏移」

字符偏移对模型没有意义：它无法从「第 12,000–23,000 字」判断那里是什么内容，只能一段段试 ——
而额度是按**次数**计的（`DEFAULT_MAX_TOOL_REQUESTS`）。按**结构**切（改动块 / 小节 / 文件）时，
抬头里的段号与内容对得上，模型可以一次就要对。
"""

from __future__ import annotations

import re
from typing import Callable, Sequence

from services.ai.budget import truncate_text, truncate_text_middle

# 抬头之外的余量：先按它给正文留位置，抬头拼好之后再按实际长度收一次（见 render_window）。
_HEADER_RESERVE = 260

# 「没给的那几段分别是什么」那一段清单的上限（字符）与每行的长度上限。
_OUTLINE_MAX_CHARS = 1_200
_LABEL_MAX_CHARS = 72

# 每个工具切出来的「段」叫什么。写进抬头，模型照着这个词点名。
#
# 四个字段分别是：计数的量词（「共 3 个改动文件」）、切法说明、「第几X」里的那个 X、
# 以及「看别的 X」里的那个 X。中文里这几处**不能共用一个字符串**：
# 「要看别的个改动文件」是病句，「这里是第 2-3 个改动文件」也别扭。
_UNITS = {
    "file_diff": ("段", "每段一个改动块", "段", "段"),
    "read_reference": ("节", "按文档自己的小节切", "节", "节"),
    "commit_detail": ("个改动文件", "每个文件一行", "个文件", "文件"),
}

# 没有结构可切时的兜底块大小（例如一份从头到尾没有小标题的文档）。
_FALLBACK_CHUNK_CHARS = 4_000

# 切段的标记：`@@`（代码补丁的块头）、`###` / `##`（配表的工作表与顶层标题）、
# `--- 第 k/N 段`（分段合并 diff，见 `platform_provider._render_segmented`）。
_SEGMENT_MARKERS = (
    re.compile(r"^@@ "),
    re.compile(r"^#{2,3} "),
    re.compile(r"^--- 第 \d+/\d+ 段"),
)
_HEADING_RE = re.compile(r"^#{1,6} ")
_FILE_ENTRY_RE = re.compile(r"^\s+- \[[^\]]*\] ")
_WINDOW_RE = re.compile(r"(\d{1,7})(?:\s*-\s*(\d{1,7}))?")


def windowed_kinds() -> tuple[str, ...]:
    """哪些工具走这套「分段 + 点名」的渲染。"""
    return tuple(_UNITS)


def parse_window(window: str, total: int) -> tuple[int, int] | None:
    """把 `"4-6"` / `"5"` 解析成 1-based 的闭区间，并夹到 `1..total`。

    解析不出来（空、越界、倒序）返回 `None` —— 调用方按「从头开始装」处理。这与
    `file_content` 的行窗口同一条口径：**窗口写坏的代价只能是拿到默认那一段**，
    而不是丢掉整条请求。
    """
    text = str(window or "").strip()
    if not text or total <= 0:
        return None
    match = _WINDOW_RE.fullmatch(text)
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if start < 1 or end < start or start > total:
        return None
    return start, min(end, total)


def split_segments(text: str, *, kind: str) -> tuple[list[str], str]:
    """把渲染好的内容切成列表，返回 `(段列表, 切法说明)`。

    `commit_detail` 特殊一点：它前面几行是提交信息（提交号 / 说明 / 作者 / 时间 / 文件数），
    那几行**每次都要给**（它们回答「这是哪条提交」），要分页的只有文件清单。
    """
    if kind == "commit_detail":
        return _split_file_list(text)
    if kind == "read_reference":
        return _split_document(text)
    return _split_blocks(text)


def _split_on(text: str, starts_new: Callable[[str], bool]) -> list[str]:
    """逐行扫，遇到「新一段的开头」就断开。第一段永远从文件开头开始。"""
    blocks: list[list[str]] = [[]]
    for line in text.splitlines(keepends=True):
        if blocks[-1] and starts_new(line):
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return ["".join(block) for block in blocks if "".join(block).strip()]


def _split_blocks(text: str) -> tuple[list[str], str]:
    """按补丁块头 / 工作表标题切；一段标记都没有时整份算一段。"""
    segments = _split_on(text, lambda line: any(m.match(line) for m in _SEGMENT_MARKERS))
    if len(segments) > 1 and not any(m.match(segments[0]) for m in _SEGMENT_MARKERS):
        # 开头那一段是「代码差异：<路径>」「配表差异：<路径>」这类抬头，它不属于任何一个
        # 改动块。并进第一段：否则模型会多数出一段（4 个 `@@` 却说「共 5 段」），
        # 而它点名要第 1 段时拿到的是一行路径。
        segments = [segments[0] + segments[1], *segments[2:]]
    if len(segments) <= 1:
        # 切不开就老老实实说「一份」，不要编出段号来。
        return ([text], "整份内容没有可切分的块头")
    return (segments, "每段一个改动块")


def _split_document(text: str) -> tuple[list[str], str]:
    """按 Markdown 小标题切；一个小标题都没有时按字符块切（否则永远只读得到前半份）。"""
    segments = _split_on(text, lambda line: bool(_HEADING_RE.match(line)))
    if len(segments) > 1:
        return (segments, "按文档自己的小节切")
    chunks = [
        text[index:index + _FALLBACK_CHUNK_CHARS]
        for index in range(0, len(text), _FALLBACK_CHUNK_CHARS)
    ]
    if len(chunks) > 1:
        return (chunks, f"这份文档没有小标题，按每 {_FALLBACK_CHUNK_CHARS} 字一块切")
    return ([text], "整份内容")


def _split_file_list(text: str) -> tuple[list[str], str]:
    """`commit_detail`：要分页的是文件清单，提交信息那几行不算段。"""
    files = _split_on(text, lambda line: bool(_FILE_ENTRY_RE.match(line)))
    files = [piece for piece in files if _FILE_ENTRY_RE.match(piece)]
    if not files:
        return ([text], "整份内容")
    return (files, "每个文件一行")


def commit_preamble(text: str) -> str:
    """`commit_detail` 抬头那几行（提交号 / 说明 / 作者 / 时间 / 文件总数）。

    **每次都给**：分页分的是文件清单，而「这是哪条提交」是读清单的前提。
    """
    head: list[str] = []
    for line in text.splitlines(keepends=True):
        if _FILE_ENTRY_RE.match(line):
            break
        head.append(line)
    return "".join(head)


def _requested_missing(window: str, total: int, unit: str) -> str:
    """点名了一段**不存在**的段号时，抬头要说一句。

    `parse_window` 在「越界」与「认不出」两种情况下都返回 `None`，调用方一律按「没点名」
    处理（从第 1 段开始装）。**这个回落本身是有意的** —— 窗口写坏的代价只能是拿到默认
    那一段，不能是丢掉整条请求。但「越界」那一支必须**说出来**：实测模型在被砍过的文本上
    点名 `"lines": "5"`（而那份只切得出 4 段），拿回的是与上一轮**逐字相同**的第 1-3 段，
    而抬头照样写着「这里是第 1-3 段」—— 它没有任何线索知道自己的点名被无声地换掉了，
    只会以为「第 5 段就是这些内容」。

    配表那条路是有这句话的（`你要的第 a-b 张不存在`），这里对齐；「认不出」仍然不吭声
    （那是写法坏了，不是段号不存在）。
    """
    text = str(window or "").strip()
    if not text or total <= 0:
        return ""
    match = _WINDOW_RE.fullmatch(text)
    if match is None:
        return ""
    start = int(match.group(1))
    if start <= total:
        # 在范围内：`"2-99"` 这种尾部越界由 `parse_window` 夹到 total，不算「不存在」。
        return ""
    return f"**你要的第 {start} {unit}不存在**：这份内容一共只有 {total} {unit}。"


def render_window(
    *,
    kind: str,
    label: str,
    text: str,
    window: str,
    limit: int,
) -> tuple[str, dict]:
    """按段渲染一份带抬头的正文。返回 `(文本, 记账用的 meta)`。

    `meta` 里三样东西给记账与续跑摘要用：`segments`（一共几段）、`shown`（这次给了第几段）、
    `truncated`（有没有装不下的内容）—— 最后这一样决定消耗面板上的「截断」列。
    """
    unit, how, ordinal, noun = _UNITS.get(kind, ("段", "按块切分", "段", "段"))
    segments, split_note = split_segments(text, kind=kind)
    total = len(segments)
    preamble = commit_preamble(text) if kind == "commit_detail" else ""

    if total <= 1:
        # 切不开就只有一条路：与这个模块存在之前**完全一样**的截断（`file_diff` 保留首尾，
        # 其余保留头部），meta 里记一句「这份是一整块」。不在这里加抬头：抬头里的段号
        # 对一份切不开的内容没有意义，而截断标记本身已经说明「你没看全」。
        body, truncated = (
            truncate_text_middle(text, limit)
            if kind == "file_diff"
            else truncate_text(text, limit)
        )
        meta = {"segments": 1, "split": split_note}
        if truncated:
            meta["truncated"] = True
        return body, meta

    head_room = max(1_000, limit - len(preamble) - _HEADER_RESERVE)
    body, chosen, clipped = _fill(segments, window, total, head_room)
    span = parse_window(window, total)
    missing_note = _requested_missing(window, total, unit)
    head = _header(
        kind, label, total, unit, how, ordinal, noun, chosen, segments, extra=missing_note
    ) + preamble
    overflow = len(head) + 2 + len(body) - limit
    if overflow > 0:
        # 正文装不下，尾巴要收掉 —— 而**「这里是第 N 段」这句话从此不再成立**：
        # `shown` 是按 `chosen` 算的，回砍之后最后那一段只剩半截。实测一份 40 段的差异：
        # 抬头写「这里是第 1-16 段」，而正文里第 16 段被砍在 `@@ -150,1 +150,2 @` 中间
        # （连块头都没给完）—— 模型于是以为第 16 段拿全了，只去要第 17 段，那半截永远拿不到。
        # 这正是本模块 docstring 要防的那类失真。
        #
        # 那句话本身也占额度，所以先拼一次把它的长度量出来，再用**实际剩余**的额度收正文
        # （一次算准，不用迭代：`room` 的定义就是「抬头拼好之后还剩多少」）。
        last = chosen[-1] + 1
        head_cut = _header(
            kind, label, total, unit, how, ordinal, noun, chosen, segments,
            extra=missing_note,
            # 说话要留余地：回砍的落点在这这一段**之中的某个位置**，也可能是把它整段砍掉
            # （实测 40 段的例子里，第 16 段的 `@@` 块头都没了）。说「只给到第 N-1 段」
            # 会少报，说「第 N 段完整」会多报 —— 而多报正是这条要修的毛病。所以只说
            # 「它可能只有前半截」，并给出**一定会拿全**的动作。
            partial=(f"**正文被额度截断了**：第 {last} {ordinal}可能只有前半截，"
                     f'要确保拿到它请直接点名 `"lines": "{last}"`。'),
        ) + preamble
        room = max(200, limit - len(head_cut) - 2)
        body, also_clipped = truncate_text(body, room)
        clipped = clipped or also_clipped
        head = head_cut
    shown = _shown_text(chosen)
    # 什么时候算「被截断」：**平台自己收掉了内容**。模型点名要第 3-4 段、我们如实给了
    # 3-4 段，那不叫截断（否则消耗面板上会出现一堆「截断」，而真相是模型按段读的）；
    # 要了 3-9 却只装得下 3-5，那才是。
    asked_more = bool(span and len(chosen) < span[1] - span[0] + 1)
    meta = {
        "segments": total,
        "shown": shown,
        "truncated": bool(clipped or asked_more or (span is None and len(chosen) < total)),
    }
    if preamble:
        meta["commit_preamble"] = True
    return head + "\n" + body, meta


def _fill(
    segments: Sequence[str],
    window: str,
    total: int,
    budget: int,
) -> tuple[str, list[int], bool]:
    """按窗口与额度挑段，返回 `(正文, 挑中的段下标, 最后一段有没有被额度截到)`。"""
    budget = max(1_000, budget)
    span = parse_window(window, total)
    start = (span[0] - 1) if span else 0
    chosen: list[int] = []
    used = 0
    for index in range(start, total):
        piece = segments[index]
        if chosen and used + len(piece) > budget:
            break
        chosen.append(index)
        used += len(piece)
        if span and index + 1 >= span[1]:
            break
    if not chosen:
        chosen = [start]
    # 每段自带结尾换行（它们是从原文按行切出来的），再拼一个换行会多出一整行空行。
    body = "\n".join(segments[index].rstrip("\n") for index in chosen)
    return body, chosen, used > budget


def _shown_text(chosen: Sequence[int]) -> str:
    if not chosen:
        return ""
    first, last = chosen[0] + 1, chosen[-1] + 1
    return f"{first}-{last}" if last > first else str(first)


def _header(
    kind: str,
    label: str,
    total: int,
    unit: str,
    how: str,
    ordinal: str,
    noun: str,
    chosen: Sequence[int],
    segments: Sequence[str],
    *,
    extra: str = "",
    partial: str = "",
) -> str:
    """抬头：共几段 / 这是第几段 / **其余几段分别是什么** / 怎么要。

    第三样（`_outline`）不是装饰：只说「还有 3 段」的话，模型不知道那 3 段里有没有它要的
    东西，只能一段段试 —— 而额度是按次数计的。把每段的第一行（改动块的 `@@` 头、文档的
    小标题、文件名）列出来，它就能一次要对。

    `extra` 与 `partial` 是两句**只在特定处境下才出现**的话，必须排在段号那句之后：
    `extra` 说「你点名的那一段不存在」（见 `_requested_missing`），`partial` 说
    「最后那一段只有前半截」（见 `render_window` 的 overflow 分支）。两句都是**对模型
    这一次点名的直接回应**，排在「其余几段是」那份清单之前，免得被清单挤到看不见。
    """
    shown = _shown_text(chosen) or "1"
    lines = [f"[{kind}] {label} 共 {total} {unit}（{how}）。这里是第 {shown} {ordinal}。"]
    # **先答模型问的那件事**：它点名了一段，那就先说那段在不在（`extra`），再说正文被
    # 额度截到哪儿（`partial`）。两句都排在「其余几段是」那份清单之前 —— 清单能长到
    # 1,200 字，排在它后面的话这两句会被挤到看不见的地方。
    if extra:
        lines.append(extra)
    if partial:
        lines.append(partial)
    missing = [index for index in range(total) if index not in set(chosen)]
    if missing:
        example = _example_window(chosen[-1] + 1, total) if chosen else "1"
        lines.append(
            f'要看别的{noun}，在请求里写 "lines": "<第几{noun}>"（例如 "{example}"）。'
            f"其余 {len(missing)} {unit}是："
        )
        lines.append(_outline(segments, missing))
    return "\n".join(lines) + "\n"


def _outline(segments: Sequence[str], missing: Sequence[int]) -> str:
    """没给出的那几段「分别是什么」—— 每段一行，取每段的第一行（并截短）。

    按**字符**也设一个上限：一份几百段的清单若全列出来，抬头自己就把正文挤没了，
    而正文才是模型真正要读的东西。
    """
    lines: list[str] = []
    used = 0
    for position, index in enumerate(missing):
        line = f"  {index + 1}. {_segment_label(segments[index])}"
        if used + len(line) > _OUTLINE_MAX_CHARS:
            lines.append(f"  …（另有 {len(missing) - position} 段未列出）")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _segment_label(segment: str) -> str:
    """一段的「标题」= 它的第一行（`@@ -1180,7 +1180,9 @@` / `### 工作表「道具」` / `  - [M] a.lua`）。"""
    first = ""
    for line in segment.splitlines():
        if line.strip():
            first = line.strip()
            break
    return first[:_LABEL_MAX_CHARS]


def _example_window(last: int, total: int) -> str:
    """抬头里那个例子：给一串**真实存在**的段号，而不是让人猜。"""
    start = min(last + 1, total)
    end = min(start + 1, total)
    return str(start) if start == end else f"{start}-{end}"
