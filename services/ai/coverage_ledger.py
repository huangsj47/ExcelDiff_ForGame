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
from services.ai.trace_evidence import decode_evidence

# 只有这两类工具的结果算「这个文件被看过」：`file_diff`（某个提交上这个文件的差异）与
# `file_content`（这个文件的内容）。`commit_detail` 只给「这个提交改了哪些文件」的名单、
# `find_references` 只回「哪个文件的第几行」、`read_reference` 读的是平台自己的规程文档。
FILE_EVIDENCE_KINDS = ("file_diff", "file_content")

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
    """本次输入的**白名单**：允许模型读的那些文件，以及三个总数。

    * `entries`：`(path, 本批次的提交)`，用来算保守口径的证据覆盖；
    * `batch_files`：白名单条数（**证据覆盖的分母**）；
    * `window_files`：本版本改动总数（**输入覆盖的分母** —— 只有它才知道「装没装下」）；
    * `listed_files`：提示词里列出的名字数。
    """
    mode = str(payload.get("mode") or MODE_WEEKLY)
    summary = _as_mapping(payload.get("summary"))

    if mode == MODE_COMMIT:
        # 单提交模式：这一次分析的对象就是那一条提交改的那一个文件。
        commit = _as_mapping(payload.get("commit"))
        path = str(commit.get("path") or "").strip()
        entries = [(path, str(commit.get("commit_id") or ""))] if path else []
        return {
            "mode": mode,
            "entries": entries,
            "batch_files": len(entries) or None,
            "window_files": len(entries) or None,
            "listed_files": len(entries) or None,
        }

    entries: list[tuple[str, str]] = []
    for item in payload.get("delta_files") or ():
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("file_path") or "").strip()
        if not path:
            continue
        entries.append((path, str(item.get("latest_commit_id") or "")))

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
    }


def _evidence_files(
    entries: Sequence[tuple[str, str]], executed: Iterable[Any]
) -> tuple[set, set, set, list[dict]]:
    """逐轮明细 → `(按 (提交, 路径) 去重, 按路径去重, 按 (路径, 段) 去重, 没取到的条目)`。

    「取到过」= 工具真的把内容交回给了模型：`failed`（取不到，例如没绑 Agent）与
    `empty`（工具明确回了「确实没有内容」）都不算。这两者分开记账是本仓库的底线，
    这里沿用同一条判据（`services/ai/trace_evidence.summarize_executed`）。

    三个集合分别对应报告里那两种去重口径与「取回多少段」：
    `pairs` 是**保守口径**（这一版改动看过没有），`paths` 是**宽松口径**（这个文件碰过
    没有），`segments` **不是覆盖率**（同一个文件分段读多次会重复计），它只说明前两个数
    是怎么来的。
    """
    pairs: set = set()
    paths: set = set()
    segments: set = set()
    failures: list[dict] = []
    expected = {path: commit for path, commit in entries}

    for item in executed or ():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "") not in FILE_EVIDENCE_KINDS:
            continue
        if item.get("failed") or item.get("empty"):
            failures.append(dict(item))
            continue
        commit, path, lines = parse_evidence_label(item.get("label"))
        # 只算**白名单里真的有**的文件：模型越权点名时平台会把它丢掉（它根本取不到内容），
        # 万一有漏网的一条，把它算进覆盖率就是虚报。
        if not path or path not in expected:
            continue
        # 按路径口径：取过这个文件的任何一版都算。
        paths.add(path)
        segments.add((path, lines))
        # 按 (提交, 路径) 口径：只有「正是白名单里那一条」才算。标签里只有 12 位提交
        # （`describe_request` 切的），所以与白名单里的完整提交号做前缀匹配。
        expect = expected[path]
        if expect and commit and not str(expect).startswith(commit):
            continue
        pairs.add((path, expect))
    return pairs, paths, segments, failures


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


