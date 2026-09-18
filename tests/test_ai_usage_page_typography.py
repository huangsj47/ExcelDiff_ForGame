# -*- coding: utf-8 -*-
"""AI 消耗面板的**排版**：字号刻度、阅读宽度、窄屏表格。

## 这一轮是怎么定下改什么的

用户只说了一句「页面表现效果不好」。效果这种事容易变成各说各话，所以先在无头 Chrome 里
真渲染出来量（`scripts/shot_ai_usage_page.py`：临时库里造一份有代表性的数据 → 走真路由取
真响应 → 截图 + 逐元素量值），量出来三条：

| 量到的 | 改前 | 说明 |
|---|---|---|
| 页面用了几档字号 | **九档**（11 12 13 14 15 16 17 22 24） | 13 与 14 只差 1px —— 「说明」与「数据」因此是同一层，整页读起来是一片文字 |
| 最长那几段说明的宽度 | 1382px（铺满卡片） | 一行上百个汉字，回行时找不着下一行的开头 |
| 420px 下项目表「项目」那一列 | **48px** | 「新手引导配表」竖着排成六行，整页被撑到 5169px 高 |

还有一条是量的时候顺带发现的：三个 11px 的节点是说明文字里的行内 `<code>`
（`xxx-*` / `gpt-4` / `gpt-4o`）—— Bootstrap 的 `code { font-size: .875em }` 在一个 13px
的段落里把它们压到了 11.4px，低于本仓库 12px 的下限。说明文字一缩小，它们还要再掉一档。

## 为什么钉这几条（而不是「字号等于 1.5rem」）

* **刻度**：钉的是「字号只能取刻度上的值」。抄某个具体数字没有意义，任何一次合理的调整
  都会改它；而「又多出一档 13px」正是当初那九档的来路，它不报错、也不难看，只是整页
  糊成一片。渲染出来才看得见，看单个声明看不出来。
* **标题不能比数字小**：改前 H1 是 22px、KPI 数字是 24px —— 页面上最响的是「7.72M」，
  而「AI 消耗」四个字反倒更小。这是**层级倒挂**，不是审美偏好。
* **中文不许用 `ch` 限宽**：`ch` 是数字 0 的宽度（12px 下一个汉字约占两个 `ch`），
  `68ch` 量出来只有 440px —— 那是把中文挤成窄条，不是限宽。本仓库为这件事踩过一次坑。
* **窄屏表格**：改成横向滚动之后必须补的另一半是**键盘可达** —— 鼠标能滑，键盘滑不动，
  右边那几列就真的够不着了。

量值在 `scripts/shot_ai_usage_page.py` 里复核（无头 Chrome，整页 + 逐元素）；测试里不引
浏览器依赖 —— 那是改样式时手动跑的工具，这里守的是「值别漂」与「守卫别被删」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "templates" / "ai_usage_dashboard.html"

# 五档：12 / 14 / 16 / 18 / 24px。12 是本仓库的下限（正文最小 12px）。
SCALE_PX = {12.0, 14.0, 16.0, 18.0, 24.0}

# 基准字号。Bootstrap 5 的 `html { font-size: 100% }` → 16px，`rem` 就是按它换算的。
ROOT_PX = 16.0


def _read() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _style() -> str:
    """本页自己那段 `<style>`（`base.html` 还有一段，只认带 `.aiu-hero` 的那段）。"""
    hits = [b for b in re.findall(r"<style>(.*?)</style>", _read(), re.S) if ".aiu-hero" in b]
    assert len(hits) == 1, f"认不出消耗面板自己的样式块（命中 {len(hits)} 段）"
    return _strip_css_comments(hits[0])


def _strip_css_comments(css: str) -> str:
    """**静态断言之前必须先剥注释。**

    本文件与页面的注释里原样写着被禁掉的写法（`code { font-size: .875em }`、
    `68ch` 这些反例本身就是注释里的说明文字）。不剥的话，`font-size` 与 `ch` 的
    扫描会把注释一起算进来 —— 那种失败极难查：实现明明是对的。
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declarations(text: str) -> dict:
    out: dict = {}
    for chunk in text.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        out[name.strip().lower()] = " ".join(value.split()).lower()
    return out


