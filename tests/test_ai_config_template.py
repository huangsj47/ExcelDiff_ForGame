# -*- coding: utf-8 -*-
"""AI 分析配置界面（摘要行 + 居中模态框）的结构契约。

## 为什么是静态断言，而不是把模板渲染出来

这个模板依赖 `base.html` 的整套上下文（当前用户、`is_admin()`、项目与仓库列表……），
为了渲染要伪造十来个东西。而这里真正要守的是「HTML 与 JS 之间的契约」—— 那些东西
渲染一次也看不出来：`document.getElementById('xxx')` 返回 `null` 时不会抛错，只是
那一段逻辑静默不执行，页面上「什么都没发生」。

## 每条断言对应的真实缺陷形态

1. **id 对不上**：JS 里加了字段、HTML 里忘了加元素 → 那一栏永远填不上/存不下去，
   控制台没有任何报错。所以下面**从 JS 里把 id 表解析出来**再回查 HTML，而不是两边各
   抄一份清单 —— 抄一份的话，两边同时抄错也照样「自洽」。
2. **模型名做成下拉**：实测存在「能正常对话但不在 `/models` 列表里」的模型
   （本机代理上的 `deepseek-v4-flash` 就不在它自己返回的 84 条里）。做成只能从列表
   选的 `<select>`，用户反而选不到自己在用的模型。
3. **Token 回显**：服务端刻意不回显密钥，模板里若带了 `value` 就等于把配置界面变成
   泄露面。同时输入框必须是 `password`。
4. **范围写死在 HTML**：界面写「1~30」而后端按别的范围校验，是这次要修掉的东西之一。
   `min`/`max` 必须由配置接口的 `field_schema` 在运行时设置。
5. **emoji 当图标**：平台统一用 FontAwesome；emoji 在不同系统上渲染成完全不同的字形，
   还会被读屏软件念出「竖起大拇指」这类无关内容。
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "templates/merged_project_view.html"

# 模态框与脚本块在模板里的边界。用它们把断言限制在这次改动的范围内 ——
# 对整个文件做断言会被页面别处的同名串误伤。
MODAL_START = '<div class="card mb-4" id="aiConfigCard">'
MODAL_END = "{% if not repositories %}"
JS_START = "// AI 分析配置：页面摘要行 + 居中模态框"
JS_END = "function normalizeRiskLevel(level) {"


def _read() -> str:
    return (PROJECT_ROOT / TEMPLATE).read_text(encoding="utf-8")


def _modal_html() -> str:
    content = _read()
    return content[content.index(MODAL_START) : content.index(MODAL_END)]


def _ai_script() -> str:
    content = _read()
    return content[content.index(JS_START) : content.index(JS_END)]


def _field_dom_map() -> dict:
    """从 JS 的 `AI_FIELD_DOM` 表里解析出「字段名 → DOM id」。"""
    script = _ai_script()
    block = re.search(r"const AI_FIELD_DOM = \{(.*?)\n\};", script, re.S)
    assert block, "AI_FIELD_DOM 表不见了 —— 下面所有 id 断言都依赖它"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


# ==========================================================================
# 1. HTML 与 JS 的 id 契约
# ==========================================================================


def test_the_field_table_is_not_empty():
    """解析本身要能失败：正则写歪了、表被删了，都应该在这里就炸掉。

    没有这条，下面那条「每个 id 都有对应元素」会在空表上通过 —— 一个什么都没测的绿。
    """
    dom_map = _field_dom_map()
    assert len(dom_map) >= 14, f"字段表只有 {len(dom_map)} 项，像是被删过"
    assert dom_map["api_model"] == "aiModelInput"
    assert dom_map["api_key"] == "aiKeyInput"


def test_every_field_the_script_addresses_exists_in_the_markup():
    html = _modal_html()
    missing = [dom_id for dom_id in _field_dom_map().values() if f'id="{dom_id}"' not in html]
    assert not missing, f"JS 会去找这些元素，但模板里没有：{missing}"


def test_every_field_has_an_inline_error_container():
    """字段级错误要显示在对应输入框下方（而不是只在顶部列一遍）。

    JS 按 `<id>Error` 的约定去找容器，所以这里按同一个约定回查。
    """
    html = _modal_html()
    missing = [
        f"{dom_id}Error"
        for dom_id in _field_dom_map().values()
        if f'id="{dom_id}Error"' not in html
    ]
    assert not missing, f"这些字段没有内联错误位置：{missing}"


def test_every_visible_field_is_wired_to_its_help_and_error_text():
    """`aria-describedby` 要把帮助文案与错误文案都挂上，读屏才知道这栏怎么填、错在哪。

    按**空格分隔的 token** 判断，不要求两条相邻 —— 模型名那栏中间还挂了获取状态
    （`...Help aiModelStatus ...Error`），顺序不同不代表漏挂。
    """
    html = _modal_html()
    missing = {}
    for dom_id in _field_dom_map().values():
        # 三种控件都要认：input / select / textarea（提示词那两栏是 textarea）。
        tag = re.search(rf'<(?:input|select|textarea)[^>]*id="{dom_id}"[^>]*>', html, re.S)
        assert tag, f"找不到 {dom_id} 的控件标签"
        described = re.search(r'aria-describedby="([^"]*)"', tag.group(0))
        tokens = described.group(1).split() if described else []
        for expected in (f"{dom_id}Help", f"{dom_id}Error"):
            if expected not in tokens:
                missing.setdefault(dom_id, []).append(expected)
    assert not missing, f"这些输入框的 aria-describedby 不完整：{missing}"


def test_every_input_has_a_label():
    """`<label for>` 而不是靠 placeholder —— placeholder 在输入后就不见了。"""
    html = _modal_html()
    missing = [dom_id for dom_id in _field_dom_map().values() if f'for="{dom_id}"' not in html]
    assert not missing, f"这些输入框没有 label：{missing}"


def test_the_summary_row_elements_exist():
    html = _modal_html()
    for element_id in ("aiConfigSummary", "aiKeyStatusBadge", "aiConfigOpenBtn"):
        assert f'id="{element_id}"' in html, f"摘要行缺少 {element_id}"


def test_the_open_button_targets_the_modal():
    html = _modal_html()
    assert 'data-bs-toggle="modal"' in html
    assert 'data-bs-target="#aiConfigModal"' in html


# ==========================================================================
# 2. 模态框遵循仓库既有约定
# ==========================================================================


def test_the_modal_follows_the_platform_conventions():
    html = _modal_html()
    assert '<div class="modal fade" id="aiConfigModal" tabindex="-1"' in html
    assert 'aria-labelledby="aiConfigModalLabel"' in html
    assert 'class="modal-title" id="aiConfigModalLabel"' in html
    # 配置项多，必须居中且可滚动，否则小屏上底部的保存按钮点不到。
    assert "modal-dialog-centered" in html
    assert "modal-dialog-scrollable" in html


def test_the_modal_offers_an_explicit_close_and_cancel():
    html = _modal_html()
    assert 'data-bs-dismiss="modal"' in html
    assert "取消" in html


def test_all_four_groups_are_present():
    html = _modal_html()
    for group in ("连接配置", "分析策略", "告警门槛", "提示词与知识"):
        assert f"<legend class=\"fs-6 fw-semibold text-primary border-bottom pb-1 mb-3\">{group}</legend>" in html, (
            f"缺少分组：{group}"
        )
    assert html.count("<fieldset") == 4
    assert html.count("</fieldset>") == 4


# ==========================================================================
# 3. 模型名：文本输入 + datalist
# ==========================================================================


def test_the_model_name_is_a_text_input_with_a_datalist():
    """**这条是实测约束**：本机代理上 `deepseek-v4-flash` 能正常对话，却不在
    `/v1/models` 返回的 84 条里。做成 `<select>` 用户就选不到自己在用的模型。
    """
    html = _modal_html()
    assert 'id="aiModelOptions"' in html, "datalist 不见了"
    assert re.search(r'<input[^>]*type="text"[^>]*id="aiModelInput"', html) or re.search(
        r'<input[^>]*id="aiModelInput"[^>]*list="aiModelOptions"', html
    ), "模型名不是带 datalist 的文本输入框"
    assert 'list="aiModelOptions"' in html


def test_the_model_name_is_not_a_select():
    """反向守卫：把上面那条改回下拉框，必须在这里失败。"""
    html = _modal_html()
    assert not re.search(r'<select[^>]*id="aiModelInput"', html), (
        "模型名又被做成了下拉框 —— 列表里没有的模型将无法配置"
    )


def test_fetching_models_does_not_overwrite_what_the_user_typed():
    """列表只填进 datalist，**绝不改写输入框的值**。

    替用户「选中第一个」等于悄悄改掉他的配置，而他不会注意到。
    """
    script = _ai_script()
    body = script[script.index("function fillAiModelOptions") : script.index("async function aiFetchModels")]
    assert "list.appendChild(option)" in body, "列表根本没填进去"
    assert "aiModelInput" not in body, "填列表时动了输入框本身"
    # 只允许碰 datalist 这一个元素：碰了别的就是把用户的输入改掉了。
    # （`option.value = ...` 是给 <option> 赋值，不在此列 —— 早先那版断言按 `.value =`
    #   一刀切，把这一句也判成了违规。）
    touched = set(re.findall(r"aiEl\('([^']+)'\)", body))
    assert touched == {"aiModelOptions"}, f"填列表时还动了：{touched - {'aiModelOptions'}}"


def test_a_missing_model_list_is_not_treated_as_a_failure():
    """端点没有 `/models` 时用中性提示 + 手填指引，**不用红色错误态**。

    **只切出那一支来断言**：整个函数里 `text-muted` 还出现在「正在获取…」那一句，
    在函数范围内断言 `"text-muted" in body` 的话，把「不支持」这一支改成红色照样通过 ——
    变异验证时它就是没捕获住，所以才写成现在这样。
    """
    script = _ai_script()
    body = script[script.index("async function aiFetchModels") : script.index("async function aiTestConnection")]
    # 切到 `} catch` 为止：catch 那一支**应该**是红色（网络真的炸了），
    # 把它一起切进来会让「不支持列表被标红」这个变异漏过去。
    fallback = body[body.index("} else if (status) {") : body.index("} catch (_err) {")]
    assert "text-muted" in fallback, "不支持的模型列表被渲染成了错误态"
    assert "text-danger" not in fallback, "不支持的模型列表被标成了红色"
    assert "手动填写" in fallback, "没有告诉用户接下来该怎么办"

    success = body[body.index("if (data.ok && count) {") : body.index("} else if (status) {")]
    assert "text-success" in success, "获取成功与不支持列表没有区分开"
    assert "fillAiModelOptions(data.models);" in body, "拿到列表却没填进去"


# ==========================================================================
# 4. Token：掩码、不回显、可切换可见性
# ==========================================================================


def test_the_token_input_is_masked():
    html = _modal_html()
    assert re.search(r'<input[^>]*type="password"[^>]*id="aiKeyInput"', html)


def test_the_template_never_prefills_the_token():
    """服务端不回显密钥，模板里也就不能有 `value` —— 一旦有，就等于把它印在页面上。"""
    html = _modal_html()
    tag = re.search(r"<input[^>]*id=\"aiKeyInput\"[^>]*>", html, re.S)
    assert tag, "找不到 Token 输入框"
    assert "value=" not in tag.group(0), "Token 输入框带了预设值"
    assert "autocomplete=\"new-password\"" in tag.group(0), "浏览器可能把旧密码自动填进来"


def test_the_token_can_be_revealed_while_typing():
    """粘贴自定义 Token 时看不见是常见失败，所以要能切换（默认隐藏）。"""
    html = _modal_html()
    assert 'id="aiKeyToggleBtn"' in html
    assert 'aria-pressed="false"' in html
    script = _ai_script()
    assert "input.type = hidden ? 'text' : 'password';" in script


# ==========================================================================
# 5. 范围来自接口，不写死在 HTML
# ==========================================================================


def test_no_numeric_range_is_hardcoded_in_the_markup():
    """`min`/`max` 必须由 `field_schema` 运行时设置。

    HTML 里写死就会出现「界面写 1~30、后端按别的范围校验」—— 用户按界面提示填了值，
    保存却报错。范围只有一份，在 `endpoint_service.FIELD_RULES` 里。
    """
    html = _modal_html()
    for dom_id in _field_dom_map().values():
        tag = re.search(rf'<input[^>]*id="{dom_id}"[^>]*>', html, re.S)
        if not tag:
            continue
        assert not re.search(r'\b(min|max)="', tag.group(0)), (
            f"{dom_id} 把范围写死在 HTML 里了，应该从 field_schema 读"
        )


def test_the_script_sets_the_range_from_the_schema():
    script = _ai_script()
    assert "input.min = rule.min;" in script
    assert "input.max = rule.max;" in script
    assert "applyAiFieldSchema(data.field_schema);" in script


def test_the_script_does_not_keep_its_own_copy_of_the_ranges():
    """本地 blur 校验的范围也必须取自 schema，不能在 JS 里再抄一份数字。"""
    script = _ai_script()
    body = script[script.index("function validateAiFieldLocally") : script.index("function fillAiModelOptions")]
    assert "aiConfigCache.field_schema[field]" in body, "本地校验没有从 schema 取范围"


# ==========================================================================
# 6. 错误呈现：可聚焦的顶部摘要 + 内联错误
# ==========================================================================


def test_the_error_summary_is_announced_and_focusable():
    html = _modal_html()
    tag = re.search(r'<div[^>]*id="aiConfigErrorSummary"[^>]*>', html)
    assert tag, "没有错误摘要容器"
    assert 'role="alert"' in tag.group(0), "读屏软件不会播报它"
    assert 'tabindex="-1"' in tag.group(0), "拿不到焦点，键盘用户找不到错误"
    assert "d-none" in tag.group(0), "初始应该是隐藏的"


def test_a_failed_save_moves_focus_to_the_error_summary():
    script = _ai_script()
    body = script[script.index("function showAiConfigErrors") : script.index("function applyAiFieldSchema")]
    assert "summary.focus();" in body
    assert "setAiFieldError(item.field, item.message" in body, "错误没有落到具体那一栏"
    assert "items.length" in body, "摘要里没有说明一共几处"


def test_the_error_list_links_back_to_the_fields():
    script = _ai_script()
    body = script[script.index("function showAiConfigErrors") : script.index("function applyAiFieldSchema")]
    assert "focusAiField(item.field);" in body


def test_the_platform_does_not_use_bootstrap_field_invalid_classes():
    """平台自己的错误语言是「内联红字 + 顶部 alert」，不是 `is-invalid`/`invalid-feedback`
    （全仓库这两个类名零命中）。跟着既有的来，避免出现两套错误样式。"""
    html = _modal_html()
    script = _ai_script()
    assert "is-invalid" not in html
    assert "invalid-feedback" not in html
    assert "is-invalid" not in script
    assert 'class="form-text text-danger' in html, "内联错误没有用平台既有的红字样式"


# ==========================================================================
# 7. 异步按钮与只读态
# ==========================================================================


def test_every_async_button_reports_a_busy_state():
    """保存 / 测试连接 / 获取模型三个按钮都要 `disabled` + `aria-busy`，防重复提交。"""
    script = _ai_script()
    assert script.count("setAttribute('aria-busy', 'true')") == 3
    assert script.count("removeAttribute('aria-busy')") == 3
    # finally 里恢复：抛异常时按钮不能永远卡在禁用状态。
    assert script.count("} finally {") >= 3


def test_the_test_connection_button_sends_the_typed_values():
    """**未保存也能测**：请求体带当前填的地址/模型/Token，而不是先存再测。"""
    script = _ai_script()
    body = script[script.index("async function aiTestConnection") : script.index("async function aiSaveConfig")]
    assert "/test-connection" in body
    assert "JSON.stringify(aiEndpointPayload())" in body, "测试连接没有带上当前填的值"


def test_a_blank_token_means_keep_the_saved_one():
    """输入框留空 = 沿用已保存的 Token，而不是把它清掉。"""
    script = _ai_script()
    body = script[script.index("function aiEndpointPayload") : script.index("function collectAiConfigPayload")]
    assert re.search(r"if \(typedKey\) payload\.api_key = typedKey;", body)


def test_the_token_is_saved_through_its_own_endpoint():
    """Token 不在 `/config` 的字段表里，走 `/api-key`；两者要分别报告成败。

    只说一句「保存失败」的话，用户会以为整份配置都没生效而重填一遍 ——
    实际配置已经存下了，只有 Token 没存上。
    """
    script = _ai_script()
    body = script[script.index("async function aiSaveConfig") : script.index("function initAiKeyToggle")]
    assert "/api-key" in body
    assert "配置已保存，但 Token 更新失败。" in body
    assert "setAiFieldError('api_key'" in body


def test_a_read_only_user_gets_a_locked_form_and_no_save_button():
    html = _modal_html()
    assert "{% if not _ai_can_edit %}" in html
    assert "仅项目管理员可修改 AI 配置" in html
    script = _ai_script()
    body = script[script.index("function initAiConfig") :]
    assert "if (!aiCanEdit)" in body
    assert "el.disabled = true;" in body
    assert "saveBtn.classList.add('d-none');" in body


def test_the_read_only_lock_covers_the_radios_and_the_switch():
    """单选框和开关不在字段表里，漏掉它们只读用户就还能改一部分配置。"""
    script = _ai_script()
    block = re.search(r"const AI_READONLY_EXTRA = \[(.*?)\];", script, re.S)
    assert block, "找不到只读额外清单"
    for element_id in ("aiWeeklyAutoToggle", "aiSourceOpenai", "aiSourceCustom", "aiKeyToggleBtn"):
        assert element_id in block.group(1), f"{element_id} 没有被只读态覆盖"


# ==========================================================================
# 8. 视觉约定
# ==========================================================================


def test_icons_are_font_awesome_not_emoji():
    emoji = re.compile("[\U0001f300-\U0001faff☀-➿️⬀-⯿]")
    for name, chunk in (("模板", _modal_html()), ("脚本", _ai_script())):
        found = emoji.findall(chunk)
        assert not found, f"{name}里出现了 emoji 图标：{found}"
    assert 'class="fas fa-sliders-h' in _modal_html()


def test_every_icon_is_hidden_from_screen_readers():
    """图标旁边已经有文字，读屏再念一遍「同步」是噪音。"""
    html = _modal_html()
    icons = re.findall(r"<i class=\"fas [^\"]*\"([^>]*)>", html)
    assert icons, "一个图标都没有，是不是把按钮文字也删了"
    assert all('aria-hidden="true"' in attrs for attrs in icons), icons


def test_the_summary_shows_an_empty_state_with_a_next_step():
    script = _ai_script()
    assert "尚未配置" in script
    assert "点「修改配置」" in script, "空状态只说「没有」，没告诉用户下一步"


def test_the_summary_is_honest_about_readiness():
    """就绪状态来自后端的 `endpoint_ready`，不是前端自己猜的。"""
    script = _ai_script()
    body = script[script.index("function renderAiConfigSummary") : script.index("async function refreshAiAnalysisConfig")]
    assert "data.endpoint_ready" in body
    assert "还没就绪" in body

# ==========================================================================
# 9. 两个 AI 抽屉页：就绪判定必须与后端一致
# ==========================================================================

DRAWER_TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _drawer_source(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _weekly_click_handler() -> str:
    """切出「点风险标签打开抽屉」那个回调，到点击回调结束为止。

    边界取页面里紧随其后的注释行，而不是靠缩进猜的 `换行 + 大括号` —— 后者一旦被别处的
    顶格大括号撞上就会切歪，而且那段写法穿过 heredoc 与正则两层引号时反斜杠会被吃掉。
    """
    source = _drawer_source("templates/merged_project_view.html")
    start = source.index("openWeeklyAiDrawer(configId, versionName);")
    end = source.index("// 页面加载时获取统计信息", start)
    return source[start:end]


def test_every_drawer_checks_readiness_not_just_the_token():
    """跑分析要**接口地址、模型名字、Token** 三样齐全。

    只看 `/api-key/status` 的后果：只配了 Token 的项目，分析按钮是可点的，点下去
    跑到一半才失败；用户拿到的是一个看不出原因的报错。
    """
    for path in DRAWER_TEMPLATES:
        source = _drawer_source(path)
        assert "endpoint_ready" in source, f"{path} 没有用 endpoint_ready 判定就绪"
        assert "api-key/status" not in source.replace("而不是 /api-key/status", ""), (
            f"{path} 还在只看密钥就绪状态"
        )


def test_every_drawer_names_what_is_missing():
    """「配置不全」要说清楚缺哪一样 —— 让用户自己去猜是最没用的提示。

    **断言缺项清单本身，而不是全文件里有没有这几个字**：这几个词在引导文案里也出现，
    按全文件查的话把某一项从清单里删掉照样能通过（变异验证时就是这样漏过去的）。
    """
    for path in DRAWER_TEMPLATES:
        source = _drawer_source(path)
        assert "function describeMissing" in source, f"{path} 没有定义 describeMissing* 函数"
        start = source.index("function describeMissing")
        # 函数体到下一个顶格的 `}` 为止。边界用 chr(10) 拼：这段代码要穿过好几层
        # 引号（heredoc → python 字符串 → 正则），反斜杠在里面会被吃掉。
        body = source[start : source.index(chr(10) + "}", start)]
        pushed = re.findall(r"missing\.push\('([^']+)'\)", body)
        assert pushed == ["接口地址", "模型名字", "Token"], f"{path} 的缺项清单不对：{pushed}"
        assert "missing.join('、')" in body, "缺项清单没有拼进提示语"
        assert "return missing.join('、') || " in body, "一项都不缺时没有兜底文案"


def test_the_weekly_drawer_does_not_auto_start_when_not_ready():
    """**回归守卫**：打开抽屉**任何情况下**都不替用户开跑分析。

    这个用例原本守的是「查缓存与自动开跑必须待在『就绪』那一支里面」——
    当时的写法是两条并列的 promise 链：

        fetchKeyStatus().then(status => { ... return; });     // 「请先配置」+ return
        refreshLatest(configId).then(c => { if (!c) startAnalysis(); });

    上面那个 `return` 只结束了自己的回调 —— 未配置的项目照样会自动把分析跑起来，
    界面上同时出现「请先配置」和一堆服务端报错。

    现在自动开跑整条路**去掉了**：用户点一下「AI分析」只是想看结果，不该替他起一次
    分析；而「没有结果」的判定里还包含「有一条被重启中断的 running 记录」，所以
    重启后点一次必定跑一次 —— 用户明明关掉了「周版本自动分析」（那个开关管后台
    调度，管不到这里）。所以这里守的东西比原来更强，并且**顺带覆盖了原来那个 bug**：
    入口里根本没有开跑调用，未就绪时当然也不会跑。

    相关：`tests/test_weekly_ai_auto_trigger_gate.py` 是这一条的服务端与三模板版本。
    """
    handler = _weekly_click_handler()

    assert "if (!status.endpoint_ready) {" in handler, "没有就绪判定"
    assert "refreshWeeklyAiLatest(configId)" in handler, "打开抽屉时没查最近一次结果"
    # 就绪判定必须**留在**这个回调里（而不是被挪到外面去），否则未就绪也不会提示。
    assert handler.index("if (!status.endpoint_ready) {") < handler.index(
        "refreshWeeklyAiLatest(configId)"
    ), "查结果跑到了就绪判定之前"
    # 断言前先剥掉 `//` 注释：本仓库的习惯是把**被去掉的那个写法原样写进说明注释**
    # （这次就是在注释里引了 `if (!hasCached) startWeeklyAiAnalysis();`），
    # 不剥离的话说明文字会被当成真实代码 —— 这个坑本次会话已经踩过两次。
    code_only = re.sub(r"//[^\n]*", " ", handler)
    assert "startWeeklyAiAnalysis" not in code_only, (
        "打开抽屉的这支回调里出现了开跑调用 —— 「看一眼结果」不该把分析跑起来"
    )


def test_the_drawer_start_button_stays_disabled_until_ready():
    handler = _weekly_click_handler()
    assert "drawerStartBtn.disabled = true;" in handler
    assert "drawerStartBtn.disabled = false;" in handler
    assert handler.index("drawerStartBtn.disabled = true;") < handler.index(
        "drawerStartBtn.disabled = false;"
    ), "先放开再禁用等于没禁用"

# ==========================================================================
# 10. 服务来源 radio 与地址的联动
# ==========================================================================


def test_choosing_the_official_source_fills_the_url():
    """选「OpenAI 官方」要把官方地址填进输入框。

    地址是必填项。选着「官方」却留一个空框，界面上看不出到底会请求哪个地址 ——
    而后端把空地址当作官方默认值，两边显示的东西对不上。
    """
    script = _ai_script()
    assert "function fillOfficialAiUrl()" in script
    assert "openai.addEventListener('change'" in script
    assert "if (openai.checked) fillOfficialAiUrl();" in script
    assert "url.value = official;" in script


def test_the_official_url_is_shown_on_load_even_when_never_configured():
    """从未配置过的项目打开配置时也要显示官方地址，而不是「选中官方 + 空地址框」。"""
    script = _ai_script()
    body = script[script.index("function fillAiConfigForm") : script.index("function renderAiConfigSummary")]
    assert "if (data.source !== 'custom' && !String(data.api_base_url || '').trim())" in body
    assert "fillOfficialAiUrl();" in body


def test_switching_to_custom_clears_only_the_official_url():
    """切到「自定义端点」时清掉自动填入的官方地址，**但不动用户自己敲的内容**。

    留着官方地址会让人以为「自定义」用的就是它，存下去才发现请求的还是官方端点；
    而把用户手输的地址也一并清掉，是比留着更糟的意外。
    """
    script = _ai_script()
    body = script[script.index("function clearOfficialAiUrl") : script.index("function initAiSourceRadios")]
    assert "custom.addEventListener('change'" in script
    assert "if (custom.checked) clearOfficialAiUrl();" in script
    assert (
        "if (url && official && String(url.value || '').trim() === official) url.value = '';" in body
    ), "清空条件必须限定为「当前值等于官方地址」"


def test_both_source_radios_are_still_there():
    html = _modal_html()
    for element_id in ("aiSourceOpenai", "aiSourceCustom"):
        assert f'id="{element_id}"' in html
    assert 'name="aiEndpointSource"' in html


# ==========================================================================
# 11. 两个文本域的填写示例
# ==========================================================================


def test_the_prompt_and_knowledge_fields_carry_a_fill_in_example():
    """示例要给在**提示区**里，输入框本身保持为空。

    往输入框里预填内容会被用户当成「已经配好的配置」，直接保存下去 ——
    所以示例是说明文字，不是默认值。
    """
    html = _modal_html()
    for dom_id in ("aiPromptInput", "aiProjectKnowledgeInput"):
        tag = re.search(rf'<textarea[^>]*id="{dom_id}"[^>]*>', html, re.S)
        assert tag, f"找不到 {dom_id}"
        assert ">" in tag.group(0)
        # textarea 的开始标签与结束标签之间必须是空的
        after = html[html.index(tag.group(0)) + len(tag.group(0)) :]
        assert after.lstrip().startswith("</textarea>"), f"{dom_id} 被预填了内容"

    assert html.count("看一个填写示例") == 2, "两个字段都该有示例入口"
    assert html.count("<details") == 2


def test_the_examples_use_the_g119_project_vocabulary():
    """示例要用本项目的真实词汇，否则用户不知道该怎么往里填。

    这里只钉住几个「写错了会误导人」的事实点：号段规则、四个阶段、吸灵器交互链。
    """
    html = _modal_html()
    for phrase in ("qz_config", "CfgXxx.lua", "6 位", "类型段", "吸灵器", "返程撤离", "Loot"):
        assert phrase in html, f"示例里缺了 {phrase}"


def test_the_examples_are_collapsed_by_default():
    """示例是长文本，默认折叠——否则这个模态框会长到没法用。"""
    html = _modal_html()
    assert html.count("<details") == html.count("</details>")
    assert "<summary" in html


def test_the_examples_do_not_leak_internal_tool_names():
    """示例会进版本库，不能带上内部工具、内部系统或同事的名字。"""
    html = _modal_html()
    for banned in ("luna", "gaia", "jelly", "阿拉丁", "unisdk", "刘彦钟"):
        assert banned not in html, f"示例里出现了内部名称：{banned}"


class TestTheSubagentFields:
    """子代理模式那两个控件（**默认关**，只对周版本生效）。

    这一组的理由与它上面那些一样：配置界面是「这个功能会不会被误用」的最后一道闸门。
    这里钉三件事 —— 代价写出来了没有、默认值是不是关、开与关的读写有没有接上。
    """

    def test_the_toggle_exists_and_says_what_it_costs(self):
        html = _modal_html()

        assert 'id="aiSubagentToggle"' in html
        # **代价必须写在用户眼前**：打开它会让模型调用次数变成 (n+1) 倍。
        # 一个不写代价的开关，用户只会从账单上发现。
        assert "分片数 + 1" in html or "分片数+1" in html, (
            "开关旁边没有写清代价（模型调用次数变成分片数 + 1 倍）"
        )
        assert "仅周版本" in html, "没写清它只对周版本生效（单提交分析不受影响）"

    def test_the_count_field_is_wired_to_dom_and_errors(self):
        dom = _field_dom_map()

        assert dom["subagent_count"] == "aiSubagentCountInput"
        html = _modal_html()
        assert 'id="aiSubagentCountInput"' in html
        assert 'id="aiSubagentCountInputHelp"' in html
        assert 'id="aiSubagentCountInputError"' in html
        assert "aria-describedby=\"aiSubagentCountInputHelp aiSubagentCountInputError\"" in html

    def test_both_fields_are_saved(self):
        script = _ai_script()

        assert "numberValue('aiSubagentCountInput', 'subagent_count')" in script
        assert "payload.subagent_enabled = !!subagentToggle.checked" in script, (
            "开关的勾选状态没有被提交 —— 打开之后什么都不会变"
        )

    def test_the_toggle_is_only_checked_on_an_explicit_true(self):
        """老库上这一列是 NULL。**只有明确的 true 才勾上** —— 它是这个功能的唯一安全默认值。"""
        script = _ai_script()

        assert "data.subagent_enabled === true" in script, (
            "用了 `!== false` 之类的写法：NULL（老行）会被勾成「已启用」"
        )

    def test_the_count_is_loaded_from_the_config(self):
        script = _ai_script()

        assert "setValue('aiSubagentCountInput', data.subagent_count)" in script
