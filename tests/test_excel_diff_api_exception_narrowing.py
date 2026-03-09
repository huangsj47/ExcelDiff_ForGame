from __future__ import annotations

from types import SimpleNamespace

import services.excel_diff_api_service as excel_api


class _Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def __lt__(self, other):
        return (self.name, "lt", other)

    def desc(self):
        return (self.name, "desc")


class _QueryResult:
    def __init__(self, first_value=None):
        self._first_value = first_value

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._first_value


class _CommitQuery:
    def __init__(self, commit, previous_commit=None):
        self._commit = commit
        self._previous_commit = previous_commit

    def get_or_404(self, _commit_id):
        return self._commit

    def filter(self, *_args, **_kwargs):
        return _QueryResult(self._previous_commit)


class _CommitModel:
    repository_id = _Field("repository_id")
    path = _Field("path")
    commit_time = _Field("commit_time")
    id = _Field("id")

    def __init__(self, commit, previous_commit=None):
        self.query = _CommitQuery(commit, previous_commit)


class _Args:
    def __init__(self, force_retry=""):
        self._force_retry = force_retry

    def get(self, key):
        if key == "force_retry":
            return self._force_retry
        return None


def _base_kwargs(*, commit, previous_commit=None, metrics_records=None, logs=None):
    repository = SimpleNamespace(id=101, name="repo")
    project = SimpleNamespace(id=501, code="P501")
    commit_model = _CommitModel(commit, previous_commit=previous_commit)

    if metrics_records is None:
        metrics_records = []
    if logs is None:
        logs = []

    return {
        "commit_id": commit.id,
        "request": SimpleNamespace(args=_Args()),
        "jsonify": lambda payload: payload,
        "time_module": SimpleNamespace(time=lambda: 1000.0),
        "Commit": commit_model,
        "db": SimpleNamespace(session=SimpleNamespace(commit=lambda: None, rollback=lambda: None)),
        "excel_cache_service": SimpleNamespace(
            is_excel_file=lambda _path: True,
            get_cached_diff=lambda *_a, **_k: None,
        ),
        "excel_html_cache_service": SimpleNamespace(
            get_cached_html=lambda *_a, **_k: None,
            generate_excel_html=lambda _data: ("<table/>", "css", "js"),
            save_html_cache=lambda *_a, **_k: None,
        ),
        "performance_metrics_service": SimpleNamespace(
            record=lambda *args, **kwargs: metrics_records.append((args, kwargs))
        ),
        "maybe_dispatch_commit_diff": lambda *_a, **_k: None,
        "get_unified_diff_data": lambda *_a, **_k: {"type": "excel", "rows": []},
        "add_excel_diff_task": lambda *_a, **_k: None,
        "ensure_commit_access_or_403": lambda _commit: (repository, project),
        "log_print": lambda message, *_a, **_k: logs.append(str(message)),
    }


def test_excel_diff_api_exception_tuples_are_declared():
    assert hasattr(excel_api, "EXCEL_DIFF_API_AGENT_RENDER_ERRORS")
    assert hasattr(excel_api, "EXCEL_DIFF_API_HTML_RENDER_ERRORS")
    assert hasattr(excel_api, "EXCEL_DIFF_API_UNEXPECTED_ERRORS")


def test_agent_ready_returns_fallback_payload_when_html_render_fails():
    commit = SimpleNamespace(id=1, commit_id="a1", path="a.xlsx", commit_time=1)
    kwargs = _base_kwargs(commit=commit)
    kwargs["maybe_dispatch_commit_diff"] = lambda *_a, **_k: {
        "status": "ready",
        "payload": {"diff_data": {"type": "excel", "rows": []}},
    }
    kwargs["excel_html_cache_service"] = SimpleNamespace(
        get_cached_html=lambda *_a, **_k: None,
        generate_excel_html=lambda _data: (_ for _ in ()).throw(RuntimeError("render failed")),
        save_html_cache=lambda *_a, **_k: None,
    )

    payload = excel_api.handle_get_excel_diff_data(**kwargs)

    assert payload["success"] is True
    assert payload["from_agent"] is True
    assert payload["html_render_failed"] is True
    assert payload["diff_data"]["type"] == "excel"


