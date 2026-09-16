# -*- coding: utf-8 -*-
"""「正文比的版本」必须和「页头写的版本」是同一个。

## 缺陷形态（线上抽样 100 条里 13 条）

页头「对比版本」由 `resolve_previous_commit(commit)` 解析（同秒有 id 兜底、库里缺失
还能回退 VCS），而正文来自 DiffCache。两边**口径不同**时会出现：

* 页头写着「对比版本 = 上一版」，正文却是与**更晚的版本**比出来的结果
  —— 真变更整体漏掉，还凭空报出两份文件里都不存在的变更（实测 6773、6785）；
* 或者正文拿的是「与空版本比」的载荷 —— 一份改了 3 行的配表在合并视图里
  显示成整表新增，页头却写着「对比版本 X」（实测 6719、6729）。

三个具体成因，本文件各钉一个：

1. **读侧**（`services/commit_diff_view_service.py`）：页面读缓存不传基线，
   交给缓存服务自己解析（只查 DB）。解析不出来时基线校验被整个跳过 ——
   同一 (提交, 文件) 下任何基线算出来的行都可能被当成这一页的正文。
2. **写侧**（`services/commit_diff_logic.py::handle_different_files_merge`）：
   单提交组传 `previous_commit=None`（那句 `file_commits[1] if len(...) > 1 else None`
   在 `len == 1` 分支里条件恒假）→ 载荷是「整份文件新增」，并以
   previous_commit_id=NULL 落库。
3. **写侧**（`handle_consecutive_commits_merge_internal`）：范围 diff 失败时的回退
   写的是 `file_commits[1]`，而 file_commits 是**升序**排的 —— 两条提交时它就是
   latest_commit 自己（自己和自己比 → 无变更），更多条时基线落在区间中间。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import services.commit_diff_logic as logic  # noqa: E402


class _Field:
    """列替身：既要做 `== 值`、又要支持 .desc()（视图服务的 order_by 用）。"""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def desc(self):
        return (self.name, "desc")


class _CommitModel:
    """最小 Commit 模型替身（视图服务用它构造查询）。"""

    repository_id = _Field("repository_id")
    path = _Field("path")
    commit_time = _Field("commit_time")
    id = _Field("id")

    def __init__(self, commit):
        self.query = SimpleNamespace(
            get_or_404=lambda _i: commit,
            filter=lambda *_a, **_k: SimpleNamespace(
                order_by=lambda *_a, **_k: SimpleNamespace(all=lambda: [commit])),
        )


def _commit(sha_char: str, path: str = "config/a.xlsx", minute: int = 0):
    return SimpleNamespace(
        id=ord(sha_char),
        repository_id=1,
        repository=SimpleNamespace(id=1, type="git", url="u", root_directory=".",
                                   username="", token=""),
        path=path,
        commit_id=sha_char * 40,
        version=sha_char * 8,
        commit_time=datetime(2026, 9, 1, 10, minute, tzinfo=timezone.utc),
        operation="M",
    )


class TestPageReadPassesTheResolvedBaseline:
    """读侧：页面必须把它已经解析出来的基线交给缓存查询。"""

    def _render_context(self, previous_commit):
        import services.commit_diff_view_service as view

        seen = {}

        def _get_cached_diff(repository_id, commit_id, file_path, **kwargs):
            seen["kwargs"] = kwargs
            return None

        commit = SimpleNamespace(id=5, commit_id="f" * 40, operation="M", repository_id=1,
                                 path="config/a.xlsx", commit_time="2026-09-01")
        view.handle_commit_diff_view(
            commit_id=5,
            time_module=SimpleNamespace(time=lambda: 1.0),
            Commit=_CommitModel(commit),
            db=SimpleNamespace(session=SimpleNamespace(delete=lambda *_a, **_k: None,
                                                      commit=lambda: None, rollback=lambda: None)),
            excel_cache_service=SimpleNamespace(is_excel_file=lambda _p: True,
                                                get_cached_diff=_get_cached_diff,
                                                save_cached_diff=lambda **_k: True),
            add_excel_diff_task=lambda *_a, **_k: None,
            threaded_git_service_cls=lambda *_a, **_k: SimpleNamespace(),
            active_git_processes={},
            get_commit_diff_mode_strategy=lambda: SimpleNamespace(async_agent_diff=False),
            resolve_previous_commit=lambda *_a, **_k: previous_commit,
            attach_author_display=lambda *_a, **_k: None,
            get_unified_diff_data=lambda *_a, **_k: None,
            get_deleted_file_diff_data=lambda *_a, **_k: None,
            get_diff_data=lambda *_a, **_k: None,
            validate_excel_diff_data=lambda *_a, **_k: (False, "empty"),
            clean_json_data=lambda data: data,
            build_commit_diff_template_context=lambda **kwargs: kwargs,
            performance_metrics_service=SimpleNamespace(record=lambda *_a, **_k: None),
            ensure_commit_access_or_403=lambda _c: (SimpleNamespace(id=1, name="r", type="git"), None),
            render_template=lambda _t, **ctx: ctx,
            log_print=lambda *_a, **_k: None,
        )
        return seen

    def test_previous_commit_id_is_forwarded(self):
        previous = SimpleNamespace(id=4, commit_id="e" * 40, version="e" * 8,
                                   commit_time="2026-08-31")
        seen = self._render_context(previous)
        assert seen["kwargs"].get("previous_commit_id") == "e" * 40, (
            "页面读缓存没有带上页头解析出来的基线 —— 缓存服务自己解析失败时会跳过基线"
            "校验，返回别的基线算出来的正文（页头与正文各说各话）"
        )

    def test_no_previous_commit_requires_the_null_baseline(self):
        seen = self._render_context(None)
        assert "previous_commit_id" in seen["kwargs"], (
            "没有前一版本时必须**显式**要求 previous_commit_id IS NULL，"
            "而不是让服务自行解析"
        )
        assert seen["kwargs"]["previous_commit_id"] is None


class TestSingleCommitGroupBaseline:
    """写侧 1：多文件选择里的单提交组，基线是该文件真正的上一版，不是「空版本」。"""

    def _run(self, monkeypatch):
        sentinel = SimpleNamespace(commit_id="p" * 40, version="p" * 8)
        captured = {}

        def _resolve(commit, file_commits=None):
            captured.setdefault("resolved_for", []).append(commit.commit_id)
            return sentinel

        def _unified(commit, previous=None):
            captured["current"] = commit.commit_id
            captured["previous"] = previous
            return {"type": "excel", "sheets": {"奖励模式": {"rows": [{"row_number": 1}]}}}

        monkeypatch.setattr(logic, "resolve_previous_commit", _resolve)
        monkeypatch.setattr(logic, "_get_unified_diff_data", _unified)

        result = logic.handle_different_files_merge({
            "config/a.xlsx": [_commit("a", minute=1)],
            "config/b.xlsx": [_commit("b", path="config/b.xlsx", minute=2)],
        })
        return captured, result

    def test_previous_is_resolved_not_none(self, monkeypatch):
        captured, result = self._run(monkeypatch)
        assert captured["previous"] is not None, (
            "单提交组又传了 previous_commit=None —— 整份文件会被当成新增"
            "（线上表现形式：一份改了 3 行的配表显示成整表新增）"
        )
        assert captured["previous"].commit_id == "p" * 40
        assert captured["resolved_for"] == ["a" * 40, "b" * 40], (
            "每个文件的单提交组都要各自解析它自己的上一版"
        )
        assert result["total_files"] == 2


class TestRangeFallbackBaseline:
    """写侧 2：范围 diff 失败时的回退，基线必须是**区间起点之前**的那一条。"""

    def _run(self, monkeypatch, commit_chars):
        sentinel = SimpleNamespace(commit_id="q" * 40, version="q" * 8)
        captured = {}

        class _FakeGitService:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_parent_commit(self, _commit_id):
                return None          # 逼出「范围 diff 失败 → 回退」这条路径

        import services.threaded_git_service as threaded_module
        monkeypatch.setattr(threaded_module, "ThreadedGitService", _FakeGitService)
        monkeypatch.setattr(logic, "_excel_cache_service",
                            SimpleNamespace(is_excel_file=lambda _p: True))
        monkeypatch.setattr(logic, "resolve_previous_commit",
                            lambda commit, file_commits=None: sentinel)

        def _unified(commit, previous=None):
            captured["current"] = commit.commit_id
            captured["previous"] = previous
            return {"type": "excel", "sheets": {}}

        monkeypatch.setattr(logic, "_get_unified_diff_data", _unified)

        commits = [_commit(ch, minute=index) for index, ch in enumerate(commit_chars)]
        logic.handle_consecutive_commits_merge_internal(commits)
        return commits, captured

    def test_two_commit_group_does_not_compare_latest_with_itself(self, monkeypatch):
        commits, captured = self._run(monkeypatch, ["a", "b"])
        assert captured["previous"].commit_id != commits[-1].commit_id, (
            "两条提交的区间回退时基线就是 latest_commit 自己 —— 整段区间的变更被算成无变更"
        )
        assert captured["previous"].commit_id == "q" * 40

    def test_longer_group_does_not_use_a_commit_from_the_middle(self, monkeypatch):
        commits, captured = self._run(monkeypatch, ["a", "b", "c"])
        middle = commits[1].commit_id
        assert captured["previous"].commit_id not in {c.commit_id for c in commits}, (
            f"基线取到了区间内部的提交（{middle[:8]}）—— 只显示区间尾部，"
            "而页头声称覆盖整段区间"
        )
