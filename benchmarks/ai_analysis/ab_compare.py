#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 比较：同一评测集上，两份结果的**准确率与成本差异**。

用法（也就是「一次优化到底有没有效」的那三步里的第三步）：

    python -m benchmarks.ai_analysis.ab_compare \\
        --baseline  benchmarks/ai_analysis/data/run20 \\
        --candidate benchmarks/ai_analysis/data/run21 \\
        --out benchmarks/ai_analysis/data/run20_vs_run21.json

## 这个脚本的立场：小样本上不许下结论

这是整个任务 H 存在的理由。复测文档 5.1 里，Run 15 → Run 20 的总 token 降了 6.8%，
看起来是一次成功的优化 —— 但同一份数据里严格证据覆盖从 5.92% 掉到 4.2%、耗时还涨了
8.4%、复核覆盖率继续下降。**只看一个数字一定得出错误结论**，而「看全部数字」还不够：
一次运行只有十几条结论，12 条金标里漏 1 条就是 8 个百分点的召回率波动，这个波动完全
可以用噪声解释。

所以：

* 样本量（**配对样本数**，不是运行次数）少于 `MIN_PAIRS_FOR_CLAIM` 时，
  一律不下「提升/下降」的结论，只报数字与区间，并明确写出「样本不足」；
* 即使样本够，也只有当**配对差值的自助法 95% 区间不含 0** 时才说「变化」；
* 召回率另有一道**非劣性边界**：召回率下降的区间上界不超过 `RECALL_TOLERANCE`
  才算「召回率没变差」—— 因为在金标上「没有显著下降」与「没有有意义的下降」
  是两件事，而任务 E 的验收要的是后者。

## 成本口径

金额读 `amount_exact`（未舍入）相加；`money()` 的产物里有 `"<0.01"` 这种文本，
`Decimal("<0.01")` 会抛异常。没有价格表的样本计入 `unpriced`，**不折成 0**：
「这次没配价格」与「这次花了 0 元」混起来，成本差异就成了编的。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from benchmarks.ai_analysis.dataset import load_dataset
from benchmarks.ai_analysis.metrics import (
    EVIDENCE_SCOPE_LEDGER_BY_PAIR,
    EVIDENCE_SCOPE_TRACE_DETAIL,
    EVIDENCE_SCOPES,
    evaluate_dataset,
)

# 少于这么多**配对样本**，不下结论。20 是刻意的偏低值：金标本身很贵（人工标注
# 一次运行的全部结论），定到 50 会让这条线永远跨不过去，于是规则被绕过。
MIN_PAIRS_FOR_CLAIM = 20
# 自助法重采样次数与固定种子：比较结果必须可复现，否则同一个问题会有两个答案。
BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20260921
# 召回率的非劣性边界（绝对值，百分点的小数形式）：下降不超过 2 个百分点
# 才算「召回率没变差」。0 会让「不显著」被当成「没变差」。
RECALL_TOLERANCE = 0.02

# 各指标的方向。`higher_is_better=False` 的那些，数值下降才是好事。
#
# 证据覆盖那两项**按口径各占一个指标名**（`evidence_coverage:<口径>`），不是
# 「一个覆盖率」：同一份结果上逐轮明细口径是 42/1009、台账保守口径是 32/1009，
# 两个数都真。名字里不带口径，跨版本比较就会拿 A 的 42 去比 B 的 32 而毫无察觉 ——
# 那不是数字难看，是结论反了（见 `sample_metric` 里那条 KeyError）。
EVIDENCE_METRIC_PREFIX = "evidence_coverage:"

METRIC_DIRECTIONS: Dict[str, bool] = {
    "recall": True,
    "false_positive_rate": False,
    "severity_exact_agreement": True,
    "evidence_locatable": True,
    "changed_file_coverage": True,
    "dependency_closure_coverage": True,
    f"{EVIDENCE_METRIC_PREFIX}{EVIDENCE_SCOPE_TRACE_DETAIL}": True,
    f"{EVIDENCE_METRIC_PREFIX}{EVIDENCE_SCOPE_LEDGER_BY_PAIR}": True,
    "tokens_total": False,
    "cost_per_sample": False,
    "duration_ms": False,
}


