from contextlib import nullcontext
from types import SimpleNamespace

import services.excel_diff_cache_service as excel_cache_module


class _DummyCol:
    def __init__(self, raise_on_none: bool = False):
        self.raise_on_none = raise_on_none

    def __lt__(self, other):
        if self.raise_on_none and other is None:
            raise AssertionError("unexpected compare with None")
        return ("lt", other)

    def __eq__(self, other):
        return ("eq", other)

    def desc(self):
        return self


class _ResultWrapper:
    def __init__(self, result):
        self._result = result

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result


class _CommitQuery:
    def __init__(self, commit, previous_commit):
        self._commit = commit
        self._previous_commit = previous_commit
        self.filter_calls = 0

    def filter_by(self, **_kwargs):
        return _ResultWrapper(self._commit)

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return _ResultWrapper(self._previous_commit)


class _PerfProbe:
    def __init__(self):
        self.records = []

    def record(self, pipeline, *, success=True, metrics=None, tags=None):
        self.records.append(
            {
                "pipeline": pipeline,
                "success": bool(success),
                "metrics": metrics or {},
                "tags": tags or {},
            }
        )


def _patch_runtime(monkeypatch, query, repository, get_unified_diff_data):
    fake_commit_model = type(
        "FakeCommitModel",
        (),
        {
            "query": query,
            "repository_id": _DummyCol(),
            "path": _DummyCol(),
            "commit_time": _DummyCol(raise_on_none=True),
            "id": _DummyCol(),
        },
    )
    fake_repo_model = type("FakeRepoModel", (), {})

    fake_db = SimpleNamespace(
        session=SimpleNamespace(
            get=lambda model, model_id: repository if (model is fake_repo_model and model_id == repository.id) else None
        )
    )
    fake_app = SimpleNamespace(app_context=lambda: nullcontext())

    monkeypatch.setattr(excel_cache_module, "Commit", fake_commit_model, raising=False)
    monkeypatch.setattr(excel_cache_module, "Repository", fake_repo_model, raising=False)
    monkeypatch.setattr(excel_cache_module, "db", fake_db, raising=False)
    monkeypatch.setattr(excel_cache_module, "app", fake_app, raising=False)
    monkeypatch.setattr(excel_cache_module, "log_print", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(excel_cache_module, "get_unified_diff_data", get_unified_diff_data, raising=False)


def test_background_cache_falls_back_to_id_query_when_commit_time_missing(monkeypatch):
    repository = SimpleNamespace(id=1)
    commit = SimpleNamespace(id=10, commit_id="abc123", path="config/a.xlsx", commit_time=None)
    previous_commit = SimpleNamespace(id=9, commit_id="prev001")
    query = _CommitQuery(commit, previous_commit)
    perf_probe = _PerfProbe()

    _patch_runtime(
        monkeypatch,
        query=query,
        repository=repository,
        get_unified_diff_data=lambda *_args, **_kwargs: {"type": "excel", "sheets": {}},
    )
    monkeypatch.setattr(excel_cache_module, "get_perf_metrics_service", lambda: perf_probe, raising=False)

    service = excel_cache_module.ExcelDiffCacheService()
    monkeypatch.setattr(service, "save_cached_diff", lambda **_kwargs: None)
    monkeypatch.setattr(service, "log_cache_operation", lambda *_args, **_kwargs: None)

    service.process_excel_diff_background(repository.id, commit.commit_id, commit.path)

    assert query.filter_calls == 1
    assert any(
        record["pipeline"] == "background_excel_cache"
        and record["success"] is True
        and record["tags"].get("source") == "background_excel"
        for record in perf_probe.records
    )
    assert not any(
        record["tags"].get("source") == "background_inner_exception"
        for record in perf_probe.records
    )


def test_background_cache_inner_exception_reports_error_details(monkeypatch):
    repository = SimpleNamespace(id=1)
    commit = SimpleNamespace(id=10, commit_id="abc123", path="config/a.xlsx", commit_time=None)
    previous_commit = SimpleNamespace(id=9, commit_id="prev001")
    query = _CommitQuery(commit, previous_commit)
    perf_probe = _PerfProbe()

    def _raise_diff_error(*_args, **_kwargs):
        raise RuntimeError("mock diff fail")

    _patch_runtime(
        monkeypatch,
        query=query,
        repository=repository,
        get_unified_diff_data=_raise_diff_error,
    )
    monkeypatch.setattr(excel_cache_module, "get_perf_metrics_service", lambda: perf_probe, raising=False)

    service = excel_cache_module.ExcelDiffCacheService()
    monkeypatch.setattr(service, "save_cached_diff", lambda **_kwargs: None)
    monkeypatch.setattr(service, "log_cache_operation", lambda *_args, **_kwargs: None)

    service.process_excel_diff_background(repository.id, commit.commit_id, commit.path)

    inner_error_records = [
        record
        for record in perf_probe.records
        if record["pipeline"] == "background_excel_cache"
        and record["success"] is False
        and record["tags"].get("source") == "background_inner_exception"
    ]
    assert inner_error_records
    assert inner_error_records[-1]["tags"].get("error_type") == "RuntimeError"
    assert "mock diff fail" in (inner_error_records[-1]["tags"].get("error_message") or "")
