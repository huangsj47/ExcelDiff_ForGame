import json
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

import services.weekly_version_logic as weekly_logic


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_weekly_diff_frontend_uses_file_tree_container_for_errors():
    content = _read("templates/weekly_version_diff.html")
    assert "fileTableBody" not in content
    assert "fileTreeContainer" in content
    assert "服务返回非JSON内容" in content


def test_cache_logs_api_backfills_project_prefix_for_legacy_messages():
    content = _read("routes/cache_management_routes.py")
    assert 'if not str(message).startswith("【")' in content
    assert 'project_code = repository_project_code_map.get(getattr(log, "repository_id", None)) or "UNKNOWN"' in content
    assert 'message = f"【{project_code}】{message}"' in content


def test_cache_services_prefix_unknown_when_project_code_missing():
    excel_content = _read("services/excel_diff_cache_service.py")
    weekly_content = _read("services/weekly_excel_cache_service.py")
    assert 'code = project_code or "UNKNOWN"' in excel_content
    assert 'code = project_code or "UNKNOWN"' in weekly_content


def test_weekly_files_api_handles_legacy_non_json_fields(monkeypatch):
    config = SimpleNamespace(
        id=2,
        project_id=1,
        repository=SimpleNamespace(
            name="repo_a",
            enable_id_confirmation=False,
        ),
    )
    fake_cache = SimpleNamespace(
        file_path="src/test.lua",
        commit_count=2,
        commit_authors="alice,bob",
        commit_messages="refs #123",
        commit_times=None,
        overall_status="pending",
        status_changed_by="",
        confirmation_status="{}",
        last_sync_time=None,
        merged_diff_data="invalid-json",
    )

    fake_config_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _config_id: config),
    )
    fake_diff_model = SimpleNamespace(
        query=SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]),
        ),
    )

    monkeypatch.setattr(weekly_logic, "WeeklyVersionConfig", fake_config_model)
    monkeypatch.setattr(weekly_logic, "WeeklyVersionDiffCache", fake_diff_model)

    app = Flask(__name__)
    with app.app_context():
        response = weekly_logic.weekly_version_files_api(2)
        payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    first_file = payload["files"][0]
    assert json.loads(first_file["commit_authors"]) == ["alice", "bob"]
    assert json.loads(first_file["commit_messages"]) == ["refs #123"]
    assert json.loads(first_file["commit_times"]) == []
    assert first_file["operations"] == []
