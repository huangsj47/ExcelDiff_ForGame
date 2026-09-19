# -*- coding: utf-8 -*-
"""平台总预算（全平台合计的上限）与「筛选范围 vs 预算周期」的口径联动。

## 这个文件守的五条性质

1. **平台档没配时，一切与改动前逐字一致。** 平台没设上限是一件不需要在每一个项目上
   说一遍的事；如果它顺手往 `notes` 里塞一句「平台没有配置总预算」，每个项目的
   预算说明都会被一句无关的话稀释，而 `reason` 也会跟着变。
2. **两档是叠加的，不是二选一。** 任意一档超了就挡住下一次分析；`over_scopes` 说清
   是哪一档。项目档管不住总量（30 个项目各设 100 元，账单一万），平台档又会让一个项目
   吃掉整个额度 —— 两个问题不同，所以两档都要有。
3. **平台档沿用同样的三条纪律**：token 用已上报部分的下界、费用算不出就不判、不抛异常。
   其中费用那一档多一条：**按项目分组、各用各的价格表**，并且要求「每组都算得出、
   币种一致」才给合计（混币种相加比没有数字更危险）。
4. **「两个本月」必须能被说清。** 筛选的「本月」决定表里列出哪些运行，预算的「本月」
   永远按各项目自己的周期算（它必须与闸门判定逐字一致）。口径不一致时给出**一键对齐**
   的目标；各项目周期不一致时**没有**目标 —— 硬选一个会让界面看起来「已经对齐了」。
5. **「平台合计」只有平台管理员能看、也只有平台管理员能改。** 它是运营数字：把所有人
   的花费加起来，能反推出别的项目烧了多少钱。所以读与写同一个闸门，而且**总览里的那一份
   也要一并收掉**（只堵一个端点等于假装挡住）；项目档自己的数照常下发。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun, AiAnalysisTrace, AiPlatformBudget
from services.ai.analysis_budget import (
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    budget_gate_reason,
    budget_status,
    platform_budget_status,
)
from services.ai.platform_budget import (
    get_platform_budget,
    platform_budget_public,
    set_platform_budget,
)
from services.ai_analysis_service import update_project_analysis_config
from services.ai_usage_service import (
    RANGE_ALL,
    RANGE_THIS_MONTH,
    RANGE_THIS_WEEK,
    UsageFilters,
    budget_period_range,
    parse_usage_filters,
    period_alignment,
    project_usage,
    usage_overview,
)

PRICE_TABLE = json.dumps(
    {
        "version": "t-1",
        "currency": "CNY",
        "models": {"fake-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2}},
    }
)
OTHER_CURRENCY_TABLE = json.dumps(
    {
        "version": "t-1",
        "currency": "USD",
        "models": {"fake-model": {"input": 2.0, "output": 8.0}},
    }
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _project() -> int:
    project = Project(code=_uid("P"), name=_uid("platform"))
    db.session.add(project)
    db.session.flush()
    return project.id


def _configure(project_id: int, payload: dict) -> None:
    ok, message, errors = update_project_analysis_config(
        project_id, payload, updated_by="tester"
    )
    assert ok, (message, errors)


def _run(
    project_id: int,
    *,
    tokens_input: int | None = 1000,
    tokens_output: int | None = 200,
    model: str = "fake-model",
    created_at: datetime | None = None,
) -> AiAnalysisRun:
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=1,
        target_key=_uid("g"),
        status="succeeded",
        scope="full",
        trigger_source="manual",
        response_text="结论",
        model=model,
        created_at=created_at or datetime.now(timezone.utc),
        started_at=created_at or datetime.now(timezone.utc),
        finished_at=created_at or datetime.now(timezone.utc),
        tokens_input=tokens_input,
        tokens_output=tokens_output,
    )
    db.session.add(run)
    db.session.flush()
    return run


def _clear_platform_budget() -> None:
    """把平台档清空。测试库是**会话级共用**的（没有逐用例重置），所以每个用例开头
    都要显式清一遍，否则前一个用例存下的上限会继续生效。"""
    for row in AiPlatformBudget.query.all():
        db.session.delete(row)
    db.session.commit()


def _clear_runs() -> None:
    """清空运行记录（连同它们的逐轮 trace 与异常明细）。

    **平台档没有项目过滤**（它就是「所有项目加起来」），所以任何一个「平台的已用量等于
    N」的断言，都必须由这个用例独占整张 `ai_analysis_run` 表 —— 测试库是会话级共用的，
    别的用例留下的运行会一起被算进去。这不是洁癖：留着它们的话，这些断言会变成
    「单独跑绿、全量跑红」，而红的原因与被测代码毫无关系。

    **子表必须先删。** `AiAnalysisRun.query.delete()` 是批量删除，不走 ORM 的级联；
    别的用例留下的 `ai_analysis_trace` 行会让这条 DELETE 撞上外键约束
    （`sqlite3.IntegrityError: FOREIGN KEY constraint failed`）—— 第一次写这个 helper 时
    就是这么炸的，而且**只在全量跑时炸**（单独跑本文件时没有 trace 行）。
    """
    AiAnalysisTrace.query.delete()
    AiAnalysisAnomaly.query.delete()
    AiAnalysisRun.query.delete()
    db.session.commit()


def _set_platform_budget(**payload) -> None:
    ok, message, errors = set_platform_budget(payload, updated_by="tester")
    assert ok, (message, errors)


@pytest.fixture(autouse=True)
def _isolate_platform_budget():
    """每个用例前后都清一遍平台预算。

    平台预算是**全局单行表**，所以它是本文件对外部世界唯一的副作用 —— 而那个副作用
    会改变**其它文件**里所有 `budget_status` 的判定：那些用例期望的是「没配预算 =
    不限制」，而我这边的用例一旦留下一个 token 上限，它们会突然变成「超预算」。
    实测症状正是如此：单独跑本文件全绿，全量跑时 `test_ai_usage_filters_and_budget.py`
    里 7 条与本轮改动无关的用例报 `assert True is False`。

    先 `create_tables()` 再清：会话级共用的测试库在第一个用例之前可能还没有这张表，
    而直接查它会抛 `no such table`。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        yield
        _clear_platform_budget()


