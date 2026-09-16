# -*- coding: utf-8 -*-
"""AI 配置路由：权限、字段级错误、两个探测接口，以及「密钥不出后端」。

## 为什么需要这个文件

`/ai-analysis/*` 这组路由此前**一个测试都没有**（`grep "ai-analysis" tests/` 零命中），
其中却包括四个权限分支和一条会带密钥向外部地址发请求的路径。没有测试意味着任何一次
重构都可能悄悄把它们改成「谁都能调」。

## 本文件守的四条性质

1. **写配置要项目管理员**，读配置要项目成员；被拒是 **403**，不是跳登录页
   （否则前端只会拿到一张 HTML 登录页，报「解析失败」，而真实原因是没有权限）。
2. **校验失败回 400 + 字段级明细**，且库里不留痕迹 —— 越界值不许被悄悄夹取。
3. **模型列表取不到不是错误**：`success=True` + `supported=False`，界面据此引导手动填写。
   这是实测约束：能正常对话的模型名可能压根不在 `/models` 里。
4. **密钥绝不出现在任何响应体里**（连掩码都不给）。
"""
from __future__ import annotations

import json
import uuid

import pytest
from flask import make_response

import routes.ai_analysis_routes as ai_routes
import services.ai_analysis_service as ai_service
from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiProjectApiKey
from services.ai.endpoint_service import ProbeResult


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _ViewClient:
    """把 `client.get/post` 转成「按 URL 解析出 view 后直接调用」。

    认证链挂在 `before_request` 上，`test_client` 走不到 view 就先被 401 拦住了。直接调
    view 正好**只测 view 内部**的权限判定 —— 那才是本文件要守的东西（仓库既有测试
    `tests/test_write_routes_are_not_get.py` 用的是同一个办法）。CSRF 同理在
    before_request 里，本文件不重复测它。

    返回真正的 `Response`（用 `make_response` 处理 view 返回的 `(body, status)` 元组），
    所以下面的断言可以照常写 `resp.status_code` / `resp.get_json()`。
    """

    def get(self, url, **_kwargs):
        return self._call("GET", url, None)

    def post(self, url, json=None, **_kwargs):
        return self._call("POST", url, json)

    def _call(self, method, url, body):
        endpoint, args = flask_app.url_map.bind("localhost").match(url, method=method)
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(url, method=method, json=body):
            return make_response(view(**args))


@pytest.fixture()
def client():
    return _ViewClient()


@pytest.fixture()
def project_id():
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("ai-route"))
        db.session.add(project)
        db.session.commit()
        return project.id


def _allow(monkeypatch, *, access=True, admin=True):
    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: access)
    monkeypatch.setattr(ai_routes, "_has_project_admin_access", lambda _pid: admin)


def _fake_client(**results):
    """把 `build_endpoint_client` 换掉：不碰数据库、不发真实请求。"""

    class _Client:
        model = "fake-model"
        _api_key = "fake-key"

    def _factory(_pid, override=None):
        return _Client(), []

    return _factory


# ==========================================================================
# 权限
# ==========================================================================


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "/models"),
        ("post", "/test-connection"),
    ],
)
def test_probe_endpoints_require_project_admin(client, project_id, monkeypatch, method, suffix):
    """两个探测接口都会**带着项目密钥向外部地址发请求**。

    用管理员权限而不是访问权限：普通成员不该有能力触发它，也就没人能拿它当探测内网的
    跳板。
    """
    _allow(monkeypatch, admin=False)
    resp = getattr(client, method)(f"/ai-analysis/projects/{project_id}{suffix}", json={})

    assert resp.status_code == 403
    assert resp.get_json()["success"] is False


def test_config_read_requires_project_access(client, project_id, monkeypatch):
    _allow(monkeypatch, access=False)
    resp = client.get(f"/ai-analysis/projects/{project_id}/config")

    assert resp.status_code == 403


def test_config_write_requires_project_admin(client, project_id, monkeypatch):
    _allow(monkeypatch, admin=False)
    resp = client.post(f"/ai-analysis/projects/{project_id}/config", json={"api_model": "m"})

    assert resp.status_code == 403


def test_denied_requests_are_not_redirects(client, project_id, monkeypatch):
    """被拒必须是 403，**不能是 302 跳登录页**。

    跳转的后果是前端拿到一张 HTML 登录页、`resp.json()` 抛解析错误，用户看到的是
    「响应格式不对」，而真实原因是没权限。
    """
    _allow(monkeypatch, access=False, admin=False)
    for method, url in (
        ("get", f"/ai-analysis/projects/{project_id}/config"),
        ("post", f"/ai-analysis/projects/{project_id}/config"),
        ("post", f"/ai-analysis/projects/{project_id}/models"),
    ):
        resp = getattr(client, method)(url, json={})
        assert resp.status_code in (401, 403), f"{url} 返回了 {resp.status_code}"


# ==========================================================================
# 读配置
# ==========================================================================


