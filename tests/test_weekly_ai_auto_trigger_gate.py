# -*- coding: utf-8 -*-
"""「周版本自动分析」关掉之后，不许再自己跑起来。

## 缺陷形态（用户报的）

用户把「周版本自动分析」关掉、重启平台，然后点开 `/weekly-version-config/1/diff`
的「AI分析」，页面上仍然出现「AI 分析进行中...」。他的判断是「关了自动分析就不该
自动触发」—— 这个判断是对的。代码里有**三条**互不相干的路都能把分析跑起来：

1. **入队时查开关，执行时不查。** `schedule_weekly_ai_analysis_tasks` 建任务前查
   `auto_weekly_enabled`，但任务一旦入队就与开关无关了；而重启时 `load_pending_tasks`
   会把上次残留的 `processing` 任务改回 `pending` **重新入队**（`task_worker_service.py`
   的 `1273-1279`）。于是「关掉开关 + 重启」反而必定跑一次。
2. **重启留下幽灵 running 记录。** `_create_run` 先写 `status='running'` 再跑，进程
   被杀就永远停在那儿；而 `_is_run_fresh` 要求 `succeeded`，所以 `/latest` 看不见它
   —— 界面以为「这个版本从没分析过」。
3. **界面把「没有结果」当成「该跑一次」。** 三个模板里打开抽屉的回调原本都是
   `if (!hasLatest) startWeeklyAiAnalysis();` —— 一个纯查看动作会把分析跑起来。

三条都要修：只修第 3 条，重启后界面仍会误判（第 2 条）；只修第 1、2 条，点开抽屉
照样会跑（第 3 条）。用户看到的「进行中」正是第 3 条打印的那句话。

## `effective_status` 那条兜底为什么没救场

`AiAnalysisRun.effective_status` / `is_stale_running`（阈值 1 小时）早就写好了，但
**生产代码里从没被读过** —— 只被 `to_dict()` 用，而 `to_dict()` 没有任何调用者。
于是「僵尸 running 显示成 failed」形同虚设，幽灵记录永远不会自行退场。
本文件用「超过阈值的 running 不许再被报成进行中」把读取侧的口径钉住。
"""
from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timedelta, timezone

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
    "templates/commit_diff_new.html",
)