def _status_of(resp):
    """取状态码。view 可能返回 `(response, 401)` 这种元组（`require_admin` 的
    JSON 分支就是这么返回的）。"""
    if isinstance(resp, tuple):
        return resp[1]
    return getattr(resp, "status_code", None)


class _ViewClient:
    """绕过 before_request 的认证链，直接调 view（同 tests/test_ai_usage_capture.py）。"""

    def _call(self, url, method, **kwargs):
        endpoint, args = flask_app.url_map.bind("localhost").match(url, method=method)
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(url, method=method, **kwargs):
            return make_response(view(**args))

    def get(self, url, **kwargs):
        return self._call(url, "GET", **kwargs)

    def post(self, url, **kwargs):
        return self._call(url, "POST", **kwargs)


@pytest.fixture()
def client():
    return _ViewClient()


# ==========================================================================
# 一、没配平台预算时，行为与改动前逐字一致
# ==========================================================================


def test_an_unconfigured_platform_budget_changes_nothing():
    """**最要紧的一条向后兼容。**

    平台没设上限时，`notes` 里不许出现任何平台相关的话（那会把项目档的说明稀释掉），
    `reason` 也必须还是改动前那一句（带项目档的收尾）。这条一旦破了，77 条既有断言的
    可读性会一起下降，而且用户会开始怀疑「平台到底有没有在管」。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 10})
        _run(project_id, tokens_input=1000, tokens_output=200)

        status = budget_status(project_id)

        assert status["over"] is True
        assert status["over_scopes"] == [SCOPE_PROJECT]
        assert status["platform"]["limited"] is False
        assert status["notes"] == [], status["notes"]
        assert status["reason"].endswith(
            "。已暂停 AI 分析，请在项目的「AI 分析配置」里调高预算或等下个周期。"
        ), status["reason"]


def test_an_unconfigured_platform_budget_reports_itself_as_unlimited():
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()

        config = get_platform_budget()

        assert config["configured"] is False
        assert config["token_limit"] is None and config["cost_limit"] is None
        status = platform_budget_status()
        assert status["limited"] is False and status["over"] is False
        # 没配 → 不产出任何 note（见上一条的理由）。
        assert status["notes"] == []


# ==========================================================================
# 二、两档叠加
# ==========================================================================


def test_a_platform_overrun_blocks_a_project_that_is_under_its_own_limit():
    """项目档宽松、平台档超了 —— 必须挡住，而且要说清是**平台**超了。

    收尾那句话必须指向平台预算的去处（「AI 消耗」页面）。写成「去项目的 AI 分析配置里
    调高预算」的话，用户会在那个抽屉里找不到任何与平台有关的字段。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 10_000_000})
        _run(project_id, tokens_input=50_000, tokens_output=0)
        _set_platform_budget(budget_token_limit=1000)

        status = budget_status(project_id)

        assert status["over"] is True
        assert status["over_scopes"] == [SCOPE_PLATFORM]
        assert status["platform"]["over"] is True
        assert status["platform"]["over_limits"] == ["tokens"]
        assert status["reason"].count("平台") >= 1, status["reason"]
        assert "「AI 消耗」页面" in status["reason"], status["reason"]
        assert budget_gate_reason(project_id, entry="test") == status["reason"]


