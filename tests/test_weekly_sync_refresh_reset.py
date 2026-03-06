import json
import uuid
from datetime import datetime, timedelta, timezone

import services.weekly_version_logic as weekly_logic
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _StubWeeklyExcelCacheService:
    @staticmethod
    def needs_merged_diff_cache(_config_id, _file_path):
        return False


def test_generate_weekly_diff_resets_confirmation_when_latest_commit_changes(monkeypatch):
    target_file = "config/weekly/reset_case.lua"
    now_utc = datetime.now(timezone.utc)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("project"), department="QA")
        db.session.add(project)
        db.session.flush()

        repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="https://example.com/demo/repo.git",
            branch="main",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()

        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repository.id,
            name=_uid("weekly"),
            branch="main",
            start_time=now_utc - timedelta(days=1),
            end_time=now_utc + timedelta(days=1),
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(config)
        db.session.flush()

        base_commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("base"),
            path=target_file,
            version="base0001",
            operation="M",
            author="base_user",
            commit_time=config.start_time - timedelta(hours=1),
            message="base",
            status="pending",
        )
        commit_v1 = Commit(
            repository_id=repository.id,
            commit_id=_uid("v1"),
            path=target_file,
            version="v1000001",
            operation="M",
            author="dev_a",
            commit_time=config.start_time + timedelta(minutes=10),
            message="first update",
            status="pending",
        )
        commit_v2 = Commit(
            repository_id=repository.id,
            commit_id=_uid("v2"),
            path=target_file,
            version="v2000001",
            operation="M",
            author="dev_b",
            commit_time=config.start_time + timedelta(minutes=20),
            message="second update",
            status="pending",
        )
        db.session.add(base_commit)
        db.session.add(commit_v1)
        db.session.add(commit_v2)
        db.session.flush()

        existing_cache = WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repository.id,
            file_path=target_file,
            merged_diff_data=json.dumps({"old": True}),
            base_commit_id=base_commit.commit_id,
            latest_commit_id=commit_v1.commit_id,
            commit_authors=json.dumps(["dev_a"]),
            commit_messages=json.dumps(["first update"]),
            commit_times=json.dumps([commit_v1.commit_time.isoformat()]),
            commit_count=1,
            confirmation_status=json.dumps({"dev": "confirmed"}),
            overall_status="confirmed",
            cache_status="completed",
            last_sync_time=now_utc - timedelta(minutes=5),
        )
        db.session.add(existing_cache)
        db.session.commit()

        monkeypatch.setattr(
            weekly_logic,
            "_generate_merged_diff_data",
            lambda *_args, **_kwargs: {"diff": "updated"},
        )
        monkeypatch.setattr(
            weekly_logic,
            "_weekly_excel_cache_service",
            _StubWeeklyExcelCacheService(),
        )

        weekly_logic.generate_weekly_merged_diff(config, target_file, [commit_v1, commit_v2])

        db.session.expire_all()
        updated_cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config.id,
            file_path=target_file,
        ).first()
        assert updated_cache is not None
        assert updated_cache.latest_commit_id == commit_v2.commit_id
        assert updated_cache.overall_status == "pending"
        assert json.loads(updated_cache.confirmation_status or "{}").get("dev") == "pending"
