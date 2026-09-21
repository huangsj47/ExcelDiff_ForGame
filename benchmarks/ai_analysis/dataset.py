#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析评测基线 —— 固定输入 + 人工金标 + 指标 + A/B 比较。

## 为什么必须先有这一层

复测文档第 5.5 节的顺序是「先建评测基线 → 再优化 token」，第 9 节写明任务 E 必须在
金标约束下做。没有金标就改分片预算，等于把「token 降了但召回率也降了」判成成功 ——
Run 20 已经出现过一次同形的事：总 token 比 Run 15 低 6.8%，但严格证据覆盖从
5.92% 掉到 4.2%、耗时反而更长（+8.4%）。单看 token 会得出「优化有效」的结论。

这一层只交付**度量它的能力**，不碰任何 token 优化。

## 三个口径（与平台既有实现一致，不许自己另立）

1. **`None` 是「未上报」，不是 0。** token、缓存、覆盖率字段缺了就报 `None` +
   缺失条数，绝不折成 0 —— 折成 0 之后「这次没采集」与「这次真的是 0」就分不开了。
   比例的分子分母缺一就是 `None`（`services/ai/usage.py`、`coverage_ledger.py` 同源）。
2. **状态看 `effective_status`**（僵尸 running 算 failed），不是 `status`；
   `degraded` 是独立第三态，不折成 succeeded。
3. **费用只在价格表算得出时才报**，读 `amount_exact`（未舍入）做聚合 ——
   `money()` 的产物里有 `"<0.01"` 这种文本，拿去 `Decimal()` 会炸。

## 数据形状

```
<dataset_dir>/
  samples/<sample_id>.json      # 一次分析的全部输入与产出（含 run 行快照）
  gold/<sample_id>.json         # 人工金标（问题清单 + 变化文件 + 依赖闭包）
  pseudonym_map.json            # 脱敏映射（同一路径 → 同一假名，跨批稳定）
  manifest.json                 # 数据集清单（版本、样本数）
```

两者都以 `sample_id` 对齐；`load_dataset` 会把「有样本没金标」「有金标没样本」
分别报出来，而不是静默取交集 —— 静默取交集会让「改了 10 条标注只生效 3 条」
看起来像没改。

读侧解析**复用既有实现**：run 级用量走 `services.ai.usage.usage_from_run`，
证据覆盖走 `services.ai.coverage_ledger.build_ledger`，
trace 明细编解码走 `services.ai.trace_evidence.decode_evidence`。
本模块只负责「把它们拼成一次可比较的评测记录」，不重写任何一套。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
#  schema 与取值
# ---------------------------------------------------------------------------

SAMPLE_SCHEMA = "ai_benchmark_sample/v1"
GOLD_SCHEMA = "ai_benchmark_gold/v1"
MANIFEST_SCHEMA = "ai_benchmark_manifest/v1"

# 与 `services/ai/verdict.py` 的严重度口径对齐；`unknown` 只给「标注时就拿不准」用，
# 不参与严重度一致性的分母（否则一个 unknown 会同时拉低一致性与覆盖率）。
GOLD_SEVERITIES: Tuple[str, ...] = ("critical", "high", "medium", "low", "unknown")
SEVERITY_ORDER: Dict[str, int] = {
    "critical": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0,
}

# 从 `ai_analysis_run` 白名单导出的列。`usage_from_run` / `ledger_from_run` 需要的
# 属性全在这里 —— 少一列就会让复用点静默退化成「未上报」，所以列清单是显式的。
RUN_ROW_FIELDS: Tuple[str, ...] = (
    "id", "project_id", "target_type", "target_id", "target_key",
    "status", "response_mode", "scope", "trigger_source", "degradation",
    "analysis_revision", "model", "prompt_version", "skill_version", "rules_version",
    "rounds_used", "tool_requests_used",
    "tokens_input", "tokens_output", "cache_read_tokens", "cache_write_tokens",
    "cache_source", "duration_ms", "tool_stats_json", "context_chars",
    "anomalies_found", "dropped_count", "pricing_version",
    "subagent_mode", "subagent_count", "conclusion_structured",
    "request_payload", "response_payload", "delta_summary",
    "created_at", "started_at", "finished_at",
)

# 阶段行（`response_payload["subagents"]` 的成员）字段清单。**与
# `services/ai/subagent.py::_member_block` 逐字对齐** —— 那是唯一的生产者。
STAGE_FIELDS: Tuple[str, ...] = (
    "label", "role", "index", "dimensions", "status", "rounds", "requests",
    "tokens_input", "tokens_output", "cache_read_tokens", "cache_write_tokens",
    "anomalies", "report_chars", "skipped_reason", "error",
)

