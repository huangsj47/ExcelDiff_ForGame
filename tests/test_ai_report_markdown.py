# -*- coding: utf-8 -*-
"""AI 报告 Markdown 渲染的守卫。

## 为什么是自己写的渲染器

汇入的是**外部模型返回的不可信文本**，而这个仓库没有 CSP、也没有任何消毒器
（`static/js/` 下只有 main.js / diff-handlers.js，全仓 grep 不到 DOMPurify）。
而报告的结构是固定子集：七个一级标题 + `-` 列表 + 少量 `**加粗**`。

`static/js/ai-report-markdown.js` 的做法是**先整体转义、再在白名单上套标签** ——
输出里的每个 `<` 都来自渲染器自己的字面量，模型给的内容永远以实体形式出现。
XSS 由构造保证，不靠消毒器兜底。这里用真实 node 跑那些用例，而不是读源码猜。

## 另一半守的是什么

三个模板必须把**成功报告**交给渲染器，而把占位语 / 进行中 / 失败信息继续按纯文本走
（失败信息里带网关原文，里面有尖括号和竖线，当 Markdown 解析只会更乱）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RENDERER = PROJECT_ROOT / "static" / "js" / "ai-report-markdown.js"

TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
    "templates/commit_diff_new.html",
)

_DRIVER = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// 间接 eval：在全局作用域求值，渲染器的 IIFE 会把 AiReportMarkdown 挂到 globalThis
(0, eval)(src);
const render = globalThis.AiReportMarkdown.render;
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map(function (c) { return { name: c.name, html: render(c.md) }; });
process.stdout.write(JSON.stringify(out));
"""

CASES = [
    {"name": "h1", "md": "# 变更理解"},
    {"name": "h3", "md": "### 小标题"},
    {"name": "h7_is_not_a_heading", "md": "####### 七个井号"},
    {"name": "ul_grouped", "md": "- 甲\n- 乙\n- 丙"},
    {"name": "ol_grouped", "md": "1. 甲\n2. 乙"},
    {"name": "list_split_by_blank_line", "md": "- 甲\n\n- 乙"},
    {"name": "bold", "md": "这是 **重点** 内容"},
    {"name": "inline_code", "md": "见 `TrapCfgMod` 文件"},
    {"name": "quote", "md": "> 信息不足"},
    {"name": "paragraph_join", "md": "第一行\n第二行"},
    {"name": "risk_lines", "md": "R1（critical）甲\nR2（high）乙\nR3（medium）丙"},
    {"name": "blank_lines_collapse", "md": "甲\n\n\n\n乙"},
    {"name": "fence", "md": "```\n- 这不是列表\n# 这不是标题\n```"},
    {"name": "unclosed_fence", "md": "```\ncode 没闭合"},
    {"name": "mixed_report", "md": "# 标题\n\n- 甲 **粗** `码`\n> 引用\n"},
    # --- XSS ---
    {"name": "xss_raw_img", "md": '<img src=x onerror="alert(1)">'},
    {"name": "xss_script_tag", "md": "<script>alert(1)</script>"},
    {"name": "xss_bold_wrapped", "md": "**<script>alert(1)</script>**"},
    {"name": "xss_in_heading", "md": "# <script>alert(1)</script>"},
    {"name": "xss_in_list", "md": "- <img src=x onerror=alert(1)>"},
    {"name": "xss_in_code_span", "md": "`<script>alert(1)</script>`"},
    {"name": "xss_attribute_break", "md": '**" onmouseover="alert(1)**'},
    {"name": "xss_in_fence", "md": "```\n<script>alert(1)</script>\n```"},
    {"name": "xss_svg", "md": "<svg/onload=alert(1)>"},
    {"name": "empty", "md": ""},
    {"name": "whitespace_only", "md": "   \n  \n"},
]


@pytest.fixture(scope="module")
def rendered() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实渲染断言")
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "driver.js"
        driver.write_text(_DRIVER, encoding="utf-8")
        cases = Path(tmp) / "cases.json"
        cases.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver), str(RENDERER), str(cases)],
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"Node 执行渲染器失败：\n{proc.stdout}\n{proc.stderr}"
    return {item["name"]: item["html"] for item in json.loads(proc.stdout)}


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------

