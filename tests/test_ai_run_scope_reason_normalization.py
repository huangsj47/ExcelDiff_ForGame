# -*- coding: utf-8 -*-
"""WP9：范围口径的两处收口 + 运行期原因归一 + `profile=tables` 的 summary 口径。

## 这个文件守的四件事

1. **`allowed_paths=[]` 是「什么都不许」**（P0，安全）。`None` 才是「不限制」——
   老 payload 缺键时行为必须逐字不变。空列表若被当成不限制，平台明明声明了「只看配表」，
   模型却读得到全部代码 diff；`change_set` 与 `result_payload` 两处必须同一口径。
2. **运行期原因归一**：`first_run` 是历史字面值，写侧统一成 `no_conclusion_baseline`
   （读侧仍认旧值）。否则 job 上会一直挂着一个「平台把我升成全量了」的原因，而契约 1.5
   之后那条升级已经不再发生。
3. **`profile=tables` 的 summary 口径**：提示词里「本次变更 N 个」与「这个版本改了多少」
   都还是**配表仓库窗口**的数 —— WP20 把**读取范围**放开（`allowed_paths=None`）之后，
   这两个数更不能跟着「可读文件数」走（`all_tracked` 时它无从计算）：输入收窄是事实，
   读取放宽不是。
4. **focus 在前、profile 收口在后**：判定读的是真实变化数，不是可读范围。

**测试库是会话级共用的**：每个用例自造独立分组，结束时按分组键收干净，绝不数全表。
"""
from __future__ import annotations

# ruff: noqa: I001 —— 同 `tests/test_ai_task_e8_paths.py`：这几个模块互为环形依赖，
# `services.task_worker_service` 必须先加载，否则任务处理器只加载了一半就被取名字。

import json
import uuid
from datetime import datetime, timedelta, timezone

from app import app, create_tables, db
from models import BackgroundTask, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import (
    MODE_INCREMENTAL,
    AiAnalysisJob,
    AiAnalysisRun,
    AiDiffSnapshot,
    AiDiffSnapshotItem,
    AiWeeklyAnalysisState,
)
import services.ai_analysis_service as ai_service
import services.task_worker_service as worker_service  # noqa: F401 —— 环形依赖的定序，见上
import services.task_worker_task_handlers as task_handlers
from services.ai import job_service
from services.ai.change_set import from_weekly_payload
from services.ai.engine import EngineOutcome
from services.ai.protocol import Anomaly
from services.ai.result_payload import result_payload
from services.ai.snapshot_store import load_baseline

TABLE = "tables/[30]道具表_CfgItem.xlsx"
CODE = "code/a.lua"
CODE_EXTRA = "code/b.lua"
TABLE_COMMIT = "a" * 40
CODE_COMMIT = "c" * 40

_MISSING = object()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ==========================================================================
#  一、`allowed_paths=[]` 语义：空列表 = 一条都不许（P0，安全）
# ==========================================================================


def _payload(allowed_paths=_MISSING) -> dict:
    """手写一份周版本 payload：`_MISSING` = 老 payload（根本没有这个键）。"""
    payload = {
        "mode": "weekly",
        "scope": "incremental",
        "delta_files": [
            {
                "file_path": TABLE,
                "latest_commit_id": TABLE_COMMIT,
                "file_type": "table",
                "repository_id": 11,
            },
            {
                "file_path": CODE,
                "latest_commit_id": CODE_COMMIT,
                "file_type": "code",
                "repository_id": 22,
            },
        ],
    }
    if allowed_paths is not _MISSING:
        payload["allowed_paths"] = allowed_paths
    return payload


