from types import SimpleNamespace

from agent import runner


def _build_settings(allow_execute_proxy: bool):
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
        allow_execute_proxy=allow_execute_proxy,
        temp_cache_upload_enabled=False,
        temp_cache_threshold_bytes=1024 * 1024,
        temp_cache_expire_days=90,
    )


def test_runner_does_not_call_execute_proxy_when_disabled(monkeypatch):
    urls = []
    state = {"claimed": False}

    monkeypatch.setattr(runner, "load_settings", lambda: _build_settings(False))
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


def test_runner_calls_execute_proxy_when_enabled(monkeypatch):
    urls = []
    state = {"claimed": False}
    report_payloads = []

    monkeypatch.setattr(runner, "load_settings", lambda: _build_settings(True))
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
                        "id": 102,
                        "task_type": "excel_diff",
                        "project_id": 1,
                        "repository_id": 2,
                        "payload": {},
                    },
                }
            runner._SHUTDOWN = True
            return 200, {"success": True, "task": None}
        if url.endswith("/api/agents/tasks/102/execute-proxy"):
            return 200, {"success": True, "status": "completed", "result_summary": {"proxy": True}}
        if url.endswith("/api/agents/tasks/102/result"):
            report_payloads.append(payload)
            runner._SHUTDOWN = True
            return 200, {"success": True}
        return 404, {"success": False}

    monkeypatch.setattr(runner, "post_json", _fake_post_json)

    runner._SHUTDOWN = False
    runner.run_agent()

    assert any("/execute-proxy" in url for url in urls)
    assert report_payloads, "应回传任务结果"
    assert report_payloads[-1].get("status") == "completed"
