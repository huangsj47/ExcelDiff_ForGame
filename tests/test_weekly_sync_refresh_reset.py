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


def test_unchanged_content_does_not_touch_the_cache_row(monkeypatch):
    """内容没变就不写库：`updated_at` 必须停在「内容最后变化的时刻」。

    为什么这条要紧：AI 分析的变更判据读的正是 `updated_at`
    （`_summarize_weekly_files` 筛 `updated_at > last_analyzed_at`），而同步是**逐文件**
    重写缓存的。每 2~3 分钟一轮同步把每一行的 `updated_at` 都顶高一次，平台就**永远**
    认为「有新变化」：既不停调度新的自动分析，手动触发也总是真跑而不是回放上次结论。
    """
    target_file = "code/pz/const/unchanged_case.lua"
    payload = {"diff": "same"}
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
            repository_id=repository.id, commit_id=_uid("base"), path=target_file,
            version="v0000000", operation="M", author="base_user",
            commit_time=now_utc - timedelta(days=2), message="base", status="pending",
        )
        commit_v1 = Commit(
            repository_id=repository.id, commit_id=_uid("v1"), path=target_file,
            version="v1000000", operation="M", author="dev_a",
            commit_time=now_utc - timedelta(hours=2), message="only change", status="pending",
        )
        db.session.add_all([base_commit, commit_v1])
        db.session.flush()

        cache = WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repository.id,
            file_path=target_file,
            merged_diff_data=json.dumps(payload),
            base_commit_id=base_commit.commit_id,
            latest_commit_id=commit_v1.commit_id,
            commit_authors=json.dumps(["dev_a"]),
            commit_messages=json.dumps(["only change"]),
            commit_times=json.dumps([commit_v1.commit_time.isoformat()]),
            commit_count=1,
            confirmation_status=json.dumps({"dev": "confirmed"}),
            overall_status="confirmed",
            cache_status="completed",
            diff_version=weekly_logic._current_diff_logic_version(),
            last_sync_time=now_utc - timedelta(minutes=5),
        )
        db.session.add(cache)
        db.session.commit()
        cache_id = cache.id
        before_stamp = cache.updated_at

        monkeypatch.setattr(weekly_logic, "_generate_merged_diff_data", lambda *_a, **_k: dict(payload))
        monkeypatch.setattr(weekly_logic, "_weekly_excel_cache_service", _StubWeeklyExcelCacheService())

        result = weekly_logic.generate_weekly_merged_diff(config, target_file, [commit_v1])

        # `updated=False` 是把「跳过写入」这条分支本身钉住 —— 只比 updated_at 的话，
        # 一个「压根没走到这里」的实现也能让上面那句通过。
        assert result is not None and result.updated is False

        db.session.expire_all()
        stored = db.session.get(WeeklyVersionDiffCache, cache_id)
        assert stored.updated_at == before_stamp, "内容没变却把 updated_at 顶高了"


def test_changed_content_still_writes_the_cache_row(monkeypatch):
    """**反自检**：「内容没变就不写」不能变成「永远不写」。

    内容真的变了（这里让重算结果与库里那行不同）却跳过写入，缓存就永久停在旧状态 ——
    而它正是页面与 AI 分析读的那一份。
    """
    target_file = "code/pz/const/changed_case.lua"
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
            repository_id=repository.id, commit_id=_uid("base"), path=target_file,
            version="v0000000", operation="M", author="base_user",
            commit_time=now_utc - timedelta(days=2), message="base", status="pending",
        )
        commit_v1 = Commit(
            repository_id=repository.id, commit_id=_uid("v1"), path=target_file,
            version="v1000000", operation="M", author="dev_a",
            commit_time=now_utc - timedelta(hours=2), message="first", status="pending",
        )
        db.session.add_all([base_commit, commit_v1])
        db.session.flush()

        cache = WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repository.id,
            file_path=target_file,
            merged_diff_data=json.dumps({"diff": "old"}),
            base_commit_id=base_commit.commit_id,
            latest_commit_id=commit_v1.commit_id,
            commit_authors=json.dumps(["dev_a"]),
            commit_messages=json.dumps(["first"]),
            commit_times=json.dumps([commit_v1.commit_time.isoformat()]),
            commit_count=1,
            confirmation_status=json.dumps({"dev": "confirmed"}),
            overall_status="confirmed",
            cache_status="completed",
            diff_version=weekly_logic._current_diff_logic_version(),
            last_sync_time=now_utc - timedelta(minutes=5),
        )
        db.session.add(cache)
        db.session.commit()
        cache_id = cache.id

        monkeypatch.setattr(weekly_logic, "_generate_merged_diff_data", lambda *_a, **_k: {"diff": "new"})
        monkeypatch.setattr(weekly_logic, "_weekly_excel_cache_service", _StubWeeklyExcelCacheService())

        result = weekly_logic.generate_weekly_merged_diff(config, target_file, [commit_v1])

        assert result is not None and result.updated is True, "内容变了却没写库"

        db.session.expire_all()
        stored = db.session.get(WeeklyVersionDiffCache, cache_id)
        assert json.loads(stored.merged_diff_data) == {"diff": "new"}
