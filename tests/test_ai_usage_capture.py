# -*- coding: utf-8 -*-
"""AI 用量采集与三个只读端点。

## 这个文件守的四条性质

1. **`None`（上游没报）与 `0`（报了且是 0）在库里就是两回事。** 落库时 `None` 必须
   保持 NULL；把它写成 0 之后，读取侧再也分不出「没命中」与「没上报」——而这两者
   在面板上的含义正好相反（0% 是结论，未知不是）。
2. **历史行（上线前）读出来是「未采集」，不是 0。** 那批行的用量列全是 NULL。
3. **聚合按周版本分组**（`target_key` = group_key），且**不会把单提交的分析混进来** ——
   混进来的话「这个周版本花了多少」会莫名偏大，而没有任何一行显示出问题。
4. **费用只在配了价格表时出现**，算不出就是 `None` + 一句理由，绝不回落成 0。

端点权限与既有 AI 路由一致（有项目权限就能看，不用平台管理员），所以这里也断言
「没有项目权限时 403」——包括**按运行 id 取明细**那条（它必须用运行自己的 project_id
判权限，不能信 URL 里的项目号）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai.usage import cache_hit_rate, usage_from_run
from services.ai_usage_service import project_usage, run_usage, usage_overview

PRICE_TABLE = json.dumps(
    {
        "version": "test-1",
        "currency": "CNY",
        "models": {"fake-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2}},
    }
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _project() -> int:
    project = Project(code=_uid("P"), name=_uid("usage"))
    db.session.add(project)
    db.session.flush()
    return project.id


def _run(
    project_id: int,
    *,
    target_type: str = "weekly",
    target_id: int = 1,
    target_key: str | None = "group-a",
    model: str = "fake-model",
    tokens_input: int | None = 1000,
    tokens_output: int | None = 200,
    cache_read: int | None = 900,
    with_cost_version: bool = True,
) -> AiAnalysisRun:
    run = AiAnalysisRun(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        status="succeeded",
        scope="full",
        trigger_source="manual",
        response_text="结论",
        model=model,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cache_read_tokens=cache_read,
        cache_source="prompt_tokens_details" if cache_read is not None else None,
        duration_ms=1234,
        context_chars=4096,
        tool_requests_used=3,
        tool_stats_json=json.dumps({"file_diff": {"calls": 2, "executions": 2}}),
        pricing_version="test-1" if with_cost_version else None,
    )
    db.session.add(run)
    db.session.flush()
    # 提交：后面的端点测试会开新的 app context（新 session），没提交的行读不到。
    db.session.commit()
    return run


def _legacy_run(project_id: int) -> AiAnalysisRun:
    """用量功能上线前的行：**用量那几列全是 NULL**。

    注意 `duration_ms` / `context_chars` / `tool_stats_json` 也要一起清掉 ——
    `usage_from_run` 判「有没有采集到用量」看的正是这几列，只清 token 的话
    它仍会被判成「已采集」（这个坑第一版就踩了）。
    """
    run = _run(
        project_id,
        target_type="commit",
        target_id=77,
        target_key=None,
        tokens_input=None,
        tokens_output=None,
        cache_read=None,
    )
    run.duration_ms = None
    run.context_chars = None
    run.tool_requests_used = None
    run.tool_stats_json = None
    run.cache_source = None
    run.pricing_version = None
    db.session.commit()
    return run


def _set_price_table(project_id: int, raw: str) -> None:
    from services.ai_analysis_service import update_project_analysis_config

    ok, message, errors = update_project_analysis_config(
        project_id, {"model_price_table": raw}, updated_by="tester"
    )
    assert ok, f"价格表没存进去：{message} {errors}"


# ==========================================================================
# 一、落库：NULL 与 0 必须分得开
# ==========================================================================


def test_persist_keeps_unreported_cache_tokens_as_null():
    """**核心回归**：上游没报缓存 → 列是 NULL，不是 0。"""
    from services.ai.engine import EngineOutcome, RoundRecord
    from services.ai_analysis_service import _persist_outcome

    with flask_app.app_context():
        create_tables()
        project_id = _project()
        run = _run(project_id, cache_read=None, tokens_input=500, tokens_output=100)
        outcome = EngineOutcome(
            status="succeeded",
            report_markdown="报告",
            rounds=(RoundRecord(index=1, status="final", prompt_tokens=500, completion_tokens=100),),
            prompt_tokens=500,
            completion_tokens=100,
            cache_read_tokens=None,
            cache_write_tokens=None,
            requests_used=2,
            context_chars=1234,
            duration_ms=999,
            tool_stats={"file_diff": {"calls": 1, "executions": 1}},
        )
        _persist_outcome(run, outcome, {"anomalies": [], "risk_level": "low"}, pricing_version="v9")
        db.session.commit()

        assert run.cache_read_tokens is None, "「未上报」被写成了 0 —— 读取侧再也分不出差别"
        assert run.cache_write_tokens is None
        assert run.duration_ms == 999
        assert run.context_chars == 1234
        assert run.tool_requests_used == 2
        assert run.tool_stats_json is not None
        assert run.pricing_version == "v9"
        assert run.dropped_count == 0
        assert run.anomalies_found == 0

        trace = AiAnalysisTrace.query.filter_by(run_id=run.id).one()
        assert trace.tokens_input == 500
        assert trace.tokens_output == 100
        assert trace.duration_ms == 0 or trace.duration_ms is not None, (
            "逐轮耗时要落库（0 是合法值，None 表示没写）"
        )


def test_persist_writes_the_round_evidence_and_the_detail_shows_it(client, monkeypatch):
    """逐轮的**证据**要落库，并且从「本次消耗」那个接口读得回来（2026-09-19 补）。

    原先只落计数，于是「这次为什么没读到 X」在面板上无从查起：模型没要、取数失败、
    被预算拒了，三种可能在三列计数上长得一模一样。这一条从引擎的 `RoundRecord` 一路
    走到接口响应，中间任何一环漏写都会红。
    """
    from services.ai.budget import ContextItem
    from services.ai.engine import EngineOutcome, RoundRecord
    from services.ai.protocol import ContextRequest, DroppedItem
    from services.ai_analysis_service import _persist_outcome

    with flask_app.app_context():
        create_tables()
        project_id = _project()
        run = _run(project_id, cache_read=None, tokens_input=500, tokens_output=100)
        outcome = EngineOutcome(
            status="succeeded",
            report_markdown="报告",
            rounds=(
                RoundRecord(
                    index=1, status="requests", request_count=2, item_count=1,
                    requests=(ContextRequest("file_diff", "a" * 40, "code/a.lua"),),
                    # 取不到时 provider 给的是一句完整的话：看着像内容，字数也不为 0
                    executed=(ContextItem(
                        "file_diff", "file_diff code/a.lua",
                        "[取数失败] code/a.lua：项目没有绑定 Agent 节点。",
                    ),),
                    dropped=(DroppedItem("request", 1, "超出本次工具请求总预算（20 次），未执行"),),
                    response_text='{"status": "need_more_context"}',
                    budget_notes=("有 1 个上下文请求因超出本次索取额度而未执行。",),
                ),
                RoundRecord(index=2, status="final", response_text='{"status": "final"}'),
            ),
            prompt_tokens=500, completion_tokens=100,
        )
        _persist_outcome(run, outcome, {"anomalies": [], "risk_level": "low"})
        db.session.commit()
        run_id = run.id

        first = AiAnalysisTrace.query.filter_by(run_id=run.id, round_index=1).one()
        assert "need_more_context" in (first.response_text or "")
        assert "超出本次索取额度" in (first.budget_notes or "")
        assert "未执行" in (first.dropped_json or "")

    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
    body = client.get(f"/ai-analysis/runs/{run_id}/usage").get_json()

    rounds = {row["round_index"]: row for row in body["rounds"]}
    assert rounds[1]["requests"][0]["path"] == "code/a.lua"
    executed = rounds[1]["executed"][0]
    assert executed["failed"] is True, "取数失败在接口上又变回「读到了内容」了"
    assert "没有绑定 Agent" in executed["reason"], executed
    assert rounds[1]["dropped"][0]["reason"].endswith("未执行")
    assert "need_more_context" in rounds[1]["response_text"]
    # 老读法（计数）不受影响
    assert rounds[1]["outcome"] == "requests" and rounds[2]["outcome"] == "final"


def test_persist_keeps_a_reported_zero_as_zero():
    """反向自检：上游报了 0 就必须存 0，不能被当成「没报」抹成 NULL。"""
    from services.ai.engine import EngineOutcome
    from services.ai_analysis_service import _persist_outcome

    with flask_app.app_context():
        create_tables()
        project_id = _project()
        run = _run(project_id, cache_read=None)
        outcome = EngineOutcome(
            status="succeeded",
            report_markdown="报告",
            prompt_tokens=800,
            completion_tokens=50,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        _persist_outcome(run, outcome, {"anomalies": []})
        db.session.commit()

        assert run.cache_read_tokens == 0
        assert run.cache_write_tokens == 0


# ==========================================================================
# 二、读取：历史行是「未采集」，不是 0
# ==========================================================================


def test_a_legacy_run_reads_as_not_collected():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        run = _legacy_run(project_id)

        usage = usage_from_run(run)

        assert usage["collected"] is False, "上线前的历史运行被当成了「有数据」"
        assert usage["tokens"]["total"] is None
        assert usage["cache"]["hit_rate"] is None
        assert usage["cost"] is None


def test_a_captured_run_reads_with_numbers_and_no_cost_without_a_table():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        run = _run(project_id)

        usage = usage_from_run(run)

        assert usage["collected"] is True
        assert usage["tokens"]["total"] == 1200
        assert usage["cache"]["hit_rate"] == pytest.approx(0.9)
        # 没有价格表 → 不给金额（**不是 0**）
        assert usage["cost"] is None


def test_the_hit_rate_is_none_when_the_cache_column_is_null():
    """有输入 token、但缓存列是 NULL：命中率必须是 None（未上报），不是 0%。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        run = _run(project_id, cache_read=None, tokens_input=1000, tokens_output=100)

        usage = usage_from_run(run)

        assert usage["cache"]["hit_rate"] is None
        assert usage["tokens"]["input"] == 1000


