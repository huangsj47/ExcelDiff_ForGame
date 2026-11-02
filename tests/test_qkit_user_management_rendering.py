"""Exercise the real Qkit user page, including serialized pending requests."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from jinja2 import ChoiceLoader, FileSystemLoader

from app import app, create_tables, db
from models import Project
from qkit_auth import routes as qroutes
from qkit_auth.models import QkitAuthProjectCreateRequest, QkitAuthProjectJoinRequest
from qkit_auth.services import ensure_qkit_user


@pytest.mark.parametrize("request_kind", ["join", "create", "both", "none"])
def test_user_management_renders_pending_requests_with_beijing_times(monkeypatch, request_kind):
    suffix = uuid4().hex[:10]
    with app.app_context():
        create_tables()
        user, error = ensure_qkit_user(
            username=f"pending_{suffix}", display_name="待审批用户", email=None, source="test"
        )
        assert error is None
        project = Project(code=f"P{suffix}", name=f"已有项目{suffix}", department="QA")
        db.session.add(project)
        db.session.flush()
        when = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)
        if request_kind in {"join", "both"}:
            db.session.add(
                QkitAuthProjectJoinRequest(
                    user_id=user.id, project_id=project.id, message=f"加入留言{suffix}", created_at=when
                )
            )
        if request_kind in {"create", "both"}:
            db.session.add(
                QkitAuthProjectCreateRequest(
                    user_id=user.id,
                    project_code=f"NEW{suffix}",
                    project_name=f"申请项目{suffix}",
                    department="QA",
                    reason=f"创建理由{suffix}",
                    created_at=when,
                )
            )
        db.session.flush()
        monkeypatch.setattr(
            app,
            "jinja_loader",
            ChoiceLoader(
                [FileSystemLoader(Path(__file__).resolve().parents[1] / "qkit_auth/templates"), app.jinja_loader]
            ),
        )
        monkeypatch.setattr(qroutes, "_resolve_user_mgmt_scope", lambda: (True, [project.id], [project]))
        with app.test_request_context("/auth/users"):
            html = qroutes.user_list()
        assert "平台用户管理" in html
        if request_kind != "none":
            assert "2026-11-02 09:30" in html
        if request_kind in {"join", "both"}:
            assert f"加入留言{suffix}" in html
        if request_kind in {"create", "both"}:
            assert f"创建理由{suffix}" in html
        # The JSON API contract remains ISO strings, independently of HTML.
        for item in qroutes.list_pending_join_requests(project.id):
            assert isinstance(item["created_at"], str)


@pytest.mark.parametrize("timestamp", ["2026-11-02T01:30:00", "2026-11-02T01:30:00+00:00", None])
def test_pending_view_does_not_mutate_api_data(timestamp):
    item = {"created_at": timestamp, "message": "申请说明"}
    view = qroutes._pending_requests_for_template([item])[0]
    assert item["created_at"] == timestamp
    assert view["message"] == "申请说明"
    if timestamp is not None:
        assert isinstance(view["created_at"], datetime)
    else:
        assert view["created_at"] is None
