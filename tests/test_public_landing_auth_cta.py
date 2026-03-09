from __future__ import annotations

from pathlib import Path

from flask import Flask, render_template


_ROOT_DIR = Path(__file__).resolve().parents[1]


def _build_app(*, auth_backend: str, register_enabled: bool) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_ROOT_DIR / "templates"),
        static_folder=str(_ROOT_DIR / "static"),
    )
    app.secret_key = "test-key"

    @app.route("/", endpoint="index")
    def _index():
        return "ok"

    @app.route("/help", endpoint="help_page")
    def _help_page():
        return "help"

    @app.route("/landing")
    def _landing():
        return render_template("public_landing.html")

    app.jinja_env.globals["csrf_token"] = lambda: "csrf-token"
    app.jinja_env.globals["is_admin"] = lambda: False
    app.jinja_env.globals["is_logged_in"] = lambda: False
    app.jinja_env.globals["get_current_user"] = lambda: None
    app.jinja_env.globals["auth_backend"] = lambda: auth_backend
    app.jinja_env.globals["public_login_url"] = lambda: "/auth/login"
    app.jinja_env.globals["public_register_enabled"] = lambda: register_enabled
    app.jinja_env.globals["public_register_url"] = lambda: "/auth/register"
    app.jinja_env.globals["deployment_mode"] = "single"
    return app


def test_public_landing_local_mode_uses_generic_login_and_shows_register():
    app = _build_app(auth_backend="local", register_enabled=True)
    client = app.test_client()

    response = client.get("/landing")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "使用 Qkit 登录" not in html
    assert "</i>登录" in html
    assert 'href="/auth/register"' in html
    assert "</i>注册" in html
    assert "Qkit 登录认证" not in html


def test_public_landing_qkit_mode_keeps_qkit_login_without_register():
    app = _build_app(auth_backend="qkit", register_enabled=False)
    client = app.test_client()

    response = client.get("/landing")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "使用 Qkit 登录" in html
    assert 'href="/auth/register"' not in html


def test_public_landing_hides_register_button_when_feature_disabled():
    app = _build_app(auth_backend="local", register_enabled=False)
    client = app.test_client()

    response = client.get("/landing")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "</i>登录" in html
    assert 'href="/auth/register"' not in html
