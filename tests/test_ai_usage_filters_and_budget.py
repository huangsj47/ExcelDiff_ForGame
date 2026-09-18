# -*- coding: utf-8 -*-
"""AI 消耗面板的筛选、预算闸门与单价表版本规矩。

## 这个文件守的六条性质

1. **筛选条件非法时回落默认，不 500。** 这些参数从地址栏来，用户会手改、会分享被截断
   的链接、会收藏半年前的地址。变成 500 的话页面直接白屏，而筛选状态就在 URL 里 ——
   用户没有任何办法回到正常状态。
2. **筛选在服务端做。** 传 `project` 只能收窄权限范围，不能扩大；按项目筛过之后
   合计里不许混进别的项目。
3. **预算「未配置 = 不限制」。** 不是 0、也不是任何默认值：0 会被读成「一个 token 都
   不许花」，把 AI 分析整个锁死，而且界面上找不到任何解释。
4. **超预算拦住三条入口**（单提交手动 / 周版本手动 / 周版本后台），并且都要给出
   **能看懂的原因**，不静默。
5. **算不出费用时不许拦。** 宁可放过一次超预算的分析，也不能因为「价格表没配」或者
   「模型名对不上」就把功能锁死 —— 后者的表现是「所有项目突然都不能分析了，
   而且界面说不出为什么」。
6. **改单价必须同时改 version。** 每次运行会记下当时的版本号，用来解释历史费用是按
   哪版算的；版本不变的话，改价前后的费用在库里分不开，而且**不会报任何错**。

另外还有一条这次改动的真实风险点：单价表从「AI 分析配置」搬到了消耗面板，保存时
**只提交 `model_price_table` 一个字段** —— 必须钉住它不会把别的配置项清掉。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
import services.ai_analysis_service as ai_service
from app import app as flask_app
from app import create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun
from services.ai.analysis_budget import (
    PERIOD_CHOICES,
    budget_gate_reason,
    budget_status,
    normalize_period,
    period_window,
)
from services.ai_analysis_service import (
    run_weekly_analysis_background,
    stream_commit_analysis,
    stream_weekly_analysis,
    update_project_analysis_config,
)
from services.ai_usage_service import (
    RANGE_ALL,
    RANGE_CHOICES,
    SOURCE_ALL,
    STATUS_ALL,
    UsageFilters,
    parse_usage_filters,
    project_usage,
    resolve_window,
    usage_overview,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = "templates/ai_usage_dashboard.html"
PROJECT_VIEW = "templates/merged_project_view.html"
WEEKLY_DRAWER = "templates/weekly_version_diff.html"
COMMIT_DRAWER = "templates/commit_diff_new.html"

PRICE_TABLE = json.dumps(
    {
        "version": "t-1",
        "currency": "CNY",
        "models": {"fake-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2}},
    }
)


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _project() -> int:
    project = Project(code=_uid("P"), name=_uid("budget"))
    db.session.add(project)
    db.session.flush()
    return project.id


def _repo(project_id: int) -> Repository:
    repo = Repository(
        project_id=project_id,
        name=_uid("code"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="code",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    return repo


def _weekly_config(project_id: int, repo_id: int) -> WeeklyVersionConfig:
    cfg = WeeklyVersionConfig(
        project_id=project_id,
        repository_id=repo_id,
        name=f"W1 - {_uid('week')}",
        description="",
        branch="main",
        start_time=datetime(2026, 3, 1),
        end_time=datetime(2026, 3, 8),
        cycle_type="custom",
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    return cfg


def _run(
    project_id: int,
    *,
    tokens_input: int | None = 1000,
    tokens_output: int | None = 200,
    model: str = "fake-model",
    trigger_source: str = "manual",
    status: str = "succeeded",
    created_at: datetime | None = None,
    target_type: str = "weekly",
) -> AiAnalysisRun:
    moment = created_at or datetime.now(timezone.utc)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type=target_type,
        # target_id 用 project_id：各个用例的项目号互不相同，断言「这个目标没有留下
        # run」时才不会被别的用例的行撞上（测试库是整个会话共用的，没有逐用例重置）。
        target_id=project_id,
        target_key="group-a",
        status=status,
        scope="full",
        trigger_source=trigger_source,
        response_text="结论",
        model=model,
        created_at=moment,
        started_at=moment,
        finished_at=moment,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        pricing_version="t-1",
    )
    db.session.add(run)
    db.session.flush()
    db.session.commit()
    return run


def _configure(project_id: int, payload: dict) -> None:
    ok, message, errors = update_project_analysis_config(project_id, payload, updated_by="tester")
    assert ok, f"配置没存进去：{message} {errors}"


def _sse_error_message(text: str) -> str:
    r"""从 SSE 文本里取出 `error` 事件的 message。

    `data` 是 `json.dumps` 出来的（中文被转成 `\uXXXX`），所以**不能直接在里面搜中文** ——
    那样断言永远为假，而失败信息看起来像是「闸门没生效」。
    """
    for block in text.split(chr(10) + chr(10)):
        if not block.startswith("event: error"):
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                return str(json.loads(line[5:].strip()).get("message") or "")
    return ""


class _ViewClient:
    """绕过 before_request 的认证链，直接调 view（同 tests/test_ai_usage_capture.py）。"""

    def get(self, url, **_kwargs):
        # query 要单独拆出来：`url_map.match` 只认路径，带 `?` 会直接 404。
        path, _, query = url.partition("?")
        endpoint, args = flask_app.url_map.bind("localhost").match(path, method="GET")
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(path + (("?" + query) if query else ""), method="GET"):
            return make_response(view(**args))


@pytest.fixture()
def client():
    return _ViewClient()


# ==========================================================================
# 一、筛选参数解析：非法值回落默认，绝不抛异常
# ==========================================================================


class TestFilterParsing:
    def test_an_empty_query_gets_the_documented_defaults(self):
        filters = parse_usage_filters({})

        assert filters.project_id is None
        assert filters.range_key == RANGE_ALL
        assert filters.source == SOURCE_ALL
        assert filters.status == STATUS_ALL
        assert filters.active is False
        assert filters.notes == ()

    def test_recognized_values_are_kept(self):
        filters = parse_usage_filters(
            {"project": "7", "range": "this_month", "source": "scheduled", "status": "failed"}
        )

        assert filters.project_id == 7
        assert filters.range_key == "this_month"
        assert filters.source == "scheduled"
        assert filters.status == "failed"
        assert filters.active is True

    def test_the_custom_range_keeps_its_dates(self):
        filters = parse_usage_filters(
            {"range": "custom", "from": "2026-03-01", "to": "2026-03-31"}
        )

        assert filters.range_key == "custom"
        assert filters.date_from.isoformat() == "2026-03-01"
        assert filters.date_to.isoformat() == "2026-03-31"

    @pytest.mark.parametrize(
        "query",
        [
            {"project": "abc"},
            {"project": "-3"},
            {"range": "上周"},
            {"source": "whatever"},
            {"status": "成功"},
            {"range": "custom"},
            {"range": "custom", "from": "2026/03/01", "to": "2026-03-31"},
            {"range": "custom", "from": "2026-03-31", "to": "2026-03-01"},
        ],
    )
    def test_every_illegal_value_falls_back_instead_of_raising(self, query):
        """**这是本文件最要紧的一条**：地址栏里的脏值不许把页面变成 500。

        它同时断言「回落了要有说明」—— 悄悄换掉用户填的条件，和报错一样让人
        摸不着头脑：他看到的数字不是他问的那个，却以为是自己填的条件生效了。
        """
        filters = parse_usage_filters(query)

        assert filters.range_key in RANGE_CHOICES
        assert filters.source in ("all", "manual", "scheduled")
        assert filters.status in ("all", "succeeded", "failed", "running")
        assert filters.notes, f"回落了却没说为什么：{query}"

    def test_reversed_dates_are_not_silently_swapped(self):
        """起止写反了**不静默对调**：对调之后界面上输入的还是反的、结果却是正的，
        用户没法从界面上看出发生了什么。回落到默认并说明，比悄悄改掉他的输入诚实。"""
        filters = parse_usage_filters(
            {"range": "custom", "from": "2026-03-31", "to": "2026-03-01"}
        )

        assert filters.range_key == RANGE_ALL
        assert filters.date_from is None and filters.date_to is None
        assert any("反了" in note for note in filters.notes)

    def test_a_partial_custom_range_is_still_usable(self):
        """只给一头是合法的（「从某天起」「到某天为止」），不该被当成非法。"""
        filters = parse_usage_filters({"range": "custom", "from": "2026-03-01"})
        assert filters.range_key == "custom"
        assert filters.date_from.isoformat() == "2026-03-01"

        since, until = resolve_window(filters)
        assert since is not None and until is None

    def test_the_query_round_trip_is_canonical(self):
        """回写给地址栏的 query 只带非默认值，而且能被自己再解析回去。"""
        filters = parse_usage_filters({"project": "7", "range": "custom",
                                       "from": "2026-03-01", "to": "2026-03-31"})
        again = parse_usage_filters(filters.as_query())

        assert again == filters, "回写的 query 解析回来不是同一份筛选"
        assert parse_usage_filters({}).as_query() == {}, "默认筛选不该往地址栏里塞东西"


# ==========================================================================
# 二、时间窗：按北京时间切日历，边界左闭右开
# ==========================================================================


class TestTheTimeWindow:
    def test_this_month_starts_at_the_beijing_first_day(self):
        # 2026-03-15 00:30 UTC = 北京时间 3-15 08:30
        filters = UsageFilters(range_key="this_month")
        since, until = resolve_window(filters, now=datetime(2026, 3, 15, 0, 30, tzinfo=timezone.utc))

        # 北京 3-01 00:00 = UTC 2-28 16:00
        assert since == datetime(2026, 2, 28, 16, 0, tzinfo=timezone.utc)
        assert until is None

    def test_this_week_starts_on_monday(self):
        # 2026-03-15 是周日；北京时间下这一周从 3-09（周一）开始
        filters = UsageFilters(range_key="this_week")
        since, _until = resolve_window(filters, now=datetime(2026, 3, 15, 4, 0, tzinfo=timezone.utc))

        assert since == datetime(2026, 3, 8, 16, 0, tzinfo=timezone.utc)

    def test_a_custom_end_date_includes_the_whole_day(self):
        """`to` 是**排他**上界，取次日 00:00 —— 用「当日 23:59:59」会漏掉那一秒里的
        记录，而且只在特定时刻复现。"""
        filters = parse_usage_filters({"range": "custom", "from": "2026-03-01", "to": "2026-03-31"})
        since, until = resolve_window(filters)

        assert since == datetime(2026, 2, 28, 16, 0, tzinfo=timezone.utc)
        assert until == datetime(2026, 3, 31, 16, 0, tzinfo=timezone.utc)

        # 北京时间 3-31 23:30 的那次运行要落进窗口里
        run = AiAnalysisRun(created_at=datetime(2026, 3, 31, 15, 30, tzinfo=timezone.utc))
        from services.ai.analysis_budget import runs_in_window

        assert runs_in_window([run], since, until) == [run]
        # 北京时间 4-01 00:30 的那次不能落进来
        after = AiAnalysisRun(created_at=datetime(2026, 3, 31, 16, 30, tzinfo=timezone.utc))
        assert runs_in_window([after], since, until) == []

    def test_a_run_without_a_timestamp_never_enters_a_window(self):
        from services.ai.analysis_budget import runs_in_window

        assert runs_in_window([AiAnalysisRun(created_at=None)], datetime(2020, 1, 1, tzinfo=timezone.utc), None) == []


# ==========================================================================
# 三、筛选落在服务端：权限只能收窄，不能被 URL 扩大
# ==========================================================================


def test_the_project_filter_narrows_and_can_never_widen():
    with flask_app.app_context():
        create_tables()
        mine = _project()
        other = _project()
        _run(mine, tokens_input=100, tokens_output=10)
        _run(other, tokens_input=999, tokens_output=99)

        # 只看自己那个项目
        body = usage_overview([mine, other], parse_usage_filters({"project": str(mine)}))
        assert [item["project_id"] for item in body["projects"]] == [mine]
        assert body["totals"]["tokens"]["input"] == 100

        # URL 里写一个无权访问的项目号 → 空结果，而不是读到别人的数
        body = usage_overview([mine], parse_usage_filters({"project": str(other)}))
        assert body["projects"] == []
        assert body["totals"]["runs"] == 0


def test_the_project_dropdown_is_never_narrowed_by_the_filter():
    """筛选下拉的项目清单**永远是全量**（按权限）。

    拿筛选后的结果当下拉选项，会让下拉里只剩一个项目 —— 用户换不回「全部项目」，
    只能去改地址栏。这是「筛选」这个交互最基本的一条可用性。
    """
    with flask_app.app_context():
        create_tables()
        first = _project()
        second = _project()

        body = usage_overview([first, second], parse_usage_filters({"project": str(first)}))

        assert {item["project_id"] for item in body["project_options"]} == {first, second}
        assert body["filters"]["project_id"] == first


def test_the_source_and_status_filters_apply_to_the_numbers():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _run(project_id, tokens_input=100, tokens_output=0, trigger_source="manual")
        _run(project_id, tokens_input=200, tokens_output=0, trigger_source="scheduled")
        _run(project_id, tokens_input=400, tokens_output=0, trigger_source="manual", status="failed")

        def total(query):
            return usage_overview([project_id], parse_usage_filters(query))["totals"]["tokens"]["input"]

        assert total({}) == 700
        assert total({"source": "scheduled"}) == 200
        assert total({"source": "manual"}) == 500
        assert total({"status": "failed"}) == 400
        assert total({"source": "manual", "status": "failed"}) == 400


def test_the_drill_uses_the_same_filtered_runs_as_its_totals():
    """下钻页的合计与明细必须是**同一批**过滤后的运行。

    合计按全部算、明细按筛选列，两个数字对不上，而且没有任何地方会报错。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _run(project_id, tokens_input=100, tokens_output=0, trigger_source="manual")
        _run(project_id, tokens_input=200, tokens_output=0, trigger_source="scheduled")

        body = project_usage(project_id, parse_usage_filters({"source": "scheduled"}))

        assert body["totals"]["tokens"]["input"] == 200
        assert len(body["runs"]) == 1
        assert body["filters"]["source"] == "scheduled"


