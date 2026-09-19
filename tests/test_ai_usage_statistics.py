# -*- coding: utf-8 -*-
"""AI 消耗统计的口径：**设置起点**（可逆）与**全量重置**（不可逆）。

## 这个文件守的五条性质

1. **起点只改这一页的口径，绝不碰预算闸门。** 面板上的数字是「这个版本花了多少」，
   一旦有人在同一个库上反复试跑调提示词，它就永远带着噪音 —— 所以要有「从这里重新算」。
   但同一次点击**不许**让额度回去：`services/ai/analysis_budget.py` 的模块说明点名警告过
   「被人当成『清账』按钮」这件事。本文件的那条红线回归就是钉这个的
   （`TestTheBudgetDoesNotFollowTheBaseline`：设起点前后预算「已用」逐字相同）。
2. **起点是「读不到 = 统计全部」**，不是「读不到 = 什么都不统计」。两者的差别是
   一次没跑过迁移的库会变成永远的空表，而页面上找不到任何解释。
3. **起点非法时不静默回落。** 格式看不懂、或者填了个未来时间 —— 都要报错并且**一个字段
   都不改**。静默当成「清空起点」的话，用户看到的是「我填错了，然后数字变多了」；
   未来时间更糟：之后的所有运行都被排除，页面永远是空的，而用户以为自己在「重置」。
4. **全量重置是真删**（`ai_analysis_run` / `_trace` / `_anomaly` 连同报告正文），
   所以：确认词不对不执行、**有在途运行就拒绝**（否则那条运行回写时会踩到已被删掉的行，
   报出来的是「分析失败」而不是「你刚重置过」），删完把起点与周版本的「上一次运行」指针
   一起清干净（留着就是脏指针）。
5. **两个写接口都要平台管理员。** 起点是平台级口径（它同时改变所有项目在面板上的数字），
   重置删的是全平台的记录 —— 两者都不是项目管理员该碰的。读侧的状态行对所有人下发，
   「谁能改」是 `statistics.can_manage`，两件事分开写。

**测试库是会话级共用的**（没有逐用例重置），所以：起点是单行表 → 每个用例前后各清一遍
（`_isolate_statistics`，理由同 `tests/test_ai_platform_budget.py` 里的平台预算）；
重置会清空整张运行表 → 断言计数前先 `_clear_runs()` 让自己独占这张表。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import (
    AiAnalysisAnomaly,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiUsageStatistics,
    AiWeeklyAnalysisState,
)
from services.ai.analysis_budget import budget_status, platform_budget_status
from services.ai.platform_budget import set_platform_budget
from services.ai.usage_statistics import (
    IN_FLIGHT_STATUSES,
    RESET_CONFIRM_WORD,
    get_usage_statistics,
    in_flight_runs,
    is_counted,
    parse_baseline_input,
    purge_usage_statistics,
    set_usage_baseline,
    statistics_public,
    usage_baseline,
)
from services.ai_analysis_service import update_project_analysis_config
from services.ai_usage_service import UsageFilters, project_usage, usage_overview

BASELINE_URL = "/ai-analysis/statistics/baseline"
RESET_URL = "/ai-analysis/statistics/reset"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _project() -> int:
    project = Project(code=_uid("P"), name=_uid("stats"))
    db.session.add(project)
    db.session.flush()
    return project.id


def _configure(project_id: int, payload: dict) -> None:
    ok, message, errors = update_project_analysis_config(project_id, payload, updated_by="tester")
    assert ok, (message, errors)


def _now() -> datetime:
    """当前时刻的 **naive UTC**（与 `created_at` 落库后的形状逐字一致）。

    刻意不用 `datetime.now(timezone.utc)`：`is_counted` 比的是库里的值，而这里的用例
    要造的是「历史数据」，写 aware 的值只会在同一 session 内多看一层 tzinfo 折算 ——
    那不是被测的代码路径。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _run(
    project_id: int,
    *,
    created_at: datetime | None = None,
    status: str = "succeeded",
    tokens_input: int = 1000,
    tokens_output: int = 200,
) -> AiAnalysisRun:
    moment = created_at or _now()
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        # target_id 用 project_id：各用例的项目号互不相同，断言「这个目标没有留下 run」
        # 时才不会被别的用例的行撞上。
        target_id=project_id,
        target_key=f"g-{uuid.uuid4().hex[:8]}",
        status=status,
        scope="full",
        trigger_source="manual",
        response_text="结论",
        model="fake-model",
        created_at=moment,
        started_at=moment,
        finished_at=moment,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
    )
    db.session.add(run)
    db.session.flush()
    db.session.commit()
    return run


def _trace(run_id: int) -> AiAnalysisTrace:
    trace = AiAnalysisTrace(run_id=run_id, round_index=1, outcome="ok")
    db.session.add(trace)
    db.session.flush()
    return trace


def _anomaly(run_id: int, project_id: int) -> AiAnalysisAnomaly:
    anomaly = AiAnalysisAnomaly(
        run_id=run_id, project_id=project_id, title="标题对不上", fingerprint="f" * 32
    )
    db.session.add(anomaly)
    db.session.flush()
    return anomaly