def _rules() -> list:
    """整页的 `(选择器表, 声明)`，**跳过 `@media` 段**。

    媒体查询里的规则只在特定宽度生效，合并进来会读出一个浏览器不一定用的值。
    花括号要自己配平：`@media` 里还嵌着规则，正则数不清。
    """
    style = _style()
    out: list = []
    depth = 0
    head_start = 0
    open_at = None
    for index, char in enumerate(style):
        if char == "{":
            if depth == 0:
                open_at = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and open_at is not None:
                head = style[head_start:open_at].strip()
                if not head.startswith("@"):
                    out.append((head, _declarations(style[open_at + 1:index])))
                head_start = index + 1
                open_at = None
    return out


def _rule(selector: str) -> dict:
    """这个选择器的**生效声明**（同一条选择器写在两处时按 CSS 的先后覆盖）。

    判据是「浏览器会取哪个值」，不是「这个字符串出现过几次」—— 这一页本来就把新增的
    规则追加在样式表末尾（后写的赢），按出现次数断言反而会挡住正常写法。
    """
    merged: dict = {}
    for head, decls in _rules():
        parts = [part.strip() for part in head.split(",")]
        if selector in parts:
            merged.update(decls)
    assert merged, f"样式里没有 `{selector}`"
    return merged


def _font_sizes() -> list:
    """全页的 `font-size` 取值（注释已剥），`em` 单独标出来。"""
    return sorted(set(re.findall(r"font-size:\s*([^;}]+)", _style())))


def _to_px(value: str) -> float:
    text = " ".join(value.split()).lower()
    if text.endswith("rem"):
        return round(float(text[:-3]) * ROOT_PX, 2)
    if text.endswith("px"):
        return float(text[:-2])
    raise AssertionError(f"这个单位不在刻度判据里（要么换算它，要么别用）：{value!r}")


# ==========================================================================
#  一、字号刻度
# ==========================================================================


class TestTheTypeScale:
    def test_every_font_size_is_on_the_scale(self):
        """**这一条就是那个「九档」的守卫。** 多出第 13px 不会报错，只会让整页糊成一片。"""
        off_scale = []
        for value in _font_sizes():
            if value.strip().endswith("em") and not value.strip().endswith("rem"):
                # `1em`（行内 code 跟随周围字号）不是一档字号，是「不设字号」。
                assert value.strip() == "1em", f"只允许 `1em` 这一种相对写法：{value!r}"
                continue
            px = _to_px(value)
            if px not in SCALE_PX:
                off_scale.append((value, px))
        assert not off_scale, (
            f"这些字号不在刻度 {sorted(SCALE_PX)} 上：{off_scale}。"
            "刻度是 12/14/16/18/24 —— 多一档只差 1px 的字号，读起来是「同一层」"
        )

    def test_the_page_title_is_at_least_as_large_as_the_numbers(self):
        """层级不能倒挂：改前 H1 是 22px、KPI 数字是 24px，页面最响的是「7.72M」。"""
        title = _to_px(_rule(".aiu-hero h1")["font-size"])
        number = _to_px(_rule(".aiu-kpi-value")["font-size"])
        assert title >= number, f"页面标题 {title}px 比 KPI 数字 {number}px 还小"

    def test_the_smallest_size_is_the_repo_floor(self):
        """12px 是下限（仓库的正文最小字号），说明文字正好停在那儿。"""
        sizes = {_to_px(v) for v in _font_sizes() if not v.strip().endswith("em")
                 or v.strip().endswith("rem")}
        assert min(sizes) == 12.0, f"最小的字号是 {min(sizes)}px"

    def test_inline_code_keeps_the_surrounding_size(self):
        """行内 `code` 要跟着周围字号走。

        Bootstrap 的 `code { font-size: .875em }`：在 13px 的说明里是 11.4px，
        在 12px 的说明里就掉到 10.5px —— 低于仓库下限，而且**没有报错**。
        （无头 Chrome 实测：改前那三个 11px 的节点就是说明里的 `xxx-*` / `gpt-4` / `gpt-4o`。）
        """
        assert _rule(".aiu-page code")["font-size"] == "1em"

    def test_the_kpi_number_shrinks_but_stays_loud_on_narrow_screens(self):
        """窄屏把 24px 收到 18px —— 仍是刻度上的一档，且仍比正文大。"""
        style = _style()
        block = style[style.index("@media (max-width: 767px)"):]
        match = re.search(r"\.aiu-kpi-value\s*\{([^}]*)\}", block)
        assert match, "窄屏那一段里没有 KPI 数字的字号规则"
        size = _to_px(_declarations(match.group(1))["font-size"])
        assert size in SCALE_PX, size
        assert size > _to_px(_rule(".aiu-table")["font-size"]), "缩到比表格正文还小了"


