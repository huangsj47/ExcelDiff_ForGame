# -*- coding: utf-8 -*-
"""安全门禁表必须命中**真实的** `request.endpoint`。

## 为什么需要这个测试

`services/app_security_bootstrap_service.py` 里有两张平台管理员门禁表
（`SENSITIVE_ENDPOINTS` / `WRITE_PROTECTED_ENDPOINTS`），判定处写的是：

    if request.path.startswith("/admin/") or request.endpoint in SENSITIVE_ENDPOINTS:

而两张表原先写的是**裸 endpoint 名**（`"delete_repository"` …共 19 条）。
Flask 给蓝图路由的 endpoint **总是**带蓝图前缀 —— 即使显式传了 `endpoint=`：

    @core_management_bp.route("/repositories/<id>/delete", methods=["POST"],
                              endpoint="delete_repository")

实测 `app.url_map.bind('localhost').match('/repositories/1/delete', method='POST')`
返回的是 **`('core_management_routes.delete_repository', {...})`**，
`"delete_repository" in SENSITIVE_ENDPOINTS` 看似成立、实际那个字符串
从不等于 `request.endpoint` —— 两张表**整张是死代码**。

之所以不容易看出来：`services/app_routing_bootstrap_service.py` 会额外注册
**121 条裸名别名规则**（`[TRACE] Registered 121 endpoint short-name aliases`），
于是裸名是一个合法的 `url_for` 目标，看起来「名字是对的」。但 Werkzeug 匹配时
返回的是蓝图全限定名，别名规则只是陪跑。

`edit_repository` 就栽在这上面：它**既**没有函数体权限判断、**又**靠这张失效的表
兜底 → 任意已登录用户可读任意项目的仓库连接配置（已在
`services/repository_misc_page_service.py` 补上项目级判定）。

## 本文件断言什么

1. 两张表里的名字**全部**存在且是**全限定**形式，并且用真实 URL 反查匹配结果
   必须等于表里的名字（这条会在「有人又写回裸名」时立刻变红）；
2. 用合成蓝图复现「蓝图前缀」这一机制本身，锁死上面那条结论的成因；
3. 门禁真的会拦人（非管理员 → 403），而**项目级页面不会被误伤**
   （`projects` 的 POST 走 `has_project_create_access` 而不是平台管理员）。

「会写库的路由不得允许 GET」是另一类问题，见 `tests/test_write_routes_are_not_get.py`。
"""
from __future__ import annotations

import os
import re
import sys

import pytest
from flask import Blueprint, Flask

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import services.app_security_bootstrap_service as security_bootstrap  # noqa: E402
from services.app_security_bootstrap_service import (  # noqa: E402
    SENSITIVE_ENDPOINTS,
    WRITE_PROTECTED_ENDPOINTS,
)

ALL_GUARDED = sorted(SENSITIVE_ENDPOINTS | WRITE_PROTECTED_ENDPOINTS)

# 项目级页面：**刻意**不在平台管理员门禁表里（见第 3 组用例）。
PROJECT_SCOPED_PAGES = {
    "edit_repository",
    "add_git_repository",
    "add_svn_repository",
}


@pytest.fixture(scope="module")
def real_app():
    """真实应用（conftest 已把测试库隔离到 .pytest_tmp 下）。"""
    from app import app as flask_app

    return flask_app


def _rules_by_endpoint(app):
    by_endpoint = {}
    for rule in app.url_map.iter_rules():
        by_endpoint.setdefault(rule.endpoint, []).append(rule)
    return by_endpoint


def _representative_path(rule):
    """把 `/repositories/<int:repository_id>/delete` 变成 `/repositories/1/delete`。"""
    path = re.sub(r"<[^:<>]+:[^<>]+>", "1", rule.rule)
    path = re.sub(r"<[^<>]+>", "1", path)
    return path or "/"


def _match_method(rule):
    candidates = sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"})
    for preferred in ("POST", "GET"):
        if preferred in candidates:
            return preferred
    return candidates[0] if candidates else "GET"


