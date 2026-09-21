# -*- coding: utf-8 -*-
"""任务 H：评测基线的四条底线。

这一层交付的是**度量能力**（不是 token 优化），所以用例盯的是「这把尺子准不准」：

1. 金标格式**往返**不丢信息、坏金标要在加载时就报错（不是算出一个错数字）；
2. 指标在**构造样本**上的已知值对得上（每一项的分母都摆明着算一遍）；
3. A/B 脚本在**小样本**上拒绝下结论（这是这一层存在的理由：Run 15→20 的
   token 降了 6.8%，但覆盖率从 5.92% 掉到 4.2%，只看一个数字必然判错）；
4. 脱敏**稳定**（同一路径永远同一假名，否则两次跑之间做差与覆盖率都对不上）
   且**默认开启**。

口径上跟着平台既有实现走，用例也钉住这些口径：`None` = 未上报（不是 0）、
比例分母缺失时是 `None`（不是 0%）、费用算不出时是 `None`（不伪造金额）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from benchmarks.ai_analysis import dataset as dataset_module  # noqa: E402
from benchmarks.ai_analysis import metrics as metrics_module  # noqa: E402
from benchmarks.ai_analysis.ab_compare import (  # noqa: E402
    MIN_PAIRS_FOR_CLAIM,
    compare_results,
    render_text,
)
from benchmarks.ai_analysis.dataset import (  # noqa: E402
    GOLD_SCHEMA,
    SAMPLE_SCHEMA,
    BenchmarkSample,
    GoldIssue,
    GoldLabel,
    load_dataset,
)

# ---------------------------------------------------------------------------
#  构造样本
# ---------------------------------------------------------------------------

def _finding(title, severity, file_paths, evidence, finding_id="F1"):
    return {
        "finding_id": finding_id,
        "title": title,
        "severity": severity,
        "file_paths": list(file_paths),
        "evidence": list(evidence),
    }


def _sample(sample_id="run20", *, findings=(), change_files=(), evidence_files=(),
            tokens=(100, 20), duration_ms=1000, trace_rows=None, cost_exact=None,
            dependency_closure=()):
    run_row = {
        "id": 20,
        "tokens_input": tokens[0],
        "tokens_output": tokens[1],
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "duration_ms": duration_ms,
        "model": "",
        "pricing_version": "",
        "rounds_used": 26,
        "tool_requests_used": 131,
        "context_chars": None,
        "tool_stats_json": None,
        "response_payload": None,
        "response_text": "",
    }
    return BenchmarkSample(
        sample_id=sample_id,
        run_row=run_row,
        inputs={
            "change_files": list(change_files),
            "evidence_detail_collected": True,
            "dependency_closure": list(dependency_closure),
        },
        outputs={},
        findings=list(findings),
        trace_rows=list(trace_rows or []),
        evidence_files=list(evidence_files),
        coverage={},
    )


# ---------------------------------------------------------------------------
#  1. 金标格式往返
# ---------------------------------------------------------------------------

class TestGoldRoundTrip:
    def test_a_gold_label_survives_a_write_read_cycle(self, tmp_path):
        gold = GoldLabel(
            sample_id="run20",
            issues=[
                GoldIssue(issue_id="G1", title="空每日上限导致不限次", severity="critical",
                          file_paths=["config/daily.xlsx"], commit_refs=["abc1234"],
                          note="消费者把空值当不限次"),
                GoldIssue(issue_id="G2", title="协议编号位移", severity="high",
                          file_paths=["proto/battle.proto"], verification_state="hypothesis"),
            ],
            change_files=["config/daily.xlsx", "proto/battle.proto"],
            dependency_closure=["config/daily.xlsx", "proto/battle.proto", "config/common.xlsx"],
            labeled_by="qa",
        )
        path = gold.save(str(tmp_path / "gold" / "run20.json"))

        loaded = GoldLabel.load(path)

        assert loaded.to_dict() == gold.to_dict()
        assert loaded.schema == GOLD_SCHEMA
        assert loaded.confirmed_issues()[0].title == "空每日上限导致不限次"
        assert len(loaded.hypothesis_issues()) == 1
        assert loaded.validate() == []

    def test_a_sample_survives_a_write_read_cycle(self, tmp_path):
        sample = _sample(
            findings=[_finding("空每日上限", "critical", ["config/daily.xlsx"],
                               ["config/daily.xlsx:12 上限为空"])],
            change_files=["config/daily.xlsx"],
            evidence_files=["config/daily.xlsx"],
        )
        path = sample.save(str(tmp_path / "samples" / "run20.json"))

        loaded = BenchmarkSample.load(path)

        assert loaded.to_dict() == sample.to_dict()
        assert loaded.schema == SAMPLE_SCHEMA
        assert loaded.findings[0]["title"] == "空每日上限"

    def test_an_unknown_schema_is_rejected_at_load(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema": "something/v9", "sample_id": "x"}), encoding="utf-8")

        with pytest.raises(dataset_module.BenchmarkFormatError, match="schema"):
            BenchmarkSample.load(str(path))

    def test_a_duplicate_issue_id_is_reported(self):
        gold = GoldLabel(
            sample_id="run20",
            issues=[
                GoldIssue(issue_id="G1", title="一", severity="high"),
                GoldIssue(issue_id="G1", title="二", severity="low"),
            ],
        )
        problems = gold.validate()
        assert any("重复" in problem for problem in problems), problems

    def test_an_unknown_severity_is_rejected(self):
        with pytest.raises(dataset_module.BenchmarkFormatError, match="severity"):
            GoldIssue.from_dict({"issue_id": "G1", "title": "x", "severity": "很高"})

    def test_loading_a_dataset_reports_unpaired_entries_instead_of_intersecting(self, tmp_path):
        """缺金标 / 缺样本都要**报出来**。

        静默取交集会让「改了 10 条标注只生效 3 条」看起来像没改。
        """
        root = tmp_path / "data"
        _sample(sample_id="run20").save(str(root / "samples" / "run20.json"))
        _sample(sample_id="run21").save(str(root / "samples" / "run21.json"))
        GoldLabel(sample_id="run20", issues=[GoldIssue("G1", "x", "high")]).save(
            str(root / "gold" / "run20.json")
        )
        GoldLabel(sample_id="run22", issues=[]).save(str(root / "gold" / "run22.json"))

        loaded = load_dataset(str(root))

        assert loaded.paired_ids() == ["run20"]
        gaps = loaded.unpaired()
        assert gaps["missing_gold"] == ["run21"]
        assert gaps["missing_sample"] == ["run22"]
        warnings = loaded.load_warnings()
        assert any("run21" in warning for warning in warnings), warnings
        assert any("run22" in warning for warning in warnings), warnings


# ---------------------------------------------------------------------------
#  2. 指标在构造样本上的已知值
# ---------------------------------------------------------------------------

class TestMetricsHaveKnownValues:
    def _evaluate(self, sample, gold):
        return metrics_module.evaluate_sample(sample, gold)

    def test_recall_and_false_positive_rate_on_a_hand_counted_sample(self):
        """2 条金标、3 条报告：命中 1 条，另 2 条是误报。

        手算：召回率 1/2 = 0.5；误报率 2/3 ≈ 0.6667。
        """
        gold = GoldLabel(
            sample_id="run20",
            issues=[
                GoldIssue("G1", "每日上限为空", "critical", file_paths=["config/daily.xlsx"]),
                GoldIssue("G2", "协议编号位移", "high", file_paths=["proto/battle.proto"]),
            ],
            change_files=["config/daily.xlsx", "proto/battle.proto"],
            dependency_closure=["config/daily.xlsx", "proto/battle.proto"],
        )
        sample = _sample(
            findings=[
                _finding("每日上限为空", "critical", ["config/daily.xlsx"],
                         ["config/daily.xlsx:12 上限写的是空"], "F1"),
                _finding("掉落表概率异常", "medium", ["config/drop.xlsx"],
                         ["config/drop.xlsx:88 概率 3.5"], "F2"),
                _finding("某个表缺少主键", "low", ["config/other.xlsx"], ["无坐标描述"], "F3"),
            ],
            change_files=["config/daily.xlsx", "proto/battle.proto"],
            evidence_files=["config/daily.xlsx", "config/drop.xlsx"],
        )

        result = self._evaluate(sample, gold)

        assert result["recall"]["matched"] == 1
        assert result["recall"]["total"] == 2
        assert result["recall"]["overall"] == 0.5
        assert result["false_positive"]["count"] == 2
        assert result["false_positive"]["total"] == 3
        assert result["false_positive"]["rate"] == round(2 / 3, 6)
        assert [item["gold_issue_id"] for item in result["matching"]] == ["G1"]
        assert [item["gold_issue_id"] for item in result["missed_gold"]] == ["G2"]

    def test_severity_agreement_is_counted_only_over_matched_pairs(self):
        """严重度一致性只在对上的那些里算 —— 没对上的没有「一致性」可言。"""
        gold = GoldLabel(
            sample_id="run20",
            issues=[
                GoldIssue("G1", "每日上限为空", "critical", file_paths=["config/daily.xlsx"]),
                GoldIssue("G2", "协议编号位移", "high", file_paths=["proto/battle.proto"]),
            ],
            change_files=["config/daily.xlsx", "proto/battle.proto"],
        )
        sample = _sample(
            findings=[
                # 对上了，但严重度降了一档（critical → high）
                _finding("每日上限为空", "high", ["config/daily.xlsx"],
                         ["config/daily.xlsx:12"], "F1"),
            ],
            change_files=["config/daily.xlsx", "proto/battle.proto"],
        )

        result = self._evaluate(sample, gold)

        assert result["severity"]["matched"] == 1
        assert result["severity"]["exact_agreement"] == 0.0
        assert result["severity"]["mean_level_delta"] == 1.0
        assert result["severity"]["confusion"] == {"critical": {"high": 1}}

    def test_evidence_locatability_needs_both_a_known_path_and_a_coordinate(self):
        """「提到了文件名」不算可定位 —— 必须**同时**有清单内的路径和坐标。"""
        gold = GoldLabel(
            sample_id="run20",
            issues=[GoldIssue("G1", "每日上限为空", "critical", file_paths=["config/daily.xlsx"])],
            change_files=["config/daily.xlsx"],
            dependency_closure=["config/daily.xlsx"],
        )
        sample = _sample(
            findings=[
                _finding("每日上限为空", "critical", ["config/daily.xlsx"],
                         ["config/daily.xlsx:12 上限为空"], "F1"),
                # 有路径没坐标
                _finding("某表概率异常", "high", ["config/daily.xlsx"],
                         ["config/daily.xlsx 里概率看着不对"], "F2"),
                # 有坐标但路径不在本次清单里
                _finding("别的表有问题", "high", ["config/other.xlsx"],
                         ["config/other.xlsx:9 有问题"], "F3"),
                # 提交短 SHA 也算坐标
                _finding("代码流程问题", "medium", ["config/daily.xlsx"],
                         ["见提交 deadbeef1234"], "F4"),
            ],
            change_files=["config/daily.xlsx"],
            evidence_files=["config/daily.xlsx"],
        )

        result = self._evaluate(sample, gold)

        assert result["evidence_locatable"]["count"] == 2
        assert result["evidence_locatable"]["total"] == 4
        assert result["evidence_locatable"]["rate"] == 0.5
        reasons = {item["finding_id"]: item["reason"] for item in result["evidence_locatable"]["unlocatable"]}
        assert "坐标" in reasons["F2"]
        assert "清单" in reasons["F3"]

    def test_changed_file_coverage_uses_the_gold_change_set(self):
        gold = GoldLabel(
            sample_id="run20",
            issues=[],
            change_files=["a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx"],
        )
        sample = _sample(
            findings=[_finding("x", "high", ["a.xlsx"], ["a.xlsx:1"])],
            change_files=["a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx"],
            evidence_files=["c.xlsx"],
        )

        result = self._evaluate(sample, gold)

        assert result["changed_file_coverage"]["covered"] == 2  # a.xlsx 报过，c.xlsx 看过
        assert result["changed_file_coverage"]["total"] == 4
        assert result["changed_file_coverage"]["ratio"] == 0.5
        assert result["changed_file_coverage"]["source"] == "gold"

    def test_dependency_closure_coverage_is_none_without_a_closure(self):
        """没有依赖图的项目**报 None**，不报 0% —— 「没算过」不是「没覆盖」。"""
        gold = GoldLabel(sample_id="run20", issues=[], change_files=["a.xlsx"])
        sample = _sample(change_files=["a.xlsx"], evidence_files=["a.xlsx"])

        result = self._evaluate(sample, gold)

        assert result["dependency_closure_coverage"]["ratio"] is None
        assert "依赖闭包" in result["dependency_closure_coverage"]["reason"]

    def test_dependency_closure_coverage_counts_only_files_with_evidence(self):
        gold = GoldLabel(
            sample_id="run20",
            issues=[],
            change_files=["a.xlsx"],
            dependency_closure=["a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx"],
        )
        sample = _sample(change_files=["a.xlsx"], evidence_files=["a.xlsx", "b.xlsx"])

        result = self._evaluate(sample, gold)

        assert result["dependency_closure_coverage"]["covered"] == 2
        assert result["dependency_closure_coverage"]["total"] == 4
        assert result["dependency_closure_coverage"]["ratio"] == 0.5

    def test_coverage_is_unknown_when_no_evidence_detail_was_collected(self):
        """「没采集过明细」→ 未知。混成 0 会让「这次没记」显示成「一个文件都没看」。"""
        gold = GoldLabel(
            sample_id="run20", issues=[], change_files=["a.xlsx"],
            dependency_closure=["a.xlsx", "b.xlsx"],
        )
        sample = _sample(change_files=["a.xlsx"], evidence_files=[])
        sample.inputs["evidence_detail_collected"] = False

        result = self._evaluate(sample, gold)

        assert result["dependency_closure_coverage"]["ratio"] is None
        assert result["changed_file_coverage"]["ratio"] == 0.0  # 报过 0 个（这次报了 0 条）

    def test_tokens_and_duration_come_from_the_run_row(self):
        gold = GoldLabel(sample_id="run20", issues=[], change_files=["a.xlsx"])
        sample = _sample(change_files=["a.xlsx"], tokens=(1722027, 277806), duration_ms=1595400)

        result = self._evaluate(sample, gold)

        assert result["tokens"]["input"] == 1722027
        assert result["tokens"]["output"] == 277806
        assert result["tokens"]["total"] == 1999833
        assert result["duration_ms"] == 1595400

    def test_cost_is_none_without_a_price_table(self):
        """价格表是空的（`DEFAULT_PRICE_TABLE` 就是这样）→ 费用为 None，不伪造金额。"""
        from services.ai.pricing import default_price_table

        gold = GoldLabel(sample_id="run20", issues=[], change_files=["a.xlsx"])
        sample = _sample(change_files=["a.xlsx"], tokens=(1_000_000, 100_000))

        result = self._evaluate_with_table(sample, gold, default_price_table())

        assert result["cost"]["amount_exact"] is None
        assert result["cost"]["amount"] is None
        assert result["cost"]["reason"]

    def test_cost_is_computed_when_the_price_table_matches(self):
        """配了价格表就要算得出来，而且读的是 `amount_exact`（未舍入）。"""
        from services.ai.pricing import ModelPrice, PriceTable

        table = PriceTable(
            models={"demo-model": ModelPrice(
                input_per_million=__import__("decimal").Decimal("2"),
                output_per_million=__import__("decimal").Decimal("8"),
                cache_read_per_million=__import__("decimal").Decimal("0.2"),
            )},
            version="test-v1",
        )
        gold = GoldLabel(sample_id="run20", issues=[], change_files=["a.xlsx"])
        sample = _sample(change_files=["a.xlsx"], tokens=(1_000_000, 100_000))
        sample.run_row["model"] = "demo-model"
        sample.run_row["cache_read_tokens"] = 0

        result = self._evaluate_with_table(sample, gold, table)

        # 2 元/M × 1M 输入 + 8 元/M × 0.1M 输出 = 2 + 0.8 = 2.8
        # 比较用 Decimal：`amount_exact` 是**未舍入**文本（2.8 而不是 2.80），
        # 按字面比会挂在一件与金额无关的格式差异上。
        assert Decimal(result["cost"]["amount_exact"]) == Decimal("2.80")
        assert result["cost"]["currency"] == "CNY"

    @staticmethod
    def _evaluate_with_table(sample, gold, table):
        return metrics_module.evaluate_sample(sample, gold, price_table=table)

    def test_a_hypothesis_in_the_gold_does_not_count_as_a_miss(self):
        """标注时写作「假设」的金标不进召回率分母，也不把报中它的报告项算成误报。"""
        gold = GoldLabel(
            sample_id="run20",
            issues=[
                GoldIssue("G1", "每日上限为空", "critical", file_paths=["config/daily.xlsx"]),
                GoldIssue("G2", "分解收益与品质倒挂", "medium",
                          file_paths=["config/reward.xlsx"], verification_state="hypothesis"),
            ],
            change_files=["config/daily.xlsx", "config/reward.xlsx"],
        )
        sample = _sample(
            findings=[
                _finding("分解收益与品质倒挂", "medium", ["config/reward.xlsx"],
                         ["config/reward.xlsx:30 收益 3 品质 -1"], "F1"),
            ],
            change_files=["config/daily.xlsx", "config/reward.xlsx"],
        )

        result = self._evaluate(sample, gold)

        assert result["gold"]["hypotheses"] == 1
        assert result["recall"]["total"] == 1        # 只算 confirmed
        assert result["recall"]["overall"] == 0.0    # G1 漏了
        assert result["false_positive"]["count"] == 1  # 报中的是假设 —— 但仍未被认领
        assert any("假设" in note for note in result["false_positive"]["notes"])

    def test_matching_is_deterministic_and_auditable(self):
        """同一份输入永远给出同一份账，而且逐对可核。"""
        gold = GoldLabel(
            sample_id="run20",
            issues=[GoldIssue("G1", "每日上限为空", "critical", file_paths=["a.xlsx"])],
            change_files=["a.xlsx"],
        )
        findings = [
            _finding("每日上限为空", "critical", ["a.xlsx"], ["a.xlsx:1"], "F1"),
            _finding("每日上限为空（另一处）", "high", ["a.xlsx"], ["a.xlsx:9"], "F2"),
        ]
        sample = _sample(findings=findings, change_files=["a.xlsx"])

        first = self._evaluate(sample, gold)
        second = self._evaluate(sample, gold)

        assert first["matching"] == second["matching"]
        assert len(first["matching"]) == 1
        pair = first["matching"][0]
        assert set(pair) == {"gold_issue_id", "gold_severity", "finding_id", "finding_index",
                             "finding_severity", "score", "file_overlap", "title_similarity"}

    def test_a_zero_denominator_gives_none_not_zero(self):
        """分母是 0（或没有金标/没有报告项）→ `None`（未知），**不是 0**。

        折成 0 之后，「这次没有可判的东西」与「这次一条都没做到」就分不开了 ——
        前者会让一个刚建的空样本把整批的平均召回率拽到 0。
        """
        gold = GoldLabel(sample_id="run20", issues=[], change_files=[])
        sample = _sample(findings=[], change_files=[])

        result = metrics_module.evaluate_sample(sample, gold)

        assert result["recall"]["overall"] is None
        assert result["false_positive"]["rate"] is None
        assert result["evidence_locatable"]["rate"] is None
        assert result["changed_file_coverage"]["ratio"] is None
        assert result["dependency_closure_coverage"]["ratio"] is None

        summary = metrics_module.summarize([result])
        assert summary["recall"]["ratio"] is None
        assert summary["recall"]["covered"] is None
        assert summary["false_positive"]["ratio"] is None

    def test_summary_is_micro_averaged(self):
        """汇总用微平均：小样本不该与大样本等权（否则一个 1 条金标的样本能左右总数）。"""
        gold_big = GoldLabel(
            sample_id="big", issues=[
                GoldIssue(f"B{i}", "标题不重合的内容", "high", file_paths=["big.xlsx"])
                for i in range(10)
            ],
            change_files=["big.xlsx"],
        )
        sample_big = _sample(
            sample_id="big",
            findings=[_finding("标题不重合的内容", "high", ["big.xlsx"], ["big.xlsx:1"], f"F{i}")
                      for i in range(10)],
            change_files=["big.xlsx"],
        )
        gold_small = GoldLabel(
            sample_id="small",
            issues=[GoldIssue("S1", "完全不同的一件事", "high", file_paths=["small.xlsx"])],
            change_files=["small.xlsx"],
        )
        sample_small = _sample(sample_id="small", findings=[], change_files=["small.xlsx"])

        results = [
            metrics_module.evaluate_sample(sample_big, gold_big),
            metrics_module.evaluate_sample(sample_small, gold_small),
        ]
        summary = metrics_module.summarize(results)

        assert summary["recall"]["covered"] == 10
        assert summary["recall"]["total"] == 11
        assert summary["recall"]["ratio"] == round(10 / 11, 6)


# ---------------------------------------------------------------------------
#  2b. 证据覆盖的两层口径：同一个分母上的两个数，不许互相代替
# ---------------------------------------------------------------------------

class TestCoverageScopesAreNotInterchangeable:
    """裁决：**主口径是逐轮明细那一层**（复测文档的验收数 42/1009），台账的保守口径
    （32/1009）是次口径 —— 它额外要求「标签里的短提交号命中白名单里的
    `latest_commit_id`」，所以只会更小。

    两个数都真，回答的是不同问题：大的那个是「这个文件碰过没有」，小的是「这一版改动
    看全了没有」。这条用例把「哪一个数挂在哪个名字下面」钉死 —— 换过来就是结论反了：
    拿 42 当保守口径会低估漏看，拿 32 当明细口径会漏报 10 个**确实取到过内容**的文件。
    """

    LATEST = {"config/a.xlsx": "a" * 40, "config/b.xlsx": "b" * 40, "config/c.xlsx": "c" * 40}

    @classmethod
    def _payload(cls):
        return {
            "mode": "weekly",
            "delta_files": [
                {"file_path": path, "latest_commit_id": commit}
                for path, commit in cls.LATEST.items()
            ],
            "summary": {"batch_files": 3, "window_files": 3},
        }

    @classmethod
    def _sample(cls, *, with_payload=True):
        """主口径 3 个、台账口径 2 个 —— **刻意让两层不一样**。

        `config/b.xlsx` 的标签写了一个不在白名单里的提交号（`f…`）：逐轮明细里它
        确实取到过内容（算主口径），但台账的保守口径要求提交号对上，所以把它排除。
        """
        executed = [
            {"kind": "file_diff", "label": "file_diff aaaaaaaaaaaa config/a.xlsx",
             "chars": 120, "failed": False, "empty": False},
            {"kind": "file_diff", "label": "file_diff ffffffffffff config/b.xlsx",
             "chars": 80, "failed": False, "empty": False},
            # 失败的那条两类都不算 —— 顺手钉住「失败不算看过」
            {"kind": "file_content", "label": "file_content cccccccccccc config/c.xlsx lines=3-4",
             "chars": 0, "failed": False, "empty": False},
            {"kind": "file_content", "label": "file_content dddddddddddd config/c.xlsx",
             "chars": 0, "failed": True, "empty": False},
        ]
        inputs = {
            "change_files": list(cls.LATEST),
            "evidence_detail_collected": True,
            "dependency_closure": [],
        }
        if with_payload:
            inputs["request_payload"] = cls._payload()
        return BenchmarkSample(
            sample_id="run20",
            run_row={"id": 20, "tokens_input": 10, "tokens_output": 2, "duration_ms": 1},
            inputs=inputs,
            outputs={},
            findings=[],
            trace_rows=[
                # 形状照**库里那一列**来：`{"details": [...]}`
                # （`trace_evidence.decode_evidence` 读的是 `details` 这个键；写成裸
                # 列表会静默解出空明细，于是两个口径都变成 0）。
                {"executed_json": json.dumps({"details": executed}, ensure_ascii=False)}
            ],
            evidence_files=list(cls.LATEST),
            coverage={},
        )

    @staticmethod
    def _rows(result):
        return {row["scope"]: row for row in result["evidence_coverage"]["scopes"]}

    def test_every_coverage_row_carries_its_scope_name(self):
        """**每一行都必须带口径名**：没有裸数字可拿，也就没法拿错。"""
        result = metrics_module.evaluate_sample(self._sample(), GoldLabel(sample_id="run20"))
        rows = self._rows(result)

        assert set(rows) == {metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL,
                             metrics_module.EVIDENCE_SCOPE_LEDGER_BY_PAIR}
        for scope, row in rows.items():
            assert row["scope"] == scope
            assert row["label"], row
            assert metrics_module.EVIDENCE_SCOPE_LABELS[scope] in row["label"] or row["label"] in \
                metrics_module.EVIDENCE_SCOPE_LABELS[scope]
        assert result["evidence_coverage"]["main_scope"] == metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL

    def test_the_two_scopes_hold_their_own_numbers(self):
        """三层数字各就各位：明细 3/3、台账 2/3、失败的那条谁都不算。"""
        result = metrics_module.evaluate_sample(self._sample(), GoldLabel(sample_id="run20"))
        rows = self._rows(result)

        detail = rows[metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL]
        assert (detail["covered"], detail["total"]) == (3, 3), detail
        assert detail["ratio"] == 1.0

        ledger = rows[metrics_module.EVIDENCE_SCOPE_LEDGER_BY_PAIR]
        assert (ledger["covered"], ledger["total"]) == (2, 3), ledger
        assert ledger["ratio"] == round(2 / 3, 6)
        assert "latest_commit_id" in ledger["label"], ledger["label"]

    def test_the_summary_rows_also_carry_their_scope(self):
        """汇总**按口径分开归并**，每行带名字 —— 混着加出来的数谁也解释不了。"""
        result = metrics_module.evaluate_sample(self._sample(), GoldLabel(sample_id="run20"))
        summary = metrics_module.summarize([result])
        rows = summary["evidence_coverage_by_scope"]["rows"]

        assert rows[metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL]["scope"] == \
            metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL
        assert (rows[metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL]["covered"],
                rows[metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL]["total"]) == (3, 3)
        assert (rows[metrics_module.EVIDENCE_SCOPE_LEDGER_BY_PAIR]["covered"],
                rows[metrics_module.EVIDENCE_SCOPE_LEDGER_BY_PAIR]["total"]) == (2, 3)
        assert summary["evidence_coverage_by_scope"]["main_scope"] == \
            metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL

    def test_the_ledger_scope_is_unknown_rather_than_borrowing_the_detail_number(self):
        """没有原始 payload / 台账时，次口径是 `None` + 原因 —— **不许拿 3 去顶**。"""
        sample = self._sample(with_payload=False)
        rows = self._rows(metrics_module.evaluate_sample(sample, GoldLabel(sample_id="run20")))

        ledger = rows[metrics_module.EVIDENCE_SCOPE_LEDGER_BY_PAIR]
        assert ledger["covered"] is None and ledger["total"] is None
        assert ledger["ratio"] is None
        assert ledger["reason"], ledger
        # 主口径不受影响：它只依赖逐轮明细
        assert rows[metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL]["covered"] == 3

    def test_an_ab_metric_name_must_carry_the_scope(self):
        """A/B 那一侧：指标名自带口径，写错口径要当场炸，不许退化成「样本不足」。"""
        from benchmarks.ai_analysis import ab_compare as ab_module

        result = metrics_module.evaluate_sample(self._sample(), GoldLabel(sample_id="run20"))

        assert ab_module.sample_metric(
            result, f"{ab_module.EVIDENCE_METRIC_PREFIX}{metrics_module.EVIDENCE_SCOPE_TRACE_DETAIL}"
        ) == 1.0
        assert ab_module.sample_metric(
            result, f"{ab_module.EVIDENCE_METRIC_PREFIX}{metrics_module.EVIDENCE_SCOPE_LEDGER_BY_PAIR}"
        ) == round(2 / 3, 6)
        with pytest.raises(KeyError):
            ab_module.sample_metric(result, f"{ab_module.EVIDENCE_METRIC_PREFIX}whatever")
        # 两个口径都必须在可比指标表里，否则 A/B 报告上根本看不见它们
        for scope in metrics_module.EVIDENCE_SCOPES:
            assert f"{ab_module.EVIDENCE_METRIC_PREFIX}{scope}" in ab_module.METRIC_DIRECTIONS


# ---------------------------------------------------------------------------
#  3. A/B：小样本上不许下结论
# ---------------------------------------------------------------------------

class TestAbCompareRefusesToOverclaim:
    def _results(self, sample_ids, *, recall_matched, recall_total, cost, tokens):
        results = []
        for sample_id in sample_ids:
            matched = recall_matched(sample_id)
            total = recall_total(sample_id)
            results.append({
                "sample_id": sample_id,
                "tokens": {"input": tokens, "output": 0, "total": tokens},
                "duration_ms": 1000,
                "cost": {"amount_exact": cost, "amount": cost, "currency": "CNY", "reason": ""},
                "recall": {"overall": (matched / total if total else None),
                           "matched": matched, "total": total, "by_severity": {}},
                "false_positive": {"rate": 0.1, "count": 1, "total": 10, "notes": []},
                "severity": {"exact_agreement": 1.0, "matched": matched,
                             "mean_level_delta": 0.0, "confusion": {}},
                "evidence_locatable": {"rate": 0.5, "count": matched, "total": total,
                                       "unlocatable": []},
                "changed_file_coverage": {"covered": 1, "total": 2, "ratio": 0.5,
                                          "source": "gold", "uncovered": []},
                "dependency_closure_coverage": {"covered": None, "total": None, "ratio": None,
                                                "reason": "没有闭包"},
                "stages": [],
            })
        return results

    def test_a_three_sample_improvement_is_not_a_conclusion(self):
        """3 个样本、召回率从 1/2 提到 2/2 —— 这**不是**「提升 50%」。"""
        ids = ["a", "b", "c"]
        baseline = self._results(ids, recall_matched=lambda _s: 1, recall_total=lambda _s: 2,
                                 cost="1.00", tokens=1000)
        candidate = self._results(ids, recall_matched=lambda _s: 2, recall_total=lambda _s: 2,
                                  cost="0.90", tokens=900)

        report = compare_results(baseline, candidate)

        assert report["paired_samples"] == 3
        assert report["verdict"] == "insufficient_samples"
        assert report["claims"] == [], f"小样本上不该有结论性判断：{report['claims']}"
        assert any(str(MIN_PAIRS_FOR_CLAIM) in warning for warning in report["warnings"])
        text = render_text(report)
        assert "不下任何" in text

    def test_a_large_sample_set_recall_drop_is_flagged_as_a_blocker(self):
        """样本够了、召回率真的掉了 —— 必须挡住「token 降了就放行」。"""
        ids = [f"s{i}" for i in range(MIN_PAIRS_FOR_CLAIM)]
        baseline = self._results(ids, recall_matched=lambda _s: 5, recall_total=lambda _s: 5,
                                 cost="1.00", tokens=1000)
        candidate = self._results(ids, recall_matched=lambda _s: 1, recall_total=lambda _s: 5,
                                  cost="0.50", tokens=500)

        report = compare_results(baseline, candidate)

        assert report["paired_samples"] == MIN_PAIRS_FOR_CLAIM
        assert report["verdict"] == "recall_regression"
        kinds = [claim["kind"] for claim in report["claims"]]
        assert "recall_regression" in kinds, report["claims"]

    def test_a_large_sample_set_with_stable_recall_and_lower_cost_says_so(self):
        ids = [f"s{i}" for i in range(MIN_PAIRS_FOR_CLAIM)]
        baseline = self._results(ids, recall_matched=lambda _s: 4, recall_total=lambda _s: 5,
                                 cost="1.00", tokens=1000)
        candidate = self._results(ids, recall_matched=lambda _s: 4, recall_total=lambda _s: 5,
                                  cost="0.60", tokens=600)

        report = compare_results(baseline, candidate)

        assert report["verdict"] == "recall_not_worse"
        kinds = [claim["kind"] for claim in report["claims"]]
        assert "recall_non_inferior" in kinds
        assert "cost_change" in kinds

    def test_metrics_without_comparable_pairs_report_none_not_zero(self):
        """闭包覆盖率两侧都是 `None` → 报「无可比样本」，不是 0。"""
        ids = ["a", "b"]
        baseline = self._results(ids, recall_matched=lambda _s: 1, recall_total=lambda _s: 2,
                                 cost="1.00", tokens=1000)
        candidate = self._results(ids, recall_matched=lambda _s: 1, recall_total=lambda _s: 2,
                                  cost="0.90", tokens=900)

        report = compare_results(baseline, candidate)

        closure = report["metrics"]["dependency_closure_coverage"]
        assert closure["n"] == 0
        assert closure["mean"] is None
        assert any("依赖闭包覆盖率" in caveat for caveat in report["caveats"])

    def test_unpaired_samples_are_reported(self):
        baseline = self._results(["a", "b", "c"], recall_matched=lambda _s: 1,
                                 recall_total=lambda _s: 2, cost="1.00", tokens=1000)
        candidate = self._results(["b", "c", "d"], recall_matched=lambda _s: 1,
                                  recall_total=lambda _s: 2, cost="1.00", tokens=1000)

        report = compare_results(baseline, candidate)

        assert report["paired_samples"] == 2
        assert report["only_baseline"] == ["a"]
        assert report["only_candidate"] == ["d"]
        assert any("只在" in warning for warning in report["warnings"])

    def test_cost_pairs_are_skipped_when_unpriced(self):
        """算不出费用的样本**不参与**成本差异，也不补 0。"""
        ids = [f"s{i}" for i in range(MIN_PAIRS_FOR_CLAIM)]
        baseline = self._results(ids, recall_matched=lambda _s: 4, recall_total=lambda _s: 5,
                                 cost="1.00", tokens=1000)
        candidate = self._results(ids, recall_matched=lambda _s: 4, recall_total=lambda _s: 5,
                                  cost=None, tokens=600)

        report = compare_results(baseline, candidate)

        cost = report["metrics"]["cost_per_sample"]
        assert cost["n"] == 0
        assert cost["samples_missing"] == MIN_PAIRS_FOR_CLAIM
        assert any("算不出费用" in caveat for caveat in report["caveats"])


# ---------------------------------------------------------------------------
#  4. 脱敏：稳定 + 默认开启
# ---------------------------------------------------------------------------

class TestRedactionIsStableAndOnByDefault:
    def _export(self, out_dir, *, run_id=20, **kwargs):
        from scripts.export_ai_benchmark_sample import export_run

        db_path = _build_fixture_db()
        return export_run(db_path, run_id, out_dir, **kwargs)

    def test_the_same_path_always_gets_the_same_pseudonym(self, tmp_path):
        """同一路径 → 同一假名。**这条不做，做差与覆盖率全都算不出来。**"""
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        first = Pseudonymizer(salt="s1")
        second = Pseudonymizer(salt="s1")

        assert first.path("code/a/b.lua") == second.path("code/a/b.lua")
        assert first.path("code/a/b.lua") != first.path("code/a/c.lua")
        # 分隔符不同但指向同一个文件 → 同一个假名
        assert first.path("code/a/b.lua") == first.path("code\\a\\b.lua")

    def test_a_different_salt_changes_the_pseudonym(self):
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        assert Pseudonymizer(salt="s1").path("a.lua") != Pseudonymizer(salt="s2").path("a.lua")

    def test_the_extension_is_kept(self):
        """扩展名要留着：Excel 与代码的覆盖率要分开算，而扩展名本身不敏感。"""
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        pseudo = Pseudonymizer().path("配表/怪物表.xlsx")

        assert pseudo.endswith(".xlsx"), pseudo
        assert "怪物表" not in pseudo

    def test_free_text_paths_are_redacted_too(self):
        """只换结构化字段会漏：工具标签 `file_diff <commit> <path>` 里全是路径。"""
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        pseudonymizer = Pseudonymizer()
        raw = "file_diff deadbeef1234 code/a/b.lua at C:\\repo\\code\\a\\b.lua:12"

        redacted = pseudonymizer.text(raw)

        assert "code/a/b.lua" not in redacted
        assert "deadbeef1234" not in redacted
        assert pseudonymizer.path("code/a/b.lua") in redacted

    def test_directory_only_paths_in_prose_are_redacted(self):
        """正文里常见「只有目录没有文件名」的路径（`a/b/C/`）—— 带扩展名那条抓不到它。"""
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        redacted = Pseudonymizer().text("把 `pkg/sub/Deep/` 与 `pkg/sub/Deep` 下的用例跑一遍")

        assert "pkg" not in redacted and "Deep" not in redacted, redacted

    def test_a_numeric_ratio_is_not_mistaken_for_a_path(self):
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        text = "命中率 3/4，覆盖率 12/1009"

        assert Pseudonymizer().text(text) == text, "数值比值被当成路径换掉了"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "已知缺陷：text() 把路径拆成「目录 + 文件名」分别换，而结构化字段走 path() "
            "（整条路径一个假名），同一个原始路径于是得到两个不同的假名。"
        ),
    )
    def test_a_label_path_redacts_to_the_same_pseudonym_as_the_structured_field(self):
        """工具标签里的路径必须换成与结构化字段**同一个**假名。

        这条不成立时，台账的保守口径（要求标签里的短提交号命中白名单里那一条）在
        **默认脱敏**的样本上会静默少算 —— 实测 Run 20：逐轮明细口径 42/1009、台账口径
        32/1009，而未脱敏导出两边都是 **42/1009**。也就是说那个 32 不是平台口径，
        是脱敏引入的偏差（平台的 `coverage_rows` 在未脱敏数据上把两种去重都算成 42）。

        为什么必须钉住：脱敏默认开启，于是**每次导出的度量都悄悄换了一个问题**，
        而报告上两个数看起来都只是「覆盖率」。
        """
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        pseudonymizer = Pseudonymizer()
        path = "config/150_shopping_mall/【158】商店商品表_CfgStoreGoods.xlsx"

        structured = pseudonymizer.path(path)
        in_label = pseudonymizer.text(f"file_diff b76e590523fb {path}")

        assert structured in in_label, f"标签里换成了另一个假名：{in_label!r}"

    def test_pseudonymising_twice_does_not_hash_twice(self):
        """幂等：对已脱敏的数据再脱敏不能产生第二个假名。

        糊掉的话，同一条路径在 `change_files` 里是一个假名、在 `findings` 里是另一个，
        覆盖率静默变 0，而两边看起来都很正常。
        """
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        pseudonymizer = Pseudonymizer()
        once = pseudonymizer.path("code/a.lua")

        assert pseudonymizer.path(once) == once
        assert pseudonymizer.commit(pseudonymizer.commit("deadbeef1234")) == pseudonymizer.commit("deadbeef1234")
        assert pseudonymizer.text(once) == once

    def test_a_short_and_a_full_sha_get_the_same_pseudonym(self):
        """同一个提交的全 SHA 与短 SHA 必须映到同一个假名。

        不然 `coverage_ledger` 的保守口径（按 `(提交, 路径)` 配对，靠前缀匹配）在
        脱敏样本上恒为 0 —— 实测 42 个文件掉成 0，而宽松口径还是 32。
        """
        from scripts.export_ai_benchmark_sample import Pseudonymizer

        pseudonymizer = Pseudonymizer()
        full = "98a2ead0ddc841e58b01ff3907ce65f54613683b"

        assert pseudonymizer.commit(full) == pseudonymizer.commit(full[:12])

    def test_redacted_json_payloads_stay_parseable(self):
        """脱敏不能把 JSON 改坏。

        实测踩过：把 `text()` 套在**序列化后**的 JSON 上，`\\b[0-9a-fA-F]{7,40}\\b`
        会把 `"duration_ms": 1595548` 里的 7 位数字当成提交短 SHA 换掉，于是
        `json.loads` 在 73,136 字节处报 `Expecting value` —— 样本文件在，读不出来。
        """
        import json as json_module

        from scripts.export_ai_benchmark_sample import Pseudonymizer

        pseudonymizer = Pseudonymizer()
        payload = {
            "duration_ms": 1595548,
            "counts": {"1234567": 8},
            "files": [{"file_path": "code/a.lua", "latest_commit_id": "98a2ead0ddc8"}],
            "note": "耗时 1595548 毫秒，见 code/a.lua",
        }

        redacted = pseudonymizer.structure(payload)
        serialized = json_module.dumps(redacted, ensure_ascii=False)

        assert json_module.loads(serialized) == redacted
        assert redacted["duration_ms"] == 1595548, "数值被当成提交号换掉了"
        assert redacted["files"][0]["file_path"] == pseudonymizer.path("code/a.lua")

    def test_export_is_redacted_and_parseable_end_to_end(self, tmp_path):
        from benchmarks.ai_analysis.dataset import BenchmarkSample
        from benchmarks.ai_analysis.profile import stage_rows

        written = self._export(str(tmp_path / "run20"))
        assert written["redacted"] is True

        sample = BenchmarkSample.load(written["sample_path"])
        assert sample.redacted is True
        assert sample.stages and sample.stages[0]["label"] == "S1"
        blob = json.dumps(sample.to_dict(), ensure_ascii=False)
        assert "配表/怪物表.xlsx" not in blob
        assert "aaaa1111bbbb2222cccc" not in blob
        assert sample.inputs["redaction"]["enabled"] is True
        assert sample.inputs["redaction"]["not_covered"], "没把「脱敏不覆盖什么」写进样本"

    def test_export_redacts_by_default_and_says_so(self, tmp_path):
        written = self._export(str(tmp_path / "run20"))

        assert written["redacted"] is True
        sample = json.loads((tmp_path / "run20" / "samples" / "run20.json").read_text("utf-8"))
        assert sample["redacted"] is True
        blob = json.dumps(sample, ensure_ascii=False)
        assert "怪物表" not in blob, "真文件名还在样本里"
        assert "aaaa1111bbbb2222cccc" not in blob, "真提交号还在样本里"

    def test_no_redact_keeps_the_original_values(self, tmp_path):
        written = self._export(str(tmp_path / "raw"), redact=False)

        assert written["redacted"] is False
        sample = json.loads((tmp_path / "raw" / "samples" / "run20.json").read_text("utf-8"))
        blob = json.dumps(sample, ensure_ascii=False)
        assert "怪物表" in blob
        assert sample["redacted"] is False

    def test_the_export_is_read_only(self, tmp_path):
        """导出不许成为生产库的第二个写入方：库文件在导出前后必须一模一样。"""
        import hashlib

        db_path = _build_fixture_db()
        from scripts.export_ai_benchmark_sample import export_run

        before = hashlib.sha1(open(db_path, "rb").read()).hexdigest()
        export_run(db_path, 20, str(tmp_path / "run20"))
        after = hashlib.sha1(open(db_path, "rb").read()).hexdigest()

        assert before == after, "导出过程改动了库文件"

    def test_a_written_connection_is_refused(self, tmp_path):
        """只读靠 URI 保证，不靠自觉：真去写会立刻报错。"""
        from scripts.export_ai_benchmark_sample import open_readonly

        db_path = _build_fixture_db()
        connection = open_readonly(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("UPDATE ai_analysis_run SET status = 'x'")
        finally:
            connection.close()

    def test_the_gold_template_is_not_placed_where_the_loader_would_read_it(self, tmp_path):
        """骨架放进 `gold/` 会被当成「0 条问题的真金标」，召回率静默变 0%。"""
        written = self._export(str(tmp_path / "run20"))

        assert "gold_templates" in written["gold_template_path"]
        template = json.loads(open(written["gold_template_path"], encoding="utf-8").read())
        assert template["sample_id"] == "run20"
        assert template["change_files"], "变化文件应当自动填进去（那是机器知道的）"

        loaded = load_dataset(str(tmp_path / "run20"))
        assert loaded.golds == {}, "骨架被 load_dataset 当成真金标了"


# ---------------------------------------------------------------------------
#  5. 阶段剖析复用既有实现
# ---------------------------------------------------------------------------

class TestStageProfiling:
    def test_stage_rows_come_from_the_payload_through_the_platform_reader(self):
        """阶段账必须走 `response_payload["subagents"]`（唯一来源），字段口径同面板。"""
        import json as json_module

        from benchmarks.ai_analysis.profile import profile_stages

        payload = {
            "subagents": [
                {"label": "S1", "role": "subagent", "status": "degraded", "rounds": 7,
                 "requests": 40, "tokens_input": 535286, "tokens_output": 60144,
                 "cache_read_tokens": 430000, "anomalies": 13},
                {"label": "S2", "role": "subagent", "status": "degraded", "rounds": 7,
                 "requests": 40, "tokens_input": 543661, "tokens_output": 68199,
                 "cache_read_tokens": None, "anomalies": 9},
                {"label": "汇总", "role": "synthesis", "status": "succeeded", "rounds": 4,
                 "requests": 19, "tokens_input": 237485, "tokens_output": 40318},
                {"label": "V1", "role": "verify", "status": "succeeded", "rounds": 3,
                 "requests": 11, "tokens_input": 103399, "tokens_output": 63280},
            ]
        }
        sample = _sample()
        sample.run_row["response_payload"] = json_module.dumps(payload, ensure_ascii=False)
        sample.run_row["tokens_input"] = 1722027
        sample.run_row["tokens_output"] = 277806

        profile = profile_stages(sample)

        assert set(profile["groups"]) == {"subagent", "synthesis", "verify"}
        assert profile["groups"]["subagent"]["tokens_input"]["total"] == 1078947
        assert profile["groups"]["subagent"]["tokens_input"]["missing"] == 0
        # 未上报的 cache_read 记一条缺失，**不折成 0**
        assert profile["groups"]["subagent"]["cache_read_tokens"]["missing"] == 1
        assert profile["groups"]["verify"]["tokens_output"]["total"] == 63280
        # 与 run 行的差额要报出来（分片之和只占 run 合计的一部分）
        assert profile["reconciliation"]["run_tokens_input"] == 1722027
        assert profile["reconciliation"]["delta_input"] == 1722027 - (
            535286 + 543661 + 237485 + 103399
        )

    def test_a_missing_payload_yields_no_stages_rather_than_a_fabricated_one(self):
        from benchmarks.ai_analysis.profile import profile_stages

        sample = _sample()

        profile = profile_stages(sample)

        assert profile["stages"] == []
        assert profile["groups"] == {}

    def test_evidence_file_extraction_filters_failed_and_non_file_tools(self):
        """`find_references` 的失败不算「这个文件看过」—— 口径照抄 coverage_ledger。"""
        import json as json_module

        from benchmarks.ai_analysis.profile import evidence_files_from_traces

        executed = [
            {"kind": "file_diff", "label": "file_diff aaaa1111bbbb22 code/a.lua", "chars": 10,
             "failed": False, "empty": False},
            {"kind": "file_diff", "label": "file_diff aaaa1111bbbb22 code/b.lua", "chars": 0,
             "failed": True, "empty": False},
            {"kind": "file_content", "label": "file_content aaaa1111bbbb22 code/c.lua",
             "chars": 0, "failed": False, "empty": True},
            {"kind": "file_content", "label": "file_content aaaa1111bbbb22 code/d.lua",
             "chars": 5, "failed": False, "empty": False},
            {"kind": "find_references", "label": "find_references code/e.lua", "chars": 3,
             "failed": False, "empty": False},
        ]
        trace_rows = [{"executed_json": json_module.dumps({"items": 5, "details": executed})}]

        files = evidence_files_from_traces(trace_rows)

        assert files == ["code/a.lua", "code/d.lua"], files

    def test_not_collected_is_not_the_same_as_nothing_collected(self):
        from benchmarks.ai_analysis.profile import coverage_from_traces

        never = coverage_from_traces([{"executed_json": None}])
        empty = coverage_from_traces([{"executed_json": json.dumps({"items": 0, "details": []})}])

        assert never["evidence_coverage"]["collected"] is False
        assert never["evidence_coverage"]["by_path"]["covered"] is None
        assert empty["evidence_coverage"]["collected"] is True


# ---------------------------------------------------------------------------
#  夹具：一个最小的只读库
# ---------------------------------------------------------------------------

def _build_fixture_db() -> str:
    """建一个带三张表的最小库（只用一次，用完交由进程退出清理）。"""
    path = os.path.join(tempfile.mkdtemp(prefix="ai_bench_fixture_"), "fixture.db")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE ai_analysis_run (
                id INTEGER PRIMARY KEY, project_id INTEGER, target_type TEXT,
                target_id TEXT, target_key TEXT, status TEXT, response_mode TEXT,
                scope TEXT, trigger_source TEXT, degradation TEXT, analysis_revision TEXT,
                model TEXT, prompt_version TEXT, skill_version TEXT, rules_version TEXT,
                rounds_used INTEGER, tool_requests_used INTEGER, tokens_input INTEGER,
                tokens_output INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                cache_source TEXT, duration_ms INTEGER, tool_stats_json TEXT,
                context_chars INTEGER, anomalies_found INTEGER, dropped_count INTEGER,
                pricing_version TEXT, subagent_mode TEXT, subagent_count INTEGER,
                conclusion_structured INTEGER, request_payload TEXT, response_payload TEXT,
                response_text TEXT, delta_summary TEXT, error_message TEXT,
                created_at TEXT, started_at TEXT, finished_at TEXT
            );
            CREATE TABLE ai_analysis_trace (
                id INTEGER PRIMARY KEY, run_id INTEGER, round_index INTEGER, agent TEXT,
                agent_round INTEGER, outcome TEXT, parsed_ok INTEGER, request_chars INTEGER,
                response_text TEXT, error TEXT, correction_hint TEXT, requests_json TEXT,
                executed_json TEXT, dropped_json TEXT, budget_notes TEXT,
                context_chars INTEGER, tokens_input INTEGER, tokens_output INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER, duration_ms INTEGER,
                created_at TEXT
            );
            CREATE TABLE ai_analysis_anomaly (
                id INTEGER PRIMARY KEY, run_id INTEGER, project_id INTEGER, fingerprint TEXT,
                title TEXT, category TEXT, severity TEXT, confidence REAL, evidence TEXT,
                commit_ref TEXT, file_path TEXT, impact TEXT, suggestion TEXT,
                disposition TEXT, disposition_by TEXT, disposition_at TEXT,
                disposition_note TEXT, created_at TEXT, updated_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO ai_analysis_run (id, project_id, target_type, target_key, status,"
            " scope, trigger_source, model, rounds_used, tool_requests_used, tokens_input,"
            " tokens_output, duration_ms, anomalies_found, subagent_count,"
            " conclusion_structured, request_payload, response_payload, response_text,"
            " tool_stats_json, created_at)"
            " VALUES (20, 1, 'weekly', 'W33', 'degraded', 'full', 'manual', 'demo-model',"
            " 26, 131, 1722027, 277806, 1595400, 19, 4, 1, ?, ?, ?, ?, '2026-09-21T16:00:00')",
            (
                json.dumps({"change_files": ["配表/怪物表.xlsx", "code/a.lua"]}, ensure_ascii=False),
                json.dumps({"subagents": [
                    {"label": "S1", "role": "subagent", "status": "degraded", "rounds": 7,
                     "requests": 40, "tokens_input": 535286, "tokens_output": 60144,
                     "cache_read_tokens": 430000, "anomalies": 13},
                ]}, ensure_ascii=False),
                "报告正文：配表/怪物表.xlsx 的每日上限为空（见提交 aaaa1111bbbb2222cccc）",
                json.dumps({"file_diff": {"calls": 95}}, ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO ai_analysis_trace (run_id, round_index, agent, outcome, parsed_ok,"
            " requests_json, executed_json, tokens_input, tokens_output, duration_ms)"
            " VALUES (20, 0, 'S1', 'final', 1, ?, ?, 1000, 200, 5000)",
            (
                json.dumps([{"type": "file_diff", "commit": "aaaa1111bbbb2222cccc",
                             "path": "配表/怪物表.xlsx"}], ensure_ascii=False),
                json.dumps([{"kind": "file_diff",
                             "label": "file_diff aaaa1111bbbb22 配表/怪物表.xlsx",
                             "chars": 120, "failed": False, "empty": False}], ensure_ascii=False),
            ),
        )
        connection.execute(
            "INSERT INTO ai_analysis_anomaly (id, run_id, project_id, fingerprint, title,"
            " category, severity, confidence, evidence, commit_ref, file_path, impact,"
            " suggestion, disposition) VALUES (1, 20, 1, 'fp1', '每日上限为空', 'config',"
            " 'critical', 0.9, '配表/怪物表.xlsx:12 上限为空', 'aaaa1111bbbb2222cccc',"
            " '配表/怪物表.xlsx', '可能不限次', '确认一下', 'open')"
        )
        connection.commit()
    finally:
        connection.close()
    return path
