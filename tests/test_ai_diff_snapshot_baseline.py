# -*- coding: utf-8 -*-
"""不可变 Diff 快照与增量基线（AI-P0-02）。

## 缺陷形态（本轮实测）

`last_analyzed_at` 一条时间水位线被当成三件事用：① 哪些缓存行算「新变化」
（`updated_at > last_analyzed_at`）② 模型有没有完整读过上一份快照 ③ 下一次是首次
全量还是增量（`_decide_scope` 的 `first_run` 支）。

降级运行刻意不推它是对的（94% 的文件没取到证据，标成「已读」是撒谎），代价是 ③
跟着一起废掉：手工路径看到 `last_analyzed_at is None`，于是**每一次**都按 `first_run`
把约 1000 个文件全放进白名单。实测 Run 20 相对 Run 15 只有 **45 个路径身份变化**，
却分析了 **1009 个文件**。

## 这一组守什么（复测文档 :161-166 的四条验收 + 指针推进的三路）

1. 降级之后再有 5 个文件变化，增量输入**只含这 5 个**加必要依赖与**明确列出的补偿项**；
2. 没有新变化时**复用结论，不创建付费运行**；
3. 全量模式**始终对目标快照建完整任务**；
4. **重建缓存但内容指纹不变，不算新变化**。

## 变红意味着什么

* `test_the_snapshot_digest_*`：快照的内容指纹不再与
  `scope_sampling.weekly_snapshot_digest` 逐字相同。那个值进了
  `AiAnalysisRun.active_key`，换了算法会让**历史活动运行突然不再匹配**、
  同一次输入被放行第二次 —— 代价是静默的。
* `test_a_degraded_run_*`：降级不再留下可用的基线，于是那条「每次手工分析都做全量」
  的循环就回来了（这正是本次要修的那件事）。
* `test_no_change_*`：没有新变化却仍然建了一条 run —— 白花一次真金白银的调用。
* `test_rebuilding_the_cache_*`：把「写库时刻」当成了「内容变了」，同步每 2~3 分钟
  跑一遍，于是每次都会判成有新变化。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import (
    AiAnalysisRun,
    AiAnalysisTrace,
    AiDiffSnapshot,
    AiDiffSnapshotItem,
    AiWeeklyAnalysisState,
)
from services.ai import snapshot_store
from services.ai_analysis_service import update_project_analysis_config
from services.ai.coverage_ledger import build_ledger
from services.ai.scope_sampling import (
    _decide_scope,
    _summarize_weekly_files,
    weekly_snapshot_digest,
)

# 窗口里的文件名刻意避开平台默认的关键路径模式（`config/`、`.sql$` …）：这一组量的是
# 增量基线，命中关键路径会把范围判定升级为全量，把变量搅在一起。
_PATH = "tables/table_{index}.xlsx"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_group(*, file_count: int = 200, commit_prefix: str = "c") -> dict:
    """一个周版本分组：项目 + 仓库（配表）+ 配置 + `file_count` 行缓存。"""
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
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
    db.session.flush()
    commits = {}
    for index in range(file_count):
        path = _PATH.format(index=index)
        commit = f"{commit_prefix}{index:04d}"
        commits[path] = commit
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg.id,
                repository_id=repo.id,
                file_path=path,
                file_type="excel",
                merged_diff_data="{}",
                base_commit_id="b" * 40,
                latest_commit_id=commit,
                commit_count=1,
                cache_status="completed",
                diff_version="1.18.0",
            )
        )
    db.session.commit()
    # **显式打开自动分析开关。** 默认值是**关**（2026-09-22 起），而本文件测的是
    # 增量基线（复用结论 / 快照指针）—— 前提是这次分析本来会跑起来。不声明这个前提，
    # 这些用例会全部停在「开关关闭」那道更靠前的闸后面，报出来的失败看起来像
    # 「没复用结论」，而真正的原因与基线无关。
    # 走 `update_project_analysis_config`（用户点开关走的就是这条），不 monkeypatch。
    ok, message, errors = update_project_analysis_config(
        project.id, {"auto_weekly_enabled": True}, updated_by="tester"
    )
    assert ok, f"开关没打开，这组用例的前提就不成立：{message} {errors}"
    return {"project": project, "repo": repo, "cfg": cfg, "commits": commits}


def _paths(group, indices) -> list:
    return [_PATH.format(index=index) for index in indices]


def _add_code_repo(group, *, file_count: int = 10) -> dict:
    """同一个分组里再加一个**代码仓库**的配置（分组按 project_id + 时间窗划）。

    用来量「`focus` 筛掉的那部分不能在变更清单与补偿清单两边只消失一边」——
    `_filter_delta_files_by_focus` 只作用于清单，补偿项必须跟着一起筛。
    """
    repo = Repository(
        project_id=group["project"].id,
        name=_uid("code"),
        type="git",
        url=f"https://example.com/{_uid('c')}.git",
        branch="main",
        resource_type="code",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=group["project"].id,
        repository_id=repo.id,
        name=group["cfg"].name,
        branch="main",
        # 与主配置同一时间窗**且同一个版本名** → 落进同一个分组（判据见
        # `project_config_source.weekly_batch_configs`）。
        start_time=group["cfg"].start_time,
        end_time=group["cfg"].end_time,
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    commits = {}
    for index in range(file_count):
        path = f"code/mod_{index}.lua"
        commit = f"e{index:04d}"
        commits[path] = commit
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg.id,
                repository_id=repo.id,
                file_path=path,
                file_type="code",
                merged_diff_data="",
                base_commit_id="b" * 40,
                latest_commit_id=commit,
                commit_count=1,
                cache_status="completed",
                diff_version="1.18.0",
            )
        )
    db.session.commit()
    group["commits"].update(commits)
    return {"repo": repo, "cfg": cfg, "commits": commits}


def _change_files(group, indices, *, commit_prefix: str = "d") -> None:
    """这些文件有了新提交 —— 这是**内容身份**变了（不是 `updated_at` 变了）。"""
    for position, index in enumerate(indices):
        row = WeeklyVersionDiffCache.query.filter_by(
            config_id=group["cfg"].id, file_path=_PATH.format(index=index)
        ).one()
        row.latest_commit_id = f"{commit_prefix}{index:04d}"
        group["commits"][row.file_path] = row.latest_commit_id
    db.session.commit()


def _settle_run(run, *, engine_status: str) -> None:
    """把这条 run 摆成「已经跑完」的样子（不真的调模型）。"""
    if engine_status == "succeeded":
        run.status = "succeeded"
    elif engine_status == "degraded":
        run.status = "degraded"
    else:
        run.status = "failed"
    run.conclusion_structured = None if engine_status == "failed" else True
    run.active_key = None
    if engine_status == "failed":
        run.response_payload = None
        run.response_text = ""
    else:
        run.response_text = "报告正文"
        run.response_payload = "{}"
    run.finished_at = datetime.now(timezone.utc)
    db.session.commit()


def _run_once(
    group,
    *,
    engine_status: str = "succeeded",
    force_full: bool = False,
    evidence=None,
) -> dict:
    """走一遍**生产路径**：建 payload → 建 run（冻结目标快照）→ 落终态 → 推进指针。

    `evidence` 传一组路径时，先给这条 run 落一份「这些文件取到过证据」的逐轮明细 ——
    覆盖门槛读的就是它（`services/ai/coverage_ledger.ledger_from_run`）。
    """
    cfg = group["cfg"]
    payload, state, skip = ai_service.build_weekly_payload(cfg.id, force_full=force_full)
    assert skip is None, f"这次 payload 没建出来：{skip}"
    run = ai_service._create_run(
        project_id=group["project"].id,
        target_type="weekly",
        target_id=cfg.id,
        target_key=payload["group"]["key"],
        response_mode="blocking",
        scope=payload["scope"],
        trigger_source="manual",
        payload=payload,
    )
    _settle_run(run, engine_status=engine_status)
    if evidence:
        _record_evidence(run, evidence, group["commits"])
    ai_service._update_weekly_state(payload, run, state, engine_status=engine_status)
    return {"run": run, "payload": payload}


def _record_evidence(run, paths, commits) -> None:
    """给这条 run 落一份逐轮明细：这些文件**取到过证据**。"""
    details = [
        {
            "kind": "file_diff",
            "label": f"file_diff {str(commits.get(path, ''))[:12]} {path}",
            "failed": False,
            "empty": False,
        }
        for path in paths
    ]
    db.session.add(
        AiAnalysisTrace(
            run_id=run.id,
            round_index=1,
            executed_json=json.dumps({"items": len(details), "details": details}),
        )
    )
    db.session.commit()


def _group_state(group) -> AiWeeklyAnalysisState:
    return AiWeeklyAnalysisState.query.filter_by(
        group_key=ai_service.build_weekly_group_key(group["cfg"])
    ).one()


def _run_count(group) -> int:
    """按**项目**过滤 —— 测试库是会话级共用的，全表 count 会「全量绿、单跑红」。"""
    return AiAnalysisRun.query.filter_by(project_id=group["project"].id).count()


# ===========================================================================
#  一、快照本身的读写与做差
# ===========================================================================


def test_the_snapshot_digest_is_byte_identical_to_the_weekly_snapshot_digest():
    """**这一条是整套东西的地基。**

    快照的 `content_digest` 必须与 `scope_sampling.weekly_snapshot_digest` 逐字相同：
    那个值进了 `AiAnalysisRun.active_key`（「同一目标 + 同一份输入」的唯一认领），
    换一套算法会让**所有历史活动运行突然不再匹配**，同一次输入被放行第二次。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=7)
        cfg_id = group["cfg"].id

        snapshot = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-digest", project_id=group["project"].id
        )
        db.session.commit()

        assert snapshot is not None
        assert snapshot.content_digest == weekly_snapshot_digest([cfg_id])
        assert snapshot.item_count == 7
        assert snapshot.is_sealed


