import services.agent_management_handlers as agent_handlers
from app import app, create_tables


def test_list_agent_nodes_has_no_store_cache_headers(monkeypatch):
    monkeypatch.setattr(agent_handlers, "build_agent_node_items", lambda: [])

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["is_admin"] = True
                sess["admin_user"] = "admin"

            resp = client.get("/api/agents", headers={"Accept": "application/json"})
            assert resp.status_code == 200, resp.get_data(as_text=True)
            data = resp.get_json() or {}
            assert data.get("success") is True
            assert "server_time" in data
            assert "no-store" in str(resp.headers.get("Cache-Control") or "").lower()
            assert str(resp.headers.get("Pragma") or "").lower() == "no-cache"

