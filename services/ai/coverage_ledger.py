#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行级的**覆盖账本**：这一次分析「看了多少、没看多少、缺口是什么」。

## 为什么要有它（AI-P1-03）

在这之前，一次分析关于「覆盖」只有一个词：报告元信息里那行**「分析范围：全量」**。
而「全量」是**分析口径**（要不要走增量基线，见 `services/ai/change_set._decide_scope`），
它一个字都没说「输入里装了多少文件」、更没说「模型实际看过哪些」。于是读者（策划 / QA）
会把提示词里列出的 200 个名字当成「它看完了这个版本」，而实测是：

* run 12：本版本改动过 **995** 个文件，白名单全给了，**取到过证据的只有 57 个**
  （保守口径 57 / 995，宽松口径 71 / 995）；
* run 13：**60 / 996**（宽松口径 69 / 996）。

「取到过证据」这个数此前**全仓零展示**：消耗面板只有按工具类型的
`calls / executions / cache_hits / failed / refused_by_budget / truncated / source_chars /
produced_chars`（`services/ai/context_tools.py:183-192`），没有一处把它除以白名单。

## 三种覆盖，各回答一个问题

| 口径 | 分子 / 分母 | 回答的问题 |
|---|---|---|
| `inventory_coverage` | 本次输入（白名单） / 本版本改动总数 | 这个版本改过的东西，有多少**进了这次的输入** |
| `listed_coverage` | 提示词里列出的名字 / 白名单 | 有多少文件**摆到了模型眼前**（名字没列出 ≠ 读不到） |
| `evidence_coverage` | 真的取到过 diff 或正文的 / 白名单 | 有多少文件**这次真的被看过** |

**`evidence_coverage` 有两种去重口径，两个数不一样，必须都写出来。** 只写一个，同一份
数据就能算出两个「覆盖率」而没人知道差在哪：

* **保守（默认；报告与结论按这一份读）**：按「(该文件在本批次的 `latest_commit_id`,
  path)」去重 —— 只有「取到的证据正是白名单里那一条」才算覆盖。模型翻的是某个文件
  **更早**那条提交的那一版时，这条白名单条目**不算被看过**（要的是这个版本当时的改动）。
* **宽松**：按 `path` 去重 —— 只要取过这个文件的任何一版就算。

## 数字从哪来（不新建采集）

原料早就落库了，这一层只做**除法与翻译**：

* `run.request_payload`：`summary`（`total_files / delta_files / batch_files /
  window_files`，见 `services/ai/scope_sampling.py:185-201`）、`delta_files`（白名单全量，
  给的是**能读的范围**）、`list_files`（提示词里列出的那些）、`policy.truncated` /
  `truncation_reason`、`focus.label`；单提交模式直接用 `commit`；
* `ai_analysis_trace` 的逐轮明细（`services/ai/trace_evidence.decode_evidence`）：
  `executed` 是这一轮真正进了提示词的东西 —— 要算「哪些文件被看过」只能从这里取；
* `run.tool_stats_json`：运行级的按工具计数（`failed` / `refused_by_budget` / `truncated`）
  —— 缺口那几条用它，因为它已经是一家子（子代理全部成员）的合计，与消耗面板同源。

## 不许做的事

* **缺数就是缺数。** 老运行没有逐轮明细时，证据覆盖是**未知**，不是 0 —— 与消耗面板
  「未上报 ≠ 0」同一条口径（`services/ai/usage.py:217`）。写成 0 等于替用户断言
  「它一个文件都没看」。
* **不猜路径。** 逐轮明细里给的是人读的标签（`describe_request` 的
  `file_diff <12 位提交> <路径>`），只能从里面取「提交 + 路径」；给不出路径的条目
  （`read_reference` / `find_references` / `commit_detail`）**不算文件证据** ——
  它们本来就不是「读了某个文件的改动」，算进来会把覆盖率虚报上去。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional, Sequence

from models.ai_analysis import AiAnalysisTrace
from services.ai import window_commits
from services.ai.trace_evidence import (  # noqa: F401
    # `FILE_EVIDENCE_KINDS` 由 `trace_evidence` 定义（那里是纯层，运行中的进度也要用它），
    # 本模块转出这个名字供既有调用方按原路径取用。
    FILE_EVIDENCE_KINDS,
    decode_evidence,
)

MODE_WEEKLY = "weekly"
MODE_COMMIT = "commit"

# 「分析范围」那一行的限定语。**它必须跟着数字走**：没有覆盖数据时一个字都不加
# （拿不确定当结论，与不加限定语一样糟）。措辞要短 —— 它嵌在表格单元格里，紧跟其后的
# 就是覆盖那几行。
SCOPE_LIMITED_NOTE = "**分析口径**，不等于「整个版本都看过了」"

# 读不出来时显示的词。**不写 0**：0 是一个结论，而这里只是「没有记录」。
UNKNOWN = "未记录"


def _as_mapping(value: Any) -> dict:
    """JSON 字符串 / dict → dict。坏数据回空 dict，不抛。"""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _positive_int(value: Any) -> Optional[int]:
    """转成正整数；转不出来或 <= 0 时返回 `None`（缺值与 0 同义：这条数没得用）。

    与 `services/ai/change_set._positive_int` 同一套判据：0 个文件的白名单不是一个
    能拿来算比例的分母。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _count(value: Any) -> Optional[int]:
    """计数类的值：读得出来就是非负整数，读不出来（含 `None`）就是 `None`。

    **不把「没记录」写成 0** —— 与 `cache_read_tokens` 那几个字段同一条口径。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _sum_counts(values: Iterable[Any]) -> Optional[int]:
    """把若干个可能缺的数加起来；**一个都没有时是 `None`**（不是 0）。"""
    known = [_count(value) for value in values]
    known = [item for item in known if item is not None]
    return sum(known) if known else None


def _ratio(covered: Any, total: Any) -> Optional[float]:
    """分子 / 分母；分子读不出来（未知）或分母缺失 / <= 0 时返回 `None`（**不是 0**）。"""
    whole = _positive_int(total)
    if whole is None:
        return None
    part = _count(covered)
    if part is None:
        return None
    return part / whole


