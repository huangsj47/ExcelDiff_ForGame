from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

import services.commit_diff_page_service as diff_page_service


class _Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def __lt__(self, other):
        return (self.name, "lt", other)

    def desc(self):
        return (self.name, "desc")


class _FilterQuery:
    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def test_commit_diff_page_exception_tuples_are_declared():
    assert hasattr(diff_page_service, "COMMIT_FULL_DIFF_FILE_READ_ERRORS")
    assert hasattr(diff_page_service, "COMMIT_FULL_DIFF_PIPELINE_ERRORS")
    assert hasattr(diff_page_service, "COMMIT_REFRESH_CACHE_SAVE_ERRORS")
    assert hasattr(diff_page_service, "COMMIT_REFRESH_UNEXPECTED_ERRORS")


def test_handle_commit_full_diff_uses_fallback_when_current_file_read_fails(monkeypatch):
    logs = []
    commit = SimpleNamespace(id=1, commit_id="abc12345", path="foo.lua", operation="M", commit_time=100)
    repository = SimpleNamespace(id=10, type="svn", url="svn://repo", root_directory=".", username="", token="")
    project = SimpleNamespace(id=20)

    commit_model = SimpleNamespace(
        repository_id=_Field("repository_id"),
        path=_Field("path"),
        commit_time=_Field("commit_time"),
        query=SimpleNamespace(
            get_or_404=lambda _id: commit,
            filter=lambda *_args, **_kwargs: _FilterQuery([commit]),
        ),
    )
    monkeypatch.setattr(
        diff_page_service.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cat failed")),
    )

    context = diff_page_service.handle_commit_full_diff(
        commit_id=1,
        Commit=commit_model,
        get_svn_service=lambda _repo: SimpleNamespace(local_path="c:/tmp/svn"),
        threaded_git_service_cls=lambda *_args, **_kwargs: None,
        get_commit_diff_mode_strategy=lambda: SimpleNamespace(
            allow_platform_local_git_clone=True,
            local_clone_block_message="blocked",
        ),
        ensure_commit_access_or_403=lambda _commit: (repository, project),
        resolve_previous_commit=lambda *_args, **_kwargs: None,
        generate_side_by_side_diff=lambda current, previous: {"current": current, "previous": previous},
        render_template=lambda _template, **kwargs: kwargs,
        log_print=lambda msg, *_args, **_kwargs: logs.append(str(msg)),
    )

    assert context["current_file_content"].startswith("获取当前版本失败")
    assert context["previous_file_content"] == ""


def test_handle_commit_full_diff_pipeline_error_returns_generic_message():
    logs = []
    commit = SimpleNamespace(id=1, commit_id="abc12345", path="foo.lua", operation="M", commit_time=100)
    repository = SimpleNamespace(id=10, type="svn", url="svn://repo", root_directory=".", username="", token="")
    project = SimpleNamespace(id=20)
    commit_model = SimpleNamespace(
        repository_id=_Field("repository_id"),
        path=_Field("path"),
        commit_time=_Field("commit_time"),
        query=SimpleNamespace(
            get_or_404=lambda _id: commit,
            filter=lambda *_args, **_kwargs: _FilterQuery([commit]),
        ),
    )

    context = diff_page_service.handle_commit_full_diff(
        commit_id=1,
        Commit=commit_model,
        get_svn_service=lambda _repo: (_ for _ in ()).throw(RuntimeError("svn init failed")),
        threaded_git_service_cls=lambda *_args, **_kwargs: None,
        get_commit_diff_mode_strategy=lambda: SimpleNamespace(
            allow_platform_local_git_clone=True,
            local_clone_block_message="blocked",
        ),
        ensure_commit_access_or_403=lambda _commit: (repository, project),
        resolve_previous_commit=lambda *_args, **_kwargs: None,
        generate_side_by_side_diff=lambda current, previous: {"current": current, "previous": previous},
        render_template=lambda _template, **kwargs: kwargs,
        log_print=lambda msg, *_args, **_kwargs: logs.append(str(msg)),
    )

    assert context["current_file_content"] == "无法获取文件内容"
    assert context["previous_file_content"] == "无法获取文件内容"
    assert any("获取文件内容失败" in log for log in logs)


