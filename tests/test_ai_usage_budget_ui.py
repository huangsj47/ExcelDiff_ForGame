# -*- coding: utf-8 -*-
"""AI 消耗面板上「平台总预算 / 口径联动 / 项目预算调整」三块的界面口径。

## 这个文件守的六条性质

1. **`null` 不是 `0`。** 上游没上报 token、费用算不出来、上限没配 —— 这三件事在
   接口里都是 `null`，而它们在界面上必须是「未上报」「不限制」，不能是 `0`。
   0 是一个**结论**（「一次都没用」「一毛钱都不许花」），说反了比不说更糟。
2. **没配上限 ≠ 0 上限。** 表单留空提交的是 `null`；填 `0` 要原样送到服务端被拒，
   而不是在前端被 `Number()` 悄悄变成 `null`（那会把一个填错的数读成「随便花」）。
3. **金额不再二次格式化。** 接口回的就是定点 2 位小数的字符串，前端再 `toFixed`
   一次等于把服务端定好的精度改写一遍。
4. **403 不是错误态。** `GET /ai-analysis/platform-budget` 是平台管理员专属；普通
   用户看到的是「只有平台管理员能查看和修改」这句说明，不是红色的「加载失败 + 重试」。
5. **口径联动三态互斥。** 同口径 → 只给正向标记；能对齐 → 给按钮；`mixed` →
   两样都不给（`target_range` 是 `null`，本来就没有能一次对齐的范围）。
6. **`budget_link.note` 与「本期预算」的数字都来自服务端。** 界面对这两件事只做显示，
   不在前端另算一份口径 —— 那才会出现「面板说没超、按钮却点不动」。

## 为什么会动 `test_ai_usage_filters_and_budget.py` 的一条断言

本页新增了两个写接口（平台总预算、单个项目的预算），所以那条「这个页面上只有一个
POST」的计数从 1 变成了 3。改法是把它换成**更强的**不变量：POST 的条数必须等于
「允许的那两个配置端点」出现次数之和 —— 原来只数条数，多一个打向别处的 POST
只要总数对得上就看不出来。禁掉 DELETE / PUT / PATCH 那几条一个字没动。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import app as flask_app
from app import create_tables
from services.ai.analysis_budget import (
    PERIOD_CHOICES,
    PERIOD_LABELS,
    platform_budget_status,
)
from services.ai.platform_budget import platform_budget_public
from services.ai_usage_service import usage_overview

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = "templates/ai_usage_dashboard.html"


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
#  剥注释：**静态断言之前必须先做这一步**
# ---------------------------------------------------------------------------
# 本模板的注释里原样引用着要禁掉的写法（「不要 `toFixed`」「不是 0」「留空 = 不限制」
# 这些句子本身就出现在注释里）。不剥就直接搜，会出现两种都很难查的失败：
# 假绿（注释里写着就算数）与假红（实现是对的，但注释里那句反例先被搜到）。


def _strip_js_comments(code: str) -> str:
    """剥掉 JS 的行注释与块注释，**字符串字面量里的 `//` 不动**。

    `'https://…'`、`accept: 'application/json'` 这类都含 `/`，用一条正则删
    `//.*$` 会把 `'https://example.com'` 从中间截断，后面的断言就再也搜不到东西。
    """
    out: list[str] = []
    index, length = 0, len(code)
    quote = ""
    while index < length:
        char = code[index]
        if quote:
            out.append(char)
            if char == "\\" and quote != "`":
                if index + 1 < length:
                    out.append(code[index + 1])
                    index += 2
                    continue
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and code[index + 1] == "/":
            while index < length and code[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and code[index + 1] == "*":
            index += 2
            while index + 1 < length and not (code[index] == "*" and code[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _dashboard_script() -> str:
    """模板里那段内联 `<script>`，**注释已经剥掉**。"""
    text = _read(DASHBOARD)
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, f"消耗面板应该只有一段内联脚本，实际 {len(blocks)} 段"
    return _strip_js_comments(blocks[0])


def _dashboard_style() -> str:
    text = _read(DASHBOARD)
    style = text[text.index("<style>"): text.index("</style>")]
    return _strip_css_comments(style)


def _function_body(script: str, name: str) -> str:
    """取出 `function <name>(...) { … }` 的函数体（按花括号配平）。

    顺序断言必须只看**这个函数**：同一个文件里有三处 `method: 'POST'`、
    好几处 `hidden = true`，整段搜出来的先后关系没有意义。
    """
    start = script.index(f"function {name}(")
    brace = script.index("{", start)
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[brace: index + 1]
        index += 1
    raise AssertionError(f"找不到 {name} 的函数体")


def _added_css() -> str:
    """本轮新增的那一段样式（平台总预算 / 口径联动 / 预算调整），注释已剥。

    起点用**选择器**而不是注释里的标题：`_strip_css_comments` 会把那段标题注释一起
    删掉，拿它当锚点会永远找不到。范围与
    `test_ai_usage_filters_and_budget.py::_added_dashboard_css` 同一取法 ——
    只对本轮新增的那一段做断言，不逼着后来的人重写页面原有的规则。
    """
    style = _dashboard_style()
    start = style.index(".aiu-linkage {")
    end = style.index("@media (max-width: 767px)")
    assert start < end, "找不到本轮新增的样式段"
    return style[start:end]


# ---------------------------------------------------------------------------
#  用 node 真跑模板里那段脚本
# ---------------------------------------------------------------------------
# 静态断言挡不住「`Number(null)` 是个合法写法」这类错误 —— 它只看得见字符串。
# 所以这里按仓库既有的做法（`tests/test_ai_usage_drawer_frontend.py`）：把**模板里
# 真实的那段脚本**读进 node 跑起来，再调用里面的纯函数断言**输出**。
#
# 顺带钉住一件静态断言做不到的事：整段脚本（含 IIFE）在加载时**不抛异常**。
# 一个 `undefined` 的函数名在浏览器里表现为「页面停在骨架屏」，而静态搜字符串
# 完全看不出来。

_DOM_STUB = """
function makeElement(tag) {
    const el = {
        tagName: tag, hidden: false, textContent: '', innerHTML: '', className: '',
        value: '', disabled: false, title: '', type: '', colSpan: 0, tabIndex: -1,
        style: {}, dataset: {}, options: [], children: [], parentNode: null,
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, removeEventListener() {}, setAttribute() {},
        getAttribute() { return null; }, removeAttribute() {},
        appendChild(child) { if (child) { child.parentNode = el; el.children.push(child); } return child; },
        removeChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
        focus() {}, closest() { return null; }, insertBefore() {}, scrollIntoView() {}
    };
    return el;
}
"""

_NODE_CASES = {
    "budgetScopeText": [
        {"limited": True,
         "period_label": "本月",
         "limits": {"tokens": 1000, "cost": "100.00", "currency": "CNY"},
         "used": {"tokens": 100, "cost": "12.00", "currency": "CNY", "runs": 3}},
        {"limited": True,
         "period_label": "本月",
         "limits": {"tokens": 1000, "cost": None, "currency": "CNY"},
         "used": {"tokens": None, "cost": None, "currency": "", "runs": 3}},
        {"limited": True,
         "period_label": "本月",
         "limits": {"tokens": 1000, "cost": None, "currency": "CNY"},
         "used": {"tokens": 0, "cost": None, "currency": "", "runs": 3}},
        {"limited": False,
         "period_label": "本月",
         "limits": {"tokens": None, "cost": None, "currency": ""},
         "used": {"tokens": None, "cost": None, "currency": "", "runs": 0}},
        None,
        # 5：平台档被收掉（普通用户）。**`limited` 仍然是真** —— 那就是服务端
        #    `redact_platform_scope` 的形状：判定留着，数字抹掉。
        {"hidden": True, "limited": True,
         "period_label": "本月",
         "limits": {"tokens": None, "cost": None, "currency": ""},
         "used": {"tokens": None, "cost": None, "currency": "", "runs": 0}},
        # 6：被收掉的那一档同时**已经超了**（界面还要给出「平台已超」那个标记）。
        {"hidden": True, "limited": True, "over": True,
         "period_label": "本月",
         "limits": {"tokens": None, "cost": None, "currency": ""},
         "used": {"tokens": None, "cost": None, "currency": "", "runs": 0}},
    ],
    "overScopesLabel": [["project"], ["platform"], ["project", "platform"], [], None],
    "meterPercent": [None, 0, 0.0042, 0.5, 1, 1.5, "abc", -1, 0.375],
    "fmtMoney": [["12.00", "CNY"], ["860.00", "CNY"], ["0.01", "USD"], ["1000.00", "EUR"],
                 ["12.00", "XYZ"], ["12.00", ""], [None, "CNY"], ["", "CNY"], ["1234.50", "CNY"],
                 # 9 起：不足一分钱。后端给的是**字符串** `"<0.01"`（整体，不是数字）。
                 ["<0.01", "CNY"], ["<0.01", "USD"], ["<0.01", "XYZ"]],
    # `aiuFmtCost(cost)` 收的是整个 `{amount, currency}` 对象（明细表里那一格）。
    "fmtCost": [{"amount": "<0.01", "currency": "CNY"},
                {"amount": "<0.01", "currency": "USD"},
                {"amount": "<0.01", "currency": "XYZ"},
                {"amount": "1.23", "currency": "CNY"},
                {"amount": None, "currency": "CNY"},
                None],
    # 周期下拉：`[当前周期, 服务端给的当前标签, 服务端下发的整张表]`。
    "periodSelect": [
        # 0：服务端下发了整张表，其中 `quarterly` 是本页自带那份表里**没有**的
        #    —— 下拉必须跟着多出这一项（「服务端加一个周期」不该再要人改前端）。
        {"period": "monthly", "label": "本月",
         "periods": [{"value": "monthly", "label": "本月"},
                     {"value": "quarterly", "label": "本季度"},
                     {"value": "all_time", "label": "全部时间"}]},
        # 1：服务端**没**下发（老缓存 / 请求失败后回落的那一帧）→ 用本页自带的表。
        {"period": "weekly", "label": "本周", "periods": None},
        # 2：下发了一个空数组，与「没下发」同义 —— 不能渲染出一个空下拉。
        {"period": "weekly", "label": "本周", "periods": []},
        # 3：非当前项的文案以**服务端**那份为准；当前项的文案用服务端刚回的 label。
        {"period": "monthly", "label": "本月（自然月）",
         "periods": [{"value": "monthly", "label": "月度"},
                     {"value": "all_time", "label": "所有时间"}]},
        # 4：当前周期不在下发的表里 → 补一项，否则 `select.value = current` 会落空
        #    （下拉显示的是第一项，用户一保存就把周期改掉了）。
        {"period": "weekly", "label": "本周",
         "periods": [{"value": "monthly", "label": "本月"},
                     {"value": "all_time", "label": "全部时间"}]},
        # 5：脏数据（没有 value 的条目）跳过。
        {"period": "monthly", "label": "本月",
         "periods": [{"value": "", "label": "空"}, None,
                     {"value": "monthly", "label": "本月"}]},
    ],
    "budgetFormPayload": [["monthly", "", ""], ["weekly", "1000", "12.50"],
                          ["all_time", "  2000  ", "  "], ["monthly", "abc", "xyz"],
                          ["monthly", "0", "0"], [None, None, None],
                          ["monthly", "1000.5", "12.345"]],
    "periodLabel": ["monthly", "weekly", "all_time", "MONTHLY", "whatever", None, ""],
    "fmtTokens": [None, 0, 999, 1500, 2500000],
}


def _function_source(script: str, name: str) -> str:
    """`function <name>(…) { … }` 的**完整源码**（含参数表）。

    与 `_function_body` 的区别：这份能直接喂给 node 求值，所以 IIFE 里的函数也能
    **真跑**（不必为了测试把内部函数挂到 `window` 上）。取的那段代码是模板里逐字的
    那一份 —— 抄写一份进测试就失去意义了。
    """
    start = script.index(f"function {name}(")
    head = script[start: script.index("{", start)]
    return head + _function_body(script, name)


def _run_node(script: str, cases: dict, probe_source: str = "") -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的纯函数断言")
    driver = _DOM_STUB + """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const cache = {};
