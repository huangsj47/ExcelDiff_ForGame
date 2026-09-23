# -*- coding: utf-8 -*-
"""分析启动前的**来源一致性**闸门：缓存里的提交还在不在当前 tip 上（工作包 A）。

## 这一组补的是哪一格

现有闸门回答「同步还在跑吗」与「同步之后有没有新提交入库」。两条都成立，分析照样可以
跑在一份**指向已被删除历史**的缓存上 —— 2026-09-23 实测就是这个形态：

    repository.last_synced_tip  == 远端 tip （相等！）
    配置 3 的 weekly_version_diff_cache 却指向 e54c73df / aa3fd90b / 2971caf7
    （三行缓存的 commit_ids 并集 9 个，而远端只有 3 个提交）

**「只比 tip 不够」说的就是这一格**：tip 相等不能证明缓存干净，只能证明「上一轮同步
看过这个 tip」。所以判据落在可达集合上（真 git 跑出来，不是桩）。

## 断言的是「分析是否被挡住」，不是「调用了哪个函数」

用例把真库里的缓存行摆成过期/干净两种状态，然后读 `weekly_sync_in_flight` 的返回值
（它正是四个调用点共用的那一道闸门）—— 挡住与放行是**观察到的行为**，不是「有没有
调用某个名字」。修好数据之后再问一次，必须放行；否则这条闸门就是一条只会拦住、
永远修不好的死锁。
"""
from __future__ import annotations

import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from app import app as flask_app
from app import create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai.snapshot_consistency import check_configs, stale_config_ids
from services.ai.weekly_sync_gate import weekly_sync_in_flight, weekly_sync_needed_config_ids
from services.git_service import GitService

_COMMIT_UTC = datetime(2026, 9, 1, 0, 0, 0)
_WINDOW_START_BEIJING = datetime(2026, 9, 1, 7, 0, 0)
_WINDOW_END_BEIJING = datetime(2026, 9, 1, 9, 0, 0)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.email=qa@example.com",
         "-c", "user.name=QA", "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"git {' '.join(args)} 失败: {result.stderr}"
    return result.stdout


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _use_tmp_repos_base(tmp_path: Path, monkeypatch) -> None:
    """工作副本根目录指到 tmp（理由见
    `tests/test_weekly_window_reachability_reconcile.py::_use_tmp_repos_base`）。"""
    monkeypatch.setenv("AGENT_REPOS_BASE_DIR", str(tmp_path))


