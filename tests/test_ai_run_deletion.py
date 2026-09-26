# -*- coding: utf-8 -*-
"""删掉一条历次结论：**基线跟着退**、子表跟着走、在跑的不许删。

## 为什么这一组值得单独一个文件

「删一条记录」看起来是一句 DELETE，但它牵动三件互不相干的事：

1. **基线**。那一条不只是给人看的存档 —— 它同时是「下一轮增量继承谁的结论」那个指针
   （`ai_weekly_analysis_state.last_concluded_run_id`）的目标。删了不修指针，下一次增量
   会去读一行已经不存在的 run：读侧 `baseline_source.previous_run` 查不到它、悄悄退到
   更早那条，而 job 的账上仍写着「基线是刚被删的那条」—— 两边都不报错，报告来路不明。
2. **两张基线来源要同时正确**。除了指针，还有一条直接查表的路
   （`previous_run`，它同时喂给模型那份「历史上报过的问题」）。删完之后两者必须指向
   同一条运行，否则出现「job 说基线是 A、提示词里注入的是 B」。
3. **子表**。三张子表里有一张（`ai_analysis_round_event`）**没有外键**，漏删既不报错
   也不留下孤儿行的症状 —— 它的症状是 run id 被复用之后，**下一次**运行读到这一批的
   事件行（幽灵行）。

## 每条判据都配一个能打红的变异

本文件里的用例都是「删掉某一句实现就会红」的形状，具体在哪一句写在各自的 docstring 里。
"""
from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import routes.ai_analysis_routes as ai_routes
import services.ai.baseline_blocks as baseline_blocks
import services.ai.run_cache_source as run_cache
import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from auth.models import AuthUser, AuthUserProject
from models.ai_analysis import (
    AiAnalysisAnomaly,
    AiAnalysisRoundEvent,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiWeeklyAnalysisState,
)
from services import ai_report_history_service as history
from services.ai.baseline_source import previous_run
from services.ai.job_service import UPGRADE_REASON_FIRST_RUN, _decide_effective_mode
from services.ai.weekly_state import get_or_create_weekly_state
from tests.test_ai_history_survives_restart import REPORT_TEXT, _setup_config
from tests.test_ai_report_export_route import _run


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def _key(cfg) -> str:
    return ai_service.build_weekly_group_key(cfg)


def _mk(
    project_id: int,
    cfg,
    *,
    when: datetime | None = None,
    status: str = "succeeded",
    structured: bool = True,
    events: int = 0,
    children: bool = True,
) -> AiAnalysisRun:
    """一条周版本次运行（带结构化结论标记，可选地挂上三张子表的行）。"""
    run = _run(
        project_id=project_id,
        status=status,
        response_text=REPORT_TEXT if status != "failed" else "",
        payload={"risk_level": "high", "anomalies": []} if status != "failed" else {},
        error_message="模型返回不可用" if status == "failed" else None,
        target_type="weekly",
        target_id=cfg.id,
        target_key=_key(cfg),
        created_at=when,
    )
    # `_run` 不认识这一列（它只负责「一次运行长什么样」），而「能不能当基线」正是本文件
    # 大部分判据的自变量，所以在这里补上。
    run.conclusion_structured = structured if status in ("succeeded", "degraded") else None
    if children:
        db.session.add(
            AiAnalysisTrace(run_id=run.id, round_index=1, outcome="final", parsed_ok=True)
        )
        db.session.add(
            AiAnalysisAnomaly(run_id=run.id, project_id=project_id, fingerprint="fp-1", title="一条")
        )
    for i in range(events):
        db.session.add(
            AiAnalysisRoundEvent(run_id=run.id, project_id=project_id, round=i + 1, status="final")
        )
    db.session.commit()
    return run


def _state(project_id: int, key: str, *, baseline: AiAnalysisRun | None = None) -> AiWeeklyAnalysisState:
    state = get_or_create_weekly_state(
        project_id=project_id,
        group_key=key,
        base_name="W1",
        start_time=datetime(2026, 3, 1),
        end_time=datetime(2026, 3, 8),
    )
    state.last_concluded_run_id = baseline.id if baseline is not None else None
    state.last_analysis_run_id = baseline.id if baseline is not None else None
    state.last_analyzed_at = datetime(2026, 3, 9, 1, 2, 3)
    state.last_snapshot_digest = "digest-before-delete"
    state.last_triggered_at = datetime(2026, 3, 9, 4, 5, 6)
    db.session.commit()
    return state