# trace 行的列（`ai_analysis_trace`），导出时按这份白名单取。
TRACE_ROW_FIELDS: Tuple[str, ...] = (
    "id", "run_id", "round_index", "agent", "agent_round", "outcome", "parsed_ok",
    "request_chars", "response_text", "error", "correction_hint",
    "requests_json", "executed_json", "dropped_json", "budget_notes",
    "context_chars", "tokens_input", "tokens_output", "cache_read_tokens",
    "cache_write_tokens", "duration_ms", "created_at",
)

# 结构化结论行（`ai_analysis_anomaly`）。有它才谈得上「报了什么」的机器可读对账；
# 没有它只能从 markdown 正文里猜标题，那种对账不可复现。
FINDING_ROW_FIELDS: Tuple[str, ...] = (
    "id", "run_id", "fingerprint", "title", "category", "severity", "confidence",
    "evidence", "commit_ref", "file_path", "impact", "suggestion", "disposition",
)


class BenchmarkFormatError(ValueError):
    """样本 / 金标不符合 schema 时抛出（带字段路径，便于定位标注错误）。"""


# ---------------------------------------------------------------------------
#  归一化助手
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> List[str]:
    """字符串清单：去空、去重、保持首次出现顺序。

    保持顺序很重要 —— 脱敏后的假名要能按顺序与原清单对齐做差。
    """
    seen: Dict[str, None] = {}
    for item in _as_list(value):
        text = str(item or "").strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


def normalize_path(path: Any) -> str:
    """路径归一：统一分隔符、去掉开头的 `./`，不做小写化。

    大小写不归一：Linux 仓库里 `Foo.lua` 与 `foo.lua` 是两个文件，合并它们会让
    覆盖率算错（而 Windows 上看到的仓库多半又是同一个）。宁可少合并，不要错合并。
    """
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


