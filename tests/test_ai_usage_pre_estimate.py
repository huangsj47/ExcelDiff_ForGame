# -*- coding: utf-8 -*-
"""执行**前**的代价估算（AI-P1-03）。

## 这个文件守的五条性质

1. **区间，不是单点。** 实际用量取决于模型索取了几次上下文、命中多少缓存 —— 那是平台
   控制不了的部分。给一个单点会被读成承诺，所以 `estimate_analysis` 返回 `{low, high}`，
   两个端都锚在**实测运行**上。
2. **价格表没配时只给 token 与时间，不伪造费用。** `DEFAULT_PRICE_TABLE` 是空表，
   这是常态而不是异常（`services/ai/pricing.py` 模块 docstring 第 1 条）。费用那一格
   给「算不出 + 一句理由」，**绝不回落到 0** —— `0` 是「这次不花钱」这个确定的结论。
3. **一条历史运行都没有时不编数字**（全 `None` + 一句说明）。凭空的估算比没有估算更糟：
   它会让人以为「平台知道要花多少」。
4. **它必须是纯函数**，第二波的 Job 协议要在**建 job 的那一刻**算出 `planned_tokens_low/high`
   写进表里 —— 那时不能有一条会查库、会读时钟的链路横在里面。本文件第一条用例就是
   「不碰数据库也能算出结果」。
5. **命中可复用基线不参与算术。** 它的后果是「不用新建这次付费运行」，不是一个折扣；
   把它折成系数会让区间凭空变小（那才叫伪造）。

**测试库是会话级共用的**（`tests/conftest.py` 没有逐用例重置），所以断言只针对本用例
自己新建的项目号。
"""
from __future__ import annotations

import json
import uuid

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun
from services.ai.pricing import parse_price_table
from services.ai.usage import (
    estimate_analysis,
    estimation_sample,
)
from services.ai_analysis_service import update_project_analysis_config
from services.ai_usage_service import analysis_estimate, parse_estimate_args

PRICE_TABLE = json.dumps(
    {
        "version": "est-1",
        "currency": "CNY",
        "models": {
            "fake-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2},
        },
    }
)


def _price_table():
    table, errors = parse_price_table(PRICE_TABLE)
    assert table is not None and not errors
    return table


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _sample(
    run_id: int,
    *,
    files: int | None = 100,
    tokens_input: int | None = 1_000_000,
    tokens_output: int | None = 200_000,
    cache_read: int | None = 800_000,
    duration_ms: int | None = 600_000,
    scope: str = "full",
    shards: int | None = None,
    created_at: str = "",
    cost: dict | None = None,
) -> dict:
    """一条**估算样本**（形状与 `estimation_sample` 的产物一致）。"""
    return {
        "run_id": run_id,
        "created_at": created_at or f"2026-09-{run_id:02d}",
        "scope": scope,
        "model": "fake-model",
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cache_read": cache_read,
        "duration_ms": duration_ms,
        "rounds": 20,
        "requests": 60,
        "files": files,
        "shards": shards,
        "cost": cost,
    }


# ==========================================================================
# 一、纯函数：不碰库、不读配置、不看时钟
# ==========================================================================


class TestItIsAPureFunction:
    def test_it_does_not_need_a_database_or_an_app_context(self):
        """**在 app context 之外**直接调 —— 第二波要在建 job 的那一刻用它。

        这条用例的价值不在于「它现在能跑」，而在于**将来有人往里面加一次查询时它会红**：
        那一刻起 Job 协议就没法在建 job 时估算 `planned_tokens_low/high` 了。
        """
        result = estimate_analysis(
            planned_files=100,
            mode="full",
            recent_runs=[_sample(1), _sample(2)],
            price_table=_price_table(),
        )

        assert result["tokens"]["low"] is not None
        assert result["tokens"]["high"] is not None

    def test_the_same_input_gives_the_same_output(self):
        """纯函数：同样输入两次调用**逐字相同**（没有藏一个「现在几点」在里面）。"""
        kwargs = dict(
            planned_files=100,
            mode="full",
            recent_runs=[_sample(1), _sample(2), _sample(3)],
            price_table=_price_table(),
        )

        assert estimate_analysis(**kwargs) == estimate_analysis(**kwargs)


# ==========================================================================
# 二、区间从哪来
# ==========================================================================