# ==========================================================================
# 一、基线跟着退（本功能的要害）
# ==========================================================================


def test_deleting_the_newest_conclusion_moves_the_pointer_to_the_previous_run():
    """删掉最新那条 → 指针退到上一条，**且与注入给模型的那条一致**。

    变异：把指针修正整段删掉（或只删 `refresh_concluded_pointer` 里那个赋值）→ 指针
    还指着已经不在库里的那条 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        first = _mk(project.id, cfg, when=now - timedelta(days=3))
        second = _mk(project.id, cfg, when=now - timedelta(days=2))
        third = _mk(project.id, cfg, when=now - timedelta(days=1))
        state = _state(project.id, key, baseline=third)

        reason, message, extra = history.delete_target_run(third.id, expected_target_key=key)

        assert reason == "", message
        assert extra["runs"] == 1
        db.session.refresh(state)
        assert state.last_concluded_run_id == second.id, (
            "删了最新那条，基线还停在被删的那条上 —— 下一次增量会拿着一个不存在的运行号去读基线"
        )
        # **两条来源必须指向同一条运行**（一条是指针、一条是直接查表）。
        injected = previous_run("weekly", key)
        assert injected is not None and injected.id == state.last_concluded_run_id, (
            "job 账上的基线与注入给模型的那条不是同一行 —— 报告会来路不明"
        )
        assert first.id not in (second.id, third.id)  # 前两条都还在（这条删除只删了一条）


def test_a_markdown_only_run_cannot_become_the_baseline():
    """退回去的那一条**必须真留下了结构化结论**。

    只有 markdown 的那次（`conclusion_structured=False`）一条结构化结论都没有：拿它当
    基线会得出「共 0 条：无」，于是模型把上一轮报过的问题全部当新发现重报一遍。

    变异：把「用 `previous_run` 取新值」换成「库里最近一条 CONCLUDED 的 run」→ 指针退到
    那条 markdown-only 的运行上 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        structured = _mk(project.id, cfg, when=now - timedelta(days=2))
        # 降级 + 只有 markdown：`status` 是「跑完了」，但没有结构化结论。
        markdown_only = _mk(
            project.id, cfg, when=now - timedelta(days=1), status="degraded", structured=False
        )
        state = _state(project.id, key, baseline=structured)

        reason, _message, _extra = history.delete_target_run(
            structured.id, expected_target_key=key
        )

        assert reason == ""
        db.session.refresh(state)
        assert state.last_concluded_run_id is None, (
            "基线退到了一条只有 markdown 的运行上 —— 下一轮会把报过的问题全部当新发现重报"
        )
        assert previous_run("weekly", key) is None, (
            "这里与 `previous_run` 必须一致：它才是喂给模型那份历史结论的来源"
        )
        # 前提守卫：那条 markdown-only 的运行**确实还在**（不是被连带删掉了才为 None）。
        assert db.session.get(AiAnalysisRun, markdown_only.id) is not None


def test_deleting_every_conclusion_makes_the_next_incremental_run_full():
    """一条都不剩 → 下一次「增量」被平台升格成全量，并带上升级原因。

    这不是新功能（`job_service._decide_effective_mode` 早就这么判），但它是这个删除功能的
    **预期后果**：用户删空了之后点增量，不该看到一份拿着空基线跑出来的报告。

    变异：删空之后不清成 NULL（留着一个已删除的运行号）→ 这里判出来的是增量 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        only = _mk(project.id, cfg)
        state = _state(project.id, key, baseline=only)

        assert _decide_effective_mode("incremental", state)[0] == "incremental", "前提不成立"

        assert history.delete_target_run(only.id, expected_target_key=key)[0] == ""

        db.session.refresh(state)
        assert state.last_concluded_run_id is None
        assert _decide_effective_mode("incremental", state) == (
            "full",
            UPGRADE_REASON_FIRST_RUN,
        ), "删空之后增量没有被升格成全量 —— 用户会拿到一份以空基线跑出来的报告"


def test_deleting_two_runs_of_the_same_group_moves_the_pointer_to_what_is_left():
    """一次删同一分组的两条（其中一条正是指针那条）→ 指针落到剩下的那条上。

    **批量入口本身**要有这条：界面一次只删一条，但保留期清理是批量的，而它走的是同一个
    `remove_analysis_runs`。变异：把那两行接线的调用整段短路（`return 0`）→ 红。

    注：`removed_run_ids`（「这一批删了哪些」）与「这一行还在不在库里」两个判据在
    SQLite 上是**互相兜底**的 —— 有存在性判据在，集合少传一个 id 也照样算得对。
    两个都留着是因为前者不依赖「同一个事务里刚被批量 DELETE 掉的行，后面那条 SELECT
    一定看不见」（隔离级别不同的后端上那是另一个问题）。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        oldest = _mk(project.id, cfg, when=now - timedelta(days=3))
        middle = _mk(project.id, cfg, when=now - timedelta(days=2))
        newest = _mk(project.id, cfg, when=now - timedelta(days=1))
        state = _state(project.id, key, baseline=newest)

        assert run_cache.remove_analysis_runs([newest.id, middle.id]) is not None

        db.session.refresh(state)
        assert state.last_concluded_run_id == oldest.id


