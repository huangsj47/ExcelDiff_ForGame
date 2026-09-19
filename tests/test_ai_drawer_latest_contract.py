# -*- coding: utf-8 -*-
"""`/latest` 两个接口的**响应形状**：顶层必须带 `run_id`。

## 缺陷形态（用户可见，但没有报错）

三份抽屉模板读的都是 `data.run_id`（`renderAiUsageLine(result.result?.usage, data.run_id)`），
因为 SSE 的 `result` 事件把 `run_id` 放在**顶层**。而 `/latest` 原先只回
`{"success": true, "result": {...}}` —— 顶层没有 `run_id`。

后果是**刷新页面那条路上「本次消耗」那一行的「明细」按钮永远不出现**（`run_id` 为 null
时按钮是隐藏的，那不是「没采集」，是按钮没了）；只有刚跑完（走 SSE）才看得到它。
第一刀加「思考过程」时又发现同一个键的第二处用处：面板要按运行号去取落库的逐轮明细。

所以这里钉住两件事：

1. 有结论时 **顶层 `run_id` 与 `result.run_id` 一致**；
2. 没有结论时**不许编一个** —— 形状是 `{"success": true, "result": None}`，没有 `run_id`
   这一项（编一个 0 或者上一笔的号，界面就会拿着它去查别人的运行）。

用真登录 + 真路由（`_has_project_access` 要过），而不是直接调 view：这一组接口的权限
判定挂在 view 内部，直接调 view 的写法（见 `tests/test_ai_config_routes.py`）会把它绕过去。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun
from tests.test_ai_history_survives_restart import REPORT_TEXT, _setup_config

TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _login(client):
    """平台管理员会话（权限判定读的是 session，不是登录表单）。"""
    with client.session_transaction() as session:
        session["is_admin"] = True
        session["admin_user"] = "drawer-contract"
        session["_csrf_token"] = _uid("csrf")


def _weekly_run(project_id: int, cfg) -> AiAnalysisRun:
    now = datetime.now(timezone.utc)
    provenance = ai_service._current_provenance(project_id)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=cfg.id,
        target_key=ai_service.build_weekly_group_key(cfg),
        status="succeeded",
        scope="full",
        trigger_source="manual",
        started_at=now,
        finished_at=now + timedelta(seconds=30),
        created_at=now,
        response_text=REPORT_TEXT,
        response_payload='{"risk_level": "high"}',
        **provenance,
    )
    db.session.add(run)
    db.session.commit()
    return run


def _commit_run(project_id: int, commit_id: int) -> AiAnalysisRun:
    now = datetime.now(timezone.utc)
    provenance = ai_service._current_provenance(project_id)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="commit",
        target_id=commit_id,
        status="succeeded",
        scope="full",
        trigger_source="manual",
        started_at=now,
        finished_at=now + timedelta(seconds=30),
        created_at=now,
        response_text=REPORT_TEXT,
        response_payload='{"risk_level": "high"}',
        **provenance,
    )
    db.session.add(run)
    db.session.commit()
    return run


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def test_the_weekly_latest_carries_the_run_id_at_the_top_level():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _weekly_run(project.id, cfg)
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/weekly/{cfg.id}/latest")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["run_id"] == run.id, "顶层没有 run_id，「明细」按钮会消失"
    assert payload["result"]["run_id"] == run.id, "两处的运行号必须是同一个"


def test_the_commit_latest_carries_the_run_id_at_the_top_level():
    with app.app_context():
        from models import Commit

        project, repo, _cfg = _setup_config()
        commit = Commit(
            repository_id=repo.id, commit_id="d" * 40, path="code/a.lua",
            operation="M", author="tester", commit_time=datetime.now(timezone.utc),
            message="测试提交", status="pending",
        )
        db.session.add(commit)
        db.session.commit()
        run = _commit_run(project.id, commit.id)
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/commit/{commit.id}/latest")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["run_id"] == run.id
    assert payload["result"]["run_id"] == run.id


def test_no_conclusion_means_no_run_id_at_all():
    """没有结论时**不许编一个运行号**：界面会拿着它去查别人的运行明细。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/weekly/{cfg.id}/latest")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {"success": True, "result": None}
    assert "run_id" not in payload


def test_every_template_reads_the_run_id_from_the_top_level():
    """三份模板读的键必须是接口真的给的那个 —— 这是这一条修复的另一半。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in TEMPLATES:
        source = (root / name).read_text(encoding="utf-8")
        assert "data.run_id" in source, f"{name} 没有从顶层读 run_id"
