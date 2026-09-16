# -*- coding: utf-8 -*-
"""区间比较（显式基线）读缓存必须与写缓存用**同一个** previous_commit_id。

## 为什么需要这个文件

`services/vcs_content_service.get_unified_diff_data(commit, previous_commit)` 是
区间/合并 diff 的入口：`previous_commit` 可能是区间起点（`get_commit_pair_diff_internal`
的 base_commit、`file_commits[1]`、周版本窗口起点），**通常不是**「该文件在该提交
上的紧邻前一条」。

它写缓存时用的是 `previous_commit.commit_id`，读缓存时却**没传** previous_commit_id
→ `get_cached_diff()` 自己去解析「权威基线」（= 紧邻前一条 c2）→ 请求「c3 对 c1」
的区间比较直接命中先前「c3 对 c2」留下的那一行。

这是本仓库最危险的一类缺陷：内容错、但不报错、也不重算，日志里还会打印一句
「🧷 基线校验通过: previous=c2」——看起来一切正常，只有对着表格数行数才发现
「被删掉的那一行」是相邻提交的值而不是区间起点的值。

## 变红意味着什么

读缓存又退回「不传基线 → 服务自解析」，区间比较会重新开始展示**别人的基线**
算出来的 diff。
"""
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import Workbook

from app import app, create_tables, db
from models import Commit, DiffCache, Project, Repository
from services import vcs_content_service

PROJECT_PATH = "client_data/range.xlsx"

# 三个版本的同一张表：c1 -> c2 -> c3 依次把 B2 的值改成 10 / 20 / 30。
# 用「值」而不是「行数」做断言，因为「c3 对 c2」与「c3 对 c1」的**行数完全一样**
# （都是 1 增 1 删），只有被删掉的那一行的值不同 —— 这正是这个 bug 在界面上
# 看不出来的原因。
VALUE_BY_INDEX = (10, 20, 30)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _xlsx(value) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "id"
    sheet["B1"] = "value"
    sheet["A2"] = 1
    sheet["B2"] = value
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _make_repository() -> Repository:
    project = Project(code=_uid("P"), name=_uid("project"), department="QA")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('repo')}.git",
        server_url="https://example.com",
        branch="main",
        resource_type="table",
        clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()
    return repository


class _GitContents:
    """内存里的「仓库文件内容」：登记 {commit_id: bytes}，并记录每次读盘。"""

    def __init__(self):
        self.contents = {}
        self.calls = []

    def __setitem__(self, commit_id, payload):
        self.contents[commit_id] = payload

    def get(self, commit_id):
        self.calls.append(commit_id)
        return self.contents.get(commit_id)

    def reset_calls(self):
        self.calls = []


def _seed_file_commits(repository: Repository, git_contents: _GitContents):
    """给同一个文件造 c1/c2/c3 三条提交，commit_time 严格递增，并登记各自的文件字节。

    严格递增是为了让「权威基线」与「区间起点」是两个**确定**的提交：
    权威基线 = c2（紧邻前一条），区间起点 = c1。
    """
    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    commits = []
    for index, value in enumerate(VALUE_BY_INDEX):
        commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("commit"),
            path=PROJECT_PATH,
            author="dev",
            commit_time=base + timedelta(hours=index),
            message=f"commit-{index}",
        )
        db.session.add(commit)
        commits.append(commit)
        git_contents[commit.commit_id] = _xlsx(value)
    db.session.flush()
    return commits


@pytest.fixture
def git_contents(monkeypatch):
    """把取文件内容换成内存里的字节。

    只替换内容来源，**不**替换缓存逻辑与 diff 计算：本文件要验证的正是
    「谁的内容进了 diff + 结果被记在哪个基线下面」，绕过其中任何一环都会让
    用例失去意义（真起一个 git 仓库只是把同一件事做慢）。
    同时记录读盘次数 —— 「第二次请求是否命中缓存」靠它判断，比看日志可靠。
    """
    store = _GitContents()

    def _fake_get_file_content_from_git(repository, commit_id, file_path):
        return store.get(commit_id)

    monkeypatch.setattr(
        vcs_content_service, "get_file_content_from_git", _fake_get_file_content_from_git
    )
    return store


def _removed_values(diff_data) -> list:
    """取 diff 里所有「被删掉的行」的值 —— 用来分辨这份 diff 是跟谁比出来的。"""
    values = []
    for sheet in (diff_data.get("sheets") or {}).values():
        for row in sheet.get("rows") or []:
            if row.get("status") == "removed":
                values.append((row.get("data") or {}).get("value"))
    return values


def _statuses(diff_data) -> set:
    statuses = set()
    for sheet in (diff_data.get("sheets") or {}).values():
        for row in sheet.get("rows") or []:
            statuses.add(row.get("status"))
    return statuses


def _cache_rows(repository, commit):
    return DiffCache.query.filter_by(
        repository_id=repository.id, commit_id=commit.commit_id, file_path=PROJECT_PATH
    ).all()


