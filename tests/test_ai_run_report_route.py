# -*- coding: utf-8 -*-
"""历次结论的三条路由：两个 `history` 与 `runs/<id>/report`。

判权的口径与 `/latest` 一致（按目标自己的项目），所以这里重点钉两件事：

1. **`/runs/<id>/report` 与 `/latest` 逐字同形** —— 界面上「历史那一份」与「当前那一份」
   走的是同一个渲染器，形状一变就会出现只有某一条路径才有的字段（或者缺一个字段）；
2. **判权只看这条运行自己的项目**，不接受调用方用参数指定。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import routes.ai_analysis_routes as ai_routes
import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Commit, Project, Repository
from tests.test_ai_history_survives_restart import REPORT_TEXT, _setup_config
from tests.test_ai_report_export_route import _login, _run


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def _commit(project_id: int):
    repo = Repository.query.filter_by(project_id=project_id).first()
    commit = Commit(
        repository_id=repo.id, commit_id="e" * 40, path="code/e.lua", operation="M",
        author="tester", commit_time=datetime.now(timezone.utc), message="测试",
        status="pending",
    )
    db.session.add(commit)
    db.session.commit()
    return commit


# ---------------------------------------------------------------------------
#  /runs/<id>/report：与 /latest 同形
# ---------------------------------------------------------------------------
def test_one_runs_report_has_the_same_shape_as_latest():
    with app.app_context():
        project, repo, cfg = _setup_config()
        commit = _commit(project.id)
        run = _run(project_id=project.id, target_type="commit", target_id=commit.id)
        with app.test_client() as client:
            _login(client)
            one = client.get(f"/ai-analysis/runs/{run.id}/report")
            latest = client.get(f"/ai-analysis/commit/{commit.id}/latest")

        assert one.status_code == 200
        body = one.get_json()
        assert body["success"] is True
        assert body["run_id"] == run.id
        # 顶层 run_id 与 result.run_id 是同一个（抽屉那行「明细」按钮要按它取数）
        assert body["result"]["run_id"] == run.id
        expected = latest.get_json()["result"]
        assert set(body["result"]) == set(expected), (
            f"两次读的形状不一致：{set(body['result']) ^ set(expected)}"
        )
        assert body["result"]["response_text"] == expected["response_text"]


def test_a_failed_run_comes_back_in_the_failure_shape():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
                   target_key=ai_service.build_weekly_group_key(cfg),
                   status="failed", response_text="", error_message="没答上来")
        with app.test_client() as client:
            _login(client)
            body = client.get(f"/ai-analysis/runs/{run.id}/report").get_json()

        assert body["result"]["status"] == "failed"
        assert body["result"]["error_message"] == "没答上来"
        assert body["result"]["result"] is None


def test_an_unknown_or_forbidden_run_is_404_and_403():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
                   target_key=ai_service.build_weekly_group_key(cfg))
        with app.test_client() as client:
            _login(client)
            assert client.get("/ai-analysis/runs/99999999/report").status_code == 404
            original = ai_routes._has_project_access
            ai_routes._has_project_access = lambda _pid: False
            try:
                denied = client.get(f"/ai-analysis/runs/{run.id}/report")
            finally:
                ai_routes._has_project_access = original

        assert denied.status_code == 403


# ---------------------------------------------------------------------------
#  两个 history 路由
# ---------------------------------------------------------------------------
def test_the_weekly_history_lists_the_runs_of_that_group():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = ai_service.build_weekly_group_key(cfg)
        older = _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
                     target_key=key, created_at=datetime.now(timezone.utc) - timedelta(days=2))
        newer = _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
                     target_key=key)
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/weekly/{cfg.id}/history")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["success"] is True
        assert payload["kind"] == "weekly"
        assert [row["run_id"] for row in payload["runs"]] == [newer.id, older.id]
        assert payload["total"] == 2
        assert payload["window_days"] > 0


def test_the_commit_history_lists_the_runs_of_that_commit():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        commit = _commit(project.id)
        run = _run(project_id=project.id, target_type="commit", target_id=commit.id)
        with app.test_client() as client:
            _login(client)
            payload = client.get(f"/ai-analysis/commit/{commit.id}/history").get_json()

        assert [row["run_id"] for row in payload["runs"]] == [run.id]
        assert payload["runs"][0]["summary"].startswith("把奖励发放")


def test_the_history_limit_is_clamped_to_the_sane_range():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
             target_key=ai_service.build_weekly_group_key(cfg))
        with app.test_client() as client:
            _login(client)
            huge = client.get(f"/ai-analysis/weekly/{cfg.id}/history?limit=99999").get_json()
            junk = client.get(f"/ai-analysis/weekly/{cfg.id}/history?limit=abc").get_json()

        assert huge["limit"] == 100
        assert junk["limit"] == 20


def test_a_history_of_a_target_i_cannot_see_is_403():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        commit = _commit(project.id)
        with app.test_client() as client:
            _login(client)
            original = ai_routes._has_project_access
            ai_routes._has_project_access = lambda _pid: False
            try:
                weekly = client.get(f"/ai-analysis/weekly/{cfg.id}/history")
                commit_resp = client.get(f"/ai-analysis/commit/{commit.id}/history")
            finally:
                ai_routes._has_project_access = original

        assert weekly.status_code == 403
        assert commit_resp.status_code == 403


def test_a_history_of_a_target_that_does_not_exist_is_404():
    with app.app_context():
        with app.test_client() as client:
            _login(client)
            assert client.get("/ai-analysis/weekly/99999999/history").status_code == 404
            assert client.get("/ai-analysis/commit/99999999/history").status_code == 404


def test_the_history_is_get_only():
    rules = [
        str(rule) for rule in app.url_map.iter_rules() if str(rule).endswith("/history")
    ]
    assert rules, "找不到 history 路由"
    for rule in rules:
        matched = [r for r in app.url_map.iter_rules() if str(r) == rule][0]
        assert matched.methods is not None and "GET" in matched.methods
        assert "POST" not in matched.methods


def test_a_run_with_no_payload_does_not_break_the_list():
    """老记录 / 坏 JSON：不许 500，行照样出来（等级与异常数留空）。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
                   target_key=ai_service.build_weekly_group_key(cfg))
        run.response_payload = "{这不是 JSON"
        db.session.commit()
        with app.test_client() as client:
            _login(client)
            payload = client.get(f"/ai-analysis/weekly/{cfg.id}/history").get_json()

        row = [item for item in payload["runs"] if item["run_id"] == run.id][0]
        # 没有结论就没有等级：空格子由界面渲染成「-」，接口不编一个
        assert row["risk_label"] == ""
        assert row["anomaly_count"] == 0
        assert row["exportable"] is True, "正文还在，就该能导出"


def test_the_history_rows_are_json_serializable():
    """行里**不许混进 ORM 对象**（那会让 `/history` 直接 500）——这条用真序列化验。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _run(project_id=project.id, target_type="weekly", target_id=cfg.id,
             target_key=ai_service.build_weekly_group_key(cfg))
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/weekly/{cfg.id}/history")

        assert response.status_code == 200
        json.dumps(response.get_json())
