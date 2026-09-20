# -*- coding: utf-8 -*-
"""同类模式扫描：全仓「JSON 根节点非对象 → 500」的写接口。

## 这条模式的来源

`payload = request.get_json(silent=True) or {}` 后面直接 `.get(...)`。`or {}` 只兜
**falsy**，`[1]` / `"str"` / `123` 都是 truthy，于是 AttributeError 一路冒到 Flask 的
默认处理器，变成 500；少数 handler 更糟 —— 它把 `'list' object has no attribute 'get'`
这句**内部异常文本**当 message 回给调用方。

排查时把 `request.get_json(...)` 的调用点全列了一遍，逐个用真实 test_client 发 `[1]`
确认。结论分三类：

* **裸奔（会 500）**：本文档下面覆盖的那些 —— 已统一改用
  `utils/json_body.read_json_object()`（一处实现、各调用点一行）。
* **已自带守卫**：`services/commit_operation_handlers.py`、
  `services/commit_status_api_service.py`、`services/repository_update_api_service.py`、
  `services/commit_diff_input_models.py`、`services/core_navigation_handlers.py` 里
  都写了 `if not isinstance(data, dict): return 4xx`，实测确实是 400，不动。
* **CSRF 取值**：`utils/request_security._csrf_token_from_request` 同样在根节点非对象时
  `.get` 崩掉。它跑在 **before_request**、每个非 GET 请求都会经过，所以畸形 body 的代价
  是「所有写接口一律 500」。已在原处收紧（见本文件最后一条）。

## 为什么这些用例都带「不出网/不写库」的断言

结构错误应当在**读懂 body 的第一时间**返回。`/api/agents/*` 这几个接口既不需要登录、
也不做 CSRF 校验（Agent 装机与探针场景），是最容易被无脑刷到的一组；它们的处理函数
里有 `db.session.commit()`，如果校验晚于落库，畸形请求反而会留下数据。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app import app as flask_app
from app import create_tables, db
from auth.models import AuthUser
from models import Project, Repository, WeeklyVersionConfig

AGENT_SECRET = "sweep-secret"

# (方法, 路径模板, 是否需要 Agent 共享密钥)
BARE_ENDPOINTS = [
    pytest.param("POST", "/api/agents/register", True, id="agent-register"),
    pytest.param("POST", "/api/agents/heartbeat", True, id="agent-heartbeat"),
    pytest.param("POST", "/api/agents/incidents/report", True, id="agent-incident-report"),
    pytest.param("POST", "/api/agents/cache/upsert", True, id="agent-cache-upsert"),
    pytest.param("POST", "/api/agents/releases/latest", True, id="agent-release-latest"),
    pytest.param("POST", "/api/agents/releases/admin/rollback", True, id="agent-release-rollback"),
    pytest.param("POST", "/api/agents/tasks/claim", True, id="agent-task-claim"),
    pytest.param("POST", "/api/agents/tasks/1/result", True, id="agent-task-result"),
    pytest.param("POST", "/api/agents/incidents/1/ignore", False, id="agent-incident-ignore"),
    pytest.param("POST", "/repositories/update-order", False, id="repository-update-order"),
    pytest.param("POST", "/repositories/swap-order", False, id="repository-swap-order"),
    pytest.param("POST", "/weekly-version-config/{config_id}/batch-confirm", False, id="weekly-batch-confirm"),
    pytest.param("POST", "/weekly-version-config/{config_id}/file-status", False, id="weekly-file-status"),
    pytest.param("POST", "/projects/{project_id}/weekly-version-config/api", False, id="weekly-config-create"),
    pytest.param("PUT", "/projects/{project_id}/weekly-version-config/api/{config_id}", False, id="weekly-config-update"),
    pytest.param("POST", "/admin/excel-cache/clear-project-cache", False, id="cache-clear-project"),
]

NON_OBJECT_BODIES = [
    pytest.param("[1]", id="array"),
    pytest.param('"str"', id="string"),
    pytest.param("123", id="number"),
]


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def admin_client(monkeypatch):
    """真实 test_client + 平台管理员（真实 ORM / 临时库），并配好 Agent 共享密钥。

    用平台管理员是必须的：`/repositories/*`、`/admin/*` 这类接口在 before_request
    里就要求平台管理员，普通项目成员会被挡在 view 之前 —— 那样测到的是 403，
    而不是「body 形状」这条路径。
    """
    monkeypatch.setenv("AGENT_SHARED_SECRET", AGENT_SECRET)

    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("sweep"))
        db.session.add(project)
        db.session.flush()

        user = AuthUser(
            username=_uid("admin"),
            password_hash="x",
            role="platform_admin",
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
        project_id, user_id, username = project.id, user.id, user.username

        repository = Repository(
            project_id=project_id,
            name=_uid("repo"),
            type="git",
            url="https://example.com/sweep.git",
            branch="main",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()
        config = WeeklyVersionConfig(
            project_id=project_id,
            repository_id=repository.id,
            name="W1",
            description="",
            branch="main",
            start_time=datetime(2026, 3, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 7, tzinfo=timezone.utc),
            cycle_type="custom",
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["auth_user_id"] = user_id
        sess["auth_username"] = username
        sess["auth_role"] = "platform_admin"
        sess["_csrf_token"] = "test-csrf-token"
    client.project_id = project_id  # type: ignore[attr-defined]
    client.config_id = config_id  # type: ignore[attr-defined]
    return client


def _call_raw(client, method: str, url: str, raw_body: str, *, agent: bool):
    return client.open(
        url,
        method=method,
        data=raw_body.encode("utf-8"),
        headers=(
            {"Content-Type": "application/json", "X-CSRFToken": "test-csrf-token"}
            | ({"X-Agent-Secret": AGENT_SECRET} if agent else {})
        ),
    )


@pytest.mark.parametrize(("method", "template", "agent"), BARE_ENDPOINTS)
@pytest.mark.parametrize("raw_body", NON_OBJECT_BODIES)
def test_a_non_object_json_root_returns_4xx_not_500(
    admin_client, method, template, agent, raw_body
):
    """改回 `request.get_json(...) or {}` 再跑：这些会变成 500（或把内部异常文本
    当 message 回 400）。4xx 之外的任何状态码都算回归。"""
    url = template.format(
        project_id=admin_client.project_id, config_id=admin_client.config_id
    )

    resp = _call_raw(admin_client, method, url, raw_body, agent=agent)

    assert 400 <= resp.status_code < 500, (
        f"{method} {url} 收到 {raw_body} 应回 4xx，实际 {resp.status_code}"
    )
    body = resp.get_json()
    assert body is not None, "错误响应必须是 JSON"
    assert body.get("message"), "要说清哪里错了"


def test_the_error_body_is_not_the_internal_exception_text(admin_client):
    """`agent_upsert_temp_cache` 原先回的是 `str(exc)` —— 也就是
    `'list' object has no attribute 'get'`。那句话对调用方毫无用处，还顺带把内部
    结构暴露出去；统一走 `read_json_object` 之后换成固定文案。
    """
    resp = _call_raw(
        admin_client, "POST", "/api/agents/cache/upsert", "[1]", agent=True
    )

    body = resp.get_json()
    assert "object has no attribute" not in body["message"]
    assert "JSON 对象" in body["message"]


def test_the_error_body_carries_both_error_conventions(admin_client):
    """仓库里两种判定并存：多数接口读 `data.success`，仓库排序那种页面读
    `data.status === 'success'`。只给一个，另一类前端会把错误显示成「未知错误」。"""
    resp = _call_raw(
        admin_client, "POST", "/repositories/update-order", "[1]", agent=False
    )

    body = resp.get_json()
    assert body["success"] is False
    assert body["status"] == "error"


# ==========================================================================
# CSRF 取值：跑在每个写请求之前的那一层
# ==========================================================================


@pytest.mark.parametrize("raw_body", NON_OBJECT_BODIES)
def test_a_broken_body_does_not_500_inside_the_csrf_check(admin_client, raw_body):
    """没有 CSRF 头时，请求会先经过 `_csrf_token_from_request`。

    那里同样写着 `request.get_json(silent=True) or {}` 然后 `.get("_csrf_token")` ——
    它挂在 `before_request` 上，**每个非 GET 请求都会走**，所以畸形 body 的代价不是
    「某个接口 500」而是「所有写接口一律 500」。
    """
    resp = admin_client.post(
        "/repositories/update-order",
        data=raw_body.encode("utf-8"),
        headers={"Content-Type": "application/json"},  # 刻意不带 CSRF 头
    )

    assert resp.status_code == 400, "应当是「CSRF token 缺失」的 400，不是 500"
    assert "CSRF" in resp.get_json()["message"]


def test_the_csrf_check_still_accepts_a_token_from_the_body(admin_client):
    """收紧形状判定时不能把「token 放在 body 里」这条既有路径弄丢。"""
    resp = admin_client.post(
        "/repositories/update-order",
        json={"_csrf_token": "test-csrf-token"},
    )

    assert resp.status_code != 400 or "CSRF" not in resp.get_json()["message"], (
        "body 里带了正确 token，不该被判成 CSRF 失败"
    )


def test_a_json_object_body_still_reaches_the_view(admin_client):
    """对照：正常对象 body 时要走到 view（否则上面的断言只是因为「压根没走到」）。

    这里用「缺少必要参数」的 400 作为证据 —— 它来自 view 内部，说明 before_request
    的 CSRF 与鉴权都放行了。
    """
    resp = admin_client.post(
        "/repositories/update-order",
        json={},
        headers={"X-CSRFToken": "test-csrf-token"},
    )

    assert resp.status_code == 400
    assert resp.get_json()["message"] == "缺少必要参数"


# ==========================================================================
# 没有 body 的请求：**不能**按「body 不是合法 JSON」判
# ==========================================================================


def test_a_body_less_request_is_not_treated_as_a_malformed_body(admin_client):
    """**回归**：带着 `Content-Type: application/json` 发一个没有 body 的 GET。

    周版本配置页面的 `editConfig` 就是这么发的，而蓝图的 `before_request` 原先写的是
    「`request.is_json` 为假时放行」—— `is_json` **只看 Content-Type**，说明不了有没有
    body，于是这条正常的读请求被判成 400，且响应是 Flask 默认的 **HTML**：
    前端 `response.json()` 先抛异常，只落到 `.catch`，用户看到的是
    「获取配置信息失败，请重试」，服务端的 message 一个字都读不到。
    同一页面上 `deleteConfig`（DELETE）是一模一样的写法，所以删除也一起失效。

    修法是把判据换成「有没有 body」：没有 body 就没有形状可校验。
    """
    url = f"/projects/{admin_client.project_id}/weekly-version-config/api/{admin_client.config_id}"

    resp = admin_client.get(url, headers={"Content-Type": "application/json"})

    assert resp.status_code == 200, (
        f"没有 body 的 GET 被判成了 {resp.status_code}：{resp.get_data(as_text=True)[:120]!r}"
    )
    assert resp.get_json()["success"] is True


def test_a_body_less_delete_is_not_judged_by_its_body(admin_client):
    """同一个坑的另一半：`deleteConfig` 也是「声明了 JSON、没有 body」的 DELETE。

    这里用一个**不存在的 config id** 去打：断言的是「**不是**因为 body 形状被拒」，
    所以期望 404（`first_or_404`），不是 400。用真的 id 会把配置删掉，
    后面同一用例里的对照就跑不起来了。
    """
    url = f"/projects/{admin_client.project_id}/weekly-version-config/api/99999999"

    # **要带 CSRF**：DELETE 不是安全方法，`enforce_csrf` 会先跑。不带的话 400 来自
    # CSRF，这条用例就变成了在测另一件事（第一版就是这么写的，看到 400 还以为守卫没修好）。
    resp = admin_client.delete(
        url,
        headers={
            "Content-Type": "application/json",
            "X-CSRFToken": "test-csrf-token",
        },
    )

    assert resp.status_code == 404, (
        f"没有 body 的 DELETE 走到 404 之前就被拦成了 {resp.status_code}"
    )


def test_a_non_object_body_is_still_rejected_by_the_same_blueprint(admin_client):
    """**反自检**：放宽「没有 body」不能顺手把「有 body 但根节点不是对象」也放了。

    少了这一条，一个「直接删掉整个 before_request」的实现能让上面两条全绿。
    """
    url = f"/projects/{admin_client.project_id}/weekly-version-config/api/{admin_client.config_id}"

    resp = _call_raw(admin_client, "PUT", url, "[1]", agent=False)

    assert resp.status_code == 400
    assert "JSON 对象" in resp.get_json()["message"]