def test_the_snapshot_digest_follows_the_content_not_the_write_time():
    """`updated_at` 是「什么时候写的」，同步每 2~3 分钟跑一遍 —— 把它算进去等于没有判据。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=3)
        cfg_id = group["cfg"].id
        group_key = "g-content"

        first = snapshot_store.seal_snapshot(
            [cfg_id], group_key=group_key, project_id=group["project"].id
        )
        # 只把写库时刻推高（同步重算了一遍、内容一字未变）
        for row in WeeklyVersionDiffCache.query.filter_by(config_id=cfg_id).all():
            row.updated_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.session.commit()

        second = snapshot_store.seal_snapshot(
            [cfg_id], group_key=group_key, project_id=group["project"].id
        )
        assert second.id == first.id, "重建缓存（内容未变）却又建了一份快照"
        assert snapshot_store.latest_sealed_snapshot(group_key).id == first.id


def test_sealing_the_same_manifest_twice_reuses_the_snapshot():
    with app.app_context():
        create_tables()
        group = _make_group(file_count=5)
        cfg_id = group["cfg"].id

        first = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-dedup", project_id=group["project"].id
        )
        db.session.commit()
        before = AiDiffSnapshotItem.query.filter_by(snapshot_id=first.id).count()

        second = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-dedup", project_id=group["project"].id
        )
        db.session.commit()

        assert second.id == first.id
        assert AiDiffSnapshotItem.query.filter_by(snapshot_id=first.id).count() == before
        assert AiDiffSnapshot.query.filter_by(group_key="g-dedup").count() == 1


def test_diff_snapshots_splits_added_changed_and_unchanged():
    with app.app_context():
        create_tables()
        group = _make_group(file_count=4)
        cfg_id = group["cfg"].id
        project_id = group["project"].id

        base = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-diff", project_id=project_id
        )
        db.session.commit()

        # 一个新增、一个换了最新提交、两个原样
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg_id,
                repository_id=group["repo"].id,
                file_path=_PATH.format(index=99),
                file_type="excel",
                base_commit_id="b" * 40,
                latest_commit_id="e999",
                commit_count=1,
                cache_status="completed",
                diff_version="1.18.0",
            )
        )
        row = WeeklyVersionDiffCache.query.filter_by(
            config_id=cfg_id, file_path=_PATH.format(index=1)
        ).one()
        row.latest_commit_id = "e111"
        db.session.commit()

        target = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-diff", project_id=project_id
        )
        db.session.commit()

        added, changed, unchanged = snapshot_store.diff_snapshots(base, target)
        assert set(added) == {(cfg_id, _PATH.format(index=99))}
        assert set(changed) == {(cfg_id, _PATH.format(index=1))}
        assert set(unchanged) == {
            (cfg_id, _PATH.format(index=0)),
            (cfg_id, _PATH.format(index=2)),
            (cfg_id, _PATH.format(index=3)),
        }


def test_a_backdated_commit_inside_the_window_is_a_change_too():
    """**回填的旧日期提交那一类漏。**

    提交是按 `commit_time` 定序挑 base/latest 的，而自动导表那类工具回填的提交可能
    **日期早于窗口里已有的提交、推送却在之后**：它落在窗口中间，`base`/`latest`/
    `diff_version` 一个都不动，可合并 diff（窗口内该文件的提交按序合起来）已经变了、
    缓存行也重写了（`weekly_file_sync.weekly_cache_is_unchanged` 就是把 `commit_count`
    算作内容变化的）。做差少了这一项，判据说「没变」；而基准每轮都往前推 ——
    这一处改动**永远**补不回来，报告里一个字都不会提。

    只把两行的条数加一、其余一字不动：判据退回三元组时这条必须红。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=6)
        cfg_id = group["cfg"].id

        first = _run_once(group)
        assert first["payload"]["scope"] == "full", "首跑不是全量，后面的增量不成立"

        touched = set(_paths(group, [1, 2]))
        for row in WeeklyVersionDiffCache.query.filter_by(config_id=cfg_id).all():
            if row.file_path in touched:
                row.commit_count = (row.commit_count or 0) + 1
        db.session.commit()

        payload, _state, skip = ai_service.build_weekly_payload(cfg_id)
        assert skip is None, f"这次 payload 没建出来：{skip}"
        delta = {item["file_path"] for item in payload["delta_files"]}
        assert touched <= delta, (
            "窗口里多了一条提交（base/latest 都没动）却没进增量输入 —— "
            f"这些文件再也不会被重看：{sorted(touched - delta)}"
        )

        # 反向：没被碰过的那几行不许跟着一起进来。
        untouched = set(_paths(group, [3, 4, 5]))
        assert not (untouched & delta), (
            f"没变化的文件被算成了变化：{sorted(untouched & delta)}"
        )