# ==========================================================================
# 二、只在真受影响时才动那个指针
# ==========================================================================


def test_deleting_an_unrelated_conclusion_leaves_a_valid_pointer_alone():
    """指针**合法地**落后于更新的运行时，删另一条不该把它搬走。

    这是真实存在的分歧：用户点过全量、或那条结论没被标成结构化，指针就与「按时间最近
    的那条」不是同一条。无条件重算会把指针在这些分歧上搬一次家 —— 而那是一次**没人
    要求**的基线变更。

    变异：`refresh_concluded_pointer` 里去掉「指针仍然有效就不动」那条闸门 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        lagging = _mk(project.id, cfg, when=now - timedelta(days=4))
        newer = _mk(project.id, cfg, when=now - timedelta(days=2))
        unrelated = _mk(project.id, cfg, when=now - timedelta(days=1))
        state = _state(project.id, key, baseline=lagging)

        assert history.delete_target_run(unrelated.id, expected_target_key=key)[0] == ""

        db.session.refresh(state)
        assert state.last_concluded_run_id == lagging.id, (
            "一次无关的删除把基线指针搬到了别处 —— 那是没人要求的基线变更"
        )
        # 前提守卫：那个「更新但没被选作基线」的运行**确实还在**（不然上面那一条
        # 「指针没动」也可能是因为它没得选）。
        assert db.session.get(AiAnalysisRun, newer.id) is not None


def test_a_dangling_pointer_is_repaired_by_any_deletion():
    """指针指向一行**本就不存在**的 run（保留期清理留下的脏指针）→ 顺手修好。

    这一条与上一条是一对：只看「在不在被删集合里」的话，这种历史脏指针永远修不好。

    变异：去掉存在性判据（只看「不在这批被删的里面」）→ 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        survivor = _mk(project.id, cfg, when=now - timedelta(days=3))
        unrelated = _mk(project.id, cfg, when=now - timedelta(days=1))
        state = _state(project.id, key, baseline=survivor)
        state.last_concluded_run_id = 999_999_999  # 一行不存在的 run
        db.session.commit()

        assert history.delete_target_run(unrelated.id, expected_target_key=key)[0] == ""

        db.session.refresh(state)
        assert state.last_concluded_run_id == survivor.id, "指向已删行的脏指针没有被修好"


def test_an_empty_group_key_leaves_the_pointer_alone():
    """分组键为空的状态行 → **一个字段都不动**。

    必须是这一条 fail-safe：`previous_run` 对空 key 返回 `None`，照直往下就把「我们不知道
    这个分组是谁」写成了「它没有基线」—— 下一次增量于是被升格成全量。

    变异：去掉 `if not key: return False` → 指针被清成 NULL → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        state = AiWeeklyAnalysisState(project_id=project.id, group_key="", last_concluded_run_id=7)
        db.session.add(state)
        db.session.commit()

        changed = baseline_blocks.refresh_concluded_pointer(state, {7})

        assert changed is False
        assert state.last_concluded_run_id == 7, "空分组键被当成了「没有基线」"


def test_a_null_pointer_is_not_backfilled():
    """指针本来就是 NULL（首次全量）→ 保持 NULL，**不反推**。

    变异：把「指针为 NULL 就直接返回」那条去掉 → 删一个无关运行会把一条更早的结论
    悄悄推成基线 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        older = _mk(project.id, cfg, when=now - timedelta(days=3))
        unrelated = _mk(project.id, cfg, when=now - timedelta(days=1))
        state = _state(project.id, key, baseline=None)

        assert history.delete_target_run(unrelated.id, expected_target_key=key)[0] == ""

        db.session.refresh(state)
        assert state.last_concluded_run_id is None, (
            "NULL 被反推成了历史结论 —— 「首次全量」这个语义被悄悄改掉了"
        )
        assert db.session.get(AiAnalysisRun, older.id) is not None