def test_the_platform_total_adds_up_every_project():
    """平台档算的是**所有项目**，不是当前这一个。

    两个项目各自都在自己的上限之内，加起来超了平台上限 —— 这正是「项目级上限管不住
    总量」的那个场景，也是这一层存在的理由。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        first = _project()
        second = _project()
        for project_id in (first, second):
            _configure(project_id, {"budget_token_limit": 1_000_000})
            _run(project_id, tokens_input=40_000, tokens_output=0)
        _set_platform_budget(budget_token_limit=70_000)

        status = budget_status(first)

        # 项目档自己没超（`over_limits` 是项目档的判据），超的是平台档。
        assert status["over_limits"] == [], "单个项目并没超自己的上限"
        assert status["platform"]["used"]["tokens"] == 80_000
        assert status["platform"]["used"]["runs"] == 2
        assert status["over"] is True, "有效判定：平台档超了就算超了"
        assert status["over_scopes"] == [SCOPE_PLATFORM]


def test_both_scopes_over_are_both_reported():
    """两档都超时，`over_scopes` 与 `reason` 都要把两件事说出来 ——
    只说一个会让用户调完一处之后仍然跑不起来，而界面上看不出还差什么。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 10})
        _run(project_id, tokens_input=50_000, tokens_output=0)
        _set_platform_budget(budget_token_limit=1000)

        status = budget_status(project_id)

        assert status["over_scopes"] == [SCOPE_PROJECT, SCOPE_PLATFORM]
        assert "项目" in status["reason"] and "平台" in status["reason"], status["reason"]


def test_the_platform_token_total_is_a_lower_bound_when_something_is_unreported():
    """上游没报 token 时，已用量是**下界**。下界超了就是真超了；下界没超就不能拦人。
    这条与项目档同一判据（`_evaluate_scope` 是两档共用的实现）。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _run(project_id, tokens_input=None, tokens_output=None)
        _set_platform_budget(budget_token_limit=1000)

        status = platform_budget_status()

        assert status["used"]["tokens"] == 0
        assert status["over"] is False
        assert any("下界" in note for note in status["notes"]), status["notes"]


def test_the_platform_budget_does_not_limit_an_unconfigured_scope():
    """只配了 token 上限时，费用那一档**不做判定**（而不是按 0 判）。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        project_id = _project()
        _configure(project_id, {"model_price_table": PRICE_TABLE})
        _run(project_id, tokens_input=10_000_000, tokens_output=0)
        _set_platform_budget(budget_token_limit=10 ** 9)

        status = platform_budget_status()

        assert status["limits"]["cost"] is None
        assert status["over"] is False


# ==========================================================================
# 三、平台档的费用合计：算不出就不判，混币种不判
# ==========================================================================


def test_the_platform_cost_total_needs_every_project_computable():
    """**一个项目算不出，合计就是算不出。**

    只把算得出的加起来会得到一个偏小、看起来完全正常的数字，而它会被拿去和上限比 ——
    这比「算不出」危险得多。没配价格表的项目在真实环境里很常见（新项目刚接入）。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        priced = _project()
        unpriced = _project()
        _configure(priced, {"model_price_table": PRICE_TABLE})
        _run(priced, tokens_input=1_000_000, tokens_output=0)   # 2.00 元
        _run(unpriced, tokens_input=1_000_000, tokens_output=0)  # 没有价格表
        _set_platform_budget(budget_cost_limit="0.5")

        status = platform_budget_status()

        assert status["used"]["cost"] is None
        assert status["over"] is False, "算不出费用时不许拦"
        assert any("算不出" in note for note in status["notes"]), status["notes"]


def test_the_platform_cost_total_refuses_to_add_two_currencies():
    """人民币 + 美元没有意义。宁可说「算不出」，也不给一个量纲不对的数。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        cny = _project()
        usd = _project()
        _configure(cny, {"model_price_table": PRICE_TABLE})
        _configure(usd, {"model_price_table": OTHER_CURRENCY_TABLE})
        for project_id in (cny, usd):
            _run(project_id, tokens_input=1_000_000, tokens_output=0)
        _set_platform_budget(budget_cost_limit="0.5")

        status = platform_budget_status()

        assert status["used"]["cost"] is None
        assert status["over"] is False
        assert any("币种" in note for note in status["notes"]), status["notes"]


