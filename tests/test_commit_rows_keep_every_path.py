# -*- coding: utf-8 -*-
"""一次提交改多个文件时，**每个文件**都必须落进 commits_log。

## 缺陷

`commits_log` 的行标识是三元组 `(repository_id, commit_id, path)` ——
`services/task_worker_service.py::_make_excel_task_key` 就是这么拼 key 的。但两条
同步路径的判重只用了 `(repository_id, commit_id)`：

* `services/svn_service.py::SVNService.sync_repository_commits`
      `Commit.query.filter_by(repository_id=..., commit_id=...).first()`
* `services/task_worker_service.py::_handle_auto_sync_task_inner`（git 后台同步）
      `existing_commit_ids` 是 commit_id 的集合

而两条路径的提交列表都是**一个文件一条**：

* `svn_service._parse_svn_log`：`for path in paths.findall('path')` → 每个 path
  一条，commit_id 都是同一个 `r{revision}`
* `git_service.get_commits`：`for file_path in ...` / diff 循环 → 每个文件一条，
  commit_id 都是同一个 hexsha

于是同一次提交的第 2 个文件起全被判为「已存在」而跳过。

**实测**（修复前，一个 SVN revision 改了 3 个文件）：

    parsed entries: [('r42','/trunk/Config/A.xlsx'),
                     ('r42','/trunk/Config/B.xlsx'),
                     ('r42','/trunk/Config/C.xlsx')]
    rows added: 1
    rows in DB: [('r42','/trunk/Config/A.xlsx')]

对「配表 diff 确认平台」来说这是致命的数据丢失：评审者在提交列表里只看得到第一个
文件，确认了这条提交就等于确认了**没看过**的改动。

`services/agent_management_handlers.py` 里的 Agent 回传路径本来就是对的 ——
它用的就是 `pair = (commit_id, file_path)`。本文件把另外两条统一到这个口径。

## 注意：修复后需要重同步才能补齐历史（且 force_reclone 不够）

增量同步的起点是 `since_date = max(repository.start_date, commits_log 中最新
commit_time)`（`services/task_worker_service.py::_handle_auto_sync_task_inner`）。
修复前被跳过、**从未落进 commits_log** 的历史文件不在这个时间窗口内，所以**普通
增量同步不会把它们补回来**。

而 `force_reclone` **也不足以**补齐：它只清理本地工作副本再重新克隆，并不重置
`since_date`（`since_date` 来自数据库里的 `commits_log` 最新 `commit_time`，而
那些行并没有变）。要补齐必须让采集回到历史起点：

* Agent 模式：`agent/handlers/auto_sync.py::_collect_commits` 用 `git log -n<limit>`
  采集、**不带日期过滤**，重派一次 auto_sync 即可；`limit` 缺省 300、夹在 50~2000，
  历史更早时要把 `limit` 调大。
* 单机模式：清掉该仓库在 `commits_log` 的记录（或删仓库重建）后再同步，
  并确认 `repository.start_date` 为空或早于要补齐的起点。

（我最初在本文件的说明里写了「或触发一次 force_reclone」，那是**错的** ——
 复核 `_handle_auto_sync_task_inner` 的 `since_date` 推导后才改正。
 详细运维步骤见 `平台配置说明.md` §12.3。）
"""
from __future__ import annotations

import os
import sys
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app, create_tables, db  # noqa: E402
from models import Commit, Project, Repository  # noqa: E402
from services.repository_creation_handlers import allocate_repository_id  # noqa: E402
from services.svn_service import SVNService  # noqa: E402

SVN_LOG_XML = """<?xml version="1.0"?>
<log>
 <logentry revision="42">
  <author>alice</author>
  <date>2026-03-02T10:00:00.000000Z</date>
  <msg>改了三张表</msg>
  <paths>
   <path action="M">/trunk/Config/A.xlsx</path>
   <path action="M">/trunk/Config/B.xlsx</path>
   <path action="A">/trunk/Config/C.xlsx</path>
  </paths>
 </logentry>
 <logentry revision="43">
  <author>bob</author>
  <date>2026-03-03T10:00:00.000000Z</date>
  <msg>只改一个</msg>
  <paths>
   <path action="M">/trunk/Config/A.xlsx</path>
  </paths>
 </logentry>
</log>"""


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_repository(name_prefix):
    """建一个仓库。

    **id 必须走 allocate_repository_id()** —— 整个系统里 `Repository.id` 的唯一
    来源就是那张 `global_repository_counter`。若测试里让主键自增随便发一个号，
    这个号会落在计数器还没用过的区间里，后面别的用例通过创建表单建仓库时
    `allocate_repository_id()` 会发出同一个号 → UNIQUE constraint failed。
    （实测：本文件最初用 `id=None` 让自增发号，导致
     tests/test_full_chain_mode_e2e.py 在全量跑时撞主键 —— 单跑时却是绿的。）
    """
    project = Project(code=_uid("P")[:12], name=_uid("proj"))
    db.session.add(project)
    db.session.commit()

    repository = Repository(
        id=allocate_repository_id(),
        project_id=project.id,
        name=f"{name_prefix}_{uuid.uuid4().hex[:8]}",
        type="svn",
        url="https://svn.example.com/repo",
        root_directory="/tmp/svn_repo",
        username="u",
        current_version="1",
        resource_type="excel",
    )
    db.session.add(repository)
    db.session.commit()
    return project, repository


