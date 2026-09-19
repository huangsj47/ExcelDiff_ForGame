# -*- coding: utf-8 -*-
r"""SVN 与 Git 在「拿哪一版当基线」这件事上必须是同一套口径。

## 缺陷形态（两种，症状一模一样）

平台对 Git 与 SVN 各写了一条分支。Git 那条把解析好的 `previous_commit` 传下去，
SVN 那条**在几个地方把它丢掉或算错**，而后果都是同一句话：

    SVN 仓库里一条只改了几格的 Excel 提交，页面把整份文件渲染成「全部新增」

因为下游 `get_unified_diff_data` 看到「没有前一版」时，会去取上一版内容的那一步
整个跳过（`if has_previous:`），比较器拿到 `previous_data = {}`，每张表都走
「新增工作表」分支。**它不是空白页、不报错 —— 是一份看起来完全合理但是假的差异。**

这里钉三处，全部是「Git 对、SVN 错」的不对称：

1. `services/commit_diff_logic.py` 的 `get_diff_data` 里，SVN Excel 分支把基线
   **写死成 `None`**（Git 分支传的是解析出来的那条）；
2. 同一个文件 `get_real_diff_data_for_merge` 的 SVN Excel 分支同样写死 `None`
   （Git 分支先读缓存）；
3. `services/svn_service.py` 取认证信息时把 **`token` 当密码**（SVN 表单写的是
   `password`），于是要认证的 SVN 服务器上 `svn log` 只带 `--username`、
   不带 `--password`，`get_file_history` 返回空 ⇒ 「库里缺前一条记录就去 VCS 找」
   那条回退**永远拿不到**基线。

以及一处「两种仓库都错」的：`_commit_id_matches` 的前缀匹配对 SVN 不成立
（`r100` 与 `r1000` 是两个完整修订号，不是缩写关系）。
"""
from __future__ import annotations

import ast
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _read(rel_path: str) -> str:
    return open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8-sig").read()


def _strip_comments(source: str) -> str:
    """剥掉注释再断言。

    这个仓库的注释里会**原样引用**要禁掉的写法（本条修复的说明就引用了
    `_get_unified_diff_data(commit, None)`），不剥就是假失败。
    """
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    return re.sub(r"(?m)#.*$", "", source)


_HARDCODED_NONE = "_get_unified_diff_data(commit, None)"
_TARGET_FUNC = "_get_unified_diff_data"


def _is_repo_type_test(test, kinds: set) -> bool:
    """这个 `if` 条件是不是 `repository.type == '<kind>'`（也允许 `in (...)`）。"""
    if not isinstance(test, ast.Compare):
        return False
    left = test.left
    if not (isinstance(left, ast.Attribute) and left.attr == "type"):
        return False
    for comparator in test.comparators:
        values = (
            comparator.elts if isinstance(comparator, (ast.Tuple, ast.List)) else [comparator]
        )
        for value in values:
            if isinstance(value, ast.Constant) and value.value in kinds:
                return True
    return False


def _calls_with_a_none_baseline(node) -> list:
    """`_get_unified_diff_data(commit, None)` 形态的调用（第二个实参是 None）。"""
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = (
            child.func.attr
            if isinstance(child.func, ast.Attribute)
            else getattr(child.func, "id", "")
        )
        if name != _TARGET_FUNC or len(child.args) < 2:
            continue
        second = child.args[1]
        if isinstance(second, ast.Constant) and second.value is None:
            found.append(child)
    return found


def _hardcoded_none_inside_svn_branches(source: str) -> list:
    """`repository.type == 'svn'` 分支**体内部**（含嵌套）那些写死 `None` 的行号。

    **用 AST 而不是「往回找最近的 `repository.type`」**：后者会把
    「这是最早的提交，与初始版本比较」那一支也算成 SVN 分支的（它在文件里排在
    SVN 分支后面几十行），而那一支传 `None` 是语义正确的。AST 的祖先关系是精确的。
    """
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _is_repo_type_test(node.test, {"svn"}):
            continue
        for statement in node.body:
            lines.extend(call.lineno for call in _calls_with_a_none_baseline(statement))
    return sorted(lines)


