# -*- coding: utf-8 -*-
"""AI 消耗面板：**「完成统计」与「活动任务」是两个口径**。

## 这个文件守的六条性质

1. **一条刚建出来的 `running` 行不许把整屏判成「不可计算」。** `_create_run` 建 run 时
   token 三列全是 NULL（分析还没跑完、上游还没报）。而聚合器的口径是**对的**：
   `cache_read` / `input` 任一缺失就不给命中率（`services/ai/usage.py`），任一条算不出
   费用就不给合计。错的是把这条「还没结账」的行混进了「已完成统计」—— 于是一条在跑的
   任务能让整屏的命中率、费用、合计 token 全部变成「未上报」，其中「费用估算」那张
   KPI 卡片**根本不渲染**。
2. **在途运行不进完成统计，但也不许被藏掉。** 它要如实露出来：`active_runs` 给出条数、
   编号与（有的话）**临时**用量，运行列表里那一条照旧诚实显示「未上报」。
3. **完成统计与「没有这条活动任务」逐字相同。** 这不是「差不多」—— 多算或少算一次运行
   的金额看起来都完全正常，只有拿两个只差一条在途运行的项目对拍才看得出来。
4. **任务结束后自动并回统计**（成功 / 失败 / 降级三种收尾都算「已完成」）。
5. **筛选范围变了，两边一起变。** 只看「进行中」时完成统计是空的、活动任务是满的；
   时间窗把那条运行排除掉之后，活动任务也要跟着消失。
6. **预算那一档不跟着展示分组走。** 分组只影响这一页怎么摆数字；预算「已用」仍按
   `services/ai/analysis_budget.py` 自己的口径（那是拦截判定的来源，少算一分钱就是
   「面板说没超、按钮却点不动」）。

## 一条**刻意**保留的行为（不要顺手「修」掉）

一次**已经结束**、但确实没上报用量的运行（失败在半路）**照样**会让合计费用变成
「算不出」。这是聚合器刻意的口径：金额可以说明「我做了假设」，而一个漏掉几次运行的
合计看起来完全正常 —— 那比「算不出」危险得多。本文件把它钉住
（`test_a_finished_but_unreported_run_still_makes_the_cost_uncomputable`），
免得下一个人把它当成同一个 bug 一起放宽。

**测试库是会话级共用的**（没有逐用例重置），所以：统计起点是全局单行表 → 每个用例前后
各清一遍（`_isolate_statistics`）；断言只针对本用例自己新建的项目号。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiUsageStatistics
from services.ai import run_progress
from services.ai.analysis_budget import budget_status
from services.ai.pricing import estimate_cost, parse_price_table
from services.ai.run_progress import ProgressSnapshot
from services.ai_analysis_service import update_project_analysis_config
from services.ai_usage_service import (
    MAX_ACTIVE_RUNS,
    UsageFilters,
    parse_usage_filters,
    project_usage,
    usage_overview,
)

PRICE_TABLE = json.dumps(
    {
        "version": "dual-1",
        "currency": "CNY",
        "models": {"fake-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2}},
    }
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now() -> datetime:
    """当前时刻的 **naive UTC**（与 `created_at` 落库后的形状逐字一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _project() -> int:
    project = Project(code=_uid("P"), name=_uid("dual"))
    db.session.add(project)
    db.session.flush()
    return project.id


def _price_the_project(project_id: int) -> None:
    ok, message, errors = update_project_analysis_config(
        project_id, {"model_price_table": PRICE_TABLE}, updated_by="tester"
    )
    assert ok, (message, errors)


def _run(
    project_id: int,
    *,
    status: str = "succeeded",
    tokens_input: int | None = 1000,
    tokens_output: int | None = 200,
    cache_read: int | None = 400,
    created_at: datetime | None = None,
) -> AiAnalysisRun:
    """一条运行记录。

    `tokens_input=None` 就是库里那条「running 且三列全 NULL」的形状 —— 与
    `ai_analysis_service._create_run` 建出来的那条**逐字一致**（它不在建行时写 token）。
    """
    moment = created_at or _now()
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        # target_id 用 project_id：各用例的项目号互不相同，不会被别的用例留下的行撞上。
        target_id=project_id,
        target_key="group-a",
        status=status,
        scope="full",
        trigger_source="manual",
        response_text="结论",
        model="fake-model",
        created_at=moment,
        started_at=moment,
        # 在途运行**没有** finished_at —— 这正是它与已完成运行的差别之一。
        finished_at=None if status in ("pending", "running") else moment,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cache_read_tokens=cache_read,
        pricing_version="dual-1",
    )
    db.session.add(run)
    db.session.flush()
    db.session.commit()
    return run


