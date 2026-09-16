# -*- coding: utf-8 -*-
"""项目级写接口必须校验调用者对**该项目**的权限。

## 为什么需要这个测试

2026-09 审计在同一个代码库里发现三处同类遗漏 —— 同一文件的兄弟接口都判了权限，
唯独某几个没判：

1. `services/weekly_version_logic.py::weekly_version_config_api`
   `::weekly_version_config_detail_api`
   同文件另有 9 处接口都做了项目权限校验（6 处以 `config.project_id`、3 处以
   `project_id`），这两个**写接口**没有。`weekly_version_config_api` 的 POST
   会创建配置并派发后台同步任务；`weekly_version_config_detail_api` 的 PUT
   会删掉 `WeeklyVersionDiffCache` 后重建同步任务，DELETE 会删配置及其
   Excel/diff 缓存与后台任务。local 后端默认开放自助注册、新用户角色是
   `normal` —— 于是任何登录用户都能枚举 `project_id` 跨项目读配置
   （含仓库名/分支/时间窗口）并新建配置。

2. `services/repository_misc_page_service.py::render_edit_repository_page`
   **完全没有鉴权**。它唯一的预期门禁是 `SENSITIVE_ENDPOINTS` 里的
   `edit_repository`，而那张表写的是裸 endpoint 名、永远匹配不上
   `request.endpoint`（详见 `tests/test_endpoint_guard_tables.py`）。
   后果：任何已登录用户可读任意项目的仓库连接配置
   （url / server_url / branch / category / resource_type）。

3. `services/repository_admin_handlers.py::test_repository`
   同文件其余四个维护接口都带 `@require_admin`，唯独它没有，也不在
   `SENSITIVE_ENDPOINTS` 里。任何已登录用户可对任意 `repository_id` 触发真实
   同步任务（agent 模式下给别的项目的仓库派任务）或本地
   `clone_or_update_repository()`，并把 git 错误文本回显出来。

## 关于判权层级的一个刻意选择

`edit_repository` 用的是 `_has_project_access`（项目成员即可），**不是**
`_has_admin_access`（平台管理员）。因为仓库编辑是项目级操作，项目管理员本来就
应当能做；写成平台管理员会把他们挡在门外 —— 那是功能回退，不是加固。
"""
from __future__ import annotations

import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _function_body(source, func_name):
    """截取某个顶层函数的**完整**函数体（到下一个顶层 def / 装饰器为止）。

    不要用固定字符数切片 —— 这些函数有几 KB，切短了会漏掉后面的写库语句，
    于是「判定早于写库」的断言会误判为「没找到写库语句」。
    """
    match = re.search(rf"^def {re.escape(func_name)}\(", source, re.M)
    assert match, f"找不到函数定义 def {func_name}("
    rest = source[match.start() :]
    nxt = re.search(r"^(?:def |@)", rest[1:], re.M)
    return rest if not nxt else rest[: nxt.start() + 1]


class TestWeeklyVersionConfigApisCheckProjectAccess:
    """两个写接口必须判项目权限 —— 同文件另有 9 处接口都判了。"""

    @pytest.mark.parametrize(
        "func_name",
        ["weekly_version_config_api", "weekly_version_config_detail_api"],
    )
    def test_has_project_access_guard(self, func_name):
        source = _read("services/weekly_version_logic.py")
        body = _function_body(source, func_name)
        assert "_has_project_access(project_id)" in body, (
            f"{func_name} 缺少 `_has_project_access(project_id)` 判定。\n"
            f"该接口是写接口（POST 建配置 / PUT 删 diff 缓存重建任务 / DELETE 删配置），"
            f"缺失判定会让任意 normal 角色用户跨项目操作。"
        )
        # 判定必须在写操作之前生效，且返回 403（而不是静默继续）
        guard_at = body.index("_has_project_access(project_id)")
        after = body[guard_at : guard_at + 220]
        assert "403" in after, (
            f"{func_name} 里的项目权限判定没有返回 403 —— 必须是硬拒绝，"
            f"否则后续代码会继续执行。"
        )

    def test_named_siblings_still_have_the_guard(self):
        """反向保险：逐一确认我们参照的兄弟接口现在仍然都在。

        刻意逐个点名而不是数个数 —— 数字断言在新增接口时会无意义地变红，
        点名断言只在「某处判定被删」时变红，正是我们想知道的。
        """
        source = _read("services/weekly_version_logic.py")
        for func_name in (
            "weekly_version_config",
            "weekly_version_list",
            "merged_project_view",
        ):
            body = _function_body(source, func_name)
            assert "_has_project_access(project_id)" in body, (
                f"{func_name} 的项目权限判定不见了 —— "
                f"我们正是以这些兄弟接口为参照，说明该文件的口径是「都判」。"
            )
        # 另有一批接口是在取到 config 之后用 config.project_id 判定的。
        assert source.count("_has_project_access(config.project_id)") >= 6, (
            "以 config.project_id 判定的那批接口少了几处，请复核。"
        )

    @pytest.mark.parametrize(
        "func_name",
        ["weekly_version_config_api", "weekly_version_config_detail_api"],
    )
    def test_guard_runs_before_any_write(self, func_name):
        """判定必须早于函数里的第一次写库，而不是写在末尾。"""
        source = _read("services/weekly_version_logic.py")
        body = _function_body(source, func_name)
        guard_at = body.index("_has_project_access(project_id)")
        write_markers = [
            m.start()
            for m in re.finditer(r"db\.session\.(add|delete|commit)|\.query\.delete\(", body)
        ]
        assert write_markers, f"{func_name} 里没找到写库语句（测试前提失效，请复核）"
        assert guard_at < min(write_markers), (
            f"{func_name} 的权限判定发生在写库之后 —— 已经晚了。"
        )