def test_an_empty_allow_list_refuses_every_path():
    """**空列表不是「没给」**：它意味着这一批一条可读路径都没有。

    这条是本文件里最像「口味」的一条，实际是安全判据：`profile=tables` 的批次里一张
    表都没有时，写侧给的就是空列表。把空列表当成不限制，模型就能读代码 diff ——
    与平台声明正好相反，而且报告层不会报错。
    """
    change = from_weekly_payload(_payload([]))

    assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is False, (
        "空白名单仍放行了配表 —— 空列表被当成了「不限制」"
    )
    assert change.scope.path_allowed(CODE_COMMIT, CODE) is False
    assert change.commits == (), "空白名单下仍把文件列进了变更清单"
    assert change.is_empty


def test_none_and_a_missing_key_keep_the_old_unlimited_behavior():
    """`None`（profile=all）与「缺键」（老 payload）必须逐字回到旧行为。

    兼容口径与上面的空列表语义**必须分开**：前者是「不知道，照旧全放」，后者是写侧
    明确声明的「一条都不许」。旧 payload 一夜之间读不到任何 diff 是最坏的回退。
    """
    for payload in (_payload(None), _payload()):
        change = from_weekly_payload(payload)

        assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is True
        assert change.scope.path_allowed(CODE_COMMIT, CODE) is True
        assert CODE in change.summary


def _anomaly(*, title: str, file_path: str, severity: str) -> Anomaly:
    return Anomaly(
        title=title,
        category="config_id",
        severity=severity,
        confidence="high",
        evidence=(f"{file_path} 第 3 行",),
        commit="d" * 40,
        file_path=file_path,
    )


def _outcome(*anomalies: Anomaly) -> EngineOutcome:
    return EngineOutcome(
        status="succeeded", anomalies=tuple(anomalies), report_markdown="# 风险评估\n"
    )


def test_an_empty_allow_list_marks_every_located_finding_out_of_scope():
    """输出侧同一口径：空列表时**有路径的结论全部越界**，不参与定级与计数。

    无路径的结论仍按旧口径放行（「什么都不许读」不等于「什么都不许说」）—— 把跨文件
    结论吞掉会静默降级，而用户看不出有一条关键结论被丢掉。
    """
    outcome = _outcome(
        _anomaly(title="代码分支条件写错", file_path=CODE, severity="critical"),
        _anomaly(title="整体流程缺回滚", file_path="", severity="high"),
    )

    payload = result_payload(outcome, {"summary": {}, "allowed_paths": []})

    by_title = {entry["title"]: entry for entry in payload["anomalies"]}
    assert by_title["代码分支条件写错"]["out_of_scope"] is True, (
        "空白名单下的结论没有标范围外 —— 它会计入风险等级"
    )
    assert by_title["整体流程缺回滚"]["out_of_scope"] is False
    assert len(payload["anomalies"]) == 2, "越界条目被丢掉了 —— 报告里必须仍能看到模型报过它"
    assert payload["risk_level"] == "mid_high", (
        "越界的那条 critical 参与了定级（等级成了 high）——「什么都不许读」反而更严重"
    )
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"
    assert any("范围外" in reason for reason in payload["risk_reasons"]), (
        "空列表把结论全筛掉时没有交代 ——「平台不采纳」会被读成「模型觉得没问题」"
    )


def test_none_keeps_the_risk_scoring_unchanged():
    """`None` 时定级逐字不变（与加 `allowed_paths` 之前同一份）。

    WP27 起 `None` 档按「本次配表差异 + claims 关联」判 in-scope，所以这里必须把
    `CODE` 放进 `delta_files` —— 否则测到的是「无任何差异锚点 → 保守单列」，
    不是「缺键与 `None` 同一口径」。
    """
    outcome = _outcome(_anomaly(title="代码分支条件写错", file_path=CODE, severity="critical"))

    baseline = {"summary": {}, "delta_files": [{"file_path": CODE}]}
    legacy = result_payload(outcome, dict(baseline))
    unlimited = result_payload(outcome, {**baseline, "allowed_paths": None})

    assert legacy["anomalies"] == unlimited["anomalies"]
    assert legacy["risk_level"] == unlimited["risk_level"] == "high"
    assert legacy["anomalies"][0]["out_of_scope"] is False


