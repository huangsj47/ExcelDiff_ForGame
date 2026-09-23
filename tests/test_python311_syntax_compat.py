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

## 第二类缺陷：表达式里复用**外层定界符**（同一份真值表定的）

反斜杠之外，3.11 还拒另一种写法：f-string 的定界符出现在它自己的表达式里。

    latest = client.get(f"/ai-analysis/weekly/{group["cfg_id"]}/latest")

3.11 上这是 `SyntaxError: f-string: unmatched '['`（`tests/test_ai_task_e8_paths.py` 第
485 行，2026-09-23 挂的 CI），3.13 上完全合法。机理与反斜杠同源：3.11 及以前整条 `f"…"`
是**一个** STRING token，表达式部分交给老解析器单独扫，它一见构成外层定界符的引号就把
字符串提前收尾；3.12（PEP 701）换成真正的词法器，同种引号可以复用。

判据的形状是拿 `py -3.11`（3.11.9）与 3.13 各跑一遍同一组片段**实测**出来的，三条要点：

  * **比的是定界符那串字符，不是「内层引号 == 外层引号」**：外层是三引号时，表达式里
    出现单个 `"` 是**合法**的（`f<三引号>{d["k"]}<三引号>` 在 3.11 上过），连续三个
    `"` 才拒；
  * **只在 `{…}` 里找**：反斜杠转义出来的引号落在外层**字面量**部分
    （`f"a <反斜杠>" b {y}"`）合法，扫整条字面量会在这里误报；
  * 定界符**以任何方式**进入表达式都拒，不限于它是个字符串的引号 —— `f"{d['a"b']}"`
    里那个 `"` 只是另一个字符串的**内容**，3.11 照样拒。

（上面写 `<三引号>` 而不是直接写出那三个引号：本文件的 docstring 自己就是三引号包的，
写出来会把它**提前收尾**成语法错误。与反斜杠用 `<反斜杠>` 是两种不同理由的同一个坑。）

所以复用 `_braced_regions`（它已经在算「哪些字符属于表达式与格式说明」）。与反斜杠那条
并列成两条全仓守卫，而不是合并：两条的**修法**不同，报出来的原因也不该混在一句话里。

**但这条判据是 3.12+ 专用的，与反斜杠那条不同。** 3.11 的 **tokenizer**（比解析器更早）
就会在复用引号处把字面量**截断**：实测 `_fstring_literals` 在 3.11 上对
`f"/ai-analysis/weekly/{group["cfg_id"]}/latest"` 返回的是
`f"/ai-analysis/weekly/{group["` —— `{` 没闭合，`_braced_regions` 于是拿到空区域。所以它的
自检在 3.11 上要么失败、要么**空绿**（两边都返回空，等于没验），一律 `skipif` 掉；3.11 那
一边的覆盖面由 `_scan_sources` 的**解析判据**兜住（这类源码在 3.11 上本来就是 `SyntaxError`），
全仓那条守卫在 3.11 上断言的就是这个等价性。**覆盖面没有缺口，只是换了一条判据兜。**

**覆盖不到的**：表达式里的字符串字面量含 `}` 时 `_braced_regions` 会提前收尾，那处之后的
定界符复用会漏报（只漏报、不误报）。PEP 701 还有**第三处**放宽 —— 表达式里放 `#` 注释 3.11
同样拒 —— 那由并列的第三条判据 `_fstring_expressions_containing_a_comment` 管（同样是
3.12+ 专用、3.11 由解析判据兜）。

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
import sys
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


