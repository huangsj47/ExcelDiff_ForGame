# -*- coding: utf-8 -*-
"""diff 高亮配色必须保持「GitHub 式高对比」的那组取值。

## 契约

新增/删除的底色与文字色是一组**成对**约定（浅色底 + 深色字），改单个颜色
会让对比度掉下来。所以这里把取值本身钉住。

## 为什么断言方式从「子串匹配」改成「解析取值」

原实现直接断言字面量子串：

    assert "background: #dafbe1;" in content

引入设计 token 之后（`background: var(--color-success-bg, #dafbe1)`），
**渲染结果完全没变**，但子串匹配会失败 —— 它断言的是「写法」而不是「配色」，
于是把「改成 token」误报成配色回归；反过来，真正把颜色改错时它未必拦得住。

现在改成把取值解析出来比对，接受两种等价写法：

* 直接字面量 `background: #dafbe1;`
* 带兜底的 token `background: var(--color-success-bg, #dafbe1);`
* 不带兜底的 token `background: var(--t);`，但要求 `--t` 在
  `static/css/style.css` 的 `:root` 里**定义值就是这个颜色**

这样「改写法」不再误报，「改颜色」无论改在字面量侧还是 token 定义侧都会红。
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _root_token_values() -> dict:
    """解析 `static/css/style.css` 里 `:root { --x: #hex; ... }` 的定义值。

    只看第一个 `:root` 块（基础 token 层），避免把媒体查询里的覆盖混进来。
    """
    content = _read("static/css/style.css")
    match = re.search(r":root\s*\{(.*?)\}", content, re.S)
    block = match.group(1) if match else ""
    return {
        name: value.strip().lower()
        for name, value in re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def _assert_background_value(content: str, expected_hex: str, *, path: str) -> None:
    """断言 `expected_hex` 真的被用作 background（字面量或等价 token）。"""
    expected = expected_hex.strip().lower()
    literal = re.compile(
        r"background(?:-color)?\s*:\s*" + re.escape(expected) + r"\s*;", re.I
    )
    if literal.search(content):
        return

    tokens = _root_token_values()
    for name, fallback in re.findall(
        r"background(?:-color)?\s*:\s*var\(\s*(--[A-Za-z0-9-]+)\s*(?:,\s*([^)]+))?\)",
        content,
        re.I,
    ):
        name = name.strip()
        fallback = (fallback or "").strip().lower()
        defined = tokens.get(name)
        # 实际渲染取值：token 已定义时**token 胜出**（CSS 变量语义），
        # 未定义时才用兜底值。所以两侧都必须等于期望色 —— 只认兜底会漏掉
        # 「token 定义被改错」这种改法（验证过：只比对兜底时，把 style.css 里的
        # --color-success-bg 改成 #00ff00 不会变红）。
        if fallback and fallback != expected:
            continue
        if defined is not None and defined != expected:
            continue
        if fallback == expected or defined == expected:
            return

    bg_tokens = {k: v for k, v in tokens.items() if "bg" in k}
    raise AssertionError(
        f"{path} 里找不到 background: {expected_hex} —— 既没有直接写该字面量，"
        f"也没有取值为它的 token。\n"
        f"若这是有意的配色调整，请连同对比度一起评估后再更新本测试；"
        f"若只是把字面量换成了 token，请确认 token 的定义值就是这个颜色"
        f"（当前 :root 里的底色 token：{bg_tokens}）"
    )


def test_weekly_full_diff_inline_highlight_uses_github_like_contrast():
    content = _read("templates/weekly_version_full_diff.html")
    assert "background: #abf2bc;" in content
    assert "background: #ffb8bd;" in content
    assert "color: #24292f;" in content
    assert "box-shadow: inset 0 0 0 1px rgba(27, 31, 36, 0.08);" in content


def test_shared_text_diff_styles_use_github_like_palette():
    path = "static/css/diff-styles.css"
    content = _read(path)
    assert "background: #abf2bc;" in content
    assert "background: #ffb8bd;" in content
    # 这四个已 token 化，按「取值」而不是「写法」断言。
    for expected in ("#dafbe1", "#ffebe9", "#ccf2d4", "#ffd7d5"):
        _assert_background_value(content, expected, path=path)


def test_git_diff_renderer_inline_palette_matches_github_like_contrast():
    content = _read("services/diff_render_helpers.py")
    assert "background: #abf2bc;" in content
    assert "background: #ffb8bd;" in content
    assert "color: #24292f;" in content


class TestPaletteResolverItself:
    """解析器自己也要被钉住 —— 否则它会「永远通过」，把上面那条断言变成空断言。"""

    def test_literal_form_is_accepted(self):
        _assert_background_value("x { background: #dafbe1; }", "#dafbe1", path="synthetic")

    def test_token_with_matching_fallback_is_accepted(self):
        _assert_background_value(
            "x { background: var(--color-success-bg, #dafbe1); }",
            "#dafbe1",
            path="synthetic",
        )

    def test_token_resolved_from_root_is_accepted(self):
        """不带兜底、但 token 定义值正确时也算通过。"""
        tokens = _root_token_values()
        assert tokens, "没能从 style.css 的 :root 解析出任何 token"
        name, value = next(iter(tokens.items()))
        _assert_background_value(f"x {{ background: var({name}); }}", value, path="synthetic")

    def test_wrong_colour_is_rejected(self):
        import pytest

        with pytest.raises(AssertionError):
            _assert_background_value("x { background: #123456; }", "#dafbe1", path="synthetic")

    def test_token_with_wrong_value_is_rejected(self):
        """token 名字写对、但定义值不对时，必须红。"""
        import pytest

        with pytest.raises(AssertionError):
            _assert_background_value(
                "x { background: var(--color-success-bg); }", "#000000", path="synthetic"
            )