def test_headings_become_heading_elements(rendered):
    assert rendered["h1"] == "<h1>变更理解</h1>"
    assert rendered["h3"] == "<h3>小标题</h3>"
    # 七个井号不是标题（CommonMark 最多六级），应当原样显示而不是吞掉
    assert "<h7>" not in rendered["h7_is_not_a_heading"]
    assert "七个井号" in rendered["h7_is_not_a_heading"]


def test_consecutive_bullets_become_one_list(rendered):
    """这条守的是「不能逐行渲染」。

    SSE 是一行一行推的。若每来一行就渲染一次，`- 甲` / `- 乙` / `- 丙` 会渲染成
    三个各自独立的单元素列表 —— 结构就散了。整体渲染才是一个 <ul> 三个 <li>。
    """
    html = rendered["ul_grouped"]
    assert html.count("<ul>") == 1, f"列表被拆成了多个：{html}"
    assert html.count("<li>") == 3
    assert html == "<ul><li>甲</li><li>乙</li><li>丙</li></ul>"


def test_ordered_and_unordered_lists_are_distinguished(rendered):
    assert rendered["ol_grouped"].count("<ol>") == 1
    assert "<li>甲</li>" in rendered["ol_grouped"]
    assert "<ul>" not in rendered["ol_grouped"]


def test_a_blank_line_ends_a_list(rendered):
    assert rendered["list_split_by_blank_line"].count("<ul>") == 2


def test_inline_markers(rendered):
    assert "<strong>重点</strong>" in rendered["bold"]
    assert "<code>TrapCfgMod</code>" in rendered["inline_code"]


def test_blockquote(rendered):
    assert "<blockquote>信息不足</blockquote>" in rendered["quote"]


def test_a_line_break_in_the_source_stays_a_line_break(rendered):
    """**换行不许被吃掉**（2026-09-25 产品决定，真机 run 70 实测）。

    这份报告的作者（模型与平台）按「一行一条」写清单：一行一条风险、一行一条测试
    建议。按标准 Markdown 的软换行规则把它们并用空格接成一段，读者拿到的是连成一片
    的长文 —— 真机 run 70 的「上次遗留（仍成立）」**25 行 / 2195 字**被并成一段，
    正是用户报的「很多报告内容没有换行，不方便阅读」。

    段落仍然是**一个** `<p>`（换行不该变成分段），只是行与行之间有 `<br>`。
    与「不许用 `white-space: pre-wrap`」不冲突：那条禁的是把标签之间的空白当正文渲染。
    """
    html = rendered["paragraph_join"]
    assert html.count("<p>") == 1, f"软换行被拆成了多段：{html}"
    assert "第一行<br>第二行" in html, f"行内换行被吃掉了：{html}"


def test_a_checklist_written_one_item_per_line_keeps_its_lines(rendered):
    """真实形态：清单每条一行（run 70 的风险清单就是 `R10…\\nR11…\\nR12…`）。

    这条钉的是**用户实际读到的那一份**：并起来的时候，25 条风险会连成一段 2195 字的
    长文，读者没法逐条看。
    """
    html = rendered["risk_lines"]
    assert html.count("<br>") == 2, f"三条清单没保住三行：{html}"
    for item in ("R1（critical）甲", "R2（high）乙", "R3（medium）丙"):
        assert item in html, f"{item} 不见了：{html}"


def test_blank_runs_do_not_produce_empty_paragraphs(rendered):
    assert "<p></p>" not in rendered["blank_lines_collapse"]


def test_a_fenced_block_is_not_parsed_as_markdown(rendered):
    """围栏里的 `#` 和 `-` 是代码，不是标题和列表。"""
    html = rendered["fence"]
    assert "<h1>" not in html, f"围栏里的 # 被当标题了：{html}"
    assert "<ul>" not in html, f"围栏里的 - 被当列表了：{html}"
    assert "- 这不是列表" in html