def test_the_hit_rate_uses_the_same_denominator_as_the_cost_estimate():
    """分母是**输入总数**（`prompt_tokens`），与 `pricing.estimate_cost` 认的那个一致。

    两处口径不一致的后果是一句自相矛盾的展示：「命中率 90%，费用却按全价算」。
    OpenAI / DeepSeek 的 `prompt_tokens` 已经含命中部分
    （`prompt_cache_hit_tokens + prompt_cache_miss_tokens == prompt_tokens`，
    在本机网关上实测过），所以两者本来就是一回事。
    """
    from services.ai.pricing import ModelPrice, PriceTable, estimate_cost

    table = PriceTable(
        models={"m": ModelPrice(input_per_million=Decimal("2"), output_per_million=Decimal("8"),
                                cache_read_per_million=Decimal("0.2"))},
        version="v1",
    )
    cost = estimate_cost("m", tokens_input=1000, tokens_output=100, cache_read=900, table=table)

    assert cache_hit_rate(900, 1000) == pytest.approx(0.9)
    # 费用那边把 1000 拆成「900 命中 + 100 未命中」：900 * 0.2/M + 100 * 2/M + ...
    assert cost.lines[0].tokens == 100, "未命中那一档要减掉命中数（分母含命中）"
    assert cost.lines[1].tokens == 900