def test_bad_query_parameters_reach_the_endpoint_as_a_page_not_an_error(client, monkeypatch):
    """脏 URL 端到端也不许 500。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()

    monkeypatch.setattr(ai_routes, "_get_accessible_project_ids", lambda: [project_id])
    resp = client.get("/ai-analysis/usage/overview?project=abc&range=%E4%B8%8A%E5%91%A8&from=xx&status=ok")

    assert resp.status_code == 200, "地址栏里的脏值把页面变成了错误页"
    body = resp.get_json()
    assert body["success"] is True
    assert body["filters"]["notes"], "回落了却没有任何说明给用户看"


def test_the_overview_exposes_the_budget_of_every_project(client, monkeypatch):
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 1000})
        _run(project_id, tokens_input=50, tokens_output=50)

    monkeypatch.setattr(ai_routes, "_get_accessible_project_ids", lambda: [project_id])
    body = client.get("/ai-analysis/usage/overview").get_json()

    entry = [item for item in body["projects"] if item["project_id"] == project_id][0]
    assert entry["budget"]["limited"] is True
    assert entry["budget"]["over"] is False
    assert entry["budget"]["used"]["tokens"] == 100
    assert entry["budget"]["limits"]["tokens"] == 1000
    assert entry["budget"]["ratios"]["tokens"] == pytest.approx(0.1)


# ==========================================================================
# 四、预算：未配置 = 不限制
# ==========================================================================


def test_an_unconfigured_budget_is_unlimited_not_zero():
    """**未配置 = 不限制。** 不是 0，也不是任何拍脑袋的默认值。

    0 会被读成「一个 token 都不许花」，把 AI 分析整个锁死；而界面上那一栏是空的，
    用户找不到任何解释。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _run(project_id, tokens_input=10 ** 9, tokens_output=10 ** 9)

        status = budget_status(project_id)

        assert status["limited"] is False
        assert status["over"] is False
        assert status["blocks_analysis"] is False
        assert status["limits"]["tokens"] is None
        assert status["limits"]["cost"] is None
        assert budget_gate_reason(project_id, entry="test") is None, "没配预算却把分析拦了"


