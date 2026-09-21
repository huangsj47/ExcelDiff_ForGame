"""快照指纹闸门：同一份输入不许被自动分析一遍又一遍。

## 缺陷形态（本轮实测出来的）

时间水位线 `AiWeeklyAnalysisState.last_analyzed_at` **只在跑完整了**的 run 上推进 ——
这是有意的：降级的 run（「上下文索取额度用尽，基于已有证据出结论」）如果把水位线推到当前
时刻，那批变更就被整体判成「已看过」，而模型其实没真读到（线上真出过 767 个文件里 748 个
没读到却被标成已分析）。

代价是那条循环：**降级跑完 → 水位线不动 → 下个周期又判「有新变化」→ 同一份输入再分析
一遍**。实测 run 9 的两个分片（S1/S3）都刚好用满 8 轮 / 40 次索取，整轮结局就是 degraded，
于是这份输入每过一个分析间隔就会被重新分析一次，而输入一字未变。

判据因此补一条**内容**判据：`(base_commit_id, latest_commit_id, diff_version)` 的指纹。
它没变 = 这一轮能看到的内容与上一轮逐字相同 = 重跑只会得到同样的结果。

## 变红意味着什么

- `test_the_digest_*`：指纹不再反映「分析的是什么」（例如误把 `updated_at` 也哈希进去，
  那它每轮同步都会变，闸门等于没有）。
- `test_a_degraded_run_*`：降级跑完不记指纹 —— 那条循环就回来了。
- `test_a_run_that_finished_cleanly_*`：跑完整了却不记指纹（不致命，但状态行会自相矛盾：
  水位线说「看过了」、指纹说「没看过」）。
- `test_the_gate_*`：老库的行没有这一列（NULL）时必须按「没分析过」走 —— 反了会把该跑的
  分析全跳过，那是比多跑一遍严重得多的错。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import AiAnalysisRun, AiWeeklyAnalysisState
from services.ai.scope_sampling import snapshot_already_analyzed, weekly_snapshot_digest


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _setup(*, latest_commit_id: str = "a" * 40):
    """一个周版本配置 + 一行缓存（指纹的唯一输入就是这行缓存）。"""
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=_uid("weekly"),
        branch="main",
        start_time=datetime.now(timezone.utc) - timedelta(days=3),
        end_time=datetime.now(timezone.utc) + timedelta(days=3),
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    cache = WeeklyVersionDiffCache(
        config_id=cfg.id,
        repository_id=repo.id,
        file_path=f"code/{_uid('f')}.lua",
        merged_diff_data="{}",
        base_commit_id="b" * 40,
        latest_commit_id=latest_commit_id,
        commit_count=1,
        cache_status="completed",
        diff_version="1.18.0",
    )
    db.session.add(cache)
    db.session.commit()
    return project, repo, cfg, cache


def test_the_digest_is_stable_for_the_same_input():
    with app.app_context():
        create_tables()
        _project, _repo, cfg, _cache = _setup()
        first = weekly_snapshot_digest([cfg.id])
        assert first and len(first) == 40
        assert weekly_snapshot_digest([cfg.id]) == first, "同一份输入两次算出不同指纹"


def test_the_digest_changes_when_only_the_latest_commit_changes():
    """**核心**：指纹必须跟着「分析能看到什么」走。

    只看文件清单会漏掉「同一个文件又改了一版」，只看 `updated_at` 则会把「同一时刻重写了
    一遍」也算成变化（那正是修 `1ad3709` 之前每轮同步都顶高 `updated_at` 的病）。
    """
    with app.app_context():
        create_tables()
        _project, _repo, cfg, cache = _setup()
        before = weekly_snapshot_digest([cfg.id])

        cache.latest_commit_id = "c" * 40
        db.session.commit()

        assert weekly_snapshot_digest([cfg.id]) != before, "同文件换了提交，指纹却没变"


def test_the_digest_is_empty_without_any_cache_rows():
    with app.app_context():
        create_tables()
        _project, _repo, cfg, cache = _setup()
        db.session.delete(cache)
        db.session.commit()
        assert weekly_snapshot_digest([cfg.id]) == ""
        assert weekly_snapshot_digest([]) == ""


def test_the_gate_treats_a_missing_digest_as_not_analyzed():
    """老库的行没有这一列（NULL）。判不了就必须按「没分析过」走 —— 反了会把该跑的
    分析全部静默跳过，比多跑一遍严重得多。"""
    with app.app_context():
        create_tables()
        _project, _repo, cfg, _cache = _setup()
        state = AiWeeklyAnalysisState(group_key=_uid("g"), project_id=None)

        assert snapshot_already_analyzed(None, [cfg.id]) is False
        assert snapshot_already_analyzed(state, [cfg.id]) is False, "没有指纹却说分析过了"


def test_the_gate_matches_only_the_same_snapshot():
    with app.app_context():
        create_tables()
        _project, _repo, cfg, cache = _setup()
        state = AiWeeklyAnalysisState(group_key=_uid("g"), project_id=None)
        state.last_snapshot_digest = weekly_snapshot_digest([cfg.id])

        assert snapshot_already_analyzed(state, [cfg.id]) is True

        cache.latest_commit_id = "d" * 40
        db.session.commit()
        assert snapshot_already_analyzed(state, [cfg.id]) is False, "换了输入还说分析过了"


def test_a_degraded_run_records_the_digest_without_moving_the_watermark():
    """**这条循环的成因与解药在同一处**：降级不推水位线（有意），但必须留下指纹。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg, _cache = _setup()
        run = AiAnalysisRun(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            status="succeeded",
            scope="full",
            trigger_source="scheduled",
        )
        db.session.add(run)
        db.session.commit()

        state = AiWeeklyAnalysisState(
            group_key=ai_service.build_weekly_group_key(cfg), project_id=project.id,
        )
        db.session.add(state)
        db.session.commit()

        ai_service._update_weekly_state(
            {"group": {"key": state.group_key, "project_id": project.id, "config_ids": [cfg.id]},
             "summary": {"delta_files": 1, "total_files": 1}},
            run,
            state,
            engine_status="degraded",
        )

        db.session.refresh(state)
        assert state.last_analyzed_at is None, "降级的 run 推了时间水位线"
        assert state.last_snapshot_digest == weekly_snapshot_digest([cfg.id])
        assert snapshot_already_analyzed(state, [cfg.id]) is True