# ==========================================================================
#  二、写侧：profile=tables 的 summary 口径（提示词 N 不再大于可读数）
# ==========================================================================


def _seed_cache(config, repo, path: str, commit: str, file_type: str | None) -> None:
    db.session.add(
        WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repo.id,
            file_path=path,
            file_type=file_type,
            merged_diff_data="{}",
            base_commit_id="b" * 40,
            latest_commit_id=commit,
            commit_count=1,
            cache_status="completed",
            diff_version="1.18.0",
        )
    )


def _make_group(
    *,
    table_paths: tuple[str, ...] = (TABLE,),
    code_paths: tuple[str, ...] = (),
    start_offset_days: tuple[int, int] = (-7, 0),
) -> dict:
    """一个周版本批次：项目 + 配表仓库 + 代码仓库 + 两条同批次配置 + 缓存行。

    **同一个「版本名」**（`W1 - <repo>`）是必须的：`weekly_batch_configs` 按
    「项目 + 窗口 + 版本名」取输入集，名字不同就不是同一批（REV-AI-002）。
    自己开 app context：这些用例的断言要在 context 内跑，造数据这一步不必一起挤进去。
    """
    with app.app_context():
        # 会话级共用的测试库：本文件可能第一个跑（`create_all` 还没人调过）。
        create_tables()
        now = datetime.now(timezone.utc)
        project = Project(code=_uid("P"), name=_uid("wp9"))
        db.session.add(project)
        db.session.flush()
        repos = {}
        for kind in ("table", "code"):
            repo = Repository(
                project_id=project.id,
                name=_uid(kind),
                type="git",
                url=f"https://example.invalid/{_uid(kind)}.git",
                branch="main",
                resource_type=kind,
                clone_status="completed",
            )
            db.session.add(repo)
            db.session.flush()
            repos[kind] = repo

        start_time = now + timedelta(days=start_offset_days[0])
        end_time = now + timedelta(days=start_offset_days[1])
        configs = {}
        for kind in ("table", "code"):
            cfg = WeeklyVersionConfig(
                project_id=project.id,
                repository_id=repos[kind].id,
                name=f"W1 - {repos[kind].name}",
                description="",
                branch="main",
                start_time=start_time,
                end_time=end_time,
                cycle_type="custom",
                is_active=True,
                auto_sync=True,
                status="active",
            )
            db.session.add(cfg)
            db.session.flush()
            configs[kind] = cfg

        for index, path in enumerate(table_paths):
            _seed_cache(
                configs["table"], repos["table"], path, f"{TABLE_COMMIT[:36]}{index:04d}", None
            )
        for index, path in enumerate(code_paths):
            _seed_cache(
                configs["code"], repos["code"], path, f"{CODE_COMMIT[:36]}{index:04d}", "code"
            )
        db.session.commit()
        return {
            "project_id": project.id,
            "cfg_table_id": configs["table"].id,
            "cfg_code_id": configs["code"].id,
            "group_key": ai_service.build_weekly_group_key(configs["table"]),
            "table_paths": list(table_paths),
            "code_paths": list(code_paths),
        }