def _percent(ratio: Optional[float]) -> str:
    """`0.0602` → `6.0%`。拿不到比例时返回 `未知`（不是 `0.0%`）。"""
    if ratio is None:
        return "未知"
    return f"{ratio * 100:.1f}%"


def _num(value: Any) -> str:
    number = _count(value)
    return str(number) if number is not None else UNKNOWN


def _layer_text(covered: Any, total: Any) -> str:
    """`57 / 995`；分母读不出来时**只写分子**。

    「未记录 / 未记录」读不成话（老 payload 里两个数一起缺的时候就会这样），而它出现在
    覆盖面那几行里 —— 那是产品要拿去汇报的那一段。分子分母都在才写分数。
    """
    done, whole = _count(covered), _count(total)
    if done is None:
        return UNKNOWN
    return f"{done} / {whole}" if whole is not None else str(done)


def _extra_input_row(label: str, value: Any) -> str:
    """「额外输入」那一行里的一部分。**三种取值三种写法**：

    * 有数（含 0）：写数字 —— 「0 个」是一个**结论**（这轮确实没有补偿项）；
    * 读不出来（老 payload 没有这个键）：写 `未记录` —— 平台没这个数，不是 0。

    两者原先都不输出（`if x:` 才写一行），于是「0 个」与「没有这个概念」在报告里
    长得一模一样。
    """
    number = _count(value)
    if number is None:
        return f"{label}{UNKNOWN}"
    if number == 0:
        return f"{label} 0 个"
    return f"{label} {number} 个"


def parse_evidence_label(label: Any) -> tuple[str, str, str]:
    """逐轮明细里那一行人读的标签 → `(12 位提交, 路径, 行窗口/段)`。

    标签是 `describe_request` 写的（`services/ai/context_tools.py`）：

        file_diff f844faa6f59c config/60_skill/角色属性表.xlsx
        file_content f844faa6f59c code/…/ConfMod.lua lines=30-31

    路径里可能有空格（中文文件名就是），所以只切前两段；`lines=` 那一段是**同一个文件的
    另一段**，必须从路径里摘出来 —— 不摘的话，「按文件去重」会把它当成两个文件数上去
    （实测 run 12：带上它是 71，摘掉才是 57）。认不出来就回三个空串，**不猜路径**。
    """
    parts = str(label or "").strip().split(" ", 2)
    if len(parts) < 3:
        return "", "", ""
    commit, tail = parts[1], parts[2].strip()
    path, _, lines = tail.partition(" lines=")
    return commit, path.strip(), lines.strip()


def _inventory(payload: Mapping[str, Any]) -> dict:
    """本次输入的**白名单**：允许模型读的那些文件，以及几个总数。

    * `entries`：`(仓库, path, 本批次的提交)` 三元组，用来算保守口径的证据覆盖
      （`repository_id` 读不出来时是空串 = **不知道**，见 `_evidence_files`）；
    * `batch_files`：白名单条数（**证据覆盖的分母**）；
    * `window_files`：本版本改动总数（**输入覆盖的分母** —— 只有它才知道「装没装下」）；
    * `listed_files`：提示词里列出的名字数；
    * `window_commits` / `input_commits`：**提交数**的两个口径（见 `coverage_rows`）。

    最后那两个数取自 `window_commits.facts_from_payload` —— **同一个函数**也是提示词里
    那三个数的来源。在这里另算一遍（比如 `len(window_commit_ids)`）看着等价，但两份实现
    迟早会在「有没有去重」「缺键算不算 0」这种地方分叉，而分叉的表现是**报告里两个数
    对不上**，正是要修的那个东西。
    """
    mode = str(payload.get("mode") or MODE_WEEKLY)
    summary = _as_mapping(payload.get("summary"))
    facts = window_commits.facts_from_payload(
        payload, window_commit_ids=payload.get("window_commit_ids") or ()
    )

    if mode == MODE_COMMIT:
        # 单提交模式：这一次分析的对象就是那一条提交改的那一个文件。
        commit = _as_mapping(payload.get("commit"))
        path = str(commit.get("path") or "").strip()
        repository_id = commit.get("repository_id")
        entries = (
            [(repository_id, path, str(commit.get("commit_id") or ""))] if path else []
        )
        return {
            "mode": mode,
            "entries": entries,
            "batch_files": len(entries) or None,
            "window_files": len(entries) or None,
            "listed_files": len(entries) or None,
            "window_commits": None,
            "input_commits": None,
        }

    entries: list[tuple] = []
    for item in payload.get("delta_files") or ():
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("file_path") or "").strip()
        if not path:
            continue
        # 仓库是**身份的一部分**（P1a）：同一条 `(提交, 路径)` 在两个仓库里可能是两份
        # 内容，只按路径记账会让两者互相顶替。写侧本来就带这个键（`scope_sampling`）。
        entries.append(
            (item.get("repository_id"), path, str(item.get("latest_commit_id") or ""))
        )

    listed = payload.get("list_files")
    listed_files = len(listed) if isinstance(listed, (list, tuple)) else None
    # 白名单条数以**这次真的装进输入的那一份**为准（`batch_files`）；它缺失时退回
    # `delta_files` 的条数 —— 这是「字段缺失时别崩」，不是把缺失当成 0。
    batch = _positive_int(summary.get("batch_files")) or len(entries) or None
    # 版本总数**只认 `window_files`**：`total_files` 会被「分析焦点」改写成筛后的条数
    # （`build_weekly_payload`），拿它当版本总数会把「只看了一半」说成「版本就这么大」。
    window = _positive_int(summary.get("window_files")) or _positive_int(
        summary.get("total_files")
    )
    return {
        "mode": mode,
        "entries": entries,
        "batch_files": batch,
        "window_files": window,
        "listed_files": listed_files,
        # 「本窗口有几条提交」与「本次输入覆盖几条提交」—— **两个数**。报告里写错的那个
        # 就是这个（实测 run 54 窗口 4 条、报告开篇写 2 条），所以它必须由程序给出来、
        # 摆在报告里，而不是留给正文自己挑一个。
        "window_commits": facts.actual,
        "input_commits": facts.latest,
    }


