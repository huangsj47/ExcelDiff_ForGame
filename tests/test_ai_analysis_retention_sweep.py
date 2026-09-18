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
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun, AiAnalysisTrace
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


def _run(project_id: int, *, age_days: int, with_children: bool = True) -> int:
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
    db.session.commit()
    return run.id


def _survivors(run_id: int) -> dict:
    return {
        "runs": AiAnalysisRun.query.filter_by(id=run_id).count(),
        "traces": AiAnalysisTrace.query.filter_by(run_id=run_id).count(),
        "anomalies": AiAnalysisAnomaly.query.filter_by(run_id=run_id).count(),
    }


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