def _clear_statistics() -> None:
    for row in AiUsageStatistics.query.all():
        db.session.delete(row)
    db.session.commit()


@pytest.fixture(autouse=True)
def _isolate_statistics():
    """统计起点是全局单行表：留一个起点下来，本文件所有合计都会无端变小。"""
    with flask_app.app_context():
        create_tables()
        _clear_statistics()
        run_progress.reset_for_tests()
        yield
        _clear_statistics()
        run_progress.reset_for_tests()


def _overview(project_id: int, **kwargs) -> dict:
    return usage_overview(
        [project_id], UsageFilters(project_id=project_id, **kwargs)
    )


def _entry(overview: dict) -> dict:
    rows = [item for item in overview["projects"] if item["project_id"]]
    assert len(rows) == 1, overview["projects"]
    return rows[0]


def _three_completed(project_id: int) -> None:
    """三条已完成的运行（token 与命中缓存都上报了）。"""
    for _ in range(3):
        _run(project_id)


# ==========================================================================
# 一、一条活动任务不许把整屏判成「不可计算」
# ==========================================================================


class TestAnActiveRunDoesNotBlankTheScreen:
    def test_the_completed_totals_are_identical_with_and_without_an_active_run(self):
        """只差一条在途运行的两个项目，完成统计必须**逐字相同**。

        这条断言是整件事的核心：`A` 比 `B` 只多一条 running 行，在修好之前
        `A` 的命中率与费用是「算不出」、`B` 的是正常数字 —— 而两个数字看上去
        都完全正常，只有对拍才看得出来。
        """
        with flask_app.app_context():
            create_tables()
            with_active = _project()
            without_active = _project()
            for project_id in (with_active, without_active):
                _price_the_project(project_id)
                _three_completed(project_id)
            running = _run(
                with_active, status="running", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            lively = _overview(with_active)
            quiet = _overview(without_active)

            # 完成统计逐字相同（`totals` 是 `completed_totals` 的历史字段名）。
            # `latest_run_id` 除外：那是**运行编号本身**，两个项目的编号当然不同。
            def _numbers(body):
                return {
                    key: value
                    for key, value in body["totals"].items()
                    if key != "latest_run_id"
                }

            assert _numbers(lively) == _numbers(quiet)
            assert lively["totals"] == lively["completed_totals"]
            assert lively["totals"]["runs"] == 3
            assert lively["totals"]["tokens"]["input"] == 3000
            assert lively["totals"]["tokens"]["total"] == 3600
            assert lively["totals"]["cache"]["hit_rate"] is not None, lively["totals"]
            assert lively["totals"]["cache"]["hit_rate"] == quiet["totals"]["cache"]["hit_rate"]
            assert lively["totals"]["cost"] is not None, "一条在途运行把整屏的费用判成了「算不出」"
            assert lively["totals"]["cost"]["amount"] == quiet["totals"]["cost"]["amount"]

            # 项目行那一格也一样。
            assert _entry(lively)["cost"]["amount"] == _entry(quiet)["cost"]["amount"]
            assert _entry(lively)["runs"] == 3
            assert _entry(lively)["cache"]["hit_rate"] is not None

            # 活动任务如实露出来（编号可用于核对）。
            assert lively["active_runs"]["count"] == 1
            assert lively["active_runs"]["latest_run_id"] == running.id
            assert _entry(lively)["running_runs"] == 1
            assert quiet["active_runs"]["count"] == 0
            assert _entry(quiet)["running_runs"] == 0

    def test_the_running_row_itself_is_still_listed_honestly(self):
        """在途那条在**运行列表里照旧出现**，而且照旧是「未上报」—— 不藏、不补 0。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _three_completed(project_id)
            running = _run(
                project_id, status="running", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            drill = project_usage(project_id, UsageFilters())

            row = [item for item in drill["runs"] if item["run_id"] == running.id]
            assert len(row) == 1, "在途的运行从列表里消失了"
            assert row[0]["status"] == "running"
            assert row[0]["usage"]["collected"] is False
            # `None` 原样交出去（界面显示「未上报」），**不是 0**。
            assert row[0]["usage"]["tokens"]["input"] is None
            assert row[0]["usage"]["tokens"]["cache_read"] is None
            assert row[0]["usage"]["cache"]["hit_rate"] is None
            # 而合计里没有它。
            assert drill["totals"]["runs"] == 3, drill["totals"]
            assert drill["active_runs"]["count"] == 1


# ==========================================================================
# 二、只有一条活动任务（新项目第一次分析还在跑）
# ==========================================================================


class TestOnlyAnActiveRun:
    def test_a_project_whose_first_run_is_still_going(self):
        """一条已完成都没有：合计是空的（不是「算不出」也不是 0），项目行仍然在。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            running = _run(
                project_id, status="running", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            overview = _overview(project_id)

            assert overview["totals"]["runs"] == 0
            assert overview["totals"]["tokens"]["input"] is None
            assert overview["totals"]["cache"]["hit_rate"] is None
            assert overview["totals"]["cost"] is None, "一次都没跑完却给出了费用"
            # 项目行还在 —— 这一屏确实有它的事，不能因为「还没结账」就整行消失。
            entry = _entry(overview)
            assert entry["runs"] == 0
            assert entry["running_runs"] == 1
            assert overview["active_runs"]["count"] == 1
            assert overview["active_runs"]["latest_run_id"] == running.id


# ==========================================================================
# 三、在途那条已经有部分 token
# ==========================================================================


class TestPartialTokensOnTheActiveRow:
    def test_a_running_row_with_partial_tokens_is_still_not_counted(self):
        """跑到一半的行可能已经有 token（子代理先收尾、或上游按轮上报）。

        它**仍然**不进完成统计：那是个半截数字，混进去等于把「已经花了多少」说成一个
        随刷新变化的数。它只出现在 `active_runs` 的临时值里。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _three_completed(project_id)
            _run(
                project_id, status="running", tokens_input=5000,
                tokens_output=800, cache_read=100,
            )

            overview = _overview(project_id)

            assert overview["totals"]["runs"] == 3
            assert overview["totals"]["tokens"]["input"] == 3000, "半截运行的 token 混进了合计"
            assert overview["totals"]["tokens"]["total"] == 3600
            assert overview["active_runs"]["count"] == 1
            # 没有进度快照 → 临时值只能是「读不到」，**不是 0**。
            assert overview["active_runs"]["live"]["tokens"] is None
            assert overview["active_runs"]["live"]["partial"] is True


# ==========================================================================
# 四、任务结束了（成功 / 失败 / 降级）就并回统计
# ==========================================================================


class TestWhenTheTaskEnds:
    def test_a_succeeded_run_joins_the_completed_totals(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            run = _run(
                project_id, status="running", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            before = _overview(project_id)
            assert before["totals"]["runs"] == 0
            assert before["active_runs"]["count"] == 1

            # 跑完了：`_persist_outcome` 就是这几列一起写下去的。
            run.status = "succeeded"
            run.finished_at = _now()
            run.tokens_input = 1000
            run.tokens_output = 200
            run.cache_read_tokens = 400
            db.session.commit()

            after = _overview(project_id)

            assert after["totals"]["runs"] == 1
            assert after["totals"]["tokens"]["input"] == 1000
            assert after["totals"]["cache"]["hit_rate"] == 0.4
            assert after["totals"]["cost"] is not None
            assert after["active_runs"]["count"] == 0, "跑完的任务还挂在「活动任务」里"

    def test_a_failed_run_is_a_finished_run(self):
        """失败也是「结账了」：它进完成统计（然后按**既有口径**让费用变成算不出）。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            failed = _run(
                project_id, status="running", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            failed.status = "failed"
            failed.finished_at = _now()
            failed.error_message = "上游超时"
            db.session.commit()

            overview = _overview(project_id)

            assert overview["active_runs"]["count"] == 0
            assert overview["totals"]["runs"] == 1, "失败的运行没进完成统计"
            # 它没上报任何用量 → 按聚合器**刻意**的口径，合计费用是「算不出」。
            assert overview["totals"]["cost"] is None

    @pytest.mark.parametrize("final_status", ["succeeded", "degraded"])
    def test_a_degraded_run_is_counted_by_its_recorded_usage(self, final_status):
        """降级（有报告但没走完，token 少）是**已结束**的运行 —— 按它落库的用量算。

        判据是「这条运行结束了」，不是「它跑得漂不漂亮」。两种落库形态都要认：
        `succeeded`（老行为，降级与跑完都写这个值）与 `degraded`
        （`models/ai_analysis/analysis_run.py` 里补上的第三态）。

        **它绝不能被算成「活动任务」**：那样降级交付的那几次会从这一页的所有数字里
        消失，而页面上还会挂一句「现在有 N 个任务运行中」—— 那几次早就跑完了。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            degraded = _run(
                project_id, status="running", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            degraded.status = final_status
            degraded.finished_at = _now()
            degraded.tokens_input = 300
            degraded.tokens_output = 20
            db.session.commit()

            overview = _overview(project_id)

            assert overview["totals"]["runs"] == 1
            assert overview["totals"]["tokens"]["total"] == 320
            assert overview["active_runs"]["count"] == 0
            assert _entry(overview)["running_runs"] == 0

    def test_a_finished_but_unreported_run_still_makes_the_cost_uncomputable(self):
        """**刻意保留，但语义收窄了**（2026-09-21，AI-P1-02）：旧口径只管「全体合计」那一栏。

        已经结束、却一次都没上报用量的运行，**照样**让 `totals.cost` / `totals.tokens.total` /
        `totals.cache.hit_rate` 变成「算不出」。这三条断言一个字没改，理由也没变：把一条算不出
        的运行从合计里**跳过**，得到的金额看起来完全正常、实际漏掉了几次真花掉的钱 —— 那是
        这个模块最想避免的错。

        ## 那为什么现在可以放宽「其余 15 次一起显示」这件事

        因为放宽的不是**同一个**数字，而是**另外一组**数字：`totals.reported_samples.*`
        明确叫「已上报样本」，带 `reported_runs` / `unknown_runs` 覆盖度，并在
        `known_value.notes` 里写明「另有 N 次没有上报，这里是已知的最低值」。它与
        `totals.cost` **并列存在、互不覆盖**，所以：

        * 「拿未知当 0」= 把没上报的那几次按 0 元计入合计，得到一个冒充总额的数字 ——
          **仍然禁止**（`totals.cost` 保持 None 就是这条）；
        * 「已知最低费用 + 覆盖度」= 一个说得出自己缺了哪几次的**下界** —— 允许，因为
          它不会冒充总额，而且用户终于能看见另外那些有效数据了。

        这条断言是**防止**下一个人把上面两件事混成一件、顺手把 `totals.cost` 也放宽。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _run(project_id)  # 一条正常的已完成运行
            _run(
                project_id, status="failed", tokens_input=None,
                tokens_output=None, cache_read=None,
            )

            overview = _overview(project_id)

            assert overview["totals"]["runs"] == 2
            assert overview["active_runs"]["count"] == 0
            assert overview["totals"]["cost"] is None, "算不出的那一条被悄悄跳过了"
            # 但命中率**不**因此变成 0：缺一条就返回 None（未上报），不是 0%。
            assert overview["totals"]["cache"]["hit_rate"] is None
            # **并列**的那一组：已上报样本的合计 + 覆盖度。它不冒充总额 —— 少算的那一条
            # 在 `unknown_runs` 里明明白白写着，金额也标着「已知最低费用」。
            sample = overview["totals"]["reported_samples"]
            assert sample["tokens"]["known_value"] == 1200
            assert sample["tokens"]["unknown_runs"] == 1
            assert sample["cache"]["known_value"] == pytest.approx(0.4)
            assert (
                Decimal(sample["cost"]["known_value"]["amount_exact"]) == _run_cost()
            ), "已上报样本的费用必须仍然算得出 —— 否则这条就退化成「一起放宽」了"


# ==========================================================================
# 五、多个并行活动任务
# ==========================================================================


class TestSeveralParallelActiveRuns:
    def test_the_count_is_the_number_of_unfinished_runs_in_scope(self):
        with flask_app.app_context():
            create_tables()
            first = _project()
            second = _project()
            for project_id in (first, second):
                _price_the_project(project_id)
                _three_completed(project_id)
            one = _run(first, status="running", tokens_input=None,
                       tokens_output=None, cache_read=None)
            two = _run(first, status="pending", tokens_input=None,
                       tokens_output=None, cache_read=None)
            three = _run(second, status="running", tokens_input=None,
                         tokens_output=None, cache_read=None)

            overview = usage_overview([first, second], UsageFilters())

            assert overview["active_runs"]["count"] == 3
            assert overview["active_runs"]["latest_run_id"] == three.id
            assert {item["run_id"] for item in overview["active_runs"]["runs"]} == {
                one.id, two.id, three.id
            }
            by_project = {item["project_id"]: item for item in overview["projects"]}
            assert by_project[first]["running_runs"] == 2
            assert by_project[second]["running_runs"] == 1
            # 两个项目的完成统计各自照旧。
            assert by_project[first]["runs"] == 3
            assert by_project[second]["runs"] == 3
            assert overview["totals"]["runs"] == 6
            assert overview["totals"]["cost"] is not None


# ==========================================================================
# 六、筛选范围变了，两边一起变
# ==========================================================================


class TestTheFilterScopeMovesBothSides:
    def test_filtering_to_running_empties_the_completed_side_only(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _three_completed(project_id)
            _run(project_id, status="running", tokens_input=None,
                 tokens_output=None, cache_read=None)

            body = usage_overview(
                [project_id],
                parse_usage_filters({"project": str(project_id), "status": "running"}),
            )

            assert body["totals"]["runs"] == 0, "只看进行中，完成统计里却有数字"
            assert body["totals"]["cost"] is None
            assert body["active_runs"]["count"] == 1
            assert _entry(body)["runs"] == 0
            assert _entry(body)["running_runs"] == 1

    def test_a_time_window_that_excludes_the_active_run_also_drops_the_note(self):
        """时间窗把那条在途运行排除掉之后，活动任务也要跟着消失 —— 提示不许残留。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            old = _now() - timedelta(days=10)
            _run(project_id, created_at=old)
            _run(project_id, status="running", tokens_input=None,
                 tokens_output=None, cache_read=None)

            everything = _overview(project_id)
            assert everything["active_runs"]["count"] == 1

            narrowed = usage_overview(
                [project_id],
                parse_usage_filters(
                    {
                        "project": str(project_id),
                        "range": "custom",
                        "from": old.date().isoformat(),
                        "to": old.date().isoformat(),
                    }
                ),
            )

            assert narrowed["active_runs"]["count"] == 0
            assert narrowed["totals"]["runs"] == 1
            assert narrowed["totals"]["tokens"]["input"] == 1000


# ==========================================================================
# 七、进度快照里的「临时值」：可以显示，不许混入
# ==========================================================================


class TestTheLiveTokensAreTemporary:
    def test_the_live_value_never_leaks_into_the_completed_totals(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _three_completed(project_id)
            running = _run(project_id, status="running", tokens_input=None,
                           tokens_output=None, cache_read=None)
            run_progress.publish(
                running.id,
                project_id,
                ProgressSnapshot(
                    run_id=running.id,
                    project_id=project_id,
                    index=2,
                    max_rounds=8,
                    status="running",
                    prompt_tokens=4000,
                    completion_tokens=500,
                    cache_read_tokens=2000,
                    cache_write_tokens=None,
                    requests_used=3,
                    requests_remaining=5,
                    items_chars=100,
                    elapsed_ms=1000,
                    updated_at=time.monotonic(),
                ),
            )

            overview = _overview(project_id)

            live = overview["active_runs"]["live"]
            assert live["tokens"] == 4500
            assert live["reported_runs"] == 1
            # **临时值永远标着「不完整」**，而且一个字都不许进完成统计。
            assert live["partial"] is True
            assert overview["totals"]["tokens"]["input"] == 3000
            assert overview["totals"]["tokens"]["total"] == 3600

    def test_a_run_that_reported_nothing_gets_none_not_zero(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _run(project_id, status="running", tokens_input=None,
                 tokens_output=None, cache_read=None)

            overview = _overview(project_id)

            assert overview["active_runs"]["live"]["tokens"] is None
            assert overview["active_runs"]["live"]["reported_runs"] == 0


# ==========================================================================
# 八、预算那一档不跟着展示分组走
# ==========================================================================


class TestTheBudgetDoesNotFollowTheDisplayGrouping:
    def test_the_budget_still_counts_every_run_in_its_own_window(self):
        """预算「已用」与闸门判定逐字一致 —— 展示分组不许反过来少算它的费用。

        少算一分钱的后果是「面板说没超、按钮却点不动」：两边的数字看上去都正常。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            # 配一个上限，闸门才会真的去数「这个周期里跑了多少」；没配上限时它按
            # 「不限制」返回，`used` 是空的，这条断言就什么也测不到了。
            ok, message, errors = update_project_analysis_config(
                project_id,
                {"model_price_table": PRICE_TABLE, "budget_token_limit": 1000000},
                updated_by="tester",
            )
            assert ok, (message, errors)
            _three_completed(project_id)
            _run(project_id, status="running", tokens_input=900,
                 tokens_output=100, cache_read=0)

            overview = _overview(project_id)
            gate = budget_status(project_id)

            assert overview["projects"][0]["budget"]["used"] == gate["used"]
            # 闸门看的是**它自己那个窗口里的全部运行**（含这条在途的），与展示口径无关。
            assert gate["used"]["runs"] == 4, gate["used"]
            assert overview["totals"]["runs"] == 3


# ==========================================================================
# 九、下钻与总览同一条口径
# ==========================================================================


class TestTheDrillDownUsesTheSameSplit:
    def test_the_weekly_rows_also_exclude_the_active_run(self):
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _three_completed(project_id)
            _run(project_id, status="running", tokens_input=None,
                 tokens_output=None, cache_read=None)

            drill = project_usage(project_id, UsageFilters())

            assert drill["totals"]["runs"] == 3
            assert drill["totals"]["cost"] is not None
            assert drill["completed_totals"] == drill["totals"]
            assert drill["active_runs"]["count"] == 1
            assert [item["runs"] for item in drill["weekly_versions"]] == [3]
            assert drill["weekly_versions"][0]["running_runs"] == 1

    def test_a_weekly_group_with_only_an_active_run_stays_visible(self):
        """那个周版本只跑过一条、还在跑 —— 行还在（`0 次运行 · 1 次运行中`），不是消失。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _run(project_id, status="running", tokens_input=None,
                 tokens_output=None, cache_read=None)

            drill = project_usage(project_id, UsageFilters())

            assert len(drill["weekly_versions"]) == 1, drill["weekly_versions"]
            row = drill["weekly_versions"][0]
            assert row["runs"] == 0
            assert row["running_runs"] == 1
            assert drill["totals"]["runs"] == 0
            assert drill["totals"]["cost"] is None


# ==========================================================================
# 十、活动任务那块的两个上限行为
# ==========================================================================


class TestTheActiveBlockShape:
    def test_the_list_is_truncated_but_the_count_is_not(self):
        """列表有上限，**计数没有** —— 页面上那句「当前有 N 个」的 N 是全量。

        截断计数的话，一个卡住的任务队列会在界面上显示成一个比实际小的数字，
        而那个数字正是用户用来判断「还要等多久」的。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)

            for _ in range(MAX_ACTIVE_RUNS + 3):
                _run(project_id, status="running", tokens_input=None,
                     tokens_output=None, cache_read=None)

            overview = _overview(project_id)
            block = overview["active_runs"]

            assert block["count"] == MAX_ACTIVE_RUNS + 3
            assert len(block["runs"]) == MAX_ACTIVE_RUNS
            assert block["truncated"] is True
            # 截的是**最早**那几条（列表按时间正序，保留最新的那些）。
            kept = {item["run_id"] for item in block["runs"]}
            assert block["latest_run_id"] in kept, "最新一条被截掉了，编号没法核对"
            assert overview["totals"]["runs"] == 0

    def test_no_active_run_gives_an_empty_block_not_a_missing_one(self):
        """没有在途任务时那一块**仍然在**（`count=0`）—— 界面的 `|| {}` 只是兜底，
        不是常规路径；少一个键会让「页面照着老响应渲染」变成常态。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _run(project_id)

            block = _overview(project_id)["active_runs"]

            assert block == {
                "count": 0,
                "latest_run_id": None,
                "truncated": False,
                "runs": [],
                "live": {"partial": True, "tokens": None, "reported_runs": 0, "unreported_runs": 0},
            }


# ==========================================================================
# 十一、已上报样本 + 覆盖度（AI-P1-02）
# ==========================================================================
# 「一个历史缺失值永久抹掉其余有效统计」是这次要修的症状：库里的 20 次已完成运行里
# 有 5 次（失败在半路）token 三列全 NULL，于是 `tokens.total` / `cache.hit_rate` /
# `cost` **三者同时**变成 None，页面整屏「未上报」—— 而另外 15 次的数据一直好好的。
#
# 修法不是放宽旧口径（那会让「漏算几次的合计」看起来完全正常），而是**并列**给出一组
# 「已上报样本」的数字与它的覆盖度。旧的三个字段一个字都不改：它们仍然是「全体齐全时
# 的精确值」，页面改成优先显示样本值并把覆盖率写在副标题里。


def _unreported_run(project_id: int) -> AiAnalysisRun:
    """一条**已经结束、却一次都没上报用量**的运行（失败在半路）。

    它不是活动任务（不会被回写），所以它进完成统计 —— 与 `_run(status="running")`
    那条「还没结账」的在途运行是**两回事**，两者进的是不同的口径。
    """
    return _run(
        project_id, status="failed", tokens_input=None, tokens_output=None, cache_read=None
    )


def _run_cost():
    """一条 `_run()` 默认参数下的准确费用（按 PRICE_TABLE 算）。

    在测试里算而不是写死 `"0.00288"`：写死的话，改 `_run()` 的默认 token 数会得到一个
    「测试红了但看不出为什么」的结果，而这里会跟着一起变。
    """
    return estimate_cost(
        "fake-model", tokens_input=1000, tokens_output=200, cache_read=400,
        table=parse_price_table(PRICE_TABLE)[0],
    ).amount


class TestReportedSamplesSurviveAMissingHistoryRow:
    def test_a_history_row_that_reported_nothing_does_not_erase_the_others(self):
        """20 次已完成运行里 5 次没上报 → 样本口径仍然给出 15 次的值与覆盖率。

        旧字段（`tokens.total` 等）**刻意保持 None**：那一份是「全体齐全时的精确值」，
        放宽它等于让一个漏掉 5 次运行的合计看起来完全正常。两者是并列关系。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            for _ in range(15):
                _run(project_id)
            for _ in range(5):
                _unreported_run(project_id)

            totals = _overview(project_id)["totals"]
            sample = totals["reported_samples"]

            assert totals["runs"] == 20
            # 旧口径一个字没改：任一条缺输入或输出，全体合计就不给值。
            assert totals["tokens"]["total"] is None
            assert totals["cache"]["hit_rate"] is None
            assert totals["cost"] is None
            # 新口径：已上报样本的合计 + 覆盖度。
            assert sample["total_runs"] == 20
            assert sample["tokens"]["reported_runs"] == 15
            assert sample["tokens"]["unknown_runs"] == 5
            assert sample["tokens"]["known_value"] == 15 * 1200
            assert sample["tokens"]["known_input"] == 15 * 1000
            assert sample["tokens"]["known_output"] == 15 * 200

    def test_the_known_cache_hit_rate_is_an_aggregate_over_the_sample_only(self):
        """已知命中率是**已上报样本的**命中合计 ÷ 输入合计，不是「按全部运行算」的假比例。

        分子分母必须来自**同一批**运行：只把 15 次的命中数除以 20 次的输入总数，
        会得到一个偏低的数字 —— 那正是「部分缺失」最容易算错、又最看不出来的地方。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            for _ in range(3):
                _run(project_id)
            _unreported_run(project_id)

            totals = _overview(project_id)["totals"]
            cache = totals["reported_samples"]["cache"]

            assert totals["cache"]["hit_rate"] is None
            assert cache["reported_runs"] == 3
            assert cache["unknown_runs"] == 1
            assert cache["known_value"] == pytest.approx(0.4)
            assert cache["known_input"] == 3000
            assert cache["known_cache_read"] == 1200

    def test_the_known_cost_sums_the_reported_samples_only(self):
        """已知费用 = 已上报样本的合计，**并明说这是已知的部分**（不是「拿未知当 0」）。

        差额那 5 次是真花了钱的（失败在半路也一样烧 token），所以这一格给的是
        「已知最低费用」：比总额小，但每一个数字都是算得出来的。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            for _ in range(3):
                _run(project_id)
            _unreported_run(project_id)

            totals = _overview(project_id)["totals"]
            cost = totals["reported_samples"]["cost"]

            assert totals["cost"] is None, "旧口径（任一条算不出就不给合计）被放宽了"
            assert cost["reported_runs"] == 3
            assert cost["unknown_runs"] == 1
            assert cost["known_value"] is not None
            assert Decimal(cost["known_value"]["amount_exact"]) == _run_cost() * 3
            # 这一句是给界面用的：为什么这里的金额比「一共花了多少」小。
            notes = cost["known_value"]["notes"] or []
            assert any("已知最低费用" in note for note in notes), notes
            assert any("1 次运行没有上报" in note for note in notes), notes

    def test_a_project_row_keeps_its_known_cost_and_rate(self):
        """项目行那一格也必须给出已知值 —— 否则用户点进来才发现「其实有数」。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            for _ in range(3):
                _run(project_id)
            _unreported_run(project_id)

            row = _entry(_overview(project_id))

            assert row["cost"] is None
            assert Decimal(row["reported_samples"]["cost"]["known_value"]["amount_exact"]) == (
                _run_cost() * 3
            )
            assert row["reported_samples"]["cache"]["known_value"] == pytest.approx(0.4)

    def test_everything_reported_means_the_two_views_agree(self):
        """全体都上报时，样本值必须与旧的精确值**逐字相同**（否则两套数字会互相矛盾）。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _three_completed(project_id)

            totals = _overview(project_id)["totals"]
            sample = totals["reported_samples"]

            assert sample["tokens"]["unknown_runs"] == 0
            assert sample["tokens"]["reported_runs"] == sample["tokens"]["total_runs"] == 3
            assert sample["tokens"]["known_value"] == totals["tokens"]["total"]
            assert sample["cache"]["known_value"] == pytest.approx(totals["cache"]["hit_rate"])
            assert (
                sample["cost"]["known_value"]["amount_exact"]
                == totals["cost"]["amount_exact"]
            )

    def test_no_completed_run_gives_a_complete_shape_not_a_missing_one(self):
        """一次都没完成时这一块**仍然在**（`known_value` 是 None）—— 界面不用写 `|| {}` 兜底。

        与旧的三个字段同一个理由：少一个键会让「页面照着老响应渲染」变成常态。
        """
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            _run(project_id, status="running", tokens_input=None,
                 tokens_output=None, cache_read=None)

            totals = _overview(project_id)["totals"]
            sample = totals["reported_samples"]

            assert totals["runs"] == 0
            assert sample["total_runs"] == 0
            for key in ("tokens", "cache", "cost"):
                assert sample[key]["reported_runs"] == 0, key
                assert sample[key]["unknown_runs"] == 0, key
                assert sample[key]["known_value"] is None, key

    def test_the_weekly_drill_row_carries_the_same_sample_block(self):
        """下钻的合计与周版本行读的是同一份聚合结果，形状不许分叉。"""
        with flask_app.app_context():
            create_tables()
            project_id = _project()
            _price_the_project(project_id)
            for _ in range(3):
                _run(project_id)
            _unreported_run(project_id)

            drill = project_usage(project_id, UsageFilters())
            week = drill["weekly_versions"][0]

            assert drill["totals"]["reported_samples"]["tokens"]["reported_runs"] == 3
            assert week["reported_samples"]["tokens"]["unknown_runs"] == 1
            assert week["reported_samples"]["tokens"]["known_value"] == 3 * 1200