def _percentile(values: List[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def paired_delta(deltas: Sequence[float], *, rounds: int = BOOTSTRAP_ROUNDS,
                 seed: int = BOOTSTRAP_SEED) -> Dict[str, Any]:
    """配对差值的均值 + 自助法 95% 区间。

    用自助法而不是 t 检验：这些指标（比例、计数）明显不正态，而 t 检验在小样本上
    会给出过于乐观的区间 —— 那正是这个脚本要防的错误方向。
    """
    values = [float(value) for value in deltas]
    if not values:
        return {
            "n": 0, "mean": None, "ci_low": None, "ci_high": None,
            "ci_excludes_zero": False, "reason": "没有可配对的样本（两侧都有值的那些）",
        }
    rng = random.Random(seed)
    mean = sum(values) / len(values)
    if len(values) == 1:
        return {
            "n": 1, "mean": round(mean, 6), "ci_low": None, "ci_high": None,
            "ci_excludes_zero": False,
            "reason": "只有一个配对样本，算不出区间（不是「区间很宽」，是算不出来）",
        }
    means: List[float] = []
    count = len(values)
    for _ in range(rounds):
        sample = [values[rng.randrange(count)] for _ in range(count)]
        means.append(sum(sample) / count)
    ci_low = _percentile(means, 0.025)
    ci_high = _percentile(means, 0.975)
    return {
        "n": count,
        "mean": round(mean, 6),
        "ci_low": round(ci_low, 6),
        "ci_high": round(ci_high, 6),
        "ci_excludes_zero": (ci_low > 0 and ci_high > 0) or (ci_low < 0 and ci_high < 0),
        "reason": "",
    }


# ---------------------------------------------------------------------------
#  逐样本指标抽取
# ---------------------------------------------------------------------------

def sample_metric(result: Dict[str, Any], metric: str) -> Optional[float]:
    """从一条逐样本结果里取出可比数值。取不到就 `None`（**不补 0**）。"""
    if metric == "recall":
        return result["recall"]["overall"]
    if metric == "false_positive_rate":
        return result["false_positive"]["rate"]
    if metric == "severity_exact_agreement":
        return result["severity"]["exact_agreement"]
    if metric == "evidence_locatable":
        return result["evidence_locatable"]["rate"]
    if metric == "changed_file_coverage":
        return result["changed_file_coverage"]["ratio"]
    if metric == "dependency_closure_coverage":
        return result["dependency_closure_coverage"]["ratio"]
    if metric.startswith(EVIDENCE_METRIC_PREFIX):
        scope = metric[len(EVIDENCE_METRIC_PREFIX):]
        if scope not in EVIDENCE_SCOPES:
            # 指标名是代码里写死的，不是数据。写错口径就当场炸掉，别退化成
            # 「这一项没有可比样本」——那看起来像样本不足，实际是口径写错了。
            raise KeyError(f"未知的证据覆盖口径：{scope!r}（可用：{list(EVIDENCE_SCOPES)}）")
        for row in (result.get("evidence_coverage") or {}).get("scopes") or []:
            if row.get("scope") == scope:
                return row.get("ratio")
        # 这条结果里没有这一层（例如样本没带台账）→ `None`，计入 `samples_missing`。
        # 这里**不能**退回另一口径的数：那正是这一整套口径名要防的事。
        return None
    if metric == "tokens_total":
        tokens = result.get("tokens") or {}
        if tokens.get("input") is None and tokens.get("output") is None:
            return None
        return float((tokens.get("input") or 0) + (tokens.get("output") or 0))
    if metric == "cost_per_sample":
        raw = (result.get("cost") or {}).get("amount_exact")
        if raw is None:
            return None
        try:
            return float(Decimal(str(raw)))
        except Exception:
            return None
    if metric == "duration_ms":
        value = result.get("duration_ms")
        return float(value) if value is not None else None
    raise KeyError(f"未知指标：{metric}")


def _metric_block(
    metric: str,
    baseline_by_id: Dict[str, Dict[str, Any]],
    candidate_by_id: Dict[str, Dict[str, Any]],
    shared_ids: Sequence[str],
) -> Dict[str, Any]:
    deltas: List[float] = []
    pairs: List[Dict[str, Any]] = []
    missing = 0
    for sample_id in shared_ids:
        left = sample_metric(baseline_by_id[sample_id], metric)
        right = sample_metric(candidate_by_id[sample_id], metric)
        if left is None or right is None:
            missing += 1
            continue
        deltas.append(right - left)
        pairs.append({"sample_id": sample_id, "baseline": left, "candidate": right})
    block = paired_delta(deltas)
    block.update({
        "metric": metric,
        "higher_is_better": METRIC_DIRECTIONS[metric],
        "samples_missing": missing,
        "pairs": pairs,
    })
    return block


# ---------------------------------------------------------------------------
#  主比较
# ---------------------------------------------------------------------------

def compare_results(
    baseline_results: Sequence[Dict[str, Any]],
    candidate_results: Sequence[Dict[str, Any]],
    *,
    baseline_summary: Optional[Dict[str, Any]] = None,
    candidate_summary: Optional[Dict[str, Any]] = None,
    min_pairs: int = MIN_PAIRS_FOR_CLAIM,
    recall_tolerance: float = RECALL_TOLERANCE,
    label_baseline: str = "baseline",
    label_candidate: str = "candidate",
) -> Dict[str, Any]:
    """在同一批 sample_id 上比较两份结果。"""
    baseline_by_id = {item["sample_id"]: item for item in baseline_results}
    candidate_by_id = {item["sample_id"]: item for item in candidate_results}
    shared = sorted(set(baseline_by_id) & set(candidate_by_id))
    only_baseline = sorted(set(baseline_by_id) - set(candidate_by_id))
    only_candidate = sorted(set(candidate_by_id) - set(baseline_by_id))

    metrics = {metric: _metric_block(metric, baseline_by_id, candidate_by_id, shared)
               for metric in METRIC_DIRECTIONS}

    warnings: List[str] = []
    if only_baseline:
        warnings.append(f"{len(only_baseline)} 个样本只在 {label_baseline} 里：{only_baseline[:5]}")
    if only_candidate:
        warnings.append(f"{len(only_candidate)} 个样本只在 {label_candidate} 里：{only_candidate[:5]}")

    pair_count = len(shared)
    enough = pair_count >= min_pairs
    claims: List[Dict[str, Any]] = []
    verdict = "insufficient_samples"

    if not enough:
        warnings.append(
            f"配对样本只有 {pair_count} 个（少于 {min_pairs} 个）：**不下任何「提升/下降」的结论**。"
            "这个规模上，一次运行的结论少报一条就是几个到十几个百分点的波动，"
            "而那个波动用噪声完全解释得通。要么补样本，要么只看方向不看幅度。"
        )
    else:
        recall = metrics["recall"]
        if recall["n"] == 0:
            verdict = "no_comparable_samples"
            warnings.append("召回率一个可配对的样本都没有，无法判断优化是否安全。")
        elif recall["ci_low"] is not None and recall["ci_low"] < -recall_tolerance:
            verdict = "recall_regression"
            claims.append({
                "kind": "recall_regression",
                "severity": "blocker",
                "text": (
                    f"召回率的下降区间下界是 {recall['ci_low']:.4f}，超过非劣性边界 "
                    f"{-recall_tolerance:.4f}：这次改动**可能真的漏报了**，不能只按 token 收益放行。"
                ),
            })
        else:
            non_inferior = (
                recall["ci_low"] is None
                or recall["ci_low"] >= -recall_tolerance
            )
            if non_inferior:
                verdict = "recall_not_worse"
                claims.append({
                    "kind": "recall_non_inferior",
                    "severity": "info",
                    "text": (
                        f"召回率没有变差（配对差均值 {recall['mean']:+.4f}，下界 "
                        f"{recall['ci_low'] if recall['ci_low'] is None else format(recall['ci_low'], '.4f')}，"
                        f"非劣性边界 {-recall_tolerance:.4f}）。"
                    ),
                })
                cost = metrics["cost_per_sample"]
                if cost["ci_excludes_zero"]:
                    cheaper = cost["mean"] < 0
                    claims.append({
                        "kind": "cost_change",
                        "severity": "info",
                        "text": (
                            f"单样本费用{'下降' if cheaper else '上升'} "
                            f"{abs(cost['mean']):.6f}（区间 [{cost['ci_low']:.6f}, {cost['ci_high']:.6f}]，不含 0）。"
                        ),
                    })
                for name in ("false_positive_rate", "evidence_locatable",
                             "changed_file_coverage", "dependency_closure_coverage",
                             "severity_exact_agreement"):
                    block = metrics[name]
                    if not block["ci_excludes_zero"]:
                        continue
                    better = (block["mean"] > 0) == block["higher_is_better"]
                    claims.append({
                        "kind": f"{name}_{'better' if better else 'worse'}",
                        "severity": "info" if better else "warn",
                        "text": (
                            f"{name} {'改善' if better else '退化'} {abs(block['mean']):.4f}"
                            f"（区间 [{block['ci_low']:.4f}, {block['ci_high']:.4f}]，不含 0）"
                        ),
                    })

    caveats = [
        "配对差值的置信区间只回答「这点数据支不支持说变了」，不回答「这个变化值不值」。",
        f"结论门槛：配对样本 ≥ {min_pairs} 条；召回率另有非劣性边界 {recall_tolerance} "
        "（下降区间下界不得低于它）。",
        "样本之间不独立（同一次运行的结论会互相影响，同一仓库的两次运行高度相关），"
        "所以真实的不确定性比这里算出来的更大 —— 区间只当作下限看。",
    ]
    if metrics["cost_per_sample"]["samples_missing"]:
        caveats.append(
            f"有 {metrics['cost_per_sample']['samples_missing']} 个样本算不出费用"
            "（没配价格表），它们**不参与**成本差异，也没有被当成 0。"
        )
    if metrics["dependency_closure_coverage"]["n"] == 0:
        caveats.append(
            "依赖闭包覆盖率没有可比样本（金标没给闭包清单，或本次没采集证据明细）——"
            "这一项现在是空的，不是 0%。"
        )

    return {
        "schema": "ai_benchmark_ab/v1",
        "baseline": label_baseline,
        "candidate": label_candidate,
        "paired_samples": pair_count,
        "only_baseline": only_baseline,
        "only_candidate": only_candidate,
        "min_pairs_for_claim": min_pairs,
        "recall_tolerance": recall_tolerance,
        "verdict": verdict,
        "claims": claims,
        "caveats": caveats,
        "warnings": warnings,
        "metrics": metrics,
        "totals": {
            "baseline": _totals(baseline_summary),
            "candidate": _totals(candidate_summary),
        },
    }


def _totals(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not summary:
        return {}
    return {
        "samples": summary.get("samples"),
        "tokens": summary.get("tokens"),
        "cost": summary.get("cost"),
        "duration_ms": summary.get("duration_ms"),
        "recall": summary.get("recall"),
        "false_positive": summary.get("false_positive"),
        "evidence_locatable": summary.get("evidence_locatable"),
        "changed_file_coverage": summary.get("changed_file_coverage"),
        "dependency_closure_coverage": summary.get("dependency_closure_coverage"),
    }


# ---------------------------------------------------------------------------
#  人读输出
# ---------------------------------------------------------------------------

def render_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"A/B 比较：{report['baseline']} → {report['candidate']}")
    lines.append(f"配对样本：{report['paired_samples']} 个（门槛 {report['min_pairs_for_claim']}）")
    lines.append(f"结论：{report['verdict']}")
    lines.append("")
    lines.append("指标（差值 = candidate - baseline）：")
    for metric, block in report["metrics"].items():
        direction = "越高越好" if block["higher_is_better"] else "越低越好"
        if block["mean"] is None:
            lines.append(f"  {metric:<32} 无可比样本（{block.get('reason') or '两侧都有值的样本为 0'}）")
            continue
        ci = (
            "区间不可算" if block["ci_low"] is None
            else f"[{block['ci_low']:+.4f}, {block['ci_high']:+.4f}]"
        )
        mark = "变化" if block["ci_excludes_zero"] else "不显著"
        lines.append(
            f"  {metric:<32} {block['mean']:+.4f}  {ci}  n={block['n']}  {direction}  {mark}"
        )
    lines.append("")
    totals = report["totals"]
    if totals.get("baseline") or totals.get("candidate"):
        for key in ("tokens", "cost", "duration_ms"):
            left = (totals.get("baseline") or {}).get(key)
            right = (totals.get("candidate") or {}).get(key)
            lines.append(f"  合计 {key}: baseline={left} candidate={right}")
        lines.append("")
    if report["claims"]:
        lines.append("结论性判断：")
        for claim in report["claims"]:
            lines.append(f"  [{claim['severity']}] {claim['text']}")
        lines.append("")
    if report["warnings"]:
        lines.append("告诫：")
        for warning in report["warnings"]:
            lines.append(f"  ⚠️ {warning}")
        lines.append("")
    if report["caveats"]:
        lines.append("口径与限制：")
        for caveat in report["caveats"]:
            lines.append(f"  · {caveat}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def compare_datasets(baseline_dir: str, candidate_dir: str, *, price_table: Any = None,
                     min_pairs: int = MIN_PAIRS_FOR_CLAIM) -> Dict[str, Any]:
    baseline = load_dataset(baseline_dir)
    candidate = load_dataset(candidate_dir)
    for name, dataset in (("baseline", baseline), ("candidate", candidate)):
        for warning in dataset.load_warnings():
            sys.stderr.write(f"[{name}] {warning}\n")
    baseline_results, baseline_summary = evaluate_dataset(baseline, price_table=price_table)
    candidate_results, candidate_summary = evaluate_dataset(candidate, price_table=price_table)
    return compare_results(
        baseline_results, candidate_results,
        baseline_summary=baseline_summary, candidate_summary=candidate_summary,
        min_pairs=min_pairs,
        label_baseline=os.path.basename(os.path.normpath(baseline_dir)),
        label_candidate=os.path.basename(os.path.normpath(candidate_dir)),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="同一评测集上比较两份 AI 分析结果")
    parser.add_argument("--baseline", required=True, help="基线数据集目录")
    parser.add_argument("--candidate", required=True, help="候选数据集目录")
    parser.add_argument("--out", default="", help="把机器可读结果写到这个 JSON 文件")
    parser.add_argument("--min-pairs", type=int, default=MIN_PAIRS_FOR_CLAIM,
                       help=f"下结论所需的最少配对样本数（默认 {MIN_PAIRS_FOR_CLAIM}）")
    parser.add_argument("--price-table", default="",
                       help="价格表 JSON（不传则费用列为 None，不伪造金额）")
    args = parser.parse_args(argv)

    price_table = None
    if args.price_table:
        from services.ai.pricing import load_price_table

        price_table, errors = load_price_table(args.price_table)
        for error in errors:
            sys.stderr.write(f"[price-table] {error}\n")
        if price_table is None:
            sys.stderr.write("[price-table] 解析失败，费用列会是 None（不伪造金额）\n")

    report = compare_datasets(args.baseline, args.candidate,
                              price_table=price_table, min_pairs=args.min_pairs)
    print(render_text(report))
    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"\n机器可读结果已写入：{args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