def test_a_zero_or_negative_budget_does_not_lock_the_project():
    """库里出现 0 只可能来自遗留数据或手工改库（配置校验不允许填 0）。

    「因为一个 0 把分析彻底锁死」的代价远大于「放过一次」，所以按不限制处理。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        row = ai_service.AiProjectAnalysisConfig(project_id=project_id)
        db.session.add(row)
        db.session.flush()
        # 直接写库，绕过校验（模拟遗留数据）
        row.budget_token_limit = 0
        row.budget_cost_limit = "0"
        db.session.commit()

        status = budget_status(project_id)
        assert status["limited"] is False
        assert budget_gate_reason(project_id, entry="test") is None


def test_a_token_overrun_blocks_with_a_readable_reason():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 1000, "budget_period": "monthly"})
        _run(project_id, tokens_input=800, tokens_output=400)

        status = budget_status(project_id)

        assert status["limited"] is True
        assert status["over"] is True
        assert status["blocks_analysis"] is True
        assert status["over_limits"] == ["tokens"]
        assert status["used"]["tokens"] == 1200
        assert "1.2k" in status["reason"] and "1.0k" in status["reason"], status["reason"]
        assert budget_gate_reason(project_id, entry="test") == status["reason"]


def test_staying_under_the_limit_does_not_block():
    """反向自检：配了预算但没超时不许拦。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 10_000})
        _run(project_id, tokens_input=800, tokens_output=400)

        status = budget_status(project_id)
        assert status["limited"] is True
        assert status["over"] is False
        assert budget_gate_reason(project_id, entry="test") is None