def test_config_read_returns_every_field_and_the_schema(client, project_id, monkeypatch):
    _allow(monkeypatch)
    with flask_app.app_context():
        ai_service.set_project_api_key(project_id, "super-secret-token")

    body = client.get(f"/ai-analysis/projects/{project_id}/config").get_json()

    assert body["success"] is True
    # 界面要用的 4 组字段都在
    for name in (
        "api_base_url",
        "api_model",
        "max_analysis_rounds",
        "max_tool_requests",
        "prompt_char_budget",
        "request_timeout_seconds",
        "min_severity",
        "min_confidence",
        "max_anomalies_per_run",
        "project_knowledge",
        "prompt_template",
    ):
        assert name in body, f"配置接口没有返回 {name}"

    schema = body["field_schema"]
    assert schema["max_analysis_rounds"]["label"] == "最大分析轮次"
    assert schema["max_analysis_rounds"]["min"] == 1
    assert schema["min_severity"]["choices"] == ["high", "critical"]

    assert body["api_key"]["configured"] is True
    assert body["source"] == "openai", "没配地址时应回落到预设"


def test_the_token_never_appears_in_any_response(client, project_id, monkeypatch):
    """**安全断言**：响应体里不能出现明文，也不给掩码（掩码会让人以为能对出来）。"""
    _allow(monkeypatch)
    with flask_app.app_context():
        ai_service.set_project_api_key(project_id, "super-secret-token")

    raw = client.get(f"/ai-analysis/projects/{project_id}/config").get_data(as_text=True)

    assert "super-secret-token" not in raw
    assert "***" not in raw
    assert "enc::" not in raw, "连密文也不该下发到浏览器"


# ==========================================================================
# 写配置
# ==========================================================================


def test_a_valid_update_succeeds(client, project_id, monkeypatch):
    _allow(monkeypatch)
    resp = client.post(
        f"/ai-analysis/projects/{project_id}/config",
        json={
            "api_base_url": "http://127.0.0.1:15721/v1",
            "api_model": "deepseek-v4-flash",
            "max_analysis_rounds": 4,
            "min_severity": "critical",
        },
    )

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    with flask_app.app_context():
        config = ai_service.get_project_analysis_config(project_id)
    assert config["api_model"] == "deepseek-v4-flash"
    assert config["max_analysis_rounds"] == 4