def _tokens_leniently(source: str) -> list[tokenize.TokenInfo]:
    """tokenize 一遍源码；**词法不完整时返回已经拿到的部分**，不往外抛。

    下面几条判据都靠它：一个文件可能既有多行字符串没闭合、又有我们要报的东西，
    不能因为前者的 `TokenError` 就把后者一起咽掉。真·解析不了的文件由
    `_scan_sources` 的解析判据单独报（那条判据与这里互不遮蔽）。
    """
    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            tokens.append(token)
    except (tokenize.TokenError, IndentationError):
        # 词法都不完整（多行字符串没闭合等）：交给 `_scan_sources` 的解析判据去报。
        pass
    return tokens


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

    offsets = _line_offsets(source)
    pending: list[tuple[int, int]] = []

    def slice_from(start: tuple[int, int], end: tuple[int, int]) -> str:
        return source[offsets[start[0] - 1] + start[1] : offsets[end[0] - 1] + end[1]]

    for token in _tokens_leniently(source):
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


def _fstring_delimiter(literal: str) -> str:
    """f-string 的定界符：1 个或 3 个引号（`f"` → `"`、`rf'''` → `'''`）。

    第一个引号字符之前的一律当前缀（`f` / `r` / 大小写及其组合）。
    """
    quote_at = next((i for i, ch in enumerate(literal) if ch in "\"'"), -1)
    if quote_at < 0:
        return ""
    quote = literal[quote_at]
    return quote * 3 if literal[quote_at : quote_at + 3] == quote * 3 else quote


def _fstring_expressions_reusing_the_outer_delimiter(source: str) -> list[str]:
    """返回所有「表达式里出现外层定界符」的 f-string 片段，形如 `line 12: f"…"`。

    ## 为什么要这条：3.11 不许在表达式里复用外层定界符

    3.11 及以前整条 `f"…"` 是**一个** STRING token，表达式部分交给老解析器单独扫一遍；
    它一见构成外层定界符的引号就把字符串提前收尾，于是：

        f"/ai-analysis/weekly/{group["cfg_id"]}/latest"   # 3.11: f-string: unmatched '['
        f"{f"{y}"}"                                        # 3.11: f-string: expecting '}'

    3.12（PEP 701）换成真正的词法器，同种引号可以复用，所以这两种写法在本机（3.13）全绿。
    **本仓真挂过一次**（`tests/test_ai_task_e8_paths.py` 第 485 行，2026-09-23）。

    ## 判据的形状是实测出来的，不是想出来的

    用 `py -3.11`（3.11.9）与 3.13 各跑一遍同一组片段，得到三条要点：

      * **比的是定界符那串字符，不是「内层引号 == 外层引号」**：外层是三引号时表达式里的
        单个 `"` 是**合法**的（`f<三引号>{d["k"]}<三引号>` 在 3.11 上过），连续三个 `"` 才拒；
      * **只在 `{…}` 里找**：转义引号落在外层字面量部分（`f"a <反斜杠>" b {y}"`）合法，
        扫整条字面量会误报；
      * 定界符**以任何方式**出现在表达式里都拒，不限于它是个字符串的引号 ——
        `f"{d['a"b']}"` 里那个 `"` 只是另一个字符串的**内容**，3.11 照样拒。

    所以复用 `_braced_regions`（它已经在算「哪些字符属于表达式与格式说明」）而不是自己
    数引号。

    ## 只在 3.12+ 上有效，3.11 上天然瞎

    3.11 的 **tokenizer**（早于解析器）就会在复用引号处把字面量截断 —— 实测
    `_fstring_literals` 对它返回的是 `f"/ai-analysis/weekly/{group["`：`{` 没闭合，
    `_braced_regions` 拿到空区域。所以它的自检全部 `skipif(3.12)`（在 3.11 上跑要么失败、
    要么空绿），3.11 那一边的覆盖面由 `_scan_sources` 的**解析判据**兜住 —— 这类源码在
    3.11 上本来就是 `SyntaxError`。**缺口只在「判据」上，不在「覆盖面」上。**

    ## 已知覆盖不到（都在 docstring 里说清，别让它们变成「以为守住了」）

      * **表达式里的字符串字面量含 `}`**（实测确认，只漏报、不误报）：`_braced_regions`
        会提前收尾（它自己的 docstring 写了），那处**之后**的定界符复用就扫不到了。
        漏网例子：`f"{d['}'] + d["k"]}"`（3.11 报 `f-string: unmatched '['`、3.13 过）。
        本仓目前没有这种写法 —— 出现了就得先修 `_braced_regions` 的收尾判据。

    曾经也报不到「表达式里带 `#` 注释」，现由**另一条并列判据**
    `_fstring_expressions_containing_a_comment` 管（同一条纪律：先拿真 3.11 与 3.13
    各编一遍定方向，再写断言）。
    """
    offenders: list[str] = []
    for line, literal in _fstring_literals(source):
        delimiter = _fstring_delimiter(literal)
        if not delimiter:
            continue
        if any(delimiter in region for region in _braced_regions(_fstring_body(literal))):
            offenders.append(f"line {line}: {literal.strip()}")
    return offenders