def test_a_new_diff_version_is_a_change_even_without_any_new_commit():
    """比较口径变了（`DIFF_LOGIC_VERSION`）就必须重看，哪怕 commit 一个都没动。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=2)
        cfg_id = group["cfg"].id

        base = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-version", project_id=group["project"].id
        )
        db.session.commit()

        for row in WeeklyVersionDiffCache.query.filter_by(config_id=cfg_id).all():
            row.diff_version = "1.19.0"
        db.session.commit()

        target = snapshot_store.seal_snapshot(
            [cfg_id], group_key="g-version", project_id=group["project"].id
        )
        db.session.commit()

        added, changed, _unchanged = snapshot_store.diff_snapshots(base, target)
        assert not added
        assert len(changed) == 2, "口径变了却判成「没变化」—— 那批文件再也不会被重看"


def test_covers_completely_refuses_to_treat_unknown_coverage_as_complete():
    """**拿不到覆盖数据时返回 False。** 把未知当完整，就是替平台声称「已完整检查」。"""
    with app.app_context():
        create_tables()
        assert snapshot_store.covers_completely(ledger=None) is False

        # 采集过明细、但一个文件都没取到 → 未知的是「比例」而不是「一定完整」
        payload = {
            "mode": "weekly",
            "scope": "full",
            "summary": {"batch_files": 10, "window_files": 10},
            "delta_files": [
                {"file_path": f"tables/t{index}.xlsx", "latest_commit_id": "c1"}
                for index in range(10)
            ],
        }
        ledger = build_ledger(request_payload=payload, executed=None, tool_stats=None)
        assert ledger["evidence_coverage"]["collected"] is False
        assert (
            snapshot_store.covers_completely(ledger=ledger, snapshot_files=10) is False
        )


def test_covers_completely_needs_both_the_whole_snapshot_and_the_evidence():
    """门槛两条都要：输入装下了整份快照，且其中取到证据的比例达标。"""
    with app.app_context():
        create_tables()
        paths = [f"tables/t{index}.xlsx" for index in range(10)]
        payload = {
            "mode": "weekly",
            "scope": "full",
            "summary": {"batch_files": 10, "window_files": 10},
            "delta_files": [
                {"file_path": path, "latest_commit_id": "c1"} for path in paths
            ],
        }
        executed = [
            {"kind": "file_diff", "label": f"file_diff c1 {path}", "failed": False}
            for path in paths[:9]
        ]
        ledger = build_ledger(
            request_payload=payload, executed=executed, tool_stats={"file_diff": {}}
        )
        # 9/10 = 0.9，正好压线（默认阈值 0.9）
        assert snapshot_store.covers_completely(ledger=ledger, snapshot_files=10) is True
        # 同一个账本，阈值抬到 0.95 就不达标
        assert (
            snapshot_store.covers_completely(
                ledger=ledger, snapshot_files=10, threshold=0.95
            )
            is False
        )
        # 这次输入只装了整份快照的一部分（增量）→ 不许说「已完整检查」
        assert (
            snapshot_store.covers_completely(ledger=ledger, snapshot_files=100) is False
        )


# ===========================================================================
#  二、四条验收
# ===========================================================================


def test_a_degraded_baseline_keeps_the_incremental_input_to_the_changed_files():
    """**验收 1**：最近运行为 degraded 时，再有 5 个文件变化，默认增量输入只包含
    这 5 个文件 + 明确列出的补偿项。

    实测 Run 20 就是这一条的反面：水位线为空 → `first_run` → 1009 个文件全进白名单，
    而真正变化的是 45 个。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=200)
        cfg_id = group["cfg"].id

        # 第一轮：首跑（没有基线）→ 全量，白名单是整份快照。
        first = _run_once(group, engine_status="degraded")
        assert first["payload"]["scope"] == "full"
        snapshot_id = first["payload"]["snapshot"]["snapshot_id"]

        # 这一轮只对 190 个文件取到了证据，另外 10 个没取到 —— 它们要进补偿集。
        uncovered = _paths(group, range(190, 200))
        covered = [path for path in group["commits"] if path not in set(uncovered)]
        _record_evidence(first["run"], covered, group["commits"])

        state = _group_state(group)
        assert state.last_analyzed_at is None, "降级的 run 推了时间水位线"
        assert state.last_concluded_run_id == first["run"].id, "降级没留下结论基线"

        # 再来 5 个文件的变化。
        _change_files(group, [1, 2, 3, 4, 5])
        delta_expected = set(_paths(group, [1, 2, 3, 4, 5]))

        payload, _state, skip = ai_service.build_weekly_payload(cfg_id)
        assert skip is None
        assert payload["scope"] == "incremental", payload["policy"]["reason"]
        assert payload["baseline"]["snapshot_id"] == snapshot_id

        delta = {item["file_path"] for item in payload["delta_files"]}
        compensation = {item["file_path"] for item in payload["compensation_files"]}
        assert delta == delta_expected | compensation
        assert delta_expected <= delta, "这 5 个变化的文件没进增量输入"
        assert compensation == set(uncovered), (
            "补偿项不是「上一轮未取到证据的那些」—— 它们必须**明确列出来**，"
            f"而不是悄悄混进变更清单：{sorted(compensation)}"
        )
        assert payload["summary"]["compensation_files"] == len(uncovered)
        assert payload["summary"]["delta_files"] == len(delta)
        # 整份窗口还是 200 —— 增量说的是「这次装进输入的」，不是「版本变小了」
        assert payload["summary"]["window_files"] == 200


