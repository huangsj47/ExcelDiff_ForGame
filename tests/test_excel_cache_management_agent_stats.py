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
            # 全局那一档只断「**至少**包含我这两行」。
            #
            # 原先断的是 `total_count == 2`，而 `/api/excel-html-cache/stats` 的
            # `agent_temp_cache` 分块是对**整张表**聚合、**没有 project 过滤**
            # （routes/cache_management_routes.py 那一档就是 `func.count(AgentTempCache.id)`）。
            # 而本仓的测试库是**会话级共用**的（没有逐用例重置），于是别的用例留下的行
            # 会被算进这两个数：只跑本文件时绿，`test_agent_execution_split.py` 先跑
            # （它建了 3 行且不清理）就红 —— 红的原因与「平台侧统计有没有坏」毫无关系。
            #
            # 精确的那一份断言交给下面按 project 过滤的 `stats-by-project`：
            # 它才是本用例**自己那两行**的证据。
            global_resp = client.get("/api/excel-html-cache/stats", headers=headers)
            assert global_resp.status_code == 200
            global_data = global_resp.get_json() or {}
            assert global_data.get("success") is True
            agent_global = global_data.get("agent_temp_cache") or {}
            assert agent_global.get("total_count") >= 2
            assert agent_global.get("expired_count") >= 1
            assert agent_global.get("active_count") >= 1
            # 这一档的数值必须自洽（总数 = 活跃 + 过期），它与「有没有人污染」无关，
            # 所以可以断严格相等 —— 顺带保证统计口径本身没算错。
            assert agent_global.get("total_count") == (
                agent_global.get("active_count", 0) + agent_global.get("expired_count", 0)
            )

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