def test_unreported_tokens_are_a_lower_bound_not_a_zero():
    """上游没报 token 时，已用量是**下界**（只统计报上来的部分）。

    下界没超就不能拦人 —— 那可能只是数据缺失；下界已经超了就一定超了。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 1000})
        _run(project_id, tokens_input=None, tokens_output=None)

        status = budget_status(project_id)

        assert status["used"]["tokens"] == 0
        assert status["over"] is False
        assert any("下界" in note for note in status["notes"]), status["notes"]


def test_a_partial_report_can_still_prove_an_overrun():
    """下界已经超了 → **确定**超了，可以拦。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 1000})
        _run(project_id, tokens_input=None, tokens_output=None)
        _run(project_id, tokens_input=2000, tokens_output=0)

        status = budget_status(project_id)
        assert status["used"]["tokens"] == 2000
        assert status["over"] is True


def test_a_cost_overrun_blocks_when_the_amount_is_known():
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"model_price_table": PRICE_TABLE, "budget_cost_limit": "0.001"})
        # 1,000,000 输入 + 0 输出 @2.0/百万 = 2.0
        _run(project_id, tokens_input=1_000_000, tokens_output=0)

        status = budget_status(project_id)

        assert status["used"]["cost"] == "2"
        assert status["over_limits"] == ["cost"]
        assert status["over"] is True
        assert "¥2" in status["reason"], status["reason"]


def test_a_cost_limit_never_blocks_when_the_amount_cannot_be_computed():
    """**最要紧的一条容错**：算不出费用时不许拦。

    三种算不出的形态都要放过：没配价格表、模型名对不上、上游没报 token。
    锁死功能的代价远大于放过一次超预算的分析，而且前者的表现是「所有项目突然都
    不能分析了，界面还说不出为什么」。
    """
    with flask_app.app_context():
        create_tables()

        # 1) 没配价格表
        no_table = _project()
        _configure(no_table, {"budget_cost_limit": "0.001"})
        _run(no_table, tokens_input=10 ** 7, tokens_output=10 ** 7)
        status = budget_status(no_table)
        assert status["used"]["cost"] is None
        assert status["over"] is False and status["blocks_analysis"] is False
        assert any("算不出" in note for note in status["notes"]), status["notes"]
        assert budget_gate_reason(no_table, entry="test") is None

        # 2) 价格表在，但模型名对不上
        unmatched = _project()
        _configure(unmatched, {"model_price_table": PRICE_TABLE, "budget_cost_limit": "0.001"})
        _run(unmatched, model="some-other-model", tokens_input=10 ** 7, tokens_output=10 ** 7)
        assert budget_status(unmatched)["over"] is False

        # 3) 价格表在、模型对得上，但上游没报 token
        unreported = _project()
        _configure(unreported, {"model_price_table": PRICE_TABLE, "budget_cost_limit": "0.001"})
        _run(unreported, tokens_input=None, tokens_output=None)
        assert budget_status(unreported)["over"] is False


def test_only_runs_inside_the_period_count_toward_the_budget():
    """预算按时段统计：上个月的用量不该把本月的预算顶爆，也不该让它凭空不够。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 1000, "budget_period": "monthly"})
        last_month = datetime.now(timezone.utc) - timedelta(days=40)
        _run(project_id, tokens_input=100_000, tokens_output=0, created_at=last_month)

        status = budget_status(project_id)
        assert status["used"]["tokens"] == 0
        assert status["over"] is False

    with flask_app.app_context():
        all_time = _project()
        _configure(all_time, {"budget_token_limit": 1000, "budget_period": "all_time"})
        _run(all_time, tokens_input=100_000, tokens_output=0, created_at=last_month)
        assert budget_status(all_time)["over"] is True


def test_the_budget_judgement_never_raises(monkeypatch):
    """判定出任何意外都降级成「不拦 + 一句说明」。

    闸门要是会因为一个脏数据抛异常，那条路径上的分析就再也没人跑得起来了。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"budget_token_limit": 1})

        def _boom(*_args, **_kwargs):
            raise RuntimeError("模拟读运行记录失败")

        monkeypatch.setattr(AiAnalysisRun, "query", property(lambda self: (_ for _ in ()).throw(RuntimeError("boom"))))
        status = budget_status(project_id)

        assert status["blocks_analysis"] is False
        assert budget_gate_reason(project_id, entry="test") is None


def test_the_period_choices_match_the_model_layer():
    """周期选项只有一份：模块常量与模型层的 choices 必须一致，否则界面上会出现
    「选中了、后端回落默认」的选项。"""
    from models.ai_analysis.project_config import BUDGET_PERIOD_CHOICES

    assert tuple(PERIOD_CHOICES) == tuple(BUDGET_PERIOD_CHOICES)
    assert normalize_period("nope") == "monthly"


# ==========================================================================
# 五、闸门拦住三条入口
# ==========================================================================


