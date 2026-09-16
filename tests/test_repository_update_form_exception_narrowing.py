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

    # 直接替换本模块的后台线程启动接缝，线程体同步执行 ——
    # 不要去改全局的 threading.Thread（见 services/repository_update_form_service.py
    # 里 start_background_thread 的说明）。
    monkeypatch.setattr(
        update_form_service,
        "start_background_thread",
        lambda target, **kwargs: target(),
    )

    # 键名必须与真实导入路径一致：生产代码写的是 from services.incremental_cache_system
    # import ...，所以只有这个键能拦住它。写成裸 "incremental_cache_system" 时
    # setitem 仍然「成功」，但导入系统查的是 services.incremental_cache_system，
    # 于是打桩静默失效、测试改走真实实现。
    fake_incremental_module = ModuleType("services.incremental_cache_system")

    class _FakeIncrementalCacheManager:
        def force_full_sync(self, _repository_id):
            raise RuntimeError("full sync failed")

    fake_incremental_module.IncrementalCacheManager = _FakeIncrementalCacheManager
    monkeypatch.setitem(sys.modules, "services.incremental_cache_system", fake_incremental_module)

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
    # 光断言「有『全量同步异常』」是**不够**的：那句 except 的元组里含 ImportError，
    # 所以哪怕 import 本身挂了（ModuleNotFoundError）也会走进来记同一句话 ——
    # 断言照样绿，而增量全量同步其实一次都没跑。这里改成钉住**桩自己抛的文本**：
    # 只有注入的假模块真的被用上，日志里才会有 "full sync failed"。
    assert any("full sync failed" in message for message in logs), (
        "日志里没有出现桩抛出的 'full sync failed' —— 说明 sys.modules 注入没生效，"
        "被测代码走的是真实实现（或 import 失败）。检查注入键名是否与真实导入路径一致。"
    )
    assert any("仓库内容重新筛选完成" in message for message in logs)
    assert redirects[-1] == "/repository_config/21"