@pytest.mark.parametrize("hit,total", [(900, 100), (1, 0), (None, 100), (100, None), (-1, 100)])
def test_an_impossible_hit_rate_is_reported_as_unreported(hit, total):
    """**命中率不许超过 100%。**

    超过 100% 只可能来自「上游给的两个字段不是同一口径」—— 已知的一种是 Anthropic 风格：
    `usage.input_tokens` 不含命中部分，而 `prompt_tokens` 是按 OpenAI/DeepSeek 口径读的。
    真拿它做比例会算出「命中率 900%」，那比「未上报」更容易误导：用户会以为是我们算错了。

    （费用那一侧对同样的数据按「全部命中」处理并带一句说明 —— 金额可以挂一句「我做了
    假设」，百分比没有地方挂那句话。）
    """
    assert cache_hit_rate(hit, total) is None


# ==========================================================================
# 三、费用：配了价格表才算，算不出不给 0
# ==========================================================================


def test_cost_appears_only_when_a_price_table_is_configured():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        # 用量放大到「金额够 2 位小数展示」的量级（见 pricing.money 的展示口径）。
        #
        # 原来这里用默认的 1000/200/900：金额 0.00198 元，展示层量化到分之后是 `<0.01`，
        # 而 `<0.01` **分辨不出**「命中档按 0.2/M 单独计价」（0.00198）与「命中档被并进
        # 未命中价」（0.002）—— 那正是这条断言要守的东西。放大之后两档差出 1.62 元，
        # 断言重新变得有区分力，而不是被放宽成「有个数就行」。
        run = _run(project_id, tokens_input=1_000_000, tokens_output=200_000, cache_read=900_000)

        assert usage_from_run(run, price_table=None)["cost"] is None

        _set_price_table(project_id, PRICE_TABLE)
        from services.ai_analysis_service import project_price_table

        table, errors = project_price_table(project_id)
        assert table is not None, errors
        usage = usage_from_run(run, price_table=table)

        assert usage["cost"]["amount"] is not None
        assert usage["cost"]["currency"] == "CNY"
        # 900k 命中按 0.2/M、100k 未命中按 2/M、200k 输出按 8/M
        # = 0.18 + 0.20 + 1.60 = 1.98（若命中被并进未命中价则是 3.60）
        assert usage["cost"]["amount"] == "1.98", usage["cost"]
        labels = [line["label"] for line in usage["cost"]["lines"]]
        assert "输入（命中缓存）" in labels, "命中那一档被合进了未命中价"