def test_the_commit_stream_is_blocked_before_any_request_is_made():
    """手动·单提交：超预算时回 `error` 事件，并且**一条 run 都不留**。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        repo = _repo(project_id)
        commit = Commit(repository_id=repo.id, commit_id=_uid("c"), status="pending")
        db.session.add(commit)
        db.session.flush()
        commit_id = commit.id
        _configure(project_id, {"budget_token_limit": 10})
        _run(project_id, tokens_input=1000, tokens_output=100)
        # 密钥要有，否则会先撞上「未配置密钥」那道（那样就测不到预算这一道了）
        ai_service.set_project_api_key(project_id, "sk-test", updated_by="tester")
        db.session.commit()

        text = "".join(stream_commit_analysis(commit_id))

        message = _sse_error_message(text)
        assert message, f"没有回 error 事件：{text[:400]}"
        assert "预算" in message, message
        assert (
            AiAnalysisRun.query.filter_by(
                target_type="commit", target_id=commit_id, project_id=project_id
            ).count() == 0
        ), "拦下了却还是留下了一条 run 记录"


def test_the_weekly_stream_is_blocked_before_any_request_is_made(monkeypatch):
    """手动·周版本：同一条闸，走的是 `error` 事件。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        repo = _repo(project_id)
        cfg = _weekly_config(project_id, repo.id)
        config_id = cfg.id
        _configure(project_id, {"budget_token_limit": 10})
        _run(project_id, tokens_input=1000, tokens_output=100)
        ai_service.set_project_api_key(project_id, "sk-test", updated_by="tester")
        db.session.commit()

        # payload 的构造要读仓库/提交/缓存，这里直接喂一份最小的形状：
        # 这一条用例要测的是**闸门的位置**，不是 payload 的构造。
        monkeypatch.setattr(
            ai_service,
            "build_weekly_payload",
            lambda *a, **k: (
                {"group": {"project_id": project_id, "key": "k"}, "summary": {}, "scope": "full"},
                None,
                None,
            ),
        )

        text = "".join(stream_weekly_analysis(config_id))

        message = _sse_error_message(text)
        assert message, f"没有回 error 事件：{text[:400]}"
        assert "预算" in message, message
        assert (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_id=config_id, project_id=project_id
            ).count() == 0
        )


