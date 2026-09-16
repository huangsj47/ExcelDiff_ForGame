#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由包（**只是一个包标记，这里不再定义蓝图**）。

## 为什么本文件是空的

这里曾经创建 `main_bp` / `project_bp` / `repository_bp` / `weekly_version_bp` /
`status_sync_bp` 五个蓝图，并由 `register_blueprints(app)` 注册，同时 import
`main_routes` / `project_routes` / `repository_routes` / `weekly_version_routes` /
`status_sync_routes` 五个模块。

**但 `register_blueprints()` 在仓库里没有任何调用方** —— 后续的路由都被拆到
`core_management_routes.py` / `*_management_routes.py` 里，由 `app.py` 直接
`register_blueprint(...)`。于是那五个模块成了**永远不会生效**的遗留代码，
而 `routes/__init__.py` 又会在每次 `import routes.*` 时把它们 import 一遍
（`app.py` 导入任何子模块都会先执行本文件）。

这个坑的危险之处在于**沉默**：往 `routes/main_routes.py` 里写一条路由，
既不生效、也不报错，只是 404。实测过：加完 `GET /healthz` 仍是 404
（`tests/test_healthz_probe.py` 的 `test_endpoint_is_fully_qualified_on_core_management_routes`
就是为此钉住「端点必须挂在真正注册的蓝图上」）。同名 URL 还会与活路由撞车
（`project_routes.py` 与 `core_management_routes.py` 都定义过
`/projects/<int:project_id>/merged-view`）。

现在这五个模块已删除（纯路由、无 helper 被别处引用，且其中定义的 URL 实测均不在
`app.url_map` 里 —— 运行时证据见删除时的提交说明）。新增路由请写到
`routes/*_management_routes.py` 或 `core_management_routes.py`，并确认
`app.py` 真的注册了对应蓝图。
"""

from __future__ import annotations