class TestTheSvnBranchKeepsTheBaselineItResolved:
    def test_no_svn_branch_hardcodes_the_baseline_to_none(self):
        source = _read("services/commit_diff_logic.py")
        offenders = _hardcoded_none_inside_svn_branches(source)
        assert offenders == [], (
            f"SVN 的 Excel 分支又把基线写死成 None 了（行 {offenders}）—— "
            f"整份表会被渲染成「全部新增」"
        )

    def test_the_scan_can_actually_fail(self):
        """**非空自检**：这段扫描器必须能在一份已知有问题的样本上报出来。

        否则上面那条断言可以因为「解析写坏了、一处都扫不到」而永远通过。
        """
        sample = (
            "if repository.type == 'git':\n"
            "    return _get_unified_diff_data(commit, previous_commit)\n"
            "elif repository.type == 'svn':\n"
            "    if is_excel:\n"
            "        return _get_unified_diff_data(commit, None)\n"
            "    return None\n"
        )
        assert _hardcoded_none_inside_svn_branches(sample) == [5]

    def test_the_scan_ignores_a_none_outside_the_svn_branch(self):
        """同一份样本里把 `None` 挪到 SVN 分支**之外**，扫描器就必须闭嘴 ——
        这正是「最早的提交」那一支的处境。"""
        sample = (
            "if repository.type == 'svn':\n"
            "    return _get_unified_diff_data(commit, previous_commit)\n"
            "if something_else:\n"
            "    return _get_unified_diff_data(commit, None)\n"
        )
        assert _hardcoded_none_inside_svn_branches(sample) == []

    def test_the_svn_excel_branches_pass_a_previous_commit(self):
        source = _strip_comments(_read("services/commit_diff_logic.py"))
        assert "_get_unified_diff_data(commit, previous_commit)" in source
        # 合并视图那一支没有 previous_commit 在作用域里，它自己解析
        assert "_get_unified_diff_data(" in source

    def test_the_earliest_commit_case_is_still_allowed_to_pass_none(self):
        """**不能靠「一律不许传 None」通过。**

        「这是最早的提交，与初始版本比较」那一支传 `None` 是**语义正确**的：
        这个文件在窗口开始时不存在，一切都该是新增。它与 `repository.type` 无关，
        Git/SVN 待遇相同 —— 所以它必须**不在**任何 `repository.type == 'svn'` 的分支里，
        而文件里还得留着它。
        """
        raw = _read("services/commit_diff_logic.py")
        assert "这是最早的提交" in raw
        # 数的时候要剥注释：修复说明里**原样引用**了那句写法（本仓注释的常见做法）
        code = _strip_comments(raw)
        assert code.count(_HARDCODED_NONE) == 1, (
            f"还应当**恰好剩一处**传 None（最早提交那一支），"
            f"实际 {code.count(_HARDCODED_NONE)} 处"
        )
        assert _hardcoded_none_inside_svn_branches(raw) == []


