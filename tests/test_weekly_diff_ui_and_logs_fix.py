import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

import services.weekly_version_logic as weekly_logic


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _build_empty_sync_background_model():
    class _Field:
        def __eq__(self, _other):
            return self

        def in_(self, _values):
            return self

        def desc(self):
            return self

    class _BgQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    return SimpleNamespace(
        task_type=_Field(),
        commit_id=_Field(),
        status=_Field(),
        id=_Field(),
        query=_BgQuery(),
    )


def test_weekly_diff_frontend_uses_file_tree_container_for_errors():
    content = _read("templates/weekly_version_diff.html")
    assert "fileTableBody" not in content
    assert "fileTreeContainer" in content
    assert "服务返回非JSON内容" in content
    assert "周版本数据处理中" in content
    assert "const syncBlocking = Boolean(data.sync_blocking) || syncRunning;" in content
    assert "(data.total_files || 0) === 0 && syncRunning" not in content


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
    monkeypatch.setattr(weekly_logic, "BackgroundTask", _build_empty_sync_background_model())
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/2/files"):
            response = weekly_logic.weekly_version_files_api(2)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    first_file = payload["files"][0]
    assert json.loads(first_file["commit_authors"]) == ["alice", "bob"]
    assert json.loads(first_file["commit_messages"]) == ["refs #123"]
    assert json.loads(first_file["commit_times"]) == []
    assert first_file["operations"] == []


def test_weekly_files_api_keeps_short_confirm_username_visible(monkeypatch):
    config = SimpleNamespace(
        id=3,
        project_id=1,
        repository=SimpleNamespace(
            name="repo_b",
            enable_id_confirmation=True,
        ),
    )
    fake_cache = SimpleNamespace(
        file_path="src/confirm.lua",
        commit_count=1,
        commit_authors='["alice"]',
        commit_messages='["ok"]',
        commit_times='["2026-03-04 12:00:00"]',
        overall_status="confirmed",
        status_changed_by="admin",
        confirmation_status='{"dev":"confirmed"}',
        last_sync_time=None,
        merged_diff_data='{"operations":["M"]}',
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
    monkeypatch.setattr(weekly_logic, "BackgroundTask", _build_empty_sync_background_model())
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/3/files"):
            response = weekly_logic.weekly_version_files_api(3)
            payload = response.get_json()

    assert payload["success"] is True
    first_file = payload["files"][0]
    assert first_file["confirm_user_display"] == "admin"


def test_weekly_files_api_triggers_sync_when_cache_empty(monkeypatch):
    config = SimpleNamespace(
        id=9,
        project_id=1,
        is_active=True,
        auto_sync=True,
        repository=SimpleNamespace(
            name="repo_sync",
            enable_id_confirmation=False,
        ),
    )

    fake_config_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _config_id: config),
    )
    fake_diff_model = SimpleNamespace(
        query=SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: []),
        ),
    )

    class _Field:
        def __eq__(self, _other):
            return self

        def in_(self, _values):
            return self

        def desc(self):
            return self

    class _BgQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    fake_background_model = SimpleNamespace(
        task_type=_Field(),
        commit_id=_Field(),
        status=_Field(),
        id=_Field(),
        query=_BgQuery(),
    )

    monkeypatch.setattr(weekly_logic, "WeeklyVersionConfig", fake_config_model)
    monkeypatch.setattr(weekly_logic, "WeeklyVersionDiffCache", fake_diff_model)
    monkeypatch.setattr(weekly_logic, "BackgroundTask", fake_background_model)
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda cid: 778 if cid == 9 else None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/9/files"):
            response = weekly_logic.weekly_version_files_api(9)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 0
    assert payload["sync_triggered"] is True
    assert payload["sync_task_id"] == 778
    assert payload["sync_task_status"] == "pending"


