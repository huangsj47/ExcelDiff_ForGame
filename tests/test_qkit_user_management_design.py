"""平台用户管理页（AUTH_BACKEND=qkit 下的 /auth/users）的版式重排守卫。

## 这个文件在防什么

`qkit_auth/templates/qkit_user_management.html` 是线上 `/auth/users` 真正渲染的
模板（`?sort_by=…&sort_dir=…&project_id=…` 这三个参数只由 `qkit_auth/routes.py`
的 `user_list()` 处理），重排前它有几类**实测**的缺陷：

1. **角色变更是权限写操作，却没有任何确认。** 点一下下拉里的「平台管理员」立刻
   发 POST，误点即提权。其余会改账号状态的操作（停用/删除）都有 confirm，
   只有这一条没有。
2. **提交按钮没有忙状态。** 「确认添加 / 保存 / 导入用户」点下去界面毫无反馈，
   用户会重复点击，同一份数据被提交两次；出错时也没有行内提示，只有裸 alert()。
3. **表头排序状态对读屏用户不可见。** 点完只换了箭头图标，`aria-sort` 从头到尾
   没出现过 —— 读屏用户听到的永远是「用户名 按钮」，不知道当前按哪列、升还是降。
   同时排序按钮 `padding:0; border:0`，盒高只有 0.74rem 的字行盒（≈14px），
   低于 WCAG 2.2 SC 2.5.8 要求的 24×24 指针目标。
4. **徽章颜色不成立。** 模板用的是 Bootstrap 5.3 才加入的 `.bg-*-subtle`
   （本仓库加载的是 5.1.3 CDN；5.3 的 Background 文档列了这一族，5.2 的迁移
   说明里还没有）。这些类在 5.1.3 上不存在 → 徽章渲染成"透明底 + 语义基色
   文字"：`text-success` #198754 在白底 3.94:1、`text-info` #0dcaf0 仅 1.96:1，
   都不到 AA 正文阈值，而且"有底色"这个语义整个丢了。

第 3、4 条是静态可判的；第 1、2 条断言的是"调用时到底发生了什么"，
所以这里把模板里**真实的** `updateRole` / `setBusy` 按花括号配对切出来，
在 Node 里配桩真跑一遍（没有 Node 的环境自动跳过这一条，其余断言仍然生效）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "qkit_auth" / "templates" / "qkit_user_management.html"
STYLE_CSS = REPO_ROOT / "static" / "css" / "style.css"

AA_NORMAL_TEXT = 4.5

_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECL_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")
_TOKEN_RE = re.compile(r"(--[A-Za-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")
_VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9-]+)\s*(?:,\s*([^()]+?)\s*)?\)")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
_CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """按花括号配对把模板里真实的 JS 函数切出来（不重写、不复制一份）。"""
    source = _source()
    start = source.find(f"function {name}(")
    assert start != -1, f"模板里找不到 {name}() —— 这段行为被改动或删除了"
    cursor = source.index("{", start)
    depth = 0
    for index in range(cursor, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name}() 的花括号不配对")


def _run_node(harness: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("环境里没有 node，跳过这条真跑 JS 的断言")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "harness.js"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


# ─────────────────────────── 行为：确认 + 忙状态 ───────────────────────────

_ROLE_HARNESS = """
const calls = [];
let confirmAnswer = true;
globalThis.confirm = (msg) => { calls.push(['confirm', msg]); return confirmAnswer; };
globalThis.alert = (msg) => { calls.push(['alert', msg]); };
globalThis.location = { reload: () => { calls.push(['reload']); } };
globalThis.userEditMap = { '7': { id: 7, username: 'zhangsan', display_name: '张三' } };
globalThis.apiCall = (url, method, body) => {
    calls.push(['apiCall', url, method, body]);
    return Promise.resolve({ success: true });
};

__FUNCTION__

(async () => {
    confirmAnswer = false;
    updateRole(7, 'platform_admin');
    const denied = calls.filter((c) => c[0] === 'apiCall');

    confirmAnswer = true;
    updateRole(7, 'project_admin');
    await new Promise((resolve) => setTimeout(resolve, 0));

    process.stdout.write(JSON.stringify({
        afterDeny: denied,
        afterAllow: calls.filter((c) => c[0] === 'apiCall'),
        confirmMessages: calls.filter((c) => c[0] === 'confirm').map((c) => c[1]),
    }));
})();
"""


def test_role_change_asks_before_it_writes():
    """角色变更是提权/降权写操作：没确认过就绝不能发出 POST。

    只断言源码里出现 `confirm(` 是不够的 —— 把 confirm 的返回值丢掉、
    或者写成 `if (confirm(...)) { /* 空 */ }` 再继续往下发请求，纯文本断言照样绿。
    这里真跑一遍：confirm 返回 false 时 `apiCall` 一次都不能被调用。
    """
    harness = _ROLE_HARNESS.replace("__FUNCTION__", _function_source("updateRole"))
    result = _run_node(harness)

    assert result["afterDeny"] == [], (
        f"用户点了「取消」之后仍然发出了权限写请求：{result['afterDeny']}"
    )
    assert len(result["afterAllow"]) == 1, (
        f"确认之后应当只发一次角色变更请求，实际 {result['afterAllow']}"
    )
    _, url, method, body = result["afterAllow"][0]
    assert url == "/auth/api/users/7/role", url
    assert method == "POST", method
    assert body == {"role": "project_admin"}, body
    assert len(result["confirmMessages"]) == 2, result["confirmMessages"]
    for message in result["confirmMessages"]:
        assert "张三" in message, f"确认文案里应当点名被改动的用户：{message}"


_BUSY_HARNESS = """
function makeButton(html) {
    return {
        disabled: false,
        innerHTML: html,
        dataset: {},
        attrs: {},
        setAttribute(name, value) { this.attrs[name] = value; },
        removeAttribute(name) { delete this.attrs[name]; },
    };
}