class TestGuardTablesPointAtRealEndpoints:
    """表里的每个名字都必须能「真的」出现在 request.endpoint 上。"""

    def test_no_bare_endpoint_names(self):
        """全限定形式是硬要求 —— 裸名永远匹配不上 request.endpoint。"""
        bare = sorted(name for name in ALL_GUARDED if "." not in name)
        assert not bare, (
            f"门禁表里出现了裸 endpoint 名：{bare}\n"
            f"Flask 的 request.endpoint 始终是 `<blueprint>.<name>`（哪怕显式传了 "
            f"endpoint=）。要写 `core_management_routes.delete_repository`，"
            f"不是 `delete_repository`。"
        )

    def test_every_entry_exists_in_url_map(self, real_app):
        known = set(_rules_by_endpoint(real_app))
        missing = sorted(name for name in ALL_GUARDED if name not in known)
        assert not missing, (
            f"门禁表里这些 endpoint 在应用里根本不存在（改名/删除后表没跟着改）：{missing}"
        )

    def test_matched_endpoint_equals_table_entry(self, real_app):
        """核心断言：用真实 URL 反查，Werkzeug 匹配出的 endpoint 必须等于表里的名字。

        这条是「裸名回归」的直接探测器 —— 若把表改回裸名，
        `match()` 仍返回 `core_management_routes.delete_repository`，
        于是它不在（裸名版的）表里，用例立刻变红。
        """
        by_endpoint = _rules_by_endpoint(real_app)
        binder = real_app.url_map.bind("localhost")
        problems = []
        for name in ALL_GUARDED:
            rules = by_endpoint.get(name) or []
            assert rules, f"{name} 没有任何 URL rule"
            matched_any = False
            for rule in rules:
                path = _representative_path(rule)
                method = _match_method(rule)
                try:
                    endpoint, _args = binder.match(path, method=method)
                except Exception as exc:  # noqa: BLE001 - 报错信息要带上原因
                    problems.append(f"{name}: {method} {path} 无法匹配（{exc}）")
                    continue
                if endpoint == name:
                    matched_any = True
                    break
                problems.append(
                    f"{name}: {method} {path} 实际匹配到 {endpoint!r}"
                )
            if not matched_any:
                continue
        assert not problems, (
            "门禁表里有 endpoint 永远不会被 request.endpoint 命中：\n  "
            + "\n  ".join(problems)
        )

    def test_project_scoped_pages_are_deliberately_excluded(self):
        """项目级页面不能进平台管理员表 —— 那会把项目管理员挡在门外。

        `edit_repository` / `add_git_repository` / `add_svn_repository` 都是项目级
        操作，函数体内用 `_has_project_access` / `_has_project_admin_access` 判权
        （后者允许项目管理员）。它们原先列在表里，只是因为表失效才没造成回退；
        一旦按上面的方式把表修好，它们就必须移出去。
        """
        offenders = sorted(
            name
            for name in ALL_GUARDED
            if name.rsplit(".", 1)[-1] in PROJECT_SCOPED_PAGES
        )
        assert not offenders, (
            f"{offenders} 是项目级页面，放进门禁表会让项目管理员无法使用。"
            f"它们的判权应留在函数体内（如 "
            f"`services/repository_misc_page_service.py::render_edit_repository_page`）。"
        )

    def test_project_scoped_pages_do_their_own_access_check(self):
        """移出表之后，函数体内的判权必须真的存在。"""
        source_path = os.path.join(PROJECT_ROOT, "services", "core_navigation_handlers.py")
        source = open(source_path, encoding="utf-8").read()
        for func_name, expected in (
            ("add_git_repository", "_has_project_admin_access"),
            ("add_svn_repository", "_has_project_admin_access"),
            ("repository_config", "_has_project_access"),
        ):
            body_at = source.index(f"def {func_name}(")
            body = source[body_at : body_at + 400]
            assert expected in body, (
                f"core_navigation_handlers.{func_name} 里找不到 {expected} 判权 —— "
                f"它已被移出平台管理员门禁表，函数体内必须自带判权。"
            )

        misc_path = os.path.join(PROJECT_ROOT, "services", "repository_misc_page_service.py")
        misc = open(misc_path, encoding="utf-8").read()
        assert "_has_project_access(project.id)" in misc, (
            "render_edit_repository_page 必须按项目判权限 —— 它原先完全没有鉴权，"
            "唯一的兜底是那张失效的门禁表。"
        )


