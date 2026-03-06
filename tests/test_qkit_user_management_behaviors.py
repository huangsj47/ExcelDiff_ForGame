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