def test_the_platform_cost_total_is_judged_when_everything_is_computable():
    """反向自检：上两条不许变成「永远不判」。都算得出且币种一致时必须给数并判。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        first = _project()
        second = _project()
        for project_id in (first, second):
            _configure(project_id, {"model_price_table": PRICE_TABLE})
            _run(project_id, tokens_input=1_000_000, tokens_output=0)
        _set_platform_budget(budget_cost_limit="3")

        status = platform_budget_status()

        assert status["used"]["cost"] == "4.00"
        assert status["over"] is True
        assert status["over_limits"] == ["cost"]


def test_a_sub_cent_limit_is_displayed_as_below_one_cent():
    """上限不足一分钱时显示 `<0.01`（展示口径，见 `pricing.money`）。

    钉住它是因为这个角落没有别的测试会走到：金额一律 2 位小数展示，0.001 元的上限
    四舍五入成 0.00 就成了「限额零元」，而那是另一件事。

    币种符号插在 `<` **之后**：`<¥0.01` 读作「不到一分钱」，`¥<0.01` 读起来像币种
    后面跟了个比较符。四处拼金额的地方（后端一处、前端两份、顶部用量条）同一规则。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _configure(project_id, {"model_price_table": PRICE_TABLE})
        _run(project_id, tokens_input=1_000_000, tokens_output=0)
        _set_platform_budget(budget_cost_limit="0.001")

        status = platform_budget_status()

        assert status["used"]["cost"] == "2.00"
        assert "<¥0.01" in status["reason"], status["reason"]
        assert "¥<0.01" not in status["reason"], (
            f"符号插在比较符前面了：{status['reason']}"
        )


# ==========================================================================
# 四、平台预算的读写
# ==========================================================================


def test_saving_a_platform_budget_validates_like_the_project_config():
    """校验复用项目档那一套（`endpoint_service.validate_field`）。

    自己再写一份的后果：平台上允许填 0（= 把全平台锁死）、项目上不允许，而这种不一致
    在界面上看不出来。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()

        ok, message, errors = set_platform_budget({"budget_token_limit": 0})
        assert ok is False and errors and errors[0]["field"] == "budget_token_limit"

        ok, message, errors = set_platform_budget({"budget_period": "yearly"})
        assert ok is False and errors and errors[0]["field"] == "budget_period"

        ok, message, errors = set_platform_budget({"budget_cost_limit": "不是数"})
        assert ok is False and errors and errors[0]["field"] == "budget_cost_limit"

        # **校验失败时一个字段都不许写。** 部分写入会留下「周期改了、上限没改」的中间
        # 状态，而用户看到的是「保存失败」——他不会想到失败前已经改了一半。
        assert get_platform_budget()["configured"] is False
        db.session.expire_all()
        assert AiPlatformBudget.query.count() == 0


def test_saving_is_a_partial_update():
    """只改一栏时，别的栏保持原值。**不能**因为一次只改周期就把上限清空。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()

        _set_platform_budget(budget_token_limit=1234, budget_cost_limit="10")
        _set_platform_budget(budget_period="weekly")

        config = get_platform_budget()
        assert config["period"] == "weekly"
        assert config["token_limit"] == 1234
        assert config["cost_limit"] == "10"


def test_the_platform_budget_is_a_single_row():
    """单行表：反复保存不许多出行。多行会立刻带来「哪一行生效」，而那个问题没有好答案。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()

        for limit in (100, 200, 300):
            _set_platform_budget(budget_token_limit=limit)

        assert AiPlatformBudget.query.count() == 1
        assert get_platform_budget()["token_limit"] == 300


# ==========================================================================
# 五、口径联动：筛选范围 vs 预算周期
# ==========================================================================


def test_the_period_maps_to_the_matching_range():
    assert budget_period_range("monthly") == RANGE_THIS_MONTH
    assert budget_period_range("weekly") == RANGE_THIS_WEEK
    assert budget_period_range("all_time") == RANGE_ALL
    # 对不上的两个范围（近 30 天 / 自定义）与任何预算周期都不同口径，**不能**含糊过去。
    assert budget_period_range("") is None
    assert budget_period_range("yearly") is None


def test_the_linkage_says_when_the_filter_and_the_budget_period_agree():
    link = period_alignment(UsageFilters(range_key=RANGE_THIS_MONTH), ["monthly"])

    assert link["aligned"] is True
    assert link["mixed"] is False
    assert link["target_range"] == RANGE_THIS_MONTH
    assert "一致" in link["note"], link["note"]


def test_the_linkage_offers_the_target_range_when_they_disagree():
    """不一致时给**一键对齐的目标**，而不是让用户自己去猜该选哪个范围。"""
    link = period_alignment(UsageFilters(range_key=RANGE_ALL), ["monthly"])

    assert link["aligned"] is False
    assert link["target_range"] == RANGE_THIS_MONTH
    assert link["target_label"] == "本月"
    assert "两回事" in link["note"], link["note"]


def test_mixed_budget_periods_have_no_single_target():
    """各项目周期不一致时**没有**能一次对齐的范围。

    硬选一个（比如取最常见的）会让另外几个项目继续不同口径，而界面上看起来
    「已经对齐了」—— 那比不一致本身更糟。
    """
    link = period_alignment(UsageFilters(range_key=RANGE_THIS_MONTH), ["monthly", "all_time"])

    assert link["mixed"] is True
    assert link["aligned"] is False
    assert link["target_range"] is None
    assert len(link["periods"]) == 2
    assert {item["period"] for item in link["periods"]} == {"monthly", "all_time"}


def test_only_projects_with_a_configured_budget_count_as_a_budget_period():
    """没配预算的项目也有个默认周期（本月），但它不该参与联动。

    把它算进来的话，界面会对一批**根本没设预算**的项目宣称「与预算周期一致」，
    而用户会以为它们受着某个上限的约束。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        unbudgeted = _project()
        _run(unbudgeted)
        budgeted = _project()
        _configure(budgeted, {"budget_period": "all_time", "budget_token_limit": 10 ** 9})
        _run(budgeted)

        body = usage_overview()

        link = body["budget_link"]
        assert link["periods"] == [{"period": "all_time", "label": "全部时间", "count": 1}]
        assert link["target_range"] == RANGE_ALL
        assert link["mixed"] is False


