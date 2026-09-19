import uuid
from datetime import datetime, timedelta, timezone

import services.repository_admin_handlers as repository_admin_handlers
from app import app, create_tables, db
from models import AgentTask, BackgroundTask, Project, Repository, WeeklyVersionConfig


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


def test_delete_repository_removes_the_weekly_version_configs_that_reference_it(monkeypatch):
    """**只要仓库被一个周版本配置引用着，它就永远删不掉。**

    `WeeklyVersionConfig.repository_id` 是 `nullable=False`，而 ORM 删父行时默认把子行的
    外键置 NULL → `IntegrityError` → 整个事务回滚。表现是 delete 永远失败，而界面上
    只 flash 一句「删除仓库失败: NOT NULL constraint failed:
    weekly_version_config.repository_id」（`delete_project` 是显式删了这一张表的）。

    这个坑只有「仓库真的被周版本配过」才踩得到 —— 上面那个用例没配周版本，所以它
    一直绿。
    """
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

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repo.id,
            name=_uid("周版本"),
            branch="master",
            start_time=now,
            end_time=now + timedelta(days=7),
        )
        db.session.add(config)
        db.session.flush()
        config_id = config.id
        db.session.commit()

        with app.test_request_context(f"/repositories/{repo.id}/delete", method="POST"):
            response = repository_admin_handlers.delete_repository.__wrapped__(repo.id)
            assert response.status_code == 302, "删除失败时它也会 302，所以下面必须直接查库"

        assert db.session.get(Repository, repo.id) is None, (
            "仓库还在 —— 多半是那一步 flush 又 IntegrityError 回滚了"
        )
        assert db.session.get(WeeklyVersionConfig, config_id) is None, (
            "周版本配置没跟着删（它就是拦住删除的那一行）"
        )