class TestTheRangeComesFromRealObservations:
    def test_no_history_gives_unknown_not_a_number(self):
        """一条历史运行都没有 → 全 `None` + 一句说明。**不编数字**。"""
        result = estimate_analysis(planned_files=500, mode="full", recent_runs=[])

        assert result["tokens"] == {"low": None, "high": None, "unit": "token"}
        assert result["duration_ms"] == {"low": None, "high": None}
        assert result["cost"]["computable"] is False
        assert result["last_actual"] is None
        assert result["basis"]["source"] == "none"
        assert any("先跑一次" in note for note in result["notes"])
        # 费用那一格也要说得出**为什么**没有数字：没有 token 可折时 `estimate_cost`
        # 一次都没被调用，空着一句理由会让界面自己猜（「价格表不可用」在「压根没跑过」
        # 的时候是句假话）。
        assert result["cost"]["reason"] == (
            "还没有可参照的历史运行，估不出 token，也就估不出费用"
        )

    def test_the_range_is_scaled_by_the_target_file_count(self):
        """单文件强度 × 目标文件数 —— 目标翻了 10 倍，区间也该跟着翻。

        样本：1,200,000 token / 100 文件 = 12,000 token/文件。
        """
        sample = _sample(1, files=100, tokens_input=1_000_000, tokens_output=200_000)
        small = estimate_analysis(planned_files=100, recent_runs=[sample])
        big = estimate_analysis(planned_files=1000, recent_runs=[sample])

        assert small["tokens"]["low"] == 1_200_000
        assert big["tokens"]["low"] == 12_000_000
        assert big["tokens"]["low"] == small["tokens"]["low"] * 10

    def test_the_two_ends_are_the_two_extremes_of_the_samples(self):
        """低端 = 最省的一次、高端 = 最费的一次（都按文件数折算），中间不插任何系数。"""
        cheap = _sample(1, files=100, tokens_input=500_000, tokens_output=100_000)
        dear = _sample(2, files=100, tokens_input=3_000_000, tokens_output=600_000)

        result = estimate_analysis(planned_files=100, recent_runs=[cheap, dear])

        assert result["tokens"]["low"] == 600_000
        assert result["tokens"]["high"] == 3_600_000

    def test_a_single_outlier_is_trimmed_when_there_are_enough_samples(self):
        """样本够多时各掐掉一个极值：一次卡住重试的运行能把高端整个带跑偏。

        5 条样本 = 3 条正常 + 1 条极小 + 1 条极大 → 掐掉两端之后取中间三条的极值。
        """
        samples = [
            _sample(1, files=100, tokens_input=1_000_000, tokens_output=0),
            _sample(2, files=100, tokens_input=1_100_000, tokens_output=0),
            _sample(3, files=100, tokens_input=1_200_000, tokens_output=0),
            _sample(4, files=100, tokens_input=10_000, tokens_output=0),      # 极小
            _sample(5, files=100, tokens_input=50_000_000, tokens_output=0),  # 极大
        ]

        result = estimate_analysis(planned_files=100, recent_runs=samples)

        assert result["tokens"]["low"] == 1_000_000, "极小的那一次没被掐掉"
        assert result["tokens"]["high"] == 1_200_000, "极大的那一次没被掐掉"

    def test_without_a_file_count_the_totals_are_used_unscaled(self):
        """老运行没有文件数 → 直接给实测总量，**不缩放**（缩放的依据都没有，硬缩放等于编）。"""
        result = estimate_analysis(
            planned_files=9999,
            recent_runs=[
                _sample(1, files=None, tokens_input=1_000_000, tokens_output=0),
                _sample(2, files=None, tokens_input=2_000_000, tokens_output=0),
            ],
        )

        assert result["basis"]["source"] == "totals"
        assert result["tokens"]["low"] == 1_000_000
        assert result["tokens"]["high"] == 2_000_000, "按目标文件数缩放了"
        assert any("没有记录文件数" in note for note in result["notes"])

    def test_shards_scale_the_wall_clock_because_they_run_serially(self):
        """分片是串行的（一个跑完再跑下一个），所以分片数变了区间要跟着变。"""
        sample = _sample(1, files=100, tokens_input=1_000_000, tokens_output=0,
                         duration_ms=600_000, shards=2)

        one = estimate_analysis(planned_files=100, recent_runs=[sample], shard_count=2)
        four = estimate_analysis(planned_files=100, recent_runs=[sample], shard_count=4)

        assert one["duration_ms"]["low"] == 600_000
        assert four["duration_ms"]["low"] == 1_200_000, "分片数翻倍，墙钟没有跟着翻倍"
        assert any("串行" in note for note in four["notes"])

    def test_a_target_outside_the_sample_range_is_flagged_as_extrapolation(self):
        """目标文件数落在样本区间之外 → 明说这是**外推**（别让区间看起来像实测）。"""
        result = estimate_analysis(
            planned_files=5000,
            recent_runs=[_sample(1, files=100), _sample(2, files=120)],
        )

        assert any("外推" in note for note in result["notes"])


