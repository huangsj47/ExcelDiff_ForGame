# -*- coding: utf-8 -*-
"""AI 取数必须**带着仓库维度**查 `commits_log`（REV-AI-001）。

## 缺陷

`commits_log` 是**跨仓库共用**的一张表，而 SVN 修订号**只在单个仓库内唯一** ——
两个仓库完全可能同时有 revision 42。取数侧的两个查询原先只按 `commit_id`（+`path`）：

    Commit.query.filter_by(commit_id=commit, path=path).order_by(Commit.id.desc())

于是「后插入的那一行赢」。后果不是报错，是**静默串号**：A 项目的分析里冒出 C 项目的
`private/other-project.lua`，同路径的文件 diff 读到 C 项目那一份缓存。

`commits_log` 上那个 `idx_commits_log_repo_commit_id` 名字很像唯一索引，**它不是** ——
这一点让这个洞活了很久（读的人以为有保护）。

## 修法

payload 的每一条**本来就带 `repository_id`**（`scope_sampling._summarize_weekly_files`
写的），只是 `change_set.from_weekly_payload` 在归并时把它扔了。现在它进了
`AnalysisScope.repository_ids_by_commit`，取数侧据此把查询收窄到「本批次真的带这个号
的那些仓库」。

**不收窄的情形是有意的**：scope 缺这个字段（手工构造、单提交模式）时退回旧行为 ——
猜一个仓库去收窄，会把「本批次真的读不到」变成「读到了别的仓库那一行」。
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import app, create_tables, db
from models import Commit, Project, Repository
from services.ai.change_set import from_weekly_payload
from services.ai.platform_provider import PlatformContextProvider

# 两个仓库共用的那个「修订号」与路径。加随机后缀是因为**测试库是会话级共用的**，
# 固定值会和别的用例留下的行撞上（而撞上的表现正是这个用例要测的东西）。
SHARED_REVISION = f"4{uuid.uuid4().hex[:7]}"
SHARED_PATH = "config/道具表.xlsx"
OTHER_PROJECT_FILE = "private/other-project.lua"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _repository(*, project_id: int, name: str) -> Repository:
    repository = Repository(
        project_id=project_id,
        name=name,
        type="svn",
        url=f"https://svn.example.invalid/{_uid('r')}",
        branch="trunk",
        clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()
    return repository


def _seed():
    """本项目的仓（A）+ 另一个项目里同号的仓（C）。**C 后建，id 更大。**"""
    now = datetime.now(timezone.utc)
    mine = Project(code=_uid("PA"), name="跨仓取数-本项目")
    theirs = Project(code=_uid("PC"), name="跨仓取数-别的项目")
    db.session.add_all([mine, theirs])
    db.session.flush()

    repo_a = _repository(project_id=mine.id, name="A仓")
    repo_c = _repository(project_id=theirs.id, name="C仓")

    db.session.add_all([
        # **先加 A 的行、后加 C 的行**，让 C 的自增 id 更大 —— 这正是缺陷现形的形状：
        # 两个查询都带 `order_by(id.desc())`，不收窄仓库时「后插入的赢」，于是读到 C。
        # （反过来造数会让**未修的代码也选对**，用例变成假绿 —— 第一版就是这么写的。）
        Commit(repository_id=repo_a.id, commit_id=SHARED_REVISION, path=SHARED_PATH,
               operation="M", author="a", commit_time=now, message="A 的同号提交"),
        # C 仓：同一个修订号、**同一条路径**，外加一个别的项目独有的文件。
        Commit(repository_id=repo_c.id, commit_id=SHARED_REVISION, path=SHARED_PATH,
               operation="M", author="c", commit_time=now, message="C 的同号提交"),
        Commit(repository_id=repo_c.id, commit_id=SHARED_REVISION, path=OTHER_PROJECT_FILE,
               operation="A", author="c", commit_time=now, message="C 的同号提交"),
    ])
    db.session.commit()
    return repo_a.id, repo_c.id


def _payload(*, repository_id):
    """写侧真实产出的那种 `delta_files` 条目（`scope_sampling` 就带这个键）。"""
    return {
        "scope": "full",
        "delta_files": [
            {
                "latest_commit_id": SHARED_REVISION,
                "file_path": SHARED_PATH,
                "repository_id": repository_id,
            }
        ],
    }


def _provider(scope):
    return PlatformContextProvider(
        loaded=SimpleNamespace(readable={}, skills={}, dimensions=()),
        scope=scope,
    )


def test_the_payload_repository_id_reaches_the_scope():
    """前提：写侧本来就带 `repository_id`，别在归并时扔掉它。"""
    with app.app_context():
        create_tables()
        repo_a_id, _ = _seed()

        scope = from_weekly_payload(_payload(repository_id=repo_a_id)).scope

        assert scope.repository_ids_by_commit.get(SHARED_REVISION) == frozenset({repo_a_id})


def test_commit_detail_does_not_leak_the_other_projects_files():
    """模型看到的那份清单里不许出现别的项目的文件。"""
    with app.app_context():
        create_tables()
        repo_a_id, _ = _seed()

        scope = from_weekly_payload(_payload(repository_id=repo_a_id)).scope
        text = _provider(scope).commit_detail(SHARED_REVISION)

        assert text is not None, "本批次真的有这个号，不该读不到"
        assert SHARED_PATH in text
        assert OTHER_PROJECT_FILE not in text, (
            "同一个修订号在别的项目里也有，取数按裸号查到了那一行 —— "
            "清单里于是出现了本项目根本没改过的文件"
        )


def test_the_same_path_does_not_read_the_other_repositorys_row():
    """同号的**同路径**：必须落在本批次那个仓库上，而不是 id 更大的那一行。"""
    with app.app_context():
        create_tables()
        repo_a_id, repo_c_id = _seed()

        scope = from_weekly_payload(_payload(repository_id=repo_a_id)).scope
        row = _provider(scope)._commit_row(SHARED_REVISION, SHARED_PATH)

        assert row is not None
        assert row.repository_id == repo_a_id, (
            f"读到了仓库 {row.repository_id} 那一行（本批次是 {repo_a_id}）"
        )
        assert row.repository_id != repo_c_id


def test_a_batch_spanning_both_repositories_keeps_both():
    """反方向：两个仓库**都在本批次**时，两边的行都该留下 —— 收窄不许收过头。"""
    with app.app_context():
        create_tables()
        repo_a_id, repo_c_id = _seed()

        payload = _payload(repository_id=repo_a_id)
        payload["delta_files"].append(
            {
                "latest_commit_id": SHARED_REVISION,
                "file_path": OTHER_PROJECT_FILE,
                "repository_id": repo_c_id,
            }
        )
        scope = from_weekly_payload(payload).scope

        text = _provider(scope).commit_detail(SHARED_REVISION)

        assert SHARED_PATH in text and OTHER_PROJECT_FILE in text


def test_an_unknown_repository_falls_back_to_the_old_query():
    """scope 里没有仓库信息时**不收窄**（旧行为逐字保留）。

    这条守的是「不许拿一个猜出来的仓库去收窄」：收窄错了会把「本批次真的读不到」
    变成「读到了别的仓库那一行」，比不修还糟。
    """
    with app.app_context():
        create_tables()
        _seed()

        payload = _payload(repository_id=None)
        scope = from_weekly_payload(payload).scope

        assert scope.repository_ids_by_commit == {}, "前提不成立：没给仓库号却记下来了"
        assert _provider(scope)._repositories_for(SHARED_REVISION) == ()
        assert _provider(scope).commit_detail(SHARED_REVISION) is not None
