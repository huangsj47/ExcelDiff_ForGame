import uuid
from datetime import datetime, timezone

from app import app, create_tables, db
from models import (
    BackgroundTask,
    Commit,
    DiffCache,
    ExcelHtmlCache,
    MergedDiffCache,
    Project,
    Repository,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    WeeklyVersionExcelCache,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _admin_headers(token: str) -> dict:
    return {"X-Admin-Token": token}


def _create_project_and_repo(repo_type: str) -> tuple[Project, Repository]:
    project = Project(code=_uid("P0"), name=_uid("project"), department="QA")
    db.session.add(project)
    db.session.flush()

    if repo_type == "git":
        repository = Repository(
            project_id=project.id,
            name=_uid("repo_git"),
            type="git",
            url=f"https://example.com/{_uid('repo')}.git",
            server_url="https://example.com",
            branch="main",
            resource_type="table",
            clone_status="completed",
        )
    else:
        repository = Repository(
            project_id=project.id,
            name=_uid("repo_svn"),
            type="svn",
            url="https://svn.example.com/svn",
            root_directory="https://svn.example.com",
            username="tester",
            password="secret",
            current_version="100",
            resource_type="table",
            clone_status="completed",
        )

    db.session.add(repository)
    db.session.flush()
    return project, repository


def test_git_branch_switch_requires_confirm_flag_and_preserves_data(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    admin_token = _uid("admin_token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        _, repository = _create_project_and_repo("git")
        repository_id = repository.id
        original_branch = repository.branch

        db.session.add(
            Commit(
                repository_id=repository_id,
                commit_id=_uid("commit"),
                path="client_data/a.xlsx",
                author="dev",
                commit_time=datetime.now(timezone.utc),
                message="init",
            )
        )
        db.session.commit()

        with app.test_client() as client:
            response = client.post(
                f"/repositories/{repository_id}/update",
                data={
                    "name": repository.name,
                    "resource_type": "table",
                    "branch": "release",
                },
                headers=_admin_headers(admin_token),
                follow_redirects=False,
            )
            assert response.status_code in (302, 303)

        db.session.remove()
        refreshed = db.session.get(Repository, repository_id)
        assert refreshed is not None
        assert refreshed.branch == original_branch
        assert Commit.query.filter_by(repository_id=repository_id).count() == 1


def test_git_branch_switch_confirmed_clears_and_rebuilds(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    admin_token = _uid("admin_token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        project, repository = _create_project_and_repo("git")
        repository_id = repository.id

        c1 = Commit(
            repository_id=repository_id,
            commit_id=_uid("commit"),
            path="client_data/a.xlsx",
            author="dev1",
            commit_time=datetime.now(timezone.utc),
            message="A",
        )
        c2 = Commit(
            repository_id=repository_id,
            commit_id=_uid("commit"),
            path="client_data/b.xlsx",
            author="dev2",
            commit_time=datetime.now(timezone.utc),
            message="B",
        )
        db.session.add_all([c1, c2])
        db.session.flush()

        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repository_id,
            name=_uid("weekly"),
            branch="main",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(config)
        db.session.flush()
        config_id = config.id

        db.session.add_all(
            [
                DiffCache(
                    repository_id=repository_id,
                    commit_id=c1.commit_id,
                    file_path=c1.path,
                    cache_status="completed",
                ),
                ExcelHtmlCache(
                    repository_id=repository_id,
                    commit_id=c1.commit_id,
                    file_path=c1.path,
                    cache_key=_uid("html"),
                    cache_status="completed",
                ),
                MergedDiffCache(
                    repository_id=repository_id,
                    cache_key=_uid("merged"),
                    file_path=c1.path,
                    base_commit_id=c1.commit_id,
                    target_commit_id=c2.commit_id,
                    cache_status="completed",
                ),
                WeeklyVersionDiffCache(
                    config_id=config.id,
                    repository_id=repository_id,
                    file_path=c1.path,
                    latest_commit_id=c2.commit_id,
                    cache_status="completed",
                ),
                WeeklyVersionExcelCache(
                    config_id=config.id,
                    repository_id=repository_id,
                    file_path=c1.path,
                    cache_key=_uid("weekly_excel"),
                    latest_commit_id=c2.commit_id,
                    cache_status="completed",
                ),
                BackgroundTask(
                    task_type="auto_sync",
                    repository_id=repository_id,
                    status="pending",
                    priority=5,
                ),
                BackgroundTask(
                    task_type="excel_diff",
                    repository_id=repository_id,
                    commit_id=c1.commit_id,
                    file_path=c1.path,
                    status="processing",
                    priority=10,
                ),
                BackgroundTask(
                    task_type="auto_sync",
                    repository_id=repository_id,
                    status="completed",
                    priority=5,
                ),
                BackgroundTask(
                    task_type="weekly_sync",
                    commit_id=str(config_id),
                    status="pending",
                    priority=3,
                ),
                BackgroundTask(
                    task_type="weekly_excel_cache",
                    repository_id=config_id,
                    file_path=c1.path,
                    status="processing",
                    priority=3,
                ),
            ]
        )
        db.session.commit()

        with app.test_client() as client:
            response = client.post(
                f"/repositories/{repository_id}/update",
                data={
                    "name": repository.name,
                    "resource_type": "table",
                    "branch": "release",
                    "confirm_branch_switch": "1",
                },
                headers=_admin_headers(admin_token),
                follow_redirects=False,
            )
            assert response.status_code in (302, 303)

        db.session.remove()
        refreshed = db.session.get(Repository, repository_id)
        assert refreshed is not None
        assert refreshed.branch == "release"

        assert Commit.query.filter_by(repository_id=repository_id).count() == 0
        assert DiffCache.query.filter_by(repository_id=repository_id).count() == 0
        assert ExcelHtmlCache.query.filter_by(repository_id=repository_id).count() == 0
        assert MergedDiffCache.query.filter_by(repository_id=repository_id).count() == 0
        assert WeeklyVersionDiffCache.query.filter_by(repository_id=repository_id).count() == 0
        assert WeeklyVersionExcelCache.query.filter_by(repository_id=repository_id).count() == 0

        assert BackgroundTask.query.filter_by(
            task_type="excel_diff",
            repository_id=repository_id,
            status="processing",
        ).count() == 0
        assert BackgroundTask.query.filter_by(
            task_type="weekly_sync",
            commit_id=str(config_id),
            status="pending",
        ).count() == 0
        assert BackgroundTask.query.filter_by(
            task_type="weekly_excel_cache",
            repository_id=config_id,
            status="processing",
        ).count() == 0

        # 清理后会新建一个 auto_sync pending；原 completed 任务仍保留。
        assert BackgroundTask.query.filter_by(
            task_type="auto_sync",
            repository_id=repository_id,
            status="pending",
        ).count() == 1
        assert BackgroundTask.query.filter_by(
            task_type="auto_sync",
            repository_id=repository_id,
            status="completed",
        ).count() == 1


def test_svn_version_switch_updates_current_version_and_rebuilds(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    admin_token = _uid("admin_token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    with app.app_context():
        create_tables()
        _, repository = _create_project_and_repo("svn")
        repository_id = repository.id

        db.session.add(
            Commit(
                repository_id=repository_id,
                commit_id=_uid("r"),
                path="client_data/a.xlsx",
                version="100",
                author="dev",
                commit_time=datetime.now(timezone.utc),
                message="init",
            )
        )
        db.session.commit()

        with app.test_client() as client:
            response = client.post(
                f"/repositories/{repository_id}/update",
                data={
                    "name": repository.name,
                    "resource_type": "table",
                    "username": "tester",
                    "current_version": "120",
                    "confirm_branch_switch": "1",
                },
                headers=_admin_headers(admin_token),
                follow_redirects=False,
            )
            assert response.status_code in (302, 303)

        db.session.remove()
        refreshed = db.session.get(Repository, repository_id)
        assert refreshed is not None
        assert refreshed.current_version == "120"
        assert Commit.query.filter_by(repository_id=repository_id).count() == 0
        assert BackgroundTask.query.filter_by(
            task_type="auto_sync",
            repository_id=repository_id,
            status="pending",
        ).count() == 1