def test_an_unclosed_fence_still_shows_its_content(rendered):
    """模型偶尔会漏掉收尾围栏 —— 不能把后面整段内容吞掉。"""
    assert "code 没闭合" in rendered["unclosed_fence"]


def test_a_realistic_report_gets_all_its_pieces(rendered):
    html = rendered["mixed_report"]
    assert "<h1>标题</h1>" in html
    assert "<ul>" in html and "<strong>粗</strong>" in html and "<code>码</code>" in html
    assert "<blockquote>" in html


# --------------------------------------------------------------------------
# XSS：这是本文件最重要的一组
# --------------------------------------------------------------------------

ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li",
    "strong", "code", "blockquote", "pre", "br",
}

_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)")
_ANY_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _tags_outside_whitelist(html: str) -> list:
    return [m.group(0) for m in _TAG_RE.finditer(html)
            if m.group(1).lower() not in ALLOWED_TAGS]


def _assert_only_whitelisted_tags(html: str, case: str) -> None:
    """最硬的一条不变量：拿掉白名单标签后，输出里不该再剩下任何 `<`。

    「转义后仍看得见 `onerror` 这个词」是**正常**的（它是文本，不是标签），
    所以不能拿危险词做子串匹配 —— 要判定的是**有没有形成真标签**。
    """
    bad = _tags_outside_whitelist(html)
    assert not bad, f"{case} 渲染出了白名单外的标签：{bad}，完整输出：{html}"
    residue = _ANY_TAG_RE.sub("", html)
    assert "<" not in residue, f"{case} 有没被识别成标签的裸 `<`：{residue}"


@pytest.mark.parametrize("case", [c["name"] for c in CASES if c["name"].startswith("xss_")])
def test_model_supplied_markup_can_never_become_a_tag(rendered, case):
    """模型给的内容里，任何标签都只能以实体形式出现。

    这是「先转义、再套白名单」的直接结果：能被写成真标签的 `<` 只有渲染器自己
    那几个字面量。
    """
    html = rendered[case]
    _assert_only_whitelisted_tags(html, case)
    # 内容本身要还在（是「转义显示」而不是「被丢掉」）
    assert "&lt;" in html or "&quot;" in html, (
        f"{case} 既没有转义痕迹也没内容，可能把内容整个丢了：{html}"
    )


def test_the_renderer_emits_no_attributes_at_all(rendered):
    """渲染器产出的标签**一个属性都没有**，所以不存在「逃出属性上下文」。

    这条比逐个比对 `onmouseover=` 之类的危险词可靠：只要输出里出现了任何带属性的
    标签，就说明有东西不是渲染器自己写的。
    """
    for name, html in rendered.items():
        for tag in _ANY_TAG_RE.findall(html):
            assert tag.strip() in {"<" + t + ">" for t in ALLOWED_TAGS} | \
                                  {"</" + t + ">" for t in ALLOWED_TAGS}, (
                f"{name} 输出了带内容的标签（可能含属性）：{tag!r}"
            )


def test_an_attribute_break_cannot_escape_an_attribute(rendered):
    """`"` 必须被转义，否则 `**" onmouseover="x**` 这类能逃出属性上下文。"""
    html = rendered["xss_attribute_break"]
    assert "&quot;" in html, f"双引号没被转义：{html}"
    assert "onmouseover=&quot;" in html, "引号被转义了，但内容没有以实体形式保留"
    _assert_only_whitelisted_tags(html, "xss_attribute_break")


def test_the_renderer_only_emits_whitelisted_tags(rendered):
    """整体不变量：所有用例的输出里，只允许出现白名单标签。"""
    for name, html in rendered.items():
        _assert_only_whitelisted_tags(html, name)


