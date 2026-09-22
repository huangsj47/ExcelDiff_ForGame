# -*- coding: utf-8 -*-
"""同步的采集口径：**分支 tip 变了没有**优先，日期水位线兜底。

## 为什么要单独钉

增量同步原先的起点是 `since_date = max(repository.start_date, commits_log 最新
commit_time)`，交给 `git log --since`。而提交日期**可以回填**（自动导表那类工具带上原始
日期），后果不只是「那几条回填的抓不到」：

```text
c1(10:00) → c2(12:00) → c3(09:00，回填的 tip)
git log --since=11:00   →  []          # c2 日期明明更新，也一起丢了
git log c1..c3          →  [c3, c2]
```

git 按提交日期倒序遍历，**第一个遇到的 tip 就比水位线旧，整个遍历当场停住**。一次推送里
只要 tip 是回填的，那一整批提交（含日期正常的）全都进不了 `commits_log`，而且没有任何
提示。所以这一组钉的不是「排序对不对」，是**这一轮到底拿哪个口径去问 git**。

## 反方向

日期口径**没有被删掉**：读不到 tip、首次同步、旧 tip 被 force-push 抹掉，都要退回它 ——
那三种情形下它是能拿到的最好口径。把 `since_date` 一并丢掉（例如一律传 `None` 全量重采）
会让每轮同步都重扫整个历史，代价是几千次 `git log --follow`（见
`threaded_git_service._collect_previous_commits`）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import git
import pytest

from services.git_service import GitService
from services.repository_sync_window import (
    apply_start_date,
    resolve_sync_window,
)
from services.threaded_git_service import ThreadedGitService

_T0 = datetime(2026, 9, 14, 0, 0, 0)
_TIP = "a" * 40
_PREV = "b" * 40


class _Result:
    def __init__(self, ok, stdout=""):
        self.returncode = 0 if ok else 1
        self.stdout = stdout
        self.stderr = "" if ok else "not found"


class _FakeGit:
    """桩的边界是 `_run_git_command` —— 真实 `GitService` 问 git 也是同一条路。

    不桩 `get_local_branch_tip()` 那种「结果方法」：那样等于把「tip 是怎么问出来的」
    整段让掉，而 `rev-parse` / `cat-file -e` 恰好是这条判据的实质。
    """

    def __init__(self, *, tip=_TIP, existing=(_PREV,), tip_error=None):
        self.tip = tip
        self.existing = set(existing)
        self.tip_error = tip_error
        self.commands = []
        self.exists_calls = []

    def _run_git_command(self, cmd, cwd=None, timeout=300):
        self.commands.append(list(cmd))
        if cmd[1] == "rev-parse":
            if self.tip_error:
                raise self.tip_error
            return _Result(True, f"{self.tip}\n")
        if cmd[1] == "cat-file":
            self.exists_calls.append(cmd[3])
            return _Result(cmd[3] in self.existing)
        raise AssertionError(f"意外的 git 命令: {cmd}")

    def _git_cmd_success(self, result):
        return result.returncode == 0


def _repository(*, last_synced_tip=None, start_date=_T0):
    return SimpleNamespace(last_synced_tip=last_synced_tip, start_date=start_date)


class TestWhichWindowThisRoundUses:
    def test_a_moved_tip_becomes_a_commit_range(self):
        """**核心**：tip 变了就用区间，而区间与提交日期无关。"""
        git = _FakeGit()
        window = resolve_sync_window(
            git, _repository(last_synced_tip=_PREV),
            latest_known_commit_time=_T0 + timedelta(hours=2),
        )
        assert window.rev_range == f"{_PREV}..{_TIP}", window
        assert git.exists_calls == [_PREV], "没有确认旧 tip 还在不在对象库里"

    def test_an_unchanged_tip_keeps_the_date_watermark(self):
        """tip 没变时采集结果必然为空 —— 原口径就是这样，不必改。"""
        git = _FakeGit(tip=_PREV)
        window = resolve_sync_window(
            git, _repository(last_synced_tip=_PREV),
            latest_known_commit_time=_T0 + timedelta(hours=2),
        )
        assert window.rev_range == ""
        assert window.since_date == _T0 + timedelta(hours=2)

    def test_the_first_sync_has_no_range_to_use(self):
        git = _FakeGit()
        window = resolve_sync_window(
            git, _repository(last_synced_tip=None),
            latest_known_commit_time=_T0 + timedelta(hours=2),
        )
        assert window.rev_range == ""
        assert window.tip == _TIP, "首次同步也要把 tip 记下来，下一轮才有得比"

    def test_a_missing_previous_tip_falls_back_to_dates(self):
        """force-push 重写历史之后旧 tip 就没了 —— 区间不成立，退回日期口径。"""
        git = _FakeGit(existing=())
        window = resolve_sync_window(
            git, _repository(last_synced_tip=_PREV),
            latest_known_commit_time=_T0 + timedelta(hours=2),
        )
        assert window.rev_range == ""
        assert window.since_date == _T0 + timedelta(hours=2)
        assert "force-push" in window.reason or "旧 tip" in window.reason, window.reason

    def test_an_unreadable_tip_falls_back_to_dates(self):
        git = _FakeGit(tip_error=RuntimeError("no repo"))
        window = resolve_sync_window(
            git, _repository(last_synced_tip=_PREV),
            latest_known_commit_time=_T0 + timedelta(hours=2),
        )
        assert window.rev_range == ""
        assert window.since_date == _T0 + timedelta(hours=2)

    def test_the_start_date_is_still_the_floor_when_there_is_no_latest_commit(self):
        git = _FakeGit()
        window = resolve_sync_window(git, _repository(last_synced_tip=None),
                                     latest_known_commit_time=None)
        assert window.since_date == _T0, "起始日期被丢了"

    def test_without_a_start_date_and_without_commits_there_is_no_floor(self):
        git = _FakeGit()
        window = resolve_sync_window(
            git, _repository(last_synced_tip=None, start_date=None),
            latest_known_commit_time=None,
        )
        assert window.since_date is None, "没有下界时应当是全量，不该编一个时间出来"


class TestTheStartDateStillBoundsTheRange:
    """区间口径不看日期，可 `start_date` 是用户声明的下界 —— 它仍然作数。"""

    def test_commits_before_the_start_date_are_dropped(self):
        commits = [
            {"commit_id": "x", "commit_time": datetime(2026, 7, 10, 12, 0, tzinfo=None)},
            {"commit_id": "y", "commit_time": datetime(2026, 7, 12, 12, 0, tzinfo=None)},
        ]
        # start_date 是**北京墙钟** 2026-07-11 10:00 → naive-UTC 2026-07-11 02:00
        kept = apply_start_date(commits, datetime(2026, 7, 11, 10, 0))
        assert [c["commit_id"] for c in kept] == ["y"], kept

    def test_an_aware_commit_time_is_converted_not_compared_raw(self):
        """`commit_time` 是 **aware UTC**，直接和 naive 的下界比会抛 `TypeError`。"""
        commits = [
            {"commit_id": "x",
             "commit_time": datetime(2026, 7, 10, 1, 0, tzinfo=_UTC)},
            {"commit_id": "y",
             "commit_time": datetime(2026, 7, 12, 1, 0, tzinfo=_UTC)},
        ]
        kept = apply_start_date(commits, datetime(2026, 7, 11, 10, 0))
        assert [c["commit_id"] for c in kept] == ["y"], kept

    def test_no_start_date_keeps_everything(self):
        commits = [{"commit_id": "x", "commit_time": None}]
        assert apply_start_date(commits, None) == commits

    def test_a_commit_without_a_time_is_kept(self):
        """取不到时间的**不丢** —— 「判不了」不等于「不满足」。"""
        commits = [{"commit_id": "x", "commit_time": None},
                   {"commit_id": "y", "commit_time": datetime(2026, 7, 1)}]
        kept = apply_start_date(commits, datetime(2026, 7, 11, 10, 0))
        assert [c["commit_id"] for c in kept] == ["x"], kept

    def test_a_broken_start_date_keeps_everything(self):
        """换算失败就**不筛**：静默丢提交比多采几条严重得多。"""
        commits = [{"commit_id": "x", "commit_time": datetime(2026, 7, 1)}]
        assert apply_start_date(commits, "不是日期") == commits


_UTC = timezone.utc

#: 两个采集器 —— 判据实现是同一份（`iter_commits_args`），但**接线**是两处。
_COLLECTORS = [
    (GitService, "get_commits"),
    (ThreadedGitService, "_get_commits_base_threaded"),
]


def _strip_comments(source: str) -> str:
    without_docstrings = re.sub(r'""".*?"""', " ", source, flags=re.S)
    return re.sub(r"#[^\n]*", " ", without_docstrings)


class TestTheCollectorDoesNotAlsoApplyTheDateFilter:
    """**区间口径下绝不能同时传 `since`。**

    这是整条修法里最容易被「顺手补上」的一处：`since` 看上去只是个下界，可 `git log
    --since` 的语义是「按提交日期剪枝并停止遍历」，与区间一起用等于把回填的提交**再丢一次**
    —— 而那时 `resolve_sync_window` 是对的、日志也写着「按提交区间」，现象与没修一样。

    所以这里不看源码字符串，直接调采集器，看它**实际传给 git 的是什么**。

    **两个采集器都要过**：默认同步走 `GitService`，并发那条走 `ThreadedGitService`。
    只钉其中一个的话，另一份被改回带 `since` 的样子照样全绿 —— 而它在生产里是活的
    （`task_worker_service` 用的是并发版）。
    """

    def _service(self, monkeypatch, seen, cls):
        class _FakeGitConfig:
            def config(self, *_args, **_kwargs):
                return ""

        class _FakeRepo:
            def __init__(self, *_args, **_kwargs):
                self.heads = {}
                self.head = "refs/heads/main"
                self.git = _FakeGitConfig()

            def iter_commits(self, rev, **kwargs):
                seen.append((rev, dict(kwargs)))
                return iter(())

        monkeypatch.setattr(git, "Repo", _FakeRepo)
        monkeypatch.setattr(os.path, "exists", lambda _p: True)

        repository = SimpleNamespace(
            id=1, branch="main", path_regex=None, name="r", type="git",
            project=SimpleNamespace(code="P"),
        )
        return cls("https://x/r.git", "repro", None, None, repository, set())

    @pytest.mark.parametrize("cls, method", _COLLECTORS, ids=[m for _c, m in _COLLECTORS])
    def test_a_range_is_passed_without_since(self, monkeypatch, cls, method):
        seen = []
        service = self._service(monkeypatch, seen, cls)
        getattr(service, method)(
            since_date=datetime(2026, 9, 14), limit=100, rev_range="old..new"
        )
        assert seen, "没有调用 iter_commits"
        rev, kwargs = seen[0]
        assert rev == "old..new", f"没按区间取：{rev!r}"
        assert "since" not in kwargs, (
            "区间口径下同时传了 since —— 等于又按提交日期剪一遍，回填的提交会再丢一次"
        )

    @pytest.mark.parametrize("cls, method", _COLLECTORS, ids=[m for _c, m in _COLLECTORS])
    def test_the_date_path_still_passes_since(self, monkeypatch, cls, method):
        """反方向：没有区间时口径一个字不变。"""
        seen = []
        service = self._service(monkeypatch, seen, cls)
        getattr(service, method)(since_date=datetime(2026, 9, 14), limit=100)
        rev, kwargs = seen[0]
        assert kwargs.get("since") == datetime(2026, 9, 14), kwargs
        assert rev != "old..new"


class TestTheSyncLoopIsWiredToIt:
    """**接线了不等于被用了。** 口径算得再对，同步没照它去问 git 也一样漏。"""

    def test_the_worker_uses_the_window(self):
        source = _strip_comments(
            Path("services/task_worker_service.py").read_text(encoding="utf-8")
        )
        assert "resolve_sync_window(" in source, "后台同步没有走「tip 变了没有」这个口径"
        assert "rev_range=window.rev_range" in source, (
            "拿了口径却没把区间交给 git —— 等于没接"
        )
        assert "apply_start_date(" in source, (
            "区间口径下没有应用 start_date 这个下界"
        )
        assert "last_synced_tip = window.tip" in source, (
            "没有把 tip 记下来，下一轮永远比不了"
        )

    def test_the_manual_sync_uses_it_too(self):
        """手工同步与后台同步是**两条各自独立的路**，只改一条等于半修。"""
        source = _strip_comments(
            Path("services/repository_maintenance_api_service.py").read_text(encoding="utf-8")
        )
        assert "resolve_sync_window(" in source, "手工同步那条路没跟上"
        assert "rev_range=window.rev_range" in source, "手工同步没把区间交给 git"
        assert "last_synced_tip = window.tip" in source, (
            "手工同步不记 tip —— 下一轮还会拿同一个旧区间白扫一遍"
        )
