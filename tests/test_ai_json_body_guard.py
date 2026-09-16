# -*- coding: utf-8 -*-
"""AI 接口：JSON 根节点不是对象时必须回 4xx，而不是 500。

## 为什么需要这个文件

`/ai-analysis/*` 的写接口此前统一写着 `payload = request.get_json(silent=True) or {}`，
然后直接 `payload.get(...)` / `dict(payload)`。这个 `or {}` 只兜 **falsy**，而 `[1]`、
`"str"`、`123`、`true` 都是 **truthy** —— 它们原样穿过，紧接着抛 AttributeError /
TypeError，被兜成 500：

    POST /ai-analysis/projects/<id>/api-key    [1]  → AttributeError: 'list' object has no attribute 'get'
    POST /ai-analysis/projects/<id>/config     [1]  → TypeError: cannot convert dictionary update sequence ...
    POST /ai-analysis/projects/<id>/models     [1]  → 同上（在 build_endpoint_client 里）
    POST /ai-analysis/projects/<id>/test-connection [1] → 同上

这不是理论边界情况：**一个登录用户改一下请求体就能打出来**，而且这四个接口里有两个会
带着项目密钥去请求用户填的地址 —— 解析失败必须发生在**建客户端之前**，否则就成了
「畸形请求也能触发一次出网」。

## 本文件守的三条性质

1. **结构错误 → 400**（沿用仓库既有约定 `{"success": False, "message": ...}`）。
2. **一条也不许打到外部端点**：用「一调用就断言失败」的 requests 代理证明
   `requests.request` 根本没被调用；并配一条**对照**（正常 body 时代理确实被调用），
   否则「没被调用」这个断言可能只是因为路径压根没走到。
3. **服务层被直接调用时也不许 500**：路由只是入口之一，脚本/后台任务会直接调
   `update_project_analysis_config` / `build_endpoint_client`。
"""
from __future__ import annotations

import uuid

import pytest
import requests as real_requests

import services.ai.llm_client as llm_client
import services.ai_analysis_service as ai_service
from app import app as flask_app
from app import create_tables, db
from auth.models import AuthUser, AuthUserProject
from models import Project
from services.ai.endpoint_service import (
    ConfigValidationError,
    FieldError,
    validate_payload,
)

# 根节点非对象的几种真实形态。`null` 与空 body **不在**这里：它们解析成 None，
# 等价于「没提交字段」，属于既有语义，另有专门的用例守着。
NON_OBJECT_BODIES = [
    pytest.param("[1]", id="array"),
    pytest.param('"str"', id="string"),
    pytest.param("123", id="number"),
    pytest.param("true", id="bool"),
    pytest.param('[{"api_model": "m"}]', id="array-of-objects"),
]

AI_WRITE_ENDPOINTS = [
    pytest.param("/api-key", id="api-key"),
    pytest.param("/config", id="config"),
    pytest.param("/models", id="models"),
    pytest.param("/test-connection", id="test-connection"),
]

# 会**带着密钥出网**的两个接口。结构校验必须在它们建客户端之前完成。
PROBE_ENDPOINTS = [
    pytest.param("/models", id="models"),
    pytest.param("/test-connection", id="test-connection"),
]


class _NoExternalHTTP:
    """把 `llm_client.requests` 换成「一调用就炸」的代理，并记下调用。

    不能直接 `monkeypatch.setattr(llm_client.requests, "request", ...)`：`llm_client`
    里的 `requests` 就是**全局 requests 模块**，那样会改到 pytest 自己和别的库。
    代理只替换这一个引用点，其余属性（`exceptions.*`）透传。

    `request` 用**断言**而不是返回假响应：本文件要证的是「根本没发出请求」，
    假响应会把「其实发出去了」掩盖成一次正常的失败。
    """

    def __init__(self):
        self.calls: list[tuple] = []

    def request(self, *args, **kwargs):  # pragma: no cover - 触发即失败
        self.calls.append((args, kwargs))
        raise AssertionError(f"畸形请求体却发起了外部请求：{args} {kwargs}")

    def __getattr__(self, name):
        return getattr(real_requests, name)