def _fstring_expressions_containing_a_comment(source: str) -> list[str]:
    """返回所有「表达式里带 `#` 注释」的 f-string 片段，形如 `line 12: # c`。

    ## 为什么要这条：3.11 不许 f-string 的表达式里有 `#`

    PEP 701 放宽的第三处。3.11 及以前，f-string 的表达式部分交给老解析器单独扫一遍，
    它一见到 `#` 就报 `f-string expression part cannot include '#'`（本仓 2026-09-23
    那一轮里也验过）。3.12+ 由真正的词法器处理，表达式里可以有注释（前提是那条注释
    得有换行收尾，所以通常出现在三引号 f-string 里）。实测（`py -3.11` vs 3.13）：

        f<三引号>{
            d['k']  # c        ← 3.11 拒 / 3.13 过
        }<三引号>

    两种外层引号（`<三引号>` 与 `'''`）都拒；注释出现在**嵌套 f-string 的表达式**里也拒。
    （`<三引号>` 是本文件的占位写法：docstring 自己就是三引号包的，写出来会把它提前收尾。）

    ## 判据：数「在 f-string 里面的 COMMENT token」，不做文本搜索

    3.11 拒的**不是**那个字符本身，而是「表达式里的注释」。实测这几条 3.11 都**合法**，
    文本搜索 `#` 会全部误报：

        f"{d['#']}"              # `#` 在表达式的字符串字面量里
        f"a # b {y}"             # `#` 在 f-string 的字面量文本里
        f"{f'{a} # b'}"          # `#` 在嵌套 f-string 的字面量文本里

    这三处的 `#` 都不是 COMMENT token（分别落在 STRING / FSTRING_MIDDLE 里），所以
    只要认 token 类型就天然不误报。而且 3.12+ 的 COMMENT token 只可能出现在表达式里 ——
    字面量文本里的 `#` 是 FSTRING_MIDDLE 的一部分 —— 于是判据简化成：

        **一个 COMMENT token，只要它落在某条 f-string 的 FSTRING_START 与配对的
        FSTRING_END 之间，就是 3.11 会拒的写法。**

    不需要按 f-string 分组，用**嵌套深度计数器**就够了（`FSTRING_START` +1、
    `FSTRING_END` -1，深度 > 0 时见到的 COMMENT 即违规）：COMMENT 不可能出现在
    FSTRING_END 之后又算进同一条 f-string 里。

    ## 与引号复用那条同命运：只在 3.12+ 上有效

    3.11 上整条 f-string 是一个 STRING token、也没有 `FSTRING_START`，深度恒为 0，
    这里返回空 —— 与 `_fstring_expressions_reusing_the_outer_delimiter` 一样是
    **3.12+ 专用**，3.11 那一边由 `_scan_sources` 的解析判据兜住。
    """
    offenders: list[str] = []
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    if fstring_start is None or fstring_end is None:
        return offenders

    depth = 0
    for token in _tokens_leniently(source):
        if token.type == fstring_start:
            depth += 1
        elif token.type == fstring_end:
            depth = max(0, depth - 1)
        elif depth > 0 and token.type == tokenize.COMMENT:
            offenders.append(f"line {token.start[0]}: {token.string.strip()}")
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
# 自检：引号复用（定界符）检测器
#
# **这些用例只在 3.12+ 上跑**（`_needs_pep701_tokenizer`）：3.11 的 tokenizer 会在复用
# 引号处把字面量截断，检测器在那里天然瞎 —— 硬跑的话要么失败（`fires_on_the_defect…`），
# 要么**空绿**（`does_not_fire_on_the_legal_spellings` 在 3.11 上两边都返回空，等于没验，
# 那种「绿」比红更坏）。3.11 上这一类由 `_scan_sources` 的解析判据兜住，见全仓守卫里
# 那条版本分支与 `_fstring_expressions_reusing_the_outer_delimiter` 的 docstring。
# --------------------------------------------------------------------------