def test_a_run_that_finished_cleanly_records_the_digest_too():
    """反向自检：跑完整了的那条路径同样要记指纹，否则状态行自相矛盾。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg, _cache = _setup()
        run = AiAnalysisRun(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            status="succeeded",
            scope="full",
            trigger_source="scheduled",
        )
        db.session.add(run)
        db.session.commit()

        ai_service._update_weekly_state(
            {"group": {"key": ai_service.build_weekly_group_key(cfg), "project_id": project.id,
                       "config_ids": [cfg.id]},
             "summary": {"delta_files": 1, "total_files": 1}},
            run,
            None,
            engine_status="succeeded",
        )

        state = AiWeeklyAnalysisState.query.filter_by(
            group_key=ai_service.build_weekly_group_key(cfg)
        ).first()
        assert state is not None
        assert state.last_analyzed_at is not None
        assert state.last_snapshot_digest == weekly_snapshot_digest([cfg.id])


# ===========================================================================
#  闸门在调度器里的位置与节流
#
#  这一处用**结构断言**（与 `tests/test_ai_weekly_sync_gate.py` 里同步闸门那条同款，
#  理由也相同）：`schedule_weekly_ai_analysis_tasks` 要的上下文（app、活跃配置、分组、
#  时间水位线、后台任务表）与这条性质无关，构造出来的用例只会验证构造本身。
#  同样按规矩**先剥注释再断言**：注释里会原样引用要断言的写法。
# ===========================================================================
def _strip_py_comments(source: str) -> str:
    import io
    import tokenize

    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        if start_row == end_row:
            line = lines[start_row - 1]
            lines[start_row - 1] = line[:start_col] + line[end_col:]
    return "\n".join(lines)


def _scheduler_body() -> str:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "services" / "task_worker_service.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def schedule_weekly_ai_analysis_tasks"):]
    return _strip_py_comments(body[: body.index("def schedule_repository_sync_tasks")])


def test_the_gate_sits_before_the_task_is_created():
    """闸门必须排在**建任务之前** —— 否则每轮都会产生一个注定被跳过的后台任务，
    任务列表里堆一串 skipped 记录（预算闸门当初就是这么改的）。
    """
    body = _scheduler_body()

    assert "snapshot_already_analyzed(" in body, "调度器没有查指纹"
    assert body.index("snapshot_already_analyzed(") < body.index("create_weekly_ai_analysis_task("), (
        "指纹闸门排在建任务之后 —— 会产生一个注定被跳过的后台任务"
    )


def test_the_skip_is_throttled_so_it_does_not_log_every_minute():
    """跳过时要**推进触发水位线**：这个条件是持久的（输入没变可能一整天），
    而调度器是 `every(1).minutes` —— 不推进就是每分钟一条同样的日志 + 每分钟算一次指纹。

    与同步闸门相反（那条**不推**水位线是故意的：同步跑完就好了，下个周期必须重试）。
    """
    body = _scheduler_body()
    gate_at = body.index("snapshot_already_analyzed(")
    create_at = body.index("create_weekly_ai_analysis_task(")
    window = body[gate_at:create_at]

    assert "continue" in window, "查到输入没变却没有跳过"
    assert "last_triggered_at" in window, (
        "跳过时不推进触发水位线 —— 会每分钟重复记一遍、算一次指纹"
    )
