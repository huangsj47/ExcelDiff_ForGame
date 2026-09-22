# -*- coding: utf-8 -*-
"""项目级互斥：同一项目下同时只允许一条 AI 分析在跑。

## 为什么需要这一组

改动之前的两层互斥都是 **target 级**的：job 级按 `target_type|target_key|focus` 算，
run 级按**快照内容**算。于是「同一目标重复点」会被附着到同一条 job（好行为，不许坏），
但**同一项目下两个不同目标可以同时跑** —— 一次分析约 3.4 元，用户要的是「这个项目里
有分析在跑，就别再让我触发新的」。

所以这一组守的是**「项目级」这三个字**：判据退回 target 级时，
`test_a_second_target_in_the_same_project_is_refused` 必须红。不红就等于根本没测出
这件事（它守的是「**另一个**目标也被挡」）。

同时守**反方向**：这道闸门不许把「同一目标重复点会附着」改坏 —— 附着那一瞬间那条 job
本来就是活的，判据必须能把它排除掉。

## 三张表里的第二张

`test_a_running_commit_analysis_without_a_job_also_blocks` 钉的是「判据要同时查
`AiAnalysisJob` 与 `AiAnalysisRun`」：单提交那条入口**根本不建 job**（直接进
`_create_run`），只查 job 表的实现在这条用例上会红 —— 而它的症状正是「项目里其实在跑，
界面却说没有」。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.ai.job_service as job_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from models.ai_analysis import MODE_INCREMENTAL, STATE_RUNNING, AiAnalysisRun
from services.ai import project_gate

_COMMIT_TARGET = 987654


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _project() -> Project:
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
    return project


def _config(project: Project) -> WeeklyVersionConfig:
    repo = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="table",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    now = datetime.now(timezone.utc)
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=_uid("weekly"),
        branch="main",
        start_time=now - timedelta(days=7),
        end_time=now,
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _running_commit_run(project: Project, *, age_seconds: int = 0) -> AiAnalysisRun:
    moment = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="commit",
        target_id=_COMMIT_TARGET,
        status=STATE_RUNNING,
        scope="full",
        trigger_source="manual",
        created_at=moment,
        started_at=moment,
    )
    db.session.add(run)
    db.session.commit()
    return run


def test_a_second_target_in_the_same_project_is_refused():
    """**这条守的就是「项目级」。** 判据退回 target 级时它必红。"""
    with app.app_context():
        create_tables()
        project = _project()
        first_cfg = _config(project)
        second_cfg = _config(project)

        first = job_service.create_or_attach_job(
            config=first_cfg, requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()
        assert first.created is True

        with pytest.raises(job_service.ProjectBusyError) as caught:
            job_service.create_or_attach_job(
                config=second_cfg, requested_mode=MODE_INCREMENTAL
            )
        assert caught.value.reason == "project_analysis_running"
        assert str(first.job_id) in str(caught.value), "拒绝时没有说清在跑的是哪一条"


def test_the_same_target_still_attaches_instead_of_being_refused():
    """反方向：**不许把「同一目标重复点会附着」改坏**。

    附着那一瞬间那条 job 本来就是活的，所以判据必须能把它排除掉 —— 排不干净的实现
    会把一次正常的重复点击变成一句「项目忙」。
    """
    with app.app_context():
        create_tables()
        project = _project()
        cfg = _config(project)

        first = job_service.create_or_attach_job(
            config=cfg, requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()
        second = job_service.create_or_attach_job(
            config=cfg, requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()

        assert second.created is False, "同一目标重复点没有附着，而是建了第二条"
        assert second.job_id == first.job_id


def test_a_running_commit_analysis_without_a_job_also_blocks():
    """**判据要同时查两张表。** 单提交那条入口不建 job，只查 job 表的实现在这里会红。"""
    with app.app_context():
        create_tables()
        project = _project()
        cfg = _config(project)
        _running_commit_run(project)

        with pytest.raises(job_service.ProjectBusyError):
            job_service.create_or_attach_job(config=cfg, requested_mode=MODE_INCREMENTAL)


def test_a_zombie_running_record_does_not_block_forever():
    """超过 `STALE_RUNNING_SECONDS` 的 running 是**进程被杀留下的僵尸**，不算「在跑」。

    拿它挡住新分析，用户只能干等一小时 —— 而那个进程已经不存在了。
    """
    with app.app_context():
        create_tables()
        project = _project()
        cfg = _config(project)
        _running_commit_run(project, age_seconds=7200)

        result = job_service.create_or_attach_job(
            config=cfg, requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()
        assert result.created is True, "僵尸 running 把新分析挡住了"


def test_another_project_is_not_blocked():
    """闸门只按**项目**过滤：别的项目在跑，与我无关。"""
    with app.app_context():
        create_tables()
        busy_project = _project()
        other_project = _project()
        _running_commit_run(busy_project)

        result = job_service.create_or_attach_job(
            config=_config(other_project), requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()
        assert result.created is True


def _running_weekly_run(project: Project, config_id: int) -> AiAnalysisRun:
    now = datetime.now(timezone.utc)
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="weekly",
        target_id=config_id,
        status=STATE_RUNNING,
        scope="full",
        trigger_source="manual",
        created_at=now,
        started_at=now,
    )
    db.session.add(run)
    db.session.commit()
    return run


def test_the_gate_only_cares_about_other_targets():
    """**闸门管的是「别的目标」。** 同一个目标自己在处理中，由既有那两层闸门裁决
    （job 的 `active_key` 唯一索引、run 的认领唯一索引），结论是「附着 /
    skipped-already_running」—— 在这里拦住会把那套正确行为改坏。

    这一条最初是漏的，被两只既有回归用例顶出来：`test_ai_job_protocol` 的并发同
    `active_key` 那条、`test_ai_run_lifecycle_idempotency` 的「手工入口撞上同输入的
    run」那条。所以补在这里，钉在闸门自己的用例里 —— 那两只用例的理由是别的事，
    它们红了会指向错的方向。
    """
    with app.app_context():
        create_tables()
        project = _project()
        cfg = _config(project)

        _running_weekly_run(project, cfg.id)
        result = job_service.create_or_attach_job(
            config=cfg, requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()
        assert result.created is True, "同一个目标自己在跑，被这道闸门挡住了"

        # 同一个项目里**另一个**目标在跑 → 仍然要挡。
        # 用第三个配置来问：第二个配置自己在跑（要排除它），而第一个配置上半场已经
        # 建过 job，再问会走「附着」那条路、根本到不了闸门。
        other_cfg = _config(project)
        _running_weekly_run(project, other_cfg.id)
        db.session.commit()
        third_cfg = _config(project)
        with pytest.raises(job_service.ProjectBusyError):
            job_service.create_or_attach_job(
                config=third_cfg, requested_mode=MODE_INCREMENTAL
            )


def test_the_gate_can_exclude_the_very_run_it_is_asked_about():
    """单提交那条路要用 `exclude_target`。

    那个提交**自己在跑** = `already_running`（带着运行号，界面据此附着上去看进度），
    不该被这道闸门改说成「项目忙」—— 那会把「连按两次就直接看到在跑的那条」丢掉。
    """
    with app.app_context():
        create_tables()
        project = _project()
        _running_commit_run(project)

        assert project_gate.describe_active_analysis(project.id) is not None
        assert project_gate.describe_active_analysis(
            project.id, exclude_target=("commit", _COMMIT_TARGET)
        ) is None, "排除自己那条之后仍然报忙 —— 界面会丢掉「附着上去看进度」这条路"
        # 排除的是**那一个**目标，不是「所有 commit」。
        assert project_gate.describe_active_analysis(
            project.id, exclude_target=("commit", _COMMIT_TARGET + 1)
        ) is not None
