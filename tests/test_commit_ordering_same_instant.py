"""同刻提交的定序：`commits[-1]`（也就是 `latest_commit_id`）必须是真正的最后一个提交。

## 为什么要单独钉

机器人提交会把 committer date 打成同一个固定时刻。线上实测：`226470e6c290` 与
`91bcc3439cf5` 的提交时间逐字相同（2026-09-17 17:00:47+08:00），而前者是后者的
**父提交** —— 平局时落回数据库行 id、行 id 是入库顺序（逐条 `git log` 新建），
于是快照的 `latest_commit_id` 指向前一个提交，模型据此判出一条并不存在的悬空引用。

这条缺陷**不会自曝**：缓存行、页面、日志全都自洽，只有拿两个提交去问 git 才能看出
谁是谁的父亲。所以这里既钉排序键本身，也钉「写缓存那条路径真的用上了它」。

## 提交 id 为什么每个用例现取

`services.commit_ordering` 的次序缓存是**进程级**的（理由见该模块的 docstring），
所以用例之间复用同一组 commit_id 会让后一个用例读到前一个用例留下的序号。
`_sha()` 就是为此：每个用例拿一组独一无二的 id。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.weekly_version_logic as weekly_logic
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.commit_ordering import (
    annotate_same_instant_order,
    commit_merge_sort_key,
    has_same_instant_commits,
    same_instant_rank,
)
from services.git_service import GitService

_TIE_TIME = datetime(2026, 9, 17, 9, 0, 47)


def _sha(tag: str) -> str:
    """一个 40 位的提交 id（只用十六进制字符，长得像真 sha）。"""
    return (tag.replace("_", "") + uuid.uuid4().hex)[:40].ljust(40, "0")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _Commit:
    """够用的 Commit 替身：排序键只读 id / repository_id / commit_id / commit_time。"""

    def __init__(self, db_id, commit_id, commit_time=_TIE_TIME, repository_id=None):
        self.id = db_id
        self.repository_id = repository_id if repository_id is not None else _sha("repo")
        self.commit_id = commit_id
        self.commit_time = commit_time


class _RecordingGitService:
    """按给定拓扑序作答的 git 替身，并记录被问过几次、问的是什么。"""

    def __init__(self, ordered_ids):
        self.ordered_ids = list(ordered_ids)
        self.calls = []

    def order_commits_by_topology(self, commit_ids):
        self.calls.append(list(commit_ids))
        known = set(commit_ids)
        return [c for c in self.ordered_ids if c in known]


class _ExplodingGitService:
    def order_commits_by_topology(self, commit_ids):  # pragma: no cover - 只该在有平局时被调
        raise AssertionError("没有平局时不该问 git")


class _StubWeeklyExcelCacheService:
    @staticmethod
    def needs_merged_diff_cache(_config_id, _file_path):
        return False

    @staticmethod
    def log_cache_operation(*_args, **_kwargs):
        return None


def test_平局组按git拓扑定序_最新的那个排在最后():
    # 入库顺序恰好是反的：父提交行 id 更大，行序会把它排在子提交后面。
    parent = _Commit(db_id=394, commit_id=_sha("parent"))
    child = _Commit(db_id=390, commit_id=_sha("child"), repository_id=parent.repository_id)

    assert [c.commit_id for c in sorted([parent, child], key=commit_merge_sort_key)] == [
        child.commit_id,
        parent.commit_id,
    ], "没有 git 答案时是行 id 兜底（与改动前逐字相同）"

    git_service = _RecordingGitService([parent.commit_id, child.commit_id])
    assert annotate_same_instant_order(git_service, [parent, child]) == 2
    assert git_service.calls == [[parent.commit_id, child.commit_id]]

    ordered = sorted([parent, child], key=commit_merge_sort_key)
    assert [c.commit_id for c in ordered] == [parent.commit_id, child.commit_id]
    assert ordered[-1] is child, "latest 必须是子提交"
    assert same_instant_rank(parent) == 0 and same_instant_rank(child) == 1


def test_没有平局时一次git都不调():
    early = _Commit(db_id=1, commit_id=_sha("early"), commit_time=_TIE_TIME)
    late = _Commit(db_id=2, commit_id=_sha("late"), commit_time=_TIE_TIME + timedelta(minutes=1))
    rows = [early, late]
    assert has_same_instant_commits(rows) is False
    assert annotate_same_instant_order(_ExplodingGitService(), rows) == 0
    assert [c.commit_id for c in sorted(rows, key=commit_merge_sort_key)] == [
        early.commit_id,
        late.commit_id,
    ]


def test_同一条提交被记了两次不算平局():
    same = _sha("same")
    rows = [_Commit(db_id=1, commit_id=same), _Commit(db_id=2, commit_id=same)]
    assert has_same_instant_commits(rows) is True
    assert annotate_same_instant_order(_ExplodingGitService(), rows) == 0


def test_git不可用时退回原次序且不抛():
    older = _Commit(db_id=394, commit_id=_sha("older"))
    newer = _Commit(db_id=390, commit_id=_sha("newer"), repository_id=older.repository_id)

    class _Failing:
        def order_commits_by_topology(self, commit_ids):
            raise RuntimeError("克隆缺失")

    assert annotate_same_instant_order(_Failing(), [older, newer]) == 0
    assert [c.commit_id for c in sorted([older, newer], key=commit_merge_sort_key)] == [
        newer.commit_id,
        older.commit_id,
    ]


@pytest.mark.parametrize("failure", ["no_clone", "non_zero", "empty_stdout"])
def test_git_service按拓扑排序的失败路径都退回传入次序(monkeypatch, tmp_path, failure):
    service = GitService("", root_directory=str(tmp_path))
    ids = [_sha("first"), _sha("second")]

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    if failure == "no_clone":
        monkeypatch.setattr(service, "local_path", str(tmp_path / "缺失"))
    else:
        result = _Result()
        if failure == "empty_stdout":
            result.returncode = 0
        monkeypatch.setattr(service, "_run_git_command", lambda *_a, **_k: result)

    assert service.order_commits_by_topology(ids) == ids


def test_git_service按rev_list输出的位置排序(monkeypatch, tmp_path):
    service = GitService("", root_directory=str(tmp_path))
    monkeypatch.setattr(service, "local_path", str(tmp_path))
    parent_sha = _sha("revparent")
    child_sha = _sha("revchild")
    seen = {}

    class _Result:
        returncode = 0
        stderr = ""

        def __init__(self):
            # 拓扑序：父提交在前；另外混进两条不属于本组的提交
            self.stdout = "\n".join(["other1", parent_sha, "other2", child_sha]) + "\n"

    def _fake_run(cmd, cwd=None, timeout=300):
        seen["cmd"] = list(cmd)
        return _Result()

    monkeypatch.setattr(service, "_run_git_command", _fake_run)

    ordered = service.order_commits_by_topology([child_sha, parent_sha])

    assert ordered == [parent_sha, child_sha]
    assert seen["cmd"][:4] == ["git", "rev-list", "--topo-order", "--reverse"]
    assert sorted(seen["cmd"][4:]) == sorted([parent_sha, child_sha])


def test_git_service输出里没有的提交排在最后(monkeypatch, tmp_path):
    service = GitService("", root_directory=str(tmp_path))
    monkeypatch.setattr(service, "local_path", str(tmp_path))
    known_sha = _sha("known")
    unknown_sha = _sha("unknown")

    class _Result:
        returncode = 0
        stderr = ""
        stdout = "other\n" + known_sha + "\n"

    monkeypatch.setattr(service, "_run_git_command", lambda *_a, **_k: _Result())

    assert service.order_commits_by_topology([unknown_sha, known_sha]) == [known_sha, unknown_sha]


def test_周版本同步把同刻提交的latest定到真正的最后一个(monkeypatch):
    """写缓存那条路径必须真的用上拓扑序 —— 只钉排序键会漏掉「没接线」。"""
    target_file = "code/pz/const/same_instant_const.lua"
    now_utc = datetime.now(timezone.utc)
    tie_time = now_utc - timedelta(hours=2)

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
            version="v0000000",
            operation="M",
            author="base_user",
            commit_time=now_utc - timedelta(days=2),
            message="base",
            status="pending",
        )
        # 真正的最后一个提交（子提交）先入库 —— 行 id 更小，行序会把它排在父提交前面。
        tip_commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("tip"),
            path=target_file,
            version="v2000000",
            operation="M",
            author="dev_b",
            commit_time=tie_time,
            message="调整段位重置规则",
            status="pending",
        )
        parent_commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("parent"),
            path=target_file,
            version="v1000000",
            operation="M",
            author="dev_a",
            commit_time=tie_time,
            message="添加排位积分规则",
            status="pending",
        )
        db.session.add_all([base_commit, tip_commit, parent_commit])
        db.session.commit()
        assert tip_commit.id < parent_commit.id, "用例前提：行序会把父提交排在后面"

        git_service = _RecordingGitService([parent_commit.commit_id, tip_commit.commit_id])
        monkeypatch.setattr(weekly_logic, "_get_git_service", lambda _repo: git_service)
        monkeypatch.setattr(weekly_logic, "_generate_merged_diff_data", lambda *_a, **_k: {"diff": "x"})
        monkeypatch.setattr(weekly_logic, "_weekly_excel_cache_service", _StubWeeklyExcelCacheService())

        outcome = weekly_logic.process_weekly_version_sync(config.id)

        assert outcome is not None
        assert git_service.calls, "检测到同刻提交就该去问一次 git 拓扑序"

        cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=config.id, file_path=target_file
        ).first()
        assert cache is not None
        assert cache.latest_commit_id == tip_commit.commit_id, "latest 被写成了父提交"


def test_仓库不是git或服务缺失时不去问拓扑序(monkeypatch):
    class _Repo:
        type = "svn"

    rows = [_Commit(db_id=1, commit_id=_sha("svn1")), _Commit(db_id=2, commit_id=_sha("svn2"))]
    monkeypatch.setattr(weekly_logic, "_get_git_service", None)
    assert weekly_logic._annotate_same_instant_order(_Repo(), rows) == 0


def test_取git服务本身出错也不打断同步(monkeypatch):
    def _boom(_repo):
        raise RuntimeError("仓库对象坏了")

    monkeypatch.setattr(weekly_logic, "_get_git_service", _boom)

    class _GitRepo:
        type = "git"

    rows = [_Commit(db_id=1, commit_id=_sha("boom1")), _Commit(db_id=2, commit_id=_sha("boom2"))]
    assert weekly_logic._annotate_same_instant_order(_GitRepo(), rows) == 0
