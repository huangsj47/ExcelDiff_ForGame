"""禁止使用 CI 的 Python 版本（3.11）不支持的语法。

## 为什么需要这条

本机开发用的是 Python 3.13，而 CI 跑的是 **3.11**（`.github/workflows/quality-gate.yml`
的 `python-version`）。两者之间夹着一个 3.12：**PEP 701 放宽了 f-string**，从 3.12
起 f-string 的**表达式部分**里允许出现反斜杠。于是下面这种写法：

    sqlite_uri = f"sqlite:///{os.path.abspath(p).replace('<反斜杠>', '/')}"

在本机（3.13）完全合法、`pytest` 全绿，到 CI（3.11）却是 `SyntaxError`。
而且它坏得特别彻底：出错的是 `tests/conftest.py`，**conftest 加载失败意味着整个
测试任务无法启动**，不是挂一条用例，而是 `Process completed with exit code 4`。
本地怎么跑都发现不了——这正是要加静态守卫的理由。

## 为什么不用 `ast.parse(..., feature_version=(3, 11))`

看着像正解，但**实测无效**：在本机 3.13 上对上面那段缺陷源码调用
`ast.parse(src, feature_version=(3, 11))`，返回的是**成功**，不抛 SyntaxError。
CPython 文档也说明 `feature_version` 是 best-effort，并不覆盖全部差异
（f-string 的分词差异就不在其列）。所以这里改走 AST：直接看每个
`FormattedValue`（f-string 里的 `{...}`）的**源码片段**里有没有反斜杠。

该判据已双向验证：对缺陷源码命中，对合法写法（如 `chr(92)`）不误报——
两条都固化成本文件里的自检用例，避免守卫哪天悄悄失效。

## 这个文件自己不能出现反斜杠字面量

它扫的是「被跟踪的全部 .py」，**包括它自己**。所以下面一律用 `chr(92)` 构造
反斜杠，而不是写字面量——否则守卫会先把自己判红。（同类坑：本仓库
`tests/test_incremental_cache_module_path.py` 的 docstring 里写裸模块名，
提交后被自己扫到。）

## 怎么在本机复现 CI 的行为

**本机是 3.13，这个文件里的坑只在 3.11 上现形**（`ast.parse` 在 3.11 上会拒绝
缺陷源码本身，见 `_fstring_expressions_with_backslash` 的 docstring）。本机装了 3.11
时可以一条命令验：

    py -3.11 -m pytest tests/test_python311_syntax_compat.py --noconftest

`--noconftest` 是必需的：`tests/conftest.py` 会 import sqlalchemy，而 3.11 那个环境
里没装。这个文件自带全部依赖，跳过 conftest 不影响它要守的东西。
（CI 上跑的是完整套件，不需要这个参数。）
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 反斜杠的码点。**不要**在这里写成字面量，理由见模块 docstring。
_BACKSLASH = chr(92)

# 3.12 才引入的 AST 节点类型（PEP 695 的 type 别名 / 泛型参数）。
# 用 getattr 取，因为本文件本身要在 3.11 上可导入——直接写 ast.TypeAlias
# 会在 3.11 上 AttributeError。
_PY312_ONLY_NODES = tuple(
    node_type
    for node_type in (
        getattr(ast, "TypeAlias", None),
        getattr(ast, "TypeVar", None),
        getattr(ast, "ParamSpec", None),
        getattr(ast, "TypeVarTuple", None),
    )
    if node_type is not None
)


def _tracked_python_files() -> list[str]:
    """被 git 跟踪的 .py 文件（用 -z，避免非 ASCII 文件名被 quotepath 转义）。"""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("不是 git 工作区，无法枚举被跟踪的源码")
    proc = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        pytest.skip(f"git ls-files 失败：{proc.stderr.strip()}")
    return [name for name in proc.stdout.split("\0") if name]


def _read_source(rel: str) -> str:
    """读源码，剥掉可能存在的 UTF-8 BOM。

    用 `utf-8-sig` 而不是 `utf-8`：CPython 自己读源文件时**会**跳过开头的 BOM
    （PEP 263 的编码探测会把 BOM 吃掉），但 `ast.parse` 收到的是字符串、
    不做这一步，于是报 `invalid non-printable character U+FEFF`。
    本仓库有若干文件带 BOM，若用 `utf-8` 读会全部误判成「无法解析」。
    """
    return (REPO_ROOT / rel).read_text(encoding="utf-8-sig")


def _neutralize_backslashes(source: str) -> str:
    """把反斜杠换成长度相同的占位字符，其余原样。

    **等长是关键**：替换后 `ast.parse` 的节点偏移量与原源码一一对应，于是可以用
    `ast.get_source_segment(原始源码, node)` 取回**带真实反斜杠**的片段。若长度变了，
    偏移量整体错位，取回来的片段就是错的（而且错得不明显）。
    """
    return source.replace(_BACKSLASH, "_")


def _collect_fstring_offenders(tree: ast.AST, source: str) -> list[str]:
    """在一棵已解析的树上找「表达式里含反斜杠的 f-string 片段」。

    `source` 是**取片段用的文本**，不一定是被解析的那份 —— 见下面的降级路径。
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue):
            continue
        for part in (node.value, node.format_spec):
            if part is None:
                continue
            segment = ast.get_source_segment(source, part)
            if segment and _BACKSLASH in segment:
                offenders.append(f"line {node.lineno}: {segment.strip()}")
    return offenders


