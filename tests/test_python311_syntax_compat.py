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
（f-string 的分词差异就不在其列）。

## 为什么也不用 AST 取片段（这条是误报过一次的）

一开始的写法是走 AST：遍历每个 `FormattedValue`（f-string 里的 `{...}`），
用 `ast.get_source_segment` 取它的源码片段，看里面有没有反斜杠。**判据本身是对的，
但 3.11 给不出正确的片段位置** —— 而 3.11 正是这个守卫要保护的解释器：

    f"{value:,} 后面有反斜杠<反斜杠>n"

第一个 `{value:,}` 的 `format_spec`，3.11 报的范围是**整条 f-string**，3.13 才报
`:,`。于是 3.11 上「表达式片段」里混进了**字面量部分**的反斜杠 —— 也就是说这个
守卫在它唯一需要工作的那个解释器上，会把合法代码判红。CI 上真挂过一次，误报的是
本仓自己的一条断言消息。

所以现在改成 **tokenize 取 f-string 原文 + 自己数花括号**（见 `_fstring_literals`），
两个解释器上结果一致，且**完全不依赖解析** —— 它甚至在源码语法有别的毛病时也照样
能报出反斜杠。

该判据已双向验证：对缺陷源码命中，对合法写法（如 `chr(92)`、以及反斜杠落在 `{}`
外面的那条真实误报）不误报——都固化成本文件里的自检用例，避免守卫哪天悄悄失效。

## 这个文件自己也要守这条规矩

它扫的是「被跟踪的全部 .py」，**包括它自己**。所以它自己的 f-string 里不能出现反斜杠
字面量；下面一律用 `chr(92)` 构造。（同类坑：本仓库
`tests/test_incremental_cache_module_path.py` 的 docstring 里写裸模块名，
提交后被自己扫到。）

**本机是 3.13，这个文件里的坑只在 3.11 上现形**（缺陷源码在 3.11 上是 `SyntaxError`，
在 3.13 上合法，所以本机跑全绿）。本机装了 3.11 时可以一条命令验：

    py -3.11 -m pytest tests/test_python311_syntax_compat.py --noconftest

`--noconftest` 是必需的：`tests/conftest.py` 会 import sqlalchemy，而 3.11 那个环境
里没装。这个文件自带全部依赖，跳过 conftest 不影响它要守的东西。
（CI 上跑的是完整套件，不需要这个参数。）
"""

from __future__ import annotations

import ast
import io
import subprocess
import tokenize
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

    保留给「这个文件在 3.11 上还能不能解析」那条判据用（见 `_scan_sources`）。
    """
    return source.replace(_BACKSLASH, "_")


def _looks_like_an_fstring(literal: str) -> bool:
    """这段字符串字面量是不是 f-string（看引号前的前缀里有没有 f/F）。"""
    quote_at = next((i for i, ch in enumerate(literal) if ch in "\"'"), -1)
    return quote_at > 0 and "f" in literal[:quote_at].lower()


def _fstring_body(literal: str) -> str:
    """剥掉前缀与引号，只留 f-string 的正文。"""
    quote_at = next((i for i, ch in enumerate(literal) if ch in "\"'"), -1)
    if quote_at < 0:
        return ""
    triple = literal[quote_at : quote_at + 3] in ('"""', "'''")
    quote = literal[quote_at : quote_at + (3 if triple else 1)]
    body = literal[quote_at + len(quote) :]
    return body[: -len(quote)] if body.endswith(quote) else body