const sandbox = {
    console: console, URLSearchParams: URLSearchParams, setTimeout: setTimeout,
    fetch: function () { return Promise.reject(new Error('offline')); },
    document: {
        getElementById(id) { return cache[id] || (cache[id] = makeElement('div')); },
        createElement(tag) { return makeElement(tag); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        readyState: 'complete'
    },
    window: {
        location: { search: '', pathname: '/ai-analysis/usage', hash: '' },
        history: { replaceState() {} },
        addEventListener() {}
    },
    bootstrap: { Modal: function () { return { show() {}, hide() {} }; } }
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'ai_usage_dashboard.js' });
// 探针：把 IIFE 里的函数（模板里逐字那段源码）放进**同一个**上下文求值，
// 于是它照样能看见 `AIU_PERIOD_ORDER` 这些全局量，还能用上面那个 DOM stub。
const PROBE = __PROBE__;
if (PROBE) vm.runInContext(PROBE, sandbox, { filename: 'probe.js' });

const A = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {
    typeofs: {
        aiuBudgetScopeText: typeof sandbox.aiuBudgetScopeText,
        aiuOverScopesLabel: typeof sandbox.aiuOverScopesLabel,
        aiuMeterPercent: typeof sandbox.aiuMeterPercent,
        aiuFmtMoney: typeof sandbox.aiuFmtMoney,
        aiuFmtCost: typeof sandbox.aiuFmtCost,
        aiuBudgetFormPayload: typeof sandbox.aiuBudgetFormPayload,
        aiuPeriodLabel: typeof sandbox.aiuPeriodLabel,
        fillPeriodSelect: typeof sandbox.__probeFillPeriodSelect
    },
    periodLabels: sandbox.AIU_PERIOD_LABELS,
    periodOrder: sandbox.AIU_PERIOD_ORDER,
    budgetScopeText: A.budgetScopeText.map(function (x) { return sandbox.aiuBudgetScopeText(x); }),
    overScopesLabel: A.overScopesLabel.map(function (x) { return sandbox.aiuOverScopesLabel(x); }),
    meterPercent: A.meterPercent.map(function (x) { return sandbox.aiuMeterPercent(x); }),
    fmtMoney: A.fmtMoney.map(function (p) { return sandbox.aiuFmtMoney(p[0], p[1]); }),
    fmtCost: A.fmtCost.map(function (x) { return sandbox.aiuFmtCost(x); }),
    budgetFormPayload: A.budgetFormPayload.map(function (p) {
        return sandbox.aiuBudgetFormPayload(p[0], p[1], p[2]);
    }),
    periodLabel: A.periodLabel.map(function (x) { return sandbox.aiuPeriodLabel(x); }),
    fmtTokens: A.fmtTokens.map(function (x) { return sandbox.aiuFmtTokens(x); }),
    // 真建一个 <select>、真调 `fillPeriodSelect`，再把渲染出来的 option 读回来。
    periodSelect: typeof sandbox.__probeFillPeriodSelect !== 'function' ? null
        : A.periodSelect.map(function (item) {
            const select = makeElement('select');
            sandbox.__probeFillPeriodSelect(select, item.period, item.label, item.periods);
            return {
                value: select.value,
                options: select.children.map(function (option) {
                    return option.value + '=' + option.textContent;
                })
            };
        })
};
process.stdout.write(JSON.stringify(out));
"""
    driver = driver.replace("__PROBE__",
                            json.dumps(probe_source and
                                       "__probeFillPeriodSelect = " + probe_source))
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "ai_usage_dashboard.js"
        script_path.write_text(script, encoding="utf-8")
        cases_path = Path(tmp) / "cases.json"
        cases_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        driver_path = Path(tmp) / "driver.js"
        driver_path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver_path), str(script_path), str(cases_path)],
            capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def js() -> dict:
    script = _dashboard_script()
    # `fillPeriodSelect` 在 IIFE 里，外面拿不到 —— 把模板里**逐字那段源码**求值进
    # 同一个上下文，于是可以真建一个 <select> 断言渲染出来的 option。
    return _run_node(script, _NODE_CASES,
                     probe_source=_function_source(script, "fillPeriodSelect"))


# ==========================================================================
#  一、纯函数：真跑
# ==========================================================================


class TestThePureFunctionsForReal:
    def test_the_module_actually_defines_every_probe(self, js):
        """探针函数都得真的存在 —— 有人重命名时，后面的 `map` 会静默产出 null 数组。

        `fillPeriodSelect` 是**求值进同一个上下文**的那一个（它在 IIFE 里，外面本来
        拿不到）：`null` 就是探针没跑起来的信号，所以它必须在这里被钉住。
        """
        assert js["typeofs"] == {
            "aiuBudgetScopeText": "function",
            "aiuOverScopesLabel": "function",
            "aiuMeterPercent": "function",
            "aiuFmtMoney": "function",
            "aiuFmtCost": "function",
            "aiuBudgetFormPayload": "function",
            "aiuPeriodLabel": "function",
            "fillPeriodSelect": "function",
        }

    def test_a_limited_scope_shows_used_over_limit(self, js):
        text = js["budgetScopeText"][0]
        # token 走页面已有的那个格式化函数（`aiuFmtTokens`：1k / 1M 缩写），
        # 金额**原样**用接口回的 2 位小数字符串。
        assert "100 / 1.0k" in text, text
        assert "¥12.00 / ¥100.00" in text, text

    def test_an_unreported_amount_is_not_zero(self, js):
        """**本文件最要紧的一条。**

        `used.tokens` 是 `null`（上游未上报）时写「未上报」；是 `0`（报了，且是 0）时
        写 `0`。两条输出必须**互不相同** —— 否则一旦有人把两支合并成
        「`Number(x) || 0`」，正例可能因为两句都不为空而全绿。
        """
        unreported = js["budgetScopeText"][1]
        reported_zero = js["budgetScopeText"][2]

        assert "未上报" in unreported, unreported
        assert "0 / 1.0k" not in unreported, f"未上报被写成了 0：{unreported}"
        assert "0 / 1.0k" in reported_zero, reported_zero
        assert "未上报" not in reported_zero, reported_zero
        assert unreported != reported_zero

    def test_an_unconfigured_scope_is_not_a_number_at_all(self, js):
        """没配上限返回 `null` —— 由调用方写「未设上限」，**不是 0 / 0**。"""
        assert js["budgetScopeText"][3] is None
        assert js["budgetScopeText"][4] is None

    def test_a_hidden_platform_scope_says_so_instead_of_claiming_unlimited(self, js):
        """**「你看不到」与「不限制」是两句相反的话。**

        平台档被收掉时 `limits` 全是 `null`，照「没配上限」那条路走会渲染成
        「未设上限」—— 那是在断言「平台没设上限」，而我们并不知道这件事。所以
        `hidden` 那一支必须排在 `limited` 之前，输出也必须是**另一句话**。

        反向自检：把那一支删掉，这两条会退化成 `null`（= 未设上限），与上面
        `[3]` 的输出一模一样 —— 所以这里额外断言它与 `null` 那条**不相等**。
        """
        hidden = js["budgetScopeText"][5]
        over_hidden = js["budgetScopeText"][6]

        assert hidden == "仅平台管理员可见", hidden
        assert over_hidden == hidden, "超没超都不该漏数字，两句必须一致"
        assert hidden is not js["budgetScopeText"][3]
        assert "不限制" not in hidden and "未设上限" not in hidden
        assert "0" not in hidden

    def test_the_over_label_says_which_scope_overran(self, js):
        assert js["overScopesLabel"] == [
            "项目已超", "平台已超", "项目与平台都已超", "", "",
        ]
        for label in js["overScopesLabel"][:3]:
            assert "已超" in label

    def test_a_ratio_that_cannot_be_computed_is_not_zero_percent(self, js):
        # [None, 0, 0.0042, 0.5, 1, 1.5, 'abc', -1, 0.375]
        assert js["meterPercent"][0] is None, "算不出比例时不能回落成 0%"
        assert js["meterPercent"][1] == 0
        assert js["meterPercent"][2] == 0      # 0.42% 四舍五入到 0，这是**算出来的** 0
        assert js["meterPercent"][3] == 50
        assert js["meterPercent"][4] == 100
        assert js["meterPercent"][5] == 100, "超了不截断成负数或超过 100 的宽度"
        assert js["meterPercent"][6] is None
        assert js["meterPercent"][7] is None
        assert js["meterPercent"][8] == 38
        # 反向自检：`None` 与「算出来是 0」是两件事，输出必须不同。
        assert js["meterPercent"][0] != js["meterPercent"][1]

    def test_money_uses_the_string_the_api_returned_verbatim(self, js):
        """**不在前端二次格式化。** 接口回的就是定点 2 位小数，原样显示。

        这里刻意用 `12.00` 与 `1234.50` 两个值：任何 `toFixed` / `Number()` 往返都会
        把 `12.00` 变成 `12`、把 `1234.50` 变成 `1234.5`。
        """
        assert js["fmtMoney"][0] == "¥12.00"
        assert js["fmtMoney"][1] == "¥860.00"
        assert js["fmtMoney"][3] == "€1000.00"
        assert js["fmtMoney"][8] == "¥1234.50", "金额被前端二次格式化了"

    def test_an_unknown_currency_keeps_its_code_instead_of_guessing_a_symbol(self, js):
        assert js["fmtMoney"][4] == "12.00 XYZ"
        assert js["fmtMoney"][5] == "12.00"

    def test_a_missing_amount_stays_missing(self, js):
        assert js["fmtMoney"][6] is None
        assert js["fmtMoney"][7] is None

    def test_a_sub_cent_amount_keeps_the_symbol_inside_the_less_than(self, js):
        """**不足一分钱**：后端给的是字符串 `"<0.01"`（`services/ai/pricing.py`），
        它是一个**整体**，所以符号要插在 `<` 之后 —— `<¥0.01`（不到一分钱）。

        写成 `¥<0.01` 就成了「币种后面跟了个比较符」，读起来是另一句话。两种拼法都是
        合法字符串，静态断言看不出来，只能真跑。
        """
        assert js["fmtMoney"][9] == "<¥0.01"
        assert js["fmtMoney"][10] == "<$0.01"
        # 反向自检：这一条才是缺陷本身（谁把拼接顺序写回符号在前，上面那条也红，
        # 但这条直接说出错在哪）。
        assert "¥<" not in js["fmtMoney"][9]
        # 认不出的币种照旧不猜符号，`<` 那一支也一样。
        assert js["fmtMoney"][11] == "<0.01 XYZ"

    def test_the_detail_table_uses_the_same_money_rule(self, js):
        """明细里那一格走的是 `aiuFmtCost(cost)`（收整个对象）—— 同一条规则。

        两份实现（`aiuFmtCost` 与 `aiuFmtMoney`）都拼金额，只改一处的话，同一笔
        「不到一分钱」在表格里显示 `¥<0.01`、在卡片里显示 `<¥0.01`。
        """
        assert js["fmtCost"][0] == "<¥0.01"
        assert js["fmtCost"][3] == "¥1.23"
        assert js["fmtCost"][4] is None
        assert js["fmtCost"][5] is None

    def test_a_blank_limit_is_submitted_as_null(self, js):
        assert js["budgetFormPayload"][0] == {
            "budget_period": "monthly",
            "budget_token_limit": None,
            "budget_cost_limit": None,
        }
        assert js["budgetFormPayload"][1]["budget_token_limit"] == "1000"
        assert js["budgetFormPayload"][1]["budget_cost_limit"] == "12.50"
        assert js["budgetFormPayload"][2]["budget_token_limit"] == "2000"
        assert js["budgetFormPayload"][2]["budget_cost_limit"] is None

    def test_a_typo_is_sent_to_the_server_instead_of_becoming_unlimited(self, js):
        """**反向自检。** `Number('abc')` 是 `NaN`，`JSON.stringify` 会把它写成
        `null` —— 一个填错的数于是被静默读成「不限制」。原样送上去，让服务端回一句
        「「abc」不是数字」，界面再落到那一栏。"""
        payload = js["budgetFormPayload"][3]
        assert payload["budget_token_limit"] == "abc", "前端把非法输入吞掉了"
        assert payload["budget_cost_limit"] == "xyz"
        # 而 `0` 也**不能**变成 `null`：0 是一个「不许花钱」的上限，服务端会拒绝它，
        # 前端悄悄改成 null 就等于把一次明确的拒绝变成了「随便花」。
        zero = js["budgetFormPayload"][4]
        assert zero["budget_token_limit"] == "0"
        assert zero["budget_cost_limit"] == "0"

    def test_a_missing_form_value_does_not_crash(self, js):
        assert js["budgetFormPayload"][5] == {
            "budget_period": "",
            "budget_token_limit": None,
            "budget_cost_limit": None,
        }

    def test_the_period_label_falls_back_to_the_key_not_to_a_guess(self, js):
        assert js["periodLabel"][:4] == ["本月", "本周", "全部时间", "本月"]
        assert js["periodLabel"][4] == "whatever", "认不出来的周期不该猜一个标签"
        assert js["periodLabel"][5] == ""


# ==========================================================================
#  二、周期选项表：服务端下发，本页自带的那份只是回落
# ==========================================================================


class TestThePeriodOptionsComeFromTheServer:
    """**事实源是服务端，且整张表随总览接口下发**（`payload.periods`）。

    这里原来钉的是「本页自带的那份表与服务端逐字一致」—— 那等于把「服务端加一个
    周期」变成一次「改了服务端还要记得改前端，忘了就红」的联动。现在正常路径走接口
    下发的那份，本页自带的那份退化成回落（老缓存 / 请求失败后回落的那一帧）。

    所以本类守两条：**下发了就用下发的**（含本页不认识的周期），**没下发才回落**。
    回落表仍然要与服务端一致 —— 它一旦漂了，只在出问题的那一帧显形，那种漂移最难发现。
    """

    def test_the_overview_ships_the_whole_table(self):
        """接口真的下发了这张表，且顺序、文案与服务端常量一致。"""
        with flask_app.app_context():
            create_tables()
            body = usage_overview([], None)

        assert body["periods"] == [
            {"value": key, "label": PERIOD_LABELS.get(key, key)} for key in PERIOD_CHOICES
        ], "总览响应里的周期选项表与服务端常量不一致（界面会渲染出一个后端不认的选项）"

    def test_the_server_table_wins_over_the_built_in_one(self, js):
        """服务端下发的那张表里加一个本页**不认识**的周期，下拉里就得有它。

        这是本类存在的理由：回落那条路在「两种来源恰好一样」时是分辨不出来的。
        """
        rendered = js["periodSelect"][0]
        assert rendered["options"] == [
            "monthly=本月", "quarterly=本季度", "all_time=全部时间",
        ], rendered
        assert rendered["value"] == "monthly"
        # 反向自检：`quarterly` 只可能来自服务端下发的那张表（本页只有三个周期）。
        assert "quarterly" not in js["periodOrder"]

    def test_the_built_in_table_only_backs_the_missing_payload(self, js):
        """没下发 / 下发空数组 → 用本页自带的那份（三个周期，与今天一样）。

        空数组与「没下发」同义：渲染出一个空下拉会让用户以为预算不能选周期。
        """
        assert js["periodSelect"][1]["options"] == [
            "monthly=本月", "weekly=本周", "all_time=全部时间",
        ]
        assert js["periodSelect"][1]["value"] == "weekly"
        assert js["periodSelect"][2] == js["periodSelect"][1]

    def test_a_non_current_option_uses_the_servers_label(self, js):
        """非当前项的文案以**服务端**那份为准（运营改文案不该要人改模板）。

        当前那一项的文案用服务端**刚回的那份** `period_label` —— 它比常量更准
        （例如「本月」被服务端渲染成带年份的说法）。
        """
        rendered = js["periodSelect"][3]
        assert rendered["options"] == ["monthly=本月（自然月）", "all_time=所有时间"]
        assert rendered["value"] == "monthly"

    def test_the_current_period_is_never_dropped_from_the_select(self, js):
        """当前周期不在下发的表里 → **补一项**。

        不补的话 `select.value = current` 落空，下拉会停在第一项上 —— 用户什么都没改、
        点保存，周期就被改成了第一项（一个静默的数据改写）。
        """
        rendered = js["periodSelect"][4]
        assert rendered["options"] == ["monthly=本月", "all_time=全部时间", "weekly=本周"]
        assert rendered["value"] == "weekly"

    def test_a_dirty_entry_is_skipped_instead_of_rendering_an_empty_option(self, js):
        assert js["periodSelect"][5]["options"] == ["monthly=本月"]
        assert js["periodSelect"][5]["value"] == "monthly"

    def test_both_selects_are_filled_from_the_payload_not_the_constants(self):
        """三处下拉（平台卡片、项目预算弹层打开时、弹层重新拉数后）都要把接口下发的
        那份传进去 —— 漏传一处，那一处就永远停在回落表上，服务端加周期时只有
        一半界面对。"""
        script = _dashboard_script()
        calls = re.findall(r"fillPeriodSelect\(\$\([^;]*;", script, re.S)
        assert len(calls) == 3, f"fillPeriodSelect 的调用点变了：{len(calls)}"
        for call in calls:
            assert "aiPeriodChoices()" in call, (
                f"这个调用点没把接口下发的那张表传进去：{call.strip()}"
            )
        load = _function_body(script, "loadOverview")
        assert "Array.isArray(body.periods) ? body.periods : null" in load

    def test_the_labels_are_the_servers_labels(self, js):
        assert js["periodLabels"] == PERIOD_LABELS, (
            f"模板里的 AIU_PERIOD_LABELS 与服务端 PERIOD_LABELS 不一致："
            f"{js['periodLabels']} vs {PERIOD_LABELS}"
        )

    def test_the_order_is_the_servers_choices(self, js):
        assert tuple(js["periodOrder"]) == tuple(PERIOD_CHOICES)

    def test_the_same_keys_are_in_the_model_layer(self):
        from models.ai_analysis.project_config import BUDGET_PERIOD_CHOICES

        assert tuple(PERIOD_CHOICES) == tuple(BUDGET_PERIOD_CHOICES)


# ==========================================================================
#  三、静态：三块界面的形状
# ==========================================================================


class TestTheLinkageRow:
    def test_the_note_is_a_live_region_with_a_font_awesome_icon(self):
        html = _read(DASHBOARD)
        marker = html.index('id="aiuBudgetLinkNote"')
        chunk = html[marker - 400: marker + 200]
        assert 'role="status"' in chunk, "口径说明不是一个 live region"
        assert "fas fa-" in chunk, "口径说明没有配 FontAwesome 图标"
        assert 'aria-hidden="true"' in chunk, "装饰性图标没有对读屏隐藏"

    def test_the_note_text_comes_from_the_server(self):
        script = _dashboard_script()
        body = _function_body(script, "renderBudgetLink")
        assert "data.note" in body, "说明文字没有用服务端那句 `budget_link.note`"

    def test_aligned_and_alignable_are_mutually_exclusive(self):
        """同口径 → 只有正向标记；能对齐 → 只有按钮。**两个同时出现**会让人以为
        那个「对齐」按钮点了没生效。"""
        body = _function_body(_dashboard_script(), "renderBudgetLink")
        assert "budgetLinkState.targetRange = data.target_range || null;" in body
        assert "!data.aligned && budgetLinkState.targetRange" in body, (
            "对齐按钮的显示条件不对：它必须在「没对齐**且**有目标范围」时才出现"
        )
        assert "chip.hidden = !data.aligned" in body

    def test_mixed_gets_no_button(self):
        """`mixed` 时服务端把 `target_range` 给成 `null`，而按钮只在有目标范围时出现 ——
        硬选一个范围只会让另外几个项目的数字继续不同口径。"""
        script = _dashboard_script()
        body = _function_body(script, "renderBudgetLink")
        assert "target_label" in body, "按钮标签里没带上目标范围，用户得自己去猜该选哪个"
        # 服务端那一句本身就把 mixed 说清了（period_alignment 的 note）。
        from services.ai_usage_service import RANGE_LABELS, UsageFilters

        filters = UsageFilters(range_key="this_month", source="all", status="all")
        link = _period_alignment_of(filters, ["weekly", "monthly"])
        assert link["mixed"] is True
        assert link["target_range"] is None
        assert "没有能一次对齐的筛选范围" in link["note"]

    def test_the_button_reuses_the_existing_filter_path(self):
        """点「对齐」必须走**已有的**那条应用筛选路径。另写一套 URL 拼装的必然结果是
        刷新 / 后退之后条件与地址栏对不上，而且没人知道它们不一样。"""
        body = _function_body(_dashboard_script(), "initBudgetLink")
        assert "applyAndReload()" in body, "没有复用页面已有的应用筛选路径"
        assert "aiuFilterRange" in body
        assert "history.replaceState" not in body, "另写了一套地址栏写入"
        assert "URLSearchParams" not in body, "另写了一套 URL 拼装"


def _period_alignment_of(filters, periods):
    from services.ai_usage_service import period_alignment

    return period_alignment(filters, periods)


class TestTheBudgetColumn:
    def test_the_column_says_what_it_counts(self):
        """整列的口径要写在表头附近 —— 「同一屏上两个『本月』」正是最容易读错的地方。"""
        html = _read(DASHBOARD)
        marker = html.index('id="aiuBudgetColumnNote"')
        chunk = html[marker: marker + 1200]
        assert "永远按各项目自己配置的预算周期统计" in chunk
        assert "任意一档超了都会挡住下一次分析" in chunk

    def test_the_two_scopes_are_both_listed(self):
        body = _function_body(_dashboard_script(), "budgetCell")
        assert "addScopeLine(td, '项目', budget)" in body
        assert "if (platform.limited) addScopeLine(td, '平台', platform)" in body, (
            "平台档配了上限时必须也列出来 —— 只显示项目档会让「项目没超、却被拦住」"
            "变成一个没有解释的现象"
        )

    def test_the_column_uses_the_shared_scope_formatter(self):
        """项目档与平台档走**同一个**格式化函数：各写一份必然漂移，而漂移的方向是
        「项目那行写未上报、平台那行写 0」。"""
        script = _dashboard_script()
        assert script.count("aiuBudgetScopeText(") >= 2

    def test_a_different_caliber_is_marked_but_the_numbers_stay(self):
        """口径不同要打标记，但**数字不藏起来** —— 预算那一列按各项目自己的周期统计
        是刻意的（它必须与闸门判定逐字一致）。"""
        body = _function_body(_dashboard_script(), "budgetCell")
        assert "budget.matches_filter === false" in body
        assert "与当前筛选范围不同口径" in body
        # 标记既有 `title` 也有 `aria-label`：`title` 对触屏与键盘用户都拿不到。
        assert "setAttribute('aria-label', why)" in body
        assert "mark.title = why" in body

    def test_the_over_badge_says_which_scope(self):
        body = _function_body(_dashboard_script(), "budgetCell")
        assert "aiuOverScopesLabel(budget.over_scopes)" in body
        assert "'aiu-budget-over'" in body
        assert "已超" in body


class TestThePlatformBudgetCard:
    def test_a_forbidden_response_is_not_an_error_state(self):
        """**反向自检。** 403/401 必须**提前 return**，不能掉到
        `showPlatformBudgetError` 那一支 —— 那会让普通用户看到红色的「加载失败 +
        重试」，去重试一个永远不会成功的请求。"""
        body = _function_body(_dashboard_script(), "loadPlatformBudget")
        denied = body.index("result.status === 401 || result.status === 403")
        failure = body.index("showPlatformBudgetError(")
        assert denied < failure, "权限判断排在错误分支后面，403 会掉进错误态"
        branch = body[denied: failure]
        assert "return" in branch, "权限不足那一支没有提前 return"
        assert "showPlatformBudgetError" not in branch
        assert "readonly.hidden = false" in branch, "权限不足时没有给出只读说明"
        # 那句说明自己也要在（`readonly` 这个变量名可以改，说明文字不能没）。
        assert "$('aiuPlatformBudgetReadonly')" in body

    def test_the_readonly_note_says_who_can_see_it(self):
        """说明必须把**看**和**改**都说了 —— 因为两件事都要平台管理员。

        中间有一版把这句话改成「只有平台管理员能修改」，配套的是「数字照常渲染、
        只有编辑器变只读」。那个做法基于一个正确的观察（当时 `usage_overview` 确实把
        平台档下发给所有人）却得出了反的结论 —— 现在服务端把总览里那一份也收掉了
        （`show_platform=platform_scope_visible()`），所以「查看」这一半重新成立，
        而且**必须**写出来：读接口现在真的回 403，用户得知道那不是页面坏了。
        """
        html = _read(DASHBOARD)
        marker = html.index('id="aiuPlatformBudgetReadonly"')
        chunk = html[marker: marker + 400]
        assert "只有平台管理员能查看和修改" in chunk

    def test_the_card_asks_for_json_so_a_denial_is_a_status_not_a_redirect(self):
        """不带 `Accept: application/json` 时，非管理员拿到的是一个 302 跳登录页，
        `fetch` 跟过去之后是一份 HTML —— 「没权限」会以「JSON 解析失败」的形式
        出现在错误态里。"""
        body = _function_body(_dashboard_script(), "loadPlatformBudget")
        assert "'Accept': 'application/json'" in body

    def test_the_editor_is_hidden_while_the_readonly_note_is_shown(self):
        body = _function_body(_dashboard_script(), "loadPlatformBudget")
        assert "body.hidden = true" in body, "加载开始时没有先把内容藏起来（会闪一下上一个项目的数据）"
        assert "readonly.hidden = true" in body

    def test_a_non_admin_gets_the_readonly_note_and_no_numbers(self):
        """403 那一支只放说明，**绝不显示数字那一块**。

        这一条是「平台合计只对平台管理员可见」在界面上的落点。注意它不只是「数字取不到
        所以画不出来」：`aiuPlatformBudgetBody`（已用 / 上限 / 进度条）必须保持在
        `hidden` 状态，否则用户会看到一张**画着 0 或者上一轮残留数字**的卡片 ——
        那两句都是在断言我们并不知道的事。
        """
        script = _dashboard_script()
        body = _function_body(script, "loadPlatformBudget")
        denied = body.index("result.status === 401 || result.status === 403")
        branch = body[denied: body.index("if (!result.ok")]
        assert "readonly.hidden = false" in branch, "权限不足时没有给出那句说明"
        assert "body.hidden = false" not in branch, (
            "403 那一支把数字那一块放出来了 —— 里面是空的 limits/used，会被画成 0"
        )
        assert "renderPlatformBudget(" not in branch, "403 那一支还去渲染了数字"
        # 撤销掉的 `can_edit` 不许回来：读接口现在直接 403，没有「能看不能改」这一档。
        assert "can_edit" not in script

    def test_the_empty_state_is_a_sentence_not_an_error(self):
        """`configured === false`（没配过）是**空态**，不是错误：卡片要引导用户去配，
        而不是报「加载失败」。"""
        body = _function_body(_dashboard_script(), "renderPlatformBudget")
        assert "还没有配置（当前不限制）" in body

    def test_the_over_notice_explains_what_is_and_is_not_interrupted(self):
        body = _function_body(_dashboard_script(), "renderPlatformOverNotice")
        assert "超预算会挡住下一次分析（手动与定时都挡），已经在跑的分析不会被中断。" in body
        html = _read(DASHBOARD)
        marker = html.index('id="aiuPlatformOverNotice"')
        # 卡片内这一条**不**播报：同一次超预算在这里与顶部横幅都会出现，
        # 两处都挂 aria-live 会让屏幕阅读器连读两遍。播报交给顶部那条。
        assert 'role="alert"' not in html[marker - 200: marker + 120], (
            "卡片内的超预算提示又挂上了 role=\"alert\" —— 它与顶部横幅会重复播报"
        )

    def test_the_form_saves_through_the_platform_endpoint(self):
        body = _function_body(_dashboard_script(), "savePlatformBudget")
        assert "aiuBudgetFormPayload(" in body, "表单没有走共用的那套「留空 = 不限制」拼装"
        assert "'Content-Type': 'application/json'" in body
        assert "JSON.stringify(" in body
        # 保存后服务端直接回一份新状态：界面不必再发一次 GET（那会把「保存成功」
        # 与「状态刷新」变成两次可能不一致的往返）。能走到保存这一步的人本来就是平台
        # 管理员（写接口在服务端还会再判一次），所以这里直接把表单填回去。
        assert "renderPlatformBudget(body.budget, body.status)" in body
        # 但上面「各项目消耗」里每一行的平台档也要跟着变。
        assert "loadOverview()" in body

    def test_field_errors_land_on_their_inputs_and_the_rest_on_the_summary(self):
        script = _dashboard_script()
        body = _function_body(script, "applyBudgetFieldErrors")
        assert "leftover.push(text)" in body, "没有对应字段的错误被丢掉了"
        assert "target.hidden = false" in body
        save = _function_body(script, "savePlatformBudget")
        assert "applyBudgetFieldErrors(body.errors, AIU_PLATFORM_FIELD_MAP)" in save
        assert "aiuPlatformFormError" in save, "没有对应字段的错误没有落到顶部摘要"

    def test_every_budget_field_has_an_error_slot(self):
        html = _read(DASHBOARD)
        for dom_id in ("aiuPlatformPeriodError", "aiuPlatformTokenError",
                       "aiuPlatformCostError"):
            assert f'id="{dom_id}"' in html, f"缺少 {dom_id}"
        script = _dashboard_script()
        for field in ("budget_period", "budget_token_limit", "budget_cost_limit"):
            assert field in script, f"{field} 没有对应的错误位"

    def test_the_save_button_uses_the_same_fetch_contract_as_the_price_editor(self):
        """CSRF 由 `base.html` 那个包装过的 `window.fetch` 自动加头（`X-CSRF-Token`），
        所以照抄同页单价表保存按钮的写法即可 —— **不要**自己另加一套 token 逻辑。"""
        script = _dashboard_script()
        for name in ("savePlatformBudget", "saveBudgetModal"):
            body = _function_body(script, name)
            assert "credentials: 'same-origin'" in body, f"{name} 没有带 cookie"
            assert "X-CSRF-Token" not in body, f"{name} 自己造了一套 CSRF 头"

    def test_the_platform_alert_is_not_rendered_when_the_scope_is_hidden(self):
        """平台档被收掉时**不弹那条横幅**，也不能改成弹一句「平台没问题」。

        横幅的判据是「平台档 over 了」，而平台档对普通用户整个不在这份响应里
        （`platform_status` 为空）—— 照旧调用会安安静静地不弹。这看着无害，但它是
        **碰巧**对的：哪天有人给 `renderPlatformAlert` 加一个「没数据就显示正常」的
        分支，普通用户就会看到一句「平台预算正常」，那是我们并不知道的事。
        所以这里把「不许调用」变成一条**显式的**判据。
        """
        script = _dashboard_script()
        load = _function_body(script, "loadOverview")
        marker = load.index("renderPlatformAlert")
        guard = load[:marker]
        assert "platform_hidden" in guard, (
            "调用 renderPlatformAlert 之前没有看 platform_hidden —— 服务端不下发"
            "平台档时，这里只是碰巧什么都没画"
        )

    def test_the_meters_are_hand_written_and_have_a_text_alternative(self):
        """仓库里没有图表库，也不引新的：条子手写，数值就在旁边那行文字里。"""
        html = _read(DASHBOARD)
        assert 'id="aiuPlatformTokenFill"' in html and 'id="aiuPlatformCostFill"' in html
        assert 'id="aiuPlatformTokenValue"' in html and 'id="aiuPlatformCostValue"' in html
        css = _added_css()
        assert ".aiu-meter__fill" in css
        for banned in ("chart.js", "Chart(", "echarts", "d3.", "highcharts"):
            assert banned not in html, f"引入了图表库：{banned}"

    def test_the_meter_is_not_drawn_when_the_ratio_cannot_be_computed(self):
        """一条停在 0% 的条子会被读成「一点都没用」，而旁边的文字说的是「未上报」。"""
        body = _function_body(_dashboard_script(), "setMeter")
        assert "percent === null ? 'none' : ''" in body


class TestTheProjectBudgetModal:
    def test_the_adjust_button_opens_the_modal(self):
        body = _function_body(_dashboard_script(), "renderProjects")
        assert "openBudgetModal(item)" in body
        assert "adjust.setAttribute('aria-label'," in body, "纯图标以外也得有可访问名"

    def test_the_row_is_not_a_button_so_the_inner_button_is_reachable(self):
        """**这一条修的是一个真实的可访问性缺陷。**

        `role="button"` 的元素，其**后代一律被当成装饰**（ARIA 的 Children
        Presentational）。整行做成按钮的写法会让行里那个「调整」按钮对读屏用户
        完全不存在 —— 键盘能 Tab 到行上，却永远 Tab 不到「调整」。
        """
        body = _function_body(_dashboard_script(), "renderProjects")
        assert "tr.setAttribute('role', 'button')" not in body
        assert "tr.tabIndex" not in body
        assert "aiu-name-btn" in body, "行里没有给键盘用户留下打开明细的真按钮"
        assert "查看项目 " in body, "那个真按钮没有可访问名"

    def test_the_modal_saves_only_the_three_budget_fields(self):
        """**只提交这三个**：服务端按 payload 里出现的字段逐个 `setattr`，
        多带一个字段就会把那个配置项一起改掉。"""
        body = _function_body(_dashboard_script(), "saveBudgetModal")
        assert "aiuBudgetFormPayload(" in body
        payload_keys = re.findall(r"(budget_[a-z_]+):", _dashboard_script())
        assert set(payload_keys) <= {
            "budget_period", "budget_token_limit", "budget_cost_limit"
        }, f"预算表单提交了不该提交的字段：{sorted(set(payload_keys))}"

    def test_a_forbidden_save_says_which_permission_is_missing(self):
        body = _function_body(_dashboard_script(), "saveBudgetModal")
        assert "result.status === 401 || result.status === 403" in body
        assert "setBudgetModalReadonly(true)" in body
        readonly = _function_body(_dashboard_script(), "setBudgetModalReadonly")
        assert "canEdit" in readonly
        assert "aiuBudgetModalReadonly" in readonly
        html = _read(DASHBOARD)
        marker = html.index('id="aiuBudgetModalReadonly"')
        assert "需要该项目的管理员权限" in html[marker: marker + 300]

    def test_the_disabled_save_button_does_not_get_re_enabled_by_the_tail(self):
        """`403` 之后那一句「finally」式的收尾会把按钮重新放开 —— 一个又变亮、
        又存不下去的按钮，比一个灰着的按钮难查得多。"""
        busy = _function_body(_dashboard_script(), "setBudgetModalBusy")
        assert "on || !budgetModalState.canEdit" in busy

    def test_the_modal_backfills_the_effective_limits_not_zero(self):
        # 回填逻辑在 `applyBudgetLimitsToModal`（打开弹层时先用总览那一行的值画上、
        # 再拉一次权威值，两处都走这一个函数）。
        body = _function_body(_dashboard_script(), "applyBudgetLimitsToModal")
        assert "source.tokens === null || source.tokens === undefined" in body
        assert "source.cost === null || source.cost === undefined" in body
        # 打开时**重新拉一次**：保存是把周期 + 两个上限整份写回的，拿旧值提交会覆盖
        # 别处的改动（见 refreshBudgetModal）。
        modal = _function_body(_dashboard_script(), "openBudgetModal")
        assert "refreshBudgetModal(" in modal

    def test_the_modal_reuses_the_shared_period_select(self):
        body = _function_body(_dashboard_script(), "openBudgetModal")
        assert "fillPeriodSelect(" in body

    def test_the_modal_refetches_before_it_can_be_saved(self):
        """**打开弹层时重新拉一次这个项目的预算。**

        保存是把「周期 + 两个上限」**整份写回**的，而弹层里那份来自总览那一行 ——
        页面加载那一刻的快照。用户可能在另一个标签页里刚改过，拿旧值提交就是把别人的
        改动覆盖回去（用户看不到任何提示，只会发现预算过一会儿又变回去了）。
        """
        script = _dashboard_script()
        body = _function_body(script, "refreshBudgetModal")
        assert "'/ai-analysis/projects/' + projectId + '/budget'" in body
        assert "credentials: 'same-origin'" in body
        assert "'Accept': 'application/json'" in body
        # 拉回来的是**权威值**，要重新画一遍周期与两个上限。
        assert "applyBudgetLimitsToModal(fresh.limits || {})" in body
        assert "fillPeriodSelect(" in body
        # 读接口是 GET：带上 method 就变成「往预算端点上写」了。
        assert "method:" not in body, "读预算的请求带了 method"
        # 先画上总览那一行的值（弹层不该空一帧），再拉权威值。
        modal = _function_body(script, "openBudgetModal")
        assert modal.index("applyBudgetLimitsToModal(") < modal.index("refreshBudgetModal(")

    def test_a_failed_refetch_says_so_instead_of_pretending_it_is_fresh(self):
        """拉不到时**不装作没事**，也不把已经画好的值抹掉。

        值照旧可编辑（回落到打开页面时的那份），但必须说清是哪一份 —— 否则用户会以为
        看到的是最新值，然后拿旧值覆盖别人刚做的改动。
        """
        body = _function_body(_dashboard_script(), "refreshBudgetModal")
        failure = body[body.index("if (!result.ok || !result.body.budget)"):]
        failure = failure[: failure.index("var fresh =")]
        assert "applyBudgetLimitsToModal" not in failure, (
            "读取失败时还去回填了一次 —— 失败响应里的空值会把已画好的上限抹成「不限制」"
        )
        assert "return null" in failure
        assert "打开页面时的值" in failure, "没告诉用户看到的是哪一份值"
        # 忙碌态的解除要排在**异常分支之后**（成功、失败、抛异常三条路都会走到那里）。
        # 只在成功那一支解除的话，一次网络抖动就会让保存按钮一直灰着 —— 而那时候
        # 弹层里明明摆着一份可以编辑、可以提交的值。
        assert body.rindex("setBudgetModalBusy(false)") > body.index(".catch("), (
            "解除忙碌态的那一步排在了异常分支之前，失败时保存按钮会一直是灰的"
        )


class TestTheTopLevelAlert:
    def test_the_platform_overrun_is_announced_at_the_top(self):
        html = _read(DASHBOARD)
        marker = html.index('id="aiuPlatformAlert"')
        assert 'role="alert"' in html[marker - 120: marker + 80]
        # 它必须排在「各项目消耗」之前：平台档超了挡的是所有项目，不是某一个。
        assert marker < html.index('id="aiuProjectRows"')

    def test_the_alert_only_appears_when_the_platform_is_over(self):
        body = _function_body(_dashboard_script(), "renderPlatformAlert")
        assert "if (!data.over)" in body
        assert "box.hidden = true" in body
        assert "手动与定时都挡" in body


# ==========================================================================
#  四、房子规矩：令牌、等宽数字、断行、三态
# ==========================================================================


class TestTheHouseRules:
    def test_the_new_styles_use_design_tokens_not_raw_hex(self):
        """颜色一律走 `var(--token, 字面量)`；`--aiu-*` 别名层里的兜底值先摘掉。"""
        css = _added_css()
        without_fallbacks = re.sub(r"var\(--[\w-]+,\s*#[0-9a-fA-F]{3,8}\)", "VAR", css)
        leftovers = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_fallbacks)
        assert not leftovers, f"本轮新增的样式里出现了不走 token 的颜色：{leftovers}"

    def test_the_new_markup_uses_no_emoji_as_icons(self):
        html = _read(DASHBOARD)
        emoji = re.compile("[\U0001f300-\U0001faff☀-➿️⬀-⯿]")
        found = emoji.findall(html)
        assert not found, f"出现了 emoji 图标：{found[:5]}"

    def test_the_new_numeric_cells_use_tabular_figures(self):
        css = _added_css()
        assert "font-variant-numeric: tabular-nums" in css

    def test_the_long_chinese_text_can_wrap(self):
        """**本仓库踩过的坑**：flex / grid 子项默认 `flex-shrink: 1`，一个被压窄的
        中文标签会在任意两个字之间断行，整段变成一字一行。所以每个可能装长文本的
        容器都要有 `min-width: 0` + 换行，而不是靠「看起来还行」。"""
        css = _added_css()
        assert "flex-wrap: wrap" in css
        assert "min-width: 0" in css
        assert "overflow-wrap: anywhere" in css

    def test_every_new_control_reaches_the_touch_target(self):
        html = _read(DASHBOARD)
        for dom_id in ("aiuPlatformPeriodSelect", "aiuPlatformTokenInput",
                       "aiuPlatformCostInput", "aiuBudgetPeriodSelect",
                       "aiuBudgetTokenInput", "aiuBudgetCostInput"):
            tag = re.search(rf'<(?:input|select)[^>]*id="{dom_id}"[^>]*>', html, re.S)
            assert tag, f"找不到 {dom_id}"
            classes = re.search(r'class="([^"]*)"', tag.group(0))
            assert classes and "aiu-input" in classes.group(1) or "aiu-select" in classes.group(1), (
                f"{dom_id} 没有走统一控件样式（那个样式带 44px 的触控目标）"
            )

    def test_the_rough_range_is_not_hardcoded_in_the_markup(self):
        """范围的唯一事实源是服务端的 `FIELD_RULES`。写在 HTML 里就会出现
        「界面写 1~30、后端按别的范围校验」—— 用户按界面提示填，保存却报错。"""
        html = _read(DASHBOARD)
        for dom_id in ("aiuPlatformTokenInput", "aiuPlatformCostInput",
                       "aiuBudgetTokenInput", "aiuBudgetCostInput"):
            tag = re.search(rf'<input[^>]*id="{dom_id}"[^>]*>', html, re.S)
            assert tag, f"找不到 {dom_id}"
            assert not re.search(r'\b(min|max|step)="', tag.group(0)), (
                f"{dom_id} 把范围写死在 HTML 里了"
            )

    def test_the_platform_card_has_all_three_states(self):
        html = _read(DASHBOARD)
        # 加载：骨架 + live region
        loading = html[html.index('id="aiuPlatformBudgetLoading"'):]
        loading = loading[: loading.index("</div>") + 6]
        assert 'role="status"' in loading and "aiu-skeleton-line" in loading
        # 错误：role=alert + 重试按钮
        assert 'id="aiuPlatformBudgetError"' in html
        assert 'id="aiuPlatformBudgetRetryBtn"' in html
        # 空态：由 renderPlatformBudget 写「还没有配置（当前不限制）」
        assert "还没有配置（当前不限制）" in html or "还没有配置（当前不限制）" in _read(DASHBOARD)

    def test_prefers_reduced_motion_is_respected(self):
        assert "prefers-reduced-motion" in _dashboard_style()


# ==========================================================================
#  五、界面消费的每一个键都真的存在
# ==========================================================================


class TestTheInterfaceConsumesRealKeys:
    """界面上有三十来个取值点。**每一个都要在真实响应里存在** —— 拼错一个键不会
    报错，只会让那一格永远是空的（「未上报」），而这正好是本页最容易被当成
    「上游没报数」的样子。"""

    def test_the_overview_carries_every_key_the_linkage_reads(self):
        with flask_app.app_context():
            create_tables()
            body = usage_overview([], None)

        link = body["budget_link"]
        for key in ("filter_range", "filter_label", "periods", "mixed", "aligned",
                    "target_range", "target_label", "note"):
            assert key in link, f"budget_link 少了 {key}"

        status = body["platform_status"]
        for key in ("limited", "over", "period", "period_label", "limits", "used",
                    "ratios", "over_limits", "reason", "notes"):
            assert key in status, f"platform_status 少了 {key}"
        for key in ("tokens", "cost", "currency"):
            assert key in status["limits"]
        for key in ("tokens", "cost", "currency", "runs"):
            assert key in status["used"]
        for key in ("tokens", "cost"):
            assert key in status["ratios"]

        budget = body["platform_budget"]
        for key in ("period", "token_limit", "cost_limit", "configured",
                    "updated_by", "updated_at"):
            assert key in budget, f"platform_budget 少了 {key}"

    def test_a_project_row_carries_the_two_scope_budget_block(self):
        """有行的那种响应里，键的形状也必须成立 —— 空屏正好掩盖掉拼错的键。"""
        import uuid as _uuid
        from datetime import datetime, timezone

        from models import Project
        from models import db as _db
        from models.ai_analysis import AiAnalysisRun

        with flask_app.app_context():
            create_tables()
            project = Project(code=f"P_{_uuid.uuid4().hex[:8]}", name="界面契约项目")
            _db.session.add(project)
            _db.session.flush()
            moment = datetime.now(timezone.utc)
            _db.session.add(AiAnalysisRun(
                project_id=project.id, target_type="weekly", target_id=project.id,
                target_key="group-a", status="succeeded", scope="full",
                trigger_source="manual", response_text="结论", model="fake-model",
                created_at=moment, started_at=moment, finished_at=moment,
                tokens_input=10, tokens_output=1, pricing_version="t-1",
            ))
            _db.session.commit()
            body = usage_overview(None, None)

        rows = [item for item in body["projects"] if item["project_id"] == project.id]
        assert rows, "造完运行还是没出现在总览里，下面的键断言会落空"
        budget = rows[0]["budget"]

        for key in ("limited", "over", "period", "period_label", "limits", "used",
                    "ratios", "over_limits", "reason", "notes", "platform",
                    "over_scopes", "align_range", "matches_filter"):
            assert key in budget, f"projects[].budget 少了 {key}"
        for key in ("limited", "over", "period", "period_label", "limits", "used",
                    "ratios", "over_limits"):
            assert key in budget["platform"], f"budget.platform 少了 {key}"

    def test_the_platform_budget_endpoint_returns_the_same_shape(self):
        with flask_app.app_context():
            create_tables()
            budget = platform_budget_public()
            status = platform_budget_status()

        assert set(budget) == {"period", "token_limit", "cost_limit", "configured",
                               "updated_by", "updated_at"}
        for key in ("limited", "over", "period", "period_label", "limits", "used",
                    "ratios", "over_limits", "reason", "notes"):
            assert key in status, f"status 少了 {key}"
        # 没配过时：`configured` 为假，两个上限都是 `None`（**不是 0**）。
        assert budget["period"] in PERIOD_CHOICES
        assert budget["token_limit"] is None or isinstance(budget["token_limit"], int)
        assert budget["cost_limit"] is None or isinstance(budget["cost_limit"], str)


# ==========================================================================
#  六、既有断言：写接口的计数
# ==========================================================================


class TestTheWriteSurfaceGrewButStayedNarrow:
    """本页新增了写接口，所以 `test_ai_usage_filters_and_budget.py` 里「只有一个 POST」
    的计数必须更新。这里把那条不变量**加强**一份，写清楚为什么这不是放宽：每一个 POST
    的**目标 URL** 都必须落在允许的那几个端点上 —— 原来只数条数，多一个打向别处的
    POST 只要总数对得上就看不出来。

    两次变动一起记在这里：先加了平台总预算与项目预算两个配置端点（1 → 3），后来又加了
    统计口径的两个写接口（设置起点 / 全量重置，3 → 4 —— 它们共用 `postStatistics`，
    所以在脚本里只多出一条，目标是变量 `url`）。"""

    def test_every_post_targets_a_configuration_endpoint(self):
        targets = _post_targets(_dashboard_script())
        assert len(targets) == 4, f"写接口的数量变了，请逐个核对：{targets}"
        for target in targets:
            assert (
                target == "'/ai-analysis/platform-budget'"
                # 共用帮助函数（`postStatistics`）的那一条：调用点见下一条用例。
                or target == "url"
                or re.fullmatch(
                    r"'/ai-analysis/projects/' \+ projectId \+ '/config'", target
                )
            ), f"有一个写请求打到了别处：{target}"

    def test_every_post_statistics_call_site_is_a_statistics_endpoint(self):
        """`postStatistics` 的**每一个调用点**传进去的 URL。

        只核对函数体是不够的：URL 是参数，函数体里看不出它指向哪 —— 而这正是
        「多一个打向别处的写请求」最容易藏身的地方。
        """
        script = _dashboard_script()
        calls = set(re.findall(r"postStatistics\(\s*('[^']*')", script))

        assert calls == {
            "'/ai-analysis/statistics/baseline'",
            "'/ai-analysis/statistics/reset'",
        }, f"统计口径的写接口多了一个：{sorted(calls)}"

    def test_the_page_still_has_no_destructive_writes(self):
        """破坏性方法一个都不许有。

        「全量重置」确实会删数据，但它走的是 **POST + 确认词**（`/ai-analysis/statistics/reset`）。
        这条断言守的从来不是「页面上没有删除动作」，而是「删除动作不许绕过那条有确认词、
        有权限、有「在途运行就拒绝」的路径」—— 一个 DELETE 请求会把这三道一起绕开。
        """
        script = _dashboard_script()
        for banned in ("method: 'DELETE'", "method: 'PUT'", "method: 'PATCH'"):
            assert banned not in script, f"消耗面板上出现了破坏性写操作：{banned}"
        # 破坏性端点只有一个调用点（`submitStatsReset`），而且要求确认词。
        assert script.count("'/ai-analysis/statistics/reset'") == 1
        reset_body = script[script.index("function submitStatsReset("):]
        reset_body = reset_body[: reset_body.index("\n    function ")]
        assert "resetConfirmWord()" in reset_body


def _post_targets(script: str) -> list:
    """每个 `method: 'POST'` 所在那次 `fetch` 的第一个参数（目标 URL）。

    只看 POST 的那几次调用：`'/ai-analysis/platform-budget'` 这个字面量在 GET 与 POST
    里各出现一次，按字面量计数会把 GET 也算进来，于是「条数对得上」变成一个假结论。
    """
    targets = []
    for match in re.finditer(r"method: 'POST'", script):
        window = script[max(0, match.start() - 800): match.start()]
        at = window.rfind("fetch(")
        assert at >= 0, "一个 POST 不在任何 fetch 调用里"
        targets.append(window[at + len("fetch("):].split(",")[0].strip())
    return targets
