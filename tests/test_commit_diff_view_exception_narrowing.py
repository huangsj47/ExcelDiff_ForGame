from __future__ import annotations

from types import SimpleNamespace

import services.commit_diff_view_service as diff_view_service


class _Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def desc(self):
        return (self.name, "desc")


class _Query:
    def __init__(self, commit):
        self._commit = commit

    def get_or_404(self, _commit_id):
        return self._commit

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return [self._commit]


class _CommitModel:
    repository_id = _Field("repository_id")
    path = _Field("path")
    commit_time = _Field("commit_time")
    id = _Field("id")

    def __init__(self, commit):
        self.query = _Query(commit)


def _build_common_kwargs(commit, logs):
    commit_model = _CommitModel(commit)
    repository = SimpleNamespace(
        id=11,
        name="repo",
        type="git",
        url="git@repo",
        root_directory=".",
        username="u",
        token="t",
    )
    project = SimpleNamespace(id=21, code="P21")

    def _log_print(message, *_args, **_kwargs):
        logs.append(str(message))

    def _render_template(_template, **context):
        return context

    return dict(
        time_module=SimpleNamespace(time=lambda: 1000.0),
        Commit=commit_model,
        db=SimpleNamespace(
            session=SimpleNamespace(delete=lambda *_a, **_k: None, commit=lambda: None, rollback=lambda: None)
        ),
        excel_cache_service=SimpleNamespace(
            is_excel_file=lambda _path: True,
            get_cached_diff=lambda *_args, **_kwargs: None,
            save_cached_diff=lambda **_kwargs: True,
        ),
        add_excel_diff_task=lambda *_args, **_kwargs: None,
        threaded_git_service_cls=lambda *_args, **_kwargs: SimpleNamespace(
            parse_excel_diff=lambda *_a, **_k: {"rows": []}
        ),
        active_git_processes={},
        get_commit_diff_mode_strategy=lambda: SimpleNamespace(async_agent_diff=False),
        resolve_previous_commit=lambda *_args, **_kwargs: None,
        get_unified_diff_data=lambda *_args, **_kwargs: {"rows": []},
        get_diff_data=lambda *_args, **_kwargs: {"type": "ok"},
        clean_json_data=lambda data: data,
        build_commit_diff_template_context=lambda **kwargs: kwargs,
        performance_metrics_service=SimpleNamespace(record=lambda *_a, **_k: None),
        ensure_commit_access_or_403=lambda _commit: (repository, project),
        render_template=_render_template,
        log_print=_log_print,
    )


def test_commit_diff_view_exception_tuples_are_declared():
    assert hasattr(diff_view_service, "COMMIT_DIFF_VIEW_AUTHOR_MAP_ERRORS")
    assert hasattr(diff_view_service, "COMMIT_DIFF_VIEW_CACHE_PROCESS_ERRORS")
    assert hasattr(diff_view_service, "COMMIT_DIFF_VIEW_EXCEL_PIPELINE_ERRORS")


def test_handle_commit_diff_view_keeps_rendering_when_author_mapping_fails():
    logs = []
    commit = SimpleNamespace(id=1, commit_id="abcdef12", operation="M", path="foo.xlsx", commit_time="2026-03-08")
    kwargs = _build_common_kwargs(commit, logs)
    kwargs.update(
        commit_id=1,
        attach_author_display=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("author map failed")),
        validate_excel_diff_data=lambda *_args, **_kwargs: (True, "ok"),
    )

    context = diff_view_service.handle_commit_diff_view(**kwargs)

    assert context["commit"] is commit
    assert any("作者姓名映射失败" in message for message in logs)


def test_handle_commit_diff_view_falls_back_when_cached_data_processing_raises():
    logs = []
    commit = SimpleNamespace(id=2, commit_id="bcdefa34", operation="M", path="bar.xlsx", commit_time="2026-03-08")
    cached_diff = SimpleNamespace(id=99, diff_data='{"rows":[]}', diff_version="v1", created_at="now")
    kwargs = _build_common_kwargs(commit, logs)

    validate_call = {"count": 0}

    def _validate(_data):
        validate_call["count"] += 1
        if validate_call["count"] == 1:
            raise RuntimeError("validate boom")
        return True, "ok"

    kwargs.update(
        commit_id=2,
        attach_author_display=lambda *_args, **_kwargs: None,
        excel_cache_service=SimpleNamespace(
            is_excel_file=lambda _path: True,
            get_cached_diff=lambda *_args, **_kwargs: cached_diff,
            save_cached_diff=lambda **_kwargs: True,
        ),
        validate_excel_diff_data=_validate,
    )

    context = diff_view_service.handle_commit_diff_view(**kwargs)

    assert context["is_excel"] is True
    assert context["diff_data"] == {"rows": []}
    assert any("缓存数据处理异常" in message for message in logs)


def test_handle_commit_diff_view_handles_excel_pipeline_error():
    logs = []
    commit = SimpleNamespace(id=3, commit_id="cdefab56", operation="M", path="err.xlsx", commit_time="2026-03-08")
    kwargs = _build_common_kwargs(commit, logs)
    kwargs.update(
        commit_id=3,
        attach_author_display=lambda *_args, **_kwargs: None,
        validate_excel_diff_data=lambda *_args, **_kwargs: (True, "ok"),
        get_unified_diff_data=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("excel diff failed")),
    )

    context = diff_view_service.handle_commit_diff_view(**kwargs)

    assert context["is_excel"] is True
    assert context["diff_data"] is None
    assert any("Excel diff generation failed" in message for message in logs)
