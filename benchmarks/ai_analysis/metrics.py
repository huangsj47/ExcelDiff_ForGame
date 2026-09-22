#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测指标：九项，每一项的**定义与边界**都写在下面，不许靠感觉取值。

指标清单（复测文档 5.5 节第 1 条）与本文档里的定义一一对应：

| 指标 | 定义 | 分母（缺了就是 `None`，不是 0） |
|---|---|---|
| 问题召回率 | 被报出来的金标问题 / 金标问题 | 金标 `confirmed` 问题数 |
| 误报率 | 没有对上任何金标问题的报告项 / 报告项 | 报告项总数 |
| 严重度一致性 | 对上的那些里，严重度**完全相同**的比例 | 对上的对数 |
| 证据可定位率 | 至少一条证据同时给出「清单内的文件路径 + 坐标」的报告项比例 | 报告项总数 |
| 变化文件覆盖率 | 变化文件里被报过或被取过证据的比例 | 变化文件数 |
| 依赖闭包覆盖率 | 依赖闭包里**取到过证据**的文件比例 | 依赖闭包文件数 |
| token | 输入/输出/合计 + 缓存命中（复用 `services/ai/usage.py`） | — |
| 费用 | 按当前价格表算；算不出就 `None` | — |
| 耗时 | `duration_ms`（复用落库值） | — |

## 三处最容易糊掉的地方，这里都做了硬约束

1. **召回率与误报率的分母不是同一批东西。** 召回率只算 `verification_state=confirmed`
   的金标；标注时明确写成「假设」的那些（`hypothesis`）既不进召回率的分母，也不把
   报中它们的报告项算成误报 —— 那本来就是「值得看一眼」的东西。它们单独统计。
2. **「对上了」是一件要能复现的事。** 匹配规则是显式的（文件路径重叠 + 标题字符
   二元组 Jaccard），匹配结果逐对落进 `matching`，可以人工核账。没有这个的话
   「召回率 63%」这个数字谁也没法验证。
3. **证据可定位 ≠ 提到了文件名。** 「怪物表.xlsx 里可能有问题」定位不到任何一行，
   复核的人还是得从头读那张表。所以要求**同时**有清单内的路径**和**坐标
   （行/列/工作表/主键，或一个 ≥7 位提交短 SHA）。

## 证据覆盖有**两层口径**，两个数都报、都带名字

复测文档的验收数 42/1009 与平台页面上的 32/1009 是**同一个分母上的两个数**：

* **主口径** `trace_detail`：逐轮明细里取到过内容的文件（`file_diff`/`file_content`，
  非失败非空）—— 判决里以它为准；
* **次口径** `ledger_by_pair`：台账的保守口径，额外要求标签里的短提交号命中白名单里的
  `latest_commit_id`（`coverage_ledger._evidence_files` 的 `pairs`）。

结果里**没有裸的覆盖率数字**：要取数只能从 `evidence_coverage.scopes` 里按
`scope` 找，汇总按 `scope` 归并，A/B 的指标名也自带口径（`evidence_coverage:<口径>`）。
两个数混用不是「数字难看」，是结论反了 —— 详见 `evidence_coverage_scopes`。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from benchmarks.ai_analysis.dataset import (
    SEVERITY_ORDER,
    BenchmarkSample,
    GoldIssue,
    GoldLabel,
    _as_str_list,
    normalize_path,
)

# ---------------------------------------------------------------------------
#  文本解析：路径与坐标
# ---------------------------------------------------------------------------

_FILE_EXTENSIONS = (
    "xlsx", "xlsm", "xlsb", "xls", "csv",
    "lua", "py", "json", "ts", "js", "cs", "cpp", "h", "java", "txt", "xml",
    "yaml", "yml", "md", "proto", "prefab", "asset", "ini", "toml", "sql",
)
_EXT_GROUP = "|".join(_FILE_EXTENSIONS)
# 路径 token：允许下划线/点/斜杠/连字符与中日韩字符。脱敏后的假名形如
# `path_ab12cd34ef56.xlsx`，也走这一条。
_PATH_RE = re.compile(
    r"[A-Za-z0-9_./\\\-一-鿿]*[A-Za-z0-9_\-一-鿿]+\.(?:" + _EXT_GROUP + r")\b",
    re.IGNORECASE,
)
# 坐标：行/列/工作表/主键区间，任意一种即可。
_COORDINATE_RES = (
    re.compile(r":\s*\d+(?:\s*[-~]\s*\d+)?"),        # :12  / :12-30
    re.compile(r"第\s*\d+\s*行"),
    re.compile(r"行\s*\d+"),
    re.compile(r"\bline\s*\d+", re.IGNORECASE),
    re.compile(r"\bL\d+\b"),
    re.compile(r"\[\s*\d+\s*(?:,\s*\d+\s*)+\]"),      # [行, 列] 坐标
    re.compile(r"\bsheet\b", re.IGNORECASE),
    re.compile(r"工作表"),
    re.compile(r"\br\d+c\d+\b", re.IGNORECASE),       # r12c3
)
# 提交短 SHA：git 的 7 位以上十六进制；12 位是本项目日志里的常见形态。
_COMMIT_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_TITLE_SPLIT_RE = re.compile(r"[\s,，。;；:：/\\|()（）\[\]【】<>《》\"'`~!！?？+\-*=#%&@^_]+")