def test_handle_refresh_commit_diff_logs_cache_save_error_and_returns_success():
    logs = []
    commit = SimpleNamespace(id=2, commit_id="def67890", path="bar.xlsx", commit_time=200)
    repository = SimpleNamespace(id=11)
    query_delete = SimpleNamespace(delete=lambda: 0)
    diff_cache_model = SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_kwargs: query_delete))
    html_cache_model = SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_kwargs: query_delete))
    commit_model = SimpleNamespace(
        repository_id=_Field("repository_id"),
        path=_Field("path"),
        commit_time=_Field("commit_time"),
        query=SimpleNamespace(
            get_or_404=lambda _id: commit,
            filter=lambda *_args, **_kwargs: _FilterQuery([]),
        ),
    )

    result = diff_page_service.handle_refresh_commit_diff(
        commit_id=2,
        Commit=commit_model,
        DiffCache=diff_cache_model,
        ExcelHtmlCache=html_cache_model,
        db=SimpleNamespace(session=SimpleNamespace(commit=lambda: None, rollback=lambda: None)),
        SQLAlchemyError=SQLAlchemyError,
        excel_cache_service=SimpleNamespace(
            is_excel_file=lambda _path: True,
            save_cached_diff=lambda **_kwargs: (_ for _ in ()).throw(OSError("save failed")),
        ),
        maybe_dispatch_commit_diff=lambda *_args, **_kwargs: None,
        get_unified_diff_data=lambda *_args, **_kwargs: {"type": "excel", "rows": []},
        safe_json_serialize=lambda payload: payload,
        ensure_commit_access_or_403=lambda _commit: (repository, None),
        jsonify=lambda payload: payload,
        log_print=lambda msg, *_args, **_kwargs: logs.append(str(msg)),
    )

    payload, status = result
    assert status == 200
    assert payload["status"] == "ready"
    assert any("保存缓存时出错" in log for log in logs)


def test_handle_refresh_commit_diff_returns_unexpected_error_for_key_error():
    logs = []
    commit = SimpleNamespace(id=3, commit_id="ghi90123", path="baz.lua", commit_time=300)
    repository = SimpleNamespace(id=12)
    commit_model = SimpleNamespace(
        repository_id=_Field("repository_id"),
        path=_Field("path"),
        commit_time=_Field("commit_time"),
        query=SimpleNamespace(
            get_or_404=lambda _id: commit,
            filter=lambda *_args, **_kwargs: _FilterQuery([]),
        ),
    )

    result = diff_page_service.handle_refresh_commit_diff(
        commit_id=3,
        Commit=commit_model,
        DiffCache=SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(delete=lambda: 0))),
        ExcelHtmlCache=SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(delete=lambda: 0))),
        db=SimpleNamespace(session=SimpleNamespace(commit=lambda: None, rollback=lambda: None)),
        SQLAlchemyError=SQLAlchemyError,
        excel_cache_service=SimpleNamespace(is_excel_file=lambda _path: False, save_cached_diff=lambda **_kwargs: True),
        maybe_dispatch_commit_diff=lambda *_args, **_kwargs: None,
        get_unified_diff_data=lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError("missing payload")),
        safe_json_serialize=lambda payload: payload,
        ensure_commit_access_or_403=lambda _commit: (repository, None),
        jsonify=lambda payload: payload,
        log_print=lambda msg, *_args, **_kwargs: logs.append(str(msg)),
    )

    payload, status = result
    assert status == 500
    assert payload["error_type"] == "unexpected_error"
    assert any("未知异常" in log for log in logs)