def test_the_watermarks_are_untouched():
    """删一条结论**只动结论基线**：时间水位线 / 快照指纹 / 节流水位线逐字不变。

    它们回答的是另外三个问题（最近一次完整分析、这份输入分析过没有、调度节流）。顺手
    清掉 `last_snapshot_digest` 的后果是下一 tick 把同一份输入当新变化再分析一遍。

    变异：修指针时顺手清了任何一个水位线 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg)
        state = _state(project.id, key, baseline=run)
        before = (
            state.last_analyzed_at,
            state.last_snapshot_digest,
            state.last_triggered_at,
            state.last_analysis_run_id,
        )

        assert history.delete_target_run(run.id, expected_target_key=key)[0] == ""

        db.session.refresh(state)
        assert (
            state.last_analyzed_at,
            state.last_snapshot_digest,
            state.last_triggered_at,
            state.last_analysis_run_id,
        ) == before, "删一条结论改动了水位线 —— 下一 tick 会把同一份输入再分析一遍"


# ==========================================================================
# 三、删干净：三张子表都要走
# ==========================================================================


def test_all_three_child_tables_go_with_the_run():
    """三张子表（trace / anomaly / **round_event**）一起删。

    `ai_analysis_round_event` 没有外键（见模型 docstring：加非空外键会打红那条静态护栏），
    所以漏删它**不报错**，症状是 run id 被复用之后下一次运行读到这一批的事件行。

    变异：去掉 `AiAnalysisRoundEvent` 那条 DELETE → `extra["events"]` 是 0、且库里还留着
    三行 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg, events=3)
        # 前提守卫：三张子表的行**确实造出来了**。没有这一句，一条都没造出来时也全绿。
        assert AiAnalysisTrace.query.filter_by(run_id=run.id).count() == 1
        assert AiAnalysisAnomaly.query.filter_by(run_id=run.id).count() == 1
        assert AiAnalysisRoundEvent.query.filter_by(run_id=run.id).count() == 3

        reason, _message, extra = history.delete_target_run(run.id, expected_target_key=key)

        assert reason == ""
        assert extra == {
            "run_id": run.id,
            "runs": 1,
            "traces": 1,
            "anomalies": 1,
            "events": 3,
        }, extra
        assert AiAnalysisTrace.query.filter_by(run_id=run.id).count() == 0
        assert AiAnalysisAnomaly.query.filter_by(run_id=run.id).count() == 0
        assert AiAnalysisRoundEvent.query.filter_by(run_id=run.id).count() == 0, (
            "逐轮事件行还在 —— 这张子表没有外键，run id 复用之后下一次运行会读到它们"
        )


def test_a_failed_run_can_be_deleted():
    """失败的记录也能删（用户口径：那是一条没有结论的存档，留着只会占着列表）。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        failed = _mk(project.id, cfg, status="failed")

        reason, _message, extra = history.delete_target_run(failed.id, expected_target_key=key)

        assert reason == ""
        assert extra["runs"] == 1
        assert db.session.get(AiAnalysisRun, failed.id) is None


def test_the_whole_delete_rolls_back_when_the_pointer_cannot_be_fixed():
    """指针修不出结果时**整笔回滚**：run 必须还在。

    留下「run 删了、指针没修」的状态比删不掉更糟：那条指针指向一个不存在的运行，而
    下一轮增量照常开跑。

    变异：把「修指针」放到 `commit` 之后 → run 已经没了 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _mk(project.id, cfg)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("模拟指针修正失败")

        original = baseline_blocks.refresh_concluded_pointers_for_removed_runs
        baseline_blocks.refresh_concluded_pointers_for_removed_runs = _boom
        try:
            result = run_cache.remove_analysis_runs([run.id])
        finally:
            baseline_blocks.refresh_concluded_pointers_for_removed_runs = original

        assert result is None, "失败必须返回 None（0 会被读成「本来就没东西可删」）"
        assert db.session.get(AiAnalysisRun, run.id) is not None, "整笔没有回滚，run 被删掉了"
        assert AiAnalysisTrace.query.filter_by(run_id=run.id).count() == 1