def paths_in_text(text: Any) -> List[str]:
    """从一段自由文本里抓出文件路径 token。"""
    return [normalize_path(match.group(0)) for match in _PATH_RE.finditer(str(text or ""))]


def has_coordinate(text: Any) -> bool:
    """这段文本里有没有「能落到具体位置」的坐标（行/列/工作表/提交短 SHA）。"""
    raw = str(text or "")
    if not raw:
        return False
    if any(pattern.search(raw) for pattern in _COORDINATE_RES):
        return True
    return any(match.group(0) for match in _COMMIT_RE.finditer(raw))


def title_tokens(title: Any) -> Set[str]:
    """标题的字符二元组集合。

    为什么不用空格分词：这些标题是中文的，没有空格可分 —— 一句话会变成**一个** token，
    于是「两个标题不一样」永远成立，匹配率恒为 0。二元组对中文可比，对英文标题也能用
    （`Null daily cap` → {nu,ul,ll,..}），代价是短标题的区分度下降，这一点靠
    `TITLE_MATCH_THRESHOLD` 兜住。
    """
    raw = str(title or "").strip().lower()
    if not raw:
        return set()
    cleaned = [chunk for chunk in _TITLE_SPLIT_RE.split(raw) if chunk]
    tokens: Set[str] = set()
    for chunk in cleaned:
        if len(chunk) == 1:
            tokens.add(chunk)
            continue
        for index in range(len(chunk) - 1):
            tokens.add(chunk[index:index + 2])
    return tokens


def title_similarity(left: Any, right: Any) -> float:
    """两个标题的 Jaccard 相似度（0~1）。任一为空返回 0.0（不是 1.0）。"""
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return shared / union if union else 0.0


# ---------------------------------------------------------------------------
#  报告项归一
# ---------------------------------------------------------------------------