_needs_pep701_tokenizer = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="3.11 的 tokenizer 会在复用引号处截断 f-string，这条判据天然瞎；那一版由解析判据兜住",
)

# 本仓 2026-09-23 真挂过 CI 的那一行（tests/test_ai_task_e8_paths.py:485）的写法。
# 3.11 报 `f-string: unmatched '['`，3.13 合法。
_QUOTE_REUSE_DEFECT_SOURCE = 'latest = client.get(f"/ai-analysis/weekly/{group["cfg_id"]}/latest")'

# 3.11 上**合法**的写法（逐条都拿 `py -3.11` 实测过）。检测器一个都不许报。
_QUOTE_REUSE_LEGAL_SOURCES = {
    # 换一种引号 —— 3.11 下的推荐写法，也是本仓 485 行改成的样子
    "other_quote": 'x = f"{d[\'k\']}"',
    # 外层三引号时，表达式里的**单个** `"` 合法（比的是定界符那串字符，不是引号字符）
    "triple_outer_single_inner": 'x = f"""{d["k"]}"""',
    # 转义引号落在外层**字面量**部分，3.11 合法 —— 扫整条字面量会在这里误报
    "escaped_in_literal_part": 'x = f"a \\" b {y}"',
    # 撇号出现在字面量部分（不是表达式），3.11 合法
    "apostrophe_in_literal_part": "x = f\"it's {y}\"",
    # 显式拼接而不是复用引号，合法
    "adjacent_other_quote": "x = f\"{'a' 'b'}\"",
    # 反斜杠在表达式里由**另一条**判据管，这条不许重复报
    "backslash_is_the_other_criterion": 'x = f"{p.replace(chr(92), chr(47))}"',
}


@_needs_pep701_tokenizer
def test_the_quote_reuse_source_really_has_the_delimiter_inside_the_expression():
    """先钉住前提：这条合成源码的定界符确实落在 `{…}` 里面。

    否则「检测器能命中」可能只是因为源码构造错了，守卫就成了空转。
    """
    literal = _fstring_literals(_QUOTE_REUSE_DEFECT_SOURCE)[0][1]
    assert _fstring_delimiter(literal) == '"', literal
    assert any('"' in region for region in _braced_regions(_fstring_body(literal)))


@_needs_pep701_tokenizer
def test_quote_reuse_detector_fires_on_the_defect_it_was_written_for():
    """核心自检：检测器必须能命中本仓真挂过的那一行。

    没有这条，将来 `_fstring_expressions_reusing_the_outer_delimiter` 被改坏（比如返回
    空列表）时，全仓守卫会**照样全绿**，守卫变成装饰品。
    """
    offenders = _fstring_expressions_reusing_the_outer_delimiter(_QUOTE_REUSE_DEFECT_SOURCE)
    assert offenders, "检测器漏掉了「表达式里复用外层定界符」的写法"
    assert offenders[0].startswith("line 1: "), offenders
    assert 'cfg_id' in offenders[0], "报出来的片段不是那一行"


