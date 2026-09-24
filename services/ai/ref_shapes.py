# -*- coding: utf-8 -*-
"""口径 ③：**依据的形状校验** —— 这条依据能不能照着它定位到东西。

## 为什么单独一个模块

它原先住在 `verdict.py`（复核裁决）里，但它判的不是「裁决对不对」，而是「一句话像不像
一个坐标」。两个读者需要它、而且判据必须逐字一致：

* `verdict._with_ref_shapes`：复核给出的 `evidence_refs` 有没有一条能照着去核；
* `claims.claim_review_of`：一条**原子断言**自己带的依据能不能核（全不可定位 ⇒ 这条断言
  的状态是「证据读不到」，而不是「已证实」）。

两处各写一份「像不像路径」的正则，迟早会有一处放过一句散文。

## 两处宁严勿宽的取舍

两侧的错法不对称：**判成不可定位**只影响「能否维持 `very_high`」或「这条断言算不算证实」
（结论留着、报告里写明理由）；**判成可定位**却会把一句散文当成证据，而那正是这一层
要拦下的东西。所以定位符里带空白一律不成形，路径里带空白同样不成形。

反过来说，**只有「有个定位符、但定位符不成形」才判死**：裸路径是合法形态，不是残缺形态。
"""

from __future__ import annotations

import re
from typing import Any

#: 定位符：行号或行号范围。
_REF_LINE_RE = re.compile(r"^\d{1,7}(?:\s*[-~]\s*\d{1,7})?$")
#: 定位符：工作表名（不许含空白、`!`、`:`、`@`）。
_REF_SHEET_RE = re.compile(r"^[^\s!:@]{1,64}$")
#: 定位符：单元格或区间（`Sheet1!A1`、`Sheet1!A1:C5`）。
_REF_CELL_RE = re.compile(r"^[^\s!:@]{1,64}![A-Za-z]{1,3}\d{1,7}(?::[A-Za-z]{1,3}\d{1,7})?$")
#: unified diff hunk：`a.lua @@ -12,3 +14,5 @@` 或兼容旧模型的 `a.lua:diff@@ -12,3 +14,5 @@`；
#: 必须同时有完整 old/new 坐标。
_REF_HUNK_RE = re.compile(
    r"^(?P<path>\S+?)(?:\s+|:diff)"
    r"@@ -\d{1,7}(?:,\d{1,7})? \+\d{1,7}(?:,\d{1,7})? @@(?: .*)?$"
)
#: 配表类扩展名。sheet / 单元格这两种定位符**只对它们成立**：`.lua` 文件没有 sheet，
#: 认下来就等于把一句自由文本当成坐标。
_SHEET_SUFFIXES = (".xlsx", ".xlsm", ".xlsb", ".xls", ".csv")


def is_locatable_ref(ref: str) -> bool:
    """这条依据能不能**照着它定位到东西**（口径 ③）。

    认可三组形态，其余一律不成形：

    * **裸路径**（`build/lua/CfgItem.lua`）：**能去查**的快照坐标 —— 平台自己的 diff
      载荷就是这么标的（`file_diff <commit> <path>`），按提交取一份快照就能核。判据复用
      `_looks_like_path`：无空白 / 控制字符、无 `@@`，且**最后一段带扩展名**。
      刻意**没有**放宽到「有分隔符就算」（`code/qz_server/src/tms/module` 这种只有目录、
      没有文件名的写法）：本仓库里可定位的东西都带扩展名（`config/*.xlsx`、
      `code/**/*.lua`），一个到目录为止的写法更可能是被截断的路径或一句概述，
      而不是一个坐标；而这一侧的错法比另一侧贵（见模块 docstring 的「宁严勿宽」）；
    * **路径 + 定位符**，定位符只认下表四种（`文件:行` / `文件:行范围` / `文件:sheet` /
      `文件:sheet!单元格`）：

      | 定位符 | 形态 | 例 |
      |---|---|---|
      | 行 / 行范围 | `^\\d{1,7}([-~]\\d{1,7})?$` | `61`、`61-66` |
      | sheet | 无空白 / `!` / `:` / `@` 的名字 | `Sheet1` |
      | 单元格 / 区间 | `sheet!A1` / `sheet!A1:C5` | `Sheet1!B2` |

      后两种**只对配表类扩展名成立**：`.lua` 没有 sheet，`Sheet1` 挂在它后面是自由文本，
      不是坐标。
    * **路径 + 完整 unified diff hunk**：同时给出 old/new 起始行和可选长度，例如
      `a.lua @@ -61,66 +66,26 @@`。兼容模型曾输出的 `a.lua:diff@@ ... @@`，
      但不接受少一侧坐标、缺路径或混入 hunk 语法的散文。
    """
    text = _ref_body(ref)
    hunk = _REF_HUNK_RE.fullmatch(text)
    if hunk:
        return _looks_like_path(hunk.group("path"))
    path, locator = _split_ref(text)
    if not locator:
        # 裸路径（或整个字符串里就没有 `:`）。判据与切分那一侧同一个函数，不另写一份。
        return _looks_like_path(text)
    if not path:
        return False
    if "," in locator or "@" in locator:
        return False
    if locator.isdigit() or _REF_LINE_RE.match(locator):
        return True
    if path.lower().endswith(_SHEET_SUFFIXES):
        return bool(_REF_SHEET_RE.match(locator) or _REF_CELL_RE.match(locator))
    return False


def _ref_body(value: Any) -> str:
    """依据的裸文本：剥掉空白与模型常加的那几对包裹符号（反引号、引号、括号）。"""
    return str(value or "").strip().strip("`'\"“”‘’（）()[]<>").strip()


def _split_ref(ref: str) -> tuple[str, str]:
    """把一条依据拆成 `(路径, 定位符)`（拆不出就是两个空串）。

    切点**从左往右**找第一个「左边像个路径」的 `:`，而不是从右边切：

    * `C:/work/a.lua:61` —— 盘符那个 `:` 的左边是 `C`，最后一段没有点，跳过；
    * `config/x.xlsx:Sheet1!A1:C5` —— 单元格区间**自带一个 `:`**，从右边切会把路径切成
      `config/x.xlsx:Sheet1!A1`（一个不存在的文件），反过来切才对；
    标准 hunk 在进入这里前已经由 `is_locatable_ref` 单独识别；不完整的 `diff@@` 残片仍会
    保留成 `(路径, 定位符)`，并由形状校验拒绝。

    路径里含空白（模型写了一句「见 xxx.lua 第 61 行」）或含 `@@` 一律判不成形：那是散文，
    不是坐标。
    """
    text = _ref_body(ref)
    for position, char in enumerate(text):
        if char != ":":
            continue
        path, locator = text[:position].strip(), text[position + 1 :].strip()
        if _looks_like_path(path) and locator:
            return path, locator
    return "", ""


def _looks_like_path(value: str) -> bool:
    """这一段像不像一个**文件路径**（不是「有斜杠」就行，见 `_split_ref`）。"""
    if not value or any(char.isspace() for char in value) or "@@" in value:
        return False
    return "." in value.rsplit("/", 1)[-1]