def _line_offsets(source: str) -> list[int]:
    """每一行首字符在 `source` 里的绝对偏移（下标 0 对应第 1 行）。"""
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _fstring_literals(source: str) -> list[tuple[int, str]]:
    """源码里每一段 f-string 字面量的 `(起始行, 原文)`。

    ## 为什么用 tokenize 而不是 AST

    因为 **3.11 对 f-string 内部节点的 position 是错的**，而 3.11 正是这个守卫要保护的
    那个解释器。实测（`py -3.11` 与 3.13 各跑一遍同一条源码）：

        f"{value:,} 后面有反斜杠<反斜杠>n"

    第一个 `{value:,}` 的 `format_spec`：
      * 3.11 → 范围 `(1,4)-(1,25)`，取出来是**整条 f-string**；
      * 3.13 → 范围 `(1,12)-(1,14)`，取出来是 `:,`（正确）。

    于是「取表达式片段、看里面有没有反斜杠」这种写法，在 3.11 上会把**字面量部分**的
    反斜杠算到表达式头上 —— 合法代码被判违规。这条真在 CI 上挂过一次，误报的是本仓
    自己的一条断言消息（`f"…：{got}<反斜杠>n"`，反斜杠在 `{}` 外面）。

    ## 两个解释器上的取法

    tokenize 在两个版本上都能拿到 f-string 的**边界**：3.11 里它是**一个 STRING
    token**，3.12+（PEP 701）拆成 `FSTRING_START / FSTRING_MIDDLE / FSTRING_END`。
    两边的 `start` / `end` 位置都是可信的（坏掉的只是 f-string **内部**节点的位置）。

    所以这里**按位置回原源码里切**，而不是把 token 的 `string` 拼起来 ——
    3.12+ 里表达式部分是普通 token，只拼 `FSTRING_MIDDLE` / `FSTRING_END` 会**把表达式
    整段丢掉**（`f"a{p.replace('/')}"` 会拼成 `f"a}"`），报出来的行号对、内容却是错的。

    嵌套 f-string 用栈接：内层的 `FSTRING_END` 先弹，外层各自成段。
    """
    out: list[tuple[int, str]] = []
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            tokens.append(token)
    except (tokenize.TokenError, IndentationError):
        # 词法都不完整（多行字符串没闭合等）：交给 `_scan_sources` 的解析判据去报。
        pass

    offsets = _line_offsets(source)
    pending: list[tuple[int, int]] = []

    def slice_from(start: tuple[int, int], end: tuple[int, int]) -> str:
        return source[offsets[start[0] - 1] + start[1] : offsets[end[0] - 1] + end[1]]

    for token in tokens:
        if fstring_start is not None and token.type == fstring_start:
            pending.append(token.start)
        elif pending and fstring_end is not None and token.type == fstring_end:
            start = pending.pop()
            out.append((start[0], slice_from(start, token.end)))
        elif not pending and token.type == tokenize.STRING and _looks_like_an_fstring(token.string):
            # 3.11 的路：整条 f-string 就是一个 STRING token。
            out.append((token.start[0], token.string))
    return out


def _braced_regions(body: str) -> list[str]:
    """f-string 正文里每一段 `{…}` 的内容（含 `:` 之后的格式说明）。

    规则按 PEP 701 要守的范围来：**表达式与格式说明都在内**，`{{` / `}}` 是转义、
    不是表达式。反斜杠落在 `{}` **外面**（也就是字面量部分）是合法的 —— 那正是这条
    守卫要放过的写法。
    """
    regions: list[str] = []
    depth = 0
    start = -1
    index = 0
    while index < len(body):
        char = body[index]
        if depth == 0 and char in "{}" and body[index : index + 2] == char * 2:
            index += 2  # `{{` / `}}` 转义
            continue
        if char == "{":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                regions.append(body[start:index])
                start = -1
        index += 1
    return regions


