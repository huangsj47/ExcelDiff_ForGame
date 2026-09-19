import uuid

from flask import session

from app import app, create_tables, db
from models import Project
from qkit_auth import routes as qroutes
from qkit_auth.models import (
    QkitAuthImportBlock,
    QkitAuthUserProject,
    QkitImportBlockType,
)
from qkit_auth.services import (
    add_user_to_project,
    ensure_qkit_user,
    remove_user_from_project,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_qkit_user(username: str):
    user, err = ensure_qkit_user(
        username=username,
        display_name=username,
        email=f"{username}@corp.netease.com",
        source="test",
    )
    assert err is None
    assert user is not None
    db.session.commit()
    return user


def test_user_list_defaults_to_username_sort(monkeypatch):
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("Project"), department="QA")
        db.session.add(project)
        db.session.flush()

        suffix = uuid.uuid4().hex[:6]
        user_b = _create_qkit_user(f"{suffix}b_user")
        user_a = _create_qkit_user(f"{suffix}a_user")
        db.session.add(
            QkitAuthUserProject(
                user_id=user_b.id,
                project_id=project.id,
                role="member",
                imported_from_qkit=False,
                import_sync_locked=True,
            )
        )
        db.session.add(
            QkitAuthUserProject(
                user_id=user_a.id,
                project_id=project.id,
                role="member",
                imported_from_qkit=False,
                import_sync_locked=True,
            )
        )
        db.session.commit()

        captured = {}

        monkeypatch.setattr(qroutes, "_resolve_user_mgmt_scope", lambda: (True, [project.id], [project]))
        monkeypatch.setattr(
            qroutes,
            "render_template",
            lambda _tpl, **kwargs: captured.update(kwargs) or "ok",
        )

        with app.test_request_context(f"/auth/users?project_id={project.id}"):
            response = qroutes.user_list()

        assert response == "ok"
        assert captured.get("sort_by") == "username"
        assert captured.get("sort_dir") == "asc"
        usernames = [item.username for item in captured.get("users") or []]
        assert user_a.username in usernames
        assert user_b.username in usernames
        assert usernames.index(user_a.username) < usernames.index(user_b.username)


def test_manual_add_clears_project_removed_import_block(monkeypatch):
    with app.app_context():
        create_tables()
        operator = _create_qkit_user(_uid("operator"))
        username = _uid("member")
        member = _create_qkit_user(username)
        project = Project(code=_uid("P"), name=_uid("Project"), department="QA")
        db.session.add(project)
        db.session.commit()

        ok, err = add_user_to_project(member.id, project.id, "member")
        assert ok is True
        assert err is None
        ok, err = remove_user_from_project(member.id, project.id, removed_by=operator.id)
        assert ok is True
        assert err is None

        block_count = QkitAuthImportBlock.query.filter_by(
            project_id=project.id,
            username=member.username,
            block_type=QkitImportBlockType.REMOVED.value,
        ).count()
        assert block_count == 1

        monkeypatch.setattr(qroutes, "_resolve_user_mgmt_scope", lambda: (True, [project.id], [project]))
        with app.test_request_context(
            "/auth/api/users/manual-add",
            method="POST",
            json={
                "display_name": member.display_name,
                "email": member.email,
                "member_role": "member",
                "function_name": "策划",
                "project_id": project.id,
            },
        ):
            session["auth_user_id"] = operator.id
            response = qroutes.api_manual_add_user()

        payload = response.get_json()
        assert payload.get("success") is True

        block_count_after = QkitAuthImportBlock.query.filter_by(
            project_id=project.id,
            username=member.username,
            block_type=QkitImportBlockType.REMOVED.value,
        ).count()
        assert block_count_after == 0


def test_a_project_admin_cannot_edit_a_user_outside_their_projects(monkeypatch):
    """**项目管理员不许改自己项目之外的用户 —— 哪怕请求里带上了自己项目的 id。**

    越界判定原来写的是 `if in_scope is None and not project_ids:`，于是攻击者只要在同一个
    请求里捎上一个自己项目的 id（`project_ids.issubset(allowed)` 轻松通过），那个分支就
    永不触发 —— 他能改**任何**用户（含平台管理员）的 `username`。而 qkit 的登录身份映射
    就是「用户名 → 行」（`_get_user_by_username`），把某个身份那一行的用户名改成自己的
    SSO 名，等于**把那个身份让给自己**；后面的成员同步还会顺带清掉受害者的项目成员关系
    （它只保留请求里那 `project_ids`）。

    所以这条钉的是：`in_scope` 是必要条件，与 `project_ids` 无关。
    """
    with app.app_context():
        create_tables()
        mine = Project(code=_uid("P"), name=_uid("Mine"), department="QA")
        other = Project(code=_uid("P"), name=_uid("Other"), department="QA")
        db.session.add_all([mine, other])
        db.session.flush()
        db.session.commit()

        attacker = _create_qkit_user(_uid("attacker"))
        victim = _create_qkit_user(_uid("victim"))
        add_user_to_project(attacker.id, mine.id, "admin")
        # 受害者**只**在另一个项目里 —— 攻击者对那个项目没有任何权限。
        add_user_to_project(victim.id, other.id, "member")

        monkeypatch.setattr(
            qroutes,
            "_resolve_user_mgmt_scope",
            lambda: (False, [mine.id], [mine]),
        )
        original_username = victim.username
        with app.test_request_context(
            f"/auth/api/users/{victim.id}/profile",
            method="POST",
            json={
                "username": "attacker_takes_over",
                "display_name": victim.display_name,
                "email": victim.email,
                "project_ids": [mine.id],      # 这一个是攻击者自己的项目 —— 它不再是通行证
                "member_role": "member",
            },
        ):
            session["auth_user_id"] = attacker.id
            response = qroutes.api_update_user_profile(victim.id)

        # 视图直接调用时拿回来的是 `(body, status)` 元组（`jsonify(...), 403`），
        # 交给 Flask 路由才变成 Response —— 两种都认，免得断言写成「有没有 status_code」。
        status = response[1] if isinstance(response, tuple) else response.status_code
        assert status == 403, response
        db.session.refresh(victim)
        assert victim.username == original_username, "别人那一行的用户名被改掉了 = 身份被顶掉"
