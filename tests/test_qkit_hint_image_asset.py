# -*- coding: utf-8 -*-
"""项目成员页的「project 参数示例」截图必须真的能取到。

## 为什么值得单独钉住

`qkit_auth/templates/qkit_project_members.html` 里有一个

    <img src="{{ url_for('qkit_auth_bp.project_name_hint_image') }}" alt="project参数示例">

而提供这张图的接口 `GET /qkit_auth/assets/project-name-simple-image` 原先这样取文件：

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "img"))

也就是「**仓库根**下的 `img/`」。可这个目录**根本不存在** ——
图片实际在 Flask 的静态目录里：`static/img/project_name_simple.png`。
于是该接口**恒返回 404**，成员页上的示例图永远是一张裂图，
浏览器控制台留一条 `project-name-simple-image: 404 (NOT FOUND)`。

这类「取文件的根目录写错了」的缺陷**不会让任何测试变红**：404 是
`send_from_directory` 的正常返回值，只有真的取一次文件、并与磁盘上的字节比对，
才能发现服务端和静态目录说的不是同一张图。

## 为什么用最小 app 而不是 import app

`qkit_auth_bp` 只在 `AUTH_BACKEND=qkit` 时注册（`auth/__init__.py::register_auth_blueprints`），
而 `app` 是模块级单例、后端在首次 import 时就定死了 —— 在测试里改环境变量已经晚了，
还会让本文件的断言依赖「谁先 import app」。所以这里自建一个最小 app 注册真实蓝图；
`test_minimal_app_static_folder_matches_the_real_one` 负责保证这个最小 app 的
静态目录与真实 app 的**同一个**，否则本文件可能在测一个不存在的部署形态。
"""
from __future__ import annotations

import os
import sys

import pytest
from flask import Flask

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from qkit_auth.routes import qkit_auth_bp  # noqa: E402
from services.app_security_bootstrap_service import (  # noqa: E402
    AUTH_EXEMPT_ENDPOINTS,
    AUTH_EXEMPT_PATHS,
)

_ASSET_URL = "/qkit_auth/assets/project-name-simple-image"
_IMAGE_ON_DISK = os.path.join(PROJECT_ROOT, "static", "img", "project_name_simple.png")


def _build_qkit_app() -> Flask:
    """只装 qkit 蓝图的最小 app，static 目录与真实 app 同锚（见模块说明）。"""
    app = Flask(__name__, static_folder=os.path.join(PROJECT_ROOT, "static"))
    app.secret_key = "test-secret"

    @app.route("/", endpoint="index")
    def _index():
        return "ok"

    app.register_blueprint(qkit_auth_bp)
    return app


@pytest.fixture()
def client():
    app = _build_qkit_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


class TestHintImageIsServed:
    def test_minimal_app_static_folder_matches_the_real_one(self, client):
        """前提校验：最小 app 的静态目录就是真实 app 的那一个。"""
        expected = os.path.normcase(os.path.join(PROJECT_ROOT, "static"))
        assert os.path.normcase(client.application.static_folder) == expected, (
            "最小 app 的 static_folder 与真实 app 的不一致，本文件的断言将失去意义"
        )

    def test_image_file_exists_in_the_static_tree(self):
        assert os.path.isfile(_IMAGE_ON_DISK), (
            f"{_IMAGE_ON_DISK} 不存在 —— 成员页的示例图没有可服务的文件。\n"
            "本测试与路由都锚定 Flask 静态目录（current_app.static_folder）。"
        )

    def test_endpoint_returns_the_png(self, client):
        response = client.get(_ASSET_URL)
        assert response.status_code == 200, (
            f"{_ASSET_URL} 返回 {response.status_code}。\n"
            "该接口原先指向仓库根的 img/ 目录（不存在），所以恒 404；"
            "取文件的根目录必须与图片实际所在位置一致（Flask 静态目录）。"
        )
        assert response.headers.get("Content-Type", "").startswith("image/png"), (
            f"Content-Type 不是 image/png：{response.headers.get('Content-Type')!r}"
        )
        with open(_IMAGE_ON_DISK, "rb") as fh:
            on_disk = fh.read()
        assert response.data == on_disk, (
            "返回的字节与 static/img/project_name_simple.png 不一致 —— "
            "说明服务的是另一份文件（同名不同图也会让成员页的示例失去意义）。"
        )
        assert len(response.data) > 1024, "返回内容太小，不像一张真实截图"

    def test_reachable_without_login(self, client):
        """成员页靠 <img src> 直接取图，带不上登录态，必须免鉴权。"""
        assert client.get(_ASSET_URL).status_code == 200
        assert "qkit_auth_bp.project_name_hint_image" in AUTH_EXEMPT_ENDPOINTS, (
            "endpoint 不在 AUTH_EXEMPT_ENDPOINTS 里 —— 登录门禁会把它挡成 302，"
            "<img> 拿到的是一段重定向 HTML。"
        )
        assert any(_ASSET_URL.startswith(prefix) for prefix in AUTH_EXEMPT_PATHS), (
            f"{_ASSET_URL} 不在 AUTH_EXEMPT_PATHS 的任何前缀下。"
        )

    def test_route_url_is_what_the_template_builds(self, client):
        """URL 不许漂移：模板用 url_for 生成，改了这里图就 404。"""
        rules = [str(rule) for rule in client.application.url_map.iter_rules()
                 if rule.endpoint == "qkit_auth_bp.project_name_hint_image"]
        assert rules == [_ASSET_URL], f"实际注册的规则：{rules}"


class TestTemplateStillUsesIt:
    def test_members_template_references_the_endpoint(self):
        """接口不是死代码：成员页仍然在用它。

        若将来改成直接引用静态文件（`url_for('static', ...)`），本测试会提醒
        把上面的断言一起改成对新写法的检查，而不是留下一个没人用、也没人测的接口。
        """
        template = os.path.join(
            PROJECT_ROOT, "qkit_auth", "templates", "qkit_project_members.html"
        )
        with open(template, encoding="utf-8") as fh:
            source = fh.read()
        assert "project_name_hint_image" in source, (
            "成员页不再引用 project_name_hint_image 了："
            "请同步更新本文件（要么删掉这个接口与它的白名单项，要么换断言）。"
        )