# ==========================================================================
# 四、拒绝的分支（服务层）
# ==========================================================================


@pytest.mark.parametrize("status", ["pending", "running"])
def test_an_in_flight_run_cannot_be_deleted(status):
    """还在跑的删不得：那条进程还会回写结果，删掉之后它踩到已删的行，报出来的是
    「分析失败」而不是「你刚删过它」。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg, status=status)

        reason, message, _extra = history.delete_target_run(run.id, expected_target_key=key)

        assert reason == "in_flight", message
        assert db.session.get(AiAnalysisRun, run.id) is not None, "被拒绝的那条不该被删掉"


def test_a_zombie_running_run_is_deletable():
    """僵尸 running（进程被杀，1 小时前）**可以删**。

    它在界面上就是一条「分析失败」（`effective_status`），列表 / `/latest` / `/progress`
    三处都这么说。按库里那个原值拒的话，用户会对着一条写着「分析失败」的记录点删除、
    被回绝说「它还在跑」。

    变异：`_removable_reason` 改用 `run.status` → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(
            project.id,
            cfg,
            status="running",
            when=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        assert run.effective_status == "failed", "前提不成立：这条记录在界面上不是失败"

        reason, _message, extra = history.delete_target_run(run.id, expected_target_key=key)

        assert reason == "", "僵尸 running 在界面上是失败，却不让删"
        assert extra["runs"] == 1


def test_a_commit_run_is_refused():
    """单提交的历次结论不开删除（产品口径：只开在周版本）。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="commit", target_id=999_999)

        reason, message, _extra = history.delete_target_run(run.id)

        assert reason == "not_weekly", message
        assert db.session.get(AiAnalysisRun, run.id) is not None


def test_a_mismatched_target_key_is_refused():
    """弹层是「同一个抽屉换目标」：带着别的版本的分组键来删，要拒。

    变异：去掉 `expected_target_key` 那段比对 → 删成功 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg)

        reason, message, _extra = history.delete_target_run(
            run.id, expected_target_key=key + "-别的版本"
        )

        assert reason == "target_mismatch", message
        assert db.session.get(AiAnalysisRun, run.id) is not None


def test_an_unknown_run_is_not_found():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        assert history.delete_target_run(999_999_999)[0] == "not_found"


# ==========================================================================
# 五、列表上那三个标记（界面按它们决定按钮与提示）
# ==========================================================================


def test_the_history_list_marks_the_baseline_and_who_may_delete():
    """`is_baseline` 标的是**指针指的那一条**（不是「时间最近的那条」），
    `deletable` 只在管理员 + 周版本 + 不在途时为真。

    变异：`is_baseline` 改成「第一条」→ 下面 `lagging` 那条断言翻面 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        lagging = _mk(project.id, cfg, when=now - timedelta(days=4))
        newer = _mk(project.id, cfg, when=now - timedelta(days=1))
        _state(project.id, key, baseline=lagging)

        payload = history.list_target_runs(kind="weekly", target_key=key, can_delete=True)

        by_id = {row["run_id"]: row for row in payload["runs"]}
        assert payload["can_delete"] is True
        assert payload["target_key"] == key
        assert by_id[lagging.id]["is_baseline"] is True
        assert by_id[newer.id]["is_baseline"] is False, (
            "「时间最近的那条」被标成了基线 —— 用户会以为删它只影响一个存档"
        )
        assert all(row["deletable"] for row in payload["runs"])

        # 不是管理员：一个都不能删（但列表照常给）。
        anonymous = history.list_target_runs(kind="weekly", target_key=key, can_delete=False)
        assert anonymous["can_delete"] is False
        assert not any(row["deletable"] for row in anonymous["runs"])


@pytest.mark.parametrize("status", ["pending", "running"])
def test_an_in_flight_run_is_not_marked_deletable(status):
    """新起的 pending / running **不进列表**（它是「还没有结论」的中间态），所以这一条
    判的是**判据函数本身**：它必须把在途两态判成「不许删」。

    走 `_removable_reason` 而不是从列表里读 `deletable`：列表里根本看不到这两种状态，
    从列表断言会变成一条永远绿的用例（空列表上 `all()` 也是真）。
    变异：改用 `run.status` 之外的东西、或干脆去掉在途那一档 → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg, status=status)

        assert history._removable_reason(run) == "in_flight"
        assert history._deletable(run, can_delete=True) is False
        # 列表那边确实看不到它（前提守卫：不是「判据错、恰好也没列出来」）。
        payload = history.list_target_runs(kind="weekly", target_key=key, can_delete=True)
        assert payload["runs"] == []


