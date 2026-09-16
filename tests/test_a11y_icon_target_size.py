"""纯图标按钮的指针目标尺寸（WCAG 2.2 SC 2.5.8，AA）。

## 这个测试在防什么

SC 2.5.8 要求指针目标至少 **24×24 CSS px**，例外只有三条：Inline、Essential、
Equivalent。表格表头里的排序按钮、列表行里的图标操作按钮都是块级排布，
拿不到 Inline 例外。

真实缺陷形态是「按钮里只有一个图标，却还把内边距清零」：

  * Bootstrap 工具类 `p-0` 是 `padding:0 !important`，会把 .btn-sm 的
    `padding:.25rem .5rem` 连同 .btn 的 `padding:.375rem .75rem` 一起抹掉；
  * 抹掉之后按钮盒就只剩「字体行高 + 边框」，`.btn-sm` 是 0.875rem × 1.5
    = 21px 加 2px 边框 ≈ 23px，宽只剩图标推进宽 ≈ 14px —— 两个方向都低于 24px。

`weekly_version_config.html` 里那种「图标 + flex-fill」的按钮只矮不窄，
`merge_diff.html` 的 `.btn-xs`（局部样式把 line-height 压到 1）只矮不窄，
形态不同但结论一样。

## 尺寸是怎么算出来的

没有浏览器可用，所以这里按 CSS 层叠顺序做静态推算，而不是猜：

  1. Bootstrap 5.1.3 的 `.btn` 基线（font-size 1rem / line-height 1.5 /
     padding .375rem .75rem / 1px 边框）；
  2. `.btn-sm` 覆盖；
  3. 项目自己的 CSS（static/css/style.css 与各模板 <style> 块）里**选择器
     全部由 class 组成**的规则 —— 只要该元素带齐这些 class 就应用；
  4. 元素的行内 style；
  5. `p-0` / `px-0` / `py-0` 工具类最后生效（Bootstrap 里它们是 !important）。

**已知局限**（刻意不建模，改动前请先确认仍然成立）：
  * 祖先上下文选择器（如 `.btn-group-sm .btn`）不参与计算。当前这类规则
     只设置 border-radius / margin，不影响目标尺寸；
  * 图标推进宽按 1em 估算（Font Awesome 字形基本是 1em 推进）；
  * `@media print` 里的规则整块丢弃（打印没有指针目标）；其余媒体查询
     （如 max-width）按无条件规则应用 —— 宁可算严，也不漏掉小屏。

规则是**按页面**取的（全局 style.css + 该模板自己的 <style> 块）。早先版本把
所有模板的 <style> 混成一个全局规则池，于是 `repository_compare.html` 里
`@media print { .btn { display: none !important } }` 会污染别的页面的推算。

若某天这些局限不再成立，本测试会给出偏大的估算 —— 那时应先修模型再加断言。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"

MIN_TARGET_PX = 24.0
REM_PX = 16.0

# Bootstrap 5.1.3 的按钮基线。按 [层叠顺序, 声明] 排列，后应用的覆盖前面的。
_BOOTSTRAP_BTN = {
    "font-size": "1rem",
    "line-height": "1.5",
    "padding-top": ".375rem",
    "padding-bottom": ".375rem",
    "padding-left": ".75rem",
    "padding-right": ".75rem",
}
_BOOTSTRAP_BTN_SM = {
    "font-size": ".875rem",
    "padding-top": ".25rem",
    "padding-bottom": ".25rem",
    "padding-left": ".5rem",
    "padding-right": ".5rem",
}
# Bootstrap 的间距工具类是 !important，覆盖一切非 !important 声明，放最后应用。
_PADDING_UTILITIES = {
    "p-0": ("padding-top", "padding-bottom", "padding-left", "padding-right"),
    "px-0": ("padding-left", "padding-right"),
    "py-0": ("padding-top", "padding-bottom"),
}

_TAG_RE = re.compile(r"<(button|a)\b([^>]*)>(.*?)</\1>", re.S)
_CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')
_STYLE_ATTR_RE = re.compile(r'style="([^"]*)"')
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECL_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")


def _strip_icon_markup(inner: str) -> str:
    """剥掉图标/空 span/其余标签，返回按钮里剩余的可见文字。"""
    text = re.sub(r"<i\b[^>]*>\s*</i>", "", inner)
    text = re.sub(r"<span\b[^>]*>\s*</span>", "", text)
    return re.sub(r"<[^>]+>", "", text).strip()


def _strip_print_media(css_text: str) -> str:
    """整块删掉 `@media print { ... }`（含嵌套花括号）。

    打印样式表里没有指针目标 —— `repository_compare.html` 的
    `@media print { .btn { display:none !important } }` 就是这种，
    当成无条件规则会把该页所有按钮都算成隐藏。
    """
    out, index = [], 0
    while True:
        match = re.search(r"@media[^{]*\bprint\b[^{]*\{", css_text[index:])
        if not match:
            out.append(css_text[index:])
            return "".join(out)
        start = index + match.start()
        out.append(css_text[index:start])
        depth, cursor = 1, index + match.end()
        while cursor < len(css_text) and depth:
            if css_text[cursor] == "{":
                depth += 1
            elif css_text[cursor] == "}":
                depth -= 1
            cursor += 1
        index = cursor


def _collect_class_rules(css_text: str) -> list[tuple[frozenset[str], dict[str, str]]]:
    """抽出「选择器只由 class 组成」的规则 → [(class 集合, 声明表)]，按出现顺序。"""
    # 必须先剥注释：`[^{}]+` 会把规则前的 /* ... */ 一起吃进选择器，
    # 于是 `.sort-header-btn` 前那段说明注释会让整条规则被当成「选择器不以 . 开头」丢掉。
    css_text = _strip_print_media(re.sub(r"/\*.*?\*/", "", css_text, flags=re.S))
    rules: list[tuple[frozenset[str], dict[str, str]]] = []
    for selector_group, body in _RULE_RE.findall(css_text):
        # 取最后一个 '{' 之后的部分：这样 @media 包裹的规则也能取到真正的选择器
        selector = selector_group.rsplit("{", 1)[-1].strip()
        decls = {name.strip().lower(): value.strip() for name, value in _DECL_RE.findall(body)}
        if not decls:
            continue
        for one in selector.split(","):
            one = one.strip()
            if not one or not one.startswith("."):
                continue
            classes = one.split(".")
            if any(not re.fullmatch(r"[A-Za-z0-9_-]+", cls) for cls in classes[1:]):
                continue  # 含伪类/伪元素/属性选择器 → 不建模
            if len(classes) - 1 != one.count("."):  # 形如 .a.b 之外的分支，跳过
                continue
            rules.append((frozenset(classes[1:]), decls))
    return rules


def _to_px(value: str) -> float | None:
    value = value.strip().lower()
    m = re.fullmatch(r"(-?[\d.]+)(px|rem|em|pt)?", value)
    if not m:
        return None
    number = float(m.group(1))
    unit = m.group(2) or "px"
    if unit == "px":
        return number
    if unit in ("rem", "em"):
        return number * REM_PX
    if unit == "pt":
        return number * 96.0 / 72.0
    return None


_SIZE_PROPS = (
    "font-size", "line-height", "padding", "padding-top", "padding-bottom",
    "padding-left", "padding-right", "min-width", "min-height", "width", "height",
    "border", "display", "align-items", "justify-content",
)
_PADDING_SIDES = ("padding-top", "padding-right", "padding-bottom", "padding-left")


def _expand_padding(value: str) -> list[str]:
    """把 padding 简写展开成 上/右/下/左 四个值（CSS 的 1/2/3/4 值语法）。"""
    parts = value.split()
    if len(parts) == 1:
        return parts * 4
    if len(parts) == 2:
        return [parts[0], parts[1], parts[0], parts[1]]
    if len(parts) == 3:
        return [parts[0], parts[1], parts[2], parts[1]]
    return parts[:4]


def _apply(resolved: dict[str, str], decls: dict[str, str]) -> None:
    for name in _SIZE_PROPS:
        if name not in decls:
            continue
        value = decls[name].replace("!important", "").strip()
        if name == "padding":
            # 简写就是四个长写的缩写，必须展开成四个长写：
            # 否则 Bootstrap 基线里的 padding-top/.375rem 会继续生效，
            # 把 .btn-xs 的 22px 高算成 26px —— 实测过，把 .btn-xs 的
            # min-height 删掉，那种模型仍然判绿（变异没被抓住）。
            for side, part in zip(_PADDING_SIDES, _expand_padding(value)):
                resolved[side] = part
            resolved.pop("padding", None)
        else:
            resolved[name] = value


def _resolve(classes: list[str], inline_style: str, rules) -> dict[str, str]:
    """按层叠顺序算出该按钮最终生效的尺寸相关声明。"""
    resolved: dict[str, str] = {}
    resolved.update(_BOOTSTRAP_BTN)
    if "btn-sm" in classes:
        resolved.update(_BOOTSTRAP_BTN_SM)
    for class_set, decls in rules:
        if class_set and class_set <= set(classes):
            _apply(resolved, decls)
    _apply(resolved, dict(_DECL_RE.findall(inline_style)))
    for utility, sides in _PADDING_UTILITIES.items():
        if utility in classes:
            for side in sides:
                resolved[side] = "0"
    return resolved


def _declared_px(resolved: dict[str, str], name: str) -> float | None:
    """读取某个方向的内边距（长写）。简写在 _apply 里已经展开，这里不再处理。"""
    assert name in _PADDING_SIDES, name
    if name not in resolved:
        return None
    return _to_px(resolved[name])


def _icon_only_buttons():
    """产出 (文件, 行号, class 列表, 行内 style, 未解决的 class)。"""
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        for match in _TAG_RE.finditer(source):
            attrs, inner = match.group(2), match.group(3)
            class_match = _CLASS_ATTR_RE.search(attrs)
            classes = class_match.group(1).split() if class_match else []
            if "btn" not in classes:
                continue
            if _strip_icon_markup(inner):
                continue
            style_match = _STYLE_ATTR_RE.search(attrs)
            line = source[: match.start()].count("\n") + 1
            yield (
                path.relative_to(REPO_ROOT).as_posix(),
                line,
                classes,
                style_match.group(1) if style_match else "",
                " ".join(inner.split())[:70],
            )


_GLOBAL_RULES: list | None = None
_RULES_CACHE: dict[str, list] = {}


def _global_rules():
    global _GLOBAL_RULES
    if _GLOBAL_RULES is None:
        _GLOBAL_RULES = _collect_class_rules(
            (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        )
    return _GLOBAL_RULES


def _rules_for(rel_path: str):
    """某个模板实际生效的规则：全局 style.css + 该模板自己的 <style> 块。

    不能把所有模板的 <style> 混在一起 —— 页面局部样式会串页（见模块 docstring）。
    """
    if rel_path not in _RULES_CACHE:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        rules = list(_global_rules())
        for block in _STYLE_BLOCK_RE.findall(source):
            rules.extend(_collect_class_rules(block))
        _RULES_CACHE[rel_path] = rules
    return _RULES_CACHE[rel_path]


def _page_rules(path):
    """兼容两种入参：相对路径字符串或 Path。"""
    return _rules_for(Path(path).as_posix().replace(str(REPO_ROOT.as_posix()) + "/", ""))


def _measure(classes, inline_style, class_rules):
    resolved = _resolve(classes, inline_style, class_rules)
    font_size = _to_px(resolved.get("font-size", "1rem")) or REM_PX
    line_height = resolved.get("line-height", "1.5")
    if line_height == "normal":
        content_height = font_size * 1.2
    elif re.fullmatch(r"-?[\d.]+", line_height):
        # 无单位 line-height 是**倍数**，不是 px —— 早先这里当成 px 处理，
        # 把 .btn-sm 的 21px 行盒算成了 1.5px，整个尺寸推算就废了。
        content_height = float(line_height) * font_size
    else:
        content_height = _to_px(line_height) or font_size * 1.5
    border = _to_px(resolved.get("border", "").split()[0]) if resolved.get("border") else 1.0
    border = 1.0 if border is None else border

    pad_x = (_declared_px(resolved, "padding-left") or 0.0) + (_declared_px(resolved, "padding-right") or 0.0)
    pad_y = (_declared_px(resolved, "padding-top") or 0.0) + (_declared_px(resolved, "padding-bottom") or 0.0)

    width = pad_x + font_size + 2 * border  # 图标推进宽按 1em 估算
    height = pad_y + content_height + 2 * border

    min_width = _to_px(resolved["min-width"]) if "min-width" in resolved else None
    min_height = _to_px(resolved["min-height"]) if "min-height" in resolved else None
    if min_width is not None:
        width = max(width, min_width)
    if min_height is not None:
        height = max(height, min_height)
    return width, height


def test_every_icon_only_button_meets_the_24px_target():
    """枚举所有纯图标按钮，按层叠推算尺寸，低于 24×24 的一律判红。"""
    offenders = []
    for path, line, classes, inline_style, icon in _icon_only_buttons():
        width, height = _measure(classes, inline_style, _rules_for(path))
        if width < MIN_TARGET_PX or height < MIN_TARGET_PX:
            offenders.append(
                f"{path}:{line} {icon} → 推算 {width:.0f}×{height:.0f}px"
                f"（class=\"{' '.join(classes)}\"）"
            )
    assert not offenders, (
        "以下纯图标按钮的推算目标尺寸不足 24×24 CSS px"
        "（WCAG 2.2 SC 2.5.8）：\n  " + "\n  ".join(offenders)
    )


def test_sort_buttons_do_not_zero_out_their_padding():
    """排序按钮曾经带 `p-0`：内边距被 !important 清零后盒子只剩图标本身。"""
    page = "templates/status_sync_management.html"
    rules = _rules_for(page)
    source = (REPO_ROOT / page).read_text(encoding="utf-8")
    buttons = [c for p, _, c, _, _ in _icon_only_buttons() if p == page]
    assert len(buttons) == 3, f"应有 3 个排序按钮，实际 {len(buttons)} 个 —— 模板结构变了"
    for classes in buttons:
        assert not ({"p-0", "px-0", "py-0"} & set(classes)), (
            f"排序按钮不能再带清零内边距的工具类：{' '.join(classes)}"
        )
        assert "sort-header-btn" in classes, (
            f"排序按钮需要 sort-header-btn 提供 24×24 的最小盒：{' '.join(classes)}"
        )
        width, height = _measure(classes, "", rules)
        assert width >= MIN_TARGET_PX and height >= MIN_TARGET_PX, (
            f"排序按钮推算尺寸 {width:.0f}×{height:.0f}px 仍不达标"
        )
    assert source.count('data-sort="') == 3


def test_sort_header_btn_rule_guarantees_the_minimum():
    """光加 class 不够 —— CSS 规则本身必须真的给出 >= 24px 的最小盒。

    只断言「模板里带了 sort-header-btn」是不够的：把 CSS 里的 min-height 删掉，
    前一条断言仍然绿 —— 那正是最容易发生的回归（改样式时顺手删掉一行）。
    """
    resolved = _resolve(
        ["btn", "btn-sm", "btn-link", "sort-header-btn"],
        "",
        _rules_for("templates/status_sync_management.html"),
    )
    assert _to_px(resolved.get("min-width", "0")) >= MIN_TARGET_PX, resolved
    assert _to_px(resolved.get("min-height", "0")) >= MIN_TARGET_PX, resolved
    # 图标要居中，否则 24px 的盒子里图标会偏到左上角
    assert resolved.get("display") == "inline-flex", resolved
    assert resolved.get("align-items") == "center", resolved


def test_btn_xs_in_merge_diff_reaches_the_minimum():
    """merge_diff.html 的局部 .btn-xs 把 line-height 压到 1，纯图标时只有 ~22px 高。"""
    rules = _rules_for("templates/merge_diff.html")
    classes = ["btn", "btn-outline-primary", "btn-xs"]
    resolved = _resolve(classes, "", rules)
    assert resolved.get("line-height") == "1", (
        "merge_diff.html 的 .btn-xs 定义变了（原本 line-height: 1）—— 请重新核对尺寸推算"
    )
    width, height = _measure(classes, "", rules)
    assert width >= MIN_TARGET_PX and height >= MIN_TARGET_PX, f"{width:.0f}×{height:.0f}px"


def test_page_local_rules_do_not_leak_across_templates():
    """反向自检：样式必须按页面取。

    `repository_compare.html` 里有 `@media print { .btn { display:none !important } }`。
    早先版本把所有模板的 <style> 混成一个全局池，这条规则就会被当成无条件规则
    套到别的页面上，把排序按钮算成 `display:none`。
    """
    global_display = _resolve(["btn", "btn-sm"], "", _global_rules()).get("display")
    assert global_display != "none !important", (
        "全局 style.css 里不该有隐藏 .btn 的规则 —— 若有，说明规则被人挪进了全局表"
    )
    other = _resolve(
        ["btn", "btn-sm", "btn-link", "sort-header-btn"],
        "",
        _rules_for("templates/status_sync_management.html"),
    )
    assert other.get("display") == "inline-flex", other


def test_the_measurement_model_is_not_vacuous():
    """反向自检：模型必须能把「p-0 + 图标」这种真实形态判成不达标。

    否则一个恒真的估算器会让上面所有断言都失去意义。
    """
    rules = _global_rules()
    width, height = _measure(["btn", "btn-sm", "btn-link", "p-0"], "", rules)
    assert width < MIN_TARGET_PX, f"模型把 p-0 图标按钮算成了 {width:.0f}px 宽，模型失效了"
    assert height < MIN_TARGET_PX, f"模型把 p-0 图标按钮算成了 {height:.0f}px 高，模型失效了"


def test_the_model_still_sees_the_bootstrap_shapes_it_promises():
    """模型自检：.btn-sm 的推算值必须与手算一致，否则整套断言都在算错的数上。

    手算：高 = 4px×2 内边距 + 21px 行盒(.875rem×1.5) + 1px×2 边框 = 31px；
          宽 = 8px×2 内边距 + 14px 图标(1em) + 2px 边框 = 32px。
    """
    width, height = _measure(["btn", "btn-sm"], "", _global_rules())
    assert (round(width), round(height)) == (32, 31), f"{width}×{height}"