# ---------------------------------------------------------------------------
#  样本
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkSample:
    """一次分析运行的全部输入与产出（脱敏后）。"""

    sample_id: str
    run_row: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    stages: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    trace_rows: List[Dict[str, Any]] = field(default_factory=list)
    evidence_files: List[str] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    schema: str = SAMPLE_SCHEMA
    created_at: str = ""
    redacted: bool = True
    notes: str = ""

    # -- 复用既有实现的入口 ---------------------------------------------------
    def run_like(self) -> SimpleNamespace:
        """给 `services/ai/usage.py` 那套 helper 用的鸭子对象。

        这些 helper 只按属性名取值（`getattr`），所以把 run 行快照包一层就够，
        不需要为评测另写一份 token/费用口径 —— 那种「另写一份」正是两套数字
        开始不一致的起点。
        """
        return SimpleNamespace(**self.run_row)

    def stage_by_label(self, label: str) -> Optional[Dict[str, Any]]:
        for stage in self.stages:
            if str(stage.get("label") or "") == str(label):
                return stage
        return None

    def change_files(self) -> List[str]:
        return _as_str_list(self.inputs.get("change_files"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "sample_id": self.sample_id,
            "created_at": self.created_at,
            "redacted": bool(self.redacted),
            "notes": self.notes,
            "run_row": dict(self.run_row),
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "stages": [dict(item) for item in self.stages],
            "findings": [dict(item) for item in self.findings],
            "trace_rows": [dict(item) for item in self.trace_rows],
            "evidence_files": list(self.evidence_files),
            "coverage": dict(self.coverage),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "BenchmarkSample":
        if not isinstance(payload, dict):
            raise BenchmarkFormatError("样本必须是 JSON 对象")
        schema = str(payload.get("schema") or "")
        if schema != SAMPLE_SCHEMA:
            raise BenchmarkFormatError(
                f"样本 schema 不匹配：期望 {SAMPLE_SCHEMA}，实际 {schema!r}"
            )
        sample_id = str(payload.get("sample_id") or "").strip()
        if not sample_id:
            raise BenchmarkFormatError("样本缺少 sample_id")
        return cls(
            sample_id=sample_id,
            run_row=dict(payload.get("run_row") or {}),
            inputs=dict(payload.get("inputs") or {}),
            outputs=dict(payload.get("outputs") or {}),
            stages=[dict(item) for item in _as_list(payload.get("stages"))],
            findings=[dict(item) for item in _as_list(payload.get("findings"))],
            trace_rows=[dict(item) for item in _as_list(payload.get("trace_rows"))],
            evidence_files=_as_str_list(payload.get("evidence_files")),
            coverage=dict(payload.get("coverage") or {}),
            schema=schema,
            created_at=str(payload.get("created_at") or ""),
            redacted=bool(payload.get("redacted", True)),
            notes=str(payload.get("notes") or ""),
        )

    def save(self, path: str) -> str:
        _write_json(path, self.to_dict())
        return path

    @classmethod
    def load(cls, path: str) -> "BenchmarkSample":
        return cls.from_dict(_read_json(path))


# ---------------------------------------------------------------------------
#  金标
# ---------------------------------------------------------------------------

@dataclass
class GoldIssue:
    """一条人工标注的问题。"""

    issue_id: str
    title: str
    severity: str = "unknown"
    file_paths: List[str] = field(default_factory=list)
    commit_refs: List[str] = field(default_factory=list)
    note: str = ""
    # 标注时是否已确证（对应报告里的 `verification_state`）。它决定这条要不要
    # 进「误报」的分母：一条**明确标注为假设**的金标问题被模型报出来不算误报，
    # 但也不该算召回 —— 单独一列 `hypotheses` 统计，别混进准确率。
    verification_state: str = "confirmed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "title": self.title,
            "severity": self.severity,
            "file_paths": list(self.file_paths),
            "commit_refs": list(self.commit_refs),
            "note": self.note,
            "verification_state": self.verification_state,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "GoldIssue":
        if not isinstance(payload, dict):
            raise BenchmarkFormatError("金标问题必须是 JSON 对象")
        issue_id = str(payload.get("issue_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not issue_id:
            raise BenchmarkFormatError("金标问题缺少 issue_id")
        if not title:
            raise BenchmarkFormatError(f"金标问题 {issue_id} 缺少 title")
        severity = str(payload.get("severity") or "unknown").strip().lower()
        if severity not in GOLD_SEVERITIES:
            raise BenchmarkFormatError(
                f"金标问题 {issue_id} 的 severity={severity!r} 不在 {GOLD_SEVERITIES} 里"
            )
        return cls(
            issue_id=issue_id,
            title=title,
            severity=severity,
            file_paths=[normalize_path(p) for p in _as_str_list(payload.get("file_paths"))],
            commit_refs=_as_str_list(payload.get("commit_refs")),
            note=str(payload.get("note") or ""),
            verification_state=str(payload.get("verification_state") or "confirmed"),
        )


@dataclass
class GoldLabel:
    """一次运行的人工金标。"""

    sample_id: str
    issues: List[GoldIssue] = field(default_factory=list)
    # 本次变化的文件全集 —— **变化文件覆盖率的分母**。留空就是 `None`（未知），
    # 不是 0：分母未知时算出来的「0% 覆盖」是编的。
    change_files: List[str] = field(default_factory=list)
    # 变化文件的依赖闭包 —— **依赖闭包覆盖率的分母**。没有依赖图的项目就留空，
    # 那一项会报 `None`，而不是把「没算过」显示成「没覆盖」。
    dependency_closure: List[str] = field(default_factory=list)
    schema: str = GOLD_SCHEMA
    labeled_by: str = ""
    labeled_at: str = ""
    notes: str = ""

    def confirmed_issues(self) -> List[GoldIssue]:
        return [issue for issue in self.issues if issue.verification_state == "confirmed"]

    def hypothesis_issues(self) -> List[GoldIssue]:
        return [issue for issue in self.issues if issue.verification_state != "confirmed"]

    def validate(self) -> List[str]:
        problems: List[str] = []
        seen: Dict[str, int] = {}
        for issue in self.issues:
            seen[issue.issue_id] = seen.get(issue.issue_id, 0) + 1
        for issue_id, count in seen.items():
            if count > 1:
                problems.append(f"issue_id 重复 {count} 次：{issue_id}")
        for name, values in (("change_files", self.change_files),
                             ("dependency_closure", self.dependency_closure)):
            if len(values) != len(set(values)):
                problems.append(f"{name} 里有重复路径")
        missing = [p for p in self.change_files if p not in set(self.dependency_closure)] if self.dependency_closure else []
        if missing:
            problems.append(
                f"有 {len(missing)} 个变化文件不在依赖闭包里（闭包应当含变化文件本身）"
            )
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "sample_id": self.sample_id,
            "labeled_by": self.labeled_by,
            "labeled_at": self.labeled_at,
            "notes": self.notes,
            "issues": [issue.to_dict() for issue in self.issues],
            "change_files": list(self.change_files),
            "dependency_closure": list(self.dependency_closure),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "GoldLabel":
        if not isinstance(payload, dict):
            raise BenchmarkFormatError("金标必须是 JSON 对象")
        schema = str(payload.get("schema") or "")
        if schema != GOLD_SCHEMA:
            raise BenchmarkFormatError(
                f"金标 schema 不匹配：期望 {GOLD_SCHEMA}，实际 {schema!r}"
            )
        sample_id = str(payload.get("sample_id") or "").strip()
        if not sample_id:
            raise BenchmarkFormatError("金标缺少 sample_id")
        label = cls(
            sample_id=sample_id,
            issues=[GoldIssue.from_dict(item) for item in _as_list(payload.get("issues"))],
            change_files=[normalize_path(p) for p in _as_str_list(payload.get("change_files"))],
            dependency_closure=[
                normalize_path(p) for p in _as_str_list(payload.get("dependency_closure"))
            ],
            schema=schema,
            labeled_by=str(payload.get("labeled_by") or ""),
            labeled_at=str(payload.get("labeled_at") or ""),
            notes=str(payload.get("notes") or ""),
        )
        return label

    def save(self, path: str) -> str:
        _write_json(path, self.to_dict())
        return path

    @classmethod
    def load(cls, path: str) -> "GoldLabel":
        return cls.from_dict(_read_json(path))

    @classmethod
    def template(cls, sample_id: str, *, change_files: Iterable[str] = (),
                 issues: Iterable[Dict[str, Any]] = ()) -> "GoldLabel":
        """从样本自动生成一份**待填**的金标骨架。

        先把「本次变化的文件」填进去（那是机器知道的），人只补问题清单 ——
        让人从零写 change_files 是纯粹的浪费，而且写漏了覆盖率分母就错了。
        """
        return cls(
            sample_id=sample_id,
            issues=[GoldIssue.from_dict(item) for item in issues],
            change_files=[normalize_path(p) for p in change_files],
            dependency_closure=[],
            labeled_by="",
            labeled_at="",
            notes="TODO: 人工补 issues / dependency_closure",
        )


# ---------------------------------------------------------------------------
#  数据集
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkDataset:
    """一个固定评测集：样本 + 金标 + 脱敏映射。"""

    root: str = ""
    samples: Dict[str, BenchmarkSample] = field(default_factory=dict)
    golds: Dict[str, GoldLabel] = field(default_factory=dict)

    def paired_ids(self) -> List[str]:
        """样本与金标都在的 sample_id（**排序**，保证两次跑比较的样本顺序一致）。"""
        return sorted(set(self.samples) & set(self.golds))

    def unpaired(self) -> Dict[str, List[str]]:
        """配不上的那些（要么缺样本、要么缺金标）。要报出来，不能静默取交集。"""
        return {
            "missing_gold": sorted(set(self.samples) - set(self.golds)),
            "missing_sample": sorted(set(self.golds) - set(self.samples)),
        }

    def load_warnings(self) -> List[str]:
        warnings: List[str] = []
        gaps = self.unpaired()
        if gaps["missing_gold"]:
            warnings.append(
                f"{len(gaps['missing_gold'])} 个样本没有金标，会被排除在指标之外："
                f"{gaps['missing_gold'][:5]}"
            )
        if gaps["missing_sample"]:
            warnings.append(
                f"{len(gaps['missing_sample'])} 份金标没有样本：{gaps['missing_sample'][:5]}"
            )
        for sample_id in self.paired_ids():
            problems = self.golds[sample_id].validate()
            for problem in problems:
                warnings.append(f"[{sample_id}] {problem}")
        return warnings


def load_dataset(root: str) -> BenchmarkDataset:
    """加载 `<root>/samples/*.json` 与 `<root>/gold/*.json`。"""
    dataset = BenchmarkDataset(root=root)
    samples_dir = os.path.join(root, "samples")
    gold_dir = os.path.join(root, "gold")
    for path in _sorted_json_files(samples_dir):
        sample = BenchmarkSample.load(path)
        dataset.samples[sample.sample_id] = sample
    for path in _sorted_json_files(gold_dir):
        gold = GoldLabel.load(path)
        dataset.golds[gold.sample_id] = gold
    return dataset


def _sorted_json_files(directory: str) -> List[str]:
    if not os.path.isdir(directory):
        return []
    names = [name for name in os.listdir(directory) if name.endswith(".json")]
    return [os.path.join(directory, name) for name in sorted(names)]


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