__FUNCTION__

const button = makeButton('<i class="fas fa-check me-1"></i>确认添加');
const original = button.innerHTML;

setBusy(button, true, '添加中...');
const busy = {
    disabled: button.disabled,
    ariaBusy: button.attrs['aria-busy'] || null,
    html: button.innerHTML,
};

// 重复置忙：快照不能被第二次覆盖，否则还原时会把转圈图标留在按钮上
setBusy(button, true, '添加中...');
setBusy(button, false);
const idle = {
    disabled: button.disabled,
    ariaBusy: button.attrs['aria-busy'] || null,
    html: button.innerHTML,
};

process.stdout.write(JSON.stringify({ original, busy, idle }));
"""


def test_submit_buttons_expose_a_busy_state_and_come_back():
    """提交中要禁用并标 aria-busy，结束后必须还原成原来的样子。

    真实缺陷形态有两种，静态断言都看不见：
      * 只置 disabled 不还原 —— 请求失败后按钮永远点不动；
      * 忙状态里把 innerHTML 换成转圈图标，重复置忙时又拿转圈当"原始内容"存快照，
        还原后按钮上永久留着一个 spinner。
    """
    harness = _BUSY_HARNESS.replace("__FUNCTION__", _function_source("setBusy"))
    result = _run_node(harness)

    assert result["busy"]["disabled"] is True, "提交中按钮没有禁用"
    assert result["busy"]["ariaBusy"] == "true", result["busy"]
    assert "spinner-border" in result["busy"]["html"], result["busy"]
    assert "添加中..." in result["busy"]["html"], result["busy"]

    assert result["idle"]["disabled"] is False, "请求结束后按钮没有恢复可点"
    assert result["idle"]["ariaBusy"] is None, result["idle"]
    assert result["idle"]["html"] == result["original"], (
        f"按钮没有还原成原始内容，残留了忙状态：{result['idle']['html']!r}"
    )


# ─────────────────────────── 结构：aria-sort 与 24px 指针目标 ───────────────────────────


def test_sortable_headers_expose_their_sort_state():
    """排序状态必须写进 `aria-sort`，不能只靠一个箭头图标。

    重排前表头里只有「按钮 + ↑/↓ 文本」，读屏用户点完排序听不出任何变化。
    """
    source = _source()
    rows = re.findall(r"\{\{\s*sortable_th\('([a-z_]+)',\s*'([^']+)'\)\s*\}\}", source)
    assert [key for key, _label in rows] == [
        "username", "display_name", "email", "role", "status",
    ], f"可排序的列变了：{rows}"

    # th 上的 aria-sort 由 sort_by / sort_dir 决定，三种取值都要出现
    th = re.search(r'aria-sort="\{\{(.*?)\}\}"', source, re.S)
    assert th, "可排序表头没有 aria-sort"
    expression = th.group(1)
    for token in ("'ascending'", "'descending'", "'none'"):
        assert token in expression, f"aria-sort 的取值里缺少 {token}：{expression}"


def test_sort_buttons_meet_the_24px_pointer_target():
    """排序按钮是块级排布，拿不到 SC 2.5.8 的 Inline 例外，必须 >= 24×24 CSS px。

    重排前 `.table-sort-btn` 是 `padding: 0; border: 0`，盒子只剩 0.74rem 的
    字行盒（≈14px 高、文本宽），两个方向都不达标。
    只断言 class 名在是不够的 —— 把 min-height 删掉，那种断言照样绿。
    """
    rules = _page_rules()
    decls = rules.get(".um-sort")
    assert decls is not None, "页面里找不到 .um-sort 规则 —— 排序按钮的样式被删了"
    assert _px(decls.get("min-width")) >= 24, decls
    assert _px(decls.get("min-height")) >= 24, decls
    assert decls.get("display") == "inline-flex", decls
    assert decls.get("align-items") == "center", decls


# ─────────────────────────── 颜色：token 化的徽章必须过 AA ───────────────────────────


def _root_tokens() -> dict[str, str]:
    css = _COMMENT_RE.sub("", STYLE_CSS.read_text(encoding="utf-8"))
    start = css.index(":root")
    cursor = css.index("{", start)
    depth = 0
    for index in range(cursor, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return dict(_TOKEN_RE.findall(css[cursor:index]))
    raise AssertionError("style.css 的 :root 块不完整")


def _page_rules() -> dict[str, dict[str, str]]:
    """页面自己的 <style> 块里「选择器只有一个 class」的规则 → {选择器: 声明表}。"""
    blocks = _STYLE_BLOCK_RE.findall(_source())
    assert blocks, "页面没有内联 <style> 块"
    rules: dict[str, dict[str, str]] = {}
    for block in blocks:
        for selector, body in _RULE_RE.findall(_COMMENT_RE.sub("", block)):
            selector = selector.rsplit("{", 1)[-1].strip()
            decls = {name.strip().lower(): value.strip() for name, value in _DECL_RE.findall(body)}
            if decls and re.fullmatch(r"\.[A-Za-z0-9_-]+", selector):
                rules.setdefault(selector, {}).update(decls)
    return rules


def _resolve(value: str, tokens: dict[str, str]) -> str:
    match = _VAR_RE.search(value or "")
    if not match:
        return value or ""
    name, fallback = match.group(1), (match.group(2) or "")
    resolved = tokens.get(name, fallback)
    return resolved if _HEX_RE.fullmatch(resolved) else fallback


def _normalize_hex(color: str) -> str:
    """把 `#fff` 这类三位简写展开成六位 —— 只看六位会把它当成"解不开"。"""
    match = re.fullmatch(r"#([0-9a-fA-F]{3})", color or "")
    if match:
        return "#" + "".join(channel * 2 for channel in match.group(1))
    return color


def _px(value: str | None) -> float:
    assert value, "缺少尺寸声明"
    match = re.fullmatch(r"(-?[\d.]+)px", value.strip())
    assert match, f"只认识 px，实际是 {value!r}"
    return float(match.group(1))


def _luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def test_badge_and_state_colours_clear_aa_normal_text():
    """徽章/状态文字的实测对比度必须 >= 4.5:1（WCAG 2.1 AA 正文）。

    重排前这些徽章用的是 Bootstrap 5.2 才有的 `.bg-*-subtle`（本仓库加载 5.1.3），
    类不存在 → 底色透明，文字落回语义基色：`text-info` #0dcaf0 在白底只有 1.96:1。
    这里把 `var(--token, 兜底)` 按 style.css 的 token 表解开，逐条实算对比度 ——
    光断言"用了 --color-*-text 这个 token"是拦不住把一个更"好看"的颜色写进兜底的。
    """
    tokens = _root_tokens()
    rules = _page_rules()
    targets = {
        selector: decls
        for selector, decls in rules.items()
        if selector.startswith((".um-badge-", ".um-state-"))
    }
    assert len(targets) >= 12, f"徽章/状态规则没找全，只找到 {sorted(targets)}"

    failures = []
    for selector, decls in sorted(targets.items()):
        background = decls.get("background") or decls.get("background-color") or "var(--color-surface, #fff)"
        foreground = _normalize_hex(_resolve(decls.get("color", ""), tokens))
        background = _normalize_hex(_resolve(background, tokens))
        assert _HEX_RE.fullmatch(foreground), f"{selector} 的文字色解不开：{decls.get('color')!r}"
        assert _HEX_RE.fullmatch(background), f"{selector} 的底色解不开：{background!r}"
        ratio = _contrast(foreground, background)
        if ratio < AA_NORMAL_TEXT:
            failures.append(f"{selector}: {foreground} on {background} = {ratio:.2f}:1")

    assert not failures, (
        f"以下徽章/状态配色的实测对比度低于 AA 正文阈值 {AA_NORMAL_TEXT}:1：\n  "
        + "\n  ".join(failures)
    )


def test_badges_do_not_rely_on_a_bootstrap_version_this_app_does_not_load():
    """`bg-*-subtle` 是 Bootstrap 5.3 才加入的，本仓库 CDN 上锁在 5.1.3。

    这类类名写下去不会报错、也不会有构建告警，只是静静地什么都不做 ——
    徽章变成"没有底色的一段彩色小字"，线上就是这样。所以钉死这个类名。
    只看真正写在 `class="…"` 里的名字：说明用的注释里点名这个坑不算违规。

    版本依据：5.3 的 Background 文档把 `.bg-*-subtle` 列在 `.bg-*` 系列里，
    而 5.2 的迁移说明 "New utilities" 一节并没有这一族（本机无法直连 CDN
    取 5.1.3 的文件核对，这里是文档依据）。修好之后这条依赖已经不存在 ——
    徽章走页面自己的 token 规则，与 CDN 上是哪个小版本无关。
    """
    applied = {name for value in _CLASS_ATTR_RE.findall(_source()) for name in value.split()}
    offenders = sorted(name for name in applied if re.fullmatch(r"bg-.+-subtle", name))
    assert not offenders, (
        f"模板里又出现了 Bootstrap 5.1.3 不存在的工具类：{offenders}"
    )
    # 徽章走页面自己的 token 规则，不再用裸的 .badge + 语义工具类组合
    assert "badge" not in applied, "徽章又回到 Bootstrap .badge + 语义工具类的写法了"