def test_the_overview_carries_the_platform_budget_and_the_linkage():
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _configure(project_id, {"budget_period": "monthly", "budget_token_limit": 10 ** 9})
        _run(project_id)
        _set_platform_budget(budget_period="monthly", budget_token_limit=10 ** 9)

        body = usage_overview([project_id], filters=UsageFilters(range_key=RANGE_THIS_MONTH))

        assert body["platform_budget"]["configured"] is True
        assert body["platform_status"]["limited"] is True
        assert body["platform_status"]["period_label"] == "本月"
        assert body["budget_link"]["aligned"] is True
        # 每一行的预算格里都要带上「与当前筛选是否同口径」与平台档，界面据此标注。
        row = next(item for item in body["projects"] if item["project_id"] == project_id)
        assert row["budget"]["matches_filter"] is True
        assert row["budget"]["align_range"] == RANGE_THIS_MONTH
        assert row["budget"]["platform"]["limited"] is True
        assert row["budget"]["over_scopes"] == []


def test_a_row_marks_a_budget_period_that_differs_from_the_filter():
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        project_id = _project()
        _configure(project_id, {"budget_period": "all_time", "budget_token_limit": 10 ** 9})
        _run(project_id)

        body = usage_overview([project_id], filters=UsageFilters(range_key=RANGE_THIS_MONTH))

        row = next(item for item in body["projects"] if item["project_id"] == project_id)
        assert row["budget"]["period"] == "all_time"
        assert row["budget"]["matches_filter"] is False
        assert row["budget"]["align_range"] == RANGE_ALL
        assert body["budget_link"]["aligned"] is False


def test_the_drill_page_carries_the_same_linkage():
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        project_id = _project()
        _configure(project_id, {"budget_period": "weekly", "budget_token_limit": 10 ** 9})
        _run(project_id)

        body = project_usage(project_id, UsageFilters(range_key=RANGE_THIS_MONTH))

        assert body["budget_link"]["target_range"] == RANGE_THIS_WEEK
        assert body["budget"]["matches_filter"] is False