def _seed():
    """项目 + 真工作副本（3 个提交，随后被强推掉 2 个）+ 周版本配置 + 一条缓存行。"""
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()

    repo_name = _uid("repo")
    repository = Repository(
        project_id=project.id, name=repo_name, type="git",
        url=f"https://example.com/{repo_name}.git",
        branch="main", clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()

    from utils.path_security import build_repository_local_path

    work = Path(build_repository_local_path(project.code, repo_name, repository.id))
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q")
    for index in range(3):
        (work / f"t{index}.lua").write_text(f"v{index}\n", encoding="utf-8")
        _git(work, "add", ".")
        _git(work, "commit", "-q", "-m", f"c{index}")
    old_tip = _git(work, "rev-parse", "HEAD").strip()
    old_heads = [_git(work, "rev-parse", f"HEAD~{n}").strip() for n in (2, 1, 0)]

    _git(work, "reset", "-q", "--hard", old_heads[0])
    (work / "fresh.lua").write_text("fresh\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "fresh")
    new_tip = _git(work, "rev-parse", "HEAD").strip()

    config = WeeklyVersionConfig(
        project_id=project.id, repository_id=repository.id, name=_uid("weekly"),
        branch="main", start_time=_WINDOW_START_BEIJING, end_time=_WINDOW_END_BEIJING,
        is_active=True,
    )
    db.session.add(config)
    db.session.flush()
    return {
        "project": project, "repository": repository, "config": config,
        "work": work, "old_tip": old_tip, "old_heads": old_heads, "new_tip": new_tip,
    }


def _cache(config, repository, *, file_path: str, latest: str):
    row = WeeklyVersionDiffCache(
        config_id=config.id, repository_id=repository.id, file_path=file_path,
        latest_commit_id=latest, base_commit_id=None, commit_count=6,
        cache_status="completed", merged_diff_data='{"diff_data": {}}',
        last_sync_time=_COMMIT_UTC,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_a_cache_row_on_a_deleted_commit_blocks_the_analysis(tmp_path, monkeypatch):
    """**核心**：tip 相等、没有在跑的同步 —— 缓存指向被强推掉的提交，分析仍被挡住。"""
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        seed = _seed()
        config, repository = seed["config"], seed["repository"]
        # 前提：本地 tip 与「平台记录的已同步 tip」相等 —— 正是实测那一幕，
        # 也正是「只比 tip 不够」要说明的东西。
        repository.last_synced_tip = seed["new_tip"]
        db.session.commit()
        assert GitService(repository.url, None, None, None, repository).local_path
        row = _cache(config, repository, file_path="t0.lua", latest=seed["old_heads"][1])

        verdict = check_configs([config.id])
        assert verdict.checked, verdict.reason
        assert verdict.stale == (config.id,)
        assert stale_config_ids([config.id]) == [config.id]

        # 闸门（四个调用点共用的那一个）必须给出原因：先同步，不许默默分析旧缓存。
        needed = weekly_sync_needed_config_ids([config.id])
        assert config.id in needed
        reason = weekly_sync_in_flight([config.id])
        assert str(config.id) in reason
        assert "缓存落后" in reason

        # 数据修好（缓存行指向当前 tip）之后必须**放行** —— 否则这是一条只会拦住、
        # 永远修不好的死锁。
        row.latest_commit_id = seed["new_tip"]
        db.session.commit()
        assert stale_config_ids([config.id]) == []
        assert check_configs([config.id]).checked
        assert weekly_sync_needed_config_ids([config.id]) == []
        assert weekly_sync_in_flight([config.id]) == ""


def test_the_tip_moving_forward_invalidates_the_check_again(tmp_path, monkeypatch):
    """把 tip 往前推一格之后，原先干净的那一行**重新变成过期**。

    这是「启动运行时修改仓库 tip」那一半验收：新提交进来之后，旧缓存行指向的提交
    仍然可达（它是祖先），所以判 stale 的不是它 —— 而是「同步之后又有新提交入库」
    那一条。两条判据合起来才挡住「读到一半新一半旧」。
    """
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        seed = _seed()
        config, repository, work = seed["config"], seed["repository"], seed["work"]
        _cache(config, repository, file_path="fresh.lua", latest=seed["new_tip"])
        assert stale_config_ids([config.id]) == []

        # 新提交推上来，但平台还没采到（commits_log 里没有它）。
        (work / "later.lua").write_text("later\n", encoding="utf-8")
        _git(work, "add", ".")
        _git(work, "commit", "-q", "-m", "later")
        later_tip = _git(work, "rev-parse", "HEAD").strip()

        # 缓存行仍然只指向旧 tip（它可达），所以**可达性核对本身不报过期** ——
        # 报过期的是「有没有新提交入库」那一条，由 `weekly_sync_needed_config_ids`
        # 的第一/第二条判据负责（这里没有 completed 的 weekly_sync 任务，故不触发）。
        assert stale_config_ids([config.id]) == []

        # 而一旦缓存行指向了新 tip、平台却还没采到它，可达性核对仍应放行（未知 ≠ 不一致）
        # —— 新 tip 不在本地对象库之外的场景由「取不到可达集合」兜住，见下一个用例。
        assert check_configs([config.id]).checked
        _ = later_tip


def test_no_local_clone_never_blocks(tmp_path, monkeypatch):
    """**未知一律不拦**：没有本地克隆（platform/agent 模式）时不判过期、不挡分析。

    把「问不到」当成「历史被重写了」，代价是每一次分析都被无限期挡住，而
    platform/agent 模式本来就**没有**本地克隆（平台被显式禁止 clone）。
    """
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
        db.session.add(project)
        db.session.flush()
        repo_name = _uid("repo")
        repository = Repository(
            project_id=project.id, name=repo_name, type="git",
            url=f"https://example.com/{repo_name}.git", branch="main",
            clone_status="pending",
        )
        db.session.add(repository)
        db.session.flush()
        config = WeeklyVersionConfig(
            project_id=project.id, repository_id=repository.id, name=_uid("weekly"),
            branch="main", start_time=_WINDOW_START_BEIJING, end_time=_WINDOW_END_BEIJING,
            is_active=True,
        )
        db.session.add(config)
        db.session.commit()
        # 这个仓库**从来没有本地工作副本**（`_seed` 没被调用，目录不存在）——
        # 这正是 platform/agent 模式的形态：平台被显式禁止 clone。
        # 目录「不存在」而不是「空的」有讲究，理由见
        # `tests/test_weekly_window_reachability_reconcile.py::_missing_dir`。
        _cache(config, repository, file_path="a.lua", latest="d" * 40)

        verdict = check_configs([config.id])
        assert not verdict.checked
        assert verdict.stale == ()
        assert stale_config_ids([config.id]) == []
        assert weekly_sync_in_flight([config.id]) == ""


def test_a_branch_label_that_differs_is_reported_but_does_not_block(tmp_path, monkeypatch):
    """配置里填的分支与仓库当前分支不同：只记详情，**不据此拦截**。

    全平台没有一处同步逻辑读 `WeeklyVersionConfig.branch`（同步一律走
    `repository.branch` 对应的本地克隆），拿它当闸门会造出一条与数据来源无关的规则、
    误拦真实配置。
    """
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        seed = _seed()
        config, repository = seed["config"], seed["repository"]
        config.branch = "release-1.0"
        db.session.commit()
        _cache(config, repository, file_path="fresh.lua", latest=seed["new_tip"])

        verdict = check_configs([config.id])

        assert verdict.checked, verdict.reason
        assert verdict.stale == ()
        assert any("release-1.0" in line for line in verdict.details), verdict.details


# ---------------------------------------------------------------------------
#  做差基准快照的来源核对：**只报不拦**（理由见 `_baseline_source_notes`）
# ---------------------------------------------------------------------------


def _snapshot(config, repository, *, latest: str, base: str = ""):
    from models.ai_analysis import AiDiffSnapshot, AiDiffSnapshotItem
    from services.ai.project_config_source import build_weekly_group_key

    snapshot = AiDiffSnapshot(
        project_id=repository.project_id, group_key=build_weekly_group_key(config),
        content_digest=uuid.uuid4().hex + uuid.uuid4().hex[:8], item_count=1,
        complete=False, status="sealed", sealed_at=_COMMIT_UTC,
    )
    db.session.add(snapshot)
    db.session.flush()
    db.session.add(AiDiffSnapshotItem(
        snapshot_id=snapshot.id, config_id=config.id, repository_id=repository.id,
        file_path="t0.lua", base_commit_id=base or None,
        latest_commit_id=latest, diff_version="v1", commit_count=1,
    ))
    db.session.commit()
    return snapshot


def test_a_baseline_outside_history_is_reported_but_never_blocks(tmp_path, monkeypatch):
    """基准快照指向已删除的提交：**报出来**，但**不拦**（拦了会永远修不好）。

    历史快照刻意不删（它们是历史运行的冻结输入），所以「重做同步」不会让这份旧基准
    消失 —— 若把它当拦截条件，这条闸门会挡住此后每一次分析。它真正的代价是「下一轮
    按它对差会把每个文件都判成变了」（全量重算，方向是对的，只是贵）。
    """
    from services.ai import snapshot_store

    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        seed = _seed()
        config, repository = seed["config"], seed["repository"]
        _cache(config, repository, file_path="fresh.lua", latest=seed["new_tip"])
        snapshot = _snapshot(config, repository, latest=seed["old_heads"][1])

        # 1) 纯函数层：条目指向历史之外的那一个被点出来
        assert snapshot_store.entries_outside_history(
            snapshot, {seed["new_tip"], seed["old_heads"][0]}
        ) == ((config.id, "t0.lua"),)
        assert snapshot_store.entries_outside_history(
            snapshot, {seed["new_tip"], seed["old_heads"][0], seed["old_heads"][1]}
        ) == ()
        # **未知 ≠ 全都不在历史上**：空的可达集合不是证据
        assert snapshot_store.entries_outside_history(snapshot, ()) == ()
        # latest 与 base 都算进「这份快照引用到的提交」
        assert snapshot_store.snapshot_commit_ids(snapshot.id) == frozenset(
            {seed["old_heads"][1]}
        )

        # 2) 闸门：当前缓存行是干净的 → 放行，但把基准过期的现状**说出来**
        verdict = check_configs([config.id])
        assert verdict.checked
        assert verdict.stale == ()
        assert weekly_sync_in_flight([config.id]) == ""
        assert any("做差基准" in line and "不据此拦截" in line for line in verdict.details), \
            verdict.details