@_needs_pep701_tokenizer
def test_quote_reuse_detector_does_not_fire_on_the_legal_spellings():
    """反证：3.11 上合法的写法一条都不许报（含那条最容易误报的转义引号）。"""
    for name, source in _QUOTE_REUSE_LEGAL_SOURCES.items():
        assert _fstring_expressions_reusing_the_outer_delimiter(source) == [], name


@_needs_pep701_tokenizer
def test_quote_reuse_counts_the_delimiter_not_the_quote_character():
    """**真值表里最刺眼的那一对**，钉住「比定界符」而不是「比引号字符」。

    外层是三引号时，表达式里嵌一个 `f"…"` 是**合法**的（3.11 过）；嵌一个
    `f<三引号>…<三引号>` 才拒。判据若写错成「内层引号 == 外层引号」，前半条就会误报。
    """
    assert _fstring_expressions_reusing_the_outer_delimiter('x = f"""{f"{y}"}"""') == []
    assert _fstring_expressions_reusing_the_outer_delimiter('x = f"""{f"""{y}"""}"""')


@_needs_pep701_tokenizer
def test_quote_reuse_is_caught_however_the_delimiter_gets_in():
    """定界符**以任何方式**进入表达式都算 —— 不限于它是个字符串的引号。"""
    # 嵌套 f-string 复用同种引号（PEP 701 的招牌例子）
    assert _fstring_expressions_reusing_the_outer_delimiter('x = f"{f"{y}"}"')
    # 格式说明里的嵌套替换字段
    assert _fstring_expressions_reusing_the_outer_delimiter('x = f"{v:{d["k"]}}"')
    # `"` 只是表达式里**另一个字符串的内容**（3.11 报 unterminated string literal）
    assert _fstring_expressions_reusing_the_outer_delimiter('x = f"{d[\'a"b\']}"')


@_needs_pep701_tokenizer
def test_quote_reuse_detection_does_not_depend_on_the_parser(monkeypatch):
    """同 `test_detection_does_not_depend_on_the_parser`：这条判据也不许调解析器。

    3.11 上缺陷源码**解析不了**，任何「先 parse 再取片段」的实现都会在那里失灵。
    """
    def always_refuse(*args, **kwargs):
        raise SyntaxError("f-string: unmatched '['")

    monkeypatch.setattr(ast, "parse", always_refuse)
    offenders = _fstring_expressions_reusing_the_outer_delimiter(_QUOTE_REUSE_DEFECT_SOURCE)

    assert offenders, "解析不了的时候检测器就漏报了"
    assert 'cfg_id' in offenders[0], "报出来的片段不是那一行"


# --------------------------------------------------------------------------
# 自检：表达式里的 `#` 注释（第三条判据，同样只在 3.12+ 上跑）
# --------------------------------------------------------------------------

# 3.11 报 `f-string expression part cannot include '#'`，3.13 合法。四条都实测过。
_COMMENT_IN_EXPRESSION_DEFECT_SOURCES = {
    # 基本形态：三引号 f-string，表达式里一条注释
    "single_level": 'x = f"""{\n    d[\'k\']  # c\n}"""',
    # 外层单引号三引号也拒
    "triple_single_outer": "x = f'''{\n    d['k']  # c\n}'''",
    # 注释在**嵌套 f-string 的**表达式里（3.11 报 unterminated string literal）
    "nested_fstring": 'x = f"{f\'\'\'{\n    a  # c\n}\'\'\'}"',
    # 注释把后面的 `}` 一起吞掉，仍然算表达式里的注释
    "comment_swallows_a_brace": 'x = f"""{\n    a  # c }\n}"""',
}