def test_a_commit_list_never_says_delete():
    """单提交的列表：`can_delete` 一律为假（服务层自己把产品口径兜住，路由传什么不算数）。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _run(project_id=project.id, target_type="commit", target_id=999_999)

        payload = history.list_target_runs(kind="commit", target_id=999_999, can_delete=True)

        assert payload["can_delete"] is False
        assert not any(row["deletable"] for row in payload["runs"])


# ==========================================================================
# 六、路由：权限、状态码、回执里那份重新取过的列表
# ==========================================================================


def _patch_admin(monkeypatch, allowed: bool):
    monkeypatch.setattr(ai_routes, "_has_project_admin_access", lambda _pid: allowed)


def _login_write(client, project_id: int) -> None:
    """一个**真的登录了**的会话。

    `tests/test_ai_report_export_route._login` 那一套（只写 `is_admin`）对 GET 够用，
    但 POST 会先过全局那道「写请求必须已登录」的闸（`utils/request_security` 的 401），
    它看的是 auth provider 的用户。所以这里按 `tests/test_ai_json_body_guard` 的办法造一个
    真实用户 —— 顺带也把 CSRF 那一步一起过了。
    """
    user = AuthUser(
        username=f"del_{uuid.uuid4().hex[:10]}",
        password_hash="x",
        role="project_admin",
        is_active=True,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(AuthUserProject(user_id=user.id, project_id=project_id, role="admin"))
    db.session.commit()
    client.environ_base["HTTP_X_CSRFTOKEN"] = _CSRF
    with client.session_transaction() as sess:
        sess["auth_user_id"] = user.id
        sess["auth_username"] = user.username
        sess["auth_role"] = "project_admin"
        sess["_csrf_token"] = _CSRF


_CSRF = "test-csrf-token"


def _post(client, url, body):
    """带 CSRF 的写请求。"""
    return client.post(
        url,
        json=body,
        headers={"Content-Type": "application/json", "X-CSRFToken": _CSRF},
    )


def test_a_non_admin_gets_403_and_the_run_survives(monkeypatch):
    """权限取项目管理员（与配置、api-key、触发分析同档）。

    变异：把闸门降成 `_has_project_access`（项目成员即可）→ 这条用例红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg)
        run_id = run.id
        _patch_admin(monkeypatch, False)
        with app.test_client() as client:
            _login_write(client, project.id)
            response = _post(client, f"/ai-analysis/runs/{run_id}/delete", {"target_key": key})

        assert response.status_code == 403, response.get_data(as_text=True)
        assert db.session.get(AiAnalysisRun, run_id) is not None