# ==========================================================================
#  二、阅读宽度
# ==========================================================================


class TestReadingWidth:
    def test_the_long_prose_blocks_are_capped(self):
        """实测 1382px 铺满卡片的长段落，一行上百个汉字。

        SKILL 的 Line Length 一条：一行 65~75 个字符；宽屏上铺满整行的正文是反模式。
        """
        capped = _rule(".aiu-hint")
        assert capped.get("max-width", "").endswith("px"), capped
        assert float(capped["max-width"][:-2]) <= 1000, capped

    def test_the_cap_is_not_expressed_in_ch(self):
        """**中文不许用 `ch` 限宽。**（本仓库踩过：`68ch` 只有 440px，是把中文挤成窄条。）"""
        assert not re.search(r"\d+(\.\d+)?ch\b", _style()), (
            "样式里出现了 `ch` 单位 —— 它是数字 0 的宽度，一个汉字约占两个 `ch`"
        )

    def test_the_notes_read_as_secondary(self):
        """说明文字要比数据小一档、行距更松，否则整页是同一层。"""
        hint = _rule(".aiu-hint")
        table = _rule(".aiu-table")
        assert _to_px(hint["font-size"]) < _to_px(table["font-size"]), (
            f"说明 {hint['font-size']} 不比表格正文 {table['font-size']} 小"
        )
        assert float(hint["line-height"]) >= 1.5, hint


# ==========================================================================
#  三、窄屏表格
# ==========================================================================


class TestNarrowScreenTables:
    def test_the_project_name_column_can_not_collapse(self):
        """改前那一列只剩 48px：「新手引导配表」竖着排成六行。

        中文可以在**任意两个字之间**断行，所以这一列的 `min-content` 宽度只有一个字 ——
        它会被压到极限。`nowrap` + 下限才是这条的修法。
        """
        assert _rule(".aiu-name")["white-space"] == "nowrap"
        cell = _rule(".aiu-table th:first-child")
        assert float(cell["min-width"].rstrip("px")) >= 100, cell

    def test_the_table_itself_has_a_floor_so_the_wrapper_scrolls(self):
        """窄屏**整体横向滚动**，而不是把某一列压成一字一行（SKILL 的 Table Handling）。"""
        floor = _rule(".aiu-table")["min-width"]
        assert floor.endswith("px") and float(floor[:-2]) >= 560, floor

    def test_every_scroll_container_is_reachable_by_keyboard(self):
        """横向滚动的那一半：**可滚动区域必须拿得到焦点**。

        鼠标/触摸能滑，键盘用户滑不动 —— 右边那几列就真的够不着了（W3C）。
        """
        html = _read()
        wrappers = re.findall(r'<div class="aiu-table-wrap"[^>]*>', html)
        assert wrappers, "一个表格容器都没有？"
        for tag in wrappers:
            assert 'role="region"' in tag, tag
            assert 'tabindex="0"' in tag, tag
            assert "aria-labelledby=" in tag, tag

    def test_every_region_name_points_at_a_real_caption(self):
        """区域名复用表格自己的 `<caption>`（同一句话只写一遍，不另抄一份说明）。"""
        html = _read()
        ids = set(re.findall(r'<caption class="aiu-caption" id="([^"]+)"', html))
        referenced = set()
        for tag in re.findall(r'<div class="aiu-table-wrap"[^>]*>', html):
            referenced |= set(re.findall(r'aria-labelledby="([^"]+)"', tag))
        assert referenced, "没有一处区域名"
        assert referenced <= ids, f"这些区域名找不到对应的 caption：{sorted(referenced - ids)}"

    def test_the_dynamically_built_wrapper_gets_the_same_treatment(self):
        """逐轮明细那张表是 JS 现造的 —— 静态模板里搜不到它的容器。

        漏掉它的表现是「只有那张表在窄屏上没法用键盘滚」，而这种不一致没人会想到去查。
        """
        html = _read()
        marker = html.index("wrap.className = 'aiu-table-wrap'")
        block = html[marker:marker + 400]
        assert "setAttribute('role', 'region')" in block, block
        assert "setAttribute('tabindex', '0')" in block, block
        assert "aria-labelledby" in block, block

    def test_the_focus_ring_is_visible(self):
        """给了可达性却看不见焦点，等于没给（拿得到焦点之后焦点圈归它管）。"""
        rule = _rule(".aiu-table-wrap:focus-visible")
        assert "outline" in rule, rule