def _clear_runs() -> None:
    """清空运行记录（连同子表）。

    **子表必须先删**：批量 delete 不走 ORM 级联，别的用例留下的 trace/anomaly 行会让
    这条 DELETE 撞上外键约束，而且**只在全量跑时炸**（同
    `tests/test_ai_platform_budget.py::_clear_runs` 记的那个坑）。
    """
    AiAnalysisTrace.query.delete()
    AiAnalysisAnomaly.query.delete()
    AiAnalysisRun.query.delete()
    db.session.commit()


def _clear_statistics() -> None:
    for row in AiUsageStatistics.query.all():
        db.session.delete(row)
    db.session.commit()


@pytest.fixture(autouse=True)
def _isolate_statistics():
    """每个用例前后都清一遍统计口径行。

    它是**全局单行表**：留一个起点下来，其它文件里所有 `usage_overview` 的合计都会
    无端变小，而失败信息看起来与被测代码毫无关系（平台预算那张表当初就是这么坑的）。
    先 `create_tables()`：会话级共用的测试库在第一个用例之前可能还没有这张表。
    """
    with flask_app.app_context():
        create_tables()
        _clear_statistics()
        yield
        _clear_statistics()


def _status_of(resp):
    """取状态码。view 可能返回 `(response, 401)` 这种元组（`require_admin` 的 JSON 分支）。"""
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

    def post(self, url, **kwargs):
        return self._call(url, "POST", **kwargs)


@pytest.fixture()
def client():
    return _ViewClient()


def _overview(project_id: int) -> dict:
    """这一个项目的总览（权限范围收成它自己，别把别的用例的行算进来）。"""
    return usage_overview([project_id], UsageFilters(project_id=project_id))


def _overview_as(viewer_is_admin: bool, monkeypatch) -> dict:
    """以「平台管理员 / 普通用户」的身份走一次真实路由，回响应体。

    **必须走路由**：`can_manage` 是路由按 `platform_scope_visible()` 传进 service 的，
    直接调 service 只能验到「传了 False 会怎样」，验不到「路由到底传了没有」。
    """
    from utils import request_security

    monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
    monkeypatch.setattr(request_security, "_has_admin_access", lambda: viewer_is_admin)
    with flask_app.test_request_context("/ai-analysis/usage/overview"):
        resp = ai_routes.ai_usage_overview()
    return (resp[0] if isinstance(resp, tuple) else resp).get_json()


# ==========================================================================
# 一、起点的语义：没有行 = 全部历史
# ==========================================================================