def test_a_sub_cent_amount_does_not_break_the_aggregation():
    """**不足一分钱曾经把只读面板与预算闸门一起打成 500。**

    `pricing.money()` 对「非零但不足一分」给的是展示串 `<0.01`（那是刻意的：显示
    `0.00` 会被读成「这次没花钱」），而聚合（`usage._sum_costs`）原本是拿它
    `Decimal(...)` 回去当数字加的 —— 那会抛 `InvalidOperation`。预算闸门那条链也走这个
    聚合，而 `analysis_budget` 的 docstring 明写「**这个函数不抛异常**」。

    这里同时钉住另一半：合计读的是**未舍入**的值。两次真实的 0.014 元相加是 0.028；
    拿已经量化到分的展示值相加会得到 0.02（少算一分钱，而且看不出来）。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _set_price_table(project_id, PRICE_TABLE)
        # 默认的那组数是 1000/200/900 → 每次 0.00198 元，正是「不足一分」那一档。
        _run(project_id)
        _run(project_id)

        from services.ai.analysis_budget import budget_status
        from services.ai.usage import aggregate_runs
        from services.ai_analysis_service import project_price_table

        table, errors = project_price_table(project_id)
        assert table is not None, errors
        runs = AiAnalysisRun.query.filter_by(project_id=project_id).all()
        stats = aggregate_runs(runs, price_table=table)

        cost = stats["cost"]
        assert cost is not None, "取不回金额只能说「算不出」，不许抛异常"
        assert cost["amount"] == "<0.01", "两次不足一分的运行合计仍不足一分"
        assert cost["amount_exact"] == "0.00396", (
            "合计要读未舍入的值：0.00198 × 2 = 0.00396（拿展示值相加会丢精度）"
        )
        # 闸门那条链：配了费用上限也不许抛（它是「算不出就不拦」，不是「算不出就炸」）。
        assert isinstance(budget_status(project_id), dict)


def test_an_unmatched_model_gives_no_amount_and_a_reason():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _set_price_table(project_id, PRICE_TABLE)
        run = _run(project_id, model="别的模型")

        from services.ai_analysis_service import project_price_table

        table, _errors = project_price_table(project_id)
        usage = usage_from_run(run, price_table=table)

        assert usage["cost"]["amount"] is None
        assert "价格表里没有匹配" in usage["cost"]["reason"]


def test_a_broken_price_table_is_rejected_at_save_time_not_at_display_time():
    """价格表 JSON 坏掉时**保存就报错**（字段级），不是等面板上算不出费用才发现。"""
    from services.ai_analysis_service import update_project_analysis_config

    with flask_app.app_context():
        create_tables()
        project_id = _project()

        ok, _message, errors = update_project_analysis_config(
            project_id, {"model_price_table": '{"models": {"m": {"input": 1, }}}'}
        )
        assert ok is False
        assert errors and errors[0]["field"] == "model_price_table"

        # 单位写错（input → inpu）会让那一档静默按 0 计价，必须也拦住
        ok, _message, errors = update_project_analysis_config(
            project_id, {"model_price_table": '{"version": "v", "models": {"m": {"inpu": 1, "output": 2}}}'}
        )
        assert ok is False, "键名写错却存进去了：那一档会静默按 0 算"


# ==========================================================================
# 四、聚合：按周版本分组，不混入单提交
# ==========================================================================


def test_weekly_groups_do_not_mix_in_commit_runs():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _run(project_id, target_key="group-a", tokens_input=1000, tokens_output=100)
        _run(project_id, target_key="group-a", tokens_input=2000, tokens_output=100)
        _run(project_id, target_key="group-b", tokens_input=500, tokens_output=50)
        _run(project_id, target_type="commit", target_id=9, target_key=None,
             tokens_input=7000, tokens_output=700)

        payload = project_usage(project_id)

        groups = {item["group_key"]: item for item in payload["weekly_versions"]}
        assert set(groups) == {"group-a", "group-b"}
        assert groups["group-a"]["tokens"]["total"] == 3200, "同一 group 的多次运行要相加"
        assert groups["group-b"]["tokens"]["total"] == 550
        # 单提交那次不该出现在周版本里
        assert all("commit" not in (item["group_key"] or "") for item in payload["weekly_versions"])
        # 但它要算进项目合计
        assert payload["totals"]["tokens"]["total"] == 3200 + 550 + 7700


def test_the_latest_weekly_group_is_marked_and_first():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        older = _run(project_id, target_key="old-group", tokens_input=10, tokens_output=1)
        newer = _run(project_id, target_key="new-group", tokens_input=20, tokens_output=2)
        older.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer.created_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.session.commit()

        payload = project_usage(project_id)

        assert payload["weekly_versions"][0]["group_key"] == "new-group"
        assert payload["weekly_versions"][0]["is_latest"] is True
        assert payload["weekly_versions"][1]["is_latest"] is False


def test_partial_missing_values_are_counted_not_silently_skipped():
    """部分运行没上报缓存：合计照实相加上报的那些，并给出缺失条数；命中率**不给**。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _run(project_id, target_key="g", tokens_input=1000, tokens_output=100, cache_read=800)
        _run(project_id, target_key="g", tokens_input=500, tokens_output=50, cache_read=None)

        payload = project_usage(project_id)
        group = payload["weekly_versions"][0]

        assert group["tokens"]["input"] == 1500
        assert group["tokens"]["cache_read"] == 800
        assert group["missing_runs"]["cache_read"] == 1
        assert group["cache"]["hit_rate"] is None, (
            "有一条没上报却算了比例 —— 那是个偏低的假数字，比「未上报」更容易误导"
        )


