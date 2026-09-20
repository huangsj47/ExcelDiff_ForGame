"""在本批次改动的文件里找一处标识符的**其它出现位置**（只给位置，不给正文）。

## 它解决什么

模块耦合这一类风险里最值钱的一句话是「**这个字段/协议/函数还有谁在用**」。
今天模型只能靠**猜文件名**：清单里有 `scripts/optional/activity.lua`，它猜另一个调用方
可能叫 `activity_mgr.lua`，然后一个个点名索取 diff —— 而额度是按次数计的（默认 20 次），
767 个文件里这么猜等于放弃。

`find_references` 把这一步变成一次调用：给一个标识符，返回 `文件:行: 那一行的内容`
（**不返回正文**，命中之后模型再点名索取真正要看的那一份）。

## 只搜**本批次改动过的文件**

这是白名单纪律的延续：这个工具能触达的内容，不超过模型本来就能用 `file_content` 单独
索取的那些文件。因此搜索**不会**扩大读取面 —— 它只是把「在一堆文件里翻同一个名字」这件事
从 N 次索取变成 1 次调用。搜到的是这些文件在**该提交上的完整内容**（不只是本次改动的行）：
「谁在读这个字段」这个问题，答案通常就在没改的那几行里。

## 三个必须如实回报的数

`scanned` / `skipped`（二进制或读不到的）/ `truncated_*`（文件数或命中数被上限截断）。
「没搜到」与「没搜完」在模型那里必须分得开 —— 否则它会写出一句「没有其它引用」，
而那只是我们扫了一半。所以抬头里这三样都写出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from utils.text_decoding import decode_text_bytes, looks_binary

# 一次搜索最多扫多少个文件、最多给多少条命中、每条命中留多长。
#
# 这三个数是**上限而不是目标**：文件数上限是为了让一次搜索不至于把整次分析卡住
# （767 个文件逐个 `git show` 是几十秒的量级），命中数上限是因为「这个字段有 300 处引用」
# 这句话本身没有价值 —— 模型要的是「还有哪几个文件在用」。
MAX_SCAN_FILES = 240
MAX_HITS = 80
MAX_LINE_CHARS = 200
# 太短的词（"id"、"a"）会把整个批次都搜出来，纯属浪费额度。让它写清楚一点再搜。
MIN_QUERY_CHARS = 3
# 单条命中给模型看的形态：`路径:行号: 内容`
_HIT_SEPARATOR = ": "


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}{_HIT_SEPARATOR}{self.text}"


@dataclass(frozen=True)
class SearchResult:
    """一次搜索的账。**每个上限都要有对应的一行**，模型才知道自己拿到的是不是全部。"""

    query: str
    hits: tuple[Hit, ...] = ()
    files_total: int = 0
    scanned: int = 0
    missing: int = 0
    binary: int = 0
    truncated_files: bool = False
    truncated_hits: bool = False
    prefix: str = ""

    @property
    def hit_files(self) -> tuple[str, ...]:
        seen: list[str] = []
        for hit in self.hits:
            if hit.path not in seen:
                seen.append(hit.path)
        return tuple(seen)


def normalize_query(raw: str) -> str:
    """搜索词：去掉首尾空白。太短的会被调用方拒掉（见 `MIN_QUERY_CHARS`）。"""
    return str(raw or "").strip()


def is_binary(content) -> bool:
    """读回来的东西能不能当文本搜。

    判据是**真正的二进制**（魔数 / NUL，`utils.text_decoding.looks_binary`），不是
    「能不能按 utf-8 解码」—— 后者会把 GBK 的 lua、gb2312 的配置判成二进制并跳过，
    于是模型搜不到任何词，而抬头把原因写成「N 个不是文本（配表等二进制，没搜）」：
    **「编码不兼容」被记成了「它是二进制」**。命中的行文本也由同一个模块的
    `decode_text_bytes` 解码，所以「判成文本的内容」与「解码用的编码清单」永远一致。
    """
    return looks_binary(content)


def search_text(text: str, query: str, *, path: str, limit: int) -> list[Hit]:
    """在一份文本里逐行找。**大小写不敏感**（同一个标识符在不同语言里的写法不一样）。"""
    needle = query.lower()
    hits: list[Hit] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line.lower():
            hits.append(
                Hit(path=path, line=index, text=line.strip()[:MAX_LINE_CHARS])
            )
            if len(hits) >= limit:
                break
    return hits


def search_files(
    entries: Iterable[tuple[str, str]],
    query: str,
    *,
    reader: Callable[[str, str], object],
    max_files: int = MAX_SCAN_FILES,
    max_hits: int = MAX_HITS,
    prefix: str = "",
) -> SearchResult:
    """按 `(路径, 提交)` 逐个读回内容并搜索。

    `reader(path, commit)` 由两端各自注入（Agent 端与平台本地用的是同一个
    `get_file_content_from_git`，只是跑在不同的进程里）。它的返回值按三态处理：
    文本 → 搜；bytes → 不是文本就跳过并计数；`None` → 读不到，计数。

    **顺序扫、扫到上限就停**，并把「停在哪」如实记进结果（`truncated_files`）——
    这个函数不并发：它与整次分析共用一条线程，也因为顺序让「扫了多少个」是确定的。
    """
    pairs = list(entries)
    total = len(pairs)
    hits: list[Hit] = []
    scanned = missing = binary = 0
    truncated_files = False

    for path, commit in pairs:
        if scanned >= max_files:
            truncated_files = True
            break
        try:
            content = reader(path, commit)
        except Exception:  # noqa: BLE001 —— 读一个文件失败只该少一条命中，不该作废整次搜索
            missing += 1
            continue
        if content is None:
            missing += 1
            continue
        if is_binary(content):
            binary += 1
            continue
        if isinstance(content, bytes):
            # 解码走 `utils.text_decoding`（**两端唯一一份实现**）：GBK 的 lua 与搜索词
            # 都要能被解出来，否则「这个词在这份文件里出现过吗」这个问题根本没法回答
            # （按 utf-8 + replace 解出来的乱码里，中文标识符一个都匹配不上）。
            content = decode_text_bytes(content)
        scanned += 1
        remaining = max_hits - len(hits)
        if remaining <= 0:
            return SearchResult(
                query=query,
                hits=tuple(hits),
                files_total=total,
                scanned=scanned,
                missing=missing,
                binary=binary,
                truncated_files=truncated_files,
                truncated_hits=True,
                prefix=prefix,
            )
        hits.extend(
            search_text(str(content), query, path=path, limit=remaining)
        )

    return SearchResult(
        query=query,
        hits=tuple(hits),
        files_total=total,
        scanned=scanned,
        missing=missing,
        binary=binary,
        truncated_files=truncated_files,
        truncated_hits=len(hits) >= max_hits,
        prefix=prefix,
    )


def render_result(result: SearchResult, *, scope_note: str = "") -> str:
    """把一次搜索渲染成给模型看的文本。

    **两种「没有」必须分得开**：

    * 「扫完了，一处都没有」→ 可以据此下结论（但仍然只覆盖了本批次改动过的文件，
      这句话要写在抬头里）；
    * 「扫了一部分」→ 只是「没扫到」，不是「不存在」。所以只要 `scanned < files_total`
      或有跳过，抬头就把这三个数摆出来。
    """
    where = f"（范围：{result.prefix}）" if result.prefix else ""
    lines = [
        f"[find_references] 关键词 `{result.query}`{where}："
        f"命中 {len(result.hits)} 处，分布在 {len(result.hit_files)} 个文件里。"
    ]
    if result.hits:
        lines.extend(hit.render() for hit in result.hits)
        lines.append(
            "以上只是**位置**。要看某一处的正文或它所在文件的改动，"
            "用 `file_content` / `file_diff` 点名索取（路径与行号直接抄上面那几行）。"
        )
    else:
        lines.append("本批次改动的文件里没有出现这个关键词。")
    lines.append(_coverage_note(result, scope_note))
    return "\n".join(lines)


def _coverage_note(result: SearchResult, scope_note: str) -> str:
    parts = [
        f"本次搜索覆盖了本批次改动的 {result.scanned}/{result.files_total} 个文件"
    ]
    if result.binary:
        parts.append(f"{result.binary} 个不是文本（配表等二进制，没搜）")
    if result.missing:
        parts.append(f"{result.missing} 个读不到（工作副本里取不到，没搜）")
    if result.truncated_files:
        parts.append("**文件数到了上限就停了，剩下的没搜**")
    if result.truncated_hits:
        parts.append("**命中数到了上限，还有没列出来的**")
    text = "；".join(parts) + "。"
    if result.scanned < result.files_total or result.missing:
        text += "所以「没搜到」只代表**搜过的这些**里没有，不代表整批里没有。"
    text += "搜索范围仅限**本批次改动过的文件**，没改动过的文件不在里面。"
    if scope_note:
        text += scope_note
    return text


@dataclass
class SearchBudget:
    """一次分析里 `find_references` 一共能扫多少个文件。

    **按扫过的文件数计，不是按调用次数**：一次搜索可以是 3 个文件，也可以是 240 个，
    后者贵两个数量级，只卡次数等于没卡。额度挂在 provider 上 —— 子代理模式下 N 个成员
    共用一个 provider，所以这一份额度是**一家子共用**的（与正文缓存同一层）。
    用完时如实告诉模型「这一轮的检索额度用完了」，而不是悄悄扫 0 个文件返回「没命中」。
    """

    limit: int = 900
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self, files: int) -> None:
        self.used += max(0, int(files))


def entries_for(paths: Sequence[str], commit_of: Callable[[str], str | None]):
    """把路径列表配成 `(路径, 提交)` 序列，跳过查不到提交的那些。"""
    pairs: list[tuple[str, str]] = []
    for path in paths:
        commit = commit_of(path)
        if commit:
            pairs.append((path, commit))
    return pairs