def test_the_delete_response_carries_the_fresh_list(monkeypatch):
    """回执里带**重新取一遍**的列表：被删的那条不在了，基线标记落到新的一行上。

    界面直接用它重画 —— 自己从旧列表里摘掉一条的话，`is_baseline` 会留在已经不再是指针
    的那一行上，而「基线退到哪」正是这个功能的全部意义。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        now = datetime.now(timezone.utc)
        older = _mk(project.id, cfg, when=now - timedelta(days=2))
        newest = _mk(project.id, cfg, when=now - timedelta(days=1))
        _state(project.id, key, baseline=newest)
        older_id, newest_id = older.id, newest.id
        _patch_admin(monkeypatch, True)
        with app.test_client() as client:
            _login_write(client, project.id)
            response = _post(
                client, f"/ai-analysis/runs/{newest_id}/delete", {"target_key": key}
            )

        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        assert body["success"] is True
        assert body["runs"] == 1
        assert body["history"]["can_delete"] is True
        assert [row["run_id"] for row in body["history"]["runs"]] == [older_id]
        assert body["history"]["runs"][0]["is_baseline"] is True, (
            "回执里那份列表没有把基线标记挪到新的一行 —— 界面会把它留在被删掉的那条上"
        )


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("in_flight", 409),
        ("mismatch", 400),
        ("missing", 404),
        ("commit", 400),
    ],
)
def test_the_route_maps_every_refusal_to_its_own_status(monkeypatch, variant, expected):
    """每一档拒绝有自己的状态码：404 已经不存在、400 请求不成立、409 只是要等一下。

    变异：全部按 400 回 → 409 那一条红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        body = {"target_key": key}
        if variant == "in_flight":
            run = _mk(project.id, cfg, status="running")
        elif variant == "commit":
            run = _run(project_id=project.id, target_type="commit", target_id=777_777)
            body = {}
        elif variant == "missing":
            # 「已经不存在了」用一个**从来没存在过**的号：造一条再删掉的话，删它这一步
            # 自己就会踩到 ORM 的级联（子表 `run_id` 被回填成 NULL），测的成了别的 bug。
            run = None
            run_id = 999_999_999
            body = {"target_key": key}
        else:
            run = _mk(project.id, cfg)
            body = {"target_key": key + "-别的"}
        run_id = run.id if run is not None else run_id
        _patch_admin(monkeypatch, True)
        with app.test_client() as client:
            _login_write(client, project.id)
            response = _post(client, f"/ai-analysis/runs/{run_id}/delete", body)

        assert response.status_code == expected, response.get_data(as_text=True)
        assert response.get_json()["success"] is False


def test_the_delete_route_is_a_write_route():
    """方法必须是 POST：它改数据，不该长得像幂等的取数请求。"""
    rules = {rule.rule: rule.methods for rule in app.url_map.iter_rules()}
    methods = rules["/ai-analysis/runs/<int:run_id>/delete"]
    assert "POST" in methods
    assert "GET" not in methods


# ==========================================================================
# 七、护栏：**任何一张带 run_id 的表，两条删除路径都要点到名**
# ==========================================================================
#
# 「删一条分析记录」在仓库里有**两条**路：按 id 删（`remove_analysis_runs`，用户删一条
# 与保留期清理都走它）与删光全部（`usage_statistics.purge_usage_statistics`）。两条路
# 各自手写要删哪几张表，于是「加第四张子表时只加一处」这件事**真的发生过**：
# `ai_analysis_round_event` 在 purge 那条路上漏了一整个版本（2026-09-26 复核发现）。
#
# 所以护栏不能只查一条路，也不能靠外键 —— 那张最容易漏的表**根本没有外键**
# （见 `models/ai_analysis/round_event.py`：加非空外键会打红项目删除那条护栏）。

#: 跟着那次运行一起死的表（有 `run_id`，行本身就是那次运行的一部分）。
_RUN_CHILD_MODELS = ("AiAnalysisTrace", "AiAnalysisAnomaly", "AiAnalysisRoundEvent")

#: 也有 `run_id`、但那是**引用**：job 指向它跑出来的那次运行，删 run 不许把 job 也删了
#: （那是「当时平台把这次分析记成什么」的账）。
_RUN_REFERENCE_MODELS = ("AiAnalysisJob",)


def _models_with_run_id() -> set:
    """所有带 `run_id` 这一列的模型的类名（**派生自 metadata**，不是手抄清单）。

    真机踩过这个坑的代价：漏删那张表的症状不是「多了几行垃圾」而是**错数据** ——
    `ai_analysis_run.id` 是裸 `Integer` 主键，表清空/删行之后 id 会被复用，而下一次运行的
    逐轮视图只按 `run_id` 过滤（`round_events.events_for_run`）。
    """
    found = set()
    for mapper in db.Model.registry.mappers:
        table = mapper.local_table
        if table is not None and "run_id" in table.columns:
            found.add(mapper.class_.__name__)
    return found


def _strip_comments(source: str) -> str:
    """静态断言前先剥注释。

    这个仓库的注释里会**原样引用**要提到的类名（本文件这几段注释就是），不剥就会假通过。
    """
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    return re.sub(r"(?m)#.*$", "", source)


