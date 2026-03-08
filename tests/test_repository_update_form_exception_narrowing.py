from __future__ import annotations

import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace

import services.repository_update_form_service as update_form_service


def _build_common_deps():
    flashes = []
    logs = []
    redirects = []

    def _flash(message, category):
        flashes.append((category, str(message)))

    def _log_print(message, *_args, **_kwargs):
        logs.append(str(message))

    def _url_for(endpoint, **kwargs):
        key = kwargs.get("repository_id", kwargs.get("project_id", ""))
        return f"/{endpoint}/{key}"

    def _redirect(target):
        redirects.append(target)
        return {"redirect": target}

    return flashes, logs, redirects, _flash, _log_print, _url_for, _redirect


def test_repository_update_form_exception_tuples_are_declared():
    assert hasattr(update_form_service, "REPOSITORY_UPDATE_FORM_FORCE_SYNC_ERRORS")
    assert hasattr(update_form_service, "REPOSITORY_UPDATE_FORM_ASYNC_REFILTER_ERRORS")
    assert hasattr(update_form_service, "REPOSITORY_UPDATE_FORM_SUBMIT_ERRORS")


def test_handle_update_repository_form_rolls_back_on_known_submit_error():
    flashes, logs, redirects, flash, log_print, url_for, redirect = _build_common_deps()

    class _Session:
        def __init__(self):
            self.rollback_called = 0

        def rollback(self):
            self.rollback_called += 1

        def commit(self):
            return None

    session = _Session()
    repository = SimpleNamespace(
        id=10,
        project_id=20,
        type="svn",
        name="repo",
        url="svn://repo",
        root_directory="trunk",
        current_version="100",
        path_regex=None,
    )
    request = SimpleNamespace(
        form={
            "name": "repo",
            "display_order": "not-an-int",
            "category": "",
            "resource_type": "",
            "current_version": "100",
            "url": "svn://repo",
            "root_directory": "trunk",
        }
    )

    result = update_form_service.handle_update_repository_form(
        repository=repository,
        request=request,
        redirect=redirect,
        url_for=url_for,
        flash=flash,
        db=SimpleNamespace(session=session),
        validate_repository_name=lambda _name: True,
        log_print=log_print,
        create_auto_sync_task=lambda *_args, **_kwargs: None,
        app=SimpleNamespace(app_context=lambda: nullcontext()),
        Commit=SimpleNamespace(),
        Repository=SimpleNamespace(),
        DiffCache=SimpleNamespace(),
        clear_repository_state_for_switch_func=lambda **_kwargs: {},
    )

    assert session.rollback_called == 1
    assert result == {"redirect": "/edit_repository/10"}
    assert any(category == "error" and "更新仓库失败" in message for category, message in flashes)
    assert redirects[-1] == "/edit_repository/10"
    assert logs == []


def test_handle_update_repository_form_refilter_logs_force_sync_exception(monkeypatch):
    flashes, logs, redirects, flash, log_print, url_for, redirect = _build_common_deps()

    class _Session:
        def __init__(self, repo):
            self.repo = repo
            self.commits = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            return None

        def get(self, _model, _repository_id):
            return self.repo

        def delete(self, _obj):
            return None

    repository = SimpleNamespace(
        id=11,
        project_id=21,
        type="git",
        name="repo",
        url="git@repo",
        server_url="git@repo",
        branch="main",
        token="",
        path_regex=r".*\.xlsx$",
        start_date=None,
    )
    session = _Session(repository)
    db = SimpleNamespace(session=session)

    request = SimpleNamespace(
        form={
            "name": "repo",
            "display_order": "1",
            "category": "",
            "resource_type": "",
            "file_type_filter": r".*\.lua$",
            "path_regex": r".*\.lua$",
            "url": "git@repo",
            "server_url": "git@repo",
            "branch": "main",
        }
    )

    class _InlineThread:
        def __init__(self, target, daemon=None):
            self._target = target
            self.daemon = daemon

        def start(self):
            self._target()

    monkeypatch.setattr("threading.Thread", _InlineThread)

    fake_incremental_module = ModuleType("incremental_cache_system")

    class _FakeIncrementalCacheManager:
        def force_full_sync(self, _repository_id):
            raise RuntimeError("full sync failed")

    fake_incremental_module.IncrementalCacheManager = _FakeIncrementalCacheManager
    monkeypatch.setitem(sys.modules, "incremental_cache_system", fake_incremental_module)

    commit_query = SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: []))
    commit_model = SimpleNamespace(query=commit_query)

    result = update_form_service.handle_update_repository_form(
        repository=repository,
        request=request,
        redirect=redirect,
        url_for=url_for,
        flash=flash,
        db=db,
        validate_repository_name=lambda _name: True,
        log_print=log_print,
        create_auto_sync_task=lambda *_args, **_kwargs: None,
        app=SimpleNamespace(app_context=lambda: nullcontext()),
        Commit=commit_model,
        Repository=SimpleNamespace(),
        DiffCache=SimpleNamespace(query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(delete=lambda: 0))),
        clear_repository_state_for_switch_func=lambda **_kwargs: {},
    )

    assert result == {"redirect": "/repository_config/21"}
    assert any(category == "info" and "后台重新筛选文件" in message for category, message in flashes)
    assert any("全量同步异常" in message for message in logs)
    assert any("仓库内容重新筛选完成" in message for message in logs)
    assert redirects[-1] == "/repository_config/21"