class TestTheSvnServiceSendsThePasswordItWasGiven:
    """`repository.token` 是 **Git** 的字段；SVN 的凭据在 `repository.password`。

    SVN 表单只写 `password`（`services/repository_update_form_service.py`），
    所以拿 `token` 当密码等于**什么都不发**。

    断言必须带**变量名与缩进**，不能只找
    `token = self.repository_password or ...`：那一串是
    `repository_token = self.repository_password or ...` 的**子串**，
    只按子串找的话，两处里改坏一处另一处还能把它「证明」成好的
    （写这条时真的踩到了 —— 变异检查没红）。
    """

    _FILE_HISTORY_LINE = "            token = self.repository_password or self.repository_token"
    _COMMIT_INFO_LINE = (
        "            repository_token = self.repository_password or self.repository_token"
    )

    def test_file_history_uses_the_password_field(self):
        source = _strip_comments(_read("services/svn_service.py"))
        assert self._FILE_HISTORY_LINE in source, (
            "get_file_history 又拿 token 当密码了：要认证的 SVN 服务器上 "
            "--password 根本不会出现在命令行里"
        )

    def test_commit_info_uses_the_password_field(self):
        source = _strip_comments(_read("services/svn_service.py"))
        assert self._COMMIT_INFO_LINE in source

    def test_exactly_two_sites_were_changed(self):
        """两处**各自**成立，而不是「其中一处对就算过」。"""
        source = _strip_comments(_read("services/svn_service.py"))
        assert source.count(self._FILE_HISTORY_LINE) == 1
        assert source.count(self._COMMIT_INFO_LINE) == 1

    def test_the_other_sites_were_already_right(self):
        """反例钉住：`_build_auth_args` / `get_commits` / `_get_file_binary_content`
        用的本来就是 `repository_password` —— 这证明「该用 password」是本文件的既有口径，
        那两处是漏改，不是另一种设计。"""
        source = _strip_comments(_read("services/svn_service.py"))
        assert source.count("password = self.repository_password") >= 3


class TestSvnRevisionsAreNotAbbreviations:
    def test_r100_and_r1000_are_different_commits(self):
        from services.commit_diff_logic import _commit_id_matches

        assert _commit_id_matches("r100", "r1000") is False
        assert _commit_id_matches("r1000", "r100") is False
        assert _commit_id_matches("r100", "r100") is True

    def test_git_short_shas_still_match_by_prefix(self):
        """改 SVN 判据**不能顺手把 Git 的前缀匹配也关了** —— 那是这个函数存在的理由。"""
        from services.commit_diff_logic import _commit_id_matches

        assert _commit_id_matches("a1b2c3", "a1b2c3d4e5f6") is True
        assert _commit_id_matches("a1b2c3d4e5f6", "a1b2c3") is True

    @pytest.mark.parametrize(
        "value,expected",
        [("r1", True), ("r19", True), ("r0", True), ("a1b2c3", False), ("r", False), ("19", False)],
    )
    def test_the_shape_test_itself(self, value, expected):
        from services.commit_lookup_service import is_svn_revision

        assert is_svn_revision(value) is expected

    def test_why_r_cannot_collide_with_a_git_sha(self):
        """`r` 不是十六进制数字 —— 这就是「凭形状判」成立的全部理由。"""
        assert "r" not in "0123456789abcdef"


class TestTheSharedCommitLookup:
    """两处「按 commit_id 找提交」的查询共用同一口径，且 SVN 不走前缀。"""

    def test_both_weekly_call_sites_use_the_shared_lookup(self):
        for rel in (
            "services/weekly_version_file_handlers.py",
            "services/weekly_deleted_excel_helpers.py",
        ):
            source = _strip_comments(_read(rel))
            assert "find_commit_by_commit_id(" in source, rel
            assert "commit_id.like(" not in source, (
                f"{rel} 又自己写前缀匹配了 —— SVN 的 r1 会匹配上 r19"
            )

    def test_the_shared_lookup_refuses_prefix_matching_for_svn(self):
        source = _strip_comments(_read("services/commit_lookup_service.py"))
        assert "if is_svn_revision(text):" in source
        assert "return None" in source

    def test_svn_revision_number_strips_only_the_prefix(self):
        from services.commit_lookup_service import svn_revision_number

        assert svn_revision_number("r19") == "19"
        assert svn_revision_number("19") == "19"
        # `replace('r','')` 会把串里每一个 r 都删掉 —— 那是「输入恰好简单」而不是
        # 「写法正确」。这条用一个含 r 的串把它区分开。
        assert svn_revision_number("r1r2") == "r1r2"
