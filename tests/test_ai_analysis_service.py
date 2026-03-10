import uuid
from datetime import datetime, timedelta, timezone

from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import AiProjectApiKey, AiWeeklyAnalysisState
import services.ai_analysis_service as ai_service


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_project():
    project = Project(code=_uid("P"), name=_uid("ai-project"))
    db.session.add(project)
    db.session.flush()
    return project


def _create_repo(project_id: int, name: str, repo_type: str, resource_type: str) -> Repository:
    repo = Repository(
        project_id=project_id,
        name=name,
        type=repo_type,
        url=f"https://example.com/{name}.git",
        branch="main",
        resource_type=resource_type,
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    return repo


def _create_weekly_config(project_id: int, repo: Repository, base_name: str, start_time, end_time):
    cfg = WeeklyVersionConfig(
        project_id=project_id,
        repository_id=repo.id,
        name=f"{base_name} - {repo.name}",
        description="",
        branch="main",
        start_time=start_time,
        end_time=end_time,
        cycle_type="custom",
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    return cfg


def _seed_diff_cache(config: WeeklyVersionConfig, repo: Repository, path: str, updated_at: datetime):
    cache = WeeklyVersionDiffCache(
        config_id=config.id,
        repository_id=repo.id,
        file_path=path,
        file_type="code",
        latest_commit_id=_uid("c"),
        commit_count=1,
        updated_at=updated_at,
    )
    db.session.add(cache)


def test_ai_weekly_payload_scope_and_policy():
    with app.app_context():
        create_tables()
        project = _create_project()
        repo_code = _create_repo(project.id, _uid("code"), "git", "code")
        repo_table = _create_repo(project.id, _uid("table"), "svn", "table")

        start_time = datetime(2026, 3, 1, 0, 0)
        end_time = datetime(2026, 3, 8, 0, 0)

        cfg_code = _create_weekly_config(project.id, repo_code, "W1", start_time, end_time)
        cfg_table = _create_weekly_config(project.id, repo_table, "W1", start_time, end_time)

        base_time = datetime.now(timezone.utc) - timedelta(hours=2)
        for i in range(5):
            _seed_diff_cache(cfg_code, repo_code, f"src/file_{i}.py", base_time)
            _seed_diff_cache(cfg_table, repo_table, f"data/file_{i}.csv", base_time)
        db.session.commit()

        payload, state, skip_reason = ai_service.build_weekly_payload(cfg_code.id)
        assert skip_reason is None
        assert payload["scope"] == "full"
        assert payload["execution"]["version"] == "latest"
        assert payload["policy"]["allow_cross_file"] is True
        assert payload["repositories"][0]["repository_id"] == repo_code.id

        last_analyzed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        group_key = ai_service.build_weekly_group_key(cfg_code)
        state = AiWeeklyAnalysisState(
            project_id=project.id,
            group_key=group_key,
            base_name="W1",
            start_time=start_time,
            end_time=end_time,
            last_analyzed_at=last_analyzed_at,
        )
        db.session.add(state)
        db.session.commit()

        updated_entry = WeeklyVersionDiffCache.query.filter_by(config_id=cfg_code.id).first()
        updated_entry.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        payload, state, skip_reason = ai_service.build_weekly_payload(cfg_code.id)
        assert skip_reason is None
        assert payload["scope"] == "incremental"
        assert payload["policy"]["reason"] == "delta_small"


def test_ai_project_api_key_status(monkeypatch):
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        monkeypatch.setattr(ai_service, "encrypt_dpapi", lambda value: f"dpapi::{value}")
        monkeypatch.setattr(ai_service, "decrypt_dpapi", lambda value: str(value).replace("dpapi::", ""))

        ok, message = ai_service.set_project_api_key(project.id, "secret-key", updated_by="tester")
        assert ok is True
        assert "updated" in message.lower()

        record = AiProjectApiKey.query.filter_by(project_id=project.id).first()
        assert record is not None
        assert record.encrypted_key.startswith("dpapi::")

        status = ai_service.get_project_api_key_status(project.id)
        assert status["configured"] is True
        assert status["updated_at"]


def test_ai_project_analysis_config_update():
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        config = ai_service.get_project_analysis_config(project.id)
        assert config["configured"] is False
        assert config["weekly_interval_minutes"] == 60

        ok, message = ai_service.update_project_analysis_config(
            project.id,
            {
                "weekly_interval_minutes": 15,
                "auto_weekly_enabled": False,
                "max_files_per_run": 150,
                "prompt_template": "test prompt",
            },
            updated_by="tester",
        )
        assert ok is True
        assert "updated" in message.lower()

        updated = ai_service.get_project_analysis_config(project.id)
        assert updated["configured"] is True
        assert updated["weekly_interval_minutes"] == 15
        assert updated["auto_weekly_enabled"] is False
        assert updated["max_files_per_run"] == 150
        assert updated["prompt_template"] == "test prompt"