def test_unsupported_markdown_degrades_to_visible_text(rendered):
    """刻意不支持的写法（链接/图片/表格）要退化成可见文字，不能消失、也不能变标签。

    这一条是「诚实降级」的守卫：宁可显示得朴素，也不能把内容弄丢。
    """
    cases = [{"name": "link", "md": "见 [文档](http://x/y) 说明"},
             {"name": "table", "md": "| a | b |\n| - | - |\n| 1 | 2 |"}]
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node")
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "d.js"
        driver.write_text(_DRIVER, encoding="utf-8")
        cf = Path(tmp) / "c.json"
        cf.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(["node", str(driver), str(RENDERER), str(cf)],
                              capture_output=True, text=True, timeout=60)
    out = {i["name"]: i["html"] for i in json.loads(proc.stdout)}
    assert "<a " not in out["link"] and "<a>" not in out["link"]
    assert "文档" in out["link"], "链接的文字被弄丢了"
    assert "http://x/y" in out["link"], "链接地址被弄丢了"
    assert "<table" not in out["table"]
    assert "1" in out["table"] and "2" in out["table"], "表格内容被弄丢了"


# --------------------------------------------------------------------------
# 三个模板的接线
# --------------------------------------------------------------------------

# 落库正文进 `set*Report` 的两种写法：
#   setAiReport(result.response_text)
#   setAiReport(AiContextNotice.withContextNotice(result.response_text, result.result))
# 后一种是 2026-09-19 之后的正解：降级 / 压过上下文的提示必须**跟着正文一起**渲染，
# 否则拉一次 `/latest` 就把那几行整体替换没了（见 `static/js/ai_context_notice.js`）。
# 这条守卫要管的是「有没有交给渲染器」，不是「参数长什么样」—— 所以按调用形态匹配，
# 而不是钉死一整行字面量（钉死的话，正当的包装会被判成「没走 Markdown」）。
_REPORT_CALL_RE = re.compile(
    r"set(?:Weekly)?AiReport\(\s*(?:AiContextNotice\.withContextNotice\(\s*)?result\.response_text"
)


