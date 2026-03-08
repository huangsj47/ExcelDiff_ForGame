from flask import Flask
from werkzeug.routing import BuildError

import services.core_navigation_handlers as cnh


def _build_min_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/", endpoint="index")
    def index():
        return "ok"

    return app


def test_has_routable_endpoint_returns_false_when_url_map_iter_rules_raises(monkeypatch):
    app = _build_min_app()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("broken-map")

    monkeypatch.setattr(app.url_map, "iter_rules", _boom)

    with app.app_context():
        assert cnh._has_routable_endpoint("index") is False


def test_safe_url_for_returns_none_when_build_fails(monkeypatch):
    app = _build_min_app()

    monkeypatch.setattr(
        cnh,
        "url_for",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BuildError("index", {}, "GET")),
    )

    with app.test_request_context("/"):
        assert cnh._safe_url_for("index") is None


def test_index_returns_500_when_template_render_raises_lookup_error(monkeypatch):
    app = _build_min_app()
    monkeypatch.setattr(cnh, "get_runtime_model", lambda _name: (lambda *_args, **_kwargs: None))
    monkeypatch.setattr(cnh, "_is_logged_in", lambda: False)
    monkeypatch.setattr(
        cnh,
        "render_template",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError("template missing")),
    )

    with app.test_request_context("/"):
        result = cnh.index()

    assert isinstance(result, tuple)
    body, status = result
    assert status == 500
    assert "首页加载错误" in str(body)