def test_the_run_child_list_is_still_complete():
    """新加一张带 `run_id` 的表时**先红**，逼人做一次判断（子行 / 只是引用）。"""
    found = _models_with_run_id()
    known = set(_RUN_CHILD_MODELS) | set(_RUN_REFERENCE_MODELS)
    assert found == known, (
        f"`run_id` 那一族变了：多出来 {sorted(found - known)}，少了 {sorted(known - found)}。"
        f"新表要么加进 `_RUN_CHILD_MODELS`（跟着 run 一起删）并在两条删除路径里点到名，"
        f"要么加进 `_RUN_REFERENCE_MODELS`（不删）。"
    )


@pytest.mark.parametrize("func_name", ["remove_analysis_runs", "purge_usage_statistics"])
def test_every_run_child_table_is_deleted_by_both_paths(func_name):
    """两条删除路径都要把**每一张**子表点到名（护栏查源码，剥注释后再查）。

    `purge_usage_statistics` 这条在 2026-09-26 是真红的 —— 它漏了
    `ai_analysis_round_event` 一整个版本。
    """
    from services.ai import usage_statistics

    func = (
        run_cache.remove_analysis_runs
        if func_name == "remove_analysis_runs"
        else usage_statistics.purge_usage_statistics
    )
    body = _strip_comments(inspect.getsource(func))
    missing = [name for name in _RUN_CHILD_MODELS if name not in body]
    assert not missing, (
        f"{func_name} 没有删这几张子表：{missing}。漏删的症状不是「留点垃圾」而是**错数据**"
        f"（run id 复用之后，下一次运行的逐轮视图会读到已经删掉的那一批）"
    )


def test_purging_takes_the_round_events_with_it():
    """**真跑一遍**（护栏只查源码，改错了名字它照样绿）。

    全量重置是**全表**动作，而且有一条「在途运行存在时拒绝」的前置闸门 —— 别的用例
    留在库里的 `running` 行会让它直接拒绝（测试库是会话级共用的，没有逐用例重置）。
    所以这里先把运行清空（与 `tests/test_ai_usage_statistics.py::_clear_runs` 同一套
    办法，子表必须先删：批量 delete 不走 ORM 级联）。
    """
    from services.ai.usage_statistics import purge_usage_statistics

    with app.app_context():
        AiAnalysisRoundEvent.query.delete()
        AiAnalysisTrace.query.delete()
        AiAnalysisAnomaly.query.delete()
        AiAnalysisRun.query.delete()
        db.session.commit()

        project, _repo, cfg = _setup_config()
        run = _mk(project.id, cfg, events=2)
        assert AiAnalysisRoundEvent.query.filter_by(run_id=run.id).count() == 2, "前提不成立"

        ok, message, deleted = purge_usage_statistics(updated_by="tester")

        assert ok, message
        assert deleted["events"] == 2, deleted
        assert AiAnalysisRoundEvent.query.count() == 0, (
            "全量重置之后逐轮事件行还在 —— 下一次运行的 id 一复用，思考过程里就会混进"
            "上一批早已删掉的轮次"
        )


def test_a_pending_child_row_does_not_break_the_delete():
    """session 里挂着一行**还没提交**的子表行时，删除照样要成功、且那行不会被剩下。

    ## 这条用例证明的**只有**这一件事

    它不证明「`expunge` 扫了 `session.new`」—— 那个循环现在**只扫 `identity_map`**，
    而这条用例照样绿，原因是自动 flush：删除函数进门先查「要删哪几行」，那次查询会把
    这一行未提交的明细先 INSERT 进去（那时父行还在），紧接着的批量 DELETE 再把它一起
    带走。所以 `session.new` 那一档在这个调用序列上根本走不到（`_forget_rows_...` 的
    docstring 里写了完整的取舍）。

    变异：`remove_analysis_runs` 里去掉 `AiAnalysisTrace` 那条 DELETE → 父行被外键顶回来
    → 整笔回滚、返回 None → 红。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _key(cfg)
        run = _mk(project.id, cfg)

        # 故意只 add、不 commit。
        db.session.add(
            AiAnalysisTrace(run_id=run.id, round_index=7, outcome="final", parsed_ok=True)
        )

        reason, message, extra = history.delete_target_run(run.id, expected_target_key=key)

        assert reason == "", f"挂着一行未提交的子表行，删除就整笔失败了：{message}"
        assert extra["runs"] == 1
        assert AiAnalysisTrace.query.filter_by(run_id=run.id).count() == 0, (
            "那一行未提交的明细留在了库里（父行已经没了 —— 幽灵行）"
        )
