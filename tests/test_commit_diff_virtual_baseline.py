# -*- coding: utf-8 -*-
"""合并 diff 的**假基线**不许碰 ORM session。

## 报障的那句话

复测文档两处写着「SQLAlchemy `Commit` 对象未在 session 中的 warning」，一处还说是
**启动**日志（`:380`、`:446`）。全树只有一处会构造 transient `Commit` 再挂
`.repository`：`services/commit_diff_logic.py` 的 Excel 连续提交合并分支。它在
**合并 diff 请求路径**上，不在启动路径上；启动恢复（`load_pending_tasks`）与
`Repository.commits` 毫无关系（全树没有任何一处从该关系上取 `Commit`）。

所以那句「启动」是对**那次服务会话的日志**的松散说法 —— 它和 §7.1 里那 1,965 行路径
日志同属一段（同一个 run 的日志）。而且 SQLAlchemy 的 warning 走 `warnings.warn`
→ **stderr**，全树没有 `captureWarnings`，它本来就不会进 `runlog.log`，只在控制台上
看得见。本文件把这条结论钉住：**目标就是那一处，且只有那一处**。

## 两件事必须分开说清楚

1. **会打那句 warning**：反向引用把 transient `Commit` 塞进持久化父对象的
   `Repository.commits`，下一次 flush 时 save-update 级联试图注册它，注册不上就
   `util.warn(...)` 然后**返回 False**（`sqlalchemy/orm/unitofwork.py` 的
   `register_object`，注释自己写着「这个条件在关系级联注册时是正常的」）。
2. **不会插入幽灵行**。这一点是实测的，不是推断的（`TestTheMechanism`）：写入是被
   **跳过**而不是被推迟，`session.new/dirty` 始终为空，行数在 commit 与 autoflush
   之后都不变。所以后果就是文档说的「关联写入被静默跳过」，没有更糟。
   —— 之所以要专门钉住它：如果它真会插行，那修法就不是「换载体」而是「先清数据」。

## 修法与它的前提

假基线改用 `SimpleNamespace`（本模块第 13 行本来就在用这个手法）。前提是**穷举**
下游从它身上读的属性：只有 `commit_id` 一个（`get_unified_diff_data` 与
`get_deleted_file_diff_data` 都只读它）。`TestTheAttributeSetIsPinned` 用一个
「读未声明属性就抛」的载体把这个集合钉死 —— 以后有人多读一个属性，测试立刻红，
而不是等到线上 `AttributeError`。
"""
from __future__ import annotations

import uuid
import warnings
from types import SimpleNamespace

from app import app as flask_app
from app import create_tables, db
from models import Commit, Project, Repository
from services import commit_diff_logic

# 测试库是**会话级共用**的（没有逐用例重置），所以每条用例自己造行、自己清，
# 断言一律按 repository_id 过滤，不写全局 count()。
_WARNING_MARKER = 'not in session'


def _make_repository():
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"假基线{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=f"r-{uuid.uuid4().hex[:6]}",
        url="https://example.com/repo.git",
        type="git",
        root_directory="repro",
    )
    db.session.add(repository)
    db.session.commit()
    return project, repository


def _make_commit(repository, *, commit_id, path, when=None):
    commit = Commit(
        repository_id=repository.id,
        commit_id=commit_id,
        path=path,
        status="pending",
        commit_time=when,
    )
    db.session.add(commit)
    db.session.commit()
    return commit


def _count_commits(repository):
    """这个仓库的提交行数。**按 repository_id 过滤**：测试库是会话级共用的，
    全局 `count()` 会被别的用例的行带偏（假红或假绿都发生过）。"""
    return db.session.query(Commit).filter_by(repository_id=repository.id).count()


def _cleanup(project, repository):
    try:
        db.session.query(Commit).filter_by(repository_id=repository.id).delete()
        db.session.query(Repository).filter_by(id=repository.id).delete()
        db.session.query(Project).filter_by(id=project.id).delete()
        db.session.commit()
    except Exception:  # pragma: no cover - 清理失败不该让用例结果变形
        db.session.rollback()


def _session_warnings(caught):
    return [str(item.message) for item in caught if _WARNING_MARKER in str(item.message)]


class TestTheMechanism:
    """钉住机制本身：会打 warning，**不会**插幽灵行。

    这一条测的是 SQLAlchemy 的行为而不是本仓库的代码 —— 但它必须存在：整条结论
    （「后果只是静默跳过，没有幽灵行」）就建立在这两件事上，靠读文档或推断都可能
    反过来。变异验证时把 `Commit()` 换回 ORM 写法，第二条用例会红。
    """

    def test_a_transient_commit_on_a_persistent_collection_warns(self):
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                _make_commit(repository, commit_id='b' * 40, path='t.xlsx')
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    virtual = Commit()
                    virtual.commit_id = 'a' * 40
                    virtual.repository = repository          # ← 就是这一句
                    virtual.path = 't.xlsx'
                    db.session.commit()

                messages = _session_warnings(caught)
                assert messages, '没有复现出那句 warning，机制已经变了'
                assert "add operation along 'Repository.commits' will not proceed" in messages[0], messages
            finally:
                _cleanup(project, repository)

    def test_and_it_does_not_insert_a_ghost_row(self):
        """**没有被插入**。写入是被跳过，不是被推迟。"""
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                _make_commit(repository, commit_id='b' * 40, path='t.xlsx')
                before = _count_commits(repository)

                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    virtual = Commit()
                    virtual.commit_id = 'a' * 40
                    virtual.repository = repository
                    virtual.path = 't.xlsx'
                    db.session.commit()
                    after_commit = _count_commits(repository)
                    # 再来一次 autoflush（任何一次查询都会触发）
                    db.session.rollback()
                    virtual2 = Commit()
                    virtual2.commit_id = 'c' * 40
                    virtual2.repository = repository
                    virtual2.path = 't2.xlsx'
                    db.session.query(Commit).filter_by(repository_id=repository.id).all()
                    after_autoflush = _count_commits(repository)
                    db.session.rollback()

                assert _session_warnings(caught), '这一条的前提是那句 warning 会出现'
                assert after_commit == before, f'幽灵行被插进来了：{before} → {after_commit}'
                assert after_autoflush == before, f'autoflush 把幽灵行插进来了：{before} → {after_autoflush}'
                assert list(db.session.new) == [] and list(db.session.dirty) == [], (
                    '那个假提交进了 session 的待写队列（那才是「会被写进去」的形态）'
                )
            finally:
                _cleanup(project, repository)


