# -*- coding: utf-8 -*-
"""帮助页的字号：**同一角色的文字，在各分节里必须一样大。**

## 这一条是怎么来的

用户看一眼就发现了：「AI 分析」那一节的字比别的分节大一圈。根因不是谁把字号写错了，
而是**没写** —— 帮助页的排版规则只挂在 `.step-content`（步骤区）与几个具名 class 上，
而 AI 那一节不是一串操作步骤（套 `.step-list` 会凭空多出序号与缩进），它的段落与小标题
直接放在 `.section-body` 下，于是长期在吃默认值：

| 元素 | 改前（默认值） | 别的分节 | 改后 |
|---|---|---|---|
| 正文 `<p>` | 16px / 行高 24px | 14.72px / 行高 25.024px | 与「别的分节」一栏相同 |
| 小标题 `<h4>` | 24px / 500 | 17.6px / 700 | 与「别的分节」一栏相同 |

差得不算多，但「同一页里同一种标题不一样大」一眼就能看出来，而它又不是有意做出的层级
差别 —— 只是一段没人管的样式。

## 所以这里钉的不是「字号等于某个数」

数字是**抄**来的（正文抄 `.step-content p`，小标题抄本页那个 `<h3>「常见疑问」` 的内联
样式）。测试因此也比「等于 1.1rem」更强：它比的是**两边是否相等**，抄错一边就红。哪天
有人调了步骤区正文的字号而没管这一节，这条会立刻告诉他。

渲染级的复核在 `scripts/diff_help_typography.py`（无头 Chrome，整页逐元素对比：改前
改后哪些元素的字号/行高动了、动在哪一节）。测试里不引浏览器依赖 —— 那是改样式时手动跑
的工具，这里守的是「值别漂」与「守卫别被删」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELP = PROJECT_ROOT / "templates" / "help.html"


def _read() -> str:
    return HELP.read_text(encoding="utf-8")


def _style() -> str:
    """本页自己那段 `<style>`（整页渲染出来会有两段，`base.html` 还有一段）。"""
    hits = [b for b in re.findall(r"<style>(.*?)</style>", _read(), re.S) if ".step-content p {" in b]
    assert len(hits) == 1, f"认不出帮助页自己的样式块（命中 {len(hits)} 段）"
    return hits[0]


def _declarations(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in text.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        out[name.strip().lower()] = " ".join(value.split()).lower()
    return out


def _rule(selector: str) -> dict[str, str]:
    """取一条规则的声明。选择器在本文件里必须**只出现一次**，否则比的可能是别处。"""
    style = _style()
    pattern = re.compile(r"(?:^|[},])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", re.M)
    hits = pattern.findall(style)
    assert len(hits) == 1, f"样式里找不到唯一的 `{selector}`（命中 {len(hits)} 处）"
    return _declarations(hits[0])


def _ai_section() -> str:
    text = _read()
    start = text.index('id="section-ai"')
    end = text.index("</section>", start)
    return text[start:end]


# 自闭合 / 空元素：它们不入栈，否则嵌套深度会被算歪。
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "link", "meta", "source", "col"})


def _direct_children(block: str) -> list[str]:
    """`block` 的**直接**子元素开标签。

    为什么不直接正则搜 `<p>`/`<h4>`：那样连 `.step-content` 里的一起捞进来了，于是
    「AI 那一节还在靠兜底规则」这句话就成了空话 —— 实测过：把那三处补上 class，一条
    也不红。兜底规则管的是 `.section-body` 的**直接**子元素，这里就得按 DOM 的层数算。
    """
    out: list[str] = []
    depth = 0
    for match in re.finditer(r"<(/?)([a-zA-Z0-9]+)([^>]*?)(/?)>", block):
        closing, name, attrs, self_closing = match.groups()
        name = name.lower()
        if name in _VOID_TAGS or self_closing:
            if depth == 0:
                out.append(f"<{name}{attrs}>")
            continue
        if closing:
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(f"<{name}{attrs}>")
        depth += 1
    return out


def _bare_direct_children(tag: str) -> list[str]:
    """AI 那一节里，**直接**放在 `.section-body` 下、且没写 class 的 `<tag>`。"""
    section = _ai_section()
    body_at = section.index('class="section-body"')
    body = section[body_at:section.rindex("</div>")]
    return [
        item
        for item in _direct_children(body)
        if re.match(rf"<{tag}\b", item) and "class=" not in item
    ]


def test_the_flat_paragraph_is_sized_like_the_step_paragraph():
    """AI 那一节的 `<p>` 与步骤区里的 `<p>` 必须同号。

    这是最要紧的一条：正文占的篇幅最大，字号差 1.28px 加行高差 1px，整节读起来就不一样了。
    """
    flat = _rule(".section-body > p:not([class])")
    step = _rule(".step-content p")
    for prop in ("font-size", "color", "line-height"):
        assert flat[prop] == step[prop], (
            f"{prop} 与步骤区正文不一致：{flat.get(prop)!r} vs {step.get(prop)!r} —— "
            f"两边是同一角色的文字，改一个就要改另一个"
        )


def test_the_flat_subheading_is_sized_like_the_pages_own_subheading():
    """AI 那一节的 `<h4>` 必须与同一页里同角色的小标题同号。

    比对对象是**本页自己**那个 `<h3>「常见疑问」` 的内联样式：它与那三处是同一种东西
    （跟在段落/列表后面、上面不带分隔线的小标题）。有 `<hr>` 分隔线的那 4 个小标题上边距
    为 0，位置不同，值不同是对的 —— 所以只比字号/字重/颜色，不比外边距。
    """
    section = _ai_section()
    inline_h3 = re.search(r'<h3 style="([^"]*)"', section)
    assert inline_h3, "找不到 AI 那一节里那个内联样式的 <h3>（比对基准没了，这条就白测了）"
    inline = _declarations(inline_h3.group(1))

    rule = _rule(".section-body > h4:not([class])")
    for prop in ("font-size", "font-weight", "color"):
        assert rule[prop] == inline[prop], (
            f"小标题的 {prop} 与同页同角色的那个 <h3> 不一致："
            f"{rule.get(prop)!r} vs {inline.get(prop)!r}"
        )
    # 上边距必须**留出来**：这一节的 h4 跟在段落/列表后面，而兜底规则的默认外边距是 0，
    # 贴着上一段就等于没分段。
    assert rule["margin"].startswith(("2", "3")), (
        f"小标题上方没有留白：margin={rule.get('margin')!r}"
    )


def test_the_fallback_rules_only_catch_elements_without_their_own_style():
    """兜底规则必须**只兜没写 class 的**元素。

    这不是洁癖，是特异性算出来的：`.section-body > h4`（0,1,1）压得过 `.sub-title`
    （0,1,0），而 `.sub-title` 是「颜色说明」那一节有意做出来的另一种小标题（自带下边框）。
    少了 `:not([class])`，那 4 个小标题会被一起改掉 —— 改一处、坏一处，而且改的人不会知道。
    """
    for selector in (".section-body > h4:not([class])", ".section-body > p:not([class])"):
        assert ":not([class])" in selector  # 位置参数上的守卫，防手滑删掉
        _rule(selector)  # 规则真的在（不在会抛断言）
    # `>` 同样是守卫：不许漏进 `.step-content`（那里的 h4 顶着序号，上方不留白）。
    assert ".section-body > h4:not([class])" in _style()
    assert ".section-body > p:not([class])" in _style()


def test_the_ai_section_still_needs_these_fallback_rules():
    """**非空自检**：AI 那一节现在还真的在用这两条兜底规则。

    上面三条比的是「规则的值对不对」。如果哪天有人把这一节改写成 `.step-content`、
    或者给每个 `<p>` 都补上 class，规则就变成了死代码 —— 那三条会**继续绿着**，
    而它们守的东西已经不存在了。所以这里断言「还有元素要靠它」。
    """
    section = _ai_section()
    bare_h4 = _bare_direct_children("h4")
    bare_p = _bare_direct_children("p")
    assert len(bare_h4) >= 1, "AI 那一节已经没有不带 class 的 <h4> 了，兜底规则成了死代码"
    assert len(bare_p) >= 1, "AI 那一节已经没有不带 class 的 <p> 了，兜底规则成了死代码"
    # 顺带钉住「兜底规则确实命中它们」这一半：选择器是 `>` + `:not([class])`，
    # 与上面那个取法必须逐字对应。
    assert section.count('class="section-body"') == 1


@pytest.mark.parametrize("selector", [".step-content p", ".step-content h4"])
def test_the_step_rules_are_untouched(selector):
    """步骤区的值一个字没动（改的是「没被规则覆盖的元素」，不是规则本身）。"""
    declarations = _rule(selector)
    assert declarations["font-size"] in {"0.92rem", "1.05rem"}
