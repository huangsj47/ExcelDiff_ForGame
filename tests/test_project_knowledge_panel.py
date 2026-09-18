# -*- coding: utf-8 -*-
"""项目专属知识面板（`#aiKnowledgeModal`）的结构、无障碍与行为契约。

## 为什么是静态断言 + 一段真跑的 JS

这个模板要十来个上下文变量才渲染得出来，仓库里也没有浏览器。而这里要守的东西
改错了**几乎全是静默的**：`aiEl('xxx')` 返回 null 时不会抛错，只是那一段逻辑
一声不响地不执行（按钮点了没反应）；CSS 少一个 `}` 会让后面所有规则一起失效；
错误没落到具体输入框时用户只会看到一句「保存失败」。

所以分两层：

* 静态断言守「HTML 与 JS 之间的契约」（id 对得上、三态齐全、图标不隐形）；
* **真跑**几个纯函数（`aiKnowledgeNameProblem` / `parseAiKnowledgeFrontmatter`），
  而不是在测试里复刻一遍它们的逻辑 —— 复刻的那份只会证明「我抄对了」。

## 与既有用例的关系

本面板落在 `test_ai_config_template.py` / `test_ai_config_panel_design.py` 的切片
范围内，所以这里还要反着守一条：**它不许动那几条既有断言的计数**（分组数 4、
示例折叠数 2、`aria-busy` 写法 3 次、字段栅格只允许 `col-12`）。那几条守的是
AI 配置面板的契约，本面板用独立命名空间满足它们，而不是把数字改掉。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = "templates/merged_project_view.html"
NODE = shutil.which("node")

MODAL_START = '<div class="card mb-4" id="aiConfigCard">'
MODAL_END = "{% if not repositories %}"
JS_START = "// AI 分析配置：页面摘要行 + 居中模态框"
JS_END = "function normalizeRiskLevel(level) {"
PANEL_CSS_START = "/* ===================================================================\n       AI 分析配置面板"
PANEL_CSS_END = "/* ===== 响应式 ===== */"
# 本面板自己的 markdown 区块（从注释 banner 到 modal 结束）
KNOW_START = "<!-- ===== 项目专属知识"
KNOW_END = "    {% if not repositories %}"
# 本面板自己的 CSS 段
KNOW_CSS_START = "/* ---------- 项目专属知识面板 ----------"


def _read(rel=TEMPLATE) -> str:
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def _modal_html() -> str:
    text = _read()
    return text[text.index(MODAL_START) : text.index(MODAL_END)]


def _ai_script() -> str:
    text = _read()
    return text[text.index(JS_START) : text.index(JS_END)]


def _know_html() -> str:
    text = _read()
    return text[text.index(KNOW_START) : text.index(KNOW_END)]


def _know_css() -> str:
    panel = _read()
    style = panel[panel.index("<style>") : panel.index("</style>")]
    block = style[style.index(PANEL_CSS_START) : style.index(PANEL_CSS_END)]
    return block[block.index(KNOW_CSS_START) :]


def _know_script() -> str:
    """本面板自己那段 JS。

    锚点取本段 banner 里那行标题（`AI_KNOW_KINDS` 就在它下面），而不是第一个函数 ——
    取函数的话会漏掉定义在函数之前的那几张常量表，而它们正是「三类内容怎么被用到」
    这句承诺的载体。
    """
    script = _ai_script()
    anchor = "// 项目专属知识：列表 / 编辑器 / 三态"
    assert anchor in script, "找不到知识面板的脚本段 —— 锚点失效了"
    return script[script.index(anchor) :]


def _declarations(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _extract_function(name: str) -> str:
    """按大括号配对抠出一个函数声明 —— 下面要跑的是真实现，不是复刻。"""
    text = _read()
    match = re.search(r"function %s\s*\(" % re.escape(name), text)
    assert match, f"找不到 {name}"
    depth = 0
    for index in range(match.start(), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise AssertionError(f"{name} 的大括号不配对")


def _run_js(harness: str, payload) -> dict:
    if NODE is None:
        pytest.skip("本机没有 node，跳过（这是唯一能真跑这几个函数的方式）")
    script = harness + f"\nconst INPUT = {json.dumps(payload)};\n"
    script += """
