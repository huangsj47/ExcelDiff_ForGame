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
        # 「前一提交」现在是注入项：与页头/正文/后台任务共用同一个解析。
        # 这里回 None（新增文件那类），断言只关心异常收窄的行为。
        "resolve_previous_commit": lambda _commit: None,
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
    monkeypatch.setenv("EXCEPTION_NARROWING_ROLLOUT_MODE", "repository")
    monkeypatch.setenv("EXCEPTION_NARROWING_ROLLOUT_REPOSITORIES", "999")
    kwargs["get_unified_diff_data"] = lambda *_a, **_k: (_ for _ in ()).throw(KeyError("missing"))

    payload, status = excel_api.handle_get_excel_diff_data(**kwargs)

    assert status == 202
    assert payload["status"] == "pending_compat"
    assert payload["compat_mode"] is True
    assert payload["error_type"] == "unexpected_error"


def test_api_resolves_the_baseline_once_and_uses_it_for_every_cache_call():
    """接口这条链的「对比版本」必须与页头是同一个，而且读/写缓存都要带上它。

    历史形态：接口自己写了一条按 `(commit_time, id)` 的查询，读缓存时**不带基线**
    —— 于是它能读到、也能写出属于**另一条基线**渲染的 HTML。线上 6767 就是这样：
    页面那条链是对的，同一时刻接口返回的 HTML 里却是只存在于更晚版本的旧值。
    """
    seen = {}
    resolver_calls = []
    commit = SimpleNamespace(id=9, commit_id="c9", path="c.xlsx", commit_time=9)
    kwargs = _base_kwargs(commit=commit)
    previous_commit = SimpleNamespace(commit_id="BASE_SHA")

    def _resolve_resolver(commit_obj):
        resolver_calls.append(commit_obj)
        return previous_commit

    def _html_read(_repo_id, _commit_id, _path, **extra):
        seen["html_read"] = extra.get("previous_commit_id")
        return None

    def _data_read(_repo_id, _commit_id, _path, **extra):
        seen["data_read"] = extra.get("previous_commit_id")
        return None

    def _html_save(*_args, **extra):
        seen["html_save"] = extra.get("previous_commit_id")
        return True

    kwargs["resolve_previous_commit"] = _resolve_resolver
    kwargs["excel_html_cache_service"] = SimpleNamespace(
        get_cached_html=_html_read,
        generate_excel_html=lambda _data: ("<table/>", "css", "js"),
        save_html_cache=_html_save,
    )
    kwargs["excel_cache_service"] = SimpleNamespace(
        is_excel_file=lambda _path: True,
        get_cached_diff=_data_read,
    )
    kwargs["get_unified_diff_data"] = lambda *_a, **_k: {
        "type": "excel",
        "sheets": {"S": {"rows": [{"row_number": 2, "status": "modified", "data": {}}]}},
        "summary": {"added": 0, "modified": 1, "removed": 0, "total": 1},
    }

    excel_api.handle_get_excel_diff_data(**kwargs)

    assert len(resolver_calls) == 1, "接口自己又解析了一遍前一提交（应当只解析一次并复用）"
    assert seen.get("html_read") == "BASE_SHA", "读 HTML 缓存没有带基线 —— 会命中别的基线渲染的 HTML"
    assert seen.get("data_read") == "BASE_SHA", "读差异缓存没有带基线"
    assert seen.get("html_save") == "BASE_SHA", (
        "写 HTML 缓存时记录的不是**这次真正用的**基线 —— 校验会形同虚设"
    )