def test_range_diff_does_not_reuse_neighbour_baseline_cache(git_contents):
    """先缓存「c3 对 c2」，再请求「c3 对 c1」→ 必须重算，不能复用 c2 那份。

    为什么需要：这是区间比较（合并 diff / 周版本窗口）的必经路径。
    `get_unified_diff_data` 写缓存时用 previous_commit.commit_id，读缓存时却不传，
    于是「读」退回服务自解析的权威基线（c2），「写」用真实基线 —— 同一个函数
    自问自答两套键，请求区间比较的人拿到的是相邻提交的结果。

    变红意味着：读缓存又没有带上本次比较的基线，区间比较开始展示别人的基线结果。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        c1, c2, c3 = _seed_file_commits(repository, git_contents)

        # 1) 路径 A：平台/后台口径 —— c3 的基线是紧邻的 c2
        neighbour = vcs_content_service.get_unified_diff_data(c3, c2)
        assert _removed_values(neighbour) == ["20"], (
            f"「c3 对 c2」应当删掉值为 20 的那一行（c2 的值），实际 {_removed_values(neighbour)}"
        )
        rows = _cache_rows(repository, c3)
        assert [row.previous_commit_id for row in rows] == [c2.commit_id]

        # 2) 路径 B：区间比较 —— 同一条 c3，基线是区间起点 c1
        ranged = vcs_content_service.get_unified_diff_data(c3, c1)
        assert _removed_values(ranged) == ["10"], (
            "「c3 对 c1」的区间比较命中了「c3 对 c2」的缓存：被删掉的是 c2 的值(20) "
            "而不是区间起点 c1 的值(10)。读缓存必须显式传入与写缓存一致的 previous_commit_id。"
        )

        # 3) 两个基线各占一行，互不覆盖
        rows = _cache_rows(repository, c3)
        assert {row.previous_commit_id for row in rows} == {c1.commit_id, c2.commit_id}, (
            f"两个基线必须各占一行，实际 {[row.previous_commit_id for row in rows]}"
        )

        # 4) 再请求一次「c3 对 c1」→ 应当命中刚写下的 c1 那一行（缓存确实生效了）
        git_contents.reset_calls()
        again = vcs_content_service.get_unified_diff_data(c3, c1)
        assert _removed_values(again) == ["10"]
        assert git_contents.calls == [], (
            "第二次请求同一个区间基线应当命中缓存；重新读文件说明缓存没建在 c1 基线下"
        )

        db.session.remove()


def test_explicit_none_baseline_does_not_hit_authoritative_baseline_row(git_contents):
    """明确传 previous_commit=None（新增文件）→ 不能命中「权威基线 c2」那一行。

    为什么需要：`get_unified_diff_data(commit, None)` 表示「这条提交没有基线，
    整份文件都是新增」。它写缓存时落的是 previous_commit_id=NULL，读缓存若不传
    → 服务自解析出 c2 → 命中 c2 那一行 → 返回「只有一行被改」而不是「整份文件
    都是新增」。这是同一个函数读/写两套键的又一种表现。

    变红意味着：无基线的请求被别人的基线缓存接走，新增文件被显示成只改了几行。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        _c1, c2, c3 = _seed_file_commits(repository, git_contents)

        # 先让权威基线（c2）那一行存在，制造「有更贴切的缓存可以偷」的局面
        assert vcs_content_service.get_unified_diff_data(c3, c2) is not None

        full = vcs_content_service.get_unified_diff_data(c3, None)
        assert _statuses(full) == {"added"}, (
            f"previous=None 表示与空版本比较，结果应当全是 added；实际 {_statuses(full)} "
            f"—— 说明它命中了 c2 基线的缓存"
        )

        rows = _cache_rows(repository, c3)
        assert None in {row.previous_commit_id for row in rows}, (
            "previous=None 的结果必须以 previous_commit_id=NULL 落库，否则下次还会串用"
        )

        db.session.remove()


def test_omitted_previous_commit_keeps_service_side_baseline_resolution(git_contents):
    """不传 previous_commit（老调用方）→ 读缓存仍由服务自解析权威基线。

    为什么需要：老调用方只写 `get_unified_diff_data(commit)`，把「和谁比」完全
    交给服务。修复读/写键不一致时很容易顺手把默认值改成 None，那会让这些调用方
    从「命中权威基线缓存」变成「每次都与空版本比重算」，是另一种回归。

    变红意味着：默认参数丢了哨兵语义，「不传」被当成了「明确无基线」。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        _c1, c2, c3 = _seed_file_commits(repository, git_contents)

        neighbour = vcs_content_service.get_unified_diff_data(c3, c2)
        assert _removed_values(neighbour) == ["20"]
        rows = _cache_rows(repository, c3)
        assert [row.previous_commit_id for row in rows] == [c2.commit_id]

        omitted = vcs_content_service.get_unified_diff_data(c3)
        assert _removed_values(omitted) == ["20"], (
            "不传 previous_commit 时应当命中服务自解析出的权威基线（c2）缓存"
        )
        assert len(_cache_rows(repository, c3)) == 1, "命中缓存就不该再写一行"

        db.session.remove()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