def _evidence_files(
    entries: Sequence[tuple], executed: Iterable[Any]
) -> tuple[set, set, set, list[dict]]:
    """逐轮明细 → `(按 (仓库, 路径, 提交) 去重, 按路径去重, 按 (路径, 段) 去重, 没取到的条目)`。

    「取到过」= 工具真的把内容交回给了模型：`failed`（取不到，例如没绑 Agent）与
    `empty`（工具明确回了「确实没有内容」）都不算。这两者分开记账是本仓库的底线，
    这里沿用同一条判据（`services/ai/trace_evidence.summarize_executed`）。

    三个集合分别对应报告里那两种去重口径与「取回多少段」：
    `pairs` 是**保守口径**（这一版改动看过没有），`paths` 是**宽松口径**（这个文件碰过
    没有），`segments` **不是覆盖率**（同一个文件分段读多次会重复计），它只说明前两个数
    是怎么来的。

    `paths` 同时就是 `inspection_coverage` 的分子：**都叫「已检查」，判据只能有一套**
    （`build_ledger` 里那处原先另写了一遍统计，既没排 `failed`/`empty`、也没校验白名单，
    于是「取数失败」被算成了「已检查」）。

    ## `entries` 是三元组，`expected` 是**一列**而不是一个值（P1a）

    `expected` 原先写成 `{路径: 提交}` —— **同一个相对路径在两个仓库里各有一条时，后写
    的顶掉先写的**：读的是 A 仓库那一版，却按 B 仓库那条提交做前缀匹配，匹配不上就被
    当成「没看过」，覆盖账于是少报。现在按路径收成一列三元组，逐条匹配。

    `pairs` 的键是 `(仓库, 提交, 路径)`：**两个仓库的同名文件是两条覆盖**，只算一条会把
    「只看了一半」说成「看完了」。仓库取自逐轮明细的 `repository_id`（`context_tools`
    写进 `meta` 的那一项）；老行没有它 ⇒ 空串 = 不知道，退回按 `(路径, 提交)` 记。
    """
    pairs: set = set()
    paths: set = set()
    segments: set = set()
    failures: list[dict] = []
    expected = _expected_by_path(entries)

    for item in executed or ():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "") not in FILE_EVIDENCE_KINDS:
            continue
        if item.get("failed") or item.get("empty"):
            failures.append(dict(item))
            continue
        commit, path, lines = parse_evidence_label(item.get("label"))
        if not path:
            continue
        # 只算**白名单里真的有**的文件：模型越权点名时平台会把它丢掉（它根本取不到内容），
        # 万一有漏网的一条，把它算进覆盖率就是虚报。
        candidates = expected.get(path)
        if not candidates:
            continue
        repository_id = str(item.get("repository_id") or "")
        # 按路径口径：取过这个文件的任何一版都算。
        paths.add(path)
        segments.add((path, lines))
        # 按 (仓库, 路径, 提交) 口径：标签里只有 12 位提交（`describe_request` 切的），
        # 所以与白名单里的完整提交号做前缀匹配；仓库只有**两边都写得出**时才拿来收窄。
        matched = _match_entry(candidates, commit, repository_id)
        if matched is None:
            continue
        # 键是 `(仓库, 路径, 提交)` 三元组 —— **路径必须在里面**：`expected` 是按路径分
        # 组的，而 `_match_entry` 还回来的只是 `(仓库, 提交)`，直接拿它当键会让所有路径
        # 折叠成同一个键（覆盖数立刻从 N 变成 1）。
        pairs.add((matched[0], path, matched[1]))
    return pairs, paths, segments, failures


def _expected_by_path(entries: Iterable[tuple]) -> dict[str, list]:
    """白名单 → `{路径: [(仓库, 提交), …]}`（同一路径的多个仓库**都留着**）。"""
    expected: dict[str, list] = {}
    for entry in entries or ():
        try:
            repository_id, path, commit = entry
        except (TypeError, ValueError):
            continue
        text = str(path or "").strip()
        if not text:
            continue
        expected.setdefault(text, []).append(
            (str(repository_id or "").strip(), str(commit or "").strip())
        )
    for rows in expected.values():
        rows.sort()
    return expected


def _match_entry(candidates: list, commit: str, repository_id: str):
    """这条证据对应白名单里的哪一条 `(仓库, 提交, 路径)`（对不上返回 `None`）。

    提交对不上就是**没看过这一版**（放宽会让覆盖率虚报）。仓库只在两边都知道、且其中
    有一条正好对上时才用来收窄 —— 拿一个「不知道」去和「知道」比，那一条会永远匹配不上，
    症状是覆盖率永远差一截。
    """
    if not commit:
        return None
    hits = [row for row in candidates if row[1] and row[1].startswith(commit)]
    if not hits:
        return None
    if repository_id:
        exact = [row for row in hits if row[0] == repository_id]
        if exact:
            return exact[0]
    return hits[0]


def _tool_totals(tool_stats: Any) -> dict:
    """`run.tool_stats_json` → 缺口要用的三个合计 + 「有没有这份账」。

    它的键就是工具类型（`file_diff` / `file_content` / `read_reference` / …），值是
    `services/ai/context_tools._STAT_COUNTERS` 那一组计数。它已经是**一家子的合计**
    （子代理模式下所有分片一起算），与消耗面板上「取数（按工具类型）」那张表同源 ——
    缺口里那几个数必须与面板一致，否则同一个事实会出现两个数（用户会以为其中一个错了）。

    **三个都在所有工具类型上求和**（`find_references` 的失败也算）：那些内容同样没有进入
    分析，报告正文里同样要写成信息缺口。只算文件证据那两类会让缺口少报一件事，而它与
    面板上的「失败」列对不上号。
    """
    stats = _as_mapping(tool_stats)
    kinds = {
        str(key): value for key, value in stats.items() if isinstance(value, Mapping)
    }
    if not kinds:
        return {"recorded": False, "failed": None, "refused_by_budget": None, "truncated": None}
    return {
        "recorded": True,
        "failed": _sum_counts(value.get("failed") for value in kinds.values()),
        "refused_by_budget": _sum_counts(
            value.get("refused_by_budget") for value in kinds.values()
        ),
        "truncated": _sum_counts(value.get("truncated") for value in kinds.values()),
    }