def test_weekly_files_api_exposes_processing_sync_status_even_when_files_exist(monkeypatch):
    config = SimpleNamespace(
        id=11,
        project_id=1,
        is_active=True,
        auto_sync=True,
        repository=SimpleNamespace(
            name="repo_processing",
            enable_id_confirmation=False,
        ),
    )
    fake_cache = SimpleNamespace(
        file_path="src/a.lua",
        commit_count=1,
        commit_authors='["alice"]',
        commit_messages='["m1"]',
        commit_times='["2026-03-09T12:00:00"]',
        overall_status="pending",
        status_changed_by="",
        confirmation_status='{"dev":"pending"}',
        last_sync_time=None,
        merged_diff_data='{"operations":["M"]}',
    )

    fake_config_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _config_id: config),
    )
    fake_diff_model = SimpleNamespace(
        query=SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]),
        ),
    )

    class _Field:
        def __eq__(self, _other):
            return self

        def in_(self, _values):
            return self

        def desc(self):
            return self

    class _BgQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return SimpleNamespace(id=9901, status="processing")

    fake_background_model = SimpleNamespace(
        task_type=_Field(),
        commit_id=_Field(),
        status=_Field(),
        id=_Field(),
        query=_BgQuery(),
    )

    monkeypatch.setattr(weekly_logic, "WeeklyVersionConfig", fake_config_model)
    monkeypatch.setattr(weekly_logic, "WeeklyVersionDiffCache", fake_diff_model)
    monkeypatch.setattr(weekly_logic, "BackgroundTask", fake_background_model)
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda _cid: None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/11/files"):
            response = weekly_logic.weekly_version_files_api(11)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    assert payload["sync_task_id"] == 9901
    assert payload["sync_task_status"] == "processing"
    assert payload["sync_triggered"] is False


def test_weekly_files_api_blocks_recent_config_until_initial_sync_completed(monkeypatch):
    config = SimpleNamespace(
        id=13,
        project_id=1,
        is_active=True,
        auto_sync=True,
        created_at=datetime.now(timezone.utc),
        repository=SimpleNamespace(
            name="repo_partial",
            enable_id_confirmation=False,
        ),
    )
    fake_cache = SimpleNamespace(
        file_path="src/partial.lua",
        commit_count=1,
        commit_authors='["alice"]',
        commit_messages='["m1"]',
        commit_times='["2026-03-09T12:00:00"]',
        overall_status="pending",
        status_changed_by="",
        confirmation_status='{"dev":"pending"}',
        last_sync_time=None,
        merged_diff_data='{"operations":["M"]}',
    )

    fake_config_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _config_id: config),
    )
    fake_diff_model = SimpleNamespace(
        query=SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]),
        ),
    )

    class _Field:
        def __eq__(self, _other):
            return self

        def in_(self, _values):
            return self

        def desc(self):
            return self

    class _BgQuery:
        def __init__(self, values):
            self._values = values
            self._idx = 0

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            if self._idx >= len(self._values):
                return None
            value = self._values[self._idx]
            self._idx += 1
            return value

    fake_background_model = SimpleNamespace(
        task_type=_Field(),
        commit_id=_Field(),
        status=_Field(),
        id=_Field(),
        query=_BgQuery([None, None, None]),
    )

    monkeypatch.setattr(weekly_logic, "WeeklyVersionConfig", fake_config_model)
    monkeypatch.setattr(weekly_logic, "WeeklyVersionDiffCache", fake_diff_model)
    monkeypatch.setattr(weekly_logic, "BackgroundTask", fake_background_model)
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda cid: 8813 if cid == 13 else None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/13/files"):
            response = weekly_logic.weekly_version_files_api(13)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    assert payload["sync_blocking"] is True
    assert payload["sync_triggered"] is True
    assert payload["sync_task_id"] == 8813
    assert payload["sync_task_status"] == "pending"


def test_merge_diff_template_uses_text_diff_classes_and_lines_renderer():
    content = _read("templates/merge_diff.html")
    assert "text-diff-container" in content
    assert "renderTextDiffFromLines" in content
    assert "Array.isArray(diffData.lines)" in content
