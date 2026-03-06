import json
import uuid
from datetime import datetime, timedelta, timezone

from app import app, create_tables, db
from models import AgentTempCache, Project


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_excel_cache_stats_include_agent_temp_cache(monkeypatch):
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="缓存统计项目", department="QA")
        db.session.add(project)
        db.session.flush()

        now_utc = datetime.now(timezone.utc)
        db.session.add(
            AgentTempCache(
                cache_key=f"agent-cache-{uuid.uuid4().hex[:8]}",
                project_id=project.id,
                payload_json=json.dumps({"ok": True}),
                payload_hash="hash-active",
                payload_size=1024,
                expire_at=now_utc + timedelta(hours=1),
            )
        )
        db.session.add(
            AgentTempCache(
                cache_key=f"agent-cache-{uuid.uuid4().hex[:8]}",
                project_id=project.id,
                payload_json=json.dumps({"ok": False}),
                payload_hash="hash-expired",
                payload_size=2048,
                expire_at=now_utc - timedelta(hours=1),
            )
        )
        db.session.commit()

        with app.test_client() as client:
            headers = {"X-Admin-Token": admin_token}
            global_resp = client.get("/api/excel-html-cache/stats", headers=headers)
            assert global_resp.status_code == 200
            global_data = global_resp.get_json() or {}
            assert global_data.get("success") is True
            agent_global = global_data.get("agent_temp_cache") or {}
            assert agent_global.get("total_count") == 2
            assert agent_global.get("expired_count") == 1
            assert agent_global.get("active_count") == 1

            project_resp = client.get("/api/excel-cache/stats-by-project", headers=headers)
            assert project_resp.status_code == 200
            project_data = project_resp.get_json() or {}
            assert project_data.get("success") is True
            project_rows = project_data.get("projects") or []
            target_row = next((row for row in project_rows if (row.get("project") or {}).get("id") == project.id), None)
            assert target_row is not None
            agent_project = target_row.get("agent_temp_cache") or {}
            assert agent_project.get("total_count") == 2
            assert agent_project.get("expired_count") == 1
            assert agent_project.get("active_count") == 1