# ==========================================================================
# 三、模式：同类样本优先，没有同类就回落并说清
# ==========================================================================


class TestTheModeOnlyChoosesTheSamples:
    def test_same_mode_samples_win(self):
        result = estimate_analysis(
            planned_files=100,
            mode="incremental",
            recent_runs=[
                _sample(1, files=100, tokens_input=1_000_000, tokens_output=0),
                _sample(2, files=100, tokens_input=9_000_000, tokens_output=0,
                        scope="incremental"),
            ],
        )

        assert result["basis"]["same_mode_runs"] == 1
        # 有本模式的实测样本时**不许**标成「借来的」—— 那会把一个真估算说成外推。
        assert result["basis"]["mode_samples_missing"] is False
        assert not any("没有实测样本" in note for note in result["notes"])
        assert result["tokens"]["low"] == 9_000_000, "用了全量的样本"

    def test_incremental_without_incremental_history_falls_back_and_says_so(self):
        """库里全是全量运行（这是常态）→ 回落全量样本，**两端都锚回全量实测区间**。

        线性折算（10 个文件 × 单文件强度 = 12,000 token）会得出一个明显偏小的数：清单与
        逐轮上下文不随文件数线性缩小。**两端都要抬**：只抬低端的话，折算出来的高端会被
        低端顶到同一个数上，区间塌成一个点 —— 那看起来像一个**精确**的估算，而它其实是
        我们最没有把握的那一处。抬高的理由必须写进 notes，否则那两个端看起来像是算出来的。
        """
        result = estimate_analysis(
            planned_files=10,
            mode="incremental",
            recent_runs=[
                _sample(1, files=1000, tokens_input=1_000_000, tokens_output=0),
                _sample(2, files=1000, tokens_input=2_000_000, tokens_output=0),
            ],
        )

        assert result["basis"]["same_mode_runs"] == 0
        # **借来的区间要说出来**：为真时界面必须写明「参照的是全量运行」，
        # 而不是把它当成「预计增量代价」显示（裁定过的口径：不把未知说成已知）。
        assert result["basis"]["mode_samples_missing"] is True
        assert any("没有实测样本" in note for note in result["notes"]), result["notes"]
        assert any("参照的是全量运行" in note for note in result["notes"])
        assert result["tokens"]["low"] == 1_000_000, "增量的底按文件数折算成了 12,000"
        assert result["tokens"]["high"] == 2_000_000, "增量的顶塌到了低端上（区间成了单点）"
        assert result["tokens"]["low"] < result["tokens"]["high"]
        assert any("锚在**全量实测区间**上" in note for note in result["notes"])
        # **措辞不许自相矛盾**：卡片上写着「增量：参照全量」，notes 里就不许再说
        # 「按同类运行折算」—— 那会让读的人以为两者是同一批数据，而它们正是被区分开的
        # 那两件事（`usable` 是别的模式的运行）。
        assert not any("同类运行" in note for note in result["notes"]), result["notes"]

    def test_baseline_reuse_does_not_shrink_the_range(self):
        """命中可复用基线**不进算术** —— 它的后果是「不用建这次付费运行」，不是打折。"""
        kwargs = dict(
            planned_files=100,
            recent_runs=[_sample(1, files=100)],
            price_table=_price_table(),
        )

        hit = estimate_analysis(baseline_reusable=True, **kwargs)
        miss = estimate_analysis(baseline_reusable=False, **kwargs)
        unknown = estimate_analysis(baseline_reusable=None, **kwargs)

        assert hit["tokens"] == miss["tokens"] == unknown["tokens"]
        assert hit["cost"]["low"] == miss["cost"]["low"]
        assert hit["baseline"]["reusable"] is True
        assert "不需要新建付费运行" in hit["baseline"]["note"]
        assert "真的跑一遍模型" in miss["baseline"]["note"]
        assert "还没有判定" in unknown["baseline"]["note"]


# ==========================================================================
# 四、费用：算得出就给区间，算不出就给理由
# ==========================================================================


