# -*- coding: utf-8 -*-
"""逐轮事件账本：**不丢账、不重复计费、重启后按事件恢复**（工作包 E 的验收）。

## 这一组守的是什么

原先只有 `ai_analysis_trace` 一本账，而它是**整次运行跑完才批量落库**的
（`services/ai_analysis_service._persist_outcome`），运行中能看的只有
`ai_analysis_job.progress_json` 里那个**最近 8 轮**的窗口。于是：

* **每个分片花了多少量不出来**（窗口是整条覆盖写的，早于窗口的轮次看不到）；
* **worker 中途被杀就整份账都没了** —— trace 一行没有，run 的 token 三列还是 NULL，
  用户重新打开抽屉只看到「这次运行没有留下逐轮记录」。

修法是把「一轮结束」做成一条**独立、幂等、立刻提交**的事件行
（`models/ai_analysis/round_event.py` + `services/ai/round_events.py`）。这一组按**行为**
（库里有什么、抽屉读到什么、模型被调了几次）验它，不看代码怎么写。

## 「杀 worker、重启」怎么模拟

需求里明确要求**在测试里用 fixture 模拟**，不许去动 8002 上真跑着的那个实例。这里的
做法是最贴近真实的一种：

1. **杀** —— 让引擎的 `on_round` 回调抛 `SystemExit`。引擎只 `except Exception`
   （`_emit` 的纪律：回调失败不作废分析），`BaseException` 会**真的穿出去** —— 这一次运行
   就此停在半路，`_persist_outcome` 永远不会被调用。这与「进程被 kill」在库里的形态
   **逐字相同**：事件行有、trace 一行没有、run 还是 `running`；
2. **重启** —— `run_progress.reset_for_tests()`（内存快照没了，等于换了进程）+
   把 run 判成 `failed`（那是平台自己的启动扫描
   `run_cache_source.fail_orphaned_analysis_runs` 干的事，这里显式做一遍，免得那条扫描
   顺手把**并行跑的别的用例**的 running 也判死 —— 测试库是会话级共用的）；
3. **重新打开抽屉** —— 读 `run_progress.snapshot(run_id)`（也就是
   `job_service.progress_payload` / SSE 那一帧读的东西）。

## 纪律

* 计数与查询一律按本次用例造的 `run_id` / `group_key` 过滤，**不数全表**
  （测试库是会话级共用的）；
* 断言落在**行为**上：库里的行、读出来的数、模型被调了几次；
* 「未上报」与「零」分开是这一组的硬要求（`None` 不许被兜成 0）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app import app as flask_app
from app import create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import (
    AiAnalysisJob,
    AiAnalysisRoundEvent,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiProjectAnalysisConfig,
    AiProjectApiKey,
    AiWeeklyAnalysisState,
)
from services.ai import round_events, run_progress
from services.ai.budget import ContextItem
from services.ai.engine import EngineLimits, RoundRecord, run_analysis
from services.ai.protocol import ContextRequest
from tests.test_ai_engine import (
    COMMIT,
    FakeProvider,
    ScriptedClient,
    _anomaly,
    _final,
    _loaded,
    _requests,
    _scope,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
#  fixture：造一次真实的运行行（事件表要挂在真实 run 上才会写，见 record 的说明）
# ---------------------------------------------------------------------------


class _Runner:
    """一次运行 + 它造出来的行（用例结束自己收干净）。"""

    def __init__(self, run_id: int, project_id: int, group: str):
        self.run_id = run_id
        self.project_id = project_id
        self.group = group


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with flask_app.app_context():
        create_tables()


@pytest.fixture(autouse=True)
def _app_context():
    """每个用例跑在一个应用上下文里（`db.session` 要它才活得起来）。"""
    with flask_app.app_context():
        yield


@pytest.fixture(autouse=True)
def _clean_state():
    run_progress.reset_for_tests()
    yield
    run_progress.reset_for_tests()


def _make_run(*, status: str = "running", with_job: bool = False) -> _Runner:
    project = Project(code=_uid("P"), name=_uid("proj"))
    db.session.add(project)
    db.session.flush()
    group = _uid("grp")
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="weekly",
        target_key=group,
        status=status,
        request_payload="{}",
    )
    db.session.add(run)
    db.session.flush()
    if with_job:
        # 生产里每次运行都挂在一条 job 下（`job_service.create_or_attach_job` → worker
        # → `_create_run`）。事件表的四列唯一键里有 job_id，`progress_payload_for_run`
        # 也要按 job 反查 run —— 所以需要它的用例显式建一条。
        db.session.add(
            AiAnalysisJob(
                project_id=project.id,
                target_type="weekly",
                target_key=group,
                state="running",
                run_id=run.id,
            )
        )
    db.session.commit()
    return _Runner(run.id, project.id, group)


def _drop(runner: _Runner) -> None:
    """收掉这个用例造的行（测试库是会话级共用的，不许留残影）。"""
    round_events.forget_run(runner.run_id)
    for row in AiAnalysisTrace.query.filter_by(run_id=runner.run_id).all():
        db.session.delete(row)
    for row in AiAnalysisJob.query.filter_by(run_id=runner.run_id).all():
        db.session.delete(row)
    run = db.session.get(AiAnalysisRun, runner.run_id)
    if run is not None:
        db.session.delete(run)
    db.session.commit()
    project = db.session.get(Project, runner.project_id)
    if project is not None:
        db.session.delete(project)
        db.session.commit()


def _event_rows(run_id: int) -> list:
    return (
        AiAnalysisRoundEvent.query.filter_by(run_id=run_id)
        .order_by(
            AiAnalysisRoundEvent.member_index.asc(),
            AiAnalysisRoundEvent.round.asc(),
        )
        .all()
    )


def _frame(
    index: int,
    *,
    member: str = "",
    member_index: int = 0,
    member_total: int = 0,
    status: str = "requests",
    tokens_in=None,
    tokens_out=None,
    cum_in=None,
    cum_out=None,
    cache_read=None,
    cache_write=None,
    counts=None,
    entry_extra=None,
) -> SimpleNamespace:
    """一帧进度，形状与 `services/ai/engine.py` 的 `RoundProgress` 逐键相同。

    `index == 0` 是引擎的 `on_start` 那一帧（它不是「一轮」）。

    ## 两个 token 口径必须分开写

    帧上的 `prompt_tokens` 是**这个成员已经累计的**量，而 `round_entry` 里的是**本轮值**
    （见 `round_events.event_fields` 的说明）。`cum_in` / `cum_out` 就是前者，不传时取
    与后者相同的值 —— 单轮成员两者本来就相等；要造多轮成员时必须显式传，否则这一帧
    自相矛盾（本轮 200、累计也只有 200），而**实时那份读累计、账本那份读逐轮**，
    两条路会算出两个数（那正是 `test_the_restored_ledger_numbers_match_...` 要抓的）。
    """
    entry = None
    if tokens_in is not None or tokens_out is not None or entry_extra:
        entry = {
            "round_index": index,
            "agent": member,
            "agent_round": index,
            "outcome": status,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "duration_ms": 1000,
            "context_chars": 500,
            "request_chars": 900,
            "requests": [],
            "executed": [],
            "dropped": [],
        }
        entry.update(entry_extra or {})
    return SimpleNamespace(
        index=index,
        max_rounds=8,
        status=status,
        prompt_tokens=tokens_in if cum_in is None else cum_in,
        completion_tokens=tokens_out if cum_out is None else cum_out,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        requests_used=0,
        requests_remaining=8,
        items_chars=500,
        elapsed_ms=1000,
        agent=member,
        agent_index=member_index,
        agent_total=member_total,
        round_entry=entry,
        round_counts=counts,
    )


# ==========================================================================
#  一、每轮一条、幂等、启动那一帧不记账
# ==========================================================================


def test_every_finished_round_lands_one_event_and_the_start_frame_does_not():
    """**这一条就是缺陷本身。** 运行中每跑完一轮，库里就多一条；`on_start` 不算一轮。"""
    runner = _make_run()
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(0, status="starting", member="S1", member_index=1, member_total=2),
        )
        assert _event_rows(runner.run_id) == [], (
            "on_start 那一帧被记成「第 0 轮」了 —— 「一共跑过几轮」从此永远是错的"
        )
        for index in (1, 2, 3):
            run_progress.publish(
                runner.run_id, runner.project_id,
                _frame(index, member="S1", member_index=1, member_total=2,
                       tokens_in=100 * index, tokens_out=10 * index, cache_read=50),
            )
        rows = _event_rows(runner.run_id)
        assert [row.round for row in rows] == [1, 2, 3]
        assert [row.tokens_input for row in rows] == [100, 200, 300]
        assert [row.tokens_output for row in rows] == [10, 20, 30]
        assert rows[0].member == "S1" and rows[0].member_index == 1
        assert rows[0].member_total == 2
        assert all(row.job_id is None for row in rows), "没有 job 的运行不该编一个 job_id"
    finally:
        _drop(runner)


def test_a_dirty_count_does_not_take_the_progress_snapshot_down_with_it():
    """脏值只让**那一个字段**缺失，不作废整帧（与 `_as_int` 是同一条纪律）。

    这里喂的是**这一层新读的那一份计数**（`round_counts`，引擎的钩子给的）里类型不对的
    值：`_optional_int` 不认就是 `None`（未上报），**不许抛**。抛出去的代价不是「这个数
    没了」，而是整帧快照被 `publish` 的宽 except 咽掉 —— 界面上「思考过程」卡住不动，
    而分析照跑（这条路径的两侧都是付费的模型调用）。
    """
    runner = _make_run()
    try:
        frame = _frame(
            1, member="S1", member_index=1, member_total=1, tokens_in=10, tokens_out=5,
            counts={"requests": "很多", "executed": None, "failed": "两条",
                    "truncated": "0", "refused": 0, "dropped": 1, "candidates": "一"},
        )
        run_progress.publish(runner.run_id, runner.project_id, frame)
        snap = run_progress.snapshot(runner.run_id)
        assert snap is not None, "一个脏计量把整帧进度作废了"
        assert snap.index == 1
        rows = _event_rows(runner.run_id)
        assert len(rows) == 1
        # 认不出来的那几个记 `None`（未上报），认得出的照记 —— 不是整条为 0。
        assert rows[0].tool_requests is None
        assert rows[0].tool_failed is None
        assert rows[0].tool_truncated == 0
        assert rows[0].tool_dropped == 1
        assert rows[0].candidates is None
    finally:
        _drop(runner)


def test_a_repeated_round_updates_the_same_row_instead_of_adding_one():
    """重试 / 晚到的帧重报同一轮：**只更新那一条**（唯一键 `run_id/member/round`）。

    **两种都测**：有 job 与没有 job 的行各一份。需求点名的那把键是
    `(job_id, run_id, member, round)`，而 SQLite 的唯一索引把 NULL 当互不相同 ——
    只留那一把的话，「没有 job 的运行」重报同一轮会**多出一行**（逐成员合计翻倍）。
    所以表上两把键都在（见 `models/ai_analysis/round_event.py`），这一条就是钉它的。
    """
    for with_job in (False, True):
        runner = _make_run(with_job=with_job)
        try:
            run_progress.publish(
                runner.run_id, runner.project_id,
                _frame(2, member="S1", member_index=1, member_total=1,
                       tokens_in=100, tokens_out=10, status="requests"),
            )
            run_progress.publish(
                runner.run_id, runner.project_id,
                _frame(2, member="S1", member_index=1, member_total=1,
                       tokens_in=180, tokens_out=40, status="final"),
            )
            rows = _event_rows(runner.run_id)
            assert len(rows) == 1, f"同一轮被记了两遍（with_job={with_job}）—— 合计会翻倍"
            assert rows[0].status == "final"
            assert rows[0].tokens_input == 180 and rows[0].tokens_output == 40
            assert (rows[0].job_id is not None) is with_job
        finally:
            _drop(runner)


def test_an_unreported_number_is_null_while_a_reported_zero_is_zero():
    """`None`（未上报）与 `0`（确实没有）在库里必须是两个值 —— 全库同一条口径。"""
    runner = _make_run()
    try:
        # 上游一次都没报（网关不回 usage）→ 四列全是 NULL
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, member="S1", member_index=1, member_total=1,
                   tokens_in=None, tokens_out=None, cache_read=None, cache_write=None),
        )
        # 上游报了 0（缓存一次都没命中）→ 是 0，不是 NULL
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(2, member="S1", member_index=1, member_total=1,
                   tokens_in=0, tokens_out=0, cache_read=0, cache_write=0),
        )
        first, second = _event_rows(runner.run_id)
        assert (first.tokens_input, first.cache_read_tokens) == (None, None)
        assert (second.tokens_input, second.cache_read_tokens) == (0, 0)
    finally:
        _drop(runner)


def test_the_tool_counts_come_from_the_untruncated_record_not_from_the_details():
    """**19 次索取不许数成 8 次**：计数从 `RoundRecord` 上取，明细有 8 条上限。

    这一条同时钉住两个实现形状：明细（`entry_json`）仍旧是截断的那一份，而计数列是
    真数。只钉一个的话，「用明细数数」这种写法会静默地把 19 记成 8。
    """
    items = tuple(
        ContextItem(kind="file_diff", label=f"config/表{index}.xlsx", text="内容" * 10)
        for index in range(12)
    )
    record = RoundRecord(
        1, "requests",
        request_count=19,
        item_count=12,
        refused_by_budget=3,
        truncated=2,
        requests=tuple(
            ContextRequest(type="file_diff", commit=COMMIT, path=f"config/表{i}.xlsx")
            for i in range(19)
        ),
        executed=items,
        candidates=None,
    )
    counts = round_events.round_counts(record)
    assert counts["requests"] == 19, "计数被明细的 8 条上限截短了"
    assert counts["executed"] == 12
    assert counts["refused"] == 3 and counts["truncated"] == 2
    assert counts["candidates"] is None, "不是交结论的那一轮，候选数该是「未上报」"

    runner = _make_run()
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, member="S1", member_index=1, member_total=1,
                   tokens_in=10, tokens_out=5, counts=counts),
        )
        row = _event_rows(runner.run_id)[0]
        assert row.tool_requests == 19 and row.tool_executed == 12
        assert row.tool_refused == 3 and row.tool_truncated == 2
        assert row.candidates is None
        entry = json.loads(row.entry_json)
        assert len(entry["requests"]) <= 8, "明细本身仍然是有上限的那一份"
    finally:
        _drop(runner)


# ==========================================================================
#  二、逐成员的账（分片 / 汇总 / 复核）
# ==========================================================================


def test_each_member_gets_its_own_line_with_tokens_tools_duration_and_candidates():
    """需求那一行：「每个成员、汇总、复核分别显示……」—— 四个角色各一条，数不串门。"""
    runner = _make_run()
    try:
        frames = [
            # 分片 S1：两轮，最后一轮交出 1 条候选
            _frame(1, member="S1", member_index=1, member_total=4,
                   tokens_in=100, tokens_out=10, cache_read=50,
                   counts={"requests": 3, "executed": 2, "failed": 1,
                           "truncated": 0, "refused": 0, "dropped": 0, "candidates": None}),
            _frame(2, member="S1", member_index=1, member_total=4, status="final",
                   tokens_in=200, tokens_out=40, cache_read=60,
                   counts={"requests": 2, "executed": 2, "failed": 0,
                           "truncated": 1, "refused": 1, "dropped": 0, "candidates": 1}),
            # 分片 S2：一轮，一条候选都没有（0，不是「未上报」）
            _frame(1, member="S2", member_index=2, member_total=4, status="final",
                   tokens_in=300, tokens_out=30,
                   counts={"requests": 1, "executed": 1, "failed": 0,
                           "truncated": 0, "refused": 0, "dropped": 0, "candidates": 0}),
            # 汇总（标签空、位次 3/4）
            _frame(1, member="", member_index=3, member_total=4, status="final",
                   tokens_in=400, tokens_out=50,
                   counts={"requests": 0, "executed": 0, "failed": 0,
                           "truncated": 0, "refused": 0, "dropped": 0, "candidates": 2}),
            # 对账轮 V1（位次 4/4）
            _frame(1, member="V1", member_index=4, member_total=4, status="final",
                   tokens_in=500, tokens_out=60,
                   counts={"requests": 4, "executed": 3, "failed": 2,
                           "truncated": 0, "refused": 0, "dropped": 0, "candidates": 0}),
        ]
        for frame in frames:
            run_progress.publish(runner.run_id, runner.project_id, frame)

        totals = round_events.member_totals(runner.run_id)
        assert [item["member"] for item in totals] == ["S1", "S2", "", "V1"]
        assert [item["role"] for item in totals] == [
            round_events.ROLE_SUBAGENT,
            round_events.ROLE_SUBAGENT,
            round_events.ROLE_SYNTHESIS,
            round_events.ROLE_VERIFY,
        ]
        s1 = totals[0]
        assert s1["rounds"] == 2
        assert s1["tokens_input"] == 300 and s1["tokens_output"] == 50
        assert s1["cache_read_tokens"] == 110
        assert s1["duration_ms"] == 2000
        assert s1["tool_requests"] == 5 and s1["tool_executed"] == 4
        assert s1["tool_failed"] == 1 and s1["tool_truncated"] == 1
        assert s1["tool_refused"] == 1
        assert s1["candidates"] == 1 and s1["candidates_reported"] is True
        assert s1["tokens_reported"] is True and s1["counts_reported"] is True
        # 一条候选都没有的那一片：0 是**结论**，不是「未上报」。
        assert totals[1]["candidates"] == 0
        assert totals[3]["candidates"] == 0
        assert totals[2]["candidates"] == 2
    finally:
        _drop(runner)


def test_a_single_member_run_is_the_main_analyst_not_a_synthesis_round():
    """单代理运行（一个成员）里，标签空的那一个叫**主分析者**，不许叫「汇总」。"""
    assert round_events.member_role("", 1, 1) == round_events.ROLE_MAIN
    assert round_events.member_role("", 3, 4) == round_events.ROLE_SYNTHESIS
    assert round_events.member_role("S1", 1, 4) == round_events.ROLE_SUBAGENT
    assert round_events.member_role("V1", 4, 4) == round_events.ROLE_VERIFY


def test_a_member_that_never_reported_says_unreported_instead_of_zero():
    """上游没报的成员：合计是 `None` + `tokens_reported=False`，**不是 0**。"""
    runner = _make_run()
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, member="S1", member_index=1, member_total=1,
                   counts={"requests": 2, "executed": 1, "failed": 0,
                           "truncated": 0, "refused": 0, "dropped": 0,
                           "candidates": None}),
        )
        total = round_events.member_totals(runner.run_id)[0]
        assert total["tokens_input"] is None and total["tokens_output"] is None
        assert total["tokens_reported"] is False
        assert total["tool_requests"] == 2, "计数报了就该显示计数"
        assert total["counts_reported"] is True
    finally:
        _drop(runner)


# ==========================================================================
#  三、中途杀 worker → 重启 → 重新打开抽屉
# ==========================================================================


class _KillAfter:
    """跑到第 n 轮时**让整个调用炸穿** —— 等价于进程被 kill（见模块 docstring）。"""

    def __init__(self, inner, kill_after: int):
        self._inner = inner
        self._kill_after = kill_after
        self.calls = 0

    def complete(self, messages, *, temperature=None):
        if self.calls >= self._kill_after:
            raise SystemExit("worker 在这一轮被杀了")
        self.calls += 1
        return self._inner.complete(messages, temperature=temperature)


def _drive_engine(runner: _Runner, client, *, max_rounds: int = 8):
    """用真引擎跑一轮，进度走**生产那条回调**（`_publish_progress` 的同一对入口）。

    直接调 `run_progress.publish` 与这里走的是同一条路（引擎的 `_emit` → `publish`），
    但只有真引擎会给出真的 `round_counts` / `round_entry` / 候选数 —— 所以这一条是
    「钩子接上了」的证据，不是「函数能调」。
    """
    from services.ai_analysis_service import _publish_progress

    def on_round(progress):
        _publish_progress(runner.run_id, runner.project_id, progress)

    return run_analysis(
        client=client,
        provider=FakeProvider(),
        loaded=_loaded(),
        scope=_scope(),
        change_summary="本次变更共 1 个提交、2 个文件。\n",
        limits=EngineLimits(max_rounds=max_rounds),
        on_round=on_round,
        on_start=on_round,
    )


def _simulate_restart(runner: _Runner) -> None:
    """重启：内存快照没了（换进程）+ 平台启动扫描把 running 判成 failed。"""
    run_progress.reset_for_tests()
    run = db.session.get(AiAnalysisRun, runner.run_id)
    run.status = "failed"
    run.finished_at = datetime.now(timezone.utc)
    run.error_message = "平台重启，本次分析被中断（未跑完，可以重新分析）。"
    db.session.commit()


def _interrupted_run(kill_after: int = 2, max_rounds: int = 8, with_job: bool = False):
    """造一次「跑到一半被杀」的运行：返回 `(runner, client)`。

    脚本：每轮都索要一次 `commit_detail`，被杀之前一轮都没交结论。
    """
    runner = _make_run(with_job=with_job)
    client = _KillAfter(
        ScriptedClient(*([_requests({"type": "commit_detail", "commit": COMMIT})] * 5)),
        kill_after,
    )
    with pytest.raises(SystemExit):
        _drive_engine(runner, client, max_rounds=max_rounds)
    return runner, client


def test_killing_the_worker_mid_run_keeps_the_completed_rounds_and_the_ledger():
    """**验收主条**：杀 worker → 重启 → 重新打开抽屉：轮次与 token 账不丢、不重复计费。"""
    runner, client = _interrupted_run(kill_after=3)
    try:
        # --- 被杀之后的库里形态：事件有、账没有（trace 一行都没有、run 还是 NULL）---
        assert len(_event_rows(runner.run_id)) == 3
        assert AiAnalysisTrace.query.filter_by(run_id=runner.run_id).count() == 0
        run = db.session.get(AiAnalysisRun, runner.run_id)
        assert run.tokens_input is None and run.tokens_output is None

        spent = client.calls  # 被杀之前真的调了几次模型
        assert spent == 3

        _simulate_restart(runner)

        # --- 重新打开抽屉：进度从**事件账本**补回来 ---
        snap = run_progress.snapshot(runner.run_id)
        assert snap is not None, "重启之后抽屉读不到进度 —— 这一条就是要防这个"
        assert snap.source == "ledger"
        assert snap.rounds_seen == 3
        assert len(snap.rounds) == 3, "已完成的 3 轮不许丢"
        assert snap.job_tokens == 3 * 15, "token 账丢了或被重复计了"
        assert snap.job_tokens_partial is False
        member = snap.members[0]
        assert member["rounds"] == 3
        assert member["tokens_input"] == 30 and member["tokens_output"] == 15

        # --- 不重复计费：恢复路径一次模型调用都没有 ---
        assert client.calls == spent, "恢复过程重跑了模型"

        # --- 账也补进了正常读取路径（用量页 / 覆盖账本 / /runs/<id>/usage 都读这两处）---
        rows = (
            AiAnalysisTrace.query.filter_by(run_id=runner.run_id)
            .order_by(AiAnalysisTrace.round_index.asc())
            .all()
        )
        assert [row.round_index for row in rows] == [1, 2, 3]
        assert [row.tokens_input for row in rows] == [10, 10, 10]
        run = db.session.get(AiAnalysisRun, runner.run_id)
        assert run.tokens_input == 30 and run.tokens_output == 15
        assert run.rounds_used == 3

        # --- 幂等：再读几次，账不会翻倍 ---
        for _ in range(3):
            again = run_progress.snapshot(runner.run_id)
            assert again is not None
            # 补账之后 run 上的 token 列已经有值了 → 这一份**不再当「尚未落库的用量」报**
            # （预算那一档已经把它算进 used 了，见 `_pending_usage`）。要验的不是那个数
            # 还在不在，而是**逐成员的账没有翻倍**：45 是「折了第二次」、90 是
            # 「事件又写了一遍」，两个都不是。
            assert again.job_tokens is None, "已落库的账被当成「尚未落库」又报了一遍"
            assert again.members[0]["tokens_input"] == 30
            assert again.members[0]["tokens_output"] == 15
            assert again.members[0]["rounds"] == 3
        assert AiAnalysisTrace.query.filter_by(run_id=runner.run_id).count() == 3
        run = db.session.get(AiAnalysisRun, runner.run_id)
        assert (run.tokens_input, run.tokens_output) == (30, 15)
        assert client.calls == spent
    finally:
        _drop(runner)


def test_a_reconnect_sees_every_round_not_just_the_last_eight():
    """SSE 断开再连 / 抽屉重开时补的是**全量** —— 实时那个窗口只有 8 轮。"""
    runner, _client = _interrupted_run(kill_after=10, max_rounds=12, with_job=True)
    try:
        assert len(_event_rows(runner.run_id)) == 10
        _simulate_restart(runner)
        snap = run_progress.snapshot(runner.run_id)
        assert snap is not None
        assert len(snap.rounds) == 10, "补回来的快照仍然被 8 轮窗口截住了"
        assert snap.rounds_seen == 10 and snap.rounds_truncated is False
        assert [item["agent_round"] for item in snap.rounds] == list(range(1, 11))

        # 抽屉与 SSE 读的是同一个入口（`job_service.progress_payload` → 这里）。
        from services.ai import job_service

        payload = job_service.progress_payload_for_run(runner.run_id)
        assert payload is not None
        assert payload["source"] == "ledger"
        assert len(payload["rounds"]) == 10
        assert payload["members"][0]["rounds"] == 10
    finally:
        _drop(runner)


def test_a_run_that_is_still_alive_is_never_rewritten_by_the_reader():
    """护栏：跑在**别的进程**里的分析每次读进度都会经过恢复路径，不许被本地补写。

    形态与真实一致：事件有、trace 没有、状态还是 `running`（写它的进程还在）。
    """
    runner = _make_run()
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, member="S1", member_index=1, member_total=1,
                   tokens_in=10, tokens_out=5),
        )
        # **换进程**：本进程没有它的内存快照（跑在别的 worker 里时就是这个形态）。
        run_progress.reset_for_tests()
        snap = run_progress.snapshot(runner.run_id)
        assert snap is not None and snap.source == "ledger", "活着的运行也该看得到它的进度"
        assert AiAnalysisTrace.query.filter_by(run_id=runner.run_id).count() == 0, (
            "对一条还在跑的运行补写了 trace —— 它会与那边最后落库的账撞唯一键"
        )
        run = db.session.get(AiAnalysisRun, runner.run_id)
        assert run.tokens_input is None
    finally:
        _drop(runner)


def test_a_run_with_a_persisted_conclusion_serves_that_instead_of_a_ledger_snapshot():
    """跑完的运行：进度那一份该让位（否则抽屉会在「分析中」与「已结束」之间来回跳）。"""
    runner = _make_run(status="succeeded")
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, member="", member_index=1, member_total=1, status="final",
                   tokens_in=10, tokens_out=5),
        )
        run = db.session.get(AiAnalysisRun, runner.run_id)
        run.response_payload = "{}"
        db.session.commit()
        # 换进程：内存里没有实时快照，这一条才真的在验「账本那一份该让位」。
        run_progress.reset_for_tests()
        assert run_progress.snapshot(runner.run_id) is None
    finally:
        _drop(runner)


def test_the_restored_ledger_numbers_match_what_the_live_snapshot_showed():
    """同一个 run：**实时那一份**与**从库里补的那一份**逐成员数字必须一样。

    两条路各写一遍求和，就会出现「跑动中 30、重启后 45」这种数 —— 那正是这个仓库最贵的
    一类缺陷。这里把它们摆在一起比。
    """
    runner = _make_run()
    try:
        for index in (1, 2):
            run_progress.publish(
                runner.run_id, runner.project_id,
                _frame(index, member="S1", member_index=1, member_total=2,
                       status="final" if index == 2 else "requests",
                       tokens_in=110 * index, tokens_out=11 * index,
                       # 累计值 = 前几轮之和：第 1 轮 110/11，第 2 轮 330/33。
                       cum_in=110 * index * (index + 1) // 2,
                       cum_out=11 * index * (index + 1) // 2,
                       cache_read=7,
                       counts={"requests": index, "executed": index, "failed": 0,
                               "truncated": 0, "refused": 0, "dropped": 0,
                               "candidates": 1 if index == 2 else None}),
            )
        live = run_progress.snapshot(runner.run_id)
        assert live is not None and live.source == ""
        run_progress.reset_for_tests()  # 换进程
        restored = run_progress.snapshot(runner.run_id)
        assert restored is not None and restored.source == "ledger"
        assert [dict(item) for item in live.members] == [dict(item) for item in restored.members]
        assert live.job_tokens == restored.job_tokens
        assert len(live.rounds) == len(restored.rounds)
    finally:
        _drop(runner)


def test_the_usage_is_not_folded_in_twice_once_it_is_persisted():
    """已经落库的账**不再**当成「本次运行尚未落库的用量」报出去（同一笔不许算两次）。

    这一条守的是快照顶上那两个字段的口径：预算那一档算「本周期已用」就是
    Σ(run.tokens_input + run.tokens_output)（`analysis_budget._evaluate_scope`），
    所以 run 上这两列一有值，事件里这份账就已经在判定里了 —— 再报一遍，
    `routes/ai_analysis_routes.ai_run_progress` 会拿它去 `with_live_usage` 加第二次。
    被中断那一次正是这个形态：`reconcile_interrupted_run` 填的就是这两列。

    **收住的是顶上那一对，不是整份账**：逐成员的账与落没落库无关，界面要靠它画
    「每个成员分别花了多少」—— 所以这里同时钉住它逐字没变。
    """
    runner = _make_run()
    try:
        run_progress.publish(
            runner.run_id, runner.project_id,
            _frame(1, member="S1", member_index=1, member_total=1,
                   tokens_in=30, tokens_out=15),
        )
        run_progress.reset_for_tests()  # 换进程：只能从账本读

        before = run_progress.snapshot(runner.run_id)
        assert before is not None and before.job_tokens == 45, (
            "账还没落库时它就是「尚未落库的用量」—— 预算那一档靠它提前说「已经超了」"
        )

        # 落库（等价于被中断那一次被补账：`reconcile_interrupted_run` 填的就是这两列）
        run = db.session.get(AiAnalysisRun, runner.run_id)
        run.tokens_input, run.tokens_output = 30, 15
        db.session.commit()

        after = run_progress.snapshot(runner.run_id)
        assert after is not None
        assert after.job_tokens is None, (
            "已经落库的账又被报成「尚未落库的用量」—— 判定里会加第二遍"
        )
        assert after.prompt_tokens is None and after.completion_tokens is None, (
            "token 那一档收住了、费用那一档还按这几个数折 —— 两个都要收（路由是拿它们"
            "按价格表算「本次尚未落库的费用」的）"
        )
        # 账本身照报：逐成员的数字与落库之前逐字一样。
        assert [dict(item) for item in after.members] == [
            dict(item) for item in before.members
        ]
        assert after.rounds_seen == before.rounds_seen == 1
    finally:
        _drop(runner)


# ==========================================================================
#  四、两个请求只产生一个活动 run
# ==========================================================================


def test_two_requests_for_the_same_target_share_one_active_run():
    """同一个目标连点两次：**附着**到同一条 job（活动 run 只有一个）。"""
    import services.ai.job_service as job_service
    from tests.test_ai_analysis_service import (
        _create_project as _svc_project,
    )
    from tests.test_ai_analysis_service import (
        _create_repo as _svc_repo,
    )
    from tests.test_ai_analysis_service import (
        _create_weekly_config,
    )

    project = _svc_project()
    repo = _svc_repo(project.id, _uid("repo"), "svn", "table")
    config = _create_weekly_config(
        project.id, repo, _uid("W"), datetime(2026, 3, 1), datetime(2026, 3, 8)
    )
    db.session.commit()
    try:
        first = job_service.create_or_attach_job(
            config=config, requested_mode="full", trigger_source="manual"
        )
        second = job_service.create_or_attach_job(
            config=config, requested_mode="full", trigger_source="manual"
        )
        assert first.created is True and first.job.id == second.job.id
        assert second.attached is True, "第二次点击没有附着 —— 会再花钱跑一次"
        active = AiAnalysisJob.query.filter(
            AiAnalysisJob.active_key == first.job.active_key
        ).all()
        assert len(active) == 1, "同一个输入指纹下出现了两条活动 job"
    finally:
        # 收尾顺序**不能随便**：`ai_project_analysis_config` / `ai_weekly_analysis_state`
        # 是这一条流程里**懒创建**的，它们的 `project_id` 是非空外键 —— 先删项目会让
        # ORM 去把子行的 project_id 置 NULL，撞 NOT NULL，整个收尾烂在这里
        # （而那会把用例真正的失败遮掉）。所以按「子 → 父」显式清。
        try:
            for row in AiAnalysisJob.query.filter_by(project_id=project.id).all():
                row.active_key = None
            db.session.commit()
            AiAnalysisJob.query.filter_by(project_id=project.id).delete(
                synchronize_session=False
            )
            AiAnalysisRun.query.filter_by(project_id=project.id).delete(
                synchronize_session=False
            )
            AiProjectAnalysisConfig.query.filter_by(project_id=project.id).delete(
                synchronize_session=False
            )
            AiWeeklyAnalysisState.query.filter_by(project_id=project.id).delete(
                synchronize_session=False
            )
            AiProjectApiKey.query.filter_by(project_id=project.id).delete(
                synchronize_session=False
            )
            db.session.commit()
            WeeklyVersionDiffCache.query.filter_by(config_id=config.id).delete(
                synchronize_session=False
            )
            db.session.commit()
            db.session.delete(config)
            db.session.delete(repo)
            db.session.commit()
            db.session.delete(project)
            db.session.commit()
        except Exception:  # noqa: BLE001 —— 收尾失败不许遮住用例本身的失败
            db.session.rollback()


# ==========================================================================
#  五、SQLite：模型调用期间不持有写事务
# ==========================================================================


def _probe_engine():
    """指向同一个库文件的**另一条连接**（`timeout` 很短：被挡住时能立刻看出来）。"""
    path = db.engine.url.database
    return create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.3})


def _probe_write(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO _lock_probe(x) VALUES (1)"))


def test_the_probe_actually_sees_a_held_write_transaction():
    """**先证明这条探针是活的**（否则下一条用例可能只是「探针坏了」而假绿）。

    在第一条连接上挂一个未提交的写事务 —— 第二条连接的写必须被挡（SQLite 的写锁）。
    """
    engine = _probe_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS _lock_probe(x INTEGER)"))
    try:
        db.session.execute(text("INSERT INTO _lock_probe(x) VALUES (9)"))
        with pytest.raises(Exception) as caught:
            _probe_write(engine)
        assert "lock" in str(caught.value).lower(), caught.value
    finally:
        db.session.rollback()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM _lock_probe"))


def test_no_write_transaction_is_held_while_the_model_call_runs():
    """**验收条**：模型调用进行中，另一条连接必须写得动库。

    写法是刻意的：探针写在**模型调用里面**（假 client 的 `complete` 里），因为要证明的
    正是「引擎每轮落完事件就收掉事务，不是攥着它跨过下一次调用」。
    """
    runner = _make_run()
    engine = _probe_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS _lock_probe(x INTEGER)"))
    seen = {"writes": 0, "locked": 0}

    class _ProbingClient:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, *, temperature=None):
            from services.ai.llm_client import ChatResult

            self.calls += 1
            try:
                _probe_write(engine)
                seen["writes"] += 1
            except Exception:  # noqa: BLE001 —— 被挡住就是这条用例要抓的形态
                seen["locked"] += 1
            text = _final(_anomaly()) if self.calls >= 3 else _requests(
                {"type": "commit_detail", "commit": COMMIT}
            )
            return ChatResult(
                text=text, model="fake", prompt_tokens=10, completion_tokens=5
            )

    try:
        client = _ProbingClient()
        outcome = _drive_engine(runner, client)
        assert outcome.status == "succeeded", (
            outcome.error_message,
            [(item.index, item.status, item.note) for item in outcome.rounds],
        )
        assert client.calls == 3
        assert seen["locked"] == 0, (
            "模型调用期间库被写事务锁住了 —— SQLite 上这会把整台机器的写都挡住"
        )
        assert seen["writes"] == 3, "探针一次都没写成，这条用例没测到东西"
        # 顺带钉住「每轮都落库了」：三次调用 → 三条事件
        assert len(_event_rows(runner.run_id)) == 3
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM _lock_probe"))
        _drop(runner)
