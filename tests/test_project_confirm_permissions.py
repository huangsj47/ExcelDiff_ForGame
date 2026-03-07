import uuid

import auth
from app import app, create_tables, db
from models import Project
from qkit_auth.models import QkitAuthUserProject
from qkit_auth.services import (
    batch_update_project_confirm_permissions,
    clear_project_confirm_permissions,
    ensure_qkit_user,
    evaluate_user_confirm_permission,
)
from utils import request_security


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_qkit_user(username_prefix: str):
    user, err = ensure_qkit_user(
        username=username_prefix,
        display_name=username_prefix,
        email=f"{username_prefix}@corp.netease.com",
        source="test",
    )
    assert err is None
    assert user is not None
    db.session.commit()
    return user


def test_qkit_project_confirm_permission_evaluate_and_security(monkeypatch):
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("Project"), department="QA")
        db.session.add(project)
        db.session.flush()

        user = _create_qkit_user(_uid("user"))
        db.session.add(
            QkitAuthUserProject(
                user_id=user.id,
                project_id=project.id,
                role="member",
                function_name="策划",
                imported_from_qkit=False,
                import_sync_locked=True,
            )
        )
        db.session.commit()

        # 无规则时默认全放行
        permission = evaluate_user_confirm_permission(user_id=user.id, project_id=project.id)
        assert permission["allow_confirm"] is True
        assert permission["allow_reject"] is True

        # 配置规则：策划仅可确认，不可拒绝
        updated = batch_update_project_confirm_permissions(
            project_id=project.id,
            function_names=["策划"],
            allow_confirm=True,
            allow_reject=False,
            updated_by=user.id,
            replace_all=True,
        )
        assert updated["affected_count"] == 1

        permission = evaluate_user_confirm_permission(user_id=user.id, project_id=project.id)
        assert permission["allow_confirm"] is True
        assert permission["allow_reject"] is False

        monkeypatch.setattr(auth, "get_auth_backend", lambda: "qkit")
        monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)
        monkeypatch.setattr(request_security, "_has_project_access", lambda _project_id: True)
        monkeypatch.setattr(request_security, "_get_current_user", lambda: user)

        allowed_confirm, msg_confirm = request_security.can_current_user_operate_project_confirmation(
            project.id,
            "confirm",
        )
        assert allowed_confirm is True
        assert msg_confirm == ""

        allowed_reject, msg_reject = request_security.can_current_user_operate_project_confirmation(
            project.id,
            "reject",
        )
        assert allowed_reject is False
        assert "未被授权" in msg_reject

        deleted = clear_project_confirm_permissions(project.id)
        assert deleted == 1


def test_project_confirm_permission_denies_when_no_project_access(monkeypatch):
    monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)
    monkeypatch.setattr(request_security, "_has_project_access", lambda _project_id: False)
    monkeypatch.setattr(request_security, "_get_current_user", lambda: object())

    allowed, message = request_security.can_current_user_operate_project_confirmation(1001, "confirm")
    assert allowed is False
    assert "无权访问" in message