def normalize_finding(row: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    """把一条落库的结论行（`ai_analysis_anomaly`）归一成评测用的报告项。

    `file_path` 是单值列、`evidence` 是自由文本 —— 路径既可能来自列，也可能只写在
    证据里，两处都收，并记录来源（`path_sources`），免得事后分不清
    「模型没给文件」还是「我们没解析出来」。
    """
    if not isinstance(row, dict):
        return {
            "finding_id": f"F{index}",
            "title": "",
            "severity": "unknown",
            "category": "",
            "file_paths": [],
            "path_sources": [],
            "evidence": [],
            "commit_refs": [],
            "raw": {},
        }

    evidence_items: List[str] = []
    for key in ("evidence", "impact", "suggestion", "note"):
        value = row.get(key)
        if not value:
            continue
        # 列里可能是字符串，也可能是清单（归一化过的样本）。两种都收：
        # 只认字符串的话，`evidence: ["..."]` 会被 `str()` 成 `"['...']"` ——
        # 括号和引号进来之后，坐标正则还能碰巧命中，路径正则就开始漏了。
        if isinstance(value, (list, tuple)):
            evidence_items.extend(str(item) for item in value if item)
        else:
            evidence_items.append(str(value))

    file_paths: List[str] = []
    path_sources: List[str] = []
    column_path = normalize_path(row.get("file_path"))
    if column_path:
        file_paths.append(column_path)
        path_sources.append("column")
    for candidate in _as_str_list(row.get("file_paths")):
        normalized = normalize_path(candidate)
        if normalized and normalized not in file_paths:
            file_paths.append(normalized)
            path_sources.append("field")
    for item in evidence_items:
        for path in paths_in_text(item):
            if path not in file_paths:
                file_paths.append(path)
                path_sources.append("evidence")

    finding_id = str(row.get("finding_id") or row.get("fingerprint") or row.get("id") or f"F{index}")
    return {
        "finding_id": finding_id,
        "title": str(row.get("title") or ""),
        "severity": str(row.get("severity") or "unknown").strip().lower(),
        "category": str(row.get("category") or ""),
        "file_paths": file_paths,
        "path_sources": path_sources,
        "evidence": evidence_items,
        "commit_refs": [str(row.get("commit_ref") or "").strip()] if row.get("commit_ref") else [],
        "raw": dict(row),
    }


def normalize_findings(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [normalize_finding(row, index) for index, row in enumerate(rows or [])]


# ---------------------------------------------------------------------------
#  匹配：报告项 ↔ 金标问题
# ---------------------------------------------------------------------------

# 标题相似度门槛。定在 0.34 而不是 0.5：同一件事在报告里的措辞差异很大
# （「空每日上限」/「每日上限为空导致不限次」的二元组 Jaccard 大约 0.4），
# 门槛抬高会把真召回判成漏报；但**文件路径重叠**是硬条件之一，所以放宽标题
# 不会让「不同文件上两件不相干的事」配上对。
TITLE_MATCH_THRESHOLD = 0.34
# 没有文件路径可依赖时（跨文件/跨切面的问题），标题必须很像才算对上。
TITLE_ONLY_THRESHOLD = 0.6


def match_findings_to_gold(
    findings: Sequence[Dict[str, Any]],
    issues: Sequence[GoldIssue],
    *,
    title_threshold: float = TITLE_MATCH_THRESHOLD,
    title_only_threshold: float = TITLE_ONLY_THRESHOLD,
) -> List[Dict[str, Any]]:
    """一对一匹配，返回逐对的账（可人工核）。

    打分：`0.5 * 文件重叠 + 0.5 * 标题相似度`。文件重叠是 Jaccard（报告项路径集合
    与金标路径集合）。命中条件：
      * 有共同文件路径**且**标题相似度 ≥ `title_threshold`；或
      * 金标那条没有文件路径，而标题相似度 ≥ `title_only_threshold`。

    贪心：按分数从高到低取，一条报告项只能对一条金标、一条金标只能被一条报告项认领。
    贪心不是最优匹配，但它是**确定的**：同一份输入永远给出同一份账，这对「两次跑的
    差异」比「理论上更优」重要。
    """
    scored: List[Tuple[float, int, int, float, float]] = []
    for finding_index, finding in enumerate(findings):
        finding_paths = set(finding.get("file_paths") or [])
        for issue_index, issue in enumerate(issues):
            issue_paths = {normalize_path(p) for p in (issue.file_paths or [])}
            shared = finding_paths & issue_paths
            if issue_paths:
                overlap = len(shared) / len(finding_paths | issue_paths) if (finding_paths | issue_paths) else 0.0
            else:
                overlap = 0.0
            similarity = title_similarity(finding.get("title"), issue.title)
            if issue_paths:
                qualifies = bool(shared) and similarity >= title_threshold
            else:
                qualifies = similarity >= title_only_threshold
            if not qualifies:
                continue
            scored.append((0.5 * overlap + 0.5 * similarity, finding_index, issue_index, overlap, similarity))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_findings: Set[int] = set()
    used_issues: Set[int] = set()
    pairs: List[Dict[str, Any]] = []
    for score, finding_index, issue_index, overlap, similarity in scored:
        if finding_index in used_findings or issue_index in used_issues:
            continue
        used_findings.add(finding_index)
        used_issues.add(issue_index)
        finding = findings[finding_index]
        issue = issues[issue_index]
        pairs.append({
            "gold_issue_id": issue.issue_id,
            "gold_severity": issue.severity,
            "finding_id": finding.get("finding_id"),
            # 记**下标**而不只是 id：导出/归一化后的 finding_id 可能重（同一个
            # fingerprint 报了两条），按 id 去重会把两条算成一条。
            "finding_index": finding_index,
            "finding_severity": finding.get("severity"),
            "score": round(score, 4),
            "file_overlap": round(overlap, 4),
            "title_similarity": round(similarity, 4),
        })
    return pairs


# ---------------------------------------------------------------------------
#  证据可定位
# ---------------------------------------------------------------------------

def evidence_locatable(finding: Dict[str, Any], inventory: Set[str]) -> Tuple[bool, str]:
    """报告项是否「可定位」。返回 `(是否, 原因)`，原因是给核账用的。

    要求（两条都要）：
      1. 至少一条证据里出现**清单内**的文件路径（清单 = 变化文件 ∪ 依赖闭包 ∪
         本次真的取到过证据的文件）；
      2. 同一份证据里出现坐标（行/列/工作表/主键坐标，或 ≥7 位提交短 SHA）。
    """
    items = list(finding.get("evidence") or [])
    if finding.get("commit_refs"):
        items.extend(str(ref) for ref in finding["commit_refs"])
    known_paths = [p for p in (finding.get("file_paths") or []) if p in inventory]
    if not known_paths:
        return False, "证据里的文件路径不在本次清单内（或根本没给路径）"
    for item in items:
        if has_coordinate(item):
            return True, ""
    if finding.get("commit_refs"):
        for ref in finding["commit_refs"]:
            if _COMMIT_RE.fullmatch(str(ref).strip()):
                return True, ""
    return False, "给了文件但没有任何坐标（行/列/工作表/主键/提交短 SHA）"


# ---------------------------------------------------------------------------
#  单项指标
# ---------------------------------------------------------------------------

def _ratio(covered: Optional[int], total: Optional[int]) -> Optional[float]:
    """比例。分子分母任一为 `None`、或分母为 0 → `None`（未知），**不是 0**。"""
    if covered is None or total is None:
        return None
    if total <= 0:
        return None
    return round(max(0, covered) / total, 6)


# ---------------------------------------------------------------------------
#  证据覆盖的两层口径 —— 同一个分母上的两个数，**不许互相代替**
# ---------------------------------------------------------------------------

# 主口径：**逐轮明细**。只看 `file_diff` / `file_content` 两类、且既不 `failed` 也不
# `empty` 的那些文件 —— 也就是 `profile.evidence_files_from_traces` 收的那一套。
# 复测文档的验收数字 **42/1009** 是这一层（分母是本次改动文件数）。
EVIDENCE_SCOPE_TRACE_DETAIL = 'trace_detail'
# 次口径：台账的**保守**口径 —— 在上一层之上再要求「标签里的短提交号正是白名单里
# 这一条的 `latest_commit_id`」（`coverage_ledger._evidence_files` 的 `pairs`）。
# 平台页面上那个 **32/1009** 是这一层。
EVIDENCE_SCOPE_LEDGER_BY_PAIR = 'ledger_by_pair'

EVIDENCE_SCOPE_LABELS = {
    EVIDENCE_SCOPE_TRACE_DETAIL:
        '逐轮明细口径：取到过内容的文件（file_diff/file_content，非失败非空；不要求提交号对上）',
    EVIDENCE_SCOPE_LEDGER_BY_PAIR:
        '台账保守口径：再要求标签里的短提交号命中白名单里的 latest_commit_id',
}

# 两个数都是真的，回答的是不同问题：大的那个是「这个文件碰过没有」，小的是「这一版
# 改动看全了没有」。**搞错方向的后果不是数字难看，而是结论反了** —— 拿 42 去说
# 「保守口径覆盖 4.2%」会低估漏看，拿 32 去说「明细里看过 32 个」会漏报 10 个文件
# 确实取到过内容。所以每一行都必须带 `scope` 字段，汇总也只按 `scope` 归并
# （见 `TestCoverageScopesAreNotInterchangeable`）。
EVIDENCE_SCOPES: Tuple[str, ...] = (EVIDENCE_SCOPE_TRACE_DETAIL, EVIDENCE_SCOPE_LEDGER_BY_PAIR)


def _trace_detail_paths(sample: BenchmarkSample) -> Set[str]:
    """主口径的文件集合（逐轮明细里**取到过内容**的那些）。

    优先用样本自带的 `evidence_files`（导出时由
    `profile.evidence_files_from_traces` 算好、写进样本的那一份），没有才回退到自己
    从 `trace_rows` 收一遍 —— 同一套判据（`FILE_EVIDENCE_KINDS` + 非 failed 非 empty），
    走平台那两个函数，不另写一份。
    """
    paths = {normalize_path(path) for path in sample.evidence_files}
    paths.discard("")
    if paths:
        return paths

    from benchmarks.ai_analysis.profile import record_like
    from services.ai.coverage_ledger import FILE_EVIDENCE_KINDS, parse_evidence_label
    from services.ai.trace_evidence import decode_evidence

    for row in sample.trace_rows or []:
        if not isinstance(row, dict) or row.get("executed_json") is None:
            continue
        for item in (decode_evidence(record_like(row)).get("executed") or []):
            if str(item.get("kind") or "") not in FILE_EVIDENCE_KINDS:
                continue
            if item.get("failed") or item.get("empty"):
                continue
            _commit, path, _lines = parse_evidence_label(item.get("label"))
            if str(path or "").strip():
                paths.add(normalize_path(path))
    return paths


def _ledger_counts(sample: BenchmarkSample) -> Optional[Dict[str, Any]]:
    """台账口径的原始计数（`build_ledger` 的 `counts`）。

    样本里有导出时算好的 `coverage` 就用它；没有就用样本自带的平台原始
    `request_payload` + 逐轮明细**现算一遍**（同一个平台函数）。两者都拿不到时返回
    `None` —— 那是「这一层没测」，不是「覆盖 0」。
    """
    coverage = sample.coverage or {}
    counts = coverage.get("counts") if isinstance(coverage, dict) else None
    if isinstance(counts, dict) and counts:
        return counts

    payload = (sample.inputs or {}).get("request_payload")
    if not isinstance(payload, dict):
        return None
    from benchmarks.ai_analysis.profile import coverage_from_traces

    ledger = coverage_from_traces(sample.trace_rows, request_payload=payload)
    counts = ledger.get("counts")
    return counts if isinstance(counts, dict) else None


def evidence_coverage_scopes(sample: BenchmarkSample, gold: GoldLabel) -> List[Dict[str, Any]]:
    """证据覆盖的**两层口径**，每行都带自己的名字。

    分母统一取「本次改动文件数」（金标给了就用金标的，否则用样本自带的）——
    文档验收的 42/1009 与 32/1009 就是同一个 1009。

    * `trace_detail`（主）：分子 = 逐轮明细里取到过内容、且**在改动清单里**的文件数；
    * `ledger_by_pair`（次）：分子/分母都取自台账（`build_ledger` 的
      `evidence_files_by_pair` / `window_files`，缺失时退回 `batch_files`），
      因为那正是平台页面上那个数的来源，在这里重算一遍就是「另立口径」。

    台账那一层在样本没有原始 payload 时给 `None` + 原因，**不拿主口径的数去顶**。
    """
    change_files = gold.change_files or sample.change_files()
    change_set = {normalize_path(path) for path in change_files}
    change_set.discard("")
    total = len(change_set)

    rows: List[Dict[str, Any]] = []

    detail_paths = _trace_detail_paths(sample)
    detail_covered = len(detail_paths & change_set) if change_set else None
    rows.append({
        "scope": EVIDENCE_SCOPE_TRACE_DETAIL,
        "label": EVIDENCE_SCOPE_LABELS[EVIDENCE_SCOPE_TRACE_DETAIL],
        "covered": detail_covered,
        "total": total or None,
        "ratio": _ratio(detail_covered, total or None),
        # 明细里取到过内容的**总数**（含不在改动清单里的）。分母取改动清单，
        # 所以这个数可能大于 `covered` —— 差值就是「看了改动之外的文件」。
        "evidence_total": len(detail_paths),
        "source": "sample.evidence_files" if sample.evidence_files else "sample.trace_rows",
    })

    counts = _ledger_counts(sample)
    if counts is None:
        rows.append({
            "scope": EVIDENCE_SCOPE_LEDGER_BY_PAIR,
            "label": EVIDENCE_SCOPE_LABELS[EVIDENCE_SCOPE_LEDGER_BY_PAIR],
            "covered": None,
            "total": None,
            "ratio": None,
            "evidence_total": None,
            "source": "none",
            "reason": (
                "样本里没有台账（`coverage`）也没有平台原始 `request_payload`，"
                "无法复核保守口径；**不要**用逐轮明细那个数替它"
            ),
        })
        return rows

    ledger_total = counts.get("window_files") or counts.get("batch_files")
    ledger_covered = counts.get("evidence_files_by_pair")
    rows.append({
        "scope": EVIDENCE_SCOPE_LEDGER_BY_PAIR,
        "label": EVIDENCE_SCOPE_LABELS[EVIDENCE_SCOPE_LEDGER_BY_PAIR],
        "covered": ledger_covered,
        "total": ledger_total,
        "ratio": _ratio(ledger_covered, ledger_total),
        "evidence_total": counts.get("evidence_files_by_path"),
        "source": "build_ledger.counts.evidence_files_by_pair",
    })
    return rows


def _evidence_inventory(sample: BenchmarkSample, gold: GoldLabel) -> Tuple[Set[str], bool]:
    """证据清单与「本次到底采集过明细没有」。

    采集过明细 = 至少一行 trace 的 `executed_json` **不是 NULL**。这是
    `coverage_ledger` 一直用的判据：「没采集过」与「采集了但一条都没有」是两件事，
    混起来会让「这次没记明细」显示成「一个文件都没看」。
    """
    inventory: Set[str] = set()
    for path in (gold.change_files or sample.change_files()):
        inventory.add(normalize_path(path))
    for path in gold.dependency_closure:
        inventory.add(normalize_path(path))
    for path in sample.evidence_files:
        inventory.add(normalize_path(path))
    inventory.discard("")
    # **不要把报告项自己声称的文件加进清单。** 加了的话「路径在清单内」就变成一个
    # 恒真的条件（自己报的就一定在清单里），证据可定位率会虚高成「有没有写坐标」——
    # 那正是这条指标想防的东西：报告里点一个没看过的文件，本来就不该算可定位。

    explicit = sample.inputs.get("evidence_detail_collected")
    if explicit is not None:
        collected = bool(explicit)
    else:
        collected = any(
            row.get("executed_json") not in (None, "")
            for row in sample.trace_rows
            if isinstance(row, dict)
        )
    return inventory, collected


def _token_and_cost(sample: BenchmarkSample, price_table: Any) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[int]]:
    """复用 `services/ai/usage.py` 算 token / 缓存 / 费用，不另立口径。"""
    from services.ai import pricing as pricing_module
    from services.ai.usage import usage_from_run

    usage = usage_from_run(sample.run_like(), price_table=price_table)
    tokens = dict(usage.get("tokens") or {})
    cost_mapping = usage.get("cost")
    amount = pricing_module.amount_of(cost_mapping) if cost_mapping else None
    cost = {
        "amount_exact": pricing_module.amount_exact(amount) if amount is not None else None,
        "amount": cost_mapping.get("amount") if isinstance(cost_mapping, dict) else None,
        "currency": (cost_mapping or {}).get("currency") if isinstance(cost_mapping, dict) else None,
        "reason": (cost_mapping or {}).get("reason") if isinstance(cost_mapping, dict) else "没有价格表",
        "price_version": usage.get("pricing_version") or "",
        "matched_pattern": (cost_mapping or {}).get("matched_pattern") if isinstance(cost_mapping, dict) else None,
    }
    return tokens, cost, usage.get("duration_ms")


# ---------------------------------------------------------------------------
#  一次样本的评测
# ---------------------------------------------------------------------------

def evaluate_sample(sample: BenchmarkSample, gold: GoldLabel, *, price_table: Any = None) -> Dict[str, Any]:
    """算一个样本上的全部九项指标。"""
    findings = normalize_findings(sample.findings)
    confirmed = gold.confirmed_issues()
    hypotheses = gold.hypothesis_issues()
    pairs = match_findings_to_gold(findings, confirmed)

    matched_finding_indexes = {pair["finding_index"] for pair in pairs}
    matched_issue_ids = {pair["gold_issue_id"] for pair in pairs}

    # --- 召回率 ---------------------------------------------------------
    by_severity: Dict[str, Dict[str, Any]] = {}
    for issue in confirmed:
        bucket = by_severity.setdefault(issue.severity, {"matched": 0, "total": 0})
        bucket["total"] += 1
    for issue in confirmed:
        if issue.issue_id in matched_issue_ids:
            by_severity[issue.severity]["matched"] += 1
    recall_by_severity = {
        severity: {
            "matched": bucket["matched"],
            "total": bucket["total"],
            "recall": _ratio(bucket["matched"], bucket["total"]),
        }
        for severity, bucket in sorted(by_severity.items())
    }
    by_family: Dict[str, Dict[str, Any]] = {}
    for issue in confirmed:
        family = str(getattr(issue, "family", "") or "uncategorized")
        bucket = by_family.setdefault(family, {"matched": 0, "total": 0})
        bucket["total"] += 1
        if issue.issue_id in matched_issue_ids:
            bucket["matched"] += 1
    recall_by_family = {
        family: {
            "matched": bucket["matched"],
            "total": bucket["total"],
            "recall": _ratio(bucket["matched"], bucket["total"]),
        }
        for family, bucket in sorted(by_family.items())
    }

    # --- 误报率 ---------------------------------------------------------
    unmatched_findings = [
        finding for index, finding in enumerate(findings)
        if index not in matched_finding_indexes
    ]
    false_positive_notes: List[str] = []
    if hypotheses:
        false_positive_notes.append(
            f"有 {len(hypotheses)} 条金标是标注时明确写作「假设」的，它们不进召回率分母、"
            "也不把报中它们的报告项算成误报（单独列在 gold.hypotheses 里）"
        )

    # --- 严重度一致性 ---------------------------------------------------
    severity_pairs = [
        pair for pair in pairs
        if pair["gold_severity"] in SEVERITY_ORDER and pair["finding_severity"] in SEVERITY_ORDER
        and pair["gold_severity"] != "unknown"
    ]
    exact = sum(1 for pair in severity_pairs if pair["gold_severity"] == pair["finding_severity"])
    deltas = [
        abs(SEVERITY_ORDER[pair["finding_severity"]] - SEVERITY_ORDER[pair["gold_severity"]])
        for pair in severity_pairs
    ]
    severity_confusion: Dict[str, Dict[str, int]] = {}
    for pair in severity_pairs:
        severity_confusion.setdefault(pair["gold_severity"], {})
        row = severity_confusion[pair["gold_severity"]]
        row[pair["finding_severity"]] = row.get(pair["finding_severity"], 0) + 1

    # --- 证据可定位 -----------------------------------------------------
    inventory, evidence_collected = _evidence_inventory(sample, gold)
    unlocatable: List[Dict[str, str]] = []
    locatable_count = 0
    for finding in findings:
        ok, reason = evidence_locatable(finding, inventory)
        if ok:
            locatable_count += 1
        else:
            unlocatable.append({
                "finding_id": str(finding.get("finding_id")),
                "title": str(finding.get("title")),
                "reason": reason,
            })

    # --- 变化文件覆盖率 -------------------------------------------------
    change_files = gold.change_files or sample.change_files()
    cited = set()
    for finding in findings:
        cited.update(normalize_path(p) for p in finding.get("file_paths") or [])
    if evidence_collected:
        cited.update(normalize_path(p) for p in sample.evidence_files)
    cited.discard("")
    covered_change_files = [path for path in change_files if path in cited]
    change_coverage = {
        "covered": len(covered_change_files) if change_files else None,
        "total": len(change_files) if change_files else None,
        "ratio": _ratio(len(covered_change_files), len(change_files)) if change_files else None,
        "source": "gold" if gold.change_files else ("sample" if change_files else "none"),
        "uncovered": [path for path in change_files if path not in cited][:20] if change_files else [],
    }

    # --- 依赖闭包覆盖率 -------------------------------------------------
    closure = gold.dependency_closure
    if not closure:
        closure_coverage = {
            "covered": None, "total": None, "ratio": None,
            "reason": "金标没有给依赖闭包清单（没有依赖图的项目就留空；这时候报 0% 是编的）",
        }
    elif not evidence_collected:
        closure_coverage = {
            "covered": None, "total": len(closure), "ratio": None,
            "reason": "本次没有采集过证据明细（trace.executed_json 全为 NULL），无法判断闭包覆盖",
        }
    else:
        evidence_paths = {normalize_path(p) for p in sample.evidence_files}
        covered = [path for path in closure if path in evidence_paths]
        closure_coverage = {
            "covered": len(covered), "total": len(closure),
            "ratio": _ratio(len(covered), len(closure)),
            "reason": "",
            "uncovered": [path for path in closure if path not in evidence_paths][:20],
        }

    tokens, cost, duration_ms = _token_and_cost(sample, price_table)
    token_total = None
    if tokens.get("input") is not None or tokens.get("output") is not None:
        token_total = int(tokens.get("input") or 0) + int(tokens.get("output") or 0)
    efficiency = {
        "tokens_per_matched_finding": (
            round(token_total / len(matched_issue_ids), 4)
            if token_total is not None and matched_issue_ids
            else None
        ),
        "tokens_per_locatable_evidence": (
            round(token_total / locatable_count, 4)
            if token_total is not None and locatable_count
            else None
        ),
    }

    return {
        "sample_id": sample.sample_id,
        "gold": {
            "issues_total": len(gold.issues),
            "confirmed": len(confirmed),
            "hypotheses": len(hypotheses),
            "change_files": len(change_files),
            "dependency_closure": len(closure),
        },
        "reported": {
            "findings_total": len(findings),
            "with_file": sum(1 for finding in findings if finding.get("file_paths")),
        },
        "recall": {
            "overall": _ratio(len(matched_issue_ids), len(confirmed)),
            "critical": _ratio(
                sum(
                    1
                    for issue in confirmed
                    if issue.severity == "critical" and issue.issue_id in matched_issue_ids
                ),
                sum(1 for issue in confirmed if issue.severity == "critical"),
            ),
            "matched": len(matched_issue_ids),
            "total": len(confirmed),
            "by_severity": recall_by_severity,
            "by_family": recall_by_family,
        },
        "false_positive": {
            "rate": _ratio(len(unmatched_findings), len(findings)),
            "count": len(unmatched_findings),
            "total": len(findings),
            "notes": false_positive_notes,
        },
        "severity": {
            "exact_agreement": _ratio(exact, len(severity_pairs)),
            "matched": len(severity_pairs),
            "mean_level_delta": (round(sum(deltas) / len(deltas), 4) if deltas else None),
            "confusion": severity_confusion,
        },
        "evidence_locatable": {
            "rate": _ratio(locatable_count, len(findings)),
            "count": locatable_count,
            "total": len(findings),
            "unlocatable": unlocatable[:20],
        },
        "changed_file_coverage": change_coverage,
        "dependency_closure_coverage": closure_coverage,
        # 证据覆盖**按口径分开报**，主口径在 `main_scope` 里写明。要取数只能从
        # `scopes` 里按名字找（`summarize` 与 A/B 比较都这么做），没有「裸数字」可拿。
        "evidence_coverage": {
            "main_scope": EVIDENCE_SCOPE_TRACE_DETAIL,
            "scopes": evidence_coverage_scopes(sample, gold),
        },
        "tokens": tokens,
        "efficiency": efficiency,
        "cost": cost,
        "duration_ms": duration_ms,
        "stages": [dict(stage) for stage in sample.stages],
        "matching": pairs,
        "unmatched_findings": [
            {"finding_id": str(item.get("finding_id")), "title": str(item.get("title"))}
            for item in unmatched_findings
        ],
        "missed_gold": [
            {"gold_issue_id": issue.issue_id, "title": issue.title, "severity": issue.severity}
            for issue in confirmed if issue.issue_id not in matched_issue_ids
        ],
    }


# ---------------------------------------------------------------------------
#  数据集汇总
# ---------------------------------------------------------------------------

def _micro(pairs: Iterable[Tuple[Optional[int], Optional[int]]]) -> Dict[str, Any]:
    """微平均：先把所有样本的分子分母加起来，再相除。

    为什么不用宏平均（各样本比例求平均）：一次小样本（金标只有 1 条问题）会把宏平均
    拽得面目全非 —— 它要么 0% 要么 100%，权重却和大样本一样。微平均才是
    「这一批一共漏了几条」的答案。
    """
    covered = 0
    total = 0
    missing = 0
    for numerator, denominator in pairs:
        if numerator is None or denominator is None:
            missing += 1
            continue
        covered += numerator
        total += denominator
    return {
        "covered": covered if total else None,
        "total": total if total else None,
        "ratio": _ratio(covered, total) if total else None,
        "samples_missing": missing,
    }


def _scope_micro_rows(sample_results: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按口径名归并证据覆盖。**每个口径一行，行上带名字**。

    取不到那一层时 `covered/total/ratio` 全是 `None`、`samples_missing` 记满，
    而不是 0 —— 与全篇「`None` = 没测到 ≠ 0」同口径。
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for scope in EVIDENCE_SCOPES:
        pairs: List[Tuple[Optional[int], Optional[int]]] = []
        for result in sample_results:
            block = result.get("evidence_coverage") or {}
            for row in block.get("scopes") or []:
                if row.get("scope") == scope:
                    pairs.append((row.get("covered"), row.get("total")))
        micro = _micro(pairs)
        if not pairs:
            micro["samples_missing"] = len(sample_results)
        micro["scope"] = scope
        micro["label"] = EVIDENCE_SCOPE_LABELS[scope]
        rows[scope] = micro
    return rows


def summarize(sample_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐样本结果汇成一份可比较的总账。"""
    results = list(sample_results)

    tokens_input = 0
    tokens_output = 0
    tokens_missing = 0
    duration_total = 0
    duration_reported = 0
    cost_total = None
    cost_unpriced = 0

    from decimal import Decimal

    from services.ai import pricing as pricing_module

    for result in results:
        tokens = result.get("tokens") or {}
        if tokens.get("input") is None and tokens.get("output") is None:
            tokens_missing += 1
        else:
            tokens_input += int(tokens.get("input") or 0)
            tokens_output += int(tokens.get("output") or 0)
        duration = result.get("duration_ms")
        if duration is None:
            continue
        duration_reported += 1
        duration_total += int(duration)
        raw_amount = (result.get("cost") or {}).get("amount_exact")
        if raw_amount is None:
            cost_unpriced += 1
            continue
        try:
            amount = Decimal(str(raw_amount))
        except Exception:
            cost_unpriced += 1
            continue
        cost_total = amount if cost_total is None else cost_total + amount

    return {
        "samples": len(results),
        "sample_ids": [result.get("sample_id") for result in results],
        "recall": _micro(
            (result["recall"]["matched"], result["recall"]["total"]) for result in results
        ),
        "false_positive": _micro(
            (result["false_positive"]["count"], result["false_positive"]["total"]) for result in results
        ),
        "severity_exact_agreement": _micro(
            (
                None if result["severity"]["exact_agreement"] is None
                else round(result["severity"]["exact_agreement"] * result["severity"]["matched"]),
                result["severity"]["matched"] if result["severity"]["exact_agreement"] is not None else None,
            )
            for result in results
        ),
        "evidence_locatable": _micro(
            (result["evidence_locatable"]["count"], result["evidence_locatable"]["total"])
            for result in results
        ),
        "changed_file_coverage": _micro(
            (result["changed_file_coverage"]["covered"], result["changed_file_coverage"]["total"])
            for result in results
        ),
        "dependency_closure_coverage": _micro(
            (result["dependency_closure_coverage"]["covered"], result["dependency_closure_coverage"]["total"])
            for result in results
        ),
        # 两层口径**各自归并**，每行都带 `scope`。混着归并就是把 42 与 32 加在一起 ——
        # 那个数既不是 42 也不是 32，谁也解释不了。
        "evidence_coverage_by_scope": {
            "main_scope": EVIDENCE_SCOPE_TRACE_DETAIL,
            "rows": _scope_micro_rows(results),
        },
        "tokens": {
            "input": tokens_input,
            "output": tokens_output,
            "total": tokens_input + tokens_output,
            "samples_missing": tokens_missing,
        },
        "cost": {
            "amount_exact": str(cost_total) if cost_total is not None else None,
            "amount": pricing_module.money(cost_total),
            "currency": pricing_module.DEFAULT_CURRENCY,
            "samples_unpriced": cost_unpriced,
        },
        "duration_ms": {
            "total": duration_total if duration_reported else None,
            "samples_reported": duration_reported,
        },
    }


def evaluate_dataset(
    dataset: Any, *, price_table: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """评测整个数据集，返回 `(逐样本结果, 汇总)`。"""
    results: List[Dict[str, Any]] = []
    for sample_id in dataset.paired_ids():
        results.append(
            evaluate_sample(dataset.samples[sample_id], dataset.golds[sample_id], price_table=price_table)
        )
    return results, summarize(results)
