# -*- coding: utf-8 -*-
"""跑的过程中的预算提示：进度快照 + 「现在就已经超了」的判定。

## 这个文件守的四条性质

1. **跑得动的时候能让界面看见进度。** 分析入口是「生成器里跑一次阻塞调用」，
   `_execute_analysis` 返回之前一个事件都 yield 不出去；所以进度走**进程内快照 + 界面
   轮询**（`services/ai/run_progress.py`）。这里钉的是「那根线真的接上了」——
   用的手法与 `test_a_real_run_calls_the_model_and_persists_the_findings` 同一套：
   假 client 替掉真实 HTTP，看漏斗里到底发生了什么。
2. **跑完一定要清掉。** 不清的后果是界面一直显示「正在跑」（进程里没有第二个清理时机）。
3. **加上尚未落库的用量之后的判定，与闸门逐字同一套判据**（下界超了才算超、费用算不出
   就不判）。`with_live_usage` 是纯函数，所以这条能直接测，不用起数据库。
4. **读不到进度不是 0。** 多进程部署、跑在别的 worker 里、进程刚重启 —— 这些情况下
   快照是 `None`，界面必须显示「进度不可用」，而不是「第 0 轮 / 已用 0」。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
from app import app as flask_app
from app import create_tables, db
from models.ai_analysis import AiAnalysisRun, AiPlatformBudget
from services.ai import run_progress
from services.ai.analysis_budget import with_live_usage

# ==========================================================================
# 一、with_live_usage：纯函数，判据必须与闸门一致
# ==========================================================================


def _status(*, token_limit=1000, cost_limit="10.00", used_tokens=900, used_cost="1.00",
            platform=None, limited=True):
    """造一份与 `budget_status` 同形的判定结果。"""
    def _node(scope, limit_t, limit_c, used_t, used_c):
        return {
            "scope": scope,
            "limited": limited,
            "over": False,
            "blocks_analysis": False,
            "checked": True,
            "period": "monthly",
            "period_label": "本月",
            "since": None,
            "until": None,
            "limits": {"tokens": limit_t, "cost": limit_c, "currency": "CNY"},
            "used": {"tokens": used_t, "cost": used_c, "currency": "CNY", "runs": 1},
            "ratios": {"tokens": None, "cost": None},
            "over_limits": [],
            "reason": "",
            "notes": [],
            "over_scopes": [],
        }

    node = _node("project", token_limit, cost_limit, used_tokens, used_cost)
    node["platform"] = platform if platform is not None else _node(
        "platform", None, None, None, None
    )
    node["platform"]["limited"] = platform is not None
    return node


def test_live_tokens_can_push_a_project_over_its_limit():
    base = _status(token_limit=1000, used_tokens=900)

    result = with_live_usage(base, tokens=200)

    assert result["used"]["tokens"] == 1100
    assert result["over"] is True
    assert result["over_scopes"] == ["project"]
    assert "含本次运行" in result["reason"], result["reason"]
    assert result["live_included"]["tokens"] == 200
    # **必须写明这个数还没落库**：它和「已用量」不是一个口径，不说的话读的人会以为
    # 面板上的数字与这里对不上是 bug。
    assert "尚未落库" in result["live_included"]["note"], result["live_included"]


def test_live_tokens_under_the_limit_change_nothing_that_matters():
    base = _status(token_limit=1000, used_tokens=100)

    result = with_live_usage(base, tokens=200)

    assert result["used"]["tokens"] == 300, "已用量要如实加上，界面要显示它"
    assert result["over"] is False
    assert result["over_scopes"] == []
    assert result["reason"] == ""


def test_live_tokens_also_count_toward_the_platform_scope():
    """两档都要加：一次运行同时消耗项目额度与平台额度。"""
    platform = {"scope": "platform", "limited": True, "over": False, "blocks_analysis": False,
                "checked": True, "period": "monthly", "period_label": "本月",
                "since": None, "until": None,
                "limits": {"tokens": 1000, "cost": None, "currency": "CNY"},
                "used": {"tokens": 950, "cost": None, "currency": "CNY", "runs": 3},
                "ratios": {"tokens": None, "cost": None}, "over_limits": [], "reason": "",
                "notes": []}
    base = _status(token_limit=None, cost_limit=None, used_tokens=None, used_cost=None,
                   platform=platform)
    base["limited"] = True

    result = with_live_usage(base, tokens=100)

    assert result["platform"]["used"]["tokens"] == 1050
    assert result["over"] is True
    assert result["over_scopes"] == ["platform"]
    assert "平台" in result["reason"], result["reason"]


def test_an_uncomputable_live_cost_is_not_judged():
    """**算不出就不判**（与闸门同一条纪律）。

    宁可不说「超了」，也不给一个基于偏小金额的假超支。反向自检：同一份状态在金额
    算得出时**必须**判超。
    """
    base = _status(cost_limit="1.00", used_cost="0.90", token_limit=None)

    no_cost = with_live_usage(base, tokens=10, cost=None)
    assert no_cost["over"] is False, "金额算不出时不许判费用那一档"

    with_cost = with_live_usage(base, tokens=10, cost=Decimal("0.50"))
    assert with_cost["over"] is True
    assert with_cost["used"]["cost"] == "1.40"
    assert with_cost["over_limits"] == ["cost"]


def test_live_usage_never_mutates_the_status_it_was_given():
    """调用方手里那份还要原样用（路由要同时回「闸门判定」与「含本次运行的判定」）。"""
    base = _status(token_limit=1000, used_tokens=900)
    before = json.dumps(base, sort_keys=True, default=str)

    with_live_usage(base, tokens=500)

    assert json.dumps(base, sort_keys=True, default=str) == before, (
        "入参被改掉了 —— 嵌套的 used / limits / over_limits 是共享引用"
    )


def test_nothing_to_add_returns_the_status_unchanged():
    base = _status()

    result = with_live_usage(base, tokens=0, cost=None)

    assert result["over"] is False
    assert "live_included" not in result


# ==========================================================================
# 二、进度快照
# ==========================================================================


class _Progress:
    """`RoundProgress` 的最小替身（字段与 services/ai/engine.py 的一致）。"""

    def __init__(self, **kwargs):
        self.index = kwargs.get("index", 1)
        self.max_rounds = kwargs.get("max_rounds", 8)
        self.status = kwargs.get("status", "requests")
        self.prompt_tokens = kwargs.get("prompt_tokens", 100)
        self.completion_tokens = kwargs.get("completion_tokens", 20)
        self.cache_read_tokens = kwargs.get("cache_read_tokens", None)
        self.cache_write_tokens = kwargs.get("cache_write_tokens", None)
        self.requests_used = kwargs.get("requests_used", 3)
        self.requests_remaining = kwargs.get("requests_remaining", 17)
        self.items_chars = kwargs.get("items_chars", 4000)
        self.elapsed_ms = kwargs.get("elapsed_ms", 1200)


@pytest.fixture(autouse=True)
def _clean_registry():
    """进度表是**进程内**的全局状态：用例之间必须清干净，否则前一个用例的快照会让
    后一个用例的 `snapshot(...) is None` 断言失败。"""
    run_progress.reset_for_tests()
    yield
    run_progress.reset_for_tests()


def test_a_published_progress_is_readable():
    run_progress.publish(7, 3, _Progress(prompt_tokens=1200, completion_tokens=300))

    snap = run_progress.snapshot(7)

    assert snap is not None
    assert snap.run_id == 7 and snap.project_id == 3
    assert snap.live_tokens == 1500, "已用量是输入 + 输出"
    assert snap.to_dict()["round"] == 1


def test_an_unknown_run_has_no_progress_rather_than_zero():
    """**读不到 ≠ 0。** 界面据此显示「进度不可用」，而不是「已用 0 token」。"""
    assert run_progress.snapshot(999999) is None


def test_clearing_removes_the_snapshot():
    run_progress.publish(7, 3, _Progress())
    run_progress.clear(7)

    assert run_progress.snapshot(7) is None
    # 幂等：清一个不存在的键不报错
    run_progress.clear(7)


def test_a_stale_snapshot_is_not_reported_as_running(monkeypatch):
    """进程里留下的残影不该被当成「正在跑」。"""
    run_progress.publish(7, 3, _Progress())
    monkeypatch.setattr(run_progress, "MAX_AGE_SECONDS", -1)

    assert run_progress.snapshot(7) is None


def test_publishing_junk_does_not_break_the_analysis():
    """进度是给界面看的一眼，而它服务的是一条**要花钱**的分析路径 ——
    为了显示进度把分析弄挂是本末倒置。"""
    run_progress.publish(7, 3, object())  # 什么都没有的对象

    snap = run_progress.snapshot(7)
    assert snap is not None and snap.live_tokens == 0


def test_the_registry_does_not_grow_without_bound(monkeypatch):
    monkeypatch.setattr(run_progress, "MAX_ENTRIES", 3)
    for run_id in range(10):
        run_progress.publish(run_id, 1, _Progress())

    alive = [run_id for run_id in range(10) if run_progress.snapshot(run_id) is not None]
    assert len(alive) <= 3, f"清理没有生效，留下 {alive}"


# ==========================================================================
# 三、漏斗里真的接上了（假 client，不打网络）
# ==========================================================================


def _prepare_weekly_run(monkeypatch):
    """建一个能真的跑起来的周版本分析（复用 test_ai_analysis_service 的造数据工具）。"""
    import services.ai_analysis_service as ai_service
    from tests.test_ai_analysis_service import (
        TABLE_PATH,
        _create_project,
        _create_repo,
        _create_weekly_config,
        _seed_diff_cache,
    )

    project = _create_project()
    repo = _create_repo(project.id, f"t{uuid.uuid4().hex[:6]}", "svn", "table")
    cfg = _create_weekly_config(
        project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
    )
    _seed_diff_cache(cfg, repo, TABLE_PATH, datetime.now(timezone.utc), commit_id="a" * 40)
    db.session.commit()

    ai_service.set_project_api_key(project.id, "k")
    ai_service.update_project_analysis_config(
        project.id,
        {"api_base_url": "http://127.0.0.1:15721/v1", "api_model": "fake-model"},
    )
    db.session.commit()
    return ai_service, project, cfg


def test_the_funnel_publishes_each_round_and_clears_when_done(monkeypatch):
    """**这条是「线接上了」的证据。**

    假 client 只答一次 final，所以引擎只会报一轮；但那一轮必须真的报出来，
    而且跑完之后快照必须消失（否则界面一直显示「正在跑」）。
    """
    from tests.test_ai_analysis_service import _FakeClient

    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)

        published = []
        cleared = []
        monkeypatch.setattr(
            ai_service, "publish_run_progress",
            lambda run_id, project_id, progress: published.append((run_id, project_id, progress)),
        )
        monkeypatch.setattr(
            ai_service, "clear_run_progress", lambda run_id: cleared.append(run_id)
        )
        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (_FakeClient(), []))

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        assert published, "跑了整整一轮都没有报进度 —— 界面永远看不到「跑到第几轮」"
        run_id, project_id, progress = published[0]
        assert project_id == project.id
        assert progress.index == 1
        assert progress.max_rounds >= 1
        # 上报的 token 与落库的账对得上（同一份数据的两个出口）。
        assert progress.prompt_tokens == 120 and progress.completion_tokens == 80
        assert cleared == [run_id], (
            f"跑完没有清掉进度快照（cleared={cleared}）—— 界面会一直显示「正在跑」"
        )


def test_the_snapshot_is_cleared_even_when_the_run_cannot_start(monkeypatch):
    """失败路径也要清。**清不掉比不报更糟**：一次失败的分析会让那个运行号永远「在跑」。"""
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        cleared = []
        monkeypatch.setattr(
            ai_service, "clear_run_progress", lambda run_id: cleared.append(run_id)
        )
        # 建不出客户端 → 走失败分支（连模型都不调）
        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (None, [{"message": "x"}]))

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "failed", outcome
        assert len(cleared) == 1, "失败路径没有清掉进度快照"


# ==========================================================================
# 四、端点：形状、权限、与「不含本次运行」的那份的关系
# ==========================================================================


class _ViewClient:
    """绕过 before_request 的认证链，直接调 view（同 tests/test_ai_usage_capture.py）。"""

    def get(self, url, **kwargs):
        endpoint, args = flask_app.url_map.bind("localhost").match(url, method="GET")
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(url, method="GET", **kwargs):
            return make_response(view(**args))


@pytest.fixture()
def client():
    return _ViewClient()


@pytest.fixture(autouse=True)
def _isolate_platform_budget():
    """平台预算是**全局单行表**：别的用例（或本文件）留下一个上限，会让这里的
    「超没超」判定飘。前后各清一次。"""
    with flask_app.app_context():
        create_tables()
        AiPlatformBudget.query.delete()
        db.session.commit()
        yield
        AiPlatformBudget.query.delete()
        db.session.commit()


def _run_row(**kwargs):
    run = AiAnalysisRun(
        project_id=kwargs.get("project_id", 1),
        target_type="weekly",
        target_id=1,
        target_key=f"g{uuid.uuid4().hex[:6]}",
        status="running",
        scope="full",
        trigger_source="manual",
        response_text="",
        model=kwargs.get("model", "fake-model"),
        tokens_input=1_000_000,
        tokens_output=0,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    db.session.add(run)
    db.session.flush()
    return run


def test_the_progress_endpoint_reports_no_progress_instead_of_zero(client, monkeypatch):
    """没在跑（或读不到）时 `progress` 是 `null`，`budget` 仍是**不含本次运行**的那份。"""
    with flask_app.app_context():
        create_tables()
        run = _run_row()
        db.session.commit()
        run_id = run.id

    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
    body = client.get(f"/ai-analysis/runs/{run_id}/progress").get_json()

    assert body["success"] is True
    assert body["progress"] is None, "读不到进度时不许编一个 0 出来"
    assert "budget" in body


def test_the_progress_endpoint_folds_the_live_usage_into_the_verdict(client, monkeypatch):
    """跑中的那份判定要**含本次运行尚未落库的用量** —— 这正是这个端点的用途。"""
    with flask_app.app_context():
        create_tables()
        import services.ai_analysis_service as ai_service
        from tests.test_ai_analysis_service import _create_project

        project = _create_project()
        db.session.commit()
        # 项目上限 1.2M：已落库 1.0M（未超），本次运行又花了 0.3M → 含本次运行即超。
        ai_service.update_project_analysis_config(
            project.id, {"budget_token_limit": 1_200_000}
        )
        run = _run_row(project_id=project.id)
        db.session.commit()
        run_id, project_id = run.id, project.id
        run_progress.publish(
            run_id, project_id, _Progress(prompt_tokens=300_000, completion_tokens=0)
        )

    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
    body = client.get(f"/ai-analysis/runs/{run_id}/progress").get_json()

    assert body["progress"]["live_tokens"] == 300_000
    assert body["budget"]["used"]["tokens"] == 1_300_000
    assert body["budget"]["over"] is True, "含本次运行已经超了，界面必须能提前说"
    assert "含本次运行" in body["budget"]["reason"], body["budget"]["reason"]

    # 反向自检：把快照清掉之后，同一份判定**不许**再显示超 —— 否则这条测的是
    # 「预算超了」，而不是「本次运行把它顶超了」。
    run_progress.clear(run_id)
    with flask_app.app_context():
        plain = client.get(f"/ai-analysis/runs/{run_id}/progress").get_json()
    assert plain["progress"] is None
    assert plain["budget"]["over"] is False, plain["budget"]


def test_the_progress_endpoint_is_denied_by_the_runs_own_project(client, monkeypatch):
    with flask_app.app_context():
        create_tables()
        run = _run_row()
        db.session.commit()
        run_id = run.id

    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: False)
    resp = client.get(f"/ai-analysis/runs/{run_id}/progress")

    assert resp.status_code == 403


def test_the_progress_endpoint_is_404_for_a_missing_run(client, monkeypatch):
    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)
    assert client.get("/ai-analysis/runs/999999/progress").status_code == 404


# ==========================================================================
# 五、前端接线（静态守卫）
# ==========================================================================


TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _source(name):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return (root / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_drawer_can_show_the_banner_and_poll_the_progress(name):
    """三份抽屉必须都能：挂横幅、监听 `run` 事件、按运行号轮询、跑完停轮询。"""
    source = _source(name)

    assert "js/ai_budget_notice.js" in source, "没有引用共享的预算提示模块"
    assert 'id="aiBudgetBanner"' in source, "抽屉里没有横幅的挂载点"
    assert "addEventListener('run'" in source, (
        "没有监听 `run` 事件 —— 运行号只在 result 事件里给的话，跑的过程中无从轮询"
    )
    assert "/progress`" in source, "没有按运行号轮询进度"
    assert "stopAiBudgetWatch()" in source or "stopBudgetWatch()" in source, "没有停轮询"
    # 停轮询必须挂在「关流」上：在每处出口各写一遍必然会漏一处，而漏掉的那次会一直轮询。
    assert "function closeWeeklyAiStream() {" in source or "function cleanupStream() {" in source


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_remedy_text_has_a_single_source(name):
    """「去哪儿调预算」这句话**不许**在模板里写死。

    两档预算（项目 / 平台）的去处不同，写死的那一份必然在某一档上是错的 ——
    用户按那句话去找，找不到，然后以为是个 bug。唯一事实源在
    `static/js/ai_budget_notice.js` 的 `PLATFORM_HOWTO` / `PROJECT_HOWTO`。
    """
    source = _source(name)

    assert "调整方式：项目概览页" not in source, (
        f"{name} 里还留着写死的「调整方式」文案 —— 平台总预算不在这条路径上"
    )
    assert "AiBudgetNotice.metaText(" in source or "AiBudgetNotice.guidance(" in source, (
        f"{name} 没有使用共享的预算提示"
    )


def test_the_shared_module_is_the_only_place_that_names_the_two_places():
    """反向守卫：两句话只应该在共享模块里各出现一次。"""
    import re

    source = _source("static/js/ai_budget_notice.js")
    # **先剥注释再数**：注释里会原样引用被断言的写法（本仓库的静态断言口径），
    # 不剥的话这里会因为一句说明文字而假红。
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

    assert code.count("PROJECT_HOWTO =") == 1, "项目档的去处没有定义"
    assert code.count("PLATFORM_HOWTO =") == 1, "平台档的去处没有定义"
    assert "「AI 消耗」页面" in code
    assert code.count("'调整方式：'") == 1, (
        "「调整方式：」这句话只能在这一处拼出来 —— 多一处就等于多一个会漂移的说法"
    )