def test_tables_profile_counts_the_config_table_window_not_the_read_scope():
    """混合批次（1 个代码 + 2 张表）+ profile=tables：

    * `summary.batch_files` / `total_files` = 配表仓库窗口（2）—— **输入**只剩它；
    * `summary.window_files` = **配表仓库窗口（2）**—— profile 先把 configs 收窄到
      配表仓库，「这个版本改了多少」的事实也跟着只剩它（2026-11 WP14 的语义）；
    * `allowed_paths is None` / `policy.decision.allowed_files is None` /
      `policy.decision.read_scope == "all_tracked"`（2026-11 WP20）：**读取**不设上限，
      未改动的引用方表与代码仓库的消费方都能按需读；
    * 提示词里不再出现「还有 1 个文件的名字没列出来 —— 但它们你可以读到」这句假话。
    """
    group = _make_group(table_paths=(TABLE, "tables/t1.xlsx"), code_paths=(CODE,))
    try:
        with app.app_context():
            payload, _state, skip = ai_service.build_weekly_payload(group["cfg_table_id"])
            assert skip is None

            assert payload["analysis_profile"] == "tables"
            # 输入收窄：代码仓库的文件不进清单，计数也只剩配表仓库那 2 个。
            assert payload["summary"]["batch_files"] == 2, (
                "profile=tables 时 summary 仍是整批计数 —— 提示词会把不可读的代码文件说成本次变更"
            )
            assert payload["summary"]["total_files"] == 2
            assert payload["summary"]["window_files"] == 2, (
                "window_files 是配表仓库窗口（profile=tables 已把 configs 收窄）"
            )
            # 读取放宽（WP20）：不再有路径白名单，可读文件数无从计算。
            assert payload["allowed_paths"] is None, (
                "tables 又把读取范围收成 delta 路径 —— 跨表/跨仓库取证读不到"
            )
            assert payload["policy"]["decision"]["allowed_files"] is None
            assert payload["policy"]["decision"]["read_scope"] == "all_tracked"

            change = from_weekly_payload(payload)
            assert "还有 1 个文件的名字没有列出来" not in change.summary, (
                "提示词仍宣称有一批不可读文件「你可以读到」—— 模型会照它去点名索取"
            )
            assert CODE not in change.summary
    finally:
        _cleanup(group)


def test_profile_narrows_the_configs_before_focus():
    """**顺序**：profile 先把 configs 收窄到配表仓库，focus 只在其中再筛，最后判范围。

    profile 是「这一轮分析哪些仓库」的范围决定，必须发生在 focus 与 `_decide_scope`
    之前；反过来（先 focus 再 profile）会让 profile 的收窄晚于阈值判定，用户选
    「只看配表」时 `scope` 仍按整批文件算 —— 那正是旧的「增量总退回全量」入口。
    """
    group = _make_group(table_paths=(TABLE, "tables/t1.xlsx"), code_paths=(CODE,))
    try:
        with app.app_context():
            payload, _state, skip = ai_service.build_weekly_payload(
                group["cfg_table_id"], focus="table"
            )
            assert skip is None
            # profile 收窄后只剩 2 张表；focus 与判定读到的都是这 2 个。
            assert payload["policy"]["decision"]["real_delta"] == 2
            # summary 的可读数（2）与配表仓库窗口（2）一致。
            assert payload["summary"]["batch_files"] == 2
            assert payload["summary"]["window_files"] == 2
    finally:
        _cleanup(group)


# ==========================================================================
#  三、运行期原因归一：first_run → no_conclusion_baseline
# ==========================================================================


def _set_api_key(project_id: int) -> None:
    ai_service.set_project_api_key(project_id, "sk-test", updated_by="tester")
    ok, message, errors = ai_service.update_project_analysis_config(
        project_id, {"auto_weekly_enabled": True}, updated_by="tester"
    )
    assert ok, (message, errors)


def _job_for(group: dict) -> tuple[int, int]:
    """走生产入口建一条增量 job（含任务行），返回 `(job_id, task_id)`。"""
    config = db.session.get(WeeklyVersionConfig, group["cfg_table_id"])
    created = job_service.create_or_attach_job(
        config=config, requested_mode=MODE_INCREMENTAL, trigger_source="manual"
    )
    db.session.commit()
    job_id, task_id = created.job.id, created.job.task_id
    assert task_id, "job 没有排出任务 —— 这条用例跑不到「运行开始」那一刻"
    return job_id, task_id