# 3.11 上**合法**的写法（逐条实测过）。文本搜索 `#` 会全部误报，认 token 类型则不会。
_COMMENT_LEGAL_SOURCES = {
    # `#` 在表达式的**字符串字面量**里 —— 是 STRING token 的内容，不是注释
    "hash_inside_a_string": 'x = f"{d[\'#\']}"',
    "hash_inside_a_string_multiline": 'x = f"""{\n    d[\'#\']\n}"""',
    # `#` 在 f-string 的**字面量文本**里 —— 是 FSTRING_MIDDLE 的一部分
    "hash_in_literal_text": 'x = f"a # b {y}"',
    "hash_in_literal_text_multiline": 'x = f"""a # b\n{y}"""',
    "hash_in_nested_literal_text": 'x = f"{f\'{a} # b\'}"',
    # f-string **外面**的普通注释：与本判据无关
    "plain_comment_outside": "x = 1  # c",
}


@_needs_pep701_tokenizer
def test_the_comment_defect_source_really_has_a_comment_inside_the_fstring():
    """先钉住前提：那个 `#` 确实是一个**落在 f-string 里面**的 COMMENT token。

    只是「源码里有 `#`、也有 f-string」不够 —— 那样连 `f"a # b {y}"` 都算违规，
    判据就成了文本搜索。要钉的是顺序：COMMENT 出现在 FSTRING_START **之后**，
    且两者之间没有 FSTRING_END（也就是它在 f-string 里面，而不是在后面）。
    """
    tokens = _tokens_leniently(_COMMENT_IN_EXPRESSION_DEFECT_SOURCES["single_level"])
    start_at = next(i for i, t in enumerate(tokens) if t.type == tokenize.FSTRING_START)
    comment_at = next(i for i, t in enumerate(tokens) if t.type == tokenize.COMMENT)

    assert start_at < comment_at, "COMMENT 出现在 f-string 开始之前"
    assert not any(t.type == tokenize.FSTRING_END for t in tokens[start_at:comment_at]), (
        "COMMENT 落在 f-string 之外，这条用例就验错东西了"
    )


@_needs_pep701_tokenizer
def test_comment_detector_fires_on_the_defect_it_was_written_for():
    """核心自检：四种形态必须全中（含嵌套 f-string 与「注释吞掉 `}`」）。"""
    for name, source in _COMMENT_IN_EXPRESSION_DEFECT_SOURCES.items():
        offenders = _fstring_expressions_containing_a_comment(source)
        assert offenders, f"{name}：检测器漏掉了「表达式里的 `#` 注释」"
        assert offenders[0].startswith("line 2: "), (name, offenders)
        assert "# c" in offenders[0], (name, offenders)


@_needs_pep701_tokenizer
def test_comment_detector_does_not_fire_on_the_legal_spellings():
    """反证：`#` 在字符串里、在字面量文本里、在 f-string 外面，3.11 都合法。

    这条是本判据最容易写错的方向 —— 只要退化成「搜 `#` 字符」，这一整组会立刻变红。
    """
    for name, source in _COMMENT_LEGAL_SOURCES.items():
        assert _fstring_expressions_containing_a_comment(source) == [], name


@_needs_pep701_tokenizer
def test_comment_detection_does_not_depend_on_the_parser(monkeypatch):
    """同前两条：这条判据也不许调解析器（3.11 上缺陷源码是解析不了的）。"""
    def always_refuse(*args, **kwargs):
        raise SyntaxError("f-string expression part cannot include '#'")

    monkeypatch.setattr(ast, "parse", always_refuse)
    offenders = _fstring_expressions_containing_a_comment(
        _COMMENT_IN_EXPRESSION_DEFECT_SOURCES["single_level"]
    )

    assert offenders, "解析不了的时候检测器就漏报了"
    assert "# c" in offenders[0], offenders


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