def _fstring_expressions_with_backslash(source: str) -> list[str]:
    """返回所有「表达式里含反斜杠的 f-string 片段」，形如 `line 12: f"…"`。

    **纯文本判据，不解析。** 这样两个解释器上的结果一致，也不受 3.11 那个 position
    缺陷影响（见 `_fstring_literals`）。代价是遇到「表达式里嵌套了含 `}` 的字符串」
    这种写法可能提前收尾 —— 那种情况只会**漏报**，而漏报由 CI 的 3.11 那一跑兜底
    （真违反 PEP 701 的写法在 3.11 上直接 SyntaxError，见 `_scan_sources`）。

    另外它**不抛异常**：解析不了的源码由调用方单独判（那条判据与「有没有反斜杠」是
    两件事，原先混在一个 try 里，会互相遮蔽）。
    """
    offenders: list[str] = []
    for line, literal in _fstring_literals(source):
        if any(_BACKSLASH in region for region in _braced_regions(_fstring_body(literal))):
            offenders.append(f"line {line}: {literal.strip()}")
    return offenders


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
    `test_detection_does_not_depend_on_the_parser`。
    """
    offenders = _fstring_expressions_with_backslash(_defective_source())
    assert offenders, "检测器漏掉了反斜杠在 f-string 表达式里的写法"
    assert "replace(" in offenders[0]


def test_detection_does_not_depend_on_the_parser(monkeypatch):
    """**检测器不能依赖解析成功。**

    这是修那次误报时最要紧的一条性质。缺陷源码在 3.11 上**解析不了**（那正是要检的
    东西），所以任何「先 parse 再取片段」的实现都会在 3.11 上失灵 —— 不是漏报，就是
    误报。这里把 `ast.parse` 替成一个永远拒绝的版本，人为造出「解析不了」：

      * 若检测器还去调解析，`SyntaxError` 会冒出来（原先的行为）；
      * 断言的这三点则要求它**根本不用解析器**，纯文本就能定位到那一行。
    """
    def always_refuse(*args, **kwargs):
        raise SyntaxError("f-string expression part cannot include a backslash")

    monkeypatch.setattr(ast, "parse", always_refuse)
    offenders = _fstring_expressions_with_backslash(_defective_source())

    assert offenders, "解析不了的时候检测器就漏报了"
    assert "replace(" in offenders[0], "报出来的片段不是表达式那一部分"
    assert _BACKSLASH in offenders[0], "片段里的反斜杠丢了"


def test_the_defective_source_is_an_offender_and_never_unparsable():
    """同一个文件不能既被报成「命中」又被报成「解析不了」。

    在 3.11 上，缺陷源码的 `SyntaxError` **就是**反斜杠本身；替身字符一换就能过，
    所以它属于「命中」那一类。若判据写错了，这条在 3.11 上会红、在 3.13 上绿 ——
    又一个只有 CI 才现形的坑。
    """
    offenders, unparsable = _scan_sources({"defect.py": _defective_source()})
    assert offenders and offenders[0].startswith("defect.py:")
    assert not unparsable, "反斜杠违规被误报成了「解析不了」"


# CI 上真误报过的那一行（tests/test_ai_token_format_agreement.py 的断言消息）：
#     f"{value:,} 在三份实现里印得不一样：{got}<反斜杠>n"
# 反斜杠在 `{}` **外面**、属于字面量部分，两个解释器上都是合法的 —— 不许报。
_CI_FALSE_POSITIVE_SOURCE = (
    'msg = f"{value:,} 在三份实现里印得不一样：{got}' + _BACKSLASH + 'n"'
)


def test_detector_does_not_fire_when_the_backslash_is_outside_the_braces():
    """**回归用例**：正是这条把 CI 判红的。

    3.11 给出的 `format_spec` 位置是整条 f-string，于是老实现把字面量里的反斜杠算到
    了表达式头上。文本判据天然没有这个毛病 —— 但要用这条钉住，免得哪天又改回 AST。
    """
    assert _BACKSLASH in _CI_FALSE_POSITIVE_SOURCE
    assert _BACKSLASH not in _braced_regions(_fstring_body(_CI_FALSE_POSITIVE_SOURCE))[0]
    assert _fstring_expressions_with_backslash(_CI_FALSE_POSITIVE_SOURCE) == []


def test_escaped_braces_are_not_expressions():
    """`{{` / `}}` 是转义出来的字面量花括号，里面的反斜杠合法。"""
    source = 'x = f"{{' + _BACKSLASH + 'n}} {v}"'
    assert _BACKSLASH in source
    assert _fstring_expressions_with_backslash(source) == []


def test_a_backslash_in_the_format_spec_counts():
    """`:` 后面的格式说明也在 PEP 701 放宽的范围内，同样要看。"""
    source = 'x = f"{v:' + _BACKSLASH + '">10}"'
    offenders = _fstring_expressions_with_backslash(source)
    assert offenders, "格式说明里的反斜杠漏了"


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

    **两条判据是分开跑的**，原先它们混在同一个 try 里，会互相遮蔽：

      * 「表达式里有没有反斜杠」**不解析**，所以源码再破也照样能报；
      * 「这个文件在当前解释器上解析得了吗」单独判，兜住其它 3.12+ 语法。

    第二判据遇到 `SyntaxError` 时会用替身字符再解析一次：**纯粹的反斜杠违规在 3.11 上
    就是靠这个区分出来的** —— 替身之后能过，说明毛病只有那一个，而它已经被第一判据报
    出来了，不该再算成「解析不了」。替身后仍过不去，才是别的语法问题。
    """
    offenders: list[str] = []
    unparsable: list[str] = []
    for name, source in sources.items():
        offenders.extend(f"{name}:{item}" for item in _fstring_expressions_with_backslash(source))
        try:
            ast.parse(source)
        except SyntaxError:
            try:
                ast.parse(_neutralize_backslashes(source))
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