def test_the_background_weekly_run_is_skipped_and_says_why():
    """**自动那条**：后台任务记成 skipped 并带上原因，不静默。

    静默跳过的表现是「自动分析不跑了，但没有任何地方说为什么」—— 用户会去翻调度器、
    翻开关、翻权限，唯独翻不到「其实是你自己设的预算到了」。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        repo = _repo(project_id)
        cfg = _weekly_config(project_id, repo.id)
        _configure(project_id, {"budget_token_limit": 10, "auto_weekly_enabled": True})
        _run(project_id, tokens_input=1000, tokens_output=100)
        db.session.commit()

        outcome = run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "over_budget"
        assert "预算" in outcome.get("message", ""), outcome
        assert (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_id=cfg.id, project_id=project_id
            ).count() == 0
        ), "跳过了却还是留下了一条 run 记录"


def test_the_scheduler_also_checks_the_budget_before_queueing():
    """调度器排队前也查一次 —— 否则任务列表里会堆一堆注定被跳过的记录。

    这里用**结构断言**而不是跑一遍调度器：`schedule_weekly_ai_analysis_tasks` 要的
    上下文（app、活跃配置、分组、水位线、后台任务表）与这条性质无关，构造出来的
    测试只会验证构造本身。真正的拦截行为由上面那条后台入口的用例钉着。
    """
    source = (PROJECT_ROOT / "services" / "task_worker_service.py").read_text(encoding="utf-8")
    body = source[source.index("def schedule_weekly_ai_analysis_tasks"):]
    body = body[: body.index("def schedule_repository_sync_tasks")]

    assert "budget_gate_reason" in body, "调度器没有查预算"
    assert body.index("budget_gate_reason") < body.index("create_weekly_ai_analysis_task("), (
        "预算判定排在建任务之后 —— 那还是会产生一个注定被跳过的后台任务"
    )
    assert "log_print" in body[body.index("budget_gate_reason"):], "跳过时没有写日志"


# ==========================================================================
# 六、单价表：改价必须改版本
# ==========================================================================


def _table(version: str, input_price: float = 1.0) -> str:
    return json.dumps(
        {"version": version, "currency": "CNY", "models": {"m": {"input": input_price, "output": 2.0}}}
    )


class TestThePriceVersionRule:
    def test_changing_a_price_without_bumping_the_version_is_rejected(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1", 1.0)})

            ok, message, errors = update_project_analysis_config(
                project_id, {"model_price_table": _table("v1", 9.0)}, updated_by="tester"
            )

            assert ok is False, "改了价、版本没改，却存进去了"
            assert errors and errors[0]["field"] == "model_price_table"
            assert "version" in message, message

        # 库里还是旧的那一份（校验失败一个字段都不落库）
        with flask_app.app_context():
            row = ai_service.get_project_analysis_config(project_id)
            assert json.loads(row["model_price_table"])["models"]["m"]["input"] == 1.0

    def test_changing_both_the_price_and_the_version_is_accepted(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1", 1.0)})
            _configure(project_id, {"model_price_table": _table("v2", 9.0)})

            row = ai_service.get_project_analysis_config(project_id)
            assert json.loads(row["model_price_table"])["version"] == "v2"

    def test_bumping_only_the_version_is_allowed(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1")})
            _configure(project_id, {"model_price_table": _table("v2")})

    def test_a_note_edit_alone_does_not_require_a_version_bump(self):
        """给一条模型加一句备注不会让历史费用对不上，不该逼着用户改版本号。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1")})
            _configure(
                project_id,
                {"model_price_table": json.dumps({
                    "version": "v1", "currency": "CNY",
                    "models": {"m": {"input": 1.0, "output": 2.0, "note": "内部价"}},
                })},
            )

    def test_the_first_configuration_has_no_previous_version_to_compare(self):
        """第一次填没有可比对的旧版本，不该被版本规矩拦住。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1", 3.0)})

    def test_clearing_the_table_is_allowed(self):
        """留空 = 移除项目价格表（回落到平台默认表），没有可比对的旧版本问题。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1")})
            _configure(project_id, {"model_price_table": ""})

            assert ai_service.get_project_analysis_config(project_id)["model_price_table"] == ""

    def test_an_invalid_json_is_reported_by_the_parser_not_the_version_rule(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _configure(project_id, {"model_price_table": _table("v1")})

            ok, _message, errors = update_project_analysis_config(
                project_id, {"model_price_table": "{ not json"}, updated_by="tester"
            )
            assert ok is False
            assert "JSON" in errors[0]["message"] or "JSON" in errors[0]["message"] or errors[0]["message"]


# ==========================================================================
# 七、单价表搬家后的真实风险：只提交单价表不能清掉别的字段
# ==========================================================================


def test_saving_only_the_price_table_keeps_every_other_field():
    """**这次改动的真实风险点。**

    单价表的入口从「AI 分析配置」搬到了消耗面板，那边保存时只发
    `{"model_price_table": ...}`。服务端是按 payload 里**出现过的**字段逐个 setattr 的，
    所以这条请求不该碰别的配置项 —— 但这件事必须有用例钉住：一旦哪天有人把它改成
    「先取默认值再整体覆盖」，用户点一次「保存单价表」就会把整份配置清成默认值，
    而界面上不会有任何提示。
    """
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(
            project_id,
            {
                "api_base_url": "https://api.example.com/v1",
                "api_model": "fake-model",
                "max_analysis_rounds": 5,
                "min_severity": "critical",
                "max_anomalies_per_run": 3,
                "budget_period": "weekly",
                "budget_token_limit": 123456,
                "budget_cost_limit": "9.5",
                "project_knowledge": "只看配表",
                "prompt_template": "补充指令",
            },
        )
        before = ai_service.get_project_analysis_config(project_id)

        _configure(project_id, {"model_price_table": _table("v1")})

        after = ai_service.get_project_analysis_config(project_id)

        for key in (
            "api_base_url", "api_model", "max_analysis_rounds", "min_severity",
            "max_anomalies_per_run", "budget_period", "budget_token_limit",
            "budget_cost_limit", "project_knowledge", "prompt_template",
        ):
            assert after[key] == before[key], f"只提交单价表，`{key}` 却被改掉了"
        assert after["model_price_table"] == _table("v1")


def test_the_panel_route_only_sends_the_price_table(client, monkeypatch):
    """端到端：从消费面板那条路保存，别的字段也不动。"""
    with flask_app.app_context():
        create_tables()
        project_id = _project()
        _configure(project_id, {"max_analysis_rounds": 7, "budget_token_limit": 555})

    monkeypatch.setattr(ai_routes, "_has_project_admin_access", lambda _pid: True)
    monkeypatch.setattr(ai_routes, "_get_current_user", lambda: None)

    endpoint = flask_app.view_functions["ai_analysis_routes.ai_project_config_update"]
    with flask_app.test_request_context(
        f"/ai-analysis/projects/{project_id}/config",
        method="POST",
        json={"model_price_table": _table("v1")},
    ):
        resp = make_response(endpoint(project_id))

        assert resp.status_code == 200
        with flask_app.app_context():
            config = ai_service.get_project_analysis_config(project_id)
            assert config["max_analysis_rounds"] == 7
            assert config["budget_token_limit"] == 555
            assert config["model_price_table"] == _table("v1")


# ==========================================================================
# 八、迁移与元组同步
# ==========================================================================


def test_the_budget_columns_are_in_the_model_the_migration_and_the_tuple():
    """新列要在三处同时存在：模型、加列迁移、测试里的元组。

    只改两处的后果是**启动期**报 `no such column`：新库（create_all）看起来一切正常，
    而已经部署出去的库在第一次读配置时就炸。
    """
    from services.db_migration_service import _migrate_ai_analysis_columns  # noqa: F401
    from tests.test_ai_models_and_migration import CONFIG_NEW_COLUMNS

    columns = set(ai_service.AiProjectAnalysisConfig.__table__.columns.keys())
    expected = {"budget_period", "budget_token_limit", "budget_cost_limit"}

    assert expected.issubset(columns), f"模型缺列：{expected - columns}"
    assert expected.issubset(set(CONFIG_NEW_COLUMNS)), (
        "新列没有进 `CONFIG_NEW_COLUMNS` —— 加列迁移的断言就管不到它们了"
    )
    # 迁移的 DDL 字典里也要有（键名与 DDL 不一致时，第二遍迁移会永远报「列已存在」）
    source = (PROJECT_ROOT / "services" / "db_migration_service.py").read_text(encoding="utf-8")
    for name in expected:
        assert f'"{name}"' in source, f"加列迁移里没有 {name}"


def test_the_budget_migration_is_idempotent(tmp_path):
    """重复执行必须无害：启动时每次都会跑一遍。

    键名写错而 DDL 正确时，第一遍能成功、之后每次启动都会再 ALTER 一次并因
    「列已存在」报错 —— 错误被兜住只打个告警，于是日志里永远躺着一条迁移失败。
    """
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import Session

    from services.db_migration_service import _migrate_ai_analysis_columns

    old_ddl = """
    CREATE TABLE ai_project_analysis_config (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL,
        auto_weekly_enabled BOOLEAN,
        weekly_interval_minutes INTEGER,
        max_files_per_run INTEGER,
        prompt_template TEXT,
        updated_by VARCHAR(100),
        created_at DATETIME,
        updated_at DATETIME
    )
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'budget.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(old_ddl)

    class _Stub:
        def __init__(self, eng):
            self.engine = eng
            self.session = Session(eng)

    stub = _Stub(engine)
    messages: list[str] = []

    def _log(*args, **_kwargs):
        messages.append(" ".join(str(arg) for arg in args))

    _migrate_ai_analysis_columns(stub, _log)
    columns = {column["name"] for column in inspect(engine).get_columns("ai_project_analysis_config")}
    assert {"budget_period", "budget_token_limit", "budget_cost_limit"}.issubset(columns)

    messages.clear()
    _migrate_ai_analysis_columns(stub, _log)

    failures = [message for message in messages if "失败" in message]
    assert not failures, f"第二遍迁移报了失败：{failures}"
    stub.session.close()
    engine.dispose()


# ==========================================================================
# 九、前端：令牌、图标、等宽数字、超预算的可见原因
# ==========================================================================

EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿️⬀-⯿]")


def _declarations(css: str) -> str:
    """只留声明，去掉注释 —— 注释里**故意**写着反例（「不能写 outline: none」这类），
    按字面搜会把说明文字当成规则。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _added_dashboard_css() -> str:
    """只取本轮新增的那一段样式（筛选栏 + 单价表编辑器）。

    页面原有的规则里有几个裸十六进制（hero 渐变、骨架屏），它们不在本次改动范围内 ——
    对整块 <style> 做断言等于顺手逼人重写无关的样式，而且改错了看不出来。
    """
    text = _read(DASHBOARD)
    style = text[text.index("<style>"):text.index("</style>")]
    start = style.index("本轮新增：筛选栏 + 模型单价表编辑器")
    end = style.index("@media (max-width: 767px)")
    assert start < end, "找不到本轮新增的样式段"
    return style[start:end]


class TestTheFrontendHoldsTheHouseRules:
    def test_the_dashboard_never_uses_emoji_as_icons(self):
        found = EMOJI.findall(_read(DASHBOARD))
        assert not found, f"消耗面板里出现了 emoji 图标：{found[:5]}"

    @pytest.mark.parametrize("path", [PROJECT_VIEW, WEEKLY_DRAWER, COMMIT_DRAWER])
    def test_the_ai_sections_never_use_emoji_as_icons(self, path):
        """只看**这一轮碰过的那几段**。

        这三份模板别处本来就有 emoji（历史遗留），对整个文件做断言会逼着后来的人去改
        与本次改动无关的地方；而 AI 这一块是纯 FontAwesome 的，新加的东西也不能破例。
        范围与 `test_ai_config_template.py::test_icons_are_font_awesome_not_emoji` 一致。
        """
        source = _read(path)
        chunks = []
        if path == PROJECT_VIEW:
            chunks.append(source[source.index("// AI 分析配置：页面摘要行 + 居中模态框"):
                                 source.index("function normalizeRiskLevel(level) {")])
            at = source.index('id="aiBudgetPeriodSelect"')
            chunks.append(source[max(0, at - 8000):at + 8000])
        else:
            at = source.index("budget.over")
            chunks.append(source[max(0, at - 4000):at + 4000])
        for chunk in chunks:
            found = EMOJI.findall(chunk)
            assert not found, f"{path} 的 AI 段里出现了 emoji 图标：{found[:5]}"

    def test_the_dashboard_uses_design_tokens_not_raw_hex(self):
        """颜色一律走 `var(--token, 字面量)`。

        `--aiu-*` 别名层里的字面量是**兜底值**，本来就该在那儿；先把它摘掉，
        剩下的十六进制才是「没用 token」。
        """
        css = _declarations(_added_dashboard_css())
        without_fallbacks = re.sub(r"var\(--[\w-]+,\s*#[0-9a-fA-F]{3,8}\)", "VAR", css)
        leftovers = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_fallbacks)
        assert not leftovers, f"本轮新增的样式里出现了不走 token 的颜色：{leftovers}"

    def test_the_dashboard_braces_are_balanced(self):
        text = _read(DASHBOARD)
        style = _declarations(text[text.index("<style>"):text.index("</style>")])
        assert style.count("{") == style.count("}")

    def test_the_numeric_columns_use_tabular_figures(self):
        """数据列要用等宽数字，否则刷新时数字宽度一跳一跳，整列都在动。"""
        css = _declarations(_read(DASHBOARD))
        assert "font-variant-numeric: tabular-nums" in css
        assert ".aiu-price-table th.num" in css

    def test_the_filters_write_their_state_into_the_url(self):
        """筛选状态要进 URL query（刷新、分享、收藏都不丢），并且**在服务端聚合**。"""
        script = _read(DASHBOARD)

        assert "history.replaceState" in script, "筛选没有写进地址栏"
        assert "URLSearchParams" in script
        assert "usage/overview' + (query ? '?' + query : '')" in script, (
            "总览接口没有带上筛选条件 —— 那就成了「拉全量再前端过滤」"
        )
        assert "usage/project/' + projectId + (query ? '?' + query : '')" in script

    def test_the_clear_and_reset_buttons_are_not_destructive(self):
        """「清空 / 重置」只动筛选状态，这个页面上不该有删除用量记录的入口。"""
        script = _read(DASHBOARD)

        assert "aiuFilterClearBtn" in script and "aiuFilterResetBtn" in script
        # 这个页面上唯一的写操作是「保存单价表」，而且它只打配置接口；
        # 删用量记录那类动作一个都不该有。
        for banned in ("method: 'DELETE'", "method: 'PUT'", "method: 'PATCH'"):
            assert banned not in script, f"消耗面板上出现了破坏性写操作：{banned}"
        assert script.count("method: 'POST'") == 1, "多了一个写请求"
        assert "projects/' + projectId + '/config'" in script, "那个 POST 不是打配置接口的"

    def test_the_budget_column_says_what_unlimited_means(self):
        script = _read(DASHBOARD)
        assert "未设上限" in script, "没配预算时那一格该说「未设上限」，而不是显示 0"
        assert "已超" in script and "aiu-budget-over" in script

    def test_the_dashboard_configures_the_price_table_and_explains_the_version_rule(self):
        html = _read(DASHBOARD)
        script = _read(DASHBOARD)

        assert 'id="aiuPriceJsonInput"' in html
        assert 'id="aiuPriceRows"' in html
        assert 'id="aiuPriceAddRowBtn"' in html
        assert "每 100 万 token" in html
        assert "不做包含匹配" in html
        assert "改单价必须同时改版本" in html
        # 保存时只提交这一个字段（服务端按出现过的字段 setattr）
        assert "JSON.stringify({ model_price_table: text })" in script

    def test_the_price_editor_reports_duplicates_and_negatives_in_plain_chinese(self):
        """校验要严，错误要给人看得懂：模型名重复、负数、缺必填都要各自说出来。"""
        script = _read(DASHBOARD)

        assert "重复了" in script
        assert "必须是非负数字" in script
        assert "输入价与输出价都必须填" in script
        assert "疑似填反了" in script, "命中价比输入价还贵这种填反了的情况要单独提示"

    def test_the_price_editor_keeps_the_keys_it_has_no_input_for(self):
        """`note` / `cache_write` 没有输入框，但丢掉它们就是**丢数据** ——
        用户手写的备注会在一次「保存」之后无声消失。"""
        script = _read(DASHBOARD)
        assert "AIU_PRICE_KEEP_KEYS" in script
        assert "'note'" in script and "'cache_write'" in script