def _declared_count(payload: Mapping[str, Any], key: str) -> Optional[int]:
    """`summary.<key>`（平台声明的计数）优先，缺失时退回 `payload[<key>]` 的条数。

    **读不出来就是 `None`**（不是 0）：原先写的是 `_count(summary[key]) or len(payload[key])`，
    缺键时 `None or 0` 会得到 **0** —— 于是「这份 payload 里没有补偿项这个概念」被写成了
    「这轮 0 个补偿项」，与 `_count` 自己的 `None` 语义（`:113`）当场矛盾。
    """
    declared = _count(_as_mapping(payload.get("summary")).get(key))
    if declared is not None:
        return declared
    rows = payload.get(key)
    return len(rows) if isinstance(rows, (list, tuple)) else None


def _extra_input_rows(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """白名单里 `source != delta` 的那些 → `(来源, 路径)`，按原顺序、同路径只留一条。

    来源由 `scope_sampling` 写在**逐文件那一行**上（`compensation` / `dependency`），
    所以「这几条到底取没取到证据」只能在这里看 —— `summary` 里那两个数只是计数。
    """
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in payload.get("delta_files") or ():
        entry = _as_mapping(item)
        source = str(entry.get("source") or "delta").strip() or "delta"
        path = str(entry.get("file_path") or "").strip()
        if source == "delta" or not path or path in seen:
            continue
        seen.add(path)
        rows.append((source, path))
    return rows


def build_ledger(
    *,
    request_payload: Any,
    executed: Optional[Iterable[Any]] = None,
    tool_stats: Any = None,
    response_payload: Any = None,
    unread_shard_skip: Optional[Mapping[str, Any]] = None,
) -> dict:
    """这次运行的覆盖账本（**纯函数**：输入是已落库的 payload、逐轮明细与按工具计数）。

    `executed` 传 `None` 表示**没有采集过逐轮明细**（老运行）：那时证据覆盖与缺口里的
    「取不到」都是 `None`（未知），报告里写「未记录」而不是 0。传空列表表示「采集过，
    而且一条都没有」—— 那是两个完全不同的结论，不能合并。

    `unread_shard_skip` 由调用方（`ledger_from_run` 那一侧）算好传进来，**不在这里重算**：
    它要读未读账的原因，而那是 `snapshot_store.unread_files` 的判断 —— 本模块是纯函数，
    拿不到 run 行，重写一份必然与那一份对不上。不传 = 没有这条补充（缺口里不提）。
    """
    payload = _as_mapping(request_payload)
    inventory = _inventory(payload)
    entries = inventory["entries"]
    batch = inventory["batch_files"]
    window = inventory["window_files"]
    listed = inventory["listed_files"]

    collected = executed is not None
    executed_rows = tuple(executed or ()) if collected else ()
    if collected:
        pairs, paths, segments, failures = _evidence_files(entries, executed_rows)
    else:
        pairs, paths, segments, failures = set(), set(), set(), []

    # 「已检查」= 白名单里**真的取到过内容**的文件（按「文件」去重）。判据与 `_evidence_files`
    # 同源（`failed` / `empty` 不算、白名单外的路径不算）——原先这里另写了一遍统计，两个都
    # 没做，于是**取数失败被算成了已检查**。
    inspected_paths: set[str] = paths

    # 补偿 / 依赖核查项：逐条看它取到证据没有（`summary` 里那两个数只是计数，
    # 而「哪一条没看成」才是报告里要写成缺口的东西）。
    extra_rows = _extra_input_rows(payload)
    extra_by_source: dict[str, dict] = {}
    for source, path in extra_rows:
        bucket = extra_by_source.setdefault(
            source, {"total": 0, "fetched": 0, "pending_paths": []}
        )
        bucket["total"] += 1
        if not collected:
            continue
        if path in paths:
            bucket["fetched"] += 1
        else:
            bucket["pending_paths"].append(path)

    manifest = _as_mapping(payload.get("manifest"))
    manifest_entries = manifest.get("entries") if isinstance(manifest.get("entries"), list) else []
    assigned_total = _count(manifest.get("total"))
    assigned_count = _count(manifest.get("assigned"))
    if assigned_total is None and manifest_entries:
        assigned_total = len(manifest_entries)
    if assigned_count is None and manifest_entries:
        assigned_count = sum(bool(_as_mapping(item).get("assigned_shards")) for item in manifest_entries)

    evidence_pair = len(pairs) if collected else None
    evidence_path = len(paths) if collected else None
    evidence_segment = len(segments) if collected else None

    totals = _tool_totals(tool_stats)
    failed_requests = totals["failed"]
    if failed_requests is None and collected:
        # 没有按工具计数（很老的行）时退回明细：`failed` 与 `empty` 都算「没拿到内容」。
        failed_requests = len(failures)

    policy = _as_mapping(payload.get("policy"))
    focus = _as_mapping(payload.get("focus"))
    list_truncated = bool(policy.get("truncated"))
    truncation_reason = str(policy.get("truncation_reason") or "").strip()

    counts = {
        "window_files": window,
        "batch_files": batch,
        "listed_files": listed,
        # 提交数的两个口径（见 `_inventory`）。读侧不许再用别的方式推这两个数。
        "window_commits": inventory.get("window_commits"),
        "input_commits": inventory.get("input_commits"),
        "evidence_files_by_pair": evidence_pair,
        "evidence_files_by_path": evidence_path,
        # 「取回多少段」**不是覆盖率**：同一个文件分段读多次会重复计（run 12 是 71 段 /
        # 57 个文件）。它只说明上面两个数是怎么来的，别拿它当分子。
        "evidence_segments": evidence_segment,
        # 还没看过的那部分（保守口径）。采集不到时同样是 None。
        "pending_files": (
            batch - evidence_pair if (batch is not None and evidence_pair is not None) else None
        ),
        "failed_requests": failed_requests,
        "refused_by_budget": totals["refused_by_budget"],
        "truncated_items": totals["truncated"],
        "list_truncated": list_truncated,
        "truncation_reason": truncation_reason,
        "tool_stats_recorded": totals["recorded"],
        "compensation_files": _declared_count(payload, "compensation_files"),
        "dependency_files": _declared_count(payload, "dependency_files"),
        # 补偿 / 依赖核查项**逐条**的账（`source` 写在白名单那一行上）：
        # `total` / `fetched` 是计数，`pending_paths` 是没取到证据的那些路径。
        # 采集不到逐轮明细时 `fetched` / `pending_paths` 只能是「未记录」——
        # 那时候平台连「看过哪些文件」都不知道，更说不出这些补上了没有。
        "pending_by_source": {
            source: {
                "total": bucket["total"],
                "fetched": bucket["fetched"] if collected else None,
                "pending": len(bucket["pending_paths"]) if collected else None,
                "pending_paths": bucket["pending_paths"] if collected else None,
            }
            for source, bucket in extra_by_source.items()
        },
        # 未读的那些文件里，有多少**分给的分片这一轮没跑起来**（`snapshot_store` 的注记）。
        # 与 `reason` 是两件事：这一条说的是「为什么没人去看它」，不是「读不到」。
        "unread_shard_skip": dict(unread_shard_skip or {}),
    }
    ledger = {
        "mode": inventory["mode"],
        "scope": str(payload.get("scope") or ""),
        "focus_label": str(focus.get("label") or "").strip(),
        "counts": counts,
        "inventory_coverage": {
            "covered": batch,
            "total": window,
            "ratio": _ratio(batch, window),
        },
        "listed_coverage": {
            "covered": listed,
            "total": batch,
            "ratio": _ratio(listed, batch),
        },
        "assignment_coverage": {
            "covered": assigned_count,
            "total": assigned_total,
            "ratio": _ratio(assigned_count, assigned_total),
        },
        "inspection_coverage": {
            "covered": len(inspected_paths) if collected else None,
            "total": assigned_total or batch,
            "ratio": _ratio(
                len(inspected_paths) if collected else None,
                assigned_total or batch,
            ),
        },
        "evidence_coverage": {
            # 报告与结论按**保守**这一份读（见模块抬头）；宽松那一份同样给出来，免得
            # 同一份数据被另一处按另一种去重口径算出第二个「覆盖率」而无人知道。
            "dedup": "pair",
            "collected": collected,
            "by_pair": {
                "covered": evidence_pair,
                "total": batch,
                "ratio": _ratio(evidence_pair, batch),
            },
            "by_path": {
                "covered": evidence_path,
                "total": batch,
                "ratio": _ratio(evidence_path, batch),
            },
            "failed_labels": [
                str(item.get("label") or item.get("kind") or "") for item in failures
            ],
        },
    }
    ledger["findings"] = _findings_account(response_payload)
    ledger["limited"] = is_limited(ledger)
    ledger["scope_note"] = SCOPE_LIMITED_NOTE if ledger["limited"] else ""
    ledger["rows"] = coverage_rows(ledger)
    ledger["gaps"] = gap_notes(ledger)
    return ledger


def is_limited(ledger: Mapping[str, Any]) -> bool:
    """这份账本里有没有**任何**「没看全」的地方（决定要不要给「全量」加限定语）。

    **拿不到证据覆盖时返回 False**：那时平台对它看了多少一无所知，加一句「不等于整个
    版本都看过了」是拿不确定当结论 —— 与不加限定语一样糟（甚至更糟：它看起来像一条
    已经核实过的提醒）。
    """
    counts = ledger.get("counts") or {}
    evidence = ledger.get("evidence_coverage") or {}
    if ledger.get("focus_label"):
        return True
    if counts.get("list_truncated"):
        return True
    if counts.get("failed_requests") or counts.get("refused_by_budget"):
        return True
    if counts.get("truncated_items"):
        return True
    batch = _positive_int(counts.get("batch_files"))
    window = _positive_int(counts.get("window_files"))
    if batch and window and batch < window:
        return True
    if not evidence.get("collected"):
        return False
    pair = _count(counts.get("evidence_files_by_pair"))
    # 采集过明细、白名单非空、但还有文件没取到证据 —— 这就是「全量 ≠ 全读」那一件事。
    if batch and (pair or 0) < batch:
        return True
    return False


def _headline_row(
    ledger: Mapping[str, Any], *, batch: Any, window: Any, evidence: Mapping[str, Any]
) -> str:
    """**漏斗那一行**：窗口里改过的 → 进了本次输入的 → 真的取到证据的。

    三个数连成一条链写（`版本 1343 → 输入 120 → 取证 63`），而不是各占一行、各带一段
    解释 —— 它们本来就是一串收窄，分开写读者要自己去拼。实测里读者（与模型）都没拼出来，
    于是把「全量」（那是**范围**口径）读成了「都看过了」（见 `coverage_rows` 的说明）。

    两种去重口径仍然都给，但只作括号里一小截；完整口径解释在
    `docs/AI分析使用说明.md`（有用例钉着那份文档必须写清两种去重）。
    **百分比跟着 `取证` 走**（`取证 57（5.7%）`）：跑完一轮实测里那个 5.7% 就是这个功能
    存在的理由，光写两个数读者要自己去除。

    提交数也并进这一行的尾巴：它防的是「正文自己挑一个提交数」（run 54 实测：窗口 4 条、
    报告开篇写 2 条），**数不能丢**，但不必各占一行。数**取不到时也要说**（「未记录」）：
    悄悄省掉一个数，读起来就成了「这个窗口没有提交」（同 `_count` 的 `None` 语义）。
    """
    batch_n = _positive_int(batch)
    window_n = _positive_int(window)
    pairs = evidence.get("by_pair") or {}
    covered = _count(pairs.get("covered")) if evidence.get("collected") else None
    with_window = str(ledger.get("mode") or "") != MODE_COMMIT
    counts = ledger.get("counts") or {}

    if with_window:
        head = (
            f"版本 {_num(window_n)} 个文件 → 输入 {_num(batch_n)}"
            if window_n is not None
            else f"版本{UNKNOWN} → 输入 {_num(batch_n)}"
        )
    else:
        # 单提交模式没有「窗口」这一层（分析对象就是那一条提交的那个文件），写了会让读者
        # 去找一个不存在的版本清单。
        head = f"这条提交改了 {_num(batch_n)} 个文件"

    if covered is None:
        tail_num = f" → 取证{UNKNOWN}"
    elif covered == 0:
        # 0 没有百分比可言（`0 / 0` 与 `0 / 3` 都会算出 0%），只写数。
        tail_num = " → 取证 0"
    else:
        rate = pairs.get("ratio")
        tail_num = f" → 取证 {covered}" + (f"（{_percent(rate)}）" if rate is not None else "")

    # 另外两截都只放括号里：另一种去重口径（一样时不写，写了只是噪声）、取回的段数
    # （一个文件可能分段读几遍 —— 「57 个文件、71 段证据」这个差别读者会问）。
    marks: list[str] = []
    by_path = evidence.get("by_path") or {}
    path_n = _count(by_path.get("covered")) if evidence.get("collected") else None
    if path_n is not None and path_n != covered:
        marks.append(f"按文件去重 {path_n}")
    segments = _count(counts.get("evidence_segments"))
    if segments:
        marks.append(f"{segments} 段证据")
    dedup = f"（{'；'.join(marks)}）" if marks else ""

    commit_part = ""
    if with_window:
        window_commits = _count(counts.get("window_commits"))
        input_commits = _count(counts.get("input_commits"))
        commit_part = (
            f"；本窗口 {window_commits} 条提交"
            if window_commits is not None
            else "；本窗口的提交数未记录"
        )
        # 两个数**各自**给出：「窗口数读不出来」不该把「进了本次输入的条数」一起吞掉 ——
        # 后者是从 `delta_files` 算得出来的（`test_a_payload_without_the_window_list...`）。
        commit_part += (
            f"、其中 {input_commits} 条进了本次输入"
            if input_commits is not None
            else "（其中进了本次输入的条数未记录）"
        )

    if with_window:
        funnel = (
            "（漏斗：窗口里改动过的 → 进了本次输入的 → 真的取到证据的，"
            "**都不是 100% 是常态**）"
        )
    else:
        funnel = (
            "（这条提交改的文件里有多少真的取到证据 —— **不是 100% 是常态**）"
        )
    return head + tail_num + dedup + commit_part + funnel


def _findings_row(findings: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """结论那一行（**本轮新增 / 基线继承**）；没有这份数据时返回 `None`（一个字都不说）。"""
    if not findings.get("recorded"):
        return None
    current = _count(findings.get("current"))
    inherited = _count(findings.get("inherited"))
    total = _count(findings.get("total"))
    retracted = _count(findings.get("retracted")) or 0
    value = (
        f"本轮新增 {_num(current)} 条 + 基线继承 {_num(inherited)} 条 = 在挂 {_num(total)} 条"
        "（**报告正文只写本轮那几条**，所以正文的条数比清单少是正常的；两处不一致时以"
        "这一行为准）"
    )
    if retracted:
        value += f"；另有 {retracted} 条已按复核裁决撤销（仍留在审计轨迹里）"
    return ("结论（本轮 / 继承）", value)


def coverage_rows(ledger: Mapping[str, Any]) -> list[tuple[str, str]]:
    """账本 → 报告元信息表里的那几行（`(项, 内容)`，**给人读的中文**）。

    放在这里而不是 `report_document`：数字与口径是同一件事，措辞与算法分开写迟早会说不到
    一起（这一行按文件去重、那一行按提交去重）。

    ## 2026-09-25：从 12~14 行收到 4 行

    产品负责人的原话是「只需要简单归类说明即可，不需要列得很详细」。收的原则**不是少写
    几个数，是同一个事实只说一遍**：

    * `覆盖（版本清单）`（本版本改了 N 个、其中 M 个进了输入）与 `覆盖（取到证据）`
      （两种去重口径）并进 `本次覆盖` 那条漏斗；
    * `提交（本窗口）` / `提交（本次输入）` 并进同一条的尾巴（为什么不能丢，见
      `_headline_row`）；
    * `覆盖（已分配）` / `覆盖（已检查）` 合成一条 `覆盖（分层）`；
    * `补偿输入` / `依赖输入` 合成一条 `额外输入`；
    * `覆盖（列出的名字）` 整行删掉 —— 它只在**清单长到列不下**时才有意义，而那件事在缺口里
      本来就有一条（`gap_notes` 的「变更清单长到列不下」）。

    **一个事实都没丢**：删掉的那些要么并进了别的行，要么只在它真的构成缺口时才出现。
    逐条数字（未读清单、每一条失败原因）仍在账本的结构化字段里，面板与运行轨迹读它们。
    """
    counts = ledger.get("counts") or {}
    evidence = ledger.get("evidence_coverage") or {}
    batch = _positive_int(counts.get("batch_files"))
    window = _positive_int(counts.get("window_files"))
    # 单提交模式：分析对象就是那一条提交改的那一个文件。「版本清单」这类措辞会让人去找一个
    # 不存在的版本 / 第二份清单，所以那一行（分层）在这里不出现（数字口径不变）。
    single_commit = str(ledger.get("mode") or "") == MODE_COMMIT

    rows: list[tuple[str, str]] = [
        ("本次覆盖", _headline_row(ledger, batch=batch, window=window, evidence=evidence))
    ]

    if not single_commit:
        # 三层覆盖率里**要摆出来的那两个**：`assignment_coverage` / `inspection_coverage`
        # 原先算了却没有任何消费点（算了不显示 = 没人看得到）。口径与那两个字段同源，
        # 不许在这里另算；`_layers_note` 读的也是同一份（缺口那条用它）。
        assigned = ledger.get("assignment_coverage") or {}
        inspected = ledger.get("inspection_coverage") or {}
        rows.append(
            (
                "覆盖（分层）",
                f"已分配 {_layer_text(assigned.get('covered'), assigned.get('total'))}"
                f"，已检查 {_layer_text(inspected.get('covered'), inspected.get('total'))}"
                "（分配＝完整清单里铺到分片的；检查＝真的取到过 diff 或正文的）",
            )
        )

    # **0 与「未记录」必须分得开**：两个都是 `if x:` 才输出时，「这轮 0 个补偿项」与
    # 「这份 payload 里没有这个概念」在报告里长得一模一样（`_count` 的 `None` 语义）。
    rows.append(
        (
            "额外输入",
            "、".join(
                (
                    _extra_input_row("补偿项", counts.get("compensation_files")),
                    _extra_input_row("依赖核查项", counts.get("dependency_files")),
                )
            )
            + "（上轮没看到、这一轮专门补上的，以及改了它就要跟着看的那一跳）",
        )
    )

    # 结论的**两个数**（本轮新增 / 基线继承）：清单里同时有本轮新报的与上一轮继承下来的。
    findings_row = _findings_row(ledger.get("findings") or {})
    if findings_row:
        rows.append(findings_row)

    return rows


def _unread_shard_skip(run: Any) -> dict:
    """没取到证据、且**分给它的分片这一轮全没跑**的那些文件（计数 + 分片名）。

    与 `payload["unread"]` 同源：直接问算出那份账的函数，不在这里重算一遍 —— 两份判据
    迟早会对不上，而它们说的是同一件事（`snapshot_store` 在函数内导入：那一层 import 了
    本模块，模块级导入会成环）。

    算不出来时回 `{"count": None}`：**未知不写成 0** —— 写成 0 就是「没有文件落在没跑的
    分片上」，那是一个结论，而这里只是没算出来（同 `_findings_account` 的纪律）。
    """
    try:
        from services.ai.snapshot_store import UNREAD_SHARD_SKIPPED_KEY, unread_files

        items = unread_files(run)
    except Exception:  # noqa: BLE001 —— 少一句补充不该让整本账算不出来
        return {"count": None}
    if items is None:
        return {"count": None}
    hit = [item for item in items if item.get(UNREAD_SHARD_SKIPPED_KEY)]
    return {
        "count": len(hit),
        "total": len(items),
        "shards": sorted({str(name) for item in hit for name in (item.get("shards") or [])}),
    }


def gap_notes(ledger: Mapping[str, Any]) -> list[str]:
    """账本 → 「缺口」那几句话（**一类一句**，每句都是「类别：条数 —— 下一步」）。

    **缺口与覆盖是两个方向的事实**：覆盖说「看了多少」，缺口说「没看的是哪些、为什么」。
    只给覆盖率，读者知道「有 94% 没看」却不知道那是清单截断、取数失败、还是额度用完了 ——
    这三件事下一步该做什么完全不同（改配置 / 查 Agent / 调额度）。

    ## 2026-09-25：收的是「解释」，一个类别都没合并

    产品负责人说这一节「不需要列得很详细」。收掉的是每条后面的**解释性从句与举例**
    （原来一条 2~3 行），**类别保持一个不少**：每一类的下一步动作不同，合成一句话读者就
    不知道该动哪个旋钮 —— 那正是 `test_every_gap_kind_shows_up` 钉着的事。所以每一类仍然
    各自一句，只是把形状统一成「类别：条数 —— 下一步」。

    逐条的未读清单与每条失败原因在面板「思考过程」的结构化账本里，报告里不抄第二份。
    **唯一的例外是「取数失败」保留一两个路径**：审计要求覆盖段里点得出取数失败的那个文件
    （`tests/test_ai_drawer_coverage.py`），而它也正是「需要人工看一眼」的那一类。
    """
    counts = ledger.get("counts") or {}
    evidence = ledger.get("evidence_coverage") or {}
    notes: list[str] = []

    if counts.get("list_truncated"):
        batch, listed = _positive_int(counts.get("batch_files")), _count(counts.get("listed_files"))
        if batch and listed is not None:
            notes.append(
                f"**变更清单长到列不下**：{batch} 个里只列了 {listed} 个名字 —— 名字没列出"
                "**不等于**读不到，这些文件仍可点名索取。"
            )
    if evidence.get("collected"):
        pending = _count(counts.get("pending_files"))
        if pending:
            notes.append(
                f"**没有取到证据**：还有 {pending} 个（本次输入 "
                f"{_num(counts.get('batch_files'))} 个；{_layers_note(ledger)}）—— "
                "这不是「这些文件没问题」，是**这次没有看**。"
            )
    else:
        notes.append(
            "**这次没有留下取数明细**：平台说不出「看过哪些文件」—— 下面那些结论"
            "**不要**当成「这些文件都看过了」。"
        )
    notes.extend(_extra_input_gaps(ledger))
    skipped = ledger.get("counts", {}).get("unread_shard_skip") or {}
    if _count(skipped.get("count")):
        names = "、".join(skipped.get("shards") or [])
        notes.append(
            f"**分片这一轮没跑起来**：{skipped['count']} 个文件因此没取到证据（{names}）"
            "—— 这一条**不是**「模型漏看」，是那条分片被跳过了（原因写在它那一行上）；"
            "它们已带着连续未读次数进了下一轮的补偿清单。"
        )
    failed = _count(counts.get("failed_requests"))
    if failed:
        labels = [one for one in (evidence.get("failed_labels") or []) if one]
        example = f"例如：{'；'.join(labels[:3])}" if labels else ""
        notes.append(
            f"**取数失败 {failed} 次** —— 这些内容没有进入分析，报告正文里要写成信息缺口"
            f"（逐条原因见面板「思考过程」）。{example}"
        )
    refused = _count(counts.get("refused_by_budget"))
    if refused:
        notes.append(
            f"**有 {refused} 次索取因为超出本次的上下文额度没有执行** —— 还有想看的没看成"
            "（额度在项目管理页 →「AI 分析配置」的「上下文索取上限」里调）。"
        )
    truncated_items = _count(counts.get("truncated_items"))
    if truncated_items:
        notes.append(
            f"**有 {truncated_items} 条取数在交给模型之前被截断**（只砍了尾巴）—— "
            "被砍掉的那部分模型没有看到。"
        )
    if ledger.get("focus_label"):
        notes.append(
            f"**本次只分析了「{ledger['focus_label']}」** —— 其它仓库的改动不在这次输入里，"
            "结论不能读成「整个版本没问题」。"
        )
    return notes


def _findings_account(response_payload: Any) -> dict:
    """结论账：**本轮新增 / 基线继承 / 合计**（读 `response_payload.final_findings`）。

    ## 它要说清的是一次实测里的自相矛盾（run 58）

    报告正文写「主结论共 13 条」，而承载结论的载荷里是 50 条 —— 差的那些是**上一轮留下的、
    本轮继续挂着**的条目（`incremental_baseline` 的 `source="baseline"` 行）。两个数各自都对，
    但它们不是同一个集合；读者先看到 13、再翻到 50 条清单，只能猜是哪个错了（或者干脆
    以为平台在虚报）。

    所以这里把两个数**分开写**，并点明正文那一份只含本轮。**没有这份数据时一个字都不说**
    （`recorded=False`）—— 老运行、失败运行都取不到它，把它们当成「0 条结论」比不说更糟。
    """
    payload = _as_mapping(response_payload)
    rows = [item for item in payload.get("final_findings") or [] if isinstance(item, Mapping)]
    if not rows:
        return {"recorded": False}
    active = [item for item in rows if item.get("active") is not False]
    inherited = sum(1 for item in active if str(item.get("source") or "") == "baseline")
    return {
        "recorded": True,
        "total": len(active),
        "inherited": inherited,
        "current": len(active) - inherited,
        "retracted": len(rows) - len(active),
    }


def _layers_note(ledger: Mapping[str, Any]) -> str:
    """「已分配 X / 已检查 Y」那一小句（缺口与行共用同一份账）。

    只写「还有 N 个没看」看不出成因：是分片压根没铺到它（已分配 < 总数），还是铺到了但
    这一轮没取到证据（已分配 = 总数、已检查 < 已分配）。两者下一步要做的事完全不同。
    """
    assigned = ledger.get("assignment_coverage") or {}
    inspected = ledger.get("inspection_coverage") or {}
    return f"已分配 {_num(assigned.get('covered'))} / 已检查 {_num(inspected.get('covered'))}"


def _extra_input_gaps(ledger: Mapping[str, Any]) -> list[str]:
    """补偿 / 依赖核查项里**没取到证据**的那些 → 缺口。

    它们进输入的唯一理由就是「上一轮没看到，这一轮补上」/「改了它就要跟着看」——没取到
    证据等于这件事没做成，而原先的缺口里一个字都不提（`gap_notes` 通篇不讲补偿与依赖）。
    """
    counts = ledger.get("counts") or {}
    by_source = counts.get("pending_by_source") or {}
    reasons = {
        "compensation": ("补偿项", "上一轮没看到、这一轮专门补上的那些"),
        "dependency": ("依赖核查项", "与本轮变更文件同名、改了就要跟着看的那一跳"),
    }
    notes: list[str] = []
    for source, (label, why) in reasons.items():
        bucket = by_source.get(source) or {}
        pending = _count(bucket.get("pending"))
        if not pending:
            continue
        paths = [str(one) for one in (bucket.get("pending_paths") or []) if one]
        example = f"例如：{'；'.join(f'`{one}`' for one in paths[:2])}。" if paths else ""
        notes.append(
            f"**{label}没有取到证据**：{_num(bucket.get('total'))} 个里 {pending} 个"
            f"（按「文件」去重）—— 它们是{why}，没取到等于这件事没做成，正文里要写成信息缺口。"
            f"{example}"
        )
    return notes


def ledger_from_run(run: Any) -> dict:
    """一条 `AiAnalysisRun` 行 → 覆盖账本（**读取侧唯一入口**）。

    `request_payload` 是分析开始时落库的输入账（白名单、列出的名字、截断标记）；逐轮明细
    在 `ai_analysis_trace` 里（子代理模式下会有多行，各分片取到的证据**一起算** ——
    它们确实都被看过了，见 `services/ai/subagent.py` 的共享取数）；缺口那三个计数取
    `run.tool_stats_json`（与消耗面板同源，且已经是全家的合计）。

    **明细有没有采集过**只能看那一列是不是 NULL：`decode_evidence` 把「没采集」与
    「采集了但没有」都读成空列表，而这两件事在报告里必须分开说（一个是未知、一个是 0）。
    """
    executed: list[dict] = []
    collected = False
    rows = (
        AiAnalysisTrace.query
        .filter(AiAnalysisTrace.run_id == getattr(run, "id", None))
        .order_by(AiAnalysisTrace.id.asc())
        .all()
    )
    for row in rows:
        if getattr(row, "executed_json", None) is None:
            continue
        collected = True
        executed.extend(decode_evidence(row).get("executed") or [])

    return build_ledger(
        request_payload=getattr(run, "request_payload", None),
        executed=executed if collected else None,
        tool_stats=getattr(run, "tool_stats_json", None),
        # 结论账（P1b-UI）：报告正文只写**本轮**那几条，而承载结论的 payload 里还有
        # 上一轮继承下来的那些 —— 两个数都得说，见 `_findings_account`。
        response_payload=getattr(run, "response_payload", None),
        # 未读项里「分给的分片没跑」那一档（真机实测补的口径）：与 `payload["unread"]`
        # 同源 —— 问算出那份账的函数，不在这里重写一份判据。
        unread_shard_skip=_unread_shard_skip(run),
    )
