# -*- coding: utf-8 -*-
"""合并 diff 的次序：**回填日期的提交不许把「最早 / 最新」判反**。

## 为什么单独一个文件

`commit_merge_sort_key` 的主键是 `commit_time`。这对**回填日期**的提交不成立：自动导表
那类工具提交时带上原始日期，于是子提交可能比父提交「旧」。按时间排完之后，
`handle_consecutive_commits_merge_internal` 拿到的是

* `earliest` = **图序上最后**的那条（日期最旧）
* `latest`   = **图序上最先**的那条（日期最新）

于是它去取 `get_parent_commit(earliest)` —— 那正是 `latest` 自己 —— 再把
`_get_unified_diff_data(latest, parent(earliest))` 交出去，**等于自己和自己比**：
整个区间的改动被算成「无变化」。同一个次序还决定 `commits[-1]`，也就是写进缓存行的
`latest_commit_id` —— 它会指向那个**旧状态**的提交。

**这一条不会自曝**：payload 与 `latest_commit_id` 两边自洽，页面与日志都没有矛盾，
只有拿两个提交去问 git 才看得出谁是谁的父亲。所以这里不测「排序键返回什么」，测
**真正被比较的是哪两个提交**（`TestWhichTwoCommitsGetCompared`）。

## 反方向也要钉

修法是把「问过 git 拓扑序」的提交整体按拓扑排。日期本来就单调时必须**逐字不变**——
过头的修法（一律按拓扑、或把没问过的也当有拓扑序）会把正常仓库的次序搅乱，而那种错误
在正常仓库上看起来完全正常，很难发现。

## 提交 id 为什么每条用例现取

`services/commit_ordering` 的两张次序缓存是**进程级**的（理由见该模块 docstring），
而测试库是**会话级共用**的：`_cleanup` 删掉 repository 之后 SQLite 会把那个 id 发给下
一个仓库，于是「换个仓库」也挡不住串号。复用同一组 commit_id 会让后一条用例读到前一条
留下的序号。既有约定与同款说明见 `tests/test_commit_ordering_same_instant.py`。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

from app import app as flask_app
from app import create_tables, db
from models import Commit, Project, Repository
from services import commit_diff_logic
from services.commit_ordering import annotate_topology_order, order_for_merge

_FILE = "config/table.xlsx"
_T0 = datetime(2026, 9, 14, 0, 0, 0)


def _sha(tag: str) -> str:
    """一个 40 位的提交 id，**每条用例现取**（理由见模块 docstring）。"""
    return (tag.replace("_", "") + uuid.uuid4().hex)[:40].ljust(40, "0")


def _ids() -> tuple:
    return _sha("old"), _sha("a"), _sha("b")


def _make_repository():
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"次序{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=f"r-{uuid.uuid4().hex[:6]}",
        url="https://example.com/repo.git",
        type="git",
        root_directory="repro",
        resource_type="table",
    )
    db.session.add(repository)
    db.session.commit()
    return project, repository


def _make_commit(repository, *, commit_id, when, path=_FILE):
    commit = Commit(
        repository_id=repository.id,
        commit_id=commit_id,
        path=path,
        operation="M",
        author="tester",
        message=f"改 {commit_id[:6]}",
        status="pending",
        commit_time=when,
    )
    db.session.add(commit)
    db.session.commit()
    return commit


def _cleanup(project, repository):
    try:
        db.session.query(Commit).filter_by(repository_id=repository.id).delete()
        db.session.query(Repository).filter_by(id=repository.id).delete()
        db.session.query(Project).filter_by(id=project.id).delete()
        db.session.commit()
    except Exception:  # pragma: no cover - 清理失败不该让用例结果变形
        db.session.rollback()


class _FakeGitService:
    """只回答「谁是父亲」与「拓扑序是什么」，并记下被问过什么。

    真实实现要 clone 一个仓库，而这里要验的只有次序 —— 拿真仓库会把「哪两个提交被比较」
    这件事埋进内容里。
    """

    def __init__(self, parents, topological):
        self.parents = dict(parents)
        self.topological = list(topological)
        self.parent_calls = []

    def get_parent_commit(self, commit_id):
        self.parent_calls.append(commit_id)
        return self.parents.get(commit_id)

    def order_commits_by_topology(self, commit_ids):
        """按给定的图序作答：只返回输入里有的，不丢不重。"""
        known = set(commit_ids)
        return [c for c in self.topological if c in known]


def _run_merge(monkeypatch, repository, commits, *, base_commit, latest_commit,
               parents, topological):
    """跑一遍 `generate_merged_diff_data`，回报**实际被比较的那一对提交**。"""
    service = _FakeGitService(parents, topological)

    import services.threaded_git_service as threaded_git_service

    monkeypatch.setattr(
        threaded_git_service, 'ThreadedGitService', lambda *_a, **_k: service
    )
    monkeypatch.setattr(
        commit_diff_logic, '_excel_cache_service',
        SimpleNamespace(is_excel_file=lambda _path: True),
    )

    compared = []

    def _fake_unified_diff(current, previous):
        compared.append(
            (getattr(current, 'commit_id', None), getattr(previous, 'commit_id', None))
        )
        return {'type': 'excel', 'sheets': []}

    monkeypatch.setattr(commit_diff_logic, '_get_unified_diff_data', _fake_unified_diff)

    result = commit_diff_logic.generate_merged_diff_data(
        repository, _FILE, base_commit, latest_commit, commits
    )
    return compared, result


class TestWhichTwoCommitsGetCompared:
    """回填日期：比较的必须是**真正的最新那条** ↔ 区间起点之前的那条。"""

    def test_a_backdated_commit_does_not_become_the_baseline(self, monkeypatch):
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                OLD, A, B = _ids()
                old = _make_commit(repository, commit_id=OLD, when=_T0)
                # 图序：OLD -> A -> B（B 是在 A 之上提交的）
                # 日期：OLD < B < A —— B 被回填成了更早的日期
                a = _make_commit(repository, commit_id=A, when=_T0 + timedelta(hours=3))
                b = _make_commit(repository, commit_id=B, when=_T0 + timedelta(hours=1))

                window = [a, b]
                annotate_topology_order(
                    _FakeGitService({A: OLD, B: A}, [OLD, A, B]), window
                )
                # 调用方（写入侧）修好之后传进来的就是真正的 tip；这里照那个传。
                compared, result = _run_merge(
                    monkeypatch, repository, window,
                    base_commit=old, latest_commit=b,
                    parents={A: OLD, B: A}, topological=[OLD, A, B],
                )

                assert result is not None
                assert compared, "没走到会算 diff 的那条分支，这条用例就没测到东西"
                current, previous = compared[0]
                assert current == B, (
                    "比较的当前版本不是真正的 tip —— 回填日期把 B 排到了 A 前面，"
                    f"于是 B 的改动整个丢了：实际比较的是 {current[:8]}"
                )
                assert previous == OLD, (
                    f"基线应当是区间起点之前的那条，实际是 {(previous or '')[:8]}"
                )
                assert (current, previous) != (A, A), (
                    "退化成了自己和自己比 —— 整段区间会被算成「无变化」"
                )
                assert result.get('latest_commit') == B, (
                    f"payload 收尾指向 {(result.get('latest_commit') or '')[:8]}，不是真正的 tip"
                )
            finally:
                _cleanup(project, repository)

    def test_monotonic_dates_keep_the_previous_behaviour(self, monkeypatch):
        """反方向：日期本来就单调时，比较的那一对必须与改动前**逐字相同**。"""
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                OLD, A, B = _ids()
                old = _make_commit(repository, commit_id=OLD, when=_T0)
                a = _make_commit(repository, commit_id=A, when=_T0 + timedelta(hours=1))
                b = _make_commit(repository, commit_id=B, when=_T0 + timedelta(hours=2))

                window = [a, b]
                annotate_topology_order(
                    _FakeGitService({A: OLD, B: A}, [OLD, A, B]), window
                )
                compared, result = _run_merge(
                    monkeypatch, repository, window,
                    base_commit=old, latest_commit=b,
                    parents={A: OLD, B: A}, topological=[OLD, A, B],
                )

                assert compared and compared[0] == (B, OLD), (
                    f"正常仓库上的比较对变了：{compared}"
                )
                assert result.get('latest_commit') == B
            finally:
                _cleanup(project, repository)


class TestTheOrderingHelperItself:
    """`order_for_merge` 是**全有或全无**的：混着来一律退回原口径。

    两套序号不可比 —— 一部分按拓扑、一部分按时间哈希进同一个 `sorted`，出来的次序是
    任意的，而且看起来完全正常。这一类「只在部分提交上生效的修正」比不修更难查。
    """

    def test_ranked_commits_are_ordered_by_topology(self):
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                OLD, A, B = _ids()
                old = _make_commit(repository, commit_id=OLD, when=_T0)
                a = _make_commit(repository, commit_id=A, when=_T0 + timedelta(hours=3))
                b = _make_commit(repository, commit_id=B, when=_T0 + timedelta(hours=1))

                annotate_topology_order(
                    _FakeGitService({}, [OLD, A, B]), [old, a, b]
                )
                ordered = [c.commit_id for c in order_for_merge([a, b, old])]
                assert ordered == [OLD, A, B], (
                    "问过 git 之后应当按拓扑序（祖先在前），实际按时间排了"
                )
            finally:
                _cleanup(project, repository)

    def test_a_partially_ranked_set_falls_back_entirely(self):
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                OLD, A, B = _ids()
                old = _make_commit(repository, commit_id=OLD, when=_T0)
                a = _make_commit(repository, commit_id=A, when=_T0 + timedelta(hours=3))
                # 只标注 old / a，b 没有任何序号 —— 这一组必须整体退回 (时间, 行 id)。
                annotate_topology_order(_FakeGitService({}, [OLD, A]), [old, a])
                b = _make_commit(repository, commit_id=B, when=_T0 + timedelta(hours=1))

                ordered = [c.commit_id for c in order_for_merge([a, b, old])]
                assert ordered == [OLD, B, A], (
                    f"部分有序的集合没有整体退回原口径：{[c[:6] for c in ordered]}"
                )
            finally:
                _cleanup(project, repository)

    def test_an_unranked_set_is_untouched(self):
        """一次 git 都没问过时，结果必须与改动前逐字相同（时间序，平局按行 id）。"""
        with flask_app.app_context():
            create_tables()
            project, repository = _make_repository()
            try:
                OLD, A, B = _ids()
                old = _make_commit(repository, commit_id=OLD, when=_T0)
                a = _make_commit(repository, commit_id=A, when=_T0 + timedelta(hours=3))
                b = _make_commit(repository, commit_id=B, when=_T0 + timedelta(hours=1))

                ordered = [c.commit_id for c in order_for_merge([a, b, old])]
                assert ordered == [OLD, B, A], [c[:6] for c in ordered]
            finally:
                _cleanup(project, repository)


def _strip_comments(source: str) -> str:
    """剥掉注释再断言 —— 本仓库的注释习惯是**把要禁掉的写法原样写进注释**

    （这一段注释自己就是例子）。不剥的话，注释里提到的那次调用会被当成真的调用。
    """
    without_block = re.sub(r'""".*?"""', " ", source, flags=re.S)
    return re.sub(r"#[^\n]*", " ", without_block)


def _function_body(source: str, name: str) -> str:
    """切出某个顶格函数的函数体（到下一个顶格 `def` 为止）。"""
    start = source.index(f"\ndef {name}(")
    rest = source[start + 1:]
    nxt = rest.find("\ndef ", 1)
    return rest if nxt == -1 else rest[:nxt]


class TestTheWritePathIsWiredToIt:
    """**接线了不等于被用了。** 次序修得再对，写入侧没调它也一样漏。

    这一条必须存在：`order_for_merge` 的行为由上面两条用例保证，而「周版本同步真的
    用了它」只能在这里钉 —— 缓存行的 `latest_commit_id` 与 `commits[-1]` 都出自那个
    调用点，漏掉它，payload 与 latest 会一起指向旧状态，而两边自洽、看不出来。
    """

    def test_the_weekly_sync_annotates_topology_before_grouping(self):
        from pathlib import Path

        source = Path("services/weekly_version_logic.py").read_text(encoding="utf-8")
        body = _strip_comments(_function_body(source, "process_weekly_version_sync"))

        assert "annotate_topology_order(repository, commits_in_range)" in body, (
            "周版本同步没有给整组提交问 git 拓扑序 —— 回填日期的提交又会把 latest 判反"
        )
        assert "order_for_merge(commits_in_range)" in body, (
            "问了拓扑序却没有按它排序"
        )
        assert body.index("annotate_topology_order(") < body.index("order_for_merge("), (
            "先排序后定序 —— 拓扑序要用在排序上，顺序反了等于没接"
        )