def test_data_cache_render_failure_returns_raw_diff_data_and_records_metric():
    metrics_records = []
    commit = SimpleNamespace(id=2, commit_id="b2", path="b.xlsx", commit_time=2)
    kwargs = _base_kwargs(commit=commit, metrics_records=metrics_records)
    kwargs["excel_cache_service"] = SimpleNamespace(
        is_excel_file=lambda _path: True,
        get_cached_diff=lambda *_a, **_k: SimpleNamespace(diff_data='{"type":"excel","rows":[1]}'),
    )
    kwargs["excel_html_cache_service"] = SimpleNamespace(
        get_cached_html=lambda *_a, **_k: None,
        generate_excel_html=lambda _data: (_ for _ in ()).throw(ValueError("bad html")),
        save_html_cache=lambda *_a, **_k: None,
    )

    payload = excel_api.handle_get_excel_diff_data(**kwargs)

    assert payload["success"] is True
    assert payload["from_cache"] is True
    assert payload["diff_data"]["type"] == "excel"
    assert any(record[1].get("tags", {}).get("source") == "data_cache_html_render_failed" for record in metrics_records)


def test_realtime_render_failure_returns_raw_diff_and_enqueues_cache_task(monkeypatch):
    metrics_records = []
    enqueued_tasks = []
    commit = SimpleNamespace(id=3, commit_id="c3", path="c.xlsx", commit_time=3)
    kwargs = _base_kwargs(commit=commit, metrics_records=metrics_records)
    monkeypatch.setattr(excel_api, "and_", lambda *args: ("and", args))
    monkeypatch.setattr(excel_api, "or_", lambda *args: ("or", args))
    kwargs["excel_cache_service"] = SimpleNamespace(
        is_excel_file=lambda _path: True,
        get_cached_diff=lambda *_a, **_k: None,
    )
    kwargs["excel_html_cache_service"] = SimpleNamespace(
        get_cached_html=lambda *_a, **_k: None,
        generate_excel_html=lambda _data: (_ for _ in ()).throw(RuntimeError("render boom")),
        save_html_cache=lambda *_a, **_k: None,
    )
    kwargs["add_excel_diff_task"] = lambda *args, **kwargs: enqueued_tasks.append((args, kwargs))

    payload = excel_api.handle_get_excel_diff_data(**kwargs)

    assert payload["success"] is True
    assert payload["from_cache"] is False
    assert payload["diff_data"]["type"] == "excel"
    assert len(enqueued_tasks) == 1
    assert any(record[1].get("tags", {}).get("source") == "realtime_html_render_failed" for record in metrics_records)


def test_unexpected_key_error_returns_unexpected_error_contract(monkeypatch):
    metrics_records = []
    logs = []
    commit = SimpleNamespace(id=4, commit_id="d4", path="d.xlsx", commit_time=4)
    kwargs = _base_kwargs(commit=commit, metrics_records=metrics_records, logs=logs)
    monkeypatch.setattr(excel_api, "and_", lambda *args: ("and", args))
    monkeypatch.setattr(excel_api, "or_", lambda *args: ("or", args))
    kwargs["get_unified_diff_data"] = lambda *_a, **_k: (_ for _ in ()).throw(KeyError("missing"))

    payload, status = excel_api.handle_get_excel_diff_data(**kwargs)

    assert status == 500
    assert payload["error_type"] == "unexpected_error"
    assert any("Excel diff处理失败" in item for item in logs)
    assert any(record[1].get("tags", {}).get("source") == "exception" for record in metrics_records)


def test_unexpected_key_error_rolls_out_as_pending_when_repository_not_enabled(monkeypatch):
    metrics_records = []
    logs = []
    commit = SimpleNamespace(id=5, commit_id="e5", path="e.xlsx", commit_time=5)
    kwargs = _base_kwargs(commit=commit, metrics_records=metrics_records, logs=logs)
    monkeypatch.setattr(excel_api, "and_", lambda *args: ("and", args))
    monkeypatch.setattr(excel_api, "or_", lambda *args: ("or", args))
    monkeypatch.setenv("EXCEPTION_NARROWING_ROLLOUT_MODE", "repository")
    monkeypatch.setenv("EXCEPTION_NARROWING_ROLLOUT_REPOSITORIES", "999")
    kwargs["get_unified_diff_data"] = lambda *_a, **_k: (_ for _ in ()).throw(KeyError("missing"))

    payload, status = excel_api.handle_get_excel_diff_data(**kwargs)

    assert status == 202
    assert payload["status"] == "pending_compat"
    assert payload["compat_mode"] is True
    assert payload["error_type"] == "unexpected_error"