def _uid(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _setup_config(*, auto_weekly: bool | None = True):
    """一个可跑的项目 + 周版本配置；`auto_weekly=None` 表示**不建配置行**。"""
    project = Project(code=_uid("P"), name=_uid("ai-project"))
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id,
        name=_uid("code"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="code",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=f"W1 - {repo.name}",
        description="",
        branch="main",
        start_time=datetime(2026, 3, 1),
        end_time=datetime(2026, 3, 8),
        cycle_type="custom",
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    if auto_weekly is not None:
        ok, message, errors = ai_service.update_project_analysis_config(
            project.id, {"auto_weekly_enabled": auto_weekly}, updated_by="tester"
        )
        assert ok, f"配置没存进去：{message} {errors}"
    db.session.commit()
    return project, repo, cfg


def _add_run(project_id: int, cfg, *, status: str, age_seconds: int = 0) -> AiAnalysisRun:
    started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=cfg.id,
        target_key=ai_service.build_weekly_group_key(cfg),
        status=status,
        scope="full",
        trigger_source="scheduled",
        started_at=started,
        created_at=started,
    )
    db.session.add(run)
    db.session.commit()
    return run


# ==========================================================================
# 一、执行时再查一次开关（入队时查过不算数）
# ==========================================================================


def test_a_queued_task_does_not_run_after_the_switch_is_turned_off():
    """**核心回归**：开关关掉之后，已经排好队的后台任务也不许跑。

    线上形态就是「排队时开关还开着 → 用户关掉 → 平台重启 → 任务被重新入队并执行」。
    只在建任务那一侧查开关挡不住这条路径，所以判据必须放在执行入口。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config(auto_weekly=True)
        # 先关掉（模拟用户排完队之后才关）+ 重启后任务被重新入队
        ai_service.update_project_analysis_config(
            project.id, {"auto_weekly_enabled": False}, updated_by="tester"
        )
        db.session.commit()

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome == {"status": "skipped", "reason": "auto_weekly_disabled"}, (
            f"关掉开关后后台任务照样跑了：{outcome}"
        )
        # **必须按 project 过滤。** 测试库是整个会话共用的（`tests/conftest.py` 只守 IO，
        # 没有逐用例重置），而别的文件真跑过周版本分析就会留下 `target_type='weekly'` 的行。
        # 原先是不带过滤的全局 `count()`，于是「这条用例红不红」取决于它跟谁一起被选中：
        # 全量跑恰好被 `test_auth_e2e` 的 `drop_all()` 清干净而变绿，而只跑
        # `-k "engine or ai_"` 时那两个整库清空的文件被排除，污染留到这里 → 必红。
        # 按 project 过滤后，断言的仍然是本用例真正关心的事：这个项目没留下 run。
        assert (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", project_id=project.id
            ).count()
            == 0
        ), "跳过了却还是留下了一条 run 记录"


def test_the_gate_does_not_block_a_run_while_the_switch_is_on():
    """反向自检：闸不能把开着的情况一起挡掉。

    只断言「不是被那道闸挡的」—— 这里没有配接口密钥，所以后面自然会失败；
    这次要证明的是**原因不同**，否则上面那条用例可能只是因为别的原因返回。
    """
    with app.app_context():
        create_tables()
        _project, _repo, cfg = _setup_config(auto_weekly=True)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome.get("reason") != "auto_weekly_disabled", (
            f"开关开着却按「已关闭」跳过了：{outcome}"
        )


def test_a_project_without_a_config_row_keeps_the_documented_default():
    """没有配置行时按文档默认（开）走 —— 这条钉的是**现状口径**，不是价值判断。

    `FIELD_DEFAULTS["auto_weekly_enabled"] = True`，所以「从没配过」=「自动分析开着」。
    界面上这个开关只在项目总览页，写的是那个页面的 `project_id`；若用户关的是另一个
    项目，承载 config 的项目仍然是默认开。这里把它写成用例，是为了让这个默认值
    变成**有人看过、改起来会被发现**的东西，而不是埋在两层 `.get(..., True)` 里。
    """
    with app.app_context():
        create_tables()
        _project, _repo, cfg = _setup_config(auto_weekly=None)

        resolved = ai_service.get_project_analysis_config(cfg.project_id)

        assert resolved["configured"] is False
        assert resolved["auto_weekly_enabled"] is True
        outcome = ai_service.run_weekly_analysis_background(cfg.id)
        assert outcome.get("reason") != "auto_weekly_disabled"


# ==========================================================================
# 二、重启留下的 running 记录必须退场
# ==========================================================================


def test_a_run_interrupted_by_a_restart_is_marked_failed():
    """重启是「这些 run 已经死了」的**确定性证据**，启动时就要判掉。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _add_run(project.id, cfg, status="running")

        assert ai_service.fail_orphaned_analysis_runs() == 1

        db.session.refresh(run)
        assert run.status == "failed", "被重启中断的记录还是 running，界面会一直误判"
        assert run.finished_at is not None, "没写完成时间，界面上看不出它是什么时候停的"
        assert "重启" in (run.error_message or ""), "没说清是怎么失败的"
        assert run.response_text == "" and run.response_payload is None, (
            "失败的 run 不该留结论字段（同一份契约见 _persist_outcome）"
        )


def test_resolving_orphans_is_idempotent():
    """启动逻辑可能被重复触发，第二遍不能再报「处理了 1 条」。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        _add_run(project.id, cfg, status="running")

        assert ai_service.fail_orphaned_analysis_runs() == 1
        assert ai_service.fail_orphaned_analysis_runs() == 0


def test_only_running_records_are_touched():
    """**只动 running。** 成功与失败的记录是历史，不能被启动清理改写。

    这一条拦的是「清理写成 `status != 'succeeded'`」这类顺手扩大范围 ——
    那会把每次失败的原因（error_message）也一起抹掉，而那是排查的唯一线索。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        ok_run = _add_run(project.id, cfg, status="succeeded")
        ok_run.response_text = "结论"
        failed_run = _add_run(project.id, cfg, status="failed")
        failed_run.error_message = "模型超时"
        db.session.commit()

        assert ai_service.fail_orphaned_analysis_runs() == 0

        db.session.refresh(ok_run)
        db.session.refresh(failed_run)
        assert ok_run.status == "succeeded" and ok_run.response_text == "结论"
        assert failed_run.status == "failed" and failed_run.error_message == "模型超时"


def test_the_worker_resolves_orphans_before_requeuing_pending_tasks():
    """接线守卫：必须在 `load_pending_tasks()`（会把上次残留的 processing 改回
    pending 重新入队）**之前**清墓碑，否则新一轮分析会和幽灵记录混在一起。

    比位置前先剥掉 `#` 注释：本仓库的习惯是把相关符号写进解释性注释里，
    而 `index()` 会匹配到注释里的那个符号 —— 这条用例第一版就是被自己的注释
    绊倒的（注释里提到 `load_pending_tasks()` 的位置比真正的调用还靠前）。
    """
    from services import task_worker_service

    source = re.sub(r"#[^\n]*", " ", inspect.getsource(task_worker_service.start_background_task_worker))
    assert "fail_orphaned_analysis_runs()" in source, "启动时没有清理被中断的分析记录"
    assert "load_pending_tasks()" in source, "找不到重新入队的调用，断言失去参照"
    assert source.index("fail_orphaned_analysis_runs()") < source.index("load_pending_tasks()"), (
        "清理排在重新入队之后 —— 幽灵记录会与新一轮分析混在一起"
    )


# ==========================================================================
# 三、进行中的分析要如实报出去，不能报成「没有结果」
# ==========================================================================


def test_an_in_progress_run_is_reported_instead_of_hidden():
    """`/latest` 要能把「正在跑」说出来。

    报成「没有结果」的后果不只是少显示一条：界面拿到「没有结果」就会去开跑一次，
    于是同一次分析在用户眼里变成两次、页面上永远挂着「进行中」。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _add_run(project.id, cfg, status="running")

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result is not None, "正在进行的分析被报成了「没有结果」"
        assert result["status"] == "running"
        assert result["in_progress"] is True
        assert result["run_id"] == run.id
        assert not result["response_text"], "进行中的记录不该带出任何结论文本"


def test_an_orphan_stale_running_run_is_not_reported_as_in_progress():
    """超过僵尸阈值（1 小时）的 running 不许再说是「进行中」。

    读取侧这条口径是 `effective_status` 的兜底：启动清理管不到「进程活着但那次分析
    真卡死了」的情况，而界面不能因此永远显示「进行中」。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        _add_run(project.id, cfg, status="running", age_seconds=7200)

        assert ai_service.get_latest_weekly_result(cfg.id) is None, (
            "僵尸 running 仍被当成「进行中」，界面会永远转圈"
        )


# ==========================================================================
# 四、前端：打开抽屉不再替用户开跑
# ==========================================================================

def _strip_js_comments(src: str) -> str:
    """先把注释剥掉再断言。

    本仓库的注释风格是**把要禁掉的写法原样写进注释里**（这次的修复说明就是这样），
    不剥离就会把说明文字当成真实代码 —— 这个坑在本次会话里已经踩过一次。
    """
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _template_sources() -> dict:
    return {
        name: _strip_js_comments(
            open(os.path.join(PROJECT_ROOT, name), encoding="utf-8").read()
        )
        for name in TEMPLATES
    }


def test_no_template_starts_an_analysis_just_because_there_is_no_result():
    """**核心回归**：查完「最近一次结果」之后不许跟着一个开跑调用。

    要同时挡住两种写法，因为它们的区别只是语法糖：

        loadLatest().then(c => { if (!c) start(); });     // 本仓库最初那个 bug 的形状
        if (!hasLatest) { start(); }                       // 后来改成的形状

    所以判据不看某个具体的 `if`，而是看**加载器调用之后紧跟着什么**：只要
    「查结果」后面出现开跑调用，就是「把查看动作变成了开跑动作」。

    「没有结果」的判定里还包含「有一条被重启中断的 running 记录」，所以这个形状让
    「重启后点一次 AI分析」必定跑一次分析 —— 而用户只是想看一眼结果。
    """
    loaders = ("loadWeeklyAiLatest(", "loadLatestResult(", "refreshWeeklyAiLatest(")
    for name, src in _template_sources().items():
        for loader in loaders:
            start = 0
            while (found := src.find(loader, start)) != -1:
                start = found + len(loader)
                tail = src[start:start + 200]
                assert not re.search(r"start(?:WeeklyAi|Ai)?(?:Analysis)?\s*\(", tail), (
                    f"{name}: {loader} 之后紧接着一个开跑调用 —— "
                    f"「看一眼结果」会把分析跑起来：…{tail[:90]}…"
                )


def test_the_only_way_to_start_an_analysis_is_the_button():
    """开跑调用的每一个**调用点**都必须落在按钮的点击回调里。

    「加载器后面不许跟开跑」只管住了自动开跑最常见的落点；这一条从另一个方向兜底：
    把整份模板里 `start*Analysis(` 的调用点全找出来，逐个要求它前面不远处有
    `addEventListener('click'` —— 也就是**只可能由点击触发**。

    两个否定后视是为了跳过**函数声明**本身（`function startAnalysis(` /
    `async function startWeeklyAiAnalysis(`），它们当然不在点击回调里。
    """
    pattern = re.compile(
        r"(?<!function )(?<!async function )\b"
        r"(?:startWeeklyAiAnalysis|startAnalysis|startAiAnalysis)\("
    )
    checked = 0
    for name, src in _template_sources().items():
        for match in pattern.finditer(src):
            checked += 1
            head = src[max(0, match.start() - 600):match.start()]
            assert "addEventListener('click'" in head, (
                f"{name}: 有一处开跑调用不在按钮的点击回调里 —— 它会自己跑起来"
            )
    assert checked > 0, "一个开跑调用都没找到，断言失去参照（名称变了？）"


def test_every_template_reports_an_in_progress_run_as_such():
    """三个模板都要认 `running` / `in_progress`，并且说成「进行中」而不是「暂无结果」。"""
    for name, src in _template_sources().items():
        assert "in_progress" in src, (
            f"{name} 不认识「进行中」这个状态，会把它显示成「暂无分析结果。」"
        )
        branch = re.search(
            r"result\.status === 'running' \|\| result\.in_progress(.{0,400})", src, re.S
        )
        assert branch is not None, f"{name} 里找不到「进行中」的分支"
        assert "AI 分析进行中" in branch.group(1), (
            f"{name} 的「进行中」分支没有给出进行中的文案"
        )
        assert "暂无分析结果" not in branch.group(1), (
            f"{name} 的「进行中」分支落进了「暂无结果」的文案"
        )


def test_the_loader_returns_early_for_the_in_progress_state():
    """「进行中」的分支必须**先于**「有结果」那一支返回。

    否则一条 running 记录会顺着往下走：后端契约里 `in_progress` 的 `response_text`
    是空串，落到「有结果」分支就会被显示成「已有结果」而正文一片空白 ——
    比说错状态更难查（徽标是绿的，抽屉是空的）。
    """
    for name, src in _template_sources().items():
        running_at = src.index("result.status === 'running'")
        has_result = re.search(r"has_?cached\s*=\s*true", src, re.I)
        assert has_result is not None, f"{name} 里找不到「已有结果」那一支"
        assert running_at < has_result.start(), (
            f"{name} 的「进行中」分支排在「已有结果」之后，空正文会被当成已有结果"
        )
        between = src[running_at:has_result.start()]
        assert "return false" in between, (
            f"{name} 的「进行中」分支没有在「有结果」之前返回"
        )
        assert "setWeeklyAiReport" not in between and "setAiReport" not in between, (
            f"{name} 的「进行中」分支把空的 response_text 送去渲染了"
        )