def build_ledger(
    *,
    request_payload: Any,
    executed: Optional[Iterable[Any]] = None,
    tool_stats: Any = None,
) -> dict:
    """这次运行的覆盖账本（**纯函数**：输入是已落库的 payload、逐轮明细与按工具计数）。

    `executed` 传 `None` 表示**没有采集过逐轮明细**（老运行）：那时证据覆盖与缺口里的
    「取不到」都是 `None`（未知），报告里写「未记录」而不是 0。传空列表表示「采集过，
    而且一条都没有」—— 那是两个完全不同的结论，不能合并。
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

    inspected_paths: set[str] = set()
    if collected:
        for raw in executed_rows:
            item = _as_mapping(raw)
            if str(item.get("kind") or "") not in FILE_EVIDENCE_KINDS:
                continue
            _commit, path, _lines = parse_evidence_label(item.get("label"))
            if path:
                inspected_paths.add(str(path))

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
        "compensation_files": _count(
            _as_mapping(payload.get("summary")).get("compensation_files")
        ) or len(payload.get("compensation_files") or []),
        "dependency_files": _count(
            _as_mapping(payload.get("summary")).get("dependency_files")
        ) or len(payload.get("dependency_files") or []),
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


def coverage_rows(ledger: Mapping[str, Any]) -> list[tuple[str, str]]:
    """账本 → 报告元信息表里的那几行（`(项, 内容)`，**给人读的中文**）。

    放在这里而不是 `report_document`：数字与口径是同一件事，措辞与算法分开写迟早会说不到
    一起（这一行按文件去重、那一行按提交去重）。
    """
    counts = ledger.get("counts") or {}
    evidence = ledger.get("evidence_coverage") or {}
    batch = _positive_int(counts.get("batch_files"))
    window = _positive_int(counts.get("window_files"))
    listed = _count(counts.get("listed_files"))
    # 单提交模式：分析对象就是那一条提交改的那一个文件。「版本清单」「列出的名字」那两种
    # 措辞在这里会让人去找一个不存在的版本 / 第二份清单，所以换一套说法（数字口径不变）。
    single_commit = str(ledger.get("mode") or "") == MODE_COMMIT
    rows: list[tuple[str, str]] = []

    if single_commit:
        rows.append(
            (
                "覆盖（本次提交）",
                f"这条提交改了 {batch} 个文件，都在本次输入里" if batch else UNKNOWN,
            )
        )
    elif window and batch:
        if batch >= window:
            rows.append(("覆盖（版本清单）", f"本版本改动过的 {window} 个文件全部进了本次输入"))
        else:
            rows.append(
                (
                    "覆盖（版本清单）",
                    f"本版本改动过 {window} 个文件，其中 {batch} 个进了本次输入"
                    f"（另有 {window - batch} 个不在本次输入里）",
                )
            )
    elif batch:
        rows.append(("覆盖（版本清单）", f"本次输入包含 {batch} 个文件（版本改动总数{UNKNOWN}）"))
    else:
        rows.append(("覆盖（版本清单）", UNKNOWN))

    # 名字那一行只在周版本模式下有意义（单提交模式没有第二份清单，那一个文件就在提示词里）。
    if not single_commit:
        if listed is None:
            rows.append(("覆盖（列出的名字）", UNKNOWN))
        elif batch and listed < batch:
            rows.append(
                (
                    "覆盖（列出的名字）",
                    f"{batch} 个文件里列出了 {listed} 个名称；其余 {batch - listed} 个没列出名字，"
                    "但它们仍在可读范围内（名字没列出 ≠ 读不到，模型可以点名索取）",
                )
            )
        elif batch:
            rows.append(("覆盖（列出的名字）", f"{batch} 个文件的名称都列进了提示词"))
        else:
            rows.append(("覆盖（列出的名字）", UNKNOWN))

    compensation = _count(counts.get("compensation_files")) or 0
    dependency = _count(counts.get("dependency_files")) or 0
    if compensation:
        rows.append(("补偿输入", f"包含 {compensation} 个上轮未覆盖补偿项，按本轮证据重新核查"))
    if dependency:
        rows.append(("依赖输入", f"包含 {dependency} 个依赖核查项，用于验证改动的上下游影响"))

    if not evidence.get("collected"):
        rows.append(
            (
                "覆盖（取到证据）",
                "这次运行没有留下取数明细，**未知**（不是 0）—— 平台只能说「没有记录」，"
                "不能说「一个文件都没看」",
            )
        )
        return rows

    by_pair = evidence.get("by_pair") or {}
    by_path = evidence.get("by_path") or {}
    pair_n = _count(by_pair.get("covered"))
    path_n = _count(by_path.get("covered"))
    if pair_n is not None and pair_n == path_n:
        delta_note = "这次两个口径算出来**一样** —— 它没有去翻更早那条提交的版本"
    else:
        delta_note = "比上面那个**大** —— 它翻过这个文件更早那条提交的版本，那也算「碰过这个文件」"
    rows.append(
        (
            "覆盖（取到证据）",
            f"{_num(pair_n)} / {_num(by_pair.get('total'))}"
            f"（{_percent(by_pair.get('ratio'))}）—— **按「(本批次最新提交, 文件)」去重**，"
            "本报告一律用这一份（保守口径）",
        )
    )
    rows.append(
        (
            "覆盖（同上·另一种去重）",
            f"{_num(path_n)} / {_num(by_path.get('total'))}"
            f"（{_percent(by_path.get('ratio'))}）—— 按「文件」去重"
            f"（取过这个文件的任何一版就算）：{delta_note}。"
            f"这次一共取回 {_num(counts.get('evidence_segments'))} 段证据"
            "（同一个文件分段读多次会重复计，**它不是覆盖率**）",
        )
    )
    return rows


def gap_notes(ledger: Mapping[str, Any]) -> list[str]:
    """账本 → 「缺口」那几句话（一条一句，报告里按条列出来）。

    **缺口与覆盖是两个方向的事实**：覆盖说「看了多少」，缺口说「没看的是哪些、为什么」。
    只给覆盖率，读者知道「有 94% 没看」却不知道那是清单截断、取数失败、还是额度用完了 ——
    这三件事下一步该做什么完全不同（改配置 / 查 Agent / 调额度）。
    """
    counts = ledger.get("counts") or {}
    evidence = ledger.get("evidence_coverage") or {}
    notes: list[str] = []

    if counts.get("list_truncated"):
        batch, listed = _positive_int(counts.get("batch_files")), _count(counts.get("listed_files"))
        tail = f"（{batch} 个文件里只列出了 {listed} 个名称）" if batch and listed is not None else ""
        notes.append(
            f"**变更清单长到列不下**：提示词里只列出了一部分文件名{tail}。"
            "名字没列出来 **≠** 读不到 —— 这些文件仍在可读范围内，模型可以点名索取它们的 diff。"
        )
    if evidence.get("collected"):
        pending = _count(counts.get("pending_files"))
        if pending:
            notes.append(
                f"**没有取到证据**：本次输入的 {_num(counts.get('batch_files'))} 个文件里，"
                f"还有 {pending} 个这次没有任何 diff 或正文进来（按「(最新提交, 文件)」去重）。"
                "这不是「这些文件没问题」，是**这次没有看**。"
            )
    else:
        notes.append(
            "**这次没有留下取数明细**：平台说不出「看过哪些文件」（只能说清输入里有哪些），"
            "所以下面那些结论**不要**当成「这些文件都看过了」。"
        )
    failed = _count(counts.get("failed_requests"))
    if failed:
        labels = [one for one in (evidence.get("failed_labels") or []) if one]
        example = f"例如：{'；'.join(labels[:3])}" if labels else ""
        notes.append(
            f"**取数失败 {failed} 次**：这些内容没有进入分析（逐条原因在抽屉的「思考过程」里），"
            f"报告正文里应把它们写成信息缺口。{example}"
        )
    refused = _count(counts.get("refused_by_budget"))
    if refused:
        notes.append(
            f"**有 {refused} 次索取因为超出本次的上下文额度没有执行**：还有想看的文件没看成"
            "（额度是「一个分析 agent」的，在项目管理页 →「AI 分析配置」的「上下文索取上限」里调）。"
        )
    truncated_items = _count(counts.get("truncated_items"))
    if truncated_items:
        notes.append(
            f"**有 {truncated_items} 条取数在交给模型之前被截断**（只砍了尾巴）："
            "被砍掉的那部分模型没有看到。"
        )
    if ledger.get("focus_label"):
        notes.append(
            f"**本次只分析了「{ledger['focus_label']}」**：其它仓库的改动不在这次输入里，"
            "结论不能读成「整个版本没问题」。"
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
    )