def test_an_unknown_coverage_does_not_invent_a_compensation_set():
    """反向自检：上一轮没有留下逐轮明细时，「哪些没看到」是**未知**，不是「全都没看到」。

    未知就报成全未覆盖，补偿集会把整份窗口再喂一遍 —— 正是这次要修的那件事，
    只是换了个名字。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=30)
        cfg_id = group["cfg"].id

        _run_once(group, engine_status="succeeded")
        _change_files(group, [7])

        payload, _state, skip = ai_service.build_weekly_payload(cfg_id)
        assert skip is None
        assert payload["compensation_files"] == []
        assert {item["file_path"] for item in payload["delta_files"]} == set(
            _paths(group, [7])
        )


def test_no_change_reuses_the_conclusion_without_creating_a_run():
    """**验收 2**：没有新变化时增量模式直接复用结论，**不创建付费运行**。

    P0-01 之后「手工点分析」走的是 job 协议（`POST /jobs` → 任务 → 本入口），
    那条老的流式入口已经没有生产调用方了。判据因此从「回放了一条 `event: cached`」
    换成**这次调用的结局**：`reason == "no_change"`（有可复用的结论，一个模型请求都
    没发出去），而那条 job 侧按 `reused` 终态收口 —— 见
    `tests/test_ai_job_settlement_and_scope.py` 里 `settle_without_run` 那一组。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=40)
        cfg_id = group["cfg"].id

        first = _run_once(group, engine_status="degraded")
        before = _run_count(group)

        outcome = ai_service.run_weekly_analysis_background(cfg_id)

        assert _run_count(group) == before, "没有新变化却建了一条 run —— 白花一次调用"
        assert outcome == {
            "status": "skipped",
            "reason": "no_change",
            "reused_run_id": first["run"].id,
        }, (
            f"没有走「复用结论」那一支，用户会白等一次（或白花一次）：{outcome}"
        )
        # 复用的正是**结论基线**那一条
        assert _group_state(group).last_concluded_run_id == first["run"].id