@pytest.fixture()
def no_external_http(monkeypatch, request):
    """把出网口换成代理。普通用例不需要调用计数时也用同一个 fixture。"""
    proxy = _NoExternalHTTP()
    monkeypatch.setattr(llm_client, "requests", proxy)
    return proxy


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def admin_client():
    """真实 test_client + 真实项目管理员身份（真实 ORM / 临时库）。

    刻意不 monkeypatch `_has_project_admin_access`：这一层要连鉴权一起验，
    否则「500 变 400」可能只是没走到 view。用数据库里的**项目**管理员
    （`auth_user_projects.role = 'admin'`）而不是平台管理员，避免「平台管理员短路
    返回 True」把项目级判定整条跳过。
    """
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("ai-body"))
        db.session.add(project)
        db.session.flush()

        user = AuthUser(
            username=_uid("pm"),
            password_hash="x",
            role="project_admin",
            is_active=True,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(
            AuthUserProject(user_id=user.id, project_id=project.id, role="admin")
        )
        db.session.commit()
        project_id, user_id, username = project.id, user.id, user.username

    client = flask_app.test_client()
    client.environ_base["HTTP_X_CSRFTOKEN"] = "test-csrf-token"
    with client.session_transaction() as sess:
        sess["auth_user_id"] = user_id
        sess["auth_username"] = username
        sess["auth_role"] = "project_admin"
        sess["_csrf_token"] = "test-csrf-token"
    client.project_id = project_id  # type: ignore[attr-defined]
    return client


def _post_raw(client, suffix: str, raw_body: str):
    """发**原始字节** body，而不是 `json=`。

    必须这样：`client.post(json=[1])` 由 flask 自己序列化，测不到「用户真的发了一个
    数组」这条路径 —— 而本文件关心的正是「请求体长什么样」。
    """
    return client.post(
        f"/ai-analysis/projects/{client.project_id}{suffix}",
        data=raw_body.encode("utf-8"),
        headers={"Content-Type": "application/json", "X-CSRFToken": "test-csrf-token"},
    )


def _prepare_saved_endpoint(project_id: int) -> None:
    """存一组**完整**的端点配置，让探测接口真的会去建客户端、发请求。"""
    ai_service.set_project_api_key(project_id, "super-secret-token")
    ok, message, errors = ai_service.update_project_analysis_config(
        project_id,
        {
            "api_base_url": "http://127.0.0.1:15721/v1",
            "api_model": "deepseek-v4-flash",
        },
    )
    assert ok, (message, errors)


# ==========================================================================
# AI 写接口：结构错误 → 400
# ==========================================================================


@pytest.mark.parametrize("suffix", AI_WRITE_ENDPOINTS)
@pytest.mark.parametrize("raw_body", NON_OBJECT_BODIES)
def test_a_non_object_json_root_is_rejected_with_400(
    admin_client, no_external_http, suffix, raw_body
):
    """四个 AI 写接口都必须回 400。

    改回 `request.get_json(silent=True) or {}` 再跑：这四条会变成 500
    （api-key 是 AttributeError、config/models/test-connection 是 TypeError）。
    """
    resp = _post_raw(admin_client, suffix, raw_body)

    assert resp.status_code == 400, (
        f"{suffix} 收到根节点非对象的 body（{raw_body}）应回 400，实际 {resp.status_code}"
    )
    body = resp.get_json()
    assert body["success"] is False
    assert body["message"], "要说清哪里错了，不能只给状态码"
    assert no_external_http.calls == [], "结构错误不该触发任何出网动作"


@pytest.mark.parametrize("suffix", PROBE_ENDPOINTS)
def test_a_broken_body_never_reaches_the_outbound_http_client(
    admin_client, no_external_http, suffix
):
    """**本文件最重要的断言**：探测接口带着项目密钥出网。

    这里把配置存全（地址 + 模型名 + Token），保证「不存配置所以提前返回 400」不会
    冒充成「校验住了」—— 存全之后唯一能拦住出网的就是 body 形状校验。
    """
    with flask_app.app_context():
        _prepare_saved_endpoint(admin_client.project_id)

    resp = _post_raw(admin_client, suffix, "[1]")

    assert resp.status_code == 400
    assert no_external_http.calls == [], (
        f"{suffix} 在 body 解析失败时仍然发起了外部请求：{no_external_http.calls}"
    )


@pytest.mark.parametrize("suffix", PROBE_ENDPOINTS)
def test_the_outbound_guard_can_actually_fail(admin_client, no_external_http, suffix):
    """对照：正常 body + 完整配置时，代理**确实**会被调用。

    没有这条，「没被调用」的断言可能只是路径压根没走到（比如配置没存上），
    于是整条断言永远为真、什么也没测。
    """
    with flask_app.app_context():
        _prepare_saved_endpoint(admin_client.project_id)

    _post_raw(admin_client, suffix, "{}")

    assert len(no_external_http.calls) == 1, (
        "正常请求体应当走到出网口，否则上面的「不出网」断言是空断言"
    )


@pytest.mark.parametrize("suffix", AI_WRITE_ENDPOINTS)
def test_null_and_absent_bodies_keep_their_old_behaviour(admin_client, suffix):
    """`null` 与空 body 解析成 None，等价于「没提交字段」—— **不是**结构错误。

    这是既有语义（`silent=True` + `or {}`），本次只改「根节点是别的类型」那条路，
    不能顺手把「没带 body」也变成 400：前端有接口就是空 body 调用的。
    """
    for raw in ("null", ""):
        resp = _post_raw(admin_client, suffix, raw)
        assert resp.status_code in (200, 400), (
            f"{suffix} 收到 {raw!r} 时不该是 {resp.status_code}：没带 body 属于既有语义"
        )
        assert resp.get_json()["success"] in (True, False)


def test_a_broken_config_body_writes_nothing(admin_client):
    """400 之外还要保证**没写库**：失败的一次不该留下半成品。"""
    with flask_app.app_context():
        ok, _message, _errors = ai_service.update_project_analysis_config(
            admin_client.project_id, {"api_model": "good-model"}
        )
        assert ok

    resp = _post_raw(admin_client, "/config", "[1]")
    assert resp.status_code == 400

    with flask_app.app_context():
        config = ai_service.get_project_analysis_config(admin_client.project_id)
    assert config["api_model"] == "good-model", "失败的那次不该动已存的配置"


def test_a_non_string_api_key_is_rejected_rather_than_coerced(admin_client):
    """Token 只有字符串一种形态。

    旧行为是 `str(123)` 兜底 → 200「已保存」，库里存的是 `"123"`、`"[1, 2]"` ——
    用户以为配上的东西根本不是他填的（同一套「不静默夹取」的取向，见
    `services/ai/endpoint_service.py` 的 `_coerce_int`）。
    """
    for raw in ("123", '[1, 2]', '{"a": 1}'):
        resp = _post_raw(admin_client, "/api-key", raw)
        assert resp.status_code == 400, f"api_key={raw} 应被拒绝，实际 {resp.status_code}"
        assert resp.get_json()["success"] is False

    with flask_app.app_context():
        status = ai_service.get_project_api_key_status(admin_client.project_id)
    assert status["configured"] is False, "被拒绝的值不该落库"


def test_a_non_object_body_is_rejected_before_the_would_be_write(admin_client, monkeypatch):
    """结构错误必须在**调用服务之前**就返回。

    换掉 `update_project_analysis_config`：它一旦被调用就说明路由把畸形 body
    交给了服务层。
    """
    import routes.ai_analysis_routes as ai_routes

    called: list = []
    monkeypatch.setattr(
        ai_routes,
        "update_project_analysis_config",
        lambda *args, **kwargs: called.append(args) or (True, "", []),
    )

    resp = _post_raw(admin_client, "/config", "[1]")

    assert resp.status_code == 400
    assert called == [], "结构错误不该走到服务层"


# ==========================================================================
# 服务层：被直接调用时也不许 500
# ==========================================================================


@pytest.mark.parametrize("payload", [[1], "str", 123, True, [{"a": 1}]])
def test_validate_payload_turns_a_non_mapping_into_a_structured_error(payload):
    """`dict(payload or {})` 对非 dict 抛的是 TypeError（→500），不是校验错误。

    改成 `ConfigValidationError` 之后，调用方拿到的是**同一个出口**的字段级错误。
    """
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_payload(payload)

    assert excinfo.value.errors, "要有可展示的错误明细"
    assert isinstance(excinfo.value.errors[0], FieldError)
    assert "JSON 对象" in str(excinfo.value)


def test_validate_payload_still_accepts_none_and_dicts():
    """None 等价于「没提交」—— 保持既有语义，不要顺手收紧成错误。"""
    assert validate_payload(None) == {}
    assert validate_payload({}) == {}
    assert validate_payload({"api_model": "m"}) == {"api_model": "m"}


def test_the_config_service_returns_errors_instead_of_raising(auth_project):
    """服务层直接被调用：`(False, 提示语, 错误明细)`，不抛异常、不写库。"""
    project_id = auth_project

    with flask_app.app_context():
        ok, message, errors = ai_service.update_project_analysis_config(project_id, [1])

        assert ok is False
        assert message
        assert errors and errors[0]["message"]
        # 失败的一次不该留下半成品。
        config = ai_service.get_project_analysis_config(project_id)
    assert config["api_model"] == ""


def test_the_probe_client_builder_returns_field_errors_instead_of_raising(auth_project):
    """同样是被直接调用的一条路：返回 `(None, errors)`，路由靠它回 400。"""
    client, errors = ai_service.build_endpoint_client(auth_project, [1])

    assert client is None
    assert errors and errors[0]["field"] == "__body__"
    assert "JSON 对象" in errors[0]["message"]


@pytest.fixture()
def auth_project():
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("svc"))
        db.session.add(project)
        db.session.commit()
        return project.id