def _assert_the_parse_criterion_covers_it_on_311(source: str) -> None:
    """3.12+ 专用的那两条判据在 3.11 上是瞎的，那里改钉「有人兜」。

    见 `_fstring_expressions_reusing_the_outer_delimiter` 与
    `_fstring_expressions_containing_a_comment` 的 docstring：这类源码在 3.11 上
    **本来就过不了 `ast.parse`**，所以 3.11 边的验收标准不是「新判据报得出来」，
    而是「解析判据确实报得出来」。

    不写成 `pytest.skip`：跳过等于**没人守**，而这个断言至少能证明覆盖面还在；
    它本身也是活的（把入参换成合法源码就会塌，见 `branchlive` 那类验证）。
    """
    _, unparsable = _scan_sources({"defect.py": source})
    assert unparsable, "3.11 上这类源码必须被解析判据报出来，否则这一类就没人守了"


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


def test_no_fstring_reuses_its_own_delimiter_inside_the_expression():
    """全仓守卫：任何被跟踪的 .py 都不得在 f-string 表达式里复用外层定界符。

    与反斜杠那条并列而不是合并：两条判据的**修法**不同（一个改用 `chr(92)` / `as_posix()`，
    一个把内层引号换一种），报出来的原因也不该混在同一句话里。

    **3.11 上换一种断言**：这条判据在 3.11 上天然瞎（3.11 的 tokenizer 会在复用引号处截断
    字面量），全仓扫过去只会得到一个**没有意义的空绿** —— 那种绿比红更坏。而 3.11 上这类
    源码本来就过不了 `ast.parse`，所以那里改钉**等价性**：拿真源码确认它确实被判成「解析
    不了」。覆盖面没有缺口，只是换了一条判据兜（见本函数 docstring 里那条版本分支的说明）。
    """
    if sys.version_info < (3, 12):
        _assert_the_parse_criterion_covers_it_on_311(_QUOTE_REUSE_DEFECT_SOURCE)
        return

    offenders: list[str] = []
    for rel in _tracked_python_files():
        offenders.extend(
            f"{rel}:{item}"
            for item in _fstring_expressions_reusing_the_outer_delimiter(_read_source(rel))
        )

    assert not offenders, (
        "以下 f-string 的表达式里出现了外层定界符。这是 Python 3.12+（PEP 701）才允许的"
        "写法，CI 的 3.11 会直接 SyntaxError（`f-string: unmatched '['`），而本机 3.13 "
        "全绿 —— 正是本仓 2026-09-23 挂 CI 的那一类。把内层字符串换成另一种引号"
        "（外层是双引号就用单引号）：\n  " + "\n  ".join(offenders)
    )


def test_no_fstring_expression_contains_a_comment():
    """全仓守卫：任何被跟踪的 .py 都不得在 f-string 的表达式里放 `#` 注释。

    第三条并列判据（PEP 701 的另一处放宽）。**认 token 类型、不搜 `#` 字符** ——
    `f"{d['#']}"`、`f"a # b {y}"` 在 3.11 上都是合法的，搜字符会误报一片（自检里钉着）。

    3.11 上这条判据同样天然瞎（整条 f-string 是一个 STRING token，认不出里面的
    COMMENT），那里换钉解析判据兜不兜得住 —— 与引号复用那条同款处理。
    """
    if sys.version_info < (3, 12):
        _assert_the_parse_criterion_covers_it_on_311(
            _COMMENT_IN_EXPRESSION_DEFECT_SOURCES["single_level"]
        )
        return

    offenders: list[str] = []
    for rel in _tracked_python_files():
        offenders.extend(
            f"{rel}:{item}"
            for item in _fstring_expressions_containing_a_comment(_read_source(rel))
        )

    assert not offenders, (
        "以下 f-string 的表达式里出现了 `#` 注释。这是 Python 3.12+（PEP 701）才允许的"
        "写法，CI 的 3.11 会直接 SyntaxError（`f-string expression part cannot include "
        "'#'`），而本机 3.13 全绿。把注释挪到 f-string 外面，或先算好再插值：\n  "
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