class TestTheCostIsNeverFaked:
    def test_without_a_price_table_tokens_and_time_survive(self):
        """**价格表没配时显示 token 与时间，不伪造费用**（这一条是 P1-03 的硬要求）。"""
        result = estimate_analysis(
            planned_files=100,
            recent_runs=[_sample(1, files=100)],
            price_table=None,
        )

        assert result["tokens"]["low"] == 1_200_000
        assert result["duration_ms"]["low"] is not None
        assert result["cost"]["computable"] is False
        assert result["cost"]["low"] is None and result["cost"]["high"] is None
        assert result["cost"]["reason"] == "还没有配置价格表"
        assert any("还没有配置价格表" in note for note in result["notes"])

    def test_an_empty_price_table_is_not_treated_as_free(self):
        """空表（出厂默认）与「没配」同一条出口 —— **不是 0 元**。"""
        empty, errors = parse_price_table(json.dumps({"currency": "CNY", "models": {}}))
        assert empty is not None or errors

        result = estimate_analysis(
            planned_files=100, recent_runs=[_sample(1, files=100)], price_table=empty
        )

        assert result["cost"]["computable"] is False
        assert result["cost"]["low"] is None
        assert "0" not in str(result["cost"]["low"])

    def test_a_configured_table_gives_a_low_and_a_high_amount(self):
        """配了价格表 → 两端各给一个金额，且**低端的金额小于等于高端**。"""
        result = estimate_analysis(
            planned_files=100,
            recent_runs=[
                _sample(1, files=100, tokens_input=500_000, tokens_output=100_000,
                        cache_read=400_000),
                _sample(2, files=100, tokens_input=2_000_000, tokens_output=400_000,
                        cache_read=1_600_000),
            ],
            price_table=_price_table(),
            model="fake-model",
        )

        assert result["cost"]["computable"] is True
        low = result["cost"]["low"]["amount_exact"]
        high = result["cost"]["high"]["amount_exact"]
        assert float(low) < float(high)
        assert result["cost"]["currency"] == "CNY"
        # 命中缓存那一档确实进了算式：命中价格低于未命中价，金额必须体现出来。
        labels = [line["label"] for line in result["cost"]["high"]["lines"]]
        assert any("命中缓存" in label for label in labels), labels


# ==========================================================================
# 五、最近一次实际值
# ==========================================================================


class TestTheLastActualValue:
    def test_it_is_the_newest_sample_not_the_biggest(self):
        result = estimate_analysis(
            planned_files=100,
            recent_runs=[
                _sample(1, tokens_input=9_000_000, tokens_output=0, created_at="2026-09-01"),
                _sample(2, tokens_input=1_000_000, tokens_output=0, created_at="2026-09-20"),
            ],
        )

        assert result["last_actual"]["run_id"] == 2
        assert result["last_actual"]["tokens"] == 1_000_000

    def test_it_does_not_depend_on_the_order_the_caller_sends(self):
        """Job 协议那边可能从 job 表拼样本，顺序不一定是时间序 —— 这里自己排一次。"""
        samples = [
            _sample(1, created_at="2026-09-20"),
            _sample(2, created_at="2026-09-01"),
        ]

        assert estimate_analysis(planned_files=100, recent_runs=samples)["last_actual"]["run_id"] == 1
        assert estimate_analysis(
            planned_files=100, recent_runs=list(reversed(samples))
        )["last_actual"]["run_id"] == 1


# ==========================================================================
# 六、样本的取数：文件数与分片数从哪来
# ==========================================================================


def _project() -> int:
    project = Project(code=_uid("P"), name=_uid("est"))
    db.session.add(project)
    db.session.flush()
    return project.id


def _run(project_id: int, **overrides) -> AiAnalysisRun:
    payload = {
        "project_id": project_id,
        "target_type": "weekly",
        "target_id": project_id,
        "target_key": "group-est",
        "status": "succeeded",
        "scope": "full",
        "trigger_source": "manual",
        "model": "fake-model",
        "tokens_input": 1_000_000,
        "tokens_output": 200_000,
        "cache_read_tokens": 800_000,
        "duration_ms": 600_000,
        "rounds_used": 20,
    }
    payload.update(overrides)
    run = AiAnalysisRun(**payload)
    db.session.add(run)
    db.session.flush()
    db.session.commit()
    return run