class TestEditRepositoryPageIsAuthorized:
    """仓库编辑页读的是仓库连接配置，必须判项目权限。"""

    SOURCE = "services/repository_misc_page_service.py"

    def test_project_access_is_enforced(self):
        source = _read(self.SOURCE)
        body = _function_body(source, "render_edit_repository_page")
        assert "_has_project_access(project.id)" in body, (
            "render_edit_repository_page 没有按项目判权限。\n"
            "它原先**完全没有鉴权**，唯一的兜底是 SENSITIVE_ENDPOINTS 里那条"
            "永不匹配的裸名 `edit_repository`。"
        )

    def test_denied_request_aborts_with_403(self):
        source = _read(self.SOURCE)
        body = _function_body(source, "render_edit_repository_page")
        assert "abort(403)" in body, (
            "拒绝路径必须 abort(403) —— 静默渲染空页面会让越权看起来像「没数据」。"
        )

    def test_guard_uses_project_access_not_platform_admin(self):
        """刻意用项目级判定：写成平台管理员会把项目管理员挡在门外。"""
        source = _read(self.SOURCE)
        body = _function_body(source, "render_edit_repository_page")
        assert "_has_admin_access" not in body, (
            "这里不应该用平台管理员判定 —— 仓库编辑是项目级操作，"
            "项目管理员应当也能做。"
        )

    def test_abort_is_injectable_for_callers_without_request_context(self):
        """`abort` 可注入，方便非请求上下文复用（本文件的既有风格）。"""
        source = _read(self.SOURCE)
        assert "abort=None" in source, "render_edit_repository_page 应保留可注入的 abort 参数"


class TestRepositoryConnectivityTestRequiresAdmin:
    """触发真实同步 / 本地 clone 的接口必须限平台管理员。"""

    SOURCE = "services/repository_admin_handlers.py"

    def test_require_admin_decorator_present(self):
        source = _read(self.SOURCE)
        match = re.search(r"^def test_repository\(", source, re.M)
        assert match, "找不到 def test_repository("
        # 取 def 之前紧邻的装饰器块
        head = source[: match.start()].rstrip().splitlines()
        decorators = []
        for line in reversed(head):
            stripped = line.strip()
            if stripped.startswith("@"):
                decorators.append(stripped)
                continue
            if stripped == "" or stripped.startswith("#"):
                continue
            break
        assert any(d.startswith("@require_admin") for d in decorators), (
            f"test_repository 缺少 @require_admin，实际装饰器：{decorators}\n"
            f"同文件其余四个维护接口都有；该接口会派发真实同步任务或本地 clone。"
        )

    def test_sibling_maintenance_endpoints_keep_the_decorator(self):
        """反向保险：四个兄弟接口的装饰器不能被顺手删掉。"""
        source = _read(self.SOURCE)
        for func_name in (
            "update_repository_order",
            "swap_repository_order",
            "delete_repository",
            "delete_project",
        ):
            match = re.search(rf"^def {re.escape(func_name)}\(", source, re.M)
            assert match, f"找不到 def {func_name}("
            head = source[: match.start()].rstrip().splitlines()
            window = "\n".join(head[-3:])
            assert "@require_admin" in window, (
                f"{func_name} 的 @require_admin 不见了：\n{window}"
            )