def test_a_picked_focus_filters_the_compensation_list_too():
    """补偿项与变更清单是**同一份输入**：`focus` 筛掉的那部分不能在两边只消失一边。

    只筛清单的后果是 `summary.compensation_files` 说「补了 10 个」而白名单里一个都没有 ——
    报告里的输入账与模型实际拿到的输入对不上，而这类「两本账各说各话」正是本次要修的病。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=20)
        code = _add_code_repo(group, file_count=10)
        cfg_id = group["cfg"].id

        # 首跑：整份快照（20 张表 + 10 个代码文件）都进了输入。
        first = _run_once(group, engine_status="succeeded")
        assert len(first["payload"]["delta_files"]) == 30
        # 但只有表那 20 个取到了证据 → 10 个代码文件是补偿候选。
        _record_evidence(
            first["run"],
            [path for path in group["commits"] if path.startswith("tables/")],
            group["commits"],
        )
        _change_files(group, [4])
        assert code["cfg"].id  # 第二个仓库的配置确实在同一个分组里

        payload, _state, skip = ai_service.build_weekly_payload(cfg_id, focus="table")
        assert skip is None
        assert payload["focus"]["label"] == "仅配表仓库"
        assert payload["compensation_files"] == [], (
            "补偿项没跟着 focus 一起筛：报告说补了这些文件，而它们不在这次的白名单里"
        )
        assert payload["summary"]["compensation_files"] == 0
        assert [item["file_path"] for item in payload["delta_files"]] == _paths(group, [4])


def test_full_mode_always_builds_a_complete_task_on_the_target_snapshot():
    """**验收 3**：全量模式始终对**目标快照**建完整分析任务 —— 不做增量减法。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=60)
        cfg_id = group["cfg"].id

        # 先有一个带快照的增量态：只有 1 个文件变了。
        _run_once(group, engine_status="succeeded")
        _change_files(group, [3])

        payload, _state, skip = ai_service.build_weekly_payload(cfg_id, force_full=True)
        assert skip is None
        assert payload["scope"] == "full"
        assert len(payload["delta_files"]) == 60, "全量模式下的输入不是整份快照"

        run = ai_service._create_run(
            project_id=group["project"].id,
            target_type="weekly",
            target_id=cfg_id,
            target_key=payload["group"]["key"],
            response_mode="blocking",
            scope=payload["scope"],
            trigger_source="manual",
            payload=payload,
        )
        target_id = payload["snapshot"]["snapshot_id"]
        target = db.session.get(AiDiffSnapshot, target_id)
        assert target is not None and target.item_count == 60
        assert run.id  # 建出来了
        # 目标快照冻的是**这一次的清单**，与上一次那份不是同一个
        assert AiDiffSnapshot.query.filter_by(
            group_key=payload["group"]["key"]
        ).count() == 2


