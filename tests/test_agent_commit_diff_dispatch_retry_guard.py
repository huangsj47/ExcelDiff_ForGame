from types import SimpleNamespace


class _FakeBindingQuery:
    def __init__(self, binding):
        self._binding = binding

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._binding


def _build_commit():
    project = SimpleNamespace(id=301, code="G119")
    repository = SimpleNamespace(id=201, project=project)
    return SimpleNamespace(
        id=101,
        commit_id="abcd1234",
        path="src/demo.lua",
        operation="M",
        repository=repository,
    )


def test_failed_commit_diff_stops_auto_retry_after_threshold(monkeypatch):
    import services.agent_commit_diff_dispatch as dispatch_module

    commit = _build_commit()
    failed_task = SimpleNamespace(id=8, status="failed", error_message="commit record not found")
    called = {"ensure": 0}

    monkeypatch.setattr(
        dispatch_module,
        "AgentProjectBinding",
        SimpleNamespace(query=_FakeBindingQuery(SimpleNamespace(agent_id=11))),
    )
    monkeypatch.setattr(
        dispatch_module,
        "db",
        SimpleNamespace(
            session=SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(id=11),
                commit=lambda: None,
                flush=lambda: None,
                rollback=lambda: None,
            )
        ),
    )
    monkeypatch.setattr(dispatch_module, "_agent_online", lambda _agent: True)
    monkeypatch.setattr(dispatch_module, "_find_latest_commit_diff_task", lambda *_args, **_kwargs: (failed_task, {}))
    monkeypatch.setattr(dispatch_module, "_count_failed_commit_diff_tasks", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(
        dispatch_module,
        "_int_env",
        lambda name, default, min_value=None, max_value=None: 1
        if name == "AGENT_COMMIT_DIFF_MAX_AUTO_RETRY_ON_FAILURE"
        else default,
    )

    def _fake_ensure(*_args, **_kwargs):
        called["ensure"] += 1
        return SimpleNamespace(id=9), True

    monkeypatch.setattr(dispatch_module, "_ensure_commit_diff_task", _fake_ensure)

    result = dispatch_module.dispatch_or_get_commit_diff(commit, force_retry=False)
    assert result.get("status") == "error"
    assert "连续失败" in str(result.get("message") or "")
    assert called["ensure"] == 0


def test_failed_commit_diff_auto_retries_within_threshold(monkeypatch):
    import services.agent_commit_diff_dispatch as dispatch_module

    commit = _build_commit()
    failed_task = SimpleNamespace(id=18, status="failed", error_message="transient error")
    called = {"ensure": 0}

    monkeypatch.setattr(
        dispatch_module,
        "AgentProjectBinding",
        SimpleNamespace(query=_FakeBindingQuery(SimpleNamespace(agent_id=21))),
    )
    monkeypatch.setattr(
        dispatch_module,
        "db",
        SimpleNamespace(
            session=SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(id=21),
                commit=lambda: None,
                flush=lambda: None,
                rollback=lambda: None,
            )
        ),
    )
    monkeypatch.setattr(dispatch_module, "_agent_online", lambda _agent: True)
    monkeypatch.setattr(dispatch_module, "_find_latest_commit_diff_task", lambda *_args, **_kwargs: (failed_task, {}))
    monkeypatch.setattr(dispatch_module, "_count_failed_commit_diff_tasks", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        dispatch_module,
        "_int_env",
        lambda name, default, min_value=None, max_value=None: 2
        if name == "AGENT_COMMIT_DIFF_MAX_AUTO_RETRY_ON_FAILURE"
        else default,
    )

    def _fake_ensure(*_args, **_kwargs):
        called["ensure"] += 1
        return SimpleNamespace(id=19), True

    monkeypatch.setattr(dispatch_module, "_ensure_commit_diff_task", _fake_ensure)

    result = dispatch_module.dispatch_or_get_commit_diff(commit, force_retry=False)
    assert result.get("status") == "pending"
    assert called["ensure"] == 1
