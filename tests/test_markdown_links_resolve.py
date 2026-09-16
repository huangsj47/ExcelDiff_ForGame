"""Markdown 相对链接必须能解析到真实文件。

## 为什么需要这条守卫

本仓库的文档之间以及与源码之间用相对链接互相引用（README → 两份说明文档，
架构说明 → app.py / services/... / scripts/...）。这类链接的失效是**静默**的：
点开才发现 404，CI 不会报，pytest 也不会报 —— 仓库历史上就出过一次
（`代码架构说明.md` 里 7 处指向已搬走的绝对路径，直到后来专门修了一轮）。

移动文档目录正是最容易制造这种失效的操作，所以这条守卫要先于搬家落地。

## 实现要点

* **必须用 `git ls-files -z`**。Windows 上 `core.quotepath` 默认开启，
  `git ls-files` 会把非 ASCII 路径转义成 `"scripts/AGENT\\345\\217\\221..."` 这种
  带引号的形式，直接 `Path(...)` 会 `OSError: [Errno 22]`（已实测踩到）。
  `-z` 走 NUL 分隔的原始字节形式，不受 quotepath 影响。
* 只检查**被跟踪**的文件：`findings.md` 这类被 gitignore 的私人笔记里的链接
  不计入（它们的引用对象本来就可能是临时的）。
* 按「该 md 文件所在目录」解析相对路径，而不是仓库根 —— 文档移进 `docs/` 后
  `./x.md` 与 `../x.md` 的语义不同，这正是要守住的东西。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# `[文字](目标)`；目标是 `(...)` 里到第一个空白或右括号为止的部分。
_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")

# 不参与解析的目标形态：外链、纯锚点、mailto。
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "//")


def _tracked_markdown() -> list[str]:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("不是 git 工作区，无法枚举被跟踪的文档")
    proc = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        pytest.skip(f"git ls-files 失败：{proc.stderr.strip()}")
    return [name for name in proc.stdout.split("\0") if name]


def _iter_relative_links():
    """产出 (md 相对路径, 行号, 原始目标)。"""
    for rel in _tracked_markdown():
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for match in _LINK_RE.finditer(source):
            raw = match.group(1).strip()
            if not raw or raw.startswith(_EXTERNAL_PREFIXES):
                continue
            line = source[: match.start()].count("\n") + 1
            yield rel, line, raw


def test_the_link_scanner_actually_finds_links():
    """反向自检：正则/枚举一旦失效，下面那条断言会变成恒真。

    这不是假想的风险 —— 第一版用 `git ls-files *.md` 时，中文文件名被
    quotepath 转义，枚举出来的路径全都读不开。
    """
    found = list(_iter_relative_links())
    assert len(found) >= 10, (
        f"只扫到 {len(found)} 条相对链接，远少于预期 —— "
        "链接解析或 git 枚举出了问题，后面的断言已经失去意义"
    )
    assert {rel for rel, _, _ in found}, "一条链接都没扫到"


def test_every_relative_link_resolves():
    broken = []
    for rel, line, raw in _iter_relative_links():
        target = raw.split("#", 1)[0].strip()
        if not target:
            continue  # 纯锚点（`x.md#sec` 形式在上面已被拆掉）
        resolved = (REPO_ROOT / rel).parent / target
        if not resolved.exists():
            broken.append(f"{rel}:{line}  ->  {raw}")
    assert not broken, (
        "以下相对链接指向不存在的路径（移动文档/源码后最容易出现）：\n  "
        + "\n  ".join(broken)
    )