class TestThePriceTableLeftTheProjectConfigModal:
    """单价表搬到了消耗面板，项目配置那边必须**搬干净**。

    留下一半（界面没有这一栏、提交时却还带着它）比不搬更糟：用户改完 JSON 点保存，
    界面回填的是服务端那份，而他以为自己的改动生效了。
    """

    def test_the_project_config_modal_has_no_price_table_field(self):
        html = _read(PROJECT_VIEW)
        for marker in (
            'id="aiPriceTableInput"',
            'for="aiPriceTableInput"',
            'id="aiPriceTableInputError"',
            'id="aiPriceTableRows"',
            'id="aiPriceTableDoc"',
            "ai-price-editor",
            "ai-price-json",
            "ai-price-doc",
        ):
            assert marker not in html, f"项目配置里还留着单价表的 {marker}"

    def test_the_project_config_script_never_submits_the_price_table(self):
        source = _read(PROJECT_VIEW)
        script = source[source.index("// AI 分析配置：页面摘要行 + 居中模态框"):source.index("function normalizeRiskLevel(level) {")]
        code = re.sub(r"//[^\n]*", " ", script)

        assert "model_price_table" not in code, "界面没有这一栏，提交时却还带着它"
        assert "aiPrice" not in code, "单价表的脚本没搬干净"

    def test_the_field_schema_still_validates_the_price_table(self):
        """字段规则留着：消耗面板走的还是同一个保存接口，校验必须仍然生效。

        字段还在 `FIELD_RULES` 里 = 服务端照旧把非法 JSON / 负数 / 缺必填挡在门外，
        与界面在哪一页无关。
        """
        from services.ai.endpoint_service import FIELD_RULES, describe_field_schema

        assert "model_price_table" in FIELD_RULES
        schema = describe_field_schema()["model_price_table"]
        assert schema["kind"] == "price_table"
        assert schema["max_length"] > 0, "消耗面板要用它做长度上限"


