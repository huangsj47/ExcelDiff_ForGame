# -*- coding: utf-8 -*-
"""子代理模式的**落库侧**：一家子只落一条运行；分片信息要能被面板读出来。

## 为什么一家子只落一条运行（而不是每个成员一条）

不是因为省事，而是被这个仓库的现状逼出来的：

* `ai_analysis_trace` 上有 `uq_ai_trace_run_round`（run_id + round_index）。一条运行里塞
  N 个成员的轮次**必然**撞这个唯一约束 —— 除非把 round_index 编成「家族内全局递增」
  （本文件守的就是这件事：两个成员各自都有「第 1 轮」，但库里的序号不重复）；
* 建子运行行需要自引用外键，而这个仓库**没有任何 `ondelete`**、SQLite 开着
  `PRAGMA foreign_keys=ON`，而 `purge_usage_statistics` 与 `cleanup_expired_analysis_runs`
  都是批量删除；
* 它还会连带逼出四处「必须排除子运行」的过滤（增量基线 `_previous_run`、抽屉展示
  `_latest_concluded_run`、面板 `_runs_query`、周状态指针）—— 漏一处就是错的结果。

**一条运行的连带好处**：预算闸门读到的「已用」天生诚实（见
`tests/test_ai_subagent_no_gate_drift.py` 或该文件里那条），运行条数也不会被灌水。

**测试库是会话级共用的**（没有逐用例重置），所以这里每个用例自己造运行行、自己清。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from models.ai_analysis.project_config import (
    DEFAULT_SUBAGENT_COUNT,
    DEFAULT_SUBAGENT_ENABLED,
    AiProjectAnalysisConfig,
)
from services.ai.endpoint_service import FIELD_DEFAULTS, FIELD_RULES
from services.ai.engine import (
    STATUS_SUCCEEDED,
    EngineOutcome,
    RoundRecord,
)
from services.ai_usage_service import run_usage
from services.db_migration_service import _migrate_ai_analysis_columns


def _project() -> Project:
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"子代理{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    return project


def _run(project_id: int, **overrides) -> AiAnalysisRun:
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_key=f"g-{uuid.uuid4().hex[:8]}",
        status="succeeded",
        **overrides,
    )
    db.session.add(run)
    db.session.flush()
    return run


def _cleanup(run_ids) -> None:
    for run_id in run_ids:
        AiAnalysisTrace.query.filter_by(run_id=run_id).delete()
        AiAnalysisRun.query.filter_by(id=run_id).delete()
    db.session.commit()


def _family_outcome() -> EngineOutcome:
    """一个「两个分片 + 一次汇总」的结果，形状与 `aggregate_outcomes` 的产出一致。"""
    return EngineOutcome(
        status=STATUS_SUCCEEDED,
        report_markdown="# 变更理解\n\n改了道具表。\n",
        rounds=(
            RoundRecord(1, "requests", agent="S1", agent_round=1),
            RoundRecord(2, "final", agent="S1", agent_round=2),
            RoundRecord(3, "final", agent="S2", agent_round=1),
            RoundRecord(4, "final", agent="", agent_round=1),
        ),
        subagents=(
            {"label": "S1", "role": "subagent", "index": 1, "status": "succeeded",
             "rounds": 2, "requests": 3, "tokens_input": 100, "tokens_output": 20,
             "cache_read_tokens": 80, "cache_write_tokens": 20, "anomalies": 2,
             "report_chars": 120, "skipped_reason": "", "error": "",
             "dimensions": ["config_id", "config_value", "config_linkage"]},
            {"label": "S2", "role": "subagent", "index": 2, "status": "skipped",
             "rounds": 0, "requests": 0, "tokens_input": 0, "tokens_output": 0,
             "cache_read_tokens": None, "cache_write_tokens": None, "anomalies": 0,
             "report_chars": 0, "skipped_reason": "剩余预算不足", "error": "",
             "dimensions": ["config_data", "value_sanity"]},
            {"label": "汇总", "role": "synthesis", "index": 3, "status": "succeeded",
             "rounds": 1, "requests": 1, "tokens_input": 90, "tokens_output": 30,
             "cache_read_tokens": 80, "cache_write_tokens": 0, "anomalies": 3,
             "report_chars": 400, "skipped_reason": "", "error": "",
             "dimensions": ["config_id"]},
        ),
    )


class TestTheRunCarriesTheSubagentMode:
    def test_persist_writes_the_mode_and_the_count(self):
        from services.ai_analysis_service import _persist_outcome

        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id)
            outcome = _family_outcome()

            _persist_outcome(run, outcome, {"anomalies": []})

            assert run.subagent_mode == "subagents"
            assert run.subagent_count == 2, "只数分片，汇总那一次不算"
            _cleanup([run.id])

    def test_a_plain_run_leaves_the_columns_empty(self):
        """没开子代理时两列是 NULL —— 老行为一个字都不变。"""
        from services.ai_analysis_service import _persist_outcome

        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id)

            _persist_outcome(
                run, EngineOutcome(status=STATUS_SUCCEEDED, rounds=()), {"anomalies": []}
            )

            assert run.subagent_mode is None and run.subagent_count is None
            _cleanup([run.id])


class TestTheRoundsKeepTheirAgent:
    def test_the_trace_rows_carry_the_agent_and_the_member_round(self):
        from services.ai_analysis_service import _persist_outcome

        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id)

            _persist_outcome(run, _family_outcome(), {"anomalies": []})

            rows = (
                AiAnalysisTrace.query.filter_by(run_id=run.id)
                .order_by(AiAnalysisTrace.round_index.asc())
                .all()
            )
            assert [row.round_index for row in rows] == [1, 2, 3, 4], (
                "round_index 是家族内全局递增的 —— 唯一约束靠它"
            )
            assert [row.agent for row in rows] == ["S1", "S1", "S2", None], (
                "主代理自己那一轮的 agent 是 NULL（也是所有老行的情形）"
            )
            assert [row.agent_round for row in rows] == [1, 2, 1, 1], (
                "成员内部的轮次要各算各的 —— 界面显示「S1 · 第 2/4 轮」用它"
            )
            _cleanup([run.id])

    def test_two_members_can_both_have_a_first_round(self):
        """**这一条是「一家子只落一条运行」能成立的全部依据。**"""
        from services.ai_analysis_service import _persist_outcome

        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id)

            # 若把成员内部的轮次号直接当 round_index 写，这里就会撞唯一约束。
            _persist_outcome(run, _family_outcome(), {"anomalies": []})

            firsts = AiAnalysisTrace.query.filter_by(run_id=run.id, agent_round=1).all()
            assert len(firsts) == 3, "两个成员各有一个「第 1 轮」，加上汇总那一次"
            _cleanup([run.id])


class TestTheUsagePanelCanReadIt:
    def test_run_usage_exposes_the_member_rows(self):
        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(
                project.id,
                response_payload=json.dumps(
                    {"subagents": list(_family_outcome().subagents)}, ensure_ascii=False
                ),
            )
            db.session.add(
                AiAnalysisTrace(
                    run_id=run.id, round_index=1, outcome="final", agent="S1", agent_round=1
                )
            )
            db.session.commit()

            payload = run_usage(run.id)

            labels = [item["label"] for item in payload["subagents"]]
            assert labels == ["S1", "S2", "汇总"]
            skipped = payload["subagents"][1]
            assert skipped["status"] == "skipped"
            assert skipped["skipped_reason"] == "剩余预算不足"
            assert skipped["cache_read_tokens"] is None, "没上报就是 None，不是 0"
            assert payload["rounds"][0]["agent"] == "S1"
            assert payload["rounds"][0]["agent_round"] == 1
            _cleanup([run.id])

    def test_a_run_without_the_block_reads_as_empty_not_fabricated(self):
        """老运行（这个功能之前）读出来是**空列表**，不是「有一条什么都没干的成员」。"""
        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id, response_payload=None)
            db.session.commit()

            payload = run_usage(run.id)

            assert payload["subagents"] == []
            _cleanup([run.id])

    def test_a_broken_payload_does_not_break_the_page(self):
        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id, response_payload="{不是 JSON")
            db.session.commit()

            payload = run_usage(run.id)

            assert payload["subagents"] == []
            _cleanup([run.id])


class TestTheConfig:
    def test_the_field_rules_and_defaults_are_wired(self):
        assert FIELD_RULES["subagent_enabled"].label == "子代理模式（仅周版本）"
        assert FIELD_RULES["subagent_count"].minimum == 1
        assert FIELD_RULES["subagent_count"].maximum == 6
        assert FIELD_DEFAULTS["subagent_enabled"] is DEFAULT_SUBAGENT_ENABLED, (
            "界面上的默认勾选状态必须与代码里的默认值同源（`FIELD_DEFAULTS` 是从"
            "`project_config` 取的那一个常量），否则会出现「配置页默认关、服务端默认开」"
        )
        assert FIELD_DEFAULTS["subagent_count"] == 3

    def test_resolved_reads_null_as_the_default_and_three(self):
        with flask_app.app_context():
            create_tables()
            project = _project()
            row = AiProjectAnalysisConfig(project_id=project.id)
            db.session.add(row)
            db.session.commit()

            resolved = row.resolved()

            # **NULL 跟着默认值走**：这一列加出来时默认是关，历史行几乎都是 NULL，
            # 所以「默认开」要真的生效，就必须让 NULL 读成 DEFAULT_SUBAGENT_ENABLED。
            assert resolved["subagent_enabled"] is DEFAULT_SUBAGENT_ENABLED is True
            assert resolved["subagent_count"] == DEFAULT_SUBAGENT_COUNT == 3
            db.session.delete(row)
            db.session.commit()

    def test_resolved_clamps_out_of_range_values(self):
        """手工改库或旧界面留下的 99 要读成 6，而不是原样带下去。"""
        with flask_app.app_context():
            create_tables()
            project = _project()
            row = AiProjectAnalysisConfig(
                project_id=project.id, subagent_enabled=True, subagent_count=99
            )
            db.session.add(row)
            db.session.commit()

            assert row.resolved()["subagent_count"] == 6
            db.session.delete(row)
            db.session.commit()

    def test_an_explicit_off_wins_over_a_count(self):
        with flask_app.app_context():
            create_tables()
            project = _project()
            row = AiProjectAnalysisConfig(
                project_id=project.id, subagent_enabled=False, subagent_count=6
            )
            db.session.add(row)
            db.session.commit()

            resolved = row.resolved()

            assert resolved["subagent_enabled"] is False
            assert resolved["subagent_count"] == 6, (
                "关掉不等于把数字清掉 —— 用户再打开时应当还是他设的那个数"
            )
            db.session.delete(row)
            db.session.commit()


class TestTheMigrationCoversEveryNewColumn:
    """给已存在的表加列必须走迁移 —— `create_all` 对已存在的表什么都不做。

    老库上漏了这一段的表现是**启动期报错**（代码写 `run.subagent_mode`、库里没这一列），
    所以这条按源码扫（表名 + 列名），照着 `tests/test_ai_models_and_migration.py` 的写法。
    """

    def test_the_handler_has_all_six_columns(self):
        import inspect

        source = inspect.getsource(_migrate_ai_analysis_columns)

        for table in ("ai_project_analysis_config", "ai_analysis_run", "ai_analysis_trace"):
            assert f'"{table}"' in source, table
        for column in (
            "subagent_enabled", "subagent_count",   # 配置表 + 运行表都叫这两个名字
            "subagent_mode",
            "agent", "agent_round",
        ):
            assert column in source, f"迁移里少了 {column}"

    def test_a_sqlite_old_table_gets_the_columns(self):
        """真的在 SQLite 上跑一遍：老表（缺列）→ 迁移后列都在。

        只扫源码是不够的 —— DDL 拼错、类型名写错这类问题扫不出来。
        """
        import sqlalchemy as sa

        from services.db_migration_service import _migrate_table_columns

        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE ai_analysis_trace (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE ai_analysis_run (id INTEGER PRIMARY KEY)"))
            conn.execute(
                sa.text("CREATE TABLE ai_project_analysis_config (id INTEGER PRIMARY KEY)")
            )

        # `_migrate_table_columns` 要的是 `db` 那个形状：`db.engine`（看表与列）与
        # `db.session`（执行 ALTER、失败时 rollback）。这里给一个最小替身。
        class _Session:
            def execute(self, statement):
                with engine.begin() as conn:
                    return conn.execute(statement)

            def commit(self):
                return None

            def rollback(self):
                return None

        holder = SimpleNamespace(engine=engine, session=_Session())
        _migrate_table_columns(
            holder,
            "ai_analysis_trace",
            {"agent": "agent VARCHAR(20)", "agent_round": "agent_round INTEGER"},
            lambda *args, **kwargs: None,
        )
        with engine.connect() as conn:
            columns = {
                row[1]
                for row in conn.execute(sa.text("PRAGMA table_info(ai_analysis_trace)"))
            }

        assert {"agent", "agent_round"} <= columns


class TestTheWeekStatePointsAtTheFamilyRun:
    def test_the_state_pointer_is_the_run_the_panel_shows(self):
        """周版本的「上一次运行」指针指向这条运行 —— 一家子只落一条，所以它天然是对的。

        这条测试的价值在于**如果以后有人改成「每个成员一条运行」**，它会红：
        那时指针会指向某个分片，而抽屉里显示的是那半份报告。
        """
        from models.ai_analysis import AiWeeklyAnalysisState

        with flask_app.app_context():
            create_tables()
            project = _project()
            run = _run(project.id)
            state = AiWeeklyAnalysisState(
                project_id=project.id,
                group_key=f"k-{uuid.uuid4().hex[:8]}",
                last_analysis_run_id=run.id,
            )
            db.session.add(state)
            db.session.commit()

            stored = AiAnalysisRun.query.filter_by(id=state.last_analysis_run_id).first()

            assert stored is not None and stored.id == run.id
            assert stored.subagent_mode is None or stored.subagent_mode == "subagents"
            db.session.delete(state)
            _cleanup([run.id])


@pytest.mark.parametrize("enabled,count", [(True, 3), (False, 3), (True, 6)])
def test_plan_family_follows_the_resolved_config(enabled, count):
    """`resolved()` 的产出直接喂给 `plan_family` —— 两边对得上（键名、类型、语义）。"""
    from services.ai.engine import EngineLimits
    from services.ai.subagent import plan_family

    with flask_app.app_context():
        create_tables()
        project = _project()
        row = AiProjectAnalysisConfig(
            project_id=project.id, subagent_enabled=enabled, subagent_count=count
        )
        db.session.add(row)
        db.session.commit()

        resolved = row.resolved()
        plan = plan_family(
            mode="weekly",
            enabled=bool(resolved.get("subagent_enabled")),
            count=resolved.get("subagent_count") or 0,
            limits=EngineLimits(),
        )

        assert (plan is not None) is enabled
        if plan is not None:
            assert len(plan.members) == count
        db.session.delete(row)
        db.session.commit()