def _code(src: str) -> str:
    """先剥注释再断言：本仓库会把要禁掉的写法原样写进注释里（踩过坑）。"""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_success_report_goes_through_the_markdown_renderer(template):
    """成功报告必须交给渲染器；这条拦的是「渲染器写好了但没接上」。"""
    src = (PROJECT_ROOT / template).read_text(encoding="utf-8")
    assert "AiReportMarkdown.render(" in src, f"{template} 没有调用渲染器"
    assert _REPORT_CALL_RE.search(_code(src)), (
        f"{template} 的整体报告没有走 Markdown 路径（仍在用纯文本 set*Output）"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_placeholders_and_errors_stay_plain_text(template):
    """占位语与失败信息必须继续走 textContent。

    失败信息会带上网关/模型返回的原文，里面可能有尖括号、竖线、井号；
    当 Markdown 解析只会显示得更乱，还可能把一个错误串渲染成标题。
    """
    src = (PROJECT_ROOT / template).read_text(encoding="utf-8")
    assert "'error')" in src, f"{template} 的失败态把 variant 弄丢了"
    # set*Output 仍是 textContent 实现
    assert "output.textContent = text;" in src, (
        f"{template} 的 set*Output 不再写 textContent —— 占位语/失败信息被当 Markdown 了"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_output_container_is_not_a_pre_element(template):
    """`<pre>` 的 white-space:pre-wrap 与渲染出来的块级元素打架。"""
    src = (PROJECT_ROOT / template).read_text(encoding="utf-8")
    assert 'class="ai-analysis-output is-empty"' in src
    import re
    assert not re.search(r'<pre[^>]*ai-analysis-output', src), (
        f"{template} 的 AI 正文容器还是 <pre>"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_report_area_does_not_keep_pre_wrap(template):
    """渲染后是块级元素，pre-wrap 会把标签之间的空白也当正文渲染。"""
    src = (PROJECT_ROOT / template).read_text(encoding="utf-8")
    block_start = src.find("/* ===== AI 抽屉样式 开始")
    block_end = src.find("/* ===== AI 抽屉样式 结束 ===== */")
    assert block_start != -1 and block_end != -1
    block = src[block_start:block_end]
    # 只检查 .ai-analysis-output 自己那条规则块，`pre` 元素的 white-space 是另一回事
    # 剥离 /* */ 注释再断言：注释里会**举例**写出这个要禁掉的写法
    rule_start = block.find(".ai-analysis-output {")
    rule_end = block.find("}", rule_start)
    rule = re.sub(r"/\*.*?\*/", " ", block[rule_start:rule_end], flags=re.S)
    assert "white-space: pre-wrap" not in rule, (
        ".ai-analysis-output 上仍有 white-space: pre-wrap"
    )


def test_the_renderer_is_loaded_on_every_page():
    """渲染器由 base.html 统一引入；三个模板都 extends base.html，缺了就是 ReferenceError。"""
    src = (PROJECT_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "js/ai-report-markdown.js" in src
    for template in TEMPLATES:
        head = (PROJECT_ROOT / template).read_text(encoding="utf-8")[:200]
        assert "extends \"base.html\"" in head, f"{template} 不再继承 base.html"


# --------------------------------------------------------------------------
# 机器裁决**不可能**靠 HTML 注释藏起来（AI-P0-05 的现场证据）
# --------------------------------------------------------------------------
# 这一段拿**真实的**规范正文（平台自己渲染的那几节）跑一遍真渲染器。它钉住的是
# 「为什么那行注释必须从契约里删掉」：这个渲染器先整体转义、再套白名单，注释会变成
# 一段可见文字。XSS 防护靠这个构造（`test_model_supplied_markup_can_never_become_a_tag` 守着它），
# **不许为了藏注释而开放原始 HTML**。


def _render(markdown: str) -> str:
    """拿真实渲染器渲染一段 markdown（node 不在就跳过）。"""
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实渲染断言")
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "d.js"
        driver.write_text(_DRIVER, encoding="utf-8")
        cases = Path(tmp) / "c.json"
        cases.write_text(
            json.dumps([{"name": "one", "md": markdown}], ensure_ascii=False),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(driver), str(RENDERER), str(cases)],
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"Node 执行渲染器失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)[0]["html"]


def test_an_html_comment_becomes_visible_text_not_hidden():
    """**这一条就是那 12,617 个字符的来源**：注释在页面上是看得见的文字。

    从前的设计假设「HTML 注释在 markdown 渲染里看不见」，而本渲染器为了防 XSS 会先把
    整个输入转义 —— 于是 `<!-- ai-verify-ruling: {...} -->` 原样显示给用户（实测 run 20
    的 `response_text` 里 35.3% 是它）。既然藏不住，它就不该写在正文里。
    """
    html = _render('<!-- ai-verify-ruling: {"changed": 1} --> 后面还有一句')

    assert "ai-verify-ruling" in html, "机器 json 的标记**看得见**（这正是当初的缺陷）"
    assert "&lt;!--" in html, "注释的开头应当以转义形态出现"
    assert "<!" not in html.replace("&lt;!", ""), "出现了真的注释节点"


def test_a_canonical_report_renders_without_any_machine_payload():
    """平台渲染的那份规范正文过一遍真渲染器：只有给人看的内容。"""
    from services.ai.verdict import (
        VERDICT_RETRACTED,
        VerifyVerdict,
        reduce_findings,
        render_ruling,
    )
    from tests.test_ai_verify_verdict import EVIDENCE_REF, _obj

    reduction = reduce_findings(
        [_obj()],
        verdicts=(
            VerifyVerdict(
                finding_id="F1",
                verdict=VERDICT_RETRACTED,
                reason="同一提交里生成文件已经删掉了",
                evidence_refs=(EVIDENCE_REF,),
            ),
        ),
    )
    html = _render(render_ruling(reduction, review_ran=True))

    assert "<h2>复核标注（平台）</h2>" in html, "那一节的标题没渲染成二级标题"
    assert "ai-verify-ruling" not in html
    assert "&lt;!" not in html, "规范正文里出现了注释（它会被显示出来）"
    assert "反证成立（撤销）" in html