def test_rebuilding_the_cache_with_the_same_identity_is_not_a_change():
    """**验收 4**：重建缓存但内容指纹不变，不会被当作新变化。

    同步每 2~3 分钟跑一遍，任何一次重算都会顶高 `updated_at` —— 判据只能是内容身份。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=25)
        cfg_id = group["cfg"].id

        _run_once(group, engine_status="succeeded")
        before = weekly_snapshot_digest([cfg_id])
        rows = [
            (row.file_path, row.base_commit_id, row.latest_commit_id, row.diff_version)
            for row in WeeklyVersionDiffCache.query.filter_by(config_id=cfg_id).all()
        ]

        # 整批重建：行 id 变了、写库时刻变了，内容身份一字未变。
        WeeklyVersionDiffCache.query.filter_by(config_id=cfg_id).delete()
        db.session.commit()
        for path, base, latest, version in rows:
            db.session.add(
                WeeklyVersionDiffCache(
                    config_id=cfg_id,
                    repository_id=group["repo"].id,
                    file_path=path,
                    file_type="excel",
                    base_commit_id=base,
                    latest_commit_id=latest,
                    commit_count=1,
                    cache_status="completed",
                    diff_version=version,
                )
            )
        db.session.commit()

        assert weekly_snapshot_digest([cfg_id]) == before
        _payload, _state, skip = ai_service.build_weekly_payload(cfg_id)
        assert skip == "no_change", "重建缓存被判成了新变化 —— 每次同步都会重跑一遍全量"


# ===========================================================================
#  三、`last_analyzed_at` 一个值不能被当成三件事用
# ===========================================================================


def test_the_first_run_branch_is_decided_by_the_baseline_snapshot():
    """`_decide_scope` 的 `first_run` 支判的是**有没有可用的基线**，不是时间水位线。

    第二参数仍然「能判真假」（既有测试按那个形状调用），但它的含义变了：调用方给的是
    **做差基准**（`snapshot_store.BaselineSnapshot`），不是 `last_analyzed_at`。
    """
    summary = {"delta_files": 3, "total_files": 200, "critical_paths": False}
    assert _decide_scope(summary, None) == ("full", "first_run")

    baseline = snapshot_store.BaselineSnapshot(
        snapshot_id=1, content_digest="x" * 40, item_count=200, complete=False, items={}
    )
    assert _decide_scope(summary, baseline) == ("incremental", "delta_small")


def test_the_watermark_is_not_consulted_once_a_snapshot_exists():
    """快照一旦存在，时间水位线就**不再参与**做差。

    这是「一个值当三件事用」被拆开的物理位置：水位线只回答「最近一次完整分析的
    时刻」，做差看的是两份 manifest 的身份差。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=50)
        cfg_id = group["cfg"].id

        _run_once(group, engine_status="succeeded")
        state = _group_state(group)
        assert state.last_analyzed_at is not None
        # 把水位线推到未来：时间判据会认为「没有新变化」，内容判据不会。
        state.last_analyzed_at = datetime.now(timezone.utc) + timedelta(days=1)
        db.session.commit()

        _change_files(group, [11, 12])

        _summary, _details, skip = _summarize_weekly_files(
            [group["cfg"]], state.last_analyzed_at
        )
        assert skip == "no_change", "时间判据没在做差（这条对照组就失去意义了）"

        payload, _state, skip = ai_service.build_weekly_payload(cfg_id)
        assert skip is None
        assert {item["file_path"] for item in payload["delta_files"]} == set(
            _paths(group, [11, 12])
        ), "水位线被推高之后，内容身份的变化被时间判据吞掉了"