def test_the_platform_period_participates_in_the_linkage():
    """平台档的周期也算一个「预算周期」—— 它同样会出现在这一屏上。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _configure(project_id, {"budget_period": "monthly", "budget_token_limit": 10 ** 9})
        _run(project_id)
        _set_platform_budget(budget_period="all_time", budget_token_limit=10 ** 9)

        link = usage_overview([project_id], filters=UsageFilters(range_key=RANGE_THIS_MONTH))["budget_link"]

        assert link["mixed"] is True
        assert link["target_range"] is None


# ==========================================================================
# 六、权限与端点形状
# ==========================================================================


def test_the_platform_budget_endpoints_require_the_platform_admin(monkeypatch):
    """读与写都要平台管理员 —— **「平台合计」只有平台管理员能看**。

    ## 这条用例中间被改反过一次，值得记下来

    有一版把它改成「读侧不挂闸门，写侧才要管理员」，理由是「`usage_overview` 本来就把
    `platform_budget` 与 `platform_status`（含全平台 used/limits）下发给任何有项目权限的
    用户，只堵这个端点等于假装挡住」。**那个观察是对的，结论却是反的**：该做的是把总览
    里那一份也收掉（现在 `usage_overview(show_platform=platform_scope_visible())` 对
    非管理员连查都不查），而不是把闸门一并拆掉。收窄信息面，不要收窄闸门。

    读侧的 403 现在说的是真话（他确实看不到这些数），所以界面显示那句只读说明是对的。
    """
    from utils import request_security

    monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
    monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)

    get_view = flask_app.view_functions["ai_analysis_routes.ai_platform_budget"]
    post_view = flask_app.view_functions["ai_analysis_routes.ai_platform_budget_update"]
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        db.session.commit()
        for view, kwargs in (
            (get_view, {}),
            (post_view, {"json": {"budget_token_limit": 1}}),
        ):
            with flask_app.test_request_context(
                "/ai-analysis/platform-budget",
                method="POST" if kwargs else "GET",
                headers={"Accept": "application/json"},
                **kwargs,
            ):
                denied = view()
            assert _status_of(denied) in (401, 403), (
                f"非平台管理员拿到了平台合计的读数：{view.__name__} 返回 {denied!r}"
            )

    # 管理员两次都通（证明拒的是权限，不是这个端点本身坏了）。
    monkeypatch.setattr(request_security, "_has_admin_access", lambda: True)
    with flask_app.test_request_context(
        "/ai-analysis/platform-budget", method="GET", headers={"Accept": "application/json"}
    ):
        allowed = get_view()
    assert _status_of(allowed) == 200, allowed


def _overview_as(viewer_is_admin: bool, monkeypatch, project_id: int) -> dict:
    """以「平台管理员 / 普通用户」的身份走一次真实路由，回响应体。

    **必须走路由**：脱敏发生在路由那一层（`platform_scope_visible()` 传给
    `usage_overview`），直接调 service 只能验到「传了 `show_platform=False` 会怎样」，
    验不到「路由到底传了没有」—— 而后者才是权限边界真正落地的地方。
    """
    from utils import request_security

    monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
    monkeypatch.setattr(request_security, "_has_admin_access", lambda: viewer_is_admin)
    # 普通用户的「可访问项目」清单：这里直接给一个项目，模拟「他确实能看这个项目」。
    monkeypatch.setattr(ai_routes, "_get_accessible_project_ids", lambda: [project_id])
    with flask_app.test_request_context("/ai-analysis/usage/overview"):
        resp = ai_routes.ai_usage_overview()
    return (resp[0] if isinstance(resp, tuple) else resp).get_json()


def test_the_overview_does_not_ship_the_platform_scope_to_a_non_admin(monkeypatch):
    """总览对非管理员**一个字都不下发**平台档 —— 不只是「界面上不画」。

    这条是上面那条闸门的配套：闸门只有配上「另一条路也拿不到」才是真的挡住了。所以这里
    不验状态码，验的是**响应体里没有那些数**（`platform_budget` / `platform_status` 为空、
    `platform_hidden` 为真），同时项目档自己的数照旧。

    ## 一个测试上的坑

    `platform_scope_visible()` 在**没有请求上下文**时一律返回 `True`（后台线程、定时任务
    那条路径上没有「给谁看」的问题，不该被脱敏）。所以想模拟「一个普通用户在浏览」就必须
    真的压一个 request context —— 只有 `app_context()` 是不够的，那样两边都会走
    「没有请求上下文」这条分支，断言会「通过」但什么都没验到。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _run(project_id)
        # 项目档也配上：这样「项目档自己的数照旧下发」才验得到东西（没配预算的项目
        # 本来就回 `used.tokens = None`，两种「None」会混在一起分不清）。
        _configure(project_id, {"budget_period": "monthly", "budget_token_limit": 10 ** 9})
        _set_platform_budget(budget_period="monthly", budget_token_limit=10 ** 9)
        db.session.commit()

        hidden = _overview_as(False, monkeypatch, project_id)
        shown = _overview_as(True, monkeypatch, project_id)

    assert hidden["platform_hidden"] is True
    assert shown["platform_hidden"] is False
    assert not hidden["platform_budget"] and not hidden["platform_status"], (
        "非管理员的响应里带着平台档：" + repr(hidden["platform_budget"])
    )
    assert shown["platform_status"], "管理员应当照旧拿到平台档状态"

    row = hidden["projects"][0]
    platform = row["budget"]["platform"]
    assert platform["hidden"] is True
    assert platform["limits"]["tokens"] is None
    assert platform["used"]["tokens"] is None
    assert platform["over"] is False and platform["limited"] is True, (
        "平台档「配了上限」这件事本身要留着 —— 界面据此才能说「有一档你看不到」"
    )
    # 项目档自己的数照旧（那是用户自己的花费）。
    assert row["budget"]["used"]["tokens"] == 1_200
    assert row["budget"]["over"] is False
    # 同一次运行里，管理员那个平台档带着真数字。
    assert shown["projects"][0]["budget"]["platform"]["hidden"] is False