# ==========================================================================
# 五、端点：形状、空态、权限
# ==========================================================================


class _ViewClient:
    """绕过 before_request 的认证链，直接调 view（同 tests/test_ai_config_routes.py）。"""

    def get(self, url, **_kwargs):
        endpoint, args = flask_app.url_map.bind("localhost").match(url, method="GET")
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(url, method="GET"):
            return make_response(view(**args))


@pytest.fixture()
def client():
    return _ViewClient()


def test_the_overview_reports_empty_state_instead_of_failing(client, monkeypatch):
    """有项目、但一次分析都没跑过 —— 面板要能渲染空态，而不是报错或显示 0 元。

    **必须限定到一个新建的空项目上。** 测试库是整个会话共用的（`tests/conftest.py`
    没有逐用例重置），`_get_accessible_project_ids → None` 会把别的用例留下的运行
    也算进来，「合计 = 0」这种断言于是看跑法红绿（这条第一版就是这么写的，实测拿到
    15 次运行）。这里断言的仍然是本用例真正关心的事：**没有数据的项目**长什么样。
    """
    with flask_app.app_context():
        create_tables()
        empty_project = _project()

    monkeypatch.setattr(ai_routes, "_get_accessible_project_ids", lambda: [empty_project])
    resp = client.get("/ai-analysis/usage/overview")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["projects"] == []
    assert body["totals"]["runs"] == 0
    assert body["totals"]["cost"] is None
    assert body["generated_at"]