def test_a_degraded_run_advances_the_conclusion_pointer_but_not_the_coverage_pointer():
    """降级有结论（能当基线）但**不代表完整覆盖** —— 两个指针各归各的。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=12)
        done = _run_once(group, engine_status="degraded")
        state = _group_state(group)

        assert state.last_analyzed_at is None
        assert state.last_analysis_run_id is None
        assert state.last_concluded_run_id == done["run"].id
        assert state.last_complete_snapshot_id is None, "降级被说成了「已完整检查」"
        # 降级仍然留下内容指纹（否则同一份输入会被一遍遍重跑）
        assert state.last_snapshot_digest == weekly_snapshot_digest([group["cfg"].id])


def test_a_failed_run_advances_nothing():
    """失败 = 什么都没学到：时间水位线、结论基线、覆盖指针、指纹**一个都不推**。

    推了指纹的后果特别隐蔽：调度器下一轮看到「输入一字未变」直接跳过，于是这个版本
    在下一批提交到来之前**再也不会被分析** —— 而它其实一次都没跑成。
    """
    with app.app_context():
        create_tables()
        group = _make_group(file_count=8)
        state = AiWeeklyAnalysisState(
            project_id=group["project"].id,
            group_key=ai_service.build_weekly_group_key(group["cfg"]),
        )
        db.session.add(state)
        db.session.commit()

        _run_once(group, engine_status="failed")
        db.session.refresh(state)

        assert state.last_analyzed_at is None
        assert state.last_analysis_run_id is None
        assert state.last_concluded_run_id is None
        assert state.last_complete_snapshot_id is None
        assert not state.last_snapshot_digest, "失败的运行记下了「这份输入看过了」"


def test_a_clean_run_without_evidence_does_not_claim_complete_coverage():
    """跑完整了、整份快照也进了输入，但**没有留下取数明细** → 覆盖未知 → 不达标。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=10)
        done = _run_once(group, engine_status="succeeded")
        state = _group_state(group)

        assert state.last_analyzed_at is not None
        assert state.last_analysis_run_id == done["run"].id
        assert state.last_concluded_run_id == done["run"].id
        assert state.last_complete_snapshot_id is None, "把「未知」当成了「完整」"


