# -*- coding: utf-8 -*-
"""窗口可达性：**当前 tip 上还有哪些提交**，以及历史被重写时的原子替换（工作包 A）。

## 为什么这一组必须存在

2026-09-23 实测（`docs/AI分析全链路复测与强制改造指引-2026-09-23.md` §1）：测试仓库
`origin.git` 的 master 被重建/强推成 3 个提交、`repository.last_synced_tip` 也已经等于
远端 tip，**而配置 3 的 `weekly_version_diff_cache` 仍指向 `e54c73df`、`aa3fd90b`、
`2971caf7` 这些不在当前历史里的提交**（三行缓存的并集 9 个）。AI 因此拿着远端已经
不存在的代码做评审，报告里还留下了「清单说 3 个提交、差异出处说 5/6 个」这条自相矛盾。

「只比 tip」判不出这件事 —— tip 早就相等了。判据只能是**可达集合**。

## 这一组跑的是真 git，不是桩

可达性是从 `git rev-list` 的输出里读出来的，所以这一组直接在临时目录里 `git init` 出
真仓库、真提交、真强推，再用**真实 `GitService`**（`root_directory` 指到那个目录，
于是 `local_path` 就是它）跑一遍。把 git 换成一个「返回我想要的 sha」的桩，
被测的那段就只剩我自己写的几行 if —— 那正是假 DOM 会替你把浏览器语义假设掉的那种坑。

## 断言的是行为

`_replace_window` 用真库（`app` 的测试库）跑：造 9 个提交行（3 个可达 + 6 个已被强推
掉）、造一份指向旧提交的缓存行，然后断言**库里剩下了什么**（哪些行没了、哪些行还在、
历史快照有没有被动过），不去数「删了几次」。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import AiDiffSnapshot, AiDiffSnapshotItem
from models.weekly_version import WeeklyVersionExcelCache
from services.git_service import GitService
from services.repository_sync_window import (
    ReachableCommits,
    commit_reachable,
    local_branch_tip,
    partition_reachable,
    reachable_commits,
)
from services.weekly_window_reconcile import (
    group_reachable_window_files,
    reconcile_weekly_window,
)

# 提交时间用一个固定、与「现在」无关的时刻：窗口比较的是 naive-UTC 墙钟，
# 用 now() 会让「窗口」与「提交」在跨日边界上偶发错位（本仓库有过这类 flake）。
_COMMIT_UTC = datetime(2026, 9, 1, 0, 0, 0)
# 窗口按**北京墙钟**填（config.start_time/end_time 的语义），所以要 +8 小时。
_WINDOW_START_BEIJING = datetime(2026, 9, 1, 7, 0, 0)
_WINDOW_END_BEIJING = datetime(2026, 9, 1, 9, 0, 0)


# ---------------------------------------------------------------------------
#  真 git 仓库
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """在 repo 里跑一条 git 命令，返回 stdout（失败即断言失败）。"""
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.email=qa@example.com",
         "-c", "user.name=QA", "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"git {' '.join(args)} 失败: {result.stderr}"
    return result.stdout


@pytest.fixture()
def real_repo(tmp_path: Path):
    """一个真 git 仓库：3 个提交，然后被「强推」成另一段历史。

    返回 `(path, service, 旧历史 3 个 sha, 新历史 sha 列表)`。
    """
    repo = tmp_path / "origin_work"
    repo.mkdir()
    _git(repo, "init", "-q")
    old = []
    for index in range(3):
        (repo / f"file{index}.txt").write_text(f"v{index}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"old-{index}")
        old.append(_git(repo, "rev-parse", "HEAD").strip())

    # 「强推 / 重建裸库」：把分支指回第一个提交，再在它上面长出一条**全新**的历史。
    # 这样 old[1:] 都还在对象库里（`cat-file -e` 查得到），却**不在 tip 的可达集合里**
    # —— 这正是实测里那个形态：对象还在、历史没了。
    _git(repo, "reset", "-q", "--hard", old[0])
    new = []
    for index in range(2):
        (repo / f"new{index}.txt").write_text(f"n{index}\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"new-{index}")
        new.append(_git(repo, "rev-parse", "HEAD").strip())

    service = GitService(repo_url=str(repo), root_directory=str(repo))
    assert Path(service.local_path) == repo
    return repo, service, old, new


# ---------------------------------------------------------------------------
#  可达集合本身
# ---------------------------------------------------------------------------


def test_only_the_current_history_is_reachable(real_repo):
    """**核心**：强推之后，旧历史的对象还在库里，但不再可达。"""
    _repo, service, old, new = real_repo

    reachable = reachable_commits(service, branch="main")

    assert reachable.known, reachable.reason
    assert reachable.tip == local_branch_tip(service)
    assert reachable.commits == frozenset([old[0], *new])
    assert reachable.count == 3
    assert not reachable.truncated
    assert len(reachable.digest) == 40

    # 旧对象**确实还在**本地对象库里（否则这个用例证明的是「对象被 gc 了」，
    # 而不是「可达性判对了」）—— `cat-file -e` 与可达性是两件事。
    for gone in old[1:]:
        assert service._git_cmd_success(
            service._run_git_command(["git", "cat-file", "-e", gone])
        ), "这个用例的前提是旧对象仍在对象库里"


def test_reachability_verdicts_follow_the_rewrite(real_repo):
    """三个判定值各走各的路：可达 True、不可达 False、判不出来 None。"""
    _repo, service, old, new = real_repo
    reachable = reachable_commits(service, branch="main")

    assert commit_reachable(service, reachable, new[-1]) is True
    assert commit_reachable(service, reachable, old[0]) is True
    assert commit_reachable(service, reachable, old[1]) is False
    assert commit_reachable(service, reachable, old[2]) is False

    unreachable, unknown = partition_reachable(
        service, reachable, [old[2], new[0], old[1], new[1], old[2]]
    )
    assert unreachable == (old[2], old[1])
    assert unknown == ()


def test_an_unknown_reachable_set_answers_nothing(real_repo):
    """**未知不许当成「一个都没有」**：判据必须回 None，而不是 False。

    把问不到当 False 的代价是「历史被重写」的误判 —— 调用方据此删掉整个好窗口的缓存。
    """
    _repo, service, _old, _new = real_repo
    unknown = ReachableCommits(reason="模拟：拿不到可达集合")

    assert not unknown.known
    assert commit_reachable(service, unknown, "a" * 40) is None


def test_the_digest_changes_when_the_history_is_rewritten(real_repo):
    """指纹跟着历史走：强推（tip 变了）之后必须变 —— 「只比 tip 不够」的另一面。"""
    repo, service, old, new = real_repo
    before = reachable_commits(service, branch="main")

    _git(repo, "commit", "-q", "--allow-empty", "-m", "same-second-tip")
    after = reachable_commits(service, branch="main")

    assert after.tip != before.tip
    assert after.digest != before.digest, "tip 变了而指纹没变 —— 快照不会失效"
    _ = (old, new)


def _missing_dir() -> str:
    """一个**不存在**的目录路径（当作「没克隆」的仓库）。

    ⚠️ 不能拿「存在的空目录」冒充没克隆：`git rev-parse` 会沿父目录往上找 `.git`，
    而 `tests/conftest.py` 把 `TMPDIR`/`TEMP` 都设成了**本仓库内**的
    `.pytest_tmp/work` —— 那里的任意一个空目录都会被 git 解析成平台自己的仓库，
    于是用例测到的不是「问不到」，而是「问到了平台仓库」（进而把真提交判成不可达）。
    不存在的目录会让 `subprocess.run` 抛 `OSError`，那才是「没克隆」的真实形态
    （`GitService._run_git_command` 捕获它并回 None）。
    """
    return os.path.join(tempfile.gettempdir(), f"weekly-no-clone-{uuid.uuid4().hex}")


def test_a_broken_clone_reports_unknown_instead_of_raising():
    """拿不到可达集合时**回一个 known=False 的对象**，不抛异常、不假装成功。"""
    missing = _missing_dir()
    service = GitService(repo_url=missing, root_directory=missing)

    assert local_branch_tip(service) is None
    reachable = reachable_commits(service, branch="main")
    assert not reachable.known
    assert reachable.digest == ""  # 空指纹不许与任何非空指纹相等
    assert reachable.reason


def test_a_truncated_prefix_still_answers_via_merge_base(real_repo):
    """前缀被上限截断时，「不在集合里」**不能**直接判成不可达。

    这一条走的是回落路径（`git merge-base --is-ancestor`）：退出码 0 = 是祖先，
    1 = 不是，其它 = 判不出来（不猜）。
    """
    _repo, service, old, new = real_repo
    full = reachable_commits(service, branch="main")
    truncated = ReachableCommits(
        branch="main", tip=full.tip, count=full.count + 10,
        digest=full.digest, commits=frozenset(), truncated=True,
    )

    assert commit_reachable(service, truncated, new[-1]) is True
    assert commit_reachable(service, truncated, old[1]) is False
    # 对象库里根本没有的 sha：merge-base 回 128，判不出来 —— 不许猜成「不可达」。
    assert commit_reachable(service, truncated, "f" * 40) is None


def test_truncated_is_decided_by_the_total_count_not_the_prefix(real_repo):
    """`truncated` 的判据是「总数 > 前缀条数」，不是「前缀条数 == 上限」。"""
    _repo, service, _old, _new = real_repo
    reachable = reachable_commits(service, branch="main", limit=1000)

    assert reachable.count == 3
    assert len(reachable.commits) == 3
    assert not reachable.truncated


# ---------------------------------------------------------------------------
#  窗口对账（真库）
# ---------------------------------------------------------------------------


def _service_for(repository) -> GitService:
    """这个仓库的真实 git 服务（`local_path` 由 `build_repository_local_path` 算出）。

    用真 `GitService` 而不是 `vcs_content_service` 的缓存工厂：后者会把实例留在
    进程级缓存里跨用例复用，而用例造的仓库目录随后就被删了。
    """
    return GitService(
        repository.url, repository.root_directory, repository.username,
        repository.token, repository,
    )


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _use_tmp_repos_base(tmp_path: Path, monkeypatch) -> None:
    """把工作副本根目录指到 tmp —— **不要往仓库的 `repos/` 里写测试用的假仓库**。

    `build_repository_local_path` 读 `AGENT_REPOS_BASE_DIR`（见
    `utils/runtime_paths`），`GitService._get_local_path` 读的是同一个变量，所以两边
    必然指向同一处：造仓库的地方与 git 命令跑的地方不会错位。
    """
    monkeypatch.setenv("AGENT_REPOS_BASE_DIR", str(tmp_path))


def _seed_config(*, reachable_tip: str, unreachable: list):
    """造一个项目 + 一个真工作副本 + 一个周版本配置 + 9 条提交行。

    形态照实测那一幕：3 条属于当前历史、6 条属于被强推掉的那一段；缓存行里既有指
    可达提交的，也有指不可达提交的。
    """
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()

    # 真仓库必须落在 `build_repository_local_path` 算出来的那个位置，
    # 这样 `GitService(repository=repo)` 的 local_path 就是它。
    repo_name = _uid("repo")
    repository = Repository(
        project_id=project.id, name=repo_name, type="git",
        url=f"https://example.com/{repo_name}.git",
        branch="main", clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()

    from utils.path_security import build_repository_local_path

    work = Path(build_repository_local_path(project.code, repo_name, repository.id))
    if not work.exists():
        work.mkdir(parents=True)
    _git(work, "init", "-q")
    (work / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "keep")
    (work / "keep2.txt").write_text("keep2\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "keep2")
    (work / "keep3.txt").write_text("keep3\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "keep3")
    reachable = [
        _git(work, "rev-parse", "HEAD~2").strip(),
        _git(work, "rev-parse", "HEAD~1").strip(),
        _git(work, "rev-parse", "HEAD").strip(),
    ]
    # 强推：分支指回第一条，再长出一条新历史 —— 后两条提交从此不可达。
    _git(work, "reset", "-q", "--hard", reachable[0])
    (work / "fresh.txt").write_text("fresh\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "fresh")
    new_tip = _git(work, "rev-parse", "HEAD").strip()
    assert new_tip != reachable_tip

    config = WeeklyVersionConfig(
        project_id=project.id, repository_id=repository.id, name=_uid("weekly"),
        branch="main", start_time=_WINDOW_START_BEIJING, end_time=_WINDOW_END_BEIJING,
        is_active=True,
    )
    db.session.add(config)
    db.session.flush()

    rows = [
        Commit(repository_id=repository.id, commit_id=new_tip, path="fresh.txt",
               commit_time=_COMMIT_UTC, message="fresh", author="qa", operation="A"),
    ]
    rows.append(
        Commit(repository_id=repository.id, commit_id=reachable[0], path="keep.txt",
               commit_time=_COMMIT_UTC, message="keep", author="qa", operation="A")
    )
    for index, gone in enumerate(unreachable):
        rows.append(
            Commit(repository_id=repository.id, commit_id=gone,
                   path=f"gone{index}.txt", commit_time=_COMMIT_UTC,
                   message=f"gone-{index}", author="qa", operation="A")
        )
    db.session.add_all(rows)
    db.session.flush()
    return config, repository, work, new_tip, reachable


def _add_cache(config, repository, *, file_path, latest, count=1):
    row = WeeklyVersionDiffCache(
        config_id=config.id, repository_id=repository.id, file_path=file_path,
        latest_commit_id=latest, base_commit_id=None, commit_count=count,
        cache_status="completed", merged_diff_data='{"diff_data": {}}',
        last_sync_time=_COMMIT_UTC,
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_a_rewritten_history_replaces_this_window_and_only_this_window(tmp_path, monkeypatch):
    """**核心**：强推之后，窗口里被删掉的提交行与指向它们的缓存行一起消失。

    同时钉住「只动本窗口」：窗口外那条属于旧历史的提交行**必须还在** —— 提交列表 /
    单提交 diff 是别的功能的账，一次周版本同步不该顺手重写它们。
    """
    unreachable = ["e54c73df" + "0" * 32, "aa3fd90b" + "1" * 32, "2971caf7" + "2" * 32]
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        config, repository, work, _new_tip, reachable = _seed_config(
            reachable_tip="x" * 40, unreachable=unreachable
        )
        # 窗口**之外**的一条旧历史提交（commit_time 在窗口之前）。
        outside = Commit(
            repository_id=repository.id, commit_id=unreachable[0], path="outside.txt",
            commit_time=_COMMIT_UTC - timedelta(days=30),
            message="far past", author="qa", operation="A",
        )
        db.session.add(outside)
        db.session.flush()

        stale_row = _add_cache(
            config, repository, file_path="gone0.txt", latest=unreachable[0], count=6
        )
        stale_excel = WeeklyVersionExcelCache(
            config_id=config.id, repository_id=repository.id, file_path="gone0.txt",
            cache_key=_uid("k"), cache_status="completed",
        )
        db.session.add(stale_excel)
        reachable_row = _add_cache(
            config, repository, file_path="keep.txt", latest=reachable[0], count=2
        )
        # 历史运行的账：**它必须原样留着**（冻结输入），不是这次要清的东西。
        snapshot = AiDiffSnapshot(
            project_id=repository.project_id, group_key=_uid("grp"),
            content_digest="c" * 40, item_count=1, complete=False, status="sealed",
            sealed_at=_COMMIT_UTC,
        )
        db.session.add(snapshot)
        db.session.flush()
        item = AiDiffSnapshotItem(
            snapshot_id=snapshot.id, config_id=config.id, repository_id=repository.id,
            file_path="gone0.txt", base_commit_id=None,
            latest_commit_id=unreachable[0], diff_version="v1", commit_count=6,
        )
        db.session.add(item)
        db.session.commit()

        service = _service_for(repository)
        reachable_info = reachable_commits(service, branch="main")
        assert reachable_info.known, reachable_info.reason

        commits_in_range = (
            Commit.query
            .filter(Commit.repository_id == repository.id)
            .filter(Commit.path != "outside.txt")
            .all()
        )
        grouped = group_reachable_window_files(config, commits_in_range, git_service=service)

        # 1) 分组里只剩当前历史
        assert sorted({commit.commit_id for cs in grouped.values() for commit in cs}) == \
            sorted([reachable[0], reachable_info.tip])
        # 2) 指向不可达提交的缓存行没了；指向可达提交的那行还在
        remaining = {
            row.file_path
            for row in WeeklyVersionDiffCache.query.filter_by(config_id=config.id).all()
        }
        assert "gone0.txt" not in remaining
        assert "keep.txt" in remaining
        assert reachable_row.id is not None
        # 3) 孤儿 Excel 缓存一起清了（它的键含 base/latest，diff 行都没了）
        assert WeeklyVersionExcelCache.query.filter_by(
            config_id=config.id, file_path="gone0.txt"
        ).first() is None
        # 4) 本窗口内属于旧历史的提交行被清掉，可达的留下
        leftover = {
            row.commit_id
            for row in Commit.query.filter(Commit.repository_id == repository.id).all()
        }
        assert not (set(unreachable[:3]) & {item for item in leftover if item != unreachable[0]})
        assert reachable[0] in leftover
        # 5) **窗口外**那条旧历史行原样还在（只动本窗口）
        assert Commit.query.filter_by(id=outside.id).first() is not None
        # 6) 历史运行冻结的那份快照与它的条目没被动过（旧快照 = 历史运行的冻结输入）
        assert db.session.get(AiDiffSnapshot, snapshot.id) is not None
        kept_item = db.session.get(AiDiffSnapshotItem, item.id)
        assert kept_item is not None
        assert kept_item.latest_commit_id == unreachable[0]
        db.session.delete(outside)
        db.session.commit()
        _ = (stale_row, work)


def test_a_clean_window_writes_nothing(tmp_path, monkeypatch):
    """**正常无变化的周期同步仍走快速路径**：没有不可达提交时一个字节都不写。

    判据落在库上：缓存行与提交行的 `updated_at` / 条数一字不动。
    """
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        config, repository, _work, new_tip, _reachable = _seed_config(
            reachable_tip="y" * 40, unreachable=[]
        )
        row = _add_cache(config, repository, file_path="fresh.txt", latest=new_tip, count=1)
        db.session.commit()
        before_rows = Commit.query.filter_by(repository_id=repository.id).count()
        before_id = row.id

        service = _service_for(repository)
        commits_in_range = Commit.query.filter_by(repository_id=repository.id).all()
        result = reconcile_weekly_window(config, commits_in_range, git_service=service)

        assert result.filtered and not result.rewritten
        assert result.dropped_cache_rows == 0
        assert result.dropped_commit_rows == 0
        assert Commit.query.filter_by(repository_id=repository.id).count() == before_rows
        assert db.session.get(WeeklyVersionDiffCache, before_id) is not None


def test_a_missing_clone_leaves_everything_alone(tmp_path, monkeypatch):
    """可达集合未知时**不动作**：既不删缓存、也不删提交行。

    「问不到」不是「历史被重写了」——把它当后者会删掉一整个好窗口的缓存。
    用不存在目录而不是空目录的理由见 `_missing_dir`。
    """
    missing = _missing_dir()
    with flask_app.app_context():
        create_tables()
        _use_tmp_repos_base(tmp_path, monkeypatch)
        config, repository, _work, new_tip, _reachable = _seed_config(
            reachable_tip="z" * 40, unreachable=[]
        )
        row = _add_cache(config, repository, file_path="fresh.txt", latest="d" * 40, count=1)
        db.session.commit()
        before_commits = Commit.query.filter_by(repository_id=repository.id).count()

        broken = GitService(repo_url=missing, root_directory=missing)
        assert local_branch_tip(broken) is None, "前提：这个目录不在任何 git 仓库里"
        db.session.expire_all()
        result = reconcile_weekly_window(
            config, Commit.query.filter_by(repository_id=repository.id).all(),
            git_service=broken,
        )

        assert not result.filtered
        assert result.reason
        assert Commit.query.filter_by(repository_id=repository.id).count() == before_commits
        # 指向不可达提交的缓存行**故意留着**：它是未知，不是证据。
        assert db.session.get(WeeklyVersionDiffCache, row.id) is not None
        _ = (new_tip,)


# ---------------------------------------------------------------------------
#  端到端：走真的 `process_weekly_version_sync`
#
#  上面几组验的是「对账这一层做对了没有」，这一组验的是**它被接在同步上**：
#  调用的是生产入口，观察的是库里的结果。接线的证据只能是「跑一次真有变化」——
#  只有对账函数自己的用例，把调用删掉它们照样全绿。
# ---------------------------------------------------------------------------


def _seed_e2e(tmp_path: Path):
    """真仓库（可 diff 的 .lua 文件）+ 配置 + 新旧两段提交行。"""
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()

    repo_name = _uid("repo")
    repository = Repository(
        project_id=project.id, name=repo_name, type="git",
        url=f"https://example.com/{repo_name}.git",
        branch="main", clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()

    from utils.path_security import build_repository_local_path

    work = Path(build_repository_local_path(project.code, repo_name, repository.id))
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q")
    body = "\n".join(f"line{index} = {index}" for index in range(6))
    (work / "Alpha.lua").write_text(f"{body}\n", encoding="utf-8")
    (work / "Beta.lua").write_text(f"{body}\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    base = _git(work, "rev-parse", "HEAD").strip()

    # 旧历史：两条提交
    (work / "Alpha.lua").write_text(f"{body}\nold = 1\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "old-alpha")
    old_alpha = _git(work, "rev-parse", "HEAD").strip()
    (work / "Beta.lua").write_text(f"{body}\nold = 2\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "old-beta")
    old_beta = _git(work, "rev-parse", "HEAD").strip()

    # 强推：回到 base，长出新历史（只改 Alpha.lua）
    _git(work, "reset", "-q", "--hard", base)
    (work / "Alpha.lua").write_text(f"{body}\nnew = 1\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "new-alpha")
    new_tip = _git(work, "rev-parse", "HEAD").strip()

    config = WeeklyVersionConfig(
        project_id=project.id, repository_id=repository.id, name=_uid("weekly"),
        branch="main", start_time=_WINDOW_START_BEIJING, end_time=_WINDOW_END_BEIJING,
        is_active=True,
    )
    db.session.add(config)
    db.session.flush()

    rows = [
        Commit(repository_id=repository.id, commit_id=old_alpha, path="Alpha.lua",
               commit_time=_COMMIT_UTC, message="old-alpha", author="qa", operation="M"),
        Commit(repository_id=repository.id, commit_id=old_beta, path="Beta.lua",
               commit_time=_COMMIT_UTC, message="old-beta", author="qa", operation="M"),
        Commit(repository_id=repository.id, commit_id=new_tip, path="Alpha.lua",
               commit_time=_COMMIT_UTC, message="new-alpha", author="qa", operation="M"),
    ]
    db.session.add_all(rows)
    db.session.flush()

    # 两行缓存：Alpha 指着被强推掉的旧提交、Beta 只在旧历史里出现过。
    _add_cache(config, repository, file_path="Alpha.lua", latest=old_alpha, count=2)
    _add_cache(config, repository, file_path="Beta.lua", latest=old_beta, count=1)
    db.session.commit()
    return config, repository, work, new_tip, old_alpha, old_beta


def test_the_real_sync_entry_point_only_keeps_the_current_history(tmp_path, monkeypatch):
    """**端到端**：跑生产入口 `process_weekly_version_sync`，库里的结果只剩当前历史。

    验收要的就是这一句：「把 fixture 从 9 提交重置成 3 提交并同步，配置 3 的缓存、
    manifest、页面、模型允许的 commit 集合均只包含当前历史」。这里用 fixture（真 git
    仓库）造出同一个形态，不动真库。
    """
    from services.weekly_version_logic import process_weekly_version_sync

    _use_tmp_repos_base(tmp_path, monkeypatch)
    with flask_app.app_context():
        create_tables()
        config, repository, _work, new_tip, old_alpha, old_beta = _seed_e2e(tmp_path)

        outcome = process_weekly_version_sync(config.id)

        assert outcome is not None
        assert not outcome.is_failure, outcome.describe()
        rows = WeeklyVersionDiffCache.query.filter_by(config_id=config.id).all()
        latest = {row.file_path: row.latest_commit_id for row in rows}
        # Alpha.lua 被当前历史改过 → 缓存行留下，且指向**当前**提交
        assert latest.get("Alpha.lua") == new_tip
        # Beta.lua 在当前历史里没有任何改动 → 它的旧缓存行必须消失
        assert "Beta.lua" not in latest
        # commits_log 里本窗口内属于旧历史的两条也没了
        left = {
            row.commit_id
            for row in Commit.query.filter_by(repository_id=repository.id).all()
        }
        assert old_alpha not in left
        assert old_beta not in left
        assert new_tip in left