def _build_service(repository, commits=None):
    """构造一个绕过 __init__ 的 SVNService（只喂需要的那几个属性）。

    走 `__new__` 是为了不碰真实的 SVN 命令行；这几个属性正是
    `_parse_svn_log` / `sync_repository_commits` 会读的全部输入。
    """
    service = SVNService.__new__(SVNService)
    service.repository = repository
    service.repository_commit_filter = None
    service.repository_log_filter_regex = None
    service.repository_path_regex = None
    service.repository_name = repository.name
    service.repository_id = repository.id
    if commits is not None:
        service.get_commits = lambda limit=1000: commits
    return service


class TestSvnSyncKeepsEveryPathOfARevision:
    def test_all_paths_of_one_revision_are_inserted(self):
        with app.app_context():
            create_tables()
            _project, repository = _make_repository("svnmulti")

            entries = _build_service(repository)._parse_svn_log(SVN_LOG_XML)
            assert len(entries) == 4, f"解析结果应为 4 条（3 + 1），实际 {entries}"

            service = _build_service(repository, entries)
            added = service.sync_repository_commits(db=db, Commit=Commit)
            db.session.commit()

            rows = (
                Commit.query.filter_by(repository_id=repository.id)
                .order_by(Commit.commit_id.asc(), Commit.path.asc())
                .all()
            )
            stored = [(row.commit_id, row.path) for row in rows]

            assert added == 4, (
                f"应当落库 4 行（r42 三个文件 + r43 一个文件），实际 {added}。\n"
                f"落库内容：{stored}\n"
                f"若只有 2 行，说明判重键又退回成了 commit_id 单键 —— "
                f"同一次提交的其余文件被当成「已存在」跳过了。"
            )
            assert [p for _, p in stored if _ == "r42"] == [
                "/trunk/Config/A.xlsx",
                "/trunk/Config/B.xlsx",
                "/trunk/Config/C.xlsx",
            ], f"r42 的三个文件都要在：{stored}"

            assert service.sync_repository_commits(db=db, Commit=Commit) == 0, (
                "第二次同步应当全部判为已存在，不再重复插入"
            )

    def test_same_path_in_different_commits_is_kept(self):
        """反向保险：同一文件在不同 revision 各占一行。"""
        with app.app_context():
            create_tables()
            _project, repository = _make_repository("svnsame")
            entries = _build_service(repository)._parse_svn_log(SVN_LOG_XML)

            service = _build_service(repository, entries)
            service.sync_repository_commits(db=db, Commit=Commit)
            db.session.commit()

            a_rows = (
                Commit.query.filter_by(repository_id=repository.id, path="/trunk/Config/A.xlsx").all()
            )
            assert sorted(row.commit_id for row in a_rows) == ["r42", "r43"], (
                f"A.xlsx 在 r42、r43 各应有一行，实际 {[r.commit_id for r in a_rows]}"
            )


class TestSyncDedupeKeyIncludesPath:
    """源码级约定：两条同步路径的判重键都必须含 path。"""

    def test_svn_service_uses_pair_key(self):
        source = open(
            os.path.join(PROJECT_ROOT, "services", "svn_service.py"), encoding="utf-8"
        ).read()
        assert "pair = (commit_data['commit_id'], commit_data.get('path', '') or '')" in source, (
            "SVN 同步的判重键必须含 path"
        )
        assert "existing_commit = Commit.query.filter_by(" not in source, (
            "svn_service 里又出现了只按 (repository_id, commit_id) 判重的查询 —— "
            "那会让同一次提交只落库第一个文件。"
        )

    def test_git_background_sync_uses_pair_key(self):
        source = open(
            os.path.join(PROJECT_ROOT, "services", "task_worker_service.py"), encoding="utf-8"
        ).read()
        assert "pair = (commit_data['commit_id'], commit_data.get('path', '') or '')" in source, (
            "git 后台同步的判重键必须含 path"
        )
        assert "if commit_data['commit_id'] in existing_commit_ids:" not in source, (
            "task_worker_service 里又出现了只按 commit_id 判重的写法。"
        )

    def test_agent_path_still_uses_pair_key(self):
        """Agent 回传路径本来就是对的口径，别被改坏。"""
        source = open(
            os.path.join(PROJECT_ROOT, "services", "agent_management_handlers.py"),
            encoding="utf-8",
        ).read()
        assert "pair = (commit_id, file_path)" in source, (
            "agent_management_handlers 里的 (commit_id, file_path) 判重不见了。"
        )