def _fstring_expressions_with_backslash(source: str) -> list[str]:
    """返回所有「表达式里含反斜杠的 f-string 片段」，形如 `path.py:12 {…}`。

    `FormattedValue` 就是 f-string 里的一个 `{...}`。它的 `value` 是表达式、
    `format_spec` 是 `:` 后面的格式说明，两处都在 PEP 701 放宽的范围内，
    所以两处都要看。

    ## 为什么要有降级路径（这条是 CI 上真挂过一次的）

    在 **3.11**（CI 的版本）上，`ast.parse` 对这段源码本身就会抛
    `f-string expression part cannot include a backslash` —— 检测器要检的东西，
    正是它没法解析的东西。测过的结论是：3.11 的 `tokenize` 能过（f-string 在 3.11
    里是单个 STRING token，拒绝它的是解析器），所以降级路径可行。

    于是解析失败时：把反斜杠换成等长占位字符再解析，节点偏移量不变，**片段仍从原始
    文本取**，拿到的就是带真实反斜杠的内容。真在 3.11 上跑过：能正确定位到那一行。

    替身之后仍然解析不了，就让 `SyntaxError` 冒上去 —— 那是「文件用了当前解释器不支持的
    其它语法」，调用方按「无法解析」报出来，而不是当作没问题跳过。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = ast.parse(_neutralize_backslashes(source))
    return _collect_fstring_offenders(tree, source)


# --------------------------------------------------------------------------
# 自检：确认检测器本身没有失效
# --------------------------------------------------------------------------


def _defective_source() -> str:
    """构造 `f"…{p.replace('<反斜杠>', '/')}"` —— 3.11 下是 SyntaxError。"""
    return (
        'x = f"a{os.path.abspath(p).replace('
        + "'"
        + _BACKSLASH * 2
        + "'"
        + ", '"
        + "/"
        + "')}\""
    )


_LEGAL_SOURCE = 'y = f"a{p.replace(chr(92), chr(47))}"'


def test_the_defective_source_really_is_defective_on_python_311():
    """先钉住前提：这条合成源码确实含反斜杠字面量。

    否则「检测器能命中」可能只是因为源码构造错了，守卫就成了空转。
    """
    source = _defective_source()
    assert _BACKSLASH in source
    # 反斜杠必须落在 `{...}` 里面，落在外面（普通字符串里）是合法的。
    assert source.index(_BACKSLASH) > source.index("{")


def test_detector_fires_on_the_defect_it_was_written_for():
    """核心自检：检测器必须能命中缺陷源码。

    没有这条，将来 `_fstring_expressions_with_backslash` 被改坏（比如返回空列表）
    时，`test_no_fstring_expression_contains_a_backslash` 会**照样全绿**，
    守卫变成装饰品。

    **这条在 3.11 与 3.13 上都要过** —— 原先它在 3.11 上会因为 `ast.parse` 拒绝
    缺陷源码而自身抛 SyntaxError（CI 上就是这么挂的）。见
    `test_the_fallback_path_finds_the_defect_when_the_parser_refuses`。
    """
    offenders = _fstring_expressions_with_backslash(_defective_source())
    assert offenders, "检测器漏掉了反斜杠在 f-string 表达式里的写法"
    assert "replace(" in offenders[0]


def test_the_fallback_path_finds_the_defect_when_the_parser_refuses(monkeypatch):
    """**降级路径必须在任何解释器上都被跑到。**

    它只在 `ast.parse` 拒绝时才走，而那只发生在 3.11 上 —— 也就是说本机（3.13）
    开发时这条分支**永远不会执行**，改坏了也看不出来，直到 CI 挂掉。所以这里把
    `ast.parse` 替成一个「见到反斜杠就拒绝」的版本，人为触发它。

    断言两件事：降级路径能找到缺陷，且**返回的片段来自原始文本**（含真实反斜杠，
    行号也对）—— 替身字符只用来解析，不能污染给用户看的内容。
    """
    real_parse = ast.parse
    seen = {"calls": 0}

    def refuse_on_backslash(source, *args, **kwargs):
        seen["calls"] += 1
        if _BACKSLASH in source:
            raise SyntaxError("f-string expression part cannot include a backslash")
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", refuse_on_backslash)
    offenders = _fstring_expressions_with_backslash(_defective_source())

    assert seen["calls"] == 2, "没有走降级路径（应当先试原始源码、再试替身字符）"
    assert offenders, "降级路径没能找到缺陷"
    assert "replace(" in offenders[0]
    assert _BACKSLASH in offenders[0], "报出来的是替身字符，而不是原始文本里的片段"
    assert "_" not in offenders[0].split("replace(", 1)[1][:6], "替身字符混进了片段内容"


def test_an_unparsable_source_is_reported_rather_than_skipped():
    """解析不了的文件要被报出来，**不能当作「没问题」跳过**。

    这是最后一层兜底：如果某个文件用了 3.12+ 的其它语法（替身反斜杠也救不回来），
    静默跳过等于守卫失效。
    """
    offenders, unparsable = _scan_sources({"broken.py": "def f(:\n"})
    assert not offenders
    assert unparsable, "解析失败的文件被静默跳过了"
    assert "broken.py" in unparsable[0]


def test_detector_does_not_fire_on_the_equivalent_legal_spelling():
    """反证：合法写法不得误报。

    `chr(92)` 是反斜杠的**合法**替代写法（本文件自己就在用），
    源码文本里没有任何反斜杠字符，3.11 下完全合法。
    """
    assert _BACKSLASH not in _LEGAL_SOURCE
    assert _fstring_expressions_with_backslash(_LEGAL_SOURCE) == []


def test_detector_does_not_fire_on_a_plain_backslash_string():
    """反斜杠出现在 f-string **之外**是合法的，不能误判。"""
    source = 'sep = "' + _BACKSLASH * 2 + '"'
    assert _fstring_expressions_with_backslash(source) == []


# --------------------------------------------------------------------------
# 真正的守卫
# --------------------------------------------------------------------------


def _scan_sources(sources: dict) -> tuple[list[str], list[str]]:
    """扫一批源码，返回 `(命中片段, 解析不了的文件)`。

    抽成函数是为了让真正的守卫与自检用例走**同一份**逻辑：守卫里那段 try/except 若各写
    一份，自检就只在验证一个副本。
    """
    offenders: list[str] = []
    unparsable: list[str] = []
    for name, source in sources.items():
        try:
            offenders.extend(f"{name}:{item}" for item in _fstring_expressions_with_backslash(source))
        except SyntaxError as exc:
            # 在当前解释器上就解析不了 —— 本身就是缺陷（可能正是 3.12+ 语法）。
            unparsable.append(f"{name}:{exc.lineno} {exc.msg}")
    return offenders, unparsable


def test_no_fstring_expression_contains_a_backslash():
    """全仓守卫：任何被跟踪的 .py 都不得在 f-string 表达式里用反斜杠。"""
    offenders, unparsable = _scan_sources(
        {rel: _read_source(rel) for rel in _tracked_python_files()}
    )

    assert not unparsable, "以下文件无法解析（可能用了当前解释器不支持的语法）：\n  " + "\n  ".join(
        unparsable
    )
    assert not offenders, (
        "以下 f-string 的表达式部分里出现了反斜杠。这是 Python 3.12+（PEP 701）"
        "才允许的写法，CI 的 3.11 会直接 SyntaxError；若该文件是 conftest.py，"
        "整个测试任务都无法启动。改用 Path(...).as_posix()、os.sep 或 chr(92)：\n  "
        + "\n  ".join(offenders)
    )


def test_no_python312_only_syntax_nodes():
    """顺带挡住 PEP 695 的 `type X = ...` / `def f[T]()` 这类 3.12+ 语法。

    在 3.11 上这些会编译失败；在 3.12+ 上则表现为特定 AST 节点类型。
    本文件在 3.11 上运行时 `_PY312_ONLY_NODES` 为空元组，此时跳过
    （那种环境下 3.11 自己就会在 CI 里报错，不需要这条兜底）。
    """
    if not _PY312_ONLY_NODES:
        pytest.skip("当前解释器没有 3.12+ 的 AST 节点类型，无需检查")

    offenders: list[str] = []
    for rel in _tracked_python_files():
        source = _read_source(rel)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue  # 由上面那条用例负责报出
        for node in ast.walk(tree):
            if isinstance(node, _PY312_ONLY_NODES):
                offenders.append(f"{rel}:{node.lineno} {type(node).__name__}")

    assert not offenders, (
        "以下位置用了 Python 3.12+ 才有的语法（PEP 695），CI 的 3.11 无法编译：\n  "
        + "\n  ".join(offenders)
    )
