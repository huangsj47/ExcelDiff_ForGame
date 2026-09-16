# -*- coding: utf-8 -*-
"""`scripts/run_ruff_changed.py` 必须真的只拦「改动行」上的问题。

## 缺陷

脚本的契约写在它自己的输出里：「gate only changed lines」「ignored existing
diagnostics outside changed lines」。但过滤那一步比较的是

    filename = _normalize_path(issue.get("filename") or "")
    if not filename or filename not in changed_lines:   → 保留

而 `changed_lines` 的键来自 `git diff`，是**仓库相对路径**
（`services/task_worker_service.py`）；`ruff check --output-format json <相对路径>`
回填的却是**绝对路径**：

    实测（Windows）：C:\\Users\\...\\ExcelDiff_ForGame\\services\\task_worker_service.py

`_normalize_path` 只把 `\\` 换成 `/`，不会做成相对路径 —— 于是
`filename not in changed_lines` **恒为真**，每一条诊断都被当成「新增行上的问题」
保留。后果：

1. 脚本宣称的「只检查改动行」完全失效，退化成「检查被碰过的整个文件」；
2. 只要改到任何一个历史 lint 债较多的文件（例如 `task_worker_service.py` 里有
   一堆 F541），CI 就会被一堆**与本次改动无关**的诊断挡住 —— 实测 39 条。
   这会让门禁变成「谁碰谁倒霉」，最后大家学会绕过它。

## 本文件断言什么

1. 绝对路径能被换算成仓库相对路径（这正是修复的核心）；
2. 只有落在改动行上的诊断会被拦下，改动行之外的被忽略；
3. 完全没被改动的文件里的诊断（`changed_lines` 里没有这个键）仍然拦下
   —— 那种情况是脚本没拿到 diff，宁可严一点；
4. 新增文件（`changed_lines[file] is None`）里的任何诊断都拦下。

## 为什么值得单独写测试

门禁脚本本身没有门禁：它坏掉的时候不会有人发现 —— 它只是变吵或变哑。上面第 2 条
就是「变哑/变吵」的分水岭，钉住它。
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import run_ruff_changed as gate  # noqa: E402


def _issue(filename, row, code="F541"):
    return {
        "filename": filename,
        "code": code,
        "message": "f-string without any placeholders",
        "location": {"row": row, "column": 1},
    }


class TestPathNormalization:
    def test_absolute_path_becomes_repo_relative(self):
        root = "C:/work/ExcelDiff_ForGame"
        absolute = "C:/work/ExcelDiff_ForGame/services/task_worker_service.py"
        assert gate._to_repo_relative(absolute, root) == "services/task_worker_service.py"

    def test_windows_backslash_absolute_path(self):
        root = r"C:\work\ExcelDiff_ForGame"
        absolute = r"C:\work\ExcelDiff_ForGame\services\svn_service.py"
        assert gate._to_repo_relative(absolute, root) == "services/svn_service.py"

    def test_already_relative_path_is_untouched(self):
        assert gate._to_repo_relative("services/svn_service.py", "/repo") == "services/svn_service.py"

    def test_repo_root_lookup_returns_repository(self):
        root = gate._repo_root()
        assert os.path.isfile(os.path.join(root, "scripts", "run_ruff_changed.py")), (
            f"_repo_root() 返回的 {root} 看起来不是本仓库根目录"
        )


class TestChangedLineFiltering:
    ROOT = "C:/work/ExcelDiff_ForGame"
    FILE = "services/task_worker_service.py"
    ABS = "C:/work/ExcelDiff_ForGame/services/task_worker_service.py"

    def test_only_changed_lines_are_kept(self):
        """核心断言：改动行之外的历史诊断必须被忽略。

        这条正是修复前失败的形态 —— 传绝对路径时，第 2 条（794 行，未改动）
        会被错误地保留下来。
        """
        changed = {self.FILE: {800, 812}}
        issues = [
            _issue(self.ABS, 800),   # 改动行 → 保留
            _issue(self.ABS, 794),   # 未改动 → 忽略
            _issue(self.ABS, 1200),  # 未改动 → 忽略
        ]
        kept, ignored = gate._filter_ruff_issues_by_changed_lines(issues, changed, repo_root=self.ROOT)
        assert [i["location"]["row"] for i in kept] == [800], (
            "只有改动行（800）上的诊断该被保留；"
            "如果 794 / 1200 也出现在 kept 里，说明绝对路径没被换算成仓库相对路径，"
            "门禁退化成「检查整个被碰过的文件」。"
        )
        assert len(ignored) == 2

    def test_untracked_in_diff_file_keeps_everything(self):
        """脚本没拿到该文件 diff 时（键不存在）宁可严一点，全部保留。"""
        issues = [_issue(self.ABS, 1)]
        kept, _ignored = gate._filter_ruff_issues_by_changed_lines(issues, {}, repo_root=self.ROOT)
        assert len(kept) == 1

    def test_new_file_keeps_everything(self):
        """新增文件用 None 表示「全部算新增」。"""
        issues = [_issue(self.ABS, 1), _issue(self.ABS, 500)]
        kept, _ignored = gate._filter_ruff_issues_by_changed_lines(
            issues, {self.FILE: None}, repo_root=self.ROOT
        )
        assert len(kept) == 2

    def test_relative_filename_still_works(self):
        """ruff 在某些 cwd 下会回填相对路径，这条路径也要能过滤。"""
        issues = [_issue(self.FILE, 794)]
        kept, ignored = gate._filter_ruff_issues_by_changed_lines(
            issues, {self.FILE: {800}}, repo_root=self.ROOT
        )
        assert kept == []
        assert len(ignored) == 1


class TestWorkflowRunsPytest:
    """CI 必须真的跑测试 —— 否则 800+ 条用例只是装饰。"""

    def test_quality_gate_invokes_pytest(self):
        path = os.path.join(PROJECT_ROOT, ".github", "workflows", "quality-gate.yml")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        assert "pytest" in source, (
            "quality-gate.yml 里没有任何 pytest 步骤 —— 测试套件不是门禁的一部分。"
        )

    def test_pytest_is_declared_in_dev_requirements(self):
        path = os.path.join(PROJECT_ROOT, "requirements-dev.txt")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        assert "pytest" in source, (
            "requirements-dev.txt 里没有 pytest —— CI 装完依赖后无 pytest 可跑，"
            "新人照它装也跑不了测试。"
        )