def test_the_overview_filters_by_accessible_projects(client, monkeypatch):
    """只能看到自己有权访问的项目 —— 而且是在**查询级**过滤（不是查完再筛）。"""
    with flask_app.app_context():
        create_tables()
        mine = _project()
        other = _project()
        _run(mine, target_key="g", tokens_input=100, tokens_output=10)
        _run(other, target_key="g", tokens_input=999, tokens_output=99)

    monkeypatch.setattr(ai_routes, "_get_accessible_project_ids", lambda: [mine])
    body = client.get("/ai-analysis/usage/overview").get_json()

    assert [item["project_id"] for item in body["projects"]] == [mine]
    assert body["totals"]["tokens"]["input"] == 100, "无权项目的用量混进了合计"


def test_the_overview_with_no_accessible_project_returns_an_empty_page(client, monkeypatch):
    monkeypatch.setattr(ai_routes, "_get_accessible_project_ids", lambda: [])
    body = client.get("/ai-analysis/usage/overview").get_json()

    assert body["success"] is True
    assert body["projects"] == []


def test_a_project_without_access_is_denied(client, monkeypatch):
    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: False)
    resp = client.get("/ai-analysis/usage/project/123")

    assert resp.status_code == 403
    assert resp.get_json()["success"] is False


def test_a_run_detail_is_denied_by_the_runs_own_project(client, monkeypatch):
    """**按运行自己的 project_id 判权限**，不信 URL 里的项目号。"""
    with flask_app.app_context():
        create_tables()
        owner = _project()
        run_id = _run(owner, target_key="g").id

    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: False)
    resp = client.get(f"/ai-analysis/runs/{run_id}/usage")
    assert resp.status_code == 403


def test_the_run_detail_includes_rounds(client, monkeypatch):
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _set_price_table(project_id, PRICE_TABLE)
        # 同上：放大到金额能被 2 位小数分辨的量级。
        run = _run(
            project_id, target_key="g", tokens_input=1_000_000, tokens_output=200_000,
            cache_read=900_000,
        )
        run_id = run.id
        db.session.add(
            AiAnalysisTrace(
                run_id=run_id, round_index=1, outcome="final", tokens_input=1_000_000,
                tokens_output=200_000, cache_read_tokens=900_000, request_chars=5000,
                context_chars=4000, duration_ms=800,
            )
        )
        db.session.commit()

    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
    body = client.get(f"/ai-analysis/runs/{run_id}/usage").get_json()

    assert body["success"] is True
    assert body["run"]["usage"]["tokens"]["total"] == 1_200_000
    assert body["run"]["usage"]["cost"]["amount"] == "1.98"
    assert [row["round_index"] for row in body["rounds"]] == [1]
    assert body["rounds"][0]["cache_read_tokens"] == 900_000


def test_a_missing_run_returns_404_not_500(client, monkeypatch):
    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
    resp = client.get("/ai-analysis/runs/999999/usage")
    assert resp.status_code == 404


def test_the_three_endpoints_are_read_only(client):
    """三个端点都必须是 GET，且**不能**碰会真花钱的分析入口。

    这条挡的是「顺手在面板上放一个『重新分析』」——那会让一个只读页面产生计费调用。
    """
    rules = [
        rule
        for rule in flask_app.url_map.iter_rules()
        if "/ai-analysis/usage" in str(rule) or "/usage" in str(rule)
    ]
    assert rules, "找不到用量端点"
    for rule in rules:
        assert rule.methods is not None
        assert "GET" in rule.methods
        assert "POST" not in rule.methods
        assert "stream" not in str(rule), f"{rule} 命中了分析流式入口"