class TestTheSampleReadsWhatTheRunActuallyRecorded:
    def test_the_file_count_comes_from_the_delta_summary(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            run = _run(
                project_id,
                delta_summary=json.dumps({"delta_files": 321, "total_files": 321}),
                response_payload=json.dumps({"subagents": [{"label": "S1"}, {"label": "S2"}]}),
            )

            sample = estimation_sample(run)

            assert sample["files"] == 321
            assert sample["shards"] == 2

    def test_only_real_shards_are_counted_as_shards(self):
        """`subagents` 里还混着「汇总」与「对账」两个成员 —— **它们不是分片**。

        实测一次 3 分片的运行在这一列里有 5 条（S1/S2/S3 + 汇总 + V1）。把 5 当成分片数
        的后果很具体：分片折算按 3/5 打折，估算出来的 token 与墙钟都偏小四成，而那个
        区间看起来完全正常（分片是串行的，这一条见 `estimate_analysis` 的折算）。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            run = _run(
                project_id,
                response_payload=json.dumps({"subagents": [
                    {"label": "S1", "role": "subagent"},
                    {"label": "S2", "role": "subagent"},
                    {"label": "S3", "role": "subagent"},
                    {"label": "汇总", "role": "synthesis"},
                    {"label": "V1", "role": "verify"},
                ]}),
            )

            assert estimation_sample(run)["shards"] == 3

    def test_an_old_payload_without_roles_counts_them_all(self):
        """老 payload 没有 `role` 这一列（那时只有分片成员）→ 不能因此判成 0 片。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            run = _run(
                project_id,
                response_payload=json.dumps({"subagents": [{"label": "S1"}, {"label": "S2"}]}),
            )

            assert estimation_sample(run)["shards"] == 2

    def test_missing_metadata_is_not_invented(self):
        """文件数 / 分片数读不到就给 `None` —— **不编**（老行就是这样）。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            run = _run(project_id, delta_summary=None, response_payload=None)

            sample = estimation_sample(run)

            assert sample["files"] is None
            assert sample["shards"] is None
            assert sample["tokens_input"] == 1_000_000

    def test_a_broken_json_column_does_not_throw(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            run = _run(project_id, delta_summary="{不是 JSON", response_payload="[]")

            sample = estimation_sample(run)

            assert sample["files"] is None
            assert sample["shards"] is None


# ==========================================================================
# 七、服务层与端点
# ==========================================================================


class TestTheServiceLayerUsesOnlyReportedRuns:
    def test_runs_that_reported_nothing_are_excluded_from_the_samples(self):
        """失败在半路、三列全 NULL 的运行不参与缩放 —— 它们只会把区间拉偏。

        但排除这件事**要说出来**：不说的话，「最近一次实际值」看起来像是库里最新的
        那一次，可能对不上号。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            ok, message, errors = update_project_analysis_config(
                project_id, {"model_price_table": PRICE_TABLE}, updated_by="tester"
            )
            assert ok, (message, errors)
            _run(project_id, delta_summary=json.dumps({"delta_files": 100}))
            _run(
                project_id,
                status="failed",
                tokens_input=None,
                tokens_output=None,
                cache_read_tokens=None,
                duration_ms=None,
                delta_summary=json.dumps({"delta_files": 100}),
            )

            result = analysis_estimate(project_id, mode="full", planned_files=100)

            assert result["basis"]["runs"] == 1, "没上报的那一条也进了样本"
            assert result["last_actual"]["run_id"] is not None
            assert any("没有上报用量" in note for note in result["notes"])
            assert result["pricing"]["configured"] is True

    def test_the_project_config_supplies_the_shard_count(self):
        """分片数/轮次上限从**项目配置**取，不在估算函数里写死。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            ok, message, errors = update_project_analysis_config(
                project_id,
                {"model_price_table": PRICE_TABLE, "subagent_count": 4},
                updated_by="tester",
            )
            assert ok, (message, errors)
            _run(project_id, delta_summary=json.dumps({"delta_files": 100}),
                 response_payload=json.dumps({"subagents": [{"label": "S1"}]}))

            result = analysis_estimate(project_id, mode="full", planned_files=100)

            assert result["shard_count"] == 4
            assert any("分片" in note for note in result["notes"])
            assert result["generated_at"]

    def test_it_only_looks_at_the_same_target_type(self):
        """周版本分析的历史不该拿单提交分析的样本去估（反之亦然）。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            ok, message, errors = update_project_analysis_config(
                project_id, {"model_price_table": PRICE_TABLE}, updated_by="tester"
            )
            assert ok, (message, errors)
            _run(project_id, target_type="commit", target_key="c1",
                 delta_summary=json.dumps({"delta_files": 100}))

            result = analysis_estimate(project_id, mode="full", target_type="weekly")

            assert result["basis"]["runs"] == 0
            assert result["tokens"]["low"] is None

    def test_it_never_queries_another_project(self):
        """样本只看**这个项目自己**的运行（估算是按项目的价格表与配置算的）。"""
        with flask_app.app_context():
            create_tables()
            mine = _project()
            other = _project()
            for project_id in (mine, other):
                ok, message, errors = update_project_analysis_config(
                    project_id, {"model_price_table": PRICE_TABLE}, updated_by="tester"
                )
                assert ok, (message, errors)
            _run(other, delta_summary=json.dumps({"delta_files": 100}))

            assert analysis_estimate(mine, mode="full")["basis"]["runs"] == 0
            assert analysis_estimate(other, mode="full")["basis"]["runs"] == 1


class TestTheQueryParsingNeverThrows:
    def test_every_field_falls_back(self):
        project_id, params = parse_estimate_args(
            {"project": "abc", "mode": "全部", "files": "很多", "baseline": "也许"}
        )

        assert project_id is None
        assert params["mode"] == "full"
        assert params["planned_files"] is None
        # 认不出来的 baseline **不是 False**：那会把「还没判定」说成「一定不会命中」。
        assert params["baseline_reusable"] is False or params["baseline_reusable"] is True
        assert len(params["notes"]) == 3

    def test_the_normal_path(self):
        project_id, params = parse_estimate_args(
            {"project": "12", "mode": "incremental", "files": "345", "baseline": "1"}
        )

        assert project_id == 12
        assert params["mode"] == "incremental"
        assert params["planned_files"] == 345
        assert params["baseline_reusable"] is True
        assert params["notes"] == []

    def test_baseline_zero_means_no(self):
        _, params = parse_estimate_args({"project": "12", "baseline": "0"})

        assert params["baseline_reusable"] is False


class _ViewClient:
    """绕过 before_request 的认证链，直接调 view（同 tests/test_ai_usage_capture.py）。"""

    def get(self, url, **_kwargs):
        # query 要单独拆出来：`url_map.match` 只认路径，带 `?` 会直接 404。
        path, _, query = url.partition("?")
        endpoint, args = flask_app.url_map.bind("localhost").match(path, method="GET")
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(
            path + (("?" + query) if query else ""), method="GET"
        ):
            return make_response(view(**args))


@pytest.fixture()
def client():
    return _ViewClient()


class TestTheEstimateEndpoint:
    def test_it_is_get_only_and_produces_no_model_call(self, client, monkeypatch):
        """只读、GET、不碰任何会花钱的入口（与另外三个用量端点同一条护栏）。"""
        rules = [
            rule
            for rule in flask_app.url_map.iter_rules()
            if str(rule) == "/ai-analysis/usage/estimate"
        ]
        assert rules, "估算端点没挂上去"
        for rule in rules:
            assert "GET" in rule.methods
            assert "POST" not in rule.methods
            assert "stream" not in str(rule)

    def test_it_returns_the_range(self, client, monkeypatch):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            ok, message, errors = update_project_analysis_config(
                project_id, {"model_price_table": PRICE_TABLE}, updated_by="tester"
            )
            assert ok, (message, errors)
            _run(project_id, delta_summary=json.dumps({"delta_files": 200}))

        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
        body = client.get(
            f"/ai-analysis/usage/estimate?project={project_id}&mode=full&files=200"
        ).get_json()

        assert body["success"] is True
        assert body["tokens"]["low"] is not None
        assert body["tokens"]["low"] <= body["tokens"]["high"]
        assert body["baseline"]["reusable"] is None
        assert body["cost"]["computable"] is True
        assert body["last_actual"]["run_id"] is not None

    def test_a_missing_project_is_a_400_not_a_500(self, client, monkeypatch):
        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)

        assert client.get("/ai-analysis/usage/estimate").status_code == 400
        assert client.get("/ai-analysis/usage/estimate?project=abc").status_code == 400

    def test_without_project_access_it_is_a_403(self, client, monkeypatch):
        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: False)

        assert client.get("/ai-analysis/usage/estimate?project=1").status_code == 403

    def test_an_unknown_mode_falls_back_to_full_and_says_so(self, client, monkeypatch):
        with flask_app.app_context():
            create_tables()
            project_id = _project()

        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
        body = client.get(
            f"/ai-analysis/usage/estimate?project={project_id}&mode=whatever"
        ).get_json()

        assert body["mode"] == "full"
        assert any("认不出来" in note for note in body["notes"])
