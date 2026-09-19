# -*- coding: utf-8 -*-
"""全仓不许再有 `Model.query.get(...)` —— SQLAlchemy 2 里它是 legacy API。

## 这一版修掉的两个「看起来在扫、其实扫不到」

1. **白名单**。原来写死了九个包 + `app.py`。根目录的 `config.py`、以及后来新增的
   `skills/` 都不在里面 —— 新开一个包，扫描范围不会跟着变大，而这条用例**仍然全绿**。
   现在反过来了：从仓库根**递归全扫**，只列**明确不装源码**的目录
   （`__pycache__` / `.git` / 缓存 / `instance` / `repos` …）。新增目录默认被扫到，
   要把某个目录排除，必须显式写进来 —— 排除是需要理由的动作，漏扫不是。
   `tests/` 从前被整个排除（因为那几个兄弟用例里写着 `.query.get(` 这个**字符串**），
   这一版不必再排除：见第 2 条。

2. **文本匹配**。`.query.get(` 出现在**注释或字符串**里也会被算成「用了老 API」。
   这方向上的错是**假红**（安全），但它逼着上一版把 `tests/` 整个排除掉，
   于是 `tests/test_excel_commit_compare.py` 里那句**真的** `Commit.query.get(commit_id)`
   就藏在那块被排除的地里 —— 假红换来的是假绿。现在用 AST 只看**真实的属性访问**，
   注释、文档字符串、测试里刻意写的那个字符串都不再干扰。

扫不到东西的失败形态最容易漏：glob 写错、根路径算错，offenders 一样是空列表，
用例一样绿。所以下面有一条**自检**——扫过的文件数必须够多。
"""
from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 明确「不装源码」的目录：按目录名匹配，任何一层命中即整棵跳过。
_SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".github",
    ".claude",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "instance",   # 运行期数据（sqlite / 发布包），不是源码
    "repos",      # 仓库工作副本
    "logs",
    "docs",
}

# 扫过的文件数下限。仓库里现在有 1000+ 个 .py；这个数只用来抓「扫描范围塌成 0」，
# 不跟着仓库增长而调整 —— 定得越低越不会误红，但必须远大于 0。
_MIN_SCANNED_FILES = 200


class _LegacyQueryGetFinder(ast.NodeVisitor):
    """找出 `X.query.get(...)`。

    只认属性访问链，不看字面量：`"...query.get(" in text` 那种判据会把注释、
    docstring、以及「禁止这种写法」的说明书本身都算成违规。
    """

    def __init__(self):
        self.hits: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):  # noqa: N802 (ast 的接口名)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            receiver = func.value
            # `X.query.get(1)` —— 直接就是 `.query`
            if isinstance(receiver, ast.Attribute) and receiver.attr == "query":
                self.hits.append((node.lineno, ast.unparse(func)))
            # `db.session.query(X).get(1)` —— `.query(...)` 的返回值上再 `.get`
            elif (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Attribute)
                and receiver.func.attr == "query"
            ):
                self.hits.append((node.lineno, ast.unparse(func)))
        self.generic_visit(node)


def _iter_source_files():
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        yield path


def _legacy_hits(path: Path) -> list[tuple[int, str]]:
    # `utf-8-sig`：`app.py` 带 UTF-8 BOM，直接用 utf-8 读出来第一个字符是 U+FEFF，
    # `ast.parse` 会以 `SyntaxError: invalid non-printable character` 报错 ——
    # 全仓扫描会在这里整个崩掉（而这不是「扫到了违规」，是「没扫完」）。
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    finder = _LegacyQueryGetFinder()
    finder.visit(tree)
    return finder.hits


def test_the_detector_tells_code_from_prose():
    """自检：判据必须只认**真实调用**。

    这一段同时放了三种东西，只有第一种该被报出来：
    真实的 `Model.query.get(...)`、注释里提到的 `.query.get(`、
    以及字符串里写的 `.query.get(`（就是本文件与那两个兄弟用例的写法）。
    """
    sample = (
        "def f(a):\n"
        "    # 这里曾经写 .query.get( 这种老写法\n"
        '    note = ".query.get("\n'
        "    print(note)\n"
        "    return a.query.get(1)\n"
    )
    tree = ast.parse(sample)
    finder = _LegacyQueryGetFinder()
    finder.visit(tree)

    assert [line for line, _text in finder.hits] == [5], (
        f"应当只报出第 5 行那次真实调用，实际报出：{finder.hits}"
    )


def test_the_scan_actually_covers_the_repository():
    """自检：扫到的文件数要是个像样的数 —— 否则「没扫到」与「没问题」分不开。"""
    files = list(_iter_source_files())

    assert len(files) >= _MIN_SCANNED_FILES, (
        f"只扫到 {len(files)} 个 .py —— 扫描范围塌了（阈值 {_MIN_SCANNED_FILES}）"
    )
    # 抽查几个**必须**在范围内的位置：根目录的 .py 与当初被漏掉的目录。
    relative = {path.relative_to(PROJECT_ROOT).as_posix() for path in files}
    for expected in ("app.py", "config.py", "services/ai_analysis_service.py"):
        assert expected in relative, f"{expected} 不在扫描范围内"


def test_repository_code_has_no_legacy_query_get_calls():
    offenders: list[str] = []

    for path in _iter_source_files():
        for line, text in _legacy_hits(path):
            offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}: {text}")

    assert offenders == [], (
        "还有地方在用 SQLAlchemy 2 里已 legacy 的 `Query.get()`，"
        "换成 `db.session.get(Model, pk)`：\n  " + "\n  ".join(offenders)
    )