const out = {};
for (const key of Object.keys(INPUT)) { out[key] = probe(INPUT[key]); }
process.stdout.write(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        temp_path = handle.name
    try:
        proc = subprocess.run(
            [NODE, temp_path], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        assert proc.returncode == 0, f"node 跑失败：{proc.stderr[:800]}"
        return json.loads(proc.stdout)
    finally:
        os.unlink(temp_path)


def _run_name_problem(cases) -> dict:
    harness = _extract_function("aiKnowledgeNameProblem") + """
function probe(value) { return aiKnowledgeNameProblem(value); }
"""
    return _run_js(harness, cases)


def _run_frontmatter(cases) -> dict:
    harness = _extract_function("parseAiKnowledgeFrontmatter") + """
function probe(value) { return parseAiKnowledgeFrontmatter(value); }
"""
    return _run_js(harness, cases)


# ==========================================================================
# 1. 入口与独立命名空间
# ==========================================================================


class TestTheEntryPoint:
    def test_there_is_a_button_on_the_ai_config_card(self):
        """入口必须和「修改配置」并排 —— 用户原本就是去那里找 AI 相关配置的。"""
        html = _modal_html()
        tag = re.search(r"<button[^>]*id=\"aiKnowledgeOpenBtn\"[^>]*>", html)
        assert tag, "AI 配置卡片上没有「项目专属知识」入口"
        assert 'data-bs-target="#aiKnowledgeModal"' in tag.group(0)
        assert 'data-bs-toggle="modal"' in tag.group(0)
        assert "项目专属知识" in html[html.index('id="aiKnowledgeOpenBtn"') :][:300]

    def test_the_modal_is_a_separate_one(self):
        """**独立模态框**，不是 AI 配置里的第五个分组。

        分组的数量、导航项与分组的一一对应、示例折叠数都已经被既有用例钉死
        （4 / 4 / 2），而知识包与配置的保存单位不同（一份文件一次 vs 改完一起）。
        """
        html = _modal_html()
        assert '<div class="modal fade" id="aiKnowledgeModal" tabindex="-1"' in html
        assert 'aria-labelledby="aiKnowledgeModalLabel"' in html
        assert 'class="modal-title" id="aiKnowledgeModalLabel"' in html
        assert "modal-dialog-centered" in html and "modal-dialog-scrollable" in html

    def test_the_panel_does_not_join_the_config_rail(self):
        """本面板不许往 AI 配置的分区导航里加项。"""
        know = _know_html()
        assert "data-ai-section" not in know, "知识面板动了 AI 配置的分区导航"
        assert "<fieldset" not in know, "知识面板加了 fieldset —— 分组数被既有用例钉在 4"
        assert "<details" not in know, "知识面板加了 details —— 示例折叠数被既有用例钉在 2"

    def test_the_panel_keeps_the_field_grid_convention(self):
        """栅格类只允许 `col-12`（既有用例对整张卡片的切片都这么要求）。"""
        know = _know_html()
        offenders = re.findall(r'class="col-(?!12\b)(?:md|sm|lg|xl)?-?\d*[^"]*"', know)
        assert not offenders, f"知识面板用了会被压成半列的栅格：{offenders}"

    def test_it_is_initialised(self):
        script = _read()
        assert "initAiKnowledge();" in script, "面板从来没有被初始化 —— 所有按钮都是死的"
        assert script.index("initAiKnowledge();") < script.index("// 初始化非活跃版本分页")

    def test_the_read_only_user_gets_no_write_affordances(self):
        """只读用户能看不能改；界面只是提示，服务端才是权威（写接口 403）。"""
        script = _know_script()
        body = script[script.index("function initAiKnowledge") :]
        assert "if (!aiCanEdit)" in body
        for element_id in ("aiKnowledgeNewDocBtn", "aiKnowledgeNewSkillBtn", "aiKnowledgeScaffoldBtn"):
            assert element_id in body[body.index("if (!aiCanEdit)") :][:600], (
                f"{element_id} 没有被只读态覆盖"
            )
        assert "仅项目管理员可修改项目专属知识" in body


# ==========================================================================
# 2. HTML 与 JS 的 id 契约
# ==========================================================================


def _js_referenced_ids() -> set:
    """从脚本里把本面板引用的 id 抠出来（不是两边各抄一份清单 —— 抄的会一起错）。"""
    return set(re.findall(r"aiEl\('(aiKnowledge[A-Za-z0-9_]*)'\)", _ai_script()))


class TestTheIdContract:
    def test_the_parser_itself_can_fail(self):
        """解析本身要能失败：锚点写歪了、函数被删了，都应该在这里炸掉。"""
        ids = _js_referenced_ids()
        assert len(ids) >= 20, f"只解析到 {len(ids)} 个 id，像是锚点失效了：{sorted(ids)}"
        assert "aiKnowledgeModal" in ids
        assert "aiKnowledgeSaveBtn" in ids

    def test_every_referenced_id_exists_in_the_markup(self):
        html = _modal_html()
        missing = [element_id for element_id in _js_referenced_ids() if f'id="{element_id}"' not in html]
        assert not missing, f"JS 会去找这些元素，但模板里没有：{sorted(missing)}"

    def test_every_editor_field_has_an_inline_error_container(self):
        """字段级错误要显示在对应输入框下方 —— 契约里字段是 `name/description/content`。"""
        html = _modal_html()
        script = _know_script()
        fields = dict(re.findall(r"(\w+):\s*'(aiKnowledge[A-Za-z0-9_]*)',", script))
        assert set(fields) == {"name", "description", "content"}, fields
        for dom_id in fields.values():
            assert f'id="{dom_id}Error"' in html, f"{dom_id} 没有内联错误位置"
            assert f'id="{dom_id}Help"' in html, f"{dom_id} 没有帮助文案位置"
            assert f'for="{dom_id}"' in html, f"{dom_id} 没有 label"
            tag = re.search(rf'<(?:input|textarea)[^>]*id="{dom_id}"[^>]*>', html, re.S)
            assert tag, f"找不到 {dom_id} 的控件标签"
            described = re.search(r'aria-describedby="([^"]*)"', tag.group(0))
            tokens = described.group(1).split() if described else []
            for expected in (f"{dom_id}Help", f"{dom_id}Error"):
                assert expected in tokens, f"{dom_id} 的 aria-describedby 少了 {expected}"

    def test_the_errors_are_rendered_not_just_toasted(self):
        """后端回的字段级错误必须摆到那一栏旁边，而不是只弹一句「保存失败」。"""
        script = _know_script()
        body = script[script.index("function showAiKnowledgeErrors") : script.index("const AI_KNOW_STATES")]
        assert "setAiKnowledgeFieldError(item.field" in body, "错误没有落到具体那一栏"
        assert "items.length" in body, "摘要里没说一共几处"
        assert "summary.focus();" in body, "失败后焦点没有移到摘要（读屏用户听不到）"
        assert "focusAiKnowledgeField(item.field);" in body, "摘要里的条目点不回对应控件"


# ==========================================================================
# 3. 三态齐全
# ==========================================================================


class TestTheThreeStates:
    def test_loading_error_and_empty_all_exist(self):
        html = _modal_html()
        for element_id in (
            "aiKnowledgeLoading",
            "aiKnowledgeLoadError",
            "aiKnowledgeEmpty",
            "aiKnowledgeGroups",
        ):
            assert f'id="{element_id}"' in html, f"少了 {element_id}"

    def test_the_failure_state_offers_a_retry(self):
        html = _modal_html()
        tag = re.search(r"<div[^>]*id=\"aiKnowledgeLoadError\"[^>]*>", html)
        assert tag, "没有失败提示容器"
        assert 'role="alert"' in tag.group(0), "读屏软件不会播报读取失败"
        assert "d-none" in tag.group(0), "失败提示初始应该隐藏"
        retry = re.search(r"<button[^>]*id=\"aiKnowledgeLoadRetryBtn\"[^>]*>", html)
        assert retry, "失败态没有重试入口"
        assert "重试" in html[html.index('id="aiKnowledgeLoadRetryBtn"') :][:200]
        script = _know_script()
        assert "aiKnowledgeLoadRetryBtn" in script, "重试按钮没有接线"

    def test_the_loading_state_is_announced(self):
        html = _modal_html()
        tag = re.search(r"<div[^>]*id=\"aiKnowledgeLoading\"[^>]*>", html)
        assert tag and 'aria-hidden' not in tag.group(0)
        # 状态行与列表加载态都要能被读屏播报，且不抢焦点
        for element_id in ("aiKnowledgeLoading", "aiKnowledgeStatus", "aiKnowledgeFeedback"):
            chunk = html[html.index(f'id="{element_id}"') - 200 : html.index(f'id="{element_id}"') + 120]
            assert 'aria-live="polite"' in chunk, f"{element_id} 不是 polite 的 live region"

    def test_the_empty_state_gives_a_next_step(self):
        html = _html = _modal_html()
        assert "还没有维护项目专属知识" in html
        # 空状态必须指向一个真实存在的按钮文案，否则用户不知道点哪
        assert "从模板创建知识包" in html
        assert 'id="aiKnowledgeScaffoldBtn"' in html


# ==========================================================================
# 4. 用户看得懂的三类说明
# ==========================================================================


class TestTheCopyExplainsTheDifference:
    def test_the_three_kinds_are_explained(self):
        """「清单」与「按需读取」的区别用户看不懂就会乱放内容 —— 必须在界面上说清。"""
        html = _know_html()
        assert "知识包清单" in html and "KNOWLEDGE.md" in html
        assert "按需读取" in html, "没有说明知识文档是按需读取的"
        assert "只有被它列出的文档才会被读到" in html, "没有说明清单是「目录」"
        assert "子 skill" in html

    def test_the_rows_explain_how_each_kind_is_used(self):
        """列表每行都要有一句「它在分析里怎么被用到」。"""
        script = _know_script()
        assert "entry.usage" in script, "行里没有用后端回来的用途说明"
        assert "defaultUsage" in script, "没有兜底用途说明"
        for kind in ("manifest", "reference", "skill"):
            assert f"{kind}:" in script

    def test_it_says_the_platform_rules_are_not_here(self):
        """用户很容易把「项目知识」与「平台评审规程」搞混，改错地方等于白改。"""
        html = _know_html()
        assert "平台内置的评审规程" in html
        assert "不在这一页" in html
        assert "由平台维护" in html

    def test_it_says_the_content_is_not_in_version_control(self):
        """`.gitignore` 里 `skills/projects/*/` 是被忽略的 —— 这是设计如此，要说清。"""
        html = _know_html()
        assert "本部署实例的数据" in html
        assert "不进版本库" in html
        assert "G119" in html, "没有举出「随仓库分发的那一份」是哪个"

    def test_it_warns_that_old_conclusions_will_be_marked_stale(self):
        """保存会改变 `skill_revision`，于是此前的结论会被标成「旧」。必须如实说。"""
        html = _know_html()
        assert "此前的分析结论会被标记为「旧」" in html
        script = _know_script()
        assert "此前的分析结论会被标记为「旧」" in script, "保存成功的提示里没有这句话"


# ==========================================================================
# 5. 删除的二次确认
# ==========================================================================


class TestTheDeleteConfirmation:
    def _delete_body(self) -> str:
        script = _know_script()
        return script[script.index("async function deleteAiKnowledgeEntry") : script.index("async function scaffoldAiKnowledge")]

    def test_it_asks_before_deleting(self):
        body = self._delete_body()
        assert "window.confirm(" in body, "删除没有二次确认"
        assert "if (!window.confirm(" in body, "确认被拒后没有返回"

    def test_it_says_what_will_be_removed(self):
        body = self._delete_body()
        assert "references/" in body, "没有说清删的是哪一份文件"
        assert "整个目录" in body, "删子 skill 没有说清会连目录一起删"

    def test_it_says_it_will_rewrite_the_manifest(self):
        """删除会顺手摘掉 KNOWLEDGE.md 里的引用行 —— 用户必须提前知道。

        不摘那一行的话知识包会变成不合法的（引用了一份不存在的文档），分析连
        清单里其他文档一起读不到。所以这个「顺手改你的文件」是必须的，
        正因为必须，才更要说出来。
        """
        body = self._delete_body()
        assert "KNOWLEDGE.md" in body
        assert "移除" in body

    def test_it_says_it_cannot_be_undone(self):
        body = self._delete_body()
        assert "无法从界面上恢复" in body, "没有说明能不能恢复"


# ==========================================================================
# 6. 视觉与无障碍硬约束
# ==========================================================================


class TestTheVisualContract:
    def test_no_emoji_anywhere_in_the_panel_or_its_script(self):
        emoji = re.compile("[\U0001f300-\U0001faff☀-➿️⬀-⯿]")
        for name, chunk in (("markup", _know_html()), ("script", _know_script())):
            found = emoji.findall(chunk)
            assert not found, f"{name} 里出现了 emoji：{found}"

    def test_every_icon_is_font_awesome_and_hidden_from_screen_readers(self):
        html = _know_html()
        icons = re.findall(r"<i class=\"fas [^\"]*\"([^>]*)>", html)
        assert len(icons) >= 8, "图标太少，是不是把按钮文字也删了"
        assert all('aria-hidden="true"' in attrs for attrs in icons), icons
        # JS 里动态生成的图标也要带 aria-hidden（行内的「编辑 / 删除」按钮）
        script = _know_script()
        assert 'aria-hidden="true"' in script, "动态图标没有对读屏隐藏"

    def test_the_css_uses_tokens_not_raw_hex(self):
        css = _declarations(_know_css())
        without_fallbacks = re.sub(r"var\(--[\w-]+,\s*#[0-9a-fA-F]{3,8}\)", "VAR", css)
        leftovers = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_fallbacks)
        assert not leftovers, f"面板样式里出现了不走 token 的颜色：{leftovers}"

    def test_the_css_does_not_kill_the_focus_ring(self):
        css = _declarations(_know_css())
        assert not re.findall(r"outline\s*:\s*(?:none|0)\b", css), "关掉了焦点轮廓"
        assert ":focus-visible" not in css, "重复定义焦点环（style.css 已有全局一条）"

    def test_the_css_braces_are_balanced(self):
        text = _read()
        style = text[text.index("<style>") : text.index("</style>")]
        stripped = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        assert stripped.count("{") == stripped.count("}"), "页面 <style> 大括号不配平"

    def test_the_editor_columns_are_single_column(self):
        """编辑器是纵向排的（一行一个字段），不要做成两列 —— 小屏上会挤成半列。"""
        css = _know_css()
        assert ".ai-know-field" in css
        assert "grid-template-columns" not in css.split(".ai-know-editor {")[1].split("}")[0]

    def test_touch_targets_grow_on_small_screens(self):
        """小屏上按钮要抬到 44px。

        这是实测出来的：375px 下页脚的「关闭」只有 31px 高（`.btn-sm` 的自然高度），
        所以页脚也必须一起抬 —— 只写工具栏那几处是不够的。
        """
        css = _know_css()
        rule = css[css.index("@media (max-width: 767.98px)", css.index("ai-know-toolbar")) :]
        rule = rule[: rule.index("}") + 1]
        assert "min-height: 44px" in rule
        for selector in (".ai-know-toolbar .btn", ".ai-know-row__actions .btn",
                         ".ai-know-editor__actions .btn", "#aiKnowledgeModal .modal-footer .btn"):
            assert selector in rule, f"{selector} 没有被抬到 44px"

    def test_the_group_label_cannot_be_squeezed_into_one_character_per_line(self):
        """分组标题的文字要 nowrap 且不许收缩。

        **实测缺陷**：标题原本是「裸文本节点 + 说明」的 flex 行，匿名 flex 项默认
        `flex-shrink: 1`，而中文只在字与字之间断行 —— 375px 下「知识包清单」被压成
        「知识/包清/单」四行。修法是给标题文字一个真实元素 + `white-space: nowrap`。
        """
        css = _know_css()
        rule = re.search(r"\.ai-know-group__label\s*\{([^}]*)\}", css)
        assert rule, ".ai-know-group__label 没有样式定义"
        body = rule.group(1)
        assert "white-space: nowrap" in body
        assert "flex: 0 0 auto" in body, "标题文字还能被压扁"
        assert "flex-wrap: wrap" in re.search(r"\.ai-know-group__title\s*\{([^}]*)\}", css).group(1), (
            "标题行不会换行 —— 窄屏上说明会把标题挤扁"
        )
        script = _know_script()
        assert "ai-know-group__label" in script, "JS 没有给标题文字加那个类，样式没生效"

    def test_the_monospace_editor_keeps_long_lines_readable(self):
        """正文是 markdown：等宽 + 不折行 + 可横向滚动（折行会让缩进看起来像内容）。"""
        rule = re.search(r"\.ai-know-textarea\s*\{([^}]*)\}", _know_css())
        assert rule, ".ai-know-textarea 没有样式定义"
        body = rule.group(1)
        assert "font-family" in body
        assert "overflow-x: auto" in body
        assert "white-space: pre" in body

    def test_long_names_can_wrap(self):
        """用户建的文件名可能很长，必须能断开 —— 否则整页出现横向滚动。"""
        rule = re.search(r"\.ai-know-row__name\s*\{([^}]*)\}", _know_css())
        assert rule and "overflow-wrap: anywhere" in rule.group(1)


# ==========================================================================
# 7. 真跑：名字检查与 frontmatter 解析
# ==========================================================================


class TestTheNameChecker:
    def test_it_rejects_what_would_produce_a_meaningless_request(self):
        """含分隔符的名字在 URL 里会多切出一段路径 → 路由 404 → 用户看到一句
        与文件名毫无关系的错误。这一层只为把那句话换成一句能读懂的。

        **它不是安全校验**：安全校验在服务端（`project_pack_service` 的白名单），
        接口是可以直接调的。所以下面这些断言只说「前端有没有拦住」，
        服务端那侧的用例在 `tests/test_project_pack_management.py`。
        """
        result = _run_name_problem(
            {
                "empty": "",
                "blank": "   ",
                "slash": "a/b",
                "backslash": "a\\b",
                "traversal": "../escape",
                "dots": "..escape",
                "too_long": "x" * 70,
                "ok": "config-table-spec",
            }
        )
        assert result["empty"] and result["blank"]
        assert "分隔符" in result["slash"] and "分隔符" in result["backslash"]
        # `../escape` 先撞上的是分隔符那一支（更贴切：用户要改的是那个 `/`）
        assert "分隔符" in result["traversal"]
        assert ".." in result["dots"]
        assert "太长" in result["too_long"]
        assert result["ok"] == "", f"合法的名字被拦下了：{result['ok']!r}"


class TestTheFrontmatterParser:
    def test_it_reads_back_what_the_platform_says(self):
        """平台按「目录名 + 一句话说明」生成的 SKILL.md，要能被原样读回来。"""
        text = "---\nname: segment-rules\ndescription: ID 段位的判定规则\n---\n\n# 段位\n\n正文。\n"
        result = _run_frontmatter({"one": text})["one"]
        assert result["description"] == "ID 段位的判定规则"
        assert result["body"].startswith("# 段位"), result["body"]

    def test_it_does_not_confuse_the_name_line_with_the_description(self):
        """`name:` 那一行不能被当成 description —— 两者都是单行标量，很容易写反。"""
        text = "---\nname: pack\ndescription: 包\n---\n\n正文\n"
        result = _run_frontmatter({"one": text})["one"]
        assert result["description"] == "包"

    def test_a_file_without_frontmatter_is_treated_as_a_whole_body(self):
        result = _run_frontmatter({"plain": "# 只有正文\n"})["plain"]
        assert result["description"] == ""
        assert result["body"] == "# 只有正文\n"

    def test_an_unterminated_frontmatter_does_not_eat_the_body(self):
        """只有一个 `---` 的坏文件：整份当正文，用户能看到它坏在哪，而不是看到空白。"""
        result = _run_frontmatter({"broken": "---\nname: x\n\n# 正文\n"})["broken"]
        assert result["body"] == "---\nname: x\n\n# 正文\n"
