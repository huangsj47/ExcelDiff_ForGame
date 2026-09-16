# -*- coding: utf-8 -*-
"""第二条 XSS 链路：周版本完整 diff 的「渲染层未转义 + 汇点 eval」。

## 与 DIFF-B03（Excel 表名进内联 onclick）是**不同**的代码路径

* DIFF-B03：服务端 Jinja 模板 + static/js 把不可信表名拼进内联事件处理器。
* 本链路：`services/diff_render_helpers.py` 渲染 diff HTML 时把不可信文本
  （文件名、被删文件内容、hunk 头）**未转义**地拼进 HTML；而
  `templates/weekly_version_full_diff.html` 在插入这份 HTML 之后，
  又把里面所有 `<script>` 逐个 `eval(script.textContent)`。

汇点让严重性升级：这条路径不需要"点在某个元素上"才触发，**插入即执行**。
两条触发路径：(a) 仓库里有文件在本周窗口内被整体删除且内容含 `<script>…</script>`；
(b) 仓库里存在文件名含 HTML 的文件（git 允许路径含 `<>`），任何非删除 diff 都会渲染文件头。

## 同文件里兄弟函数都做了转义

`render_github_style_diff()` / `render_deleted_content_details()` /
`_render_truncated_notice_html()` 都调了 `html.escape`，只有下面几处漏了 ——
所以这是遗漏，不是设计。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services.diff_render_helpers import (
    render_deleted_file_content,
    render_excel_diff_html,
    render_git_diff_content,
    render_new_file_content,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SINK_TEMPLATE = PROJECT_ROOT / "templates" / "weekly_version_full_diff.html"

# 无害载荷：只写一个变量，不触碰任何真实数据
IMG_PAYLOAD = "x<img src=q onerror=window.__qa=1>.xlsx"
QUOTE_PAYLOAD = 'a"><img src=q onerror="window.__qa=1">.xlsx'
SCRIPT_PAYLOAD = "<script>window.__qa=1</script>"

_TAG_RE = re.compile(r"<([a-zA-Z][^\s/>]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>", re.S)


def _tag_names(markup: str):
    return [m.group(1).lower() for m in _TAG_RE.finditer(markup)]


def _attr_names(markup: str):
    names = []
    for match in _TAG_RE.finditer(markup):
        for attr in re.finditer(r'([^\s=/>]+)\s*=\s*"[^"]*"', match.group(2)):
            names.append(attr.group(1).lower())
    return names


_PAYLOAD_NODE_RE = re.compile(
    r'<script type="application/json"[^>]*data-weekly-excel-diff="1"[^>]*>(.*?)</script>',
    re.S,
)


def _outside_payload(markup: str) -> str:
    """去掉 JSON 载荷标签的**内容**再扫描。

    `<script>` 元素的内容是 raw text（script data state），浏览器不会把它当 HTML
    解析 —— 所以载荷字符串里出现 `<img onerror>` 是惰性的。真正要防的是它**逃出**
    标签（即内容里出现原始 `</script>`），那条由 test_excel_payload_cannot_break_out
    单独断言。
    """
    return _PAYLOAD_NODE_RE.sub(
        '<script type="application/json" data-weekly-excel-diff="1"></script>', markup
    )


def _inline_handler_attrs(markup: str):
    found = []
    for match in _TAG_RE.finditer(markup):
        for attr in re.finditer(r'([^\s=/>]+)\s*=\s*"[^"]*"', match.group(2)):
            if attr.group(1).lower().startswith("on"):
                found.append((match.group(1).lower(), attr.group(1).lower()))
    return found


# ==========================================================================
# 1. 文件名进入文件头（render_new_file_content / render_git_diff_content）
# ==========================================================================


def test_new_file_content_escapes_the_file_name_in_the_header():
    """文件名里的 `<img onerror>` 不能变成真标签。"""
    out = render_new_file_content("hello\n", IMG_PAYLOAD, "deadbeef")

    assert "img" not in _tag_names(out), (
        f"文件名里的 <img> 被渲染成了真标签：\n{out[:400]}"
    )
    assert _inline_handler_attrs(out) == [], (
        f"文件名把内联处理器顶成了新属性：{_inline_handler_attrs(out)}"
    )
    # 不能靠"把文件名删掉"通过：它要原样（转义后）显示出来
    assert "x&lt;img src=q onerror=window.__qa=1&gt;.xlsx" in out, (
        f"文件名没有作为文本渲染出来：\n{out[:400]}"
    )


def test_git_diff_content_escapes_the_file_name_in_the_header():
    """同一个文件头模板，普通（非删除）diff 也走这条。"""
    out = render_git_diff_content("+added line\n", IMG_PAYLOAD, "base", "head")

    assert "img" not in _tag_names(out), (
        f"文件名里的 <img> 被渲染成了真标签：\n{out[:400]}"
    )
    assert _inline_handler_attrs(out) == []
    assert "x&lt;img src=q onerror=window.__qa=1&gt;.xlsx" in out


# ==========================================================================
# 2. 被删文件的原始内容进入预览（render_deleted_file_content）
# ==========================================================================


def test_deleted_file_preview_escapes_script_tags_from_file_content():
    """被删文件内容里的 <script> 必须只作为文本显示。"""
    diff = f"@@ -1,2 +0,0 @@\n-{SCRIPT_PAYLOAD}\n-old\n"
    out = render_deleted_file_content(diff, "normal.xlsx")

    assert "script" not in _tag_names(out), (
        "被删文件内容里的 <script> 变成了真标签 —— 而宿主页面会对插入内容 eval，"
        f"等于插入即执行：\n{out[:600]}"
    )
    assert "&lt;script&gt;window.__qa=1&lt;/script&gt;" in out, (
        "删除内容预览没有把内容作为文本显示出来"
    )


def test_deleted_file_preview_escapes_a_quoted_file_name():
    """文件名里的 `"` 不能逃出属性 / 造出新标签。"""
    out = render_deleted_file_content("@@ -1 +0,0 @@\n-x\n", QUOTE_PAYLOAD)

    assert "img" not in _tag_names(out), (
        f"带引号的文件名把 <img> 顶成了真标签：\n{out[:600]}"
    )
    # 该函数自身有一处静态的 onclick="showDeletedContent()"（不是注入点），
    # 所以断言的是"载荷里的 onerror 没有变成属性"。
    assert "onerror" not in _attr_names(out), (
        f"带引号的文件名顶出了 onerror 属性：{_attr_names(out)}"
    )
    assert "a&quot;&gt;&lt;img src=q onerror=&quot;window.__qa=1&quot;&gt;.xlsx" in out, (
        f"文件名没有作为转义后的文本显示：\n{out[:600]}"
    )


def test_deleted_file_previous_version_url_encodes_the_path():
    """文件名是仓库里的路径：进 URL 必须 urlencode，否则 `&` 能改写 query。"""
    class _Config:
        id = 7

    class _Cache:
        base_commit_id = "abc123"

    out = render_deleted_file_content(
        "@@ -1 +0,0 @@\n-x\n", "a&commit_id=evil#frag.xlsx", _Config(), _Cache()
    )

    match = re.search(r'href="([^"]*file-previous-version[^"]*)"', out)
    assert match, f"没有渲染出「查看上一版本」链接：\n{out[:400]}"
    href = match.group(1)
    assert "&amp;commit_id=evil" not in href and "&commit_id=evil" not in href, (
        f"路径里的 `&` 逃进了 query，可以改写参数：{href}"
    )
    assert "%26commit_id%3Devil" in href, f"路径没有被 urlencode：{href}"
    assert href.startswith("/weekly-version-config/7/file-previous-version?file_path=")


# ==========================================================================
# 3. Excel 合并 diff：不再产出可执行内联脚本
# ==========================================================================


def _excel_payload(sheet_name="Sheet1"):
    return {
        "type": "excel",
        "sheets": {
            sheet_name: {
                "has_changes": True,
                "headers": ["id", "name"],
                "rows": [{"row_number": 2, "status": "modified", "data": {"id": "1"}}],
            }
        },
    }


def _payload_node(markup: str):
    match = re.search(
        r'<script type="application/json"[^>]*data-weekly-excel-diff="1"[^>]*>(.*?)</script>',
        markup,
        re.S,
    )
    assert match, f"没有找到 JSON 数据载荷节点：\n{markup[:600]}"
    return match.group(1)


def test_excel_render_emits_no_executable_inline_script():
    """载荷必须是 type="application/json"（浏览器不执行），不能是 JS。"""
    out = render_excel_diff_html(_excel_payload(), "Config/A.xlsx")

    executable = [
        m.group(1)
        for m in re.finditer(r"<script(?![^>]*application/json)([^>]*)>", out, re.I)
    ]
    assert executable == [], (
        f"渲染层仍然产出可执行内联脚本（宿主页面会 eval 它）：{executable}"
    )
    assert "window.weeklyExcelDiffData" not in out, (
        "渲染层不该再直接给 window 赋值 —— 那是内联脚本形态"
    )
    assert 'type="application/json"' in out, "数据载荷标签不见了"


def test_excel_payload_cannot_break_out_of_the_script_tag():
    """`json.dumps` 不转义 `</script>`：sheet 名里的它必须被中和。"""
    payload = _excel_payload("</script><img src=q onerror=window.__qa=1>")
    out = render_excel_diff_html(payload, "Config/A.xlsx")

    raw = _payload_node(out)
    assert "</script" not in raw.lower(), (
        f"载荷里出现了原始的 `</script>`，会提前闭合标签：{raw[:300]}"
    )
    # 载荷之外（真正的 HTML 上下文）不能出现被顶出来的标签
    assert "img" not in _tag_names(_outside_payload(out)), (
        f"sheet 名逃出了 script 标签，落回 HTML 上下文：\n{out[:600]}"
    )


def test_excel_payload_round_trips_so_the_display_still_works():
    """转义不能损坏数据：页面 JSON.parse 出来必须与原始 payload 等价。

    这条防的是"用把数据弄坏/删掉的方式让上面几条变绿"。
    """
    payload = _excel_payload("</script>危险表名")
    out = render_excel_diff_html(payload, "Config/A.xlsx")

    parsed = json.loads(_payload_node(out))
    assert parsed["sheets"] == payload["sheets"], "载荷解析回来与原始数据不一致"
    assert parsed["type"] == "excel"


def test_excel_render_still_keeps_the_dom_hooks_init_depends_on():
    """初始化路径依赖的 DOM 锚点不能被顺手删掉。"""
    out = render_excel_diff_html(_excel_payload(), "Config/A.xlsx")

    for anchor in ('id="excel-sheet-tabs"', 'id="excel-content"', "excel-diff-wrapper"):
        assert anchor in out, f"渲染结果缺少 {anchor}，" "周版本 Excel 合并 diff 会白屏"


# ==========================================================================
# 4. 汇点：宿主页面不得再执行插入内容里的脚本
# ==========================================================================


@pytest.fixture(scope="module")
def sink_source() -> str:
    return SINK_TEMPLATE.read_text(encoding="utf-8")


def test_sink_no_longer_evals_inserted_scripts(sink_source):
    """`eval(script.textContent)` 这个汇点必须消失（注释里提到旧实现不算）。"""
    code = re.sub(r"//[^\n]*", "", sink_source)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)

    assert "eval(" not in code, "宿主页面仍然对插入内容执行 eval"
    assert "querySelectorAll('script')" not in code, (
        "仍在搜集插入内容里的 <script> 并执行"
    )


def test_sink_reads_the_payload_with_json_parse(sink_source):
    """新路径：显式 JSON.parse 非可执行载荷，再走原来的初始化调用。"""
    assert "data-weekly-excel-diff" in sink_source
    assert "JSON.parse" in sink_source
    # 初始化时序与旧实现一致：数据就绪 → initWeeklyExcelDiffWhenReady
    assert "window.initWeeklyExcelDiffWhenReady()" in sink_source
    assert "window.weeklyExcelDiffData = JSON.parse" in sink_source