def test_a_first_run_records_no_conclusion_baseline_instead_of_first_run(monkeypatch):
    """首跑（没有可做差快照）开跑后，job 上的原因**不再是 `first_run`**。

    `first_run` 是 2026-11-01 之前的历史字面值。契约 1.5 之后「没有结论基线」不再意味着
    「升成全量」，原因码也就必须跟着改名 —— 否则界面会照着旧词说「平台把这次增量升成了
    全量」，而真实原因只是「还没有基准快照」。
    """
    group = _make_group(table_paths=(TABLE,))
    try:
        with app.app_context():
            _set_api_key(group["project_id"])
            job_id, task_id = _job_for(group)

            def _execute(*_args, **_kwargs):
                return {"status": "succeeded", "error_message": None}

            monkeypatch.setattr(ai_service, "_execute_analysis", _execute)
            outcome = ai_service.run_weekly_analysis_background(
                group["cfg_table_id"],
                task_id=task_id,
                trigger_source="manual",
                requested_mode="incremental",
            )
            assert outcome["status"] == "succeeded", outcome

            job = db.session.get(AiAnalysisJob, job_id)
            assert job.state == "running", "运行开始了，job 却没进「分析中」"
            assert job.effective_mode == "full", "首跑（无基准快照）的输入口径应当是整窗"
            assert job.upgrade_reason == "no_conclusion_baseline", (
                f"首跑原因仍写着历史字面值：{job.upgrade_reason!r}"
            )
    finally:
        _cleanup(group)


def test_the_already_running_settlement_normalizes_the_reason_too():
    """第二条写入口（`already_running` 收口）必须用同一个词。

    两处各写一遍字面值，迟早会一处归一、一处漏 —— 那正是这个用例存在的理由：它走的是
    `task_handlers.settle_job_from_result`，不是 `run_weekly_analysis_background`。
    """
    group = _make_group(table_paths=(TABLE,))
    try:
        with app.app_context():
            _set_api_key(group["project_id"])
            job_id, task_id = _job_for(group)

            run = AiAnalysisRun(
                project_id=group["project_id"],
                target_type="weekly",
                target_id=group["cfg_table_id"],
                target_key=group["group_key"],
                status="running",
                scope="full",
                trigger_source="manual",
                active_key=f"claim-{_uid('k')}",
                request_payload=json.dumps(
                    {"policy": {"reason": "first_run"}, "scope": "full"},
                    ensure_ascii=False,
                ),
            )
            db.session.add(run)
            db.session.commit()

            job = task_handlers.settle_job_from_result(
                {"status": "skipped", "reason": "already_running", "run_id": run.id},
                task_id=task_id,
            )
            assert job is not None
            assert job.effective_mode == "full"
            assert job.upgrade_reason == "no_conclusion_baseline", (
                f"already_running 收口仍写着历史字面值：{job.upgrade_reason!r}"
            )
    finally:
        _cleanup(group)


# ==========================================================================
#  收尾：按分组键清掉本文件造出来的分析行
# ==========================================================================


def _cleanup(group: dict) -> None:
    """按**分组键**收掉本用例造出来的分析行（会话级共用的库里不许全表 delete）。

    自己开 app context：调用点在 `finally` 里，可能已经在用例的 context 之外。
    """
    with app.app_context():
        group_key = group["group_key"]
        snapshot_ids = [
            row.id for row in AiDiffSnapshot.query.filter_by(group_key=group_key).all()
        ]
        if snapshot_ids:
            AiDiffSnapshotItem.query.filter(
                AiDiffSnapshotItem.snapshot_id.in_(snapshot_ids)
            ).delete(synchronize_session=False)
            AiDiffSnapshot.query.filter(
                AiDiffSnapshot.id.in_(snapshot_ids)
            ).delete(synchronize_session=False)
        AiAnalysisJob.query.filter(AiAnalysisJob.target_key == group_key).delete(
            synchronize_session=False
        )
        BackgroundTask.query.filter(BackgroundTask.file_path == group_key).delete(
            synchronize_session=False
        )
        AiWeeklyAnalysisState.query.filter(
            AiWeeklyAnalysisState.group_key == group_key
        ).delete(synchronize_session=False)
        AiAnalysisRun.query.filter(AiAnalysisRun.target_key == group_key).delete(
            synchronize_session=False
        )
        db.session.commit()