class TestTheMergePathIsClean:
    """真的那条调用点：合并 diff 走到假基线分支时，不许再打那句 warning。"""

    def _stub_service(self, monkeypatch, parent_commit_id):
        class _FakeGitService:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_parent_commit(self, _commit_id):
                return parent_commit_id

        import services.threaded_git_service as threaded_git_service

        monkeypatch.setattr(threaded_git_service, 'ThreadedGitService', _FakeGitService)
        monkeypatch.setattr(
            commit_diff_logic, '_excel_cache_service',
            SimpleNamespace(is_excel_file=lambda _path: True),
        )

    def test_the_virtual_baseline_does_not_warn(self, monkeypatch):
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                earliest = _make_commit(repository, commit_id='1' * 40, path='t.xlsx')
                latest = _make_commit(repository, commit_id='2' * 40, path='t.xlsx')
                before = _count_commits(repository)

                self._stub_service(monkeypatch, parent_commit_id='a' * 40)

                seen_baseline = []

                def _fake_unified_diff(_latest, previous):
                    seen_baseline.append(previous)
                    # 真实实现进来第一件事是查缓存 —— 而任何一次查询都会 autoflush。
                    # 假基线一旦挂在 repo.commits 上，warning 就在这一刻出现。
                    db.session.query(Commit).filter_by(repository_id=repository.id).all()
                    return {'type': 'excel', 'sheets': []}

                monkeypatch.setattr(commit_diff_logic, '_get_unified_diff_data', _fake_unified_diff)

                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    result = commit_diff_logic.handle_consecutive_commits_merge_internal([earliest, latest])

                assert result is not None, '没走到假基线分支，这条用例就没测到东西'
                assert seen_baseline, '没有把假基线交给 _get_unified_diff_data'
                baseline = seen_baseline[0]
                assert baseline.commit_id == 'a' * 40
                assert _session_warnings(caught) == [], _session_warnings(caught)
                assert not isinstance(baseline, Commit), (
                    '假基线又是 ORM 对象了 —— 挂在 repo.commits 上就会触发那条 warning'
                )
                assert _count_commits(repository) == before, (
                    f'多出了行：{before} → {_count_commits(repository)}'
                )
            finally:
                _cleanup(project, repository)


class TestTheAttributeSetIsPinned:
    """假基线上**只允许**读 `commit_id`。

    读未声明的属性直接抛，测试里立刻看得见；生产里那会是一句 AttributeError
    （比静默跳过好，但仍然是一次线上故障）。所以集合要在这里钉住。
    """

    class _StrictCarrier:
        """只带 `commit_id`/`path`，并且记录谁被读过。"""

        def __init__(self, **fields):
            object.__setattr__(self, '_fields', dict(fields))
            object.__setattr__(self, '_read', [])

        def __getattr__(self, name):
            fields = object.__getattribute__(self, '_fields')
            if name in fields:
                object.__getattribute__(self, '_read').append(name)
                return fields[name]
            raise AttributeError(
                f'假基线被读了一个没声明的属性 `{name}`：下游对它的依赖变了，'
                f'要么在 SimpleNamespace 里补上，要么别再传假基线（见本文件 docstring）'
            )

        @property
        def read_attributes(self):
            return sorted(set(object.__getattribute__(self, '_read')))

    def test_only_commit_id_is_read_from_the_baseline(self, monkeypatch):
        from services import vcs_content_service

        carrier = self._StrictCarrier(commit_id='a' * 40, path='t.xlsx')
        commit = SimpleNamespace(
            commit_id='b' * 40, path='code/not_excel.lua', operation='M',
            commit_time=None,
            repository=SimpleNamespace(id=1, project_id=9, type='git',
                                       project=SimpleNamespace(code='DEMO')),
        )

        monkeypatch.setattr(
            vcs_content_service, 'get_file_content_from_git',
            lambda _repo, _commit_id, _path: b'content',
        )
        monkeypatch.setattr(
            vcs_content_service, 'get_perf_metrics_service',
            lambda: SimpleNamespace(record=lambda *_a, **_k: None),
        )

        class _FakeDiffService:
            def process_diff(self, *_args, **_kwargs):
                return {'type': 'text', 'sections': []}

        import services.diff_service as diff_service_module

        monkeypatch.setattr(diff_service_module, 'DiffService', _FakeDiffService)

        with flask_app.app_context():
            create_tables()
            result = vcs_content_service.get_unified_diff_data(commit, carrier)

        assert result is not None, '没算出差异，这条用例没覆盖到读属性那一段'
        assert carrier.read_attributes == ['commit_id'], (
            f'下游从假基线上读了不止 commit_id：{carrier.read_attributes}'
        )
