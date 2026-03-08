from __future__ import annotations

import builtins
import sys
from types import ModuleType, SimpleNamespace

import services.commit_list_page_service as list_page_service


class _Field:
    def __init__(self, name):
        self.name = name

    def desc(self):
        return (self.name, "desc")

    def contains(self, value):
        return (self.name, "contains", value)

    def in_(self, values):
        return (self.name, "in", tuple(values))

    def ilike(self, value):
        return (self.name, "ilike", value)

    def like(self, value):
        return (self.name, "like", value)

    def __ge__(self, other):
        return (self.name, "ge", other)


class _LowerExpr:
    def __init__(self, field_name):
        self.field_name = field_name

    def like(self, value):
        return (self.field_name, "like", value)

    def in_(self, values):
        return (self.field_name, "in", tuple(values))


class _Args:
    def __init__(self, values):
        self._values = values

    def getlist(self, key):
        value = self._values.get(key, [])
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]

    def get(self, key, default=None, type=None):  # noqa: A002
        if key not in self._values:
            return default
        value = self._values[key]
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class _Pagination:
    def __init__(self, items):
        self.items = items
        self.total = len(items)
        self.pages = 1


class _CommitQuery:
    def __init__(self, commits):
        self._commits = commits

    def filter_by(self, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def paginate(self, **_kwargs):
        return _Pagination(self._commits)

    def count(self):
        return len(self._commits)


class _CommitModel:
    repository_id = _Field("repository_id")
    commit_time = _Field("commit_time")
    author = _Field("author")
    path = _Field("path")
    version = _Field("version")
    operation = _Field("operation")
    status = _Field("status")

    def __init__(self, commits):
        self.query = _CommitQuery(commits)


def _base_context(args_dict):
    logs = []
    commit = SimpleNamespace(
        id=1,
        author="alice",
        path="foo.lua",
        version="v1",
        operation="M",
        status="confirmed",
        status_changed_by="alice",
    )
    commit_model = _CommitModel([commit])
    repo = SimpleNamespace(
        id=10,
        name="repo_main",
        type="git",
        branch="main",
        start_date=None,
        clone_status="completed",
        clone_error="",
    )
    project = SimpleNamespace(id=20, repositories=[repo])
    repo.project = project
    repository_model = SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _id: repo))
    request = SimpleNamespace(args=_Args(args_dict))

    def _log_print(message, *_args, **_kwargs):
        logs.append(str(message))

    def _render_template(_template, **context):
        return context

    return {
        "logs": logs,
        "kwargs": dict(
            repository_id=10,
            Repository=repository_model,
            Commit=commit_model,
            request=request,
            abort=lambda code: (_ for _ in ()).throw(RuntimeError(f"abort:{code}")),
            render_template=_render_template,
            log_print=_log_print,
            has_project_access=lambda _project_id: True,
            queue_missing_git_branch_refresh=lambda *_args, **_kwargs: False,
            attach_author_display=lambda *_args, **_kwargs: None,
        ),
    }


def _install_fake_auth_modules(monkeypatch, *, user_query):
    auth_module = ModuleType("auth")
    auth_module.get_auth_backend = lambda: "local"
    auth_models_module = ModuleType("auth.models")

    class _AuthUser:
        username = _Field("username")
        display_name = _Field("display_name")
        email = _Field("email")
        query = user_query

    auth_models_module.AuthUser = _AuthUser
    monkeypatch.setitem(sys.modules, "auth", auth_module)
    monkeypatch.setitem(sys.modules, "auth.models", auth_models_module)


def test_commit_list_page_exception_tuples_are_declared():
    assert hasattr(list_page_service, "COMMIT_LIST_PAGE_USER_MODEL_ERRORS")
    assert hasattr(list_page_service, "COMMIT_LIST_PAGE_AUTHOR_FILTER_ERRORS")
    assert hasattr(list_page_service, "COMMIT_LIST_PAGE_USER_MAPPING_ERRORS")
    assert hasattr(list_page_service, "COMMIT_LIST_PAGE_AUTHOR_ATTACH_ERRORS")


def test_commit_list_page_logs_user_model_load_failure(monkeypatch):
    ctx = _base_context({})
    original_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "auth":
            raise ImportError("auth missing")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    result = list_page_service.handle_commit_list_page(**ctx["kwargs"])

    assert result["repository"].id == 10
    assert any("加载账号模型失败" in log for log in ctx["logs"])


def test_commit_list_page_logs_author_filter_failure(monkeypatch):
    ctx = _base_context({"author": "alice"})
    monkeypatch.setattr(list_page_service, "func", SimpleNamespace(lower=lambda field: _LowerExpr(field.name)))
    monkeypatch.setattr(list_page_service, "or_", lambda *args: ("or", args))

    class _ExplodingQuery:
        def filter(self, *_args, **_kwargs):
            raise RuntimeError("user filter boom")

    _install_fake_auth_modules(monkeypatch, user_query=_ExplodingQuery())

    result = list_page_service.handle_commit_list_page(**ctx["kwargs"])

    assert result["filters"]["author"] == "alice"
    assert any("按姓名筛选作者失败" in log for log in ctx["logs"])


def test_commit_list_page_logs_user_mapping_failure(monkeypatch):
    ctx = _base_context({})
    monkeypatch.setattr(list_page_service, "func", SimpleNamespace(lower=lambda field: _LowerExpr(field.name)))
    monkeypatch.setattr(list_page_service, "or_", lambda *args: ("or", args))

    class _ExplodingQuery:
        def filter(self, *_args, **_kwargs):
            raise RuntimeError("user mapping boom")

    _install_fake_auth_modules(monkeypatch, user_query=_ExplodingQuery())

    result = list_page_service.handle_commit_list_page(**ctx["kwargs"])

    assert result["pagination"].total == 1
    assert any("加载作者/确认用户姓名映射失败" in log for log in ctx["logs"])


def test_commit_list_page_logs_attach_author_display_failure(monkeypatch):
    ctx = _base_context({})
    ctx["kwargs"]["attach_author_display"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("attach failed")
    )

    result = list_page_service.handle_commit_list_page(**ctx["kwargs"])

    assert result["pagination"].pages == 1
    assert any("补齐提交列表作者映射失败" in log for log in ctx["logs"])
