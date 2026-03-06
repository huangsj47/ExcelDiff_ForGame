#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import uuid

from app import app, create_tables, db
from models import Project
from qkit_auth.services import (
    add_user_to_project,
    ensure_qkit_user,
    get_project_import_config,
    import_project_users_from_redmine,
    remove_user_from_project,
    upsert_project_import_config,
)


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


def test_import_config_token_scoped_by_user_and_host_by_project():
    with app.app_context():
        create_tables()
        user_a = _create_qkit_user(_uid("qa"))
        user_b = _create_qkit_user(_uid("qb"))

        project = Project(code=_uid("P"), name=_uid("Project"), department="QA")
        db.session.add(project)
        db.session.commit()

        saved, err = upsert_project_import_config(
            project.id,
            token="token-user-a",
            host="g119.pm.netease.com",
            project_name="G119-破碎之地",
            updated_by=user_a.id,
        )
        assert err is None
        assert saved is not None

        config_a = get_project_import_config(project.id, user_id=user_a.id)
        assert config_a.get("token") == "token-user-a"
        assert config_a.get("host") == "g119.pm.netease.com"
        assert config_a.get("project_name") == "G119-破碎之地"

        config_b = get_project_import_config(project.id, user_id=user_b.id)
        assert config_b.get("token") == ""
        assert config_b.get("host") == "g119.pm.netease.com"
        assert config_b.get("project_name") == "G119-破碎之地"


def test_import_config_project_switch_keeps_user_token_and_clears_new_project_host():
    with app.app_context():
        create_tables()
        user_a = _create_qkit_user(_uid("qa"))

        project_1 = Project(code=_uid("P1"), name=_uid("ProjectA"), department="QA")
        project_2 = Project(code=_uid("P2"), name=_uid("ProjectB"), department="QA")
        db.session.add(project_1)
        db.session.add(project_2)
        db.session.commit()

        saved, err = upsert_project_import_config(
            project_1.id,
            token="user-a-token",
            host="g120.pm.netease.com",
            project_name="G120-破碎之地",
            updated_by=user_a.id,
        )
        assert err is None
        assert saved is not None

        switched = get_project_import_config(project_2.id, user_id=user_a.id)
        assert switched.get("token") == "user-a-token"
        assert switched.get("host") == ""
        assert switched.get("project_name") == ""


def test_import_uses_current_user_token_instead_of_project_shared_token(monkeypatch):
    with app.app_context():
        create_tables()
        user_a = _create_qkit_user(_uid("qa"))
        user_b = _create_qkit_user(_uid("qb"))
        project = Project(code=_uid("P"), name=_uid("Project"), department="QA")
        db.session.add(project)
        db.session.commit()

        saved, err = upsert_project_import_config(
            project.id,
            token="token-user-a",
            host="g121.pm.netease.com",
            project_name="G121-破碎之地",
            updated_by=user_a.id,
        )
        assert err is None
        assert saved is not None

        called = {"params": None}

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"success": True, "data": []}

        def _fake_get(url, params=None, timeout=None):
            called["params"] = dict(params or {})
            return _FakeResponse()

        monkeypatch.setattr("qkit_auth.services.requests.get", _fake_get)

        result_a = import_project_users_from_redmine(
            project_id=project.id,
            operator_user_id=user_a.id,
        )
        assert result_a.get("success") is True
        assert called["params"] is not None
        assert called["params"].get("token") == "token-user-a"
        assert called["params"].get("host") == "g121.pm.netease.com"
        assert called["params"].get("project") == "G121-破碎之地"

        result_b = import_project_users_from_redmine(
            project_id=project.id,
            operator_user_id=user_b.id,
        )
        assert result_b.get("success") is False
        assert "token 或 host 为空" in str(result_b.get("message") or "")


def test_import_returns_newly_added_functions_dedup(monkeypatch):
    with app.app_context():
        create_tables()
        user = _create_qkit_user(_uid("importer"))
        project = Project(code=_uid("P"), name=_uid("Project"), department="QA")
        db.session.add(project)
        db.session.commit()

        saved, err = upsert_project_import_config(
            project.id,
            token="token-import",
            host="g130.pm.netease.com",
            project_name="G130-测试",
            updated_by=user.id,
        )
        assert err is None
        assert saved is not None

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": True,
                    "data": [
                        {"mail": "a@corp.netease.com", "name": "A", "function_name": "策划"},
                        {"mail": "b@corp.netease.com", "name": "B", "function_name": "策划"},
                        {"mail": "c@corp.netease.com", "name": "C", "function_name": "QA"},
                    ],
                }

        monkeypatch.setattr("qkit_auth.services.requests.get", lambda *args, **kwargs: _FakeResponse())

        result = import_project_users_from_redmine(
            project_id=project.id,
            operator_user_id=user.id,
        )
        assert result.get("success") is True
        assert result.get("added") == 3
        assert sorted(result.get("newly_added_functions") or []) == ["QA", "策划"]


def test_removed_user_block_is_project_scoped(monkeypatch):
    with app.app_context():
        create_tables()
        operator = _create_qkit_user(_uid("operator"))
        user_name = _uid("member")
        member = _create_qkit_user(user_name)
        project_a = Project(code=_uid("PA"), name=_uid("ProjectA"), department="QA")
        project_b = Project(code=_uid("PB"), name=_uid("ProjectB"), department="QA")
        db.session.add(project_a)
        db.session.add(project_b)
        db.session.commit()

        ok, err = add_user_to_project(member.id, project_a.id, "member")
        assert ok is True
        assert err is None
        ok, err = remove_user_from_project(member.id, project_a.id, removed_by=operator.id)
        assert ok is True
        assert err is None

        saved_a, err_a = upsert_project_import_config(
            project_a.id,
            token="token-a",
            host="g131.pm.netease.com",
            project_name="G131-A",
            updated_by=operator.id,
        )
        assert err_a is None
        assert saved_a is not None
        saved_b, err_b = upsert_project_import_config(
            project_b.id,
            token="token-b",
            host="g132.pm.netease.com",
            project_name="G132-B",
            updated_by=operator.id,
        )
        assert err_b is None
        assert saved_b is not None

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": True,
                    "data": [
                        {
                            "mail": f"{user_name}@corp.netease.com",
                            "name": user_name,
                            "function_name": "策划",
                        }
                    ],
                }

        monkeypatch.setattr("qkit_auth.services.requests.get", lambda *args, **kwargs: _FakeResponse())

        result_a = import_project_users_from_redmine(
            project_id=project_a.id,
            operator_user_id=operator.id,
        )
        assert result_a.get("success") is True
        assert result_a.get("added") == 0
        assert user_name in (result_a.get("skipped_removed") or [])

        result_b = import_project_users_from_redmine(
            project_id=project_b.id,
            operator_user_id=operator.id,
        )
        assert result_b.get("success") is True
        assert result_b.get("added") == 1
