# -*- coding: utf-8 -*-
"""Agent 接口：身份凭据不外泄、管理按钮要过 CSRF、停/启后台任务得真的生效。

## 三条缺陷的共同形态

`/api/agents/` 这个前缀在**鉴权**和 **CSRF** 两处都被整段放行了
（`AUTH_EXEMPT_PATHS` 里的 `"/api/agents/"`、以及原先 CSRF 里那句
`request.path.startswith("/api/agents/")`），理由是 Agent 是机器进程、拿不到会话。
这个理由对 **Agent 调的那些接口**成立，但路径前缀把这个前缀底下**所有**接口都算进去了。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app, create_tables, db  # noqa: E402
from models import AgentNode  # noqa: E402

AGENT_SECRET = "agent-secret-for-this-test-file"


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _short(endpoint: str) -> str:
    return str(endpoint or "").rsplit(".", 1)[-1]


@pytest.fixture()
def agent_env(monkeypatch):
    monkeypatch.setenv("AGENT_SHARED_SECRET", AGENT_SECRET)
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    with app.app_context():
        create_tables()
        yield


def _register(client, *, agent_code, token_header=None, token_body=None):
    headers = {"X-Agent-Secret": AGENT_SECRET}
    if token_header is not None:
        headers["X-Agent-Token"] = token_header
    payload = {"agent_code": agent_code, "agent_name": agent_code, "project_codes": []}
    if token_body is not None:
        payload["agent_token"] = token_body
    return client.post("/api/agents/register", json=payload, headers=headers)


def _stored_token(agent_code):
    return AgentNode.query.filter_by(agent_code=agent_code).first().agent_token


class TestRegisterNeverHandsOutSomebodyElsesToken:
    """`/api/agents/register` 曾经**无条件**把 `agent.agent_token` 放进响应。

    `agent_token` 是平台唯一的**按节点**凭据（`_get_agent_by_identity` 用它把心跳、
    领任务、回传结果绑到具体节点上）。而这一层的门禁只是 `X-Agent-Secret` ——
    一把所有 Agent 共用的对称密钥。于是任何一个拿得到它的人（任何一个 Agent 节点、
    任何一个看过部署脚本的人）只要填上别人的 `agent_code`，就能领到那个节点的身份，
    然后以那个节点的名义领任务（任务 payload 里带着该仓库的明文口令）、
    伪造结果回传，全程无声。
    """

    def test_a_second_holder_without_the_token_gets_a_rotation_not_the_old_token(self, agent_env):
        code = _uid("victim")
        with app.test_client() as client:
            first = _register(client, agent_code=code)
            assert first.status_code == 200, first.get_data(as_text=True)
            original = first.get_json()["agent_token"]
            assert original
            assert first.get_json()["agent_token_rotated"] is True

            # 攻击者：有共享密钥、**没有**那个 token，冒充同一个 agent_code。
            attack = _register(client, agent_code=code)
            assert attack.status_code == 200
            handed_out = attack.get_json()["agent_token"]

            assert handed_out != original, "把旧的身份凭据交给了拿不出它的人"
            assert _stored_token(code) == handed_out, "轮换没有落库"

    def test_a_holder_that_proves_its_identity_keeps_the_same_token(self, agent_env):
        """带对了原 token → 原样奉还。**这条不能少**：否则同一个 `agent_code` 的
        两个实例（滚动重启期间同时活着）会互相轮换、永不停歇地互相踢下线。"""
        code = _uid("honest")
        with app.test_client() as client:
            first = _register(client, agent_code=code).get_json()["agent_token"]
            again = _register(client, agent_code=code, token_header=first)

            assert again.status_code == 200
            body = again.get_json()
            assert body["agent_token"] == first
            assert body["agent_token_rotated"] is False
            assert _stored_token(code) == first

    def test_the_token_may_be_presented_in_the_body_too(self, agent_env):
        """两种携带方式（`X-Agent-Token` 头 / 请求体字段）都要认 —— 别的 Agent 接口
        两个都读，注册这一条不能只认其中一个。"""
        code = _uid("bodytoken")
        with app.test_client() as client:
            first = _register(client, agent_code=code).get_json()["agent_token"]
            again = _register(client, agent_code=code, token_body=first)
            assert again.get_json()["agent_token"] == first
            assert again.get_json()["agent_token_rotated"] is False

    def test_the_lost_token_recovery_path_still_works(self, agent_env):
        """Agent 丢了 token（401 之后清空 token 重新注册，见 agent/runner.py 的
        `agent_token = ""` 分支）必须能拿到新的 —— 这条恢复路径是**被设计过的**，
        轮换不能把它堵死。"""
        code = _uid("lost")
        with app.test_client() as client:
            stale = _register(client, agent_code=code).get_json()["agent_token"]
            _register(client, agent_code=code)          # 别人把它轮换掉了
            recovered = _register(client, agent_code=code, token_header=stale)

            assert recovered.status_code == 200
            fresh = recovered.get_json()["agent_token"]
            assert fresh != stale
            assert fresh == _stored_token(code)

    def test_a_wrong_token_is_compared_in_constant_time(self):
        """这一处是**凭据比较**，必须走 `hmac.compare_digest`（同
        `_validate_agent_shared_secret`）。两串凭据直接用 `==` 比会按字节短路，
        泄漏「前 N 位猜对了」的计时信号。"""
        source = open(
            os.path.join(PROJECT_ROOT, "services", "agent_management_handlers.py"),
            encoding="utf-8-sig",
        ).read()
        marker = "presented_token and agent.agent_token and hmac.compare_digest("
        assert marker in source, "身份凭据的比较没有走定长时间比较"


class TestTheAdminButtonsBehindTheAgentPrefixNeedCsrf:
    """`ignore_agent_incident` 与 `rollback_agent_release` 是**管理员在网页上点的按钮**：
    只要求 `@require_admin`、不校验 agent secret，却因为 `startswith("/api/agents/")`
    一并免掉了 CSRF。管理员登录着的时候访问一个恶意页面（或同一可注册域下的任意页面），
    那个页面 POST 过来 —— 浏览器带上 session cookie、`@require_admin` 通过，
    「忽略所有告警」「回滚 Agent 版本」就被点掉了。
    """

    _AGENT_HANDLERS = frozenset({
        "register_agent_node",
        "agent_heartbeat",
        "agent_report_incident",
        "agent_upsert_temp_cache",
        "agent_get_latest_release",
        "agent_download_release_package",
        "agent_claim_task",
        "agent_report_task_result",
        "ignore_agent_incident",
        "rollback_agent_release",
        "resolve_agent_temp_cache",
        "list_agent_nodes",
        "list_agent_incidents",
        "list_agent_tasks",
        "list_agent_releases",
        "agent_abnormal_summary",
        "get_agent_temp_cache",
        "agent_overview_page",
    })

    def test_what_the_url_map_actually_names_these_endpoints(self):
        """名单按 url_map 里的 endpoint 名比对，而同一个 handler 在本仓注册了**两份**
        （蓝图前缀那份 + 短名别名那份，实际命中哪一份由 Werkzeug 的排序决定）。
        所以比对取末段 —— 只认一种写法会在某次路由调整后静默失效。"""
        adapter = app.url_map.bind("localhost")
        for path, method in (
            ("/api/agents/heartbeat", "POST"),
            ("/api/agents/register", "POST"),
            ("/api/agents/tasks/claim", "POST"),
            ("/api/agents/incidents/1/ignore", "POST"),
            ("/api/agents/releases/admin/rollback", "POST"),
        ):
            endpoint, _ = adapter.match(path, method=method)
            assert _short(endpoint) in self._AGENT_HANDLERS, (path, endpoint)

    def test_only_the_agent_authenticated_endpoints_are_exempt(self):
        from services.app_security_bootstrap_service import AGENT_SECRET_ENDPOINTS

        assert "agent_heartbeat" in AGENT_SECRET_ENDPOINTS
        assert "register_agent_node" in AGENT_SECRET_ENDPOINTS
        assert "agent_claim_task" in AGENT_SECRET_ENDPOINTS
        # 管理按钮**不在**名单里 —— 这正是这条修复的全部意义
        assert "ignore_agent_incident" not in AGENT_SECRET_ENDPOINTS
        assert "rollback_agent_release" not in AGENT_SECRET_ENDPOINTS
        assert "resolve_agent_temp_cache" not in AGENT_SECRET_ENDPOINTS

    def test_the_csrf_exemption_is_not_a_path_prefix(self):
        """**这一条是根因**：CSRF 豁免的判据不能再是 `request.path.startswith`。

        前缀会把「以后新增的任何 `/api/agents/` 接口」一起放行 —— 失败开放；
        按 endpoint 列白名单是失败关闭：新接口忘了列进来，表现是「Agent 调不通 +
        日志里一行 CSRF 校验失败」，一眼看得见，而不是静默少一道防护。
        """
        source = open(
            os.path.join(PROJECT_ROOT, "services", "app_security_bootstrap_service.py"),
            encoding="utf-8-sig",
        ).read()
        start = source.index("def enforce_csrf")
        end = source.index("@app.errorhandler", start)
        csrf_body = source[start:end]
        # 只在这段 CSRF 逻辑里找。`AUTH_EXEMPT_PATHS` 里那条 `"/api/agents/"` 是
        # **按设计保留的**：Agent 拿不到会话，登录门禁那一层必须放行它。
        assert 'startswith("/api/agents/")' not in csrf_body, (
            "CSRF 豁免又变回路径前缀了 —— ignore_agent_incident / "
            "rollback_agent_release 会跟着一起被放行"
        )
        assert "_is_agent_secret_endpoint()" in csrf_body

    def test_a_session_write_without_a_csrf_token_is_refused(self, agent_env):
        """**真的发一次请求**，用会话身份（不是 ADMIN_API_TOKEN —— 那条在 CSRF 之前
        就 return 了）且不带 token → 必须被挡。这条证明的是「挡得住」，
        而不是只证明「代码里没写 startswith」。"""
        with app.app_context():
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_csrf_token"] = "session-csrf-token"
                    sess["is_admin"] = True
                resp = client.post(
                    "/api/agents/incidents/1/ignore",
                    json={"ignored": True},
                )
                assert resp.status_code == 400, (
                    f"不带 CSRF token 的管理员写请求应当被挡（400），"
                    f"得到 {resp.status_code}: {resp.get_data(as_text=True)[:200]}"
                )
                assert "CSRF" in resp.get_data(as_text=True)

    def test_the_same_write_goes_through_once_the_token_is_present(self, agent_env):
        """反过来也要成立：带上 token 之后**必须不再是 400**。

        少了这一条，上面那条「挡得住」可以靠「把整个接口弄坏」通过 ——
        而 `templates/admin_agents.html` 的忽略按钮**今天就在用**这个接口。"""
        with app.app_context():
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_csrf_token"] = "session-csrf-token"
                    sess["is_admin"] = True
                resp = client.post(
                    "/api/agents/incidents/1/ignore",
                    json={"ignored": True},
                    headers={"X-CSRF-Token": "session-csrf-token"},
                )
                # 告警 1 不存在 → 业务上的 404；关键是 CSRF 这一层过了
                assert resp.status_code != 400, resp.get_data(as_text=True)[:200]

    def test_an_unknown_path_still_answers_404_not_400(self, agent_env):
        """**没有 handler 会执行的请求不该被 CSRF 拦。**

        这是把前缀豁免改成 endpoint 白名单时踩出来的回归：
        `/api/agents/tasks/<id>/execute-proxy` 是**已经下线**的接口，
        `request.endpoint` 为 None → 落进 CSRF 校验 → Agent 收到
        「CSRF token invalid or missing」，而真正的问题是「这条路径不存在」。

        它同时钉住了另一个更一般的口径：**CSRF 拦的是「跨站页面驱动一个真实的
        handler」**；路径不存在（404）或方法不对（405）时没有任何 handler 会跑，
        也就没有可拦的东西。
        """
        with app.app_context():
            with app.test_client() as client:
                resp = client.post(
                    "/api/agents/tasks/123/execute-proxy",
                    json={},
                    headers={"X-Agent-Secret": AGENT_SECRET},
                )
                assert resp.status_code == 404, (
                    f"不存在的路径应当回 404，得到 {resp.status_code}: "
                    f"{resp.get_data(as_text=True)[:200]}"
                )

    def test_a_method_mismatch_still_answers_405_not_400(self, agent_env):
        """同上，方法不对（405）时也没有 handler 会跑。"""
        with app.app_context():
            with app.test_client() as client:
                # `/api/agents/tasks` 只有 GET
                resp = client.post(
                    "/api/agents/tasks",
                    json={},
                    headers={"X-Agent-Secret": AGENT_SECRET},
                )
                assert resp.status_code == 405, (
                    f"方法不对应当回 405，得到 {resp.status_code}: "
                    f"{resp.get_data(as_text=True)[:200]}"
                )

    def test_a_real_handler_is_still_csrf_protected(self, agent_env):
        """**非空自检**：上面两条不能靠「CSRF 整个关掉」通过 ——
        命中真实 handler 的写请求仍然必须被挡。"""
        with app.app_context():
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_csrf_token"] = "session-csrf-token"
                    sess["is_admin"] = True
                resp = client.post(
                    "/api/agents/releases/admin/rollback",
                    json={},
                )
                assert resp.status_code == 400, (
                    f"命中真实 handler 的写请求必须过 CSRF，得到 {resp.status_code}"
                )
                assert "CSRF" in resp.get_data(as_text=True)
