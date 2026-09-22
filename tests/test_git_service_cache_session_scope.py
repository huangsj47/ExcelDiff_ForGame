# -*- coding: utf-8 -*-
"""`get_git_service` 的缓存不能把**别的会话里的 ORM 对象**递给本次调用方。

## 症状（2026-09-22 在真机上复现）

后台的 `excel_diff` 任务先建了缓存项；任务结束、它的 app context 一收，缓存里那个
`Repository` 就 detached 了。之后管理员点「同步」**必然 500**：

    Git操作失败: 仓库同步失败: Instance <Repository at 0x...> is not bound to a
    Session; attribute refresh operation cannot proceed

三次调用报的是**同一个对象地址**，所以不是偶发。更重的是下一次：失败路径会把仓库写进
`last_sync_error`，而 `sync_failure_backoff_active` 据此**把该仓库的自动同步也停 30 分钟**
（`services/repository_sync_backoff.py`）—— 一个坏掉的按钮把定时同步一起带走了。

## 为什么必须是「commit 过再离开会话」

detached 本身不报错：已经加载过的属性直接返回内存里的值。报错发生在**属性过期之后又要
刷新**时 —— 同一会话里 `commit()` 过（`expire_on_commit` 默认开）就会把属性全标脏。
所以用例必须让第一个会话 commit 一次再离开，否则它会在修复前也通过（假绿）。

## 修法

缓存仍然按 `id_url` 复用实例（它持有线程池，重建等于漏线程），但**返回前把实例上的
`repository` 换成本次调用方那个对象**。同一个仓库的任意两个实例字段值相同（同 id 同 url），
换过去只换会话归属，不换语义。
"""
from __future__ import annotations

import uuid

from app import app, create_tables, db
from models import Project, Repository
from services import vcs_content_service
from services.vcs_content_service import get_git_service


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_repository() -> int:
    project = Project(code=_uid("P"), name="git-service-cache-scope")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        clone_status="completed",
    )
    db.session.add(repository)
    db.session.commit()
    return repository.id


def test_the_cached_service_follows_the_callers_session():
    with app.app_context():
        create_tables()
        repo_id = _make_repository()

    vcs_content_service._git_service_cache.clear()

    # 第一个会话：建出缓存项，然后 commit 一次让属性过期，再离开会话
    with app.app_context():
        first = db.session.get(Repository, repo_id)
        service = get_git_service(first)
        db.session.commit()

    # 第二个会话：拿到的是同一条缓存，但必须是**本会话**那个对象
    with app.app_context():
        second = db.session.get(Repository, repo_id)
        cached = get_git_service(second)

        assert cached is service, "没走到缓存这条路，用例前提不成立（先别急着改断言）"
        assert cached.repository is second, (
            "交回来的还是第一个会话那个 Repository —— 它已经 detached，"
            "下一个读属性的人会拿到 DetachedInstanceError"
        )
        # 真正的失败形态：属性过期后要刷新，而对象不属于任何会话
        assert cached.repository.branch == "main"
        assert cached.repository.id == second.id


def test_a_second_repository_does_not_reuse_the_first_ones_service():
    """反方向：不同仓库（同 id 不同 url 也好、不同 id 也好）不许共用一条缓存项。"""
    with app.app_context():
        create_tables()
        repo_a = _make_repository()
        repo_b = _make_repository()

    vcs_content_service._git_service_cache.clear()

    with app.app_context():
        service_a = get_git_service(db.session.get(Repository, repo_a))
    with app.app_context():
        service_b = get_git_service(db.session.get(Repository, repo_b))

    assert service_a is not service_b
    assert service_b.repository.id == repo_b
