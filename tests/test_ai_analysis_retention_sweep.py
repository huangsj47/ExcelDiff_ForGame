# -*- coding: utf-8 -*-
"""AI 分析的保留策略必须**真的删掉东西**（含子表）。

## 缺陷形态

`cleanup_expired_analysis_runs` 只对 `ai_analysis_run` 发一条批量 DELETE。而
`AiAnalysisTrace` / `AiAnalysisAnomaly` 的 `run_id` 外键**没有** `ON DELETE CASCADE`
（全仓 `models/` 里没有一处 `ondelete`），SQLite 的 `PRAGMA foreign_keys=ON` 又是真的
开着的（`utils/sqlite_config.py` 的 connect 监听器，`app.py:251` 导入）。

于是只要有一条过期 run 带着轮次明细（**每次真跑过分析的都是**），那条 DELETE 就抛
`FOREIGN KEY constraint failed`，整个事务回滚 —— 不是「留下孤儿行」，而是
**这条保留策略从上线起一条都没删过**：`ai_analysis_run` 与 `ai_analysis_trace`
只涨不减（trace 每轮一行、含完整回答文本，是这两张表里的大头）。

实测（修复前）：`cleanup_expired_analysis_runs(retention_days=30)` 返回 `None`，
打印「清理AI分析缓存失败: (sqlite3.IntegrityError) FOREIGN KEY constraint failed」，
run / trace / anomaly 一行都没少。

## 第二种缺陷形态：没有外键的那张子表（`ai_analysis_round_event`）

`ai_analysis_trace` / `ai_analysis_anomaly` 漏删会**报错**（FK 顶回来），所以当初漏掉它们
是立刻看得见的；而 `ai_analysis_round_event` **刻意没有外键**（见模型 docstring），
漏掉它一声不响 —— 而且后果更重：

* 删父行既不报错、也不带走它，事件行**留成孤儿**；
* run 的 id 会被后面新建的 run **复用**（SQLite 取「现存最大 + 1」）；
* 于是**下一次**运行的逐轮视图会把上一批早已被清理掉的事件行混进来
  （`services/ai/round_events.events_for_run` 只按 `run_id` 读，再按
  `member_index/round/id` 排序）。

症状是幽灵行而不是垃圾：2026-09-25 的全量测试红过一次，机制正是它
（`tests/test_ai_usage_input_breakdown.py` 读到了别的运行留下的 `reasoning_tokens=None`）。

## 断言口径

* 「run 被删了」不够，**子表也必须被删** —— 否则就是换成了留下取不到的死行；
* 反向自检：窗口内的 run 连同子表必须**原样保留**（免得把清理写成整表清空也变绿）；
* 前提守卫：在这个环境里 FK 确实是拦人的（证明上面这套推理成立，而不是我猜错了）。

保留天数传 10000、行龄 20000 天：测试库是整个会话共用的
（`tests/conftest.py` 只守 IO，没有逐用例重置），把 cutoff 放得离别的用例的行很远，
就不会顺手删掉别人的数据。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import app, create_tables, db
from models import Project
from models.ai_analysis import (
    AiAnalysisAnomaly,
    AiAnalysisRoundEvent,
    AiAnalysisRun,
    AiAnalysisTrace,
)
from services.ai_analysis_service import cleanup_expired_analysis_runs

# 离别的用例足够远的窗口（见模块 docstring）
_FAR_DAYS = 10000
_OLD_AGE_DAYS = 20000


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _project() -> Project:
    project = Project(code=_uid("P"), name=_uid("retention"))
    db.session.add(project)
    db.session.flush()
    return project


def _run(project_id: int, *, age_days: int, with_children: bool = True, events: int = 0) -> int:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=1,
        target_key=_uid("group"),
        status="succeeded",
        scope="full",
        trigger_source="manual",
        created_at=created,
        started_at=created,
        finished_at=created,
    )
    db.session.add(run)
    db.session.flush()
    if with_children:
        db.session.add(
            AiAnalysisTrace(
                run_id=run.id, round_index=1, outcome="final", response_text="结论"
            )
        )
        db.session.add(
            AiAnalysisAnomaly(run_id=run.id, project_id=project_id, title="异常")
        )
    # 逐轮事件行：条数由调用方点名（`round` 是成员内序号，同一成员内不许重复）。
    for round_no in range(1, events + 1):
        db.session.add(
            AiAnalysisRoundEvent(
                run_id=run.id,
                project_id=project_id,
                member="S1",
                member_index=1,
                member_total=2,
                round=round_no,
                status="final",
                tokens_input=100,
                tokens_output=20,
            )
        )
    db.session.commit()
    return run.id


def _survivors(run_id: int) -> dict:
    return {
        "runs": AiAnalysisRun.query.filter_by(id=run_id).count(),
        "traces": AiAnalysisTrace.query.filter_by(run_id=run_id).count(),
        "anomalies": AiAnalysisAnomaly.query.filter_by(run_id=run_id).count(),
    }


def _event_count(run_id: int) -> int:
    """这条 run 还留着几行逐轮事件（**单独一个读数**，不并进 `_survivors`：
    那个字典的形状被别的用例按整体比过，加一个键就是改口径）。"""
    return AiAnalysisRoundEvent.query.filter_by(run_id=run_id).count()


def test_the_sweep_deletes_expired_runs_and_their_children():
    """**核心回归**：过期 run 连同 trace / anomaly 一起走。"""
    with app.app_context():
        create_tables()
        project = _project()
        old_run = _run(project.id, age_days=_OLD_AGE_DAYS)

        result = cleanup_expired_analysis_runs(retention_days=_FAR_DAYS)

        assert result is not None, (
            "清理失败了（返回 None）—— 这几乎总是 FK 约束把整条 DELETE 顶回来了；"
            "子表必须先删"
        )
        assert result >= 1, f"过期 run 没被删掉：返回 {result}"
        left = _survivors(old_run)
        assert left["runs"] == 0, "过期 run 还在"
        assert left["traces"] == 0, (
            "run 删了但轮次明细还在 —— 子表没删只是把孤儿行留下了："
            "AiAnalysisAnomaly.queue 只按 run_id 查，这些行谁都取不到"
        )
        assert left["anomalies"] == 0, "run 删了但异常条目还在"


def test_the_sweep_deletes_expired_round_events_too(monkeypatch):
    """**核心回归（幽灵行）**：过期 run 的逐轮事件行必须一起走。

    这张子表**没有外键**（见模块 docstring 的第二种缺陷形态与模型 docstring），所以
    漏删它不报错 —— 后果不是「留下几行垃圾」，而是 run id 被复用之后，**下一次**运行的
    逐轮视图读到这一批的事件行。
    """
    from services.ai import run_cache_source

    # 日志口径也一起钉住。断言挂在 `log_print` 上而不是 `capsys`：日志器在 import 时就
    # 持有了原始 stdout 的引用，`capsys` 抓不到它写出去的内容（本仓库既有测试记着这条）。
    messages: list[str] = []
    monkeypatch.setattr(
        run_cache_source,
        "log_print",
        lambda *args, **kwargs: messages.append(str(args[0] or "")),
    )

    with app.app_context():
        create_tables()
        project = _project()
        old_run = _run(project.id, age_days=_OLD_AGE_DAYS, events=3)
        fresh_run = _run(project.id, age_days=1, events=2)

        # 前提守卫：事件行**确实造出来了**。没有这一句，「清理后一条不剩」在「压根没造
        # 出来」时也是绿的 —— 那这条用例什么也没证明（本仓库的老毛病）。
        assert _event_count(old_run) == 3, "前提不成立：过期 run 的事件行没造出来，会假绿"

        result = cleanup_expired_analysis_runs(retention_days=_FAR_DAYS)

        assert result is not None, (
            "清理失败了（返回 None）—— 这几乎总是 FK 约束把整条 DELETE 顶回来了；"
            "子表必须先删"
        )
        assert result >= 1, f"过期 run 没被删掉：返回 {result}"
        assert _event_count(old_run) == 0, (
            "run 删了但逐轮事件行还在 —— 这张子表没有外键，删父行既不报错也不带走它；"
            "run id 会被复用，下一次运行的逐轮视图（round_events.events_for_run 只按 "
            "run_id 读）会把这些行当成自己的"
        )
        # 反向自检放在同一条用例里：免得把「整表清空」写成绿的。
        assert _event_count(fresh_run) == 2, "窗口内的运行的事件行被一起清掉了"
        assert _survivors(old_run) == {"runs": 0, "traces": 0, "anomalies": 0}, (
            "加事件行不该改坏原来那两张子表的删法"
        )
        joined = "\n".join(messages)
        assert "3 条轮次事件" in joined, (
            f"清理日志没把事件行的条数报出来 —— 出了事没人知道这几行是跟着 run 走的：{joined}"
        )


def test_runs_inside_the_window_survive_with_their_children():
    """反向自检：窗口内的记录（含子表）必须原样保留。

    没有这一条，「把清理写成整表清空」也能让上面那条变绿。
    """
    with app.app_context():
        create_tables()
        project = _project()
        fresh_run = _run(project.id, age_days=1)

        cleanup_expired_analysis_runs(retention_days=_FAR_DAYS)

        left = _survivors(fresh_run)
        assert left == {"runs": 1, "traces": 1, "anomalies": 1}, (
            f"窗口内的记录被清掉了：{left}"
        )


def test_an_expired_run_without_children_is_one_transaction():
    """没有子表的过期 run 同样要删掉（子表的两条 DELETE 不该影响它）。"""
    with app.app_context():
        create_tables()
        project = _project()
        bare_run = _run(project.id, age_days=_OLD_AGE_DAYS, with_children=False)

        assert cleanup_expired_analysis_runs(retention_days=_FAR_DAYS) is not None
        assert _survivors(bare_run)["runs"] == 0


def test_foreign_keys_are_enforced_here():
    """前提守卫：这套推理建立在「FK 真的拦人」上，先把这个前提钉住。

    如果哪天有人关掉 `PRAGMA foreign_keys`，本文件的核心结论（必须先删子表）就不再
    由约束强制了 —— 这条会先红，提示「理由变了，去改注释和断言」。
    """
    with app.app_context():
        create_tables()
        project = _project()
        run_id = _run(project.id, age_days=1)

        with pytest.raises(IntegrityError):
            db.session.execute(
                db.text("DELETE FROM ai_analysis_run WHERE id = :rid"), {"rid": run_id}
            )
            db.session.commit()
        db.session.rollback()
        assert _survivors(run_id)["runs"] == 1, "回滚之后父行应当还在"
