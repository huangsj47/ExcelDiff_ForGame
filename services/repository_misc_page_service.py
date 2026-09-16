"""Misc repository page/helpers extracted from app.py."""

from __future__ import annotations

import os

from utils.request_security import _has_project_access


def render_edit_repository_page(*, repository_id, Repository, render_template, abort=None):
    """Render repository edit page by repository type.

    权限：本函数原先**没有任何权限判断** —— 它唯一的预期门禁是
    `SENSITIVE_ENDPOINTS`（services/app_security_bootstrap_service.py）里的
    `edit_repository`。但那张表写的是**裸 endpoint 名**，而 Flask 的
    `request.endpoint` 是 `<blueprint>.<name>` 形式（实测：
    `core_management_routes.edit_repository`），`in` 判断**永远不成立** ——
    于是任何已登录用户都能读任意项目的仓库连接配置（url / server_url /
    branch / category / resource_type）。

    （那张表本身已修为全限定名，见 `services/app_security_bootstrap_service.py`
    的模块注释；`edit_repository` 被**刻意**留在表外，理由见下。）

    这里补上真正的授权：按仓库所属项目判权限。刻意不用平台管理员判定 ——
    仓库编辑本来就是项目级操作，项目管理员也应当能做；写成平台管理员会把
    项目管理员挡在门外。
    """
    if abort is None:
        from flask import abort as _abort
        abort = _abort

    repository = Repository.query.get_or_404(repository_id)
    project = repository.project
    if project is None or not _has_project_access(project.id):
        abort(403)
    if repository.type == "git":
        return render_template(
            "add_git_repository.html",
            project=project,
            repository=repository,
            is_edit=True,
        )
    return render_template(
        "add_svn_repository.html",
        project=project,
        repository=repository,
        is_edit=True,
    )


def check_local_repository_exists(
    *,
    project_code,
    repository_name,
    repository_id,
    build_repository_local_path,
):
    """Check whether local repository directory exists."""
    try:
        local_path = build_repository_local_path(project_code, repository_name, repository_id, strict=False)
    except (TypeError, ValueError):
        return False
    return os.path.exists(local_path)