class TestTheBudgetConfigFields:
    def test_the_modal_shows_all_three_budget_fields_with_labels_and_error_slots(self):
        html = _read(PROJECT_VIEW)
        for dom_id in ("aiBudgetPeriodSelect", "aiBudgetTokenInput", "aiBudgetCostInput"):
            assert f'id="{dom_id}"' in html, f"缺少 {dom_id}"
            assert f'for="{dom_id}"' in html, f"{dom_id} 没有 label"
            assert f'id="{dom_id}Help"' in html, f"{dom_id} 没有帮助文案"
            assert f'id="{dom_id}Error"' in html, f"{dom_id} 没有内联错误位置"

    def test_the_fields_say_that_blank_means_unlimited(self):
        html = _read(PROJECT_VIEW)
        text = html[html.index('id="aiBudgetTokenInputHelp"'):]
        text = text[: text.index("</div>") + 6]
        assert "留空 = 不限制" in text.replace("<strong>", "").replace("</strong>", "")

    def test_the_ranges_are_not_hardcoded_in_the_markup(self):
        """范围只有一份（`FIELD_RULES`），写在 HTML 里就会出现「界面写 1~30、
        后端按别的范围校验」—— 用户按界面提示填，保存却报错。"""
        html = _read(PROJECT_VIEW)
        for dom_id in ("aiBudgetTokenInput", "aiBudgetCostInput"):
            tag = re.search(rf'<input[^>]*id="{dom_id}"[^>]*>', html, re.S)
            assert tag, f"找不到 {dom_id}"
            assert not re.search(r'\b(min|max)="', tag.group(0)), f"{dom_id} 把范围写死在 HTML 里了"

    def test_the_script_reads_the_range_from_the_schema_for_optional_fields(self):
        script = _read(PROJECT_VIEW)
        assert "AI_RANGE_KINDS" in script
        assert "'optional_int'" in script and "'optional_money'" in script


class TestTheDrawersSayWhyTheButtonIsDisabled:
    """超预算时按钮要禁用，**而且要写明原因** —— 一个只变灰、不给理由的按钮，
    用户只会以为页面坏了。"""

    @pytest.mark.parametrize("path", [WEEKLY_DRAWER, COMMIT_DRAWER, PROJECT_VIEW])
    def test_every_drawer_checks_the_budget(self, path):
        source = _read(path)
        assert "budget.over" in source or "budget && budget.over" in source, (
            f"{path} 没有看预算状态"
        )

    @pytest.mark.parametrize("path", [WEEKLY_DRAWER, COMMIT_DRAWER, PROJECT_VIEW])
    def test_every_drawer_shows_the_reason_not_just_a_disabled_button(self, path):
        source = _read(path)
        assert "budget.reason" in source or "budget.reason" in source, f"{path} 没有显示拦截原因"
        assert "调整方式" in source, f"{path} 没有告诉用户怎么恢复"

    def test_the_drawers_disable_before_they_check(self):
        """按钮先禁用再判定，不能「先放开、再禁用」——那等于没禁用。"""
        source = _read(PROJECT_VIEW)
        handler = source[source.index("openWeeklyAiDrawer(configId, versionName);"):]
        handler = handler[: handler.index("// 页面加载时获取统计信息")]

        # 顺序必须是：先禁用 → 查预算 → 超了就 return（按钮保持禁用）→ 否则才放开。
        disable = handler.index("drawerStartBtn.disabled = true;")
        check = handler.index("budget.over")
        enable = handler.index("drawerStartBtn.disabled = false;")
        assert disable < check < enable, "按钮的禁用/启用与预算判定的顺序不对"
        assert "return;" in handler[check:enable], (
            "超预算那一支没有提前 return —— 后面那句会把按钮重新放开"
        )
