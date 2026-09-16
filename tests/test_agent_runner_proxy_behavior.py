from types import SimpleNamespace

import pytest

from agent import runner


def _build_settings():
    return SimpleNamespace(
        platform_base_url="http://127.0.0.1:8002",
        agent_shared_secret="s1",
        agent_code="agent-test-1",
        agent_name="agent-test-1",
        agent_host="127.0.0.1",
        agent_port=9010,
        default_admin_username="admin",
        project_codes=[],
        heartbeat_interval_seconds=1,
        register_retry_interval_seconds=1,
        task_poll_interval_seconds=0,
        metrics_interval_seconds=300,
        local_task_types=[],
        repos_base_dir="agent_repos",
        log_verbose=False,
        temp_cache_upload_enabled=False,
        temp_cache_threshold_bytes=1024 * 1024,
        temp_cache_expire_days=90,
    )


def test_runner_never_calls_execute_proxy(monkeypatch):
    urls = []
    state = {"claimed": False}

    monkeypatch.setattr(runner, "load_settings", _build_settings)
    monkeypatch.setattr(runner, "collect_agent_metrics", lambda *_: {})
    monkeypatch.setattr(runner, "execute_task", lambda task, settings: ("failed", None, "local failed", None))
    monkeypatch.setattr(runner.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "_log", lambda *args, **kwargs: None)

    def _fake_post_json(url, payload, headers=None, timeout=0):
        urls.append(url)
        if url.endswith("/api/agents/register"):
            return 200, {"success": True, "agent_token": "token-1", "created_project_codes": [], "idempotent_project_codes": []}
        if url.endswith("/api/agents/heartbeat"):
            return 200, {"success": True}
        if url.endswith("/api/agents/tasks/claim"):
            if not state["claimed"]:
                state["claimed"] = True
                return 200, {
                    "success": True,
                    "task": {
                        "id": 101,
                        "task_type": "excel_diff",
                        "project_id": 1,
                        "repository_id": 2,
                        "payload": {},
                    },
                }
            runner._SHUTDOWN = True
            return 200, {"success": True, "task": None}
        if url.endswith("/api/agents/tasks/101/result"):
            runner._SHUTDOWN = True
            return 200, {"success": True}
        return 404, {"success": False}

    monkeypatch.setattr(runner, "post_json", _fake_post_json)

    runner._SHUTDOWN = False
    runner.run_agent()

    assert any(url.endswith("/api/agents/tasks/101/result") for url in urls)
    assert not any("/execute-proxy" in url for url in urls)


@pytest.mark.parametrize("first_status", [401, 403, 404, 409, 500])
def test_runtime_report_retry_refreshes_identity_and_discards_stale_tasks(monkeypatch, first_status):
    from agent import runner_runtime as runtime
    from agent import task_heartbeat

    settings = _build_settings()
    settings.auto_update_enabled = False
    settings.auto_update_install_deps = False
    registrations, reports = [], []
    state = {"clock": 1000, "claimed": False}

    def clock():
        state["clock"] += 10
        return state["clock"]

    def post(url, payload, **kwargs):
        assert len(reports) < 5, "失效结果不能无限重试"
        if url.endswith("/register"):
            registrations.append(1)
            return 200, {"success": True, "agent_token": f"token-{len(registrations)}"}
        if url.endswith("/heartbeat"):
            return 200, {"success": True}
        if url.endswith("/claim"):
            if state["claimed"]:
                runtime._SHUTDOWN = True
                return 200, {"success": True, "task": None}
            state["claimed"] = True
            return 200, {"success": True, "task": {"id": 1, "task_type": "noop", "attempt": 7}}
        if url.endswith("/result"):
            reports.append(dict(payload))
            # 401 连续两次：首次回传和补回传都失败，之后才重新注册。
            failures = 2 if first_status == 401 else 1
            return (first_status, {"success": False}) if len(reports) <= failures else (200, {"success": True})
        raise AssertionError(url)

    monkeypatch.setattr(runtime, "load_settings", lambda: settings)
    monkeypatch.setattr(runtime, "collect_agent_metrics", lambda *_: {})
    monkeypatch.setattr(runtime, "execute_task", lambda *_: ("completed", "done", None, None))
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(runtime.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runtime.time, "time", clock)
    monkeypatch.setattr(runtime, "post_json", post)
    monkeypatch.setattr(task_heartbeat, "post_json", post)
    monkeypatch.setattr(runtime, "_SHUTDOWN", False)
    monkeypatch.setattr(runtime, "_LAST_SIGNAL_NAME", "")
    runtime.run_agent()
    assert all(p["attempt"] == 7 for p in reports)
    if first_status in (403, 404, 409):
        assert len(reports) == 1
    else:
        assert len(reports) >= 2
    if first_status == 401:
        assert reports[-1]["agent_token"] == "token-2"