class TestBlueprintPrefixMechanism:
    """把「蓝图前缀」这一机制本身固定下来，免得后人以为是测试环境差异。"""

    def test_blueprint_endpoint_gets_prefixed_even_with_explicit_endpoint(self):
        app = Flask(__name__)
        bp = Blueprint("core_management_routes", __name__)

        @bp.route("/repositories/<int:repository_id>/delete", methods=["POST"], endpoint="delete_repository")
        def _delete_repository(repository_id):  # pragma: no cover - 只用来注册
            return "ok"

        app.register_blueprint(bp)
        endpoint, _args = app.url_map.bind("localhost").match(
            "/repositories/1/delete", method="POST"
        )
        assert endpoint == "core_management_routes.delete_repository", (
            "Flask 的行为变了？下面所有门禁表的写法都建立在这个结论上。"
        )
        assert endpoint != "delete_repository", (
            "裸名曾被误认为就是 request.endpoint —— 这正是两张表失效的原因。"
        )


def _configure_guard(app, *, has_admin, has_project_create, log=None):
    security_bootstrap.configure_app_security_bootstrap(
        app=app,
        log_print=log or (lambda *_a, **_k: None),
        csrf_session_key="_csrf_token",
        enable_admin_security=True,
        deployment_mode="single",
        csrf_token=lambda: "csrf-token",
        has_admin_access=lambda: has_admin,
        is_logged_in=lambda: True,
        get_current_user=lambda: {"username": "tester"},
        has_project_access=lambda *_a, **_k: True,
        has_project_admin_access=lambda *_a, **_k: True,
        is_valid_admin_token=lambda: False,
        unauthorized_admin_response=lambda: ("forbidden", 403),
        unauthorized_login_response=lambda: ("login-required", 401),
        has_project_create_access=lambda: has_project_create,
        csrf_token_from_request=lambda: "csrf-token",
        csrf_error_response=lambda msg: (msg, 400),
        is_same_origin_request=lambda: True,
        get_excel_column_letter=lambda _idx: "A",
        format_beijing_time=lambda *_a, **_k: "2026-03-08 12:00:00",
    )


def _guarded_app():
    """合成一个带 `core_management_routes` 蓝图的小应用（只保留被测路由）。"""
    app = Flask(__name__)
    app.secret_key = "test-key"
    bp = Blueprint("core_management_routes", __name__)

    @bp.route("/repositories/<int:repository_id>/delete", methods=["POST"], endpoint="delete_repository")
    def _delete_repository(repository_id):  # pragma: no cover - 不应被真正执行
        return "deleted"

    @bp.route("/projects", methods=["GET", "POST"], endpoint="projects")
    def _projects():  # pragma: no cover - 不应被真正执行
        return "projects"

    @bp.route(
        "/projects/<int:project_id>/repositories/add-git",
        endpoint="add_git_repository",
    )
    def _add_git_repository(project_id):  # pragma: no cover - 不应被真正执行
        return "add-git-page"

    app.register_blueprint(bp)
    return app


class TestGuardActuallyBlocks:
    """门禁必须真的拦人 —— 否则「表是对的」没有意义。"""

    def test_non_admin_is_blocked_on_sensitive_endpoint(self):
        app = _guarded_app()
        _configure_guard(app, has_admin=False, has_project_create=False)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "csrf-token"

        resp = client.post("/repositories/1/delete")
        assert resp.status_code == 403, (
            f"非平台管理员不应能调用 delete_repository，实际 {resp.status_code}"
        )

    def test_projects_write_uses_project_create_access_not_platform_admin(self):
        """`projects` 的 POST 归 `has_project_create_access` 管，不是平台管理员。

        这条用例把 WRITE_PROTECTED 分支里那个 `request.endpoint == "....projects"`
        的比较也钉住了：如果比较值退化成裸名 `"projects"`，就会掉进
        `elif not has_admin_access()` 分支 → 403；而正确实现下
        `has_project_create_access()=True` → 放行。
        """
        app = _guarded_app()
        _configure_guard(app, has_admin=False, has_project_create=True)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "csrf-token"

        resp = client.post("/projects")
        assert resp.status_code == 200, (
            f"有建项目权限的用户应能 POST /projects（agent 模式下这是常规路径），"
            f"实际 {resp.status_code}。多半是 WRITE_PROTECTED 分支里的 endpoint "
            f"比较退化成了裸名，从而误判为「需要平台管理员」。"
        )

    def test_project_scoped_page_is_not_admin_gated(self):
        """项目管理员能打开 add-git 页面 —— 证明没被门禁表误伤。"""
        app = _guarded_app()
        _configure_guard(app, has_admin=False, has_project_create=False)
        client = app.test_client()

        resp = client.get("/projects/1/repositories/add-git")
        assert resp.status_code == 200, (
            f"项目级页面被平台管理员门禁挡住了（{resp.status_code}）—— "
            f"项目管理员将无法添加仓库。"
        )
