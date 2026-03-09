from datetime import datetime, timezone
from types import SimpleNamespace

from flask import Flask

import services.weekly_version_logic as weekly_logic


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


def test_weekly_files_non_initial_processing_sync_should_not_block(monkeypatch):
    config = SimpleNamespace(
        id=21,
        project_id=1,
        is_active=True,
        auto_sync=True,
        repository=SimpleNamespace(name="repo_processing", enable_id_confirmation=False),
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

    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionConfig",
        SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _config_id: config)),
    )
    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionDiffCache",
        SimpleNamespace(
            query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]))
        ),
    )
    monkeypatch.setattr(
        weekly_logic,
        "BackgroundTask",
        SimpleNamespace(
            task_type=_Field(),
            commit_id=_Field(),
            status=_Field(),
            id=_Field(),
            # latest_sync_task -> processing；existing_sync_task -> processing
            query=_BgQuery([SimpleNamespace(id=2101, status="processing"), SimpleNamespace(id=2101, status="processing")]),
        ),
    )
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda _cid: None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/21/files"):
            response = weekly_logic.weekly_version_files_api(21)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    assert payload["sync_task_status"] == "processing"
    assert payload["sync_blocking"] is False


def test_weekly_files_recent_config_with_completed_sync_should_not_block(monkeypatch):
    config = SimpleNamespace(
        id=22,
        project_id=1,
        is_active=True,
        auto_sync=True,
        created_at=datetime.now(timezone.utc),
        repository=SimpleNamespace(name="repo_recent_done", enable_id_confirmation=False),
    )
    fake_cache = SimpleNamespace(
        file_path="src/recent_done.lua",
        commit_count=2,
        commit_authors='["alice"]',
        commit_messages='["m1"]',
        commit_times='["2026-03-09T12:00:00"]',
        overall_status="pending",
        status_changed_by="",
        confirmation_status='{"dev":"pending"}',
        last_sync_time=None,
        merged_diff_data='{"operations":["M"]}',
    )

    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionConfig",
        SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _config_id: config)),
    )
    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionDiffCache",
        SimpleNamespace(
            query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]))
        ),
    )
    monkeypatch.setattr(
        weekly_logic,
        "BackgroundTask",
        SimpleNamespace(
            task_type=_Field(),
            commit_id=_Field(),
            status=_Field(),
            id=_Field(),
            # latest_sync_task -> processing；existing_sync_task -> processing；completed_sync_task -> completed
            query=_BgQuery(
                [
                    SimpleNamespace(id=2201, status="processing"),
                    SimpleNamespace(id=2201, status="processing"),
                    SimpleNamespace(id=2199, status="completed"),
                ]
            ),
        ),
    )
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda _cid: None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/22/files"):
            response = weekly_logic.weekly_version_files_api(22)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    assert payload["sync_task_status"] == "processing"
    assert payload["sync_blocking"] is False
    assert payload["sync_triggered"] is False


def test_weekly_files_recent_config_with_initial_cache_data_should_not_block(monkeypatch):
    config = SimpleNamespace(
        id=23,
        project_id=1,
        is_active=True,
        auto_sync=True,
        created_at=datetime.now(timezone.utc),
        repository=SimpleNamespace(name="repo_recent_data", enable_id_confirmation=False),
    )
    fake_cache = SimpleNamespace(
        file_path="src/ready.lua",
        commit_count=1,
        commit_authors='["alice"]',
        commit_messages='["m1"]',
        commit_times='["2026-03-09T12:00:00"]',
        overall_status="pending",
        status_changed_by="",
        confirmation_status='{"dev":"pending"}',
        last_sync_time=datetime.now(timezone.utc),
        merged_diff_data='{"operations":["M"]}',
    )

    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionConfig",
        SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _config_id: config)),
    )
    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionDiffCache",
        SimpleNamespace(
            query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]))
        ),
    )
    monkeypatch.setattr(
        weekly_logic,
        "BackgroundTask",
        SimpleNamespace(
            task_type=_Field(),
            commit_id=_Field(),
            status=_Field(),
            id=_Field(),
            query=_BgQuery(
                [
                    SimpleNamespace(id=2301, status="pending", created_at=datetime.now(timezone.utc), started_at=None, error_message=None),
                    SimpleNamespace(id=2301, status="pending", created_at=datetime.now(timezone.utc), started_at=None, error_message=None),
                    None,
                ]
            ),
        ),
    )
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda _cid: None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/23/files"):
            response = weekly_logic.weekly_version_files_api(23)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    assert payload["sync_task_status"] == "pending"
    assert payload["sync_blocking"] is False


def test_weekly_files_stale_pending_sync_task_should_not_block(monkeypatch):
    old_time = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    config = SimpleNamespace(
        id=24,
        project_id=1,
        is_active=True,
        auto_sync=True,
        created_at=datetime.now(timezone.utc),
        repository=SimpleNamespace(name="repo_stale_task", enable_id_confirmation=False),
    )
    fake_cache = SimpleNamespace(
        file_path="src/stale.lua",
        commit_count=1,
        commit_authors='["alice"]',
        commit_messages='["m1"]',
        commit_times='["2026-03-09T12:00:00"]',
        overall_status="pending",
        status_changed_by="",
        confirmation_status='{"dev":"pending"}',
        last_sync_time=datetime.now(timezone.utc),
        merged_diff_data='{"operations":["M"]}',
    )
    stale_task = SimpleNamespace(
        id=2401,
        status="pending",
        created_at=old_time,
        started_at=None,
        error_message=None,
    )

    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionConfig",
        SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _config_id: config)),
    )
    monkeypatch.setattr(
        weekly_logic,
        "WeeklyVersionDiffCache",
        SimpleNamespace(
            query=SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [fake_cache]))
        ),
    )
    monkeypatch.setattr(
        weekly_logic,
        "BackgroundTask",
        SimpleNamespace(
            task_type=_Field(),
            commit_id=_Field(),
            status=_Field(),
            id=_Field(),
            query=_BgQuery([stale_task, stale_task, None]),
        ),
    )
    monkeypatch.setattr(weekly_logic, "_create_weekly_sync_task", lambda _cid: None)
    monkeypatch.setattr(weekly_logic, "_has_project_access", lambda _project_id: True)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context("/weekly-version-config/24/files"):
            response = weekly_logic.weekly_version_files_api(24)
            payload = response.get_json()

    assert payload["success"] is True
    assert payload["total_files"] == 1
    assert payload["sync_blocking"] is False
