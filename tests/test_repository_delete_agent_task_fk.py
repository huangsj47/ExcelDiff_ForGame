import uuid

import services.repository_admin_handlers as repository_admin_handlers
from app import app, create_tables, db
from models import AgentTask, BackgroundTask, Project, Repository


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_delete_repository_removes_agent_tasks_before_background_tasks(monkeypatch):
    monkeypatch.setattr(
        repository_admin_handlers,
        "delete_local_repository_directory",
        lambda *_args, **_kwargs: None,
    )

    with app.app_context():
        create_tables()

        project = Project(code=_uid("P"), name=_uid("proj"))
        db.session.add(project)
        db.session.flush()

        repo = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="ssh://git@example.com/group/repo.git",
            branch="master",
            clone_status="failed",
        )
        db.session.add(repo)
        db.session.flush()

        source_task = BackgroundTask(
            task_type="auto_sync",
            repository_id=repo.id,
            status="failed",
            error_message="git clone failed",
        )
        db.session.add(source_task)
        db.session.flush()

        agent_task = AgentTask(
            task_type="auto_sync",
            priority=10,
            project_id=project.id,
            repository_id=repo.id,
            source_task_id=source_task.id,
            payload="{}",
            status="failed",
            error_message="agent local executor crashed",
        )
        db.session.add(agent_task)
        db.session.commit()

        with app.test_request_context(f"/repositories/{repo.id}/delete", method="POST"):
            response = repository_admin_handlers.delete_repository.__wrapped__(repo.id)
            assert response.status_code == 302

        assert db.session.get(Repository, repo.id) is None
        assert BackgroundTask.query.filter_by(repository_id=repo.id).count() == 0
        assert AgentTask.query.filter_by(repository_id=repo.id).count() == 0
