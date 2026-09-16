# -*- coding: utf-8 -*-
"""`/healthz` 存活探针：数据库挂了必须返回 503，且不泄露任何信息。

## 为什么值得单独钉住

平台此前**没有**探针接口。运维只能拿 `/test` 或 `/` 当探针，两者都不是探针：

* `/test` 恒返回 `"服务器正常工作！"`，**完全不碰数据库** —— 数据库已挂、
  磁盘写满、迁移失败时它照样 200。分布式部署里这意味着负载均衡会把一个已经
  不可用的实例一直留在池子里，故障表现为「部分请求随机失败」，极难归因。
* `/` 要查询项目列表并渲染模板，把它当探针等于每次探测都触发一次业务查询。

一个「数据库挂了也返回 200」的探针比没有探针更糟：它把故障伪装成健康。
所以 `test_database_failure_returns_503` 是本文件的重点。

## 另一个钉住的点：端点必须注册在**活着的**蓝图上

平台里存在一套**从未被注册**的遗留路由包（`routes/__init__.py` 定义了
`main_bp` 等 5 个蓝图，`register_blueprints()` 全仓无调用方）。往
`routes/main_routes.py` 里加路由**不会生效，也不会报错** —— 实测加完
`GET /healthz` 仍然是 404。所以这里断言 `request.endpoint` 的全限定名，
确保实现挂在真正注册的 `core_management_routes` 上。
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app  # noqa: E402
from services.app_security_bootstrap_service import AUTH_EXEMPT_PATHS  # noqa: E402

# 探针响应体里**不允许**出现的子串（路径、版本、表名、连接串片段）。
_FORBIDDEN_IN_BODY = (
    "instance",
    "diff_platform",
    "sqlite",
    "mysql",
    ".db",
    "commits_log",
    "repository",
    "Traceback",
    "C:",
    "/",
    "\\",
)


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


class TestHealthzHappyPath:
    def test_returns_200_with_minimal_body(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json() == {"status": "ok", "database": "ok"}

    def test_body_is_tiny_and_fixed_shape(self, client):
        """响应体必须极小且字段固定 —— 探针日志不该被无意义内容撑爆。"""
        response = client.get("/healthz")
        body = response.get_json()
        assert set(body) == {"status", "database"}
        assert len(response.data) < 128, f"响应体 {len(response.data)} 字节，过大了"

    def test_body_leaks_nothing_sensitive(self, client):
        """不得泄露路径、版本、表名、连接串。"""
        text = client.get("/healthz").data.decode("utf-8")
        leaked = [token for token in _FORBIDDEN_IN_BODY if token in text]
        assert not leaked, (
            f"探针响应体里出现了不该有的内容 {leaked}：{text!r}\n"
            "探针是免鉴权接口，任何路径/版本/表名都等于对外泄露部署细节。"
        )


class TestHealthzReflectsDatabaseFailure:
    """核心断言：数据库不可用时必须 503。"""

    def test_database_failure_returns_503(self, client, monkeypatch):
        """让 `SELECT 1` 抛异常，模拟数据库挂掉/磁盘写满/连接池耗尽。"""
        import routes.core_management_routes as route_module

        def _boom():
            return False, RuntimeError("database is locked")

        monkeypatch.setattr(route_module, "_probe_database_ok", _boom)

        response = client.get("/healthz")
        assert response.status_code == 503, (
            "数据库不可用时探针返回了 200 —— 这正是「探针把故障伪装成健康」的形态，"
            "负载均衡会把坏实例一直留在池子里。"
        )
        assert response.get_json() == {"status": "degraded", "database": "unavailable"}

    def test_failure_reason_is_not_echoed_in_the_body(self, client, monkeypatch):
        """失败详情只进服务端日志，不返回给调用方；且日志里也要脱敏。"""
        import routes.core_management_routes as route_module

        def _boom():
            return False, RuntimeError("secret-dsn-user:pw@10.1.2.3/diff_platform")

        monkeypatch.setattr(route_module, "_probe_database_ok", _boom)
        text = client.get("/healthz").data.decode("utf-8")
        assert "secret-dsn-user" not in text
        assert "10.1.2.3" not in text
        assert "pw@10" not in text

    def test_failure_reason_is_redacted_in_the_log(self, client, monkeypatch):
        """日志里也不能出现连接串口令。

        数据库连接类异常的 `str()` 常常自带完整连接串，直接写日志等于把口令
        落到日志文件（实测未脱敏时日志里出现过 `user:pw@host/db`）。

        这里替换 `utils.safe_print.log_print`（路由是函数内 import，所以在调用
        时取模块属性），而不是抓 stdout —— 平台的 log_print 会经过日志队列/文件，
        stdout 上抓不到，断言会假通过。
        """
        import routes.core_management_routes as route_module
        import utils.safe_print as safe_print

        logged = []

        def _boom():
            return False, RuntimeError(
                "(pymysql) mysql+pymysql://admin:S3CR3T@10.1.2.3:3306/diff_platform"
            )

        monkeypatch.setattr(route_module, "_probe_database_ok", _boom)
        monkeypatch.setattr(
            safe_print,
            "log_print",
            lambda message, *a, **k: logged.append(str(message)),
        )

        assert client.get("/healthz").status_code == 503

        combined = "\n".join(logged)
        assert combined, "失败时一条日志都没有 —— 排查时看不到原因"
        assert "S3CR3T" not in combined, (
            f"健康检查日志里泄露了数据库口令：{combined!r}\n"
            "失败原因必须先过 utils.security_utils.sanitize_text 再落日志。"
        )
        assert "健康检查失败" in combined, "脱敏不该把整条日志吞掉 —— 排查还是要能看到原因"

    def test_real_probe_reports_failure_when_the_engine_is_broken(self, client, monkeypatch):
        """不 mock 探针本身，改 mock 引擎 —— 证明 `_probe_database_ok` 真的在探数据库。

        上面两条 mock 掉的是 `_probe_database_ok`，所以它们只证明「探针返回 False
        时路由给出 503」。这一条把 `_probe_database_ok` 内部依赖的引擎连接打坏，
        证明它是**真的**去连数据库、而不是恒返回 True。
        """
        from sqlalchemy.exc import OperationalError

        import routes.core_management_routes as route_module
        from flask_sqlalchemy import SQLAlchemy

        def _broken_engine(self):
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))

        monkeypatch.setattr(SQLAlchemy, "engine", property(_broken_engine))
        # 前提校验：探针内部真的会走到 db.engine。
        ok, exc = route_module._probe_database_ok()
        assert ok is False and exc is not None


class TestHealthzAccessControl:
    def test_anonymous_request_is_allowed(self, client):
        """探针拿不到会话 Cookie，必须免鉴权。"""
        assert client.get("/healthz").status_code == 200

    def test_path_is_registered_as_auth_exempt(self):
        assert "/healthz" in AUTH_EXEMPT_PATHS, (
            "/healthz 不在 AUTH_EXEMPT_PATHS 里 —— 登录门禁会把探针挡在 302/401，"
            "负载均衡看到的是一个永远不健康的实例。"
        )

    def test_route_declares_get_only(self):
        """只允许 GET。

        这里断言 url_map 上声明的方法，而不是发一个 POST 看状态码：本平台的
        405 会被错误处理链路转成 302（实测 POST /healthz → 302 Location: /），
        断言状态码等于在测错误处理器而不是在测探针。
        """
        methods = set()
        for rule in app.url_map.iter_rules():
            if str(rule) == "/healthz":
                methods |= set(rule.methods)
        assert "GET" in methods
        assert "POST" not in methods and "PUT" not in methods and "DELETE" not in methods, (
            f"/healthz 声明了写方法：{sorted(methods)} —— 探针只该是只读 GET"
        )

    def test_reports_no_csrf_requirement_for_get(self, client):
        """GET 不该因为缺 CSRF token 被拦（enforce_csrf 对 GET 直接放行）。"""
        response = client.get("/healthz", headers={"Origin": "http://evil.example"})
        # 同源校验在这里**可以**拒绝（返回 400），但绝不能因为缺 CSRF token 而 400；
        # 两种 400 的文案不同，这里按文案区分。
        if response.status_code == 400:
            body = response.data.decode("utf-8", errors="ignore")
            assert "CSRF token invalid or missing" not in body


class TestHealthzIsOnALiveBlueprint:
    def test_endpoint_is_fully_qualified_on_core_management_routes(self):
        """必须挂在真正注册的蓝图上。

        `routes/main_routes.py` 的 `main_bp` 从未被注册
        （`routes/__init__.py::register_blueprints()` 全仓无调用方），
        路由写在那里不会生效、也不会报错 —— 实测加完仍是 404。
        """
        matches = [
            rule for rule in app.url_map.iter_rules() if str(rule) == "/healthz"
        ]
        endpoints = {rule.endpoint for rule in matches}
        assert "core_management_routes.healthz" in endpoints, (
            f"/healthz 没有注册在 core_management_routes 上，实际端点：{endpoints}"
        )

    def test_healthz_is_reachable(self, client):
        """再钉一次「真的能访问到」—— 端点存在但 404 是这次踩过的坑。"""
        assert client.get("/healthz").status_code != 404