def test_a_successful_run_advances_the_complete_snapshot_pointer_when_it_qualifies():
    """整份快照都进了输入、而且证据也取到了 → 才推进完整覆盖指针。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=10)
        done = _run_once(
            group, engine_status="succeeded", evidence=sorted(group["commits"])
        )
        state = _group_state(group)

        assert state.last_analyzed_at is not None
        assert state.last_analysis_run_id == done["run"].id
        assert (
            state.last_complete_snapshot_id
            == done["payload"]["snapshot"]["snapshot_id"]
        )


def test_the_complete_coverage_threshold_is_configurable(monkeypatch):
    """阈值可配置、有明确默认值；配置坏掉时回落默认值而不是判成「达标」。

    打的是**实现所在那个模块**的 `get_project_analysis_config`：
    `services/ai_analysis_service` 只是它的回导别名（本文件贴着长度闸门，实现搬去了
    `services/ai/baseline_blocks.py`）。打错地方这条会静默变成「默认值恒成立」。
    """
    import services.ai.baseline_blocks as baseline_blocks

    with app.app_context():
        create_tables()
        group = _make_group(file_count=10)
        payload = {"group": {"project_id": group["project"].id}}
        assert (
            ai_service._complete_coverage_ratio(payload)
            == snapshot_store.DEFAULT_COMPLETE_COVERAGE_RATIO
        )
        monkeypatch.setattr(
            baseline_blocks, "get_project_analysis_config",
            lambda *_a, **_k: {"complete_coverage_ratio": 0.5},
        )
        assert ai_service._complete_coverage_ratio(payload) == 0.5
        monkeypatch.setattr(
            baseline_blocks, "get_project_analysis_config",
            lambda *_a, **_k: {"complete_coverage_ratio": "坏值"},
        )
        assert (
            ai_service._complete_coverage_ratio(payload)
            == snapshot_store.DEFAULT_COMPLETE_COVERAGE_RATIO
        )
        # 0 与 1 之外的取值同样回落默认值 —— 不能把「阈值写成 0」当成「全都达标」
        monkeypatch.setattr(
            baseline_blocks, "get_project_analysis_config",
            lambda *_a, **_k: {"complete_coverage_ratio": 0},
        )
        assert (
            ai_service._complete_coverage_ratio(payload)
            == snapshot_store.DEFAULT_COMPLETE_COVERAGE_RATIO
        )


# ===========================================================================
#  四、项目删除时不许留下孤儿快照
# ===========================================================================


def test_deleting_a_project_clears_the_diff_snapshots(monkeypatch):
    """快照挂在项目上（非空外键），不清就是「项目永远删不掉」。"""
    import services.repository_admin_handlers as handlers

    monkeypatch.setattr(
        handlers, "delete_local_repository_directory", lambda *_a, **_k: None
    )
    with app.app_context():
        create_tables()
        group = _make_group(file_count=6)
        _run_once(group, engine_status="succeeded")
        project_id = group["project"].id
        snapshot_ids = [
            row.id
            for row in AiDiffSnapshot.query.filter_by(project_id=project_id).all()
        ]
        assert snapshot_ids

        with app.test_request_context(f"/projects/{project_id}/delete", method="POST"):
            assert handlers.delete_project.__wrapped__(project_id).status_code == 302

        assert db.session.get(Project, project_id) is None
        assert AiDiffSnapshot.query.filter_by(project_id=project_id).count() == 0
        assert (
            AiDiffSnapshotItem.query.filter(
                AiDiffSnapshotItem.snapshot_id.in_(snapshot_ids)
            ).count()
            == 0
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
