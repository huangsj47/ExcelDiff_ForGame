from __future__ import annotations

import builtins
from types import SimpleNamespace

from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

import services.auth_bootstrap_service as auth_bootstrap


def _log_collector():
    logs = []

    def _log_print(message, *_args, **_kwargs):
        logs.append(str(message))

    return logs, _log_print


def test_auth_bootstrap_exception_tuples_are_declared():
    assert hasattr(auth_bootstrap, "AUTH_ROUTE_DISCOVERY_ERRORS")
    assert hasattr(auth_bootstrap, "AUTH_QKIT_ROUTE_REGISTER_ERRORS")
    assert hasattr(auth_bootstrap, "AUTH_DEFAULT_DATA_INIT_ERRORS")
    assert hasattr(auth_bootstrap, "AUTH_MODULE_INIT_ERRORS")


def test_register_qkit_fallback_endpoints_recovers_from_route_discovery_error(monkeypatch):
    logs, log_print = _log_collector()
    added_rules = []
    app_stub = SimpleNamespace(
        config={},
        url_map=SimpleNamespace(
            iter_rules=lambda: (_ for _ in ()).throw(RuntimeError("iter rules failed"))
        ),
        add_url_rule=lambda rule, **kwargs: added_rules.append((rule, kwargs.get("endpoint"))),
    )

    monkeypatch.setenv("AUTH_BACKEND", "qkit")
    auth_bootstrap.register_qkit_fallback_endpoints(app_stub, log_print)

    assert len(added_rules) == 3
    assert any("已注册 qkit 兜底路由" in message for message in logs)


def test_register_qkit_fallback_endpoints_logs_register_failure(monkeypatch):
    logs, log_print = _log_collector()
    app_stub = SimpleNamespace(
        config={},
        url_map=SimpleNamespace(iter_rules=lambda: []),
        add_url_rule=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate endpoint")),
    )

    monkeypatch.setenv("AUTH_BACKEND", "qkit")
    auth_bootstrap.register_qkit_fallback_endpoints(app_stub, log_print)

    assert any("注册 qkit 兜底路由失败" in message for message in logs)


def test_log_auth_route_diagnostics_handles_iter_rules_failure(monkeypatch):
    logs, log_print = _log_collector()
    app_stub = SimpleNamespace(
        config={"AUTH_INIT_FAILED": True},
        url_map=SimpleNamespace(
            iter_rules=lambda: (_ for _ in ()).throw(RuntimeError("iter rules failed"))
        ),
    )

    monkeypatch.setenv("AUTH_BACKEND", "qkit")
    monkeypatch.setenv("QKIT_LOGIN_SERVICE", "http://qkit-login")
    auth_bootstrap.log_auth_route_diagnostics(app_stub, log_print)

    assert any("AUTH 路由诊断: backend=qkit" in message for message in logs)
    assert any("QKIT_LOGIN_SERVICE=http://qkit-login" in message for message in logs)


def test_initialize_auth_subsystem_handles_import_error(monkeypatch):
    logs, log_print = _log_collector()
    app = Flask(__name__)
    app.secret_key = "test-key"
    db = SimpleNamespace()

    original_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "auth":
            raise ImportError("auth module missing")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    auth_bootstrap.initialize_auth_subsystem(app=app, db=db, log_print=log_print)

    assert app.config["AUTH_INIT_FAILED"] is True
    assert app.config["AUTH_INIT_ERROR"].startswith("ImportError:")
    assert any("账号系统初始化失败（ImportError）" in message for message in logs)


def test_initialize_auth_subsystem_handles_default_data_and_init_errors(monkeypatch):
    logs, log_print = _log_collector()
    app = Flask(__name__)
    app.secret_key = "test-key"
    db = SimpleNamespace()

    fake_auth = SimpleNamespace(
        init_auth=lambda *_args, **_kwargs: None,
        register_auth_blueprints=lambda *_args, **_kwargs: None,
        init_auth_default_data=lambda: (_ for _ in ()).throw(SQLAlchemyError("seed failed")),
    )
    original_import = builtins.__import__

    def _fake_import_default_data(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "auth":
            return fake_auth
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import_default_data)
    auth_bootstrap.initialize_auth_subsystem(app=app, db=db, log_print=log_print)

    assert app.config["AUTH_INIT_FAILED"] is False
    assert any("default data init skipped" in message for message in logs)

    fake_auth_fail = SimpleNamespace(
        init_auth=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("init boom")),
        register_auth_blueprints=lambda *_args, **_kwargs: None,
        init_auth_default_data=lambda: None,
    )

    def _fake_import_init_error(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "auth":
            return fake_auth_fail
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import_init_error)
    auth_bootstrap.initialize_auth_subsystem(app=app, db=db, log_print=log_print)

    assert app.config["AUTH_INIT_FAILED"] is True
    assert app.config["AUTH_INIT_ERROR"].startswith("RuntimeError:")