def test_a_non_admin_overview_still_blocks_on_the_platform_scope(monkeypatch):
    """平台档超了：非管理员那份响应里**数字没有，但「拦住了」在**。

    脱敏的红线是「只动显示，不动判定」。这条用一个真的超了的平台档把红线钉住 ——
    脱敏把 `blocks_analysis` 一起抹掉的话，界面会显示「一切正常」而分析其实跑不起来。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _run(project_id)
        _configure(project_id, {"budget_period": "all_time", "budget_token_limit": 10 ** 9})
        _set_platform_budget(budget_period="all_time", budget_token_limit=1)
        db.session.commit()

        body = _overview_as(False, monkeypatch, project_id)

    budget = body["projects"][0]["budget"]
    # **判定不动**：脱敏只抹数字，超没超、超的是哪一档一个字不改。这一格是这一整套的
    # 红线 —— 抹掉 `over` 的话界面会显示「一切正常」，而分析其实跑不起来。
    assert budget["over"] is True
    assert budget["over_scopes"] == [SCOPE_PLATFORM]
    assert "平台" in budget["reason"], budget["reason"]
    # 额度那一栏是空的（不是 0），而且界面能看出「这里被收掉了」。
    platform = budget["platform"]
    assert platform["hidden"] is True and platform["over"] is True
    assert platform["limits"]["tokens"] is None


def test_a_non_admin_still_gets_the_platform_verdict_without_the_numbers(monkeypatch):
    """拦下来的**理由**对非管理员也要说清是平台档，只是不给额度。

    这是脱敏的边界：事实（哪一档超了、去哪儿改）必须保留，数字才抹。抹掉事实的后果很具体
    ——用户会去调自己项目的预算，调了没用，然后来报 bug。
    """
    from utils import request_security

    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _run(project_id)
        _set_platform_budget(budget_period="all_time", budget_token_limit=1)
        db.session.commit()

        monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
        monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)
        with flask_app.test_request_context("/ai-analysis/commit/1/stream"):
            reason = budget_gate_reason(project_id, entry="test")

        monkeypatch.setattr(request_security, "_has_admin_access", lambda: True)
        with flask_app.test_request_context("/ai-analysis/commit/1/stream"):
            admin_reason = budget_gate_reason(project_id, entry="test")

    assert reason, "平台档超了却没拦住"
    assert "平台" in reason, f"理由里没说是平台档，用户会去调自己项目的预算：{reason}"
    assert admin_reason != reason, (
        "管理员与普通用户拿到的理由逐字相同 —— 说明脱敏根本没生效（额度数字还在）"
    )


def test_the_platform_budget_endpoint_reads_and_writes(client, monkeypatch):
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()

    monkeypatch.setattr("utils.request_security._has_admin_access", lambda: True)

    resp = client.get("/ai-analysis/platform-budget")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    assert body["budget"]["configured"] is False
    assert body["status"]["limited"] is False

    resp = client.post(
        "/ai-analysis/platform-budget",
        json={"budget_period": "monthly", "budget_token_limit": 500_000},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    saved = resp.get_json()
    assert saved["success"] is True
    assert saved["budget"]["configured"] is True
    assert saved["budget"]["token_limit"] == 500_000
    # 保存后立刻回一份新状态：界面不必再发一次 GET。
    assert saved["status"]["limited"] is True

    # 字段级错误要逐项回，界面据此把那栏标红。
    resp = client.post("/ai-analysis/platform-budget", json={"budget_token_limit": -5})
    assert resp.status_code == 400
    assert resp.get_json()["errors"][0]["field"] == "budget_token_limit"


def test_the_public_payload_never_leaks_the_model_columns():
    """给界面的那一份只有这四个字段。多回一列就多一处将来会被改错的地方。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _set_platform_budget(budget_token_limit=1000)

        public = platform_budget_public()

        assert set(public) == {
            "period",
            "token_limit",
            "cost_limit",
            "configured",
            "updated_by",
            "updated_at",
        }
        assert public["updated_by"] == "tester"


