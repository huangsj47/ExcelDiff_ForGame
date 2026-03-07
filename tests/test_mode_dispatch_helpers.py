from types import SimpleNamespace


def test_maybe_dispatch_commit_diff_returns_none_when_not_agent_mode(monkeypatch):
    import services.agent_commit_diff_dispatch as dispatch_module

    called = {"count": 0}

    def _fake_dispatch(commit, force_retry=False):
        called["count"] += 1
        return {"status": "pending"}

    monkeypatch.setattr(dispatch_module, "is_agent_dispatch_mode", lambda: False)
    monkeypatch.setattr(dispatch_module, "dispatch_or_get_commit_diff", _fake_dispatch)

    result = dispatch_module.maybe_dispatch_commit_diff(SimpleNamespace(id=1), force_retry=True)
    assert result is None
    assert called["count"] == 0


def test_maybe_dispatch_commit_diff_delegates_when_agent_mode(monkeypatch):
    import services.agent_commit_diff_dispatch as dispatch_module

    called = {"count": 0, "force_retry": None}

    def _fake_dispatch(commit, force_retry=False):
        called["count"] += 1
        called["force_retry"] = force_retry
        return {"status": "ready", "payload": {"k": "v"}}

    monkeypatch.setattr(dispatch_module, "is_agent_dispatch_mode", lambda: True)
    monkeypatch.setattr(dispatch_module, "dispatch_or_get_commit_diff", _fake_dispatch)

    result = dispatch_module.maybe_dispatch_commit_diff(SimpleNamespace(id=2), force_retry=True)
    assert isinstance(result, dict)
    assert result.get("status") == "ready"
    assert called["count"] == 1
    assert called["force_retry"] is True


def test_dispatch_auto_sync_task_when_agent_mode(monkeypatch):
    import services.task_worker_service as worker

    called = {"count": 0, "repo": None, "payload": None}

    def _fake_create_auto_sync_task(repository_id, extra_payload=None):
        called["count"] += 1
        called["repo"] = repository_id
        called["payload"] = extra_payload
        return 123

    monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: True)
    monkeypatch.setattr(worker, "create_auto_sync_task", _fake_create_auto_sync_task)

    handled, task_id = worker.dispatch_auto_sync_task_when_agent_mode(
        88, extra_payload={"force_reclone": True}
    )
    assert handled is True
    assert task_id == 123
    assert called["count"] == 1
    assert called["repo"] == 88
    assert called["payload"] == {"force_reclone": True}


def test_dispatch_auto_sync_task_when_agent_mode_noop_in_single(monkeypatch):
    import services.task_worker_service as worker

    called = {"count": 0}

    def _fake_create_auto_sync_task(repository_id, extra_payload=None):
        called["count"] += 1
        return 999

    monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
    monkeypatch.setattr(worker, "create_auto_sync_task", _fake_create_auto_sync_task)

    handled, task_id = worker.dispatch_auto_sync_task_when_agent_mode(99)
    assert handled is False
    assert task_id is None
    assert called["count"] == 0