def test_an_invalid_update_returns_field_level_errors(client, project_id, monkeypatch):
    """**400 + errors[]**：界面据此把错误标到具体那一栏。

    只在顶部说一句「保存失败」会让用户不知道自己哪一栏填错了。
    """
    _allow(monkeypatch)
    resp = client.post(
        f"/ai-analysis/projects/{project_id}/config",
        json={"max_analysis_rounds": 999, "api_base_url": "ftp://bad"},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert {item["field"] for item in body["errors"]} == {"max_analysis_rounds", "api_base_url"}
    for item in body["errors"]:
        assert item["label"] and item["message"]


def test_an_invalid_update_writes_nothing(client, project_id, monkeypatch):
    _allow(monkeypatch)
    client.post(f"/ai-analysis/projects/{project_id}/config", json={"api_model": "good-model"})
    client.post(f"/ai-analysis/projects/{project_id}/config", json={"api_model": "y" * 300})

    with flask_app.app_context():
        config = ai_service.get_project_analysis_config(project_id)
    assert config["api_model"] == "good-model", "失败的那次不该动已存的配置"


# ==========================================================================
# 模型列表
# ==========================================================================


def test_models_are_returned_as_plain_ids(client, project_id, monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(ai_routes, "build_endpoint_client", _fake_client())
    monkeypatch.setattr(
        ai_routes,
        "probe_models",
        lambda _client: ProbeResult(
            ok=True, supported=True, message="已获取 2 个可用模型。", models=("a", "b")
        ),
    )

    body = client.post(f"/ai-analysis/projects/{project_id}/models", json={}).get_json()

    assert body["success"] is True
    assert body["ok"] is True
    assert body["models"] == ["a", "b"]


def test_an_unsupported_model_list_is_not_an_error(client, project_id, monkeypatch):
    """**这条是实测约束**：端点不支持 `/models` 时必须降级成「请手动填写」，
    而不是把配置流程卡住 —— 本机代理上能正常对话的 `deepseek-v4-flash` 就不在
    它自己返回的 84 条列表里。
    """
    _allow(monkeypatch)
    monkeypatch.setattr(ai_routes, "build_endpoint_client", _fake_client())
    monkeypatch.setattr(
        ai_routes,
        "probe_models",
        lambda _client: ProbeResult(
            ok=False,
            supported=False,
            message="该端点不支持获取模型列表，请手动填写模型名字。",
            detail="HTTP 404",
        ),
    )

    resp = client.post(f"/ai-analysis/projects/{project_id}/models", json={})
    body = resp.get_json()

    assert resp.status_code == 200, "取不到列表不是 HTTP 错误"
    assert body["success"] is True
    assert body["ok"] is False
    assert body["supported"] is False
    assert "手动填写" in body["message"]


def test_models_reports_incomplete_configuration_with_field_errors(client, project_id, monkeypatch):
    _allow(monkeypatch)

    def _incomplete(_pid, override=None):
        return None, [{"field": "api_base_url", "label": "接口地址", "message": "请先填写接口地址"}]

    monkeypatch.setattr(ai_routes, "build_endpoint_client", _incomplete)

    resp = client.post(f"/ai-analysis/projects/{project_id}/models", json={})
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["errors"][0]["field"] == "api_base_url"


# ==========================================================================
# 连接测试
# ==========================================================================


def test_a_successful_connection_test_reports_the_model(client, project_id, monkeypatch):
    _allow(monkeypatch)
    monkeypatch.setattr(ai_routes, "build_endpoint_client", _fake_client())
    monkeypatch.setattr(
        ai_routes,
        "probe_connection",
        lambda _client: ProbeResult(
            ok=True, message="连接成功，模型 deepseek-v4-flash 响应正常。", latency_ms=142
        ),
    )

    body = client.post(
        f"/ai-analysis/projects/{project_id}/test-connection",
        json={"api_base_url": "http://127.0.0.1:15721/v1", "api_model": "deepseek-v4-flash"},
    ).get_json()

    assert body["ok"] is True
    assert body["latency_ms"] == 142
    assert "deepseek-v4-flash" in body["message"]


def test_a_failed_connection_test_reports_the_reason_over_http_200(client, project_id, monkeypatch):
    """探测失败也是 HTTP 200：接口调用本身成功了，失败的是「这个端点连不上」。

    用 HTTP 状态码表达探测结果，会让前端无法区分「请求没发出去」和「发出去但连不上」。
    """
    _allow(monkeypatch)
    monkeypatch.setattr(ai_routes, "build_endpoint_client", _fake_client())
    monkeypatch.setattr(
        ai_routes,
        "probe_connection",
        lambda _client: ProbeResult(ok=False, message="连接失败：Connection refused"),
    )

    resp = client.post(f"/ai-analysis/projects/{project_id}/test-connection", json={})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is False
    assert "refused" in body["message"]


def test_the_probe_endpoints_never_echo_a_token_back(client, project_id, monkeypatch):
    """把密钥塞进请求体，响应里也不该出现它（服务端只回探测结论）。"""
    _allow(monkeypatch)
    monkeypatch.setattr(ai_routes, "build_endpoint_client", _fake_client())
    monkeypatch.setattr(
        ai_routes, "probe_models", lambda _client: ProbeResult(ok=True, models=("m",))
    )

    raw = client.post(
        f"/ai-analysis/projects/{project_id}/models",
        json={"api_key": "typed-secret-token", "api_model": "m"},
    ).get_data(as_text=True)

    assert "typed-secret-token" not in raw


def test_the_probe_endpoints_do_not_write_the_config(client, project_id, monkeypatch):
    """探测走的是「请求体里的临时值」，**不该顺手把配置存下来**。

    用户可能正在试一组还没定下来的参数；把他试的中间值写进库，会让「取消」变成
    「其实已经保存了」。
    """
    _allow(monkeypatch)
    monkeypatch.setattr(ai_routes, "build_endpoint_client", _fake_client())
    monkeypatch.setattr(
        ai_routes, "probe_models", lambda _client: ProbeResult(ok=True, models=("m",))
    )

    client.post(
        f"/ai-analysis/projects/{project_id}/models",
        json={"api_base_url": "http://127.0.0.1:9999/v1", "api_model": "typed"},
    )

    with flask_app.app_context():
        config = ai_service.get_project_analysis_config(project_id)
    assert config["api_model"] == ""
    assert config["api_base_url"] == ""


def test_the_config_response_is_json_serialisable(client, project_id, monkeypatch):
    """schema 是从 dataclass 序列化出来的，别把不可 JSON 化的东西漏进去。"""
    _allow(monkeypatch)
    raw = client.get(f"/ai-analysis/projects/{project_id}/config").get_data(as_text=True)
    json.loads(raw)  # 抛异常即失败


def test_the_api_key_route_reports_the_new_format(client, project_id, monkeypatch):
    """密钥状态要带上加密格式：界面据此提示「旧的 Windows 密文请在 Windows 上重存一次」。"""
    _allow(monkeypatch)
    resp = client.post(
        f"/ai-analysis/projects/{project_id}/api-key", json={"api_key": "secret-xyz"}
    )
    assert resp.status_code == 200

    body = client.get(f"/ai-analysis/projects/{project_id}/api-key/status").get_json()
    assert body["configured"] is True
    assert body["format"] == "fernet"

    with flask_app.app_context():
        record = AiProjectApiKey.query.filter_by(project_id=project_id).first()
    assert record.encrypted_key.startswith("enc::")