def test_an_overrun_outside_the_window_does_not_block():
    """平台档同样按时段统计：上个月的用量不该把本月的预算顶爆。

    这条与项目档的 `test_only_runs_inside_the_period_count_toward_the_budget` 对应 ——
    两档共用 `_evaluate_scope`，但**时段窗口是各自算的**（项目档用项目配置的周期，
    平台档用平台配置的周期），所以这里要单独钉一次。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        last_month = datetime.now(timezone.utc) - timedelta(days=40)
        _run(project_id, tokens_input=1_000_000, tokens_output=0, created_at=last_month)
        _set_platform_budget(budget_period="monthly", budget_token_limit=1000)

        status = platform_budget_status()

        assert status["used"]["tokens"] == 0
        assert status["over"] is False
        assert status["used"]["runs"] == 0


@pytest.mark.parametrize("period", ["monthly", "weekly", "all_time"])
def test_every_period_choice_is_usable_for_the_platform_scope(period):
    """三个周期都要能用。参数化是为了将来加周期时**这条会自己长出来**，
    而不是留下两个没人试过的取值。"""
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        project_id = _project()
        _run(project_id, tokens_input=5000, tokens_output=0)
        _set_platform_budget(budget_period=period, budget_token_limit=10)

        status = platform_budget_status()

        assert status["period"] == period
        assert status["over"] is True


def test_the_filters_parser_still_accepts_the_field_names_the_ui_will_send():
    """联动按钮会去改 `range` —— 这个键必须仍然被认出来（不是回落默认）。

    一条便宜的护栏：联动是本轮新增的唯一一个「界面驱动查询参数」的地方，它与
    `parse_usage_filters` 的取值表必须同源（`RANGE_THIS_MONTH` 等常量）。
    """
    filters = parse_usage_filters({"range": RANGE_THIS_MONTH})

    assert filters.range_key == RANGE_THIS_MONTH
    assert filters.notes == ()


def test_every_budget_field_maps_to_a_real_column():
    """**这条是给一个真实踩过的坑守门的。**

    第一版写库用的是展示用的那张映射（`budget_token_limit` → `token_limit` 没转），于是
    `setattr(row, "budget_token_limit", 300)` 在 SQLAlchemy 模型上**成功地**创了一个游离
    的实例属性：不报错、不写库，`get_platform_budget()` 读回来还是默认值。用户看到的是
    「保存成功，刷新就没了」。

    所以这里逐个字段验「能落到真实的列上」，并要求两张映射的键集一致 —— 将来加字段时，
    漏改任何一张都会在这里红，而不是在用户的浏览器里红。
    """
    from services.ai import platform_budget as module

    columns = set(AiPlatformBudget.__table__.columns.keys())
    assert set(module._FIELD_TO_COLUMN) == set(module.BUDGET_FIELDS), (
        "字段集合与列映射的键集必须一一对应"
    )
    assert set(module._FIELD_TO_KEY) == set(module.BUDGET_FIELDS)
    for field in module.BUDGET_FIELDS:
        column = module._column_for(field)
        assert column in columns, f"{field} 映射到了不存在的列 {column}"
    # 展示用的键与列名**不必**相同（`budget_token_limit` 读出来叫 `token_limit`），
    # 但它们不能指向同一个东西这点必须靠代码而不是靠记性 —— 写错一个立刻炸。
    with pytest.raises(KeyError):
        module._column_for("budget_not_a_field")


# ==========================================================================
# 「有几个项目被停了」—— 一个会随筛选变化的数字比没有这个数字更糟
# ==========================================================================


def test_the_over_budget_count_does_not_shrink_when_the_filter_excludes_the_project():
    """**被停掉的项目恰恰不再产生运行记录，所以最容易被筛掉。**

    卡片的口气是全局事实（「这些项目的 AI 分析已被暂停（手动与定时都停）」），而原先
    这个数是从**筛选之后**的 `entries` 里数出来的。项目 A 本月超了预算 → 闸门停掉它的
    手动与定时分析 → 它不再产生运行记录 → 用户把范围改成「本周」时 A 不在这一屏里，
    KPI 显示 0 个，而 A 此刻确实是停着的。

    判据按「换个窗口数字不变」来断：这是唯一有意义的口径。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        stopped = _project()
        # `all_time` 的项目档：判据只看这个项目自己的全部运行，与面板选哪个窗口无关。
        _configure(stopped, {"budget_period": "all_time", "budget_token_limit": 500})
        # 10 天前跑过一次、用掉 1200 token → 项目档早就超了，而它此后不会再有运行记录
        _run(stopped, tokens_input=1200, tokens_output=0,
             created_at=datetime.now(timezone.utc) - timedelta(days=10))

        all_time = usage_overview([stopped], filters=UsageFilters(range_key=RANGE_ALL))
        this_week = usage_overview([stopped], filters=UsageFilters(range_key=RANGE_THIS_WEEK))

        assert all_time["over_budget_projects"] == 1, all_time["over_budget_projects"]
        assert this_week["projects"] == [], "前提：本周这一屏确实没有它的运行记录"
        assert this_week["over_budget_projects"] == 1, (
            "把范围收成「本周」之后被停掉的项目就数不出来了 —— 而它正是最该被提起的那个"
        )


def test_a_platform_level_overage_does_not_count_every_project_as_over():
    """平台总预算超了会让**每一行**都标红，但那是「平台档超了」，不是「这个项目被停了」。

    `over` 是项目档与平台档的**或**（`analysis_budget.budget_status`），所以按 `over`
    数出来的其实是「这一屏有几个项目」—— 与卡片的措辞不是一回事。
    """
    with flask_app.app_context():
        create_tables()
        _clear_platform_budget()
        _clear_runs()
        first, second = _project(), _project()
        for project_id in (first, second):
            _configure(project_id, {"budget_period": "monthly", "budget_token_limit": 10 ** 9})
            _run(project_id, tokens_input=1000, tokens_output=0)
        # 平台档卡在 500：两个项目都没超自己的，但平台档超了
        _set_platform_budget(budget_period="monthly", budget_token_limit=500)

        body = usage_overview([first, second], filters=UsageFilters(range_key="this_month"))

        rows = {item["project_id"]: item for item in body["projects"]}
        assert all(row["budget"]["over"] for row in rows.values()), (
            "前提：平台档超了，每一行的 `over` 都为真"
        )
        assert body["over_budget_projects"] == 0, (
            "平台档超了被数成了「N 个项目已超预算」—— 卡片说的是「这些项目的分析已被暂停」"
        )