class TestTheBaselineMeansEverythingUntilItIsSet:
    def test_no_row_at_all_means_the_whole_history(self):
        """**「读不到」必须等同于「统计全部」**。

        反过来（读不到 = 什么都不统计）的话，一次没跑过 `create_all`、或者迁移还没建这张
        表的库会显示一张全零的面板 —— 而页面上没有任何东西能解释这件事。
        """
        with flask_app.app_context():
            create_tables()
            _clear_statistics()

            assert usage_baseline() is None
            assert get_usage_statistics()["counted_since"] is None
            # 任何一条运行都算数，包括没有 `created_at` 的（那类记录进不了时间窗，
            # 但「全部历史」这一档下它照样显示）。
            assert is_counted(object(), None) is True

    def test_the_baseline_is_stored_as_utc_and_shown_as_beijing(self):
        """库里存 naive UTC，界面上给北京墙钟。**进出各换一次，别在别处再换。**"""
        with flask_app.app_context():
            create_tables()
            since, problem = parse_baseline_input("2026-09-01T08:00")

            assert problem is None
            # 北京时间 08:00 = UTC 00:00
            assert since.isoformat() == "2026-09-01T00:00:00"
            assert since.tzinfo is None, "落库的必须是 naive（SQLite 会丢 tzinfo）"

            ok, message, errors = set_usage_baseline(since, updated_by="tester")
            assert ok, (message, errors)

            stored = AiUsageStatistics.query.one()
            assert stored.counted_since.isoformat() == "2026-09-01T00:00:00"
            assert stored.updated_by == "tester"
            assert stored.reset_at is None, "设起点不是重置，不该留下重置痕迹"

            shown = get_usage_statistics()
            assert shown["counted_since"] == "2026-09-01T00:00:00"
            # `<input type="datetime-local">` 认的就是这个形状（北京墙钟）。
            assert shown["counted_since_local"] == "2026-09-01T08:00:00"
            assert "2026-09-01 08:00" in message

    def test_a_run_without_a_timestamp_is_counted_out_once_a_baseline_exists(self):
        """没有 `created_at` 的记录在有起点时**不计入**。

        它进不了任何时间窗，也就不该悄悄混进「起点之后」的合计里。
        """
        with flask_app.app_context():
            create_tables()
            run = AiAnalysisRun(project_id=1, target_type="weekly", status="succeeded")
            assert is_counted(run, datetime(2026, 1, 1)) is False

    def test_the_baseline_survives_a_naive_aware_mismatch(self):
        """库里读出来的 `created_at` 与刚写进去的对象，tzinfo 状态可能不同。

        直接比较会抛 `TypeError`（面板 500），或者差 8 小时。两边都过一遍 `as_utc`
        就只剩一种口径。
        """
        with flask_app.app_context():
            baseline = datetime(2026, 1, 1)
            aware_run = AiAnalysisRun(project_id=1, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            naive_run = AiAnalysisRun(project_id=1, created_at=datetime(2026, 1, 1))

            assert is_counted(aware_run, baseline) is True
            assert is_counted(naive_run, baseline) is True


class TestAnIllegalBaselineIsRejectedInWords:
    @pytest.mark.parametrize("raw", ["昨天", "2026/09/01", "2026-09-01T08:00+08:00 之后", "abc"])
    def test_an_unparseable_value_is_not_silently_treated_as_cleared(self, raw):
        """填错了就要报错。静默当 None 的话，界面上会变成「我填错了，然后数字变多了」。"""
        value, problem = parse_baseline_input(raw)

        assert value is None
        assert problem, f"没解析出来却不说为什么：{raw}"

    def test_a_future_baseline_is_refused(self):
        """未来时间会把**之后**的运行也排除掉：页面永远是空的，而用户以为自己在重置。"""
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        value, problem = parse_baseline_input(future.isoformat())

        assert value is None
        assert "未来" in problem, problem

    def test_empty_means_clear(self):
        """空串/None 是**明确的**「恢复全部历史」，不是错误。"""
        assert parse_baseline_input(None) == (None, None)
        assert parse_baseline_input("") == (None, None)
        assert parse_baseline_input("   ") == (None, None)


# ==========================================================================
# 二、面板读路径：起点之后才计数
# ==========================================================================


class TestThePanelCountsFromTheBaseline:
    def test_the_overview_and_the_drill_down_agree(self):
        """总览与下钻用的是**同一批**运行。

        两边各写一遍过滤的必然结果是某天开始对不上 ——「上面写 1 次、下面列 3 条」，
        而两个数字看上去都很正常。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            moment = _now()
            _run(project_id, created_at=moment - timedelta(days=3), tokens_input=700)
            _run(project_id, created_at=moment - timedelta(hours=2), tokens_input=1000)
            _run(project_id, created_at=moment - timedelta(hours=1), tokens_input=1000)

            set_usage_baseline(moment - timedelta(days=1), updated_by="tester")

            overview = _overview(project_id)
            assert overview["totals"]["runs"] == 2, overview["totals"]
            # token 是只看得到两条的：起点之前那条的 700 一个都不许混进来。
            assert overview["totals"]["tokens"]["input"] == 2000, overview["totals"]["tokens"]
            assert [item["runs"] for item in overview["projects"]] == [2]
            assert overview["statistics"]["counted_since"] is not None
            assert overview["statistics"]["excluded_runs"] == 1

            drill = project_usage(project_id, UsageFilters())
            assert drill["totals"]["runs"] == 2, drill["totals"]
            assert len(drill["runs"]) == 2
            assert sum(item["runs"] for item in drill["weekly_versions"]) == 2

    def test_excluded_runs_counts_only_what_this_screen_dropped(self):
        """「此前 N 次未计入」数的必须是**眼前这一屏**被排除了多少条。

        在筛选之前数的话，筛到上个月时那个数字与眼前的表毫无关系，用户会以为自己在看
        一个残缺的列表。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            moment = _now()
            # 起点之前的都是成功的，失败的只有起点之后那一条 —— 这样「只看失败」
            # 这一屏的筛选结果与起点完全不相交。
            _run(project_id, created_at=moment - timedelta(days=10), status="succeeded")
            _run(project_id, created_at=moment - timedelta(hours=1), status="failed")

            set_usage_baseline(moment - timedelta(days=5), updated_by="tester")

            # 全部状态：两条都过了筛选，其中一条在起点之前 → 这一屏排除了 1 条。
            assert _overview(project_id)["statistics"]["excluded_runs"] == 1
            # 只看失败：那一条本来就不在当前筛选里，所以这一屏一条都没被排除。
            failed_only = usage_overview(
                [project_id], UsageFilters(project_id=project_id, status="failed")
            )
            assert failed_only["statistics"]["excluded_runs"] == 0, failed_only["statistics"]
            assert failed_only["totals"]["runs"] == 1

    def test_clearing_the_baseline_restores_every_number(self):
        """可逆性 —— 这正是「起点」与「全量重置」必须分开做两个功能的原因。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            moment = _now()
            _run(project_id, created_at=moment - timedelta(days=3), tokens_input=700)
            _run(project_id, created_at=moment - timedelta(hours=1), tokens_input=1000)

            before = _overview(project_id)["totals"]
            set_usage_baseline(moment - timedelta(days=1), updated_by="tester")
            narrowed = _overview(project_id)["totals"]
            ok, message, _ = set_usage_baseline(None, updated_by="tester")
            after = _overview(project_id)["totals"]

            assert ok, message
            assert usage_baseline() is None
            assert narrowed["runs"] < before["runs"]
            assert after == before, (after, before)


class TestTheBudgetDoesNotFollowTheBaseline:
    """**红线回归**：起点只改这一页的口径，绝不进预算闸门。

    接错了的后果不是「数字难看」，而是「点一下设置起点 = 把额度送回去」——
    `services/ai/analysis_budget.py` 的模块说明点名警告过这种用法。
    """

    def _snapshot(self, project_id: int) -> tuple[dict, dict, dict]:
        """闸门的两档 + 面板上那一格（下钻里的 `budget`）。

        面板那一格从**下钻**取而不是总览：起点把所有运行都挡住之后，总览里这个项目
        连行都不剩了（总览的行是按运行分组建出来的），拿 `projects[0]` 会 IndexError。
        下钻的预算块是**无条件**算的，正好用来验证「它没跟着起点变」。
        """
        return (
            budget_status(project_id),
            platform_budget_status(),
            project_usage(project_id, UsageFilters())["budget"],
        )

    def test_the_used_amount_is_identical_before_and_after(self):
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _configure(project_id, {"budget_token_limit": 1_000_000})
            set_platform_budget({"budget_token_limit": 5_000_000}, updated_by="tester")
            moment = _now()
            _run(project_id, created_at=moment - timedelta(days=2), tokens_input=50_000, tokens_output=0)

            project_before, platform_before, block_before = self._snapshot(project_id)

            # 起点设在所有运行之后：这一页从此「一次运行都没有」。
            set_usage_baseline(moment, updated_by="tester")
            overview = _overview(project_id)
            assert overview["totals"]["runs"] == 0
            # 连项目行都不剩 —— 总览的行是**按运行分组建出来的**，一条运行都没有就没有行。
            # 这是「起点之前的运行完全不显示」的直接后果，不是 bug：界面上此时该说的是
            # 「统计起点之后还没有分析」，而不是画一行 0。
            assert overview["projects"] == []
            assert overview["statistics"]["excluded_runs"] == 1

            project_after, platform_after, block_after = self._snapshot(project_id)

            assert project_after["used"] == project_before["used"], (
                "预算的「已用」跟着统计起点变了 —— 起点被接进闸门了"
            )
            assert platform_after["used"] == platform_before["used"]
            assert project_after["over"] == project_before["over"] is False
            # 面板上那一格（走 `budget_status`）也必须逐字相同。
            assert block_after == block_before

    def test_the_gate_still_blocks_after_a_baseline_hides_the_spending(self):
        """反过来钉一次：起点把这一页清空之后，**闸门照样拦**。

        只比 `used` 还不够 —— 万一哪天有人把 `over` 也接上起点，
        「面板说没超、分析照样被拦」会变成新的谜题。
        """
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _configure(project_id, {"budget_token_limit": 10_000})
            _run(project_id, tokens_input=50_000, tokens_output=0)
            assert budget_status(project_id)["over"] is True

            set_usage_baseline(_now(), updated_by="tester")

            assert _overview(project_id)["totals"]["runs"] == 0
            assert budget_status(project_id)["over"] is True


# ==========================================================================
# 三、全量重置：真删，且不可逆
# ==========================================================================


class TestTheFullReset:
    def test_it_deletes_runs_and_everything_hanging_off_them(self):
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            _clear_statistics()
            project_id = _project()
            first = _run(project_id)
            second = _run(project_id)
            _trace(first.id)
            _trace(second.id)
            _anomaly(first.id, project_id)

            ok, message, deleted = purge_usage_statistics(updated_by="tester")

            assert ok, message
            assert deleted == {"runs": 2, "traces": 2, "anomalies": 1}, deleted
            assert AiAnalysisRun.query.count() == 0
            assert AiAnalysisTrace.query.count() == 0
            assert AiAnalysisAnomaly.query.count() == 0
            assert "2" in message
            # 起点没有意义了（一条运行都不剩），留着只会在界面上显示一个不存在的「此前」。
            assert usage_baseline() is None
            stats = get_usage_statistics()
            assert stats["counted_since"] is None
            assert stats["reset_at"] is not None
            assert stats["reset_by"] == "tester"

    def test_it_clears_the_weekly_pointer(self):
        """`ai_weekly_analysis_state.last_analysis_run_id` 指向被删掉的行时就是脏指针。

        这一列当前只写不读，但留着一个指向不存在记录的外键值，迟早有人当真。
        """
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            run = _run(project_id)
            state = AiWeeklyAnalysisState(
                project_id=project_id,
                group_key=_uid("g"),
                last_analysis_run_id=run.id,
            )
            db.session.add(state)
            db.session.commit()

            ok, message, _ = purge_usage_statistics(updated_by="tester")

            assert ok, message
            db.session.expire_all()
            assert AiWeeklyAnalysisState.query.filter_by(id=state.id).one().last_analysis_run_id is None

    def test_it_clears_the_baseline_so_the_next_visit_is_not_confusing(self):
        """重置之后再显示「此前 N 次未计入」就是在说一件已经不存在的事。"""
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _run(project_id)
            set_usage_baseline(_now() - timedelta(days=1), updated_by="tester")
            assert usage_baseline() is not None

            ok, _, _ = purge_usage_statistics(updated_by="tester")

            assert ok
            assert usage_baseline() is None

    @pytest.mark.parametrize("status", IN_FLIGHT_STATUSES)
    def test_it_refuses_while_a_run_is_still_in_flight(self, status):
        """**在途运行存在时直接拒绝。**

        删掉那一行之后，正在跑的分析回写结果时会踩空 —— 用户看到的是「分析失败」，
        而不是「你刚重置过」。他不知道这两件事有关，只会去查分析为什么坏。
        """
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _run(project_id)
            _run(project_id, status=status)

            ok, message, deleted = purge_usage_statistics(updated_by="tester")

            assert ok is False
            assert deleted == {}
            assert "没有结束" in message, message
            assert in_flight_runs() == 1
            # 一条都不许删：包括那条已经跑完的。
            assert AiAnalysisRun.query.count() == 2

    def test_a_long_stale_running_row_still_blocks(self):
        """超时很久的 `running` 照样挡住 —— 这是**刻意的**。

        `effective_status` 会把超时的 running 显示成 failed，但重置要防的是「这条记录还会
        被回写」，而超时只是「看起来死了」，进程可能只是慢。所以这里看库里的原值。
        挡住不会卡死：卡死的那条会被调度器在超时后重置成失败（提示「AI分析任务超时，
        已被调度器重置」），届时即可重置 —— 这句话也写在给用户的拒绝原因里。
        """
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _run(project_id, created_at=_now() - timedelta(hours=5), status="running")

            ok, message, _ = purge_usage_statistics(updated_by="tester")

            assert ok is False
            assert "调度器" in message, message
            assert AiAnalysisRun.query.count() == 1

    def test_the_refusal_leaves_no_trace_of_a_partial_delete(self):
        """被拒时必须**什么都没动**：一半删一半留比直接失败更难收拾。"""
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            _clear_statistics()
            project_id = _project()
            run = _run(project_id)
            _trace(run.id)
            _run(project_id, status="running")

            purge_usage_statistics(updated_by="tester")

            assert AiAnalysisRun.query.count() == 2
            assert AiAnalysisTrace.query.count() == 1
            assert AiUsageStatistics.query.count() == 0


# ==========================================================================
# 四、路由与权限
# ==========================================================================


class TestTheEndpoints:
    def test_both_writes_need_the_platform_admin(self, monkeypatch):
        """起点与重置都要平台管理员：一个改全平台的口径，一个删全平台的记录。"""
        from utils import request_security

        monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
        monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)

        views = [
            (flask_app.view_functions["ai_analysis_routes.ai_statistics_baseline_update"],
             {"json": {"since": None}}),
            (flask_app.view_functions["ai_analysis_routes.ai_statistics_reset"],
             {"json": {"confirm": RESET_CONFIRM_WORD}}),
        ]
        with flask_app.app_context():
            create_tables()
            _clear_statistics()
            for view, kwargs in views:
                with flask_app.test_request_context(
                    "/ai-analysis/statistics", method="POST",
                    headers={"Accept": "application/json"}, **kwargs,
                ):
                    denied = view()
                assert _status_of(denied) in (401, 403), (
                    f"{view.__name__} 让非管理员通过了：{denied!r}"
                )

        # 管理员两次都通（证明拒的是权限，不是端点本身坏了）。
        monkeypatch.setattr(request_security, "_has_admin_access", lambda: True)
        with flask_app.test_request_context(
            "/ai-analysis/statistics", method="POST",
            headers={"Accept": "application/json"}, json={"since": None},
        ):
            allowed = views[0][0]()
        assert _status_of(allowed) == 200, allowed

    def test_the_baseline_endpoint_writes_and_clears(self, client, monkeypatch):
        monkeypatch.setattr("utils.request_security._has_admin_access", lambda: True)
        with flask_app.app_context():
            create_tables()
            _clear_statistics()

        resp = client.post(BASELINE_URL, json={"since": "2026-09-01T08:00"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["success"] is True
        # 保存后直接回一份新状态：界面不必再发一次 GET（那会把「保存成功」与
        # 「状态刷新」变成两次可能不一致的往返）。
        assert body["statistics"]["counted_since_local"] == "2026-09-01T08:00:00"
        assert usage_baseline().isoformat() == "2026-09-01T00:00:00"

        resp = client.post(BASELINE_URL, json={"since": None})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["statistics"]["counted_since"] is None
        assert usage_baseline() is None

    def test_a_missing_since_key_is_an_error_not_a_clear(self, client, monkeypatch):
        """`since` 必须出现（哪怕是 null）。

        键缺失时静默清除的话，一个拼错的字段名（`since_at`）会让用户以为「只是没设成」，
        而起点其实已经被清掉了。
        """
        monkeypatch.setattr("utils.request_security._has_admin_access", lambda: True)
        with flask_app.app_context():
            create_tables()
            _clear_statistics()
            set_usage_baseline(datetime(2026, 9, 1), updated_by="tester")

        resp = client.post(BASELINE_URL, json={"since_at": "2026-09-02T08:00"})

        assert resp.status_code == 400
        assert resp.get_json()["errors"][0]["field"] == "since"
        assert usage_baseline() is not None, "键名写错却把起点清掉了"

    @pytest.mark.parametrize("raw", ["昨天", "2026/09/01"])
    def test_a_bad_value_changes_nothing(self, client, monkeypatch, raw):
        monkeypatch.setattr("utils.request_security._has_admin_access", lambda: True)
        with flask_app.app_context():
            create_tables()
            _clear_statistics()
            set_usage_baseline(datetime(2026, 9, 1), updated_by="tester")

        resp = client.post(BASELINE_URL, json={"since": raw})

        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        assert usage_baseline().isoformat() == "2026-09-01T00:00:00"

    def test_the_reset_endpoint_needs_the_confirm_word(self, client, monkeypatch):
        """确认词不是安全边界（权限才是），它防的是误触 —— 这个动作没有撤销。"""
        monkeypatch.setattr("utils.request_security._has_admin_access", lambda: True)
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _run(project_id)

        for wrong in ("", "清空", "reset", None):
            resp = client.post(RESET_URL, json={"confirm": wrong})
            assert resp.status_code == 400, (wrong, resp.get_data(as_text=True))
            assert RESET_CONFIRM_WORD in resp.get_json()["message"]
        with flask_app.app_context():
            assert AiAnalysisRun.query.count() == 1, "确认词不对却删了东西"

        resp = client.post(RESET_URL, json={"confirm": RESET_CONFIRM_WORD})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["success"] is True
        assert body["deleted"] == {"runs": 1, "traces": 0, "anomalies": 0}
        assert body["statistics"]["reset_at"] is not None
        with flask_app.app_context():
            assert AiAnalysisRun.query.count() == 0

    def test_a_refused_reset_comes_back_as_a_400_with_the_reason(self, client, monkeypatch):
        """在途运行 → 拒绝，而且要说清为什么（不是一句「重置失败」）。"""
        monkeypatch.setattr("utils.request_security._has_admin_access", lambda: True)
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _run(project_id, status="running")

        resp = client.post(RESET_URL, json={"confirm": RESET_CONFIRM_WORD})

        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "没有结束" in body["message"], body
        assert "deleted" not in body
        with flask_app.app_context():
            assert AiAnalysisRun.query.count() == 1


class TestWhoCanChangeTheBaseline:
    def test_the_status_line_goes_to_everyone_but_the_buttons_do_not(self, monkeypatch):
        """数字是怎么来的必须看得见；能不能改是另一件事。

        两件事分开写，才不会出现「界面上有个点不动的按钮」或者「谁都能改全平台口径」。
        """
        with flask_app.app_context():
            create_tables()
            _clear_statistics()
            set_usage_baseline(datetime(2026, 9, 1), updated_by="tester")

        plain = _overview_as(False, monkeypatch)["statistics"]
        admin = _overview_as(True, monkeypatch)["statistics"]

        # 状态行两份都有（且是同一份口径）。
        assert plain["counted_since_local"] == admin["counted_since_local"] == "2026-09-01T08:00:00"
        assert plain["can_manage"] is False
        assert admin["can_manage"] is True

    def test_the_in_flight_count_is_shipped_so_the_button_can_be_blocked(self, monkeypatch):
        """界面要在点之前就知道「现在不能重置」，而不是点完读一句错误。

        这也是 `statistics` 里带 `in_flight_runs` 的原因 —— 服务端仍然会拒（那才是边界），
        这里只是让按钮提前变成可解释的状态。
        """
        with flask_app.app_context():
            create_tables()
            _clear_runs()
            project_id = _project()
            _run(project_id, status="running")

        stats = _overview_as(True, monkeypatch)["statistics"]

        assert stats["in_flight_runs"] == 1


# ==========================================================================
# 五、漂移守卫
# ==========================================================================


def test_the_statistics_table_columns_are_the_ones_the_service_reads():
    """口径表的列在「模型」与「测试元组」两处一致。

    不接进加列迁移（`db_migration_service`）：这张表是**新建**的，`create_all()` 直接建；
    迁移服务只服务「给已有表加列」（见该模块的说明）。所以要守的是「服务读的列都真的存在」——
    列名写错的表现是面板 500，而 `create_all` 看起来一切正常。
    """
    columns = set(AiUsageStatistics.__table__.columns.keys())

    assert {"counted_since", "reset_at", "reset_by", "updated_at", "updated_by"} <= columns, (
        f"口径表缺列：{'counted_since', 'reset_at', 'reset_by', 'updated_at', 'updated_by'} - columns"
    )
    assert AiUsageStatistics.__tablename__ == "ai_usage_statistics"


def test_the_payload_ships_the_confirm_word_the_route_checks():
    """确认词的**事实源是服务端**（路由校验的就是它），界面拿到的必须是同一个字。

    两边各写一份的话，改词的那天界面会一直提交一个被拒的值，而看上去像是后端坏了。
    """
    payload = statistics_public(can_manage=True)

    assert payload["confirm_word"] == RESET_CONFIRM_WORD


def test_the_reset_scope_number_is_the_whole_table_not_the_current_screen():
    """弹层里的「会删掉多少条」必须是**整张表**（`total_runs`）。

    重置删的是整张 `ai_analysis_run`，与当前筛选、统计起点都无关。弹层里写
    「这一页看到 24 次，它们都会被删掉」而实际删掉 300 条，是破坏性动作最不能有的
    那种不一致 —— 用户正是照着那个数字决定要不要按下去的。
    """
    with flask_app.app_context():
        create_tables()
        _clear_runs()
        _clear_statistics()
        first = _project()
        second = _project()
        _run(first)
        _run(second)
        _run(second)

        payload = statistics_public(can_manage=True)
        assert payload["total_runs"] == 3
        # 非管理员那一份不带这个数：它是**全平台**的总条数，与这一屏的权限范围无关，
        # 而普通用户也没有那个按钮可点。
        assert "total_runs" not in statistics_public(can_manage=False)


def test_the_in_flight_count_is_gated_by_the_same_permission():
    """**「全平台正在跑几条」与 `total_runs` 是同一类数字，不能只扣一个。**

    `in_flight_runs()` 不按项目过滤（`AiAnalysisRun.query.filter(status.in_(…))`），
    而 `/ai-analysis/usage/overview` 是**登录即可访问**的（不是 /admin/ 下的路径，
    见路由注释）—— 所以任何登录用户都能读到「全平台正在跑几条」。`total_runs` 正是
    为这一点被扣住的，两个数属于同一类。

    界面也从不在非管理员那里读它（`renderStatistics`：`blocked = canManage &&
    inFlight > 0`），发出去是**发而不用**的数据。
    """
    with flask_app.app_context():
        create_tables()
        _clear_runs()
        _clear_statistics()
        _run(_project())

        assert statistics_public(can_manage=True)["in_flight_runs"] >= 0
        assert "in_flight_runs" not in statistics_public(can_manage=False), (
            "全平台在跑的条数发给了非管理员 —— 它与 total_runs 是同一类口径"
        )


# ==========================================================================
# 六、界面（静态断言）
# ==========================================================================
# 静态断言只看得见字符串，所以这里守的都是「写了没有 / 有没有写反」，不守观感。
# 观感那部分靠 `scripts/shot_ai_usage_page.py`（无头 Chrome 真渲染 + 逐元素量值）。
# 剥注释的 helper 与 `tests/test_ai_usage_budget_ui.py` 共用 —— 模板的注释里原样写着
# 被禁掉的写法（「不是 0」「不删记录」这类反例本身就是注释内容），不剥就会假红/假绿。

from tests.test_ai_usage_budget_ui import _dashboard_script, _function_body  # noqa: E402

DASHBOARD = "templates/ai_usage_dashboard.html"


class TestTheStatisticsCard:
    def _html(self) -> str:
        return (Path(__file__).resolve().parents[1] / DASHBOARD).read_text(encoding="utf-8")

    def test_the_card_sits_between_the_kpis_and_the_project_table(self):
        """数字紧跟着它的来路：KPI 在上，口径卡片紧随其后。

        位置本身是口径的一部分 —— 一张放在页脚的「统计起点」说明，读的人会先看到
        变小了的数字再去到处找原因。
        """
        html = self._html()

        assert 'id="aiuStatsCard"' in html
        assert html.index('id="aiuKpis"') < html.index('id="aiuStatsCard"') < html.index(
            'id="aiuProjectRows"'
        ), "统计口径卡片跑到 KPI 之前或项目表之后去了"

    def test_everyone_sees_the_status_line_and_only_admins_see_the_buttons(self):
        """数字是怎么来的必须看得见；能不能改是另一件事（服务端仍会拒，那才是边界）。"""
        body = _function_body(_dashboard_script(), "renderStatistics")

        assert "canManage" in body
        assert "readonly.hidden = canManage" in body, "普通用户看不到口径说明"
        assert "actions.hidden = !canManage" in body, "普通用户看到了改动入口"

    def test_the_reset_button_is_blocked_before_it_is_clicked(self):
        """在途运行 → 按钮提前变成不可点，并且**说明为什么**。

        点完再读一句错误的话，用户已经以为删成功了。
        """
        body = _function_body(_dashboard_script(), "renderStatistics")

        assert "resetBtn.disabled = blocked" in body
        assert "note.hidden = !blocked" in body, "挡住了按钮却不说原因"
        assert "没有结束" in body

    def test_the_baseline_modal_prefills_now_in_beijing(self):
        """预填的是**北京墙钟**的现在。

        浏览器的时区可能不是北京，而这个框里的值会被服务端当北京墙钟解释 ——
        用 `new Date().toISOString()` 直接截，在非北京时区的机器上会偏 8 小时。

        **`getTimezoneOffset()` 在这里是个陷阱**：它是「UTC − 本地」，加它等于把时差
        再减一遍。在北京的机器上（也就是用户）它会退回 UTC 数字，预填与 `max` 都差 8 小时，
        而界面上完全看不出来。无头 Chrome 复核时量到的 `max` 正是那个错的形状。
        """
        body = _function_body(_dashboard_script(), "statsNowLocal")

        assert "getTimezoneOffset" not in body, "拿本地墙上时间算北京墙钟，会差一个时差"
        assert "8 * 3600000" in body
        assert "Date.now()" in body, "取的是时刻本身（与时区无关），不是墙上时间"

    def test_both_writes_ask_for_json_and_treat_a_denial_as_a_permission_note(self):
        """不带 `Accept: application/json` 时，非管理员拿到的是 302 跳登录页，
        `fetch` 跟过去之后是一份 HTML ——「没权限」会以「JSON 解析失败」的形式出现。"""
        script = _dashboard_script()
        body = _function_body(script, "postStatistics")

        assert "'Accept': 'application/json'" in body
        assert "method: 'POST'" in body
        for name in ("saveStatsBaseline", "clearStatsBaseline", "submitStatsReset"):
            caller = _function_body(script, name)
            assert "result.status === 401 || result.status === 403" in caller, (
                f"{name} 没把 403 当成权限说明 —— 它会显示成「保存失败」让人一直重试"
            )
            assert "statsLostPermission()" in caller

    def test_the_two_actions_go_to_the_two_documented_endpoints(self):
        script = _dashboard_script()

        assert "'/ai-analysis/statistics/baseline'" in script
        assert "'/ai-analysis/statistics/reset'" in script
        # 「恢复为全部历史」提交的是显式的 null，不是空串（空串在服务端等于「没传」，
        # 而路由要求 `since` 这个键必须出现）。这里钉的是那一行**长什么样**。
        assert "since: null" in _function_body(script, "clearStatsBaseline")
        # 起点提交的是 `datetime-local` 的值本身（北京墙钟），换算在服务端做一次。
        assert "since: value" in _function_body(script, "saveStatsBaseline")

    def test_the_reset_needs_the_typed_word_before_the_button_unlocks(self):
        """确认词不是安全边界（权限才是），它防的是误触：点错了没有撤销。

        前端这一层只是省一次往返；真正把关的是路由（确认词不对直接 400）。
        """
        script = _dashboard_script()
        sync = _function_body(script, "syncResetConfirm")

        assert "input.value.trim() === resetConfirmWord()" in sync
        assert "btn.disabled = !matched" in sync
        # 确认词从服务端下发的那份来（`statistics.confirm_word`），只在没拿到响应时回落。
        assert "confirm_word" in script
        assert "'重置'" in script

    def test_the_reset_modal_lists_the_consequences_including_the_budget_one(self):
        """后果要如实写在**点之前**。

        最容易被漏掉的一条是「预算的已用也会归零」：它是连带后果（记录被删了），
        不写的话，用户会在下一次分析被放行时才自己发现额度也回来了。
        """
        html = self._html()
        marker = html.index('id="aiuResetModal"')
        # 重置弹层是最后一个弹层，后面紧跟的就是样式块 —— 比按 `</div>` 数嵌套稳。
        modal = html[marker: html.index("<style>", marker)]

        assert "不可撤销" in modal
        assert "预算的「已用」也会归零" in modal
        assert "AI 报告正文" in modal
        assert "全量分析" in modal, "没写「下一次分析会退化成全量分析」"
        assert 'id="aiuStatsConfirmInput"' in modal

    def test_the_reset_modal_counts_the_whole_table_not_the_current_screen(self):
        """「会删掉多少条」要对得上**真的会删掉多少**。

        拿 `totals.runs` 填那一格是最容易犯的错：它带着当前筛选，也可能被统计起点挡住 ——
        界面上写着「这一页看到 3 次」，而按下去删掉的是 300 条。
        """
        body = _function_body(_dashboard_script(), "openResetModal")

        assert "var total = statsState.total_runs;" in body, (
            "「会删掉多少条」不是从 `total_runs`（整张表）来的"
        )
        count_line = next(
            line for line in body.splitlines() if "countCell.textContent" in line
        )
        assert "total" in count_line, "那一格填的不是整张表的条数"
        # 当前这一屏的合计只允许出现在那句补充说明里，**一次**：它多出现一次的地方，
        # 就是「会删掉多少条」被算错的地方。
        assert body.count(".overview.totals.runs") == 1
        assert "aiuResetScopeNote" in body

    def test_the_two_endpoints_do_not_share_a_button(self):
        """「设置起点」与「全量重置」必须是两个按钮，而且破坏性的那个是红色的。

        合成一个入口（先设起点，点两次才删库）会把「改口径」和「删历史」变成同一个
        肌肉记忆 —— 而这个页面上只有后者不可撤销。
        """
        script = _dashboard_script()
        html = self._html()

        assert "'aiuStatsBaselineBtn'" in script and "'aiuStatsResetBtn'" in script
        assert 'id="aiuStatsBaselineBtn"' in html and 'id="aiuStatsResetBtn"' in html
        reset_line = next(
            line for line in html.splitlines() if 'id="aiuStatsResetBtn"' in line
        )
        assert "aiu-btn danger" in reset_line, "破坏性动作没有用 danger 样式"

    def test_the_page_no_longer_claims_it_has_no_destructive_action(self):
        """**两条自相矛盾的旧文字必须一起改掉。**

        改前页面上写着「这个页面上没有破坏性操作」与「这个页面上不该有删除按钮」——
        加了「全量重置」之后这两句就成了假话，而且是最坏的那种：用户在别处读到
        「这一页不会删数据」，于是放心地点了那个红色的按钮。
        """
        html = self._html()

        assert "这个页面上没有破坏性操作" not in html
        assert "这个页面上不该有删除按钮" not in html
        # 筛选区那两个按钮仍要说清它们只动筛选条件。
        assert "只动筛选条件" in html
