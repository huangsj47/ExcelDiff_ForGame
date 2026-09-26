# -*- coding: utf-8 -*-
"""三个预算输入框按 **M** 填（0.6 / 100 / 3），落库仍是原始单位。

## 为什么这件事值得单独一组用例

单位换算的失效**不报错、只错六个数量级**。三种形态：

1. **回填没换算** —— 库里存着 560000，输入框显示 `560000`，而框上的 `min` 是 0.15
   （schema 按 M 给的那一份）。用户什么都没动、点保存，界面判「超过上限 2」全红。
   他会以为配置坏了，而库里那个数是好的。
2. **收集没换算** —— 用户在框里填 `0.6`（他读到的就是「按 M 填」），提交上去 0.6，
   服务端按原始单位收下 → 水位变成 0 字符。**分析当场什么都装不下**，
   而页面上没有任何异常。
3. **最小的一类：回填截位** —— 显示 `0.56` 没问题，但显示 `1.235` 而库里是
   `1234567` 就是「用户没动过的数被改掉了」。这一种最阴：它只在非整数倍的配置上出现，
   而凡是人手工敲过的数都可能是非整数倍。

## 判据为什么是**往返**，不是「换算函数返回值对」

单看 `aiToMillions(560000) === '0.56'` 一个实现乘以 100 万也能编过去（只是另一个方向
错）。往返（`from(to(x)) === x`）才把两个方向绑成一件事，而且它正是用户在界面上做的事
（打开 → 保存）。**反方向的一半同样要有**：非 M 的栏（间隔分钟数）必须原样通过 ——
否则一个「所有栏都除一百万」的实现也能让往返通过，而那会把周期间隔变成 0.00012。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "templates/merged_project_view.html"

#: 三个按 M 填的字段（顺序无关）。这份清单是**断言的一部分**：漏一个或塞进第四个
#: 都要在这里说清楚，而不是让模板自己说了算。
M_FIELDS = ["prompt_char_budget", "budget_token_limit", "single_run_token_limit"]

#: 三个输入框的 DOM id（模板里的写法）。
M_INPUTS = [
    ("aiPromptCharBudgetInput", "prompt_char_budget"),
    ("aiSingleRunBudgetInput", "single_run_token_limit"),
    ("aiBudgetTokenInput", "budget_token_limit"),
]


def _read() -> str:
    return (PROJECT_ROOT / TEMPLATE).read_text(encoding="utf-8")


def _script() -> str:
    """模板里 AI 配置那一段 `<script>`（到下一个无关函数为止）。"""
    content = _read()
    start = content.index("// AI 分析配置：页面摘要行 + 居中模态框")
    return content[start : content.index("function normalizeRiskLevel(level) {")]


def _strip_js_comments(code: str) -> str:
    """剥掉注释再断言 —— 这一段的注释里原样引用着要禁掉的写法，不剥会假绿/假红。"""
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


# ==========================================================================
# 一、真跑：把模板里那两个换算函数抽出来在 node 里跑往返
# ==========================================================================

_PROBE = r"""
__EXTRACTED__

globalThis.__probe = {
    toM: function (value) { return aiToMillions(value); },
    fromM: function (raw) { return aiFromMillions(raw); },
    bounds: function (rule, field) { return aiRuleBounds(rule, field); },
};
"""

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sandbox = { console: console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(payload.probe, sandbox, { filename: 'units_probe.js' });
const P = sandbox.__probe;

const out = { roundTrip: {}, bounds: {}, raw: {} };
for (const raw of payload.roundTrip) {
    const shown = P.toM(raw);
    out.roundTrip[String(raw)] = { shown: shown, back: P.fromM(shown) };
}
for (const spec of payload.bounds) {
    out.bounds[spec.name] = P.bounds(spec.rule, spec.field);
}
for (const raw of payload.raw) {
    out.raw[String(raw)] = P.toM(raw);
}
process.stdout.write(JSON.stringify(out));
"""

_RESULTS: dict = {}


def _probe_source() -> str:
    """抽取**模板里逐字那一份**换算代码（抄一份进测试就没有意义了）。"""
    script = _script()
    extracted = []
    for name in ("AI_MILLION", "AI_MILLION_FIELDS"):
        match = re.search(rf"^const {name} = .*?;$", script, re.M | re.S)
        assert match, f"模板里没有 `const {name} = …;`"
        extracted.append(match.group(0))
    for name in ("aiToMillions", "aiFromMillions", "aiRuleBounds"):
        start = script.index(f"function {name}(")
        brace = script.index("{", start)
        depth, index = 0, brace
        while index < len(script):
            if script[index] == "{":
                depth += 1
            elif script[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        extracted.append(script[start : index + 1])
    return _PROBE.replace("__EXTRACTED__", "\n".join(extracted))


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_units_driver.js"
    payload = workdir / "ai_units_cases.json"
    driver.write_text(_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps(
            {
                "probe": _probe_source(),
                # 往返样本：平台的默认值、范围下限、上界，以及**一个手工敲过的非整数倍**
                # （1,234,567 —— 这一条是「回填截位」那类缺陷的唯一见证）。
                "roundTrip": [
                    560_000, 150_000, 2_000_000, 3_000_000, 500_000, 50_000_000,
                    100_000_000, 1_500_000, 1_234_567, 1,
                ],
                "raw": ["", None, 0, -5, "0.6", "abc"],
                "bounds": [
                    {"name": "chars", "field": "prompt_char_budget",
                     "rule": {"min": 150_000, "max": 2_000_000}},
                    {"name": "single", "field": "single_run_token_limit",
                     "rule": {"min": 500_000, "max": 50_000_000}},
                    # 反向的一半：**非 M 的栏一个字都不许改**。
                    {"name": "interval", "field": "weekly_interval_minutes",
                     "rule": {"min": 5, "max": 10_080}},
                    {"name": "open", "field": "budget_token_limit",
                     "rule": {"min": None, "max": 1_000_000_000_000}},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"模板里的换算代码在 node 里跑不起来：\n{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


def test_every_value_the_form_can_show_comes_back_as_itself():
    """**往返必须逐字相等** —— 界面显示什么，保存就写回什么。

    这一条同时管住三件事：回填换了算（否则 `back` 差六个数量级）、收集没换回来
    （同上）、以及**回填截位**（1,234,567 显示成 1.235 就存回 1,235,000 —— 那是把用户
    没动过的配置改掉了）。所以样本里必须有一个非整数倍值，否则截位那一类看不出来。
    """
    round_trip = _run_node()["roundTrip"]

    for raw, pair in round_trip.items():
        assert pair["shown"] != "", f"{raw} 回填成了空 —— 那会把用户的配置显示成「没配」"
        assert pair["back"] == raw, (
            f"{raw} 在界面上显示成 {pair['shown']}，保存写回的是 {pair['back']}"
        )
    # 样本本身要有效：至少有一个非整数倍，否则上面那条对「截位」是盲的。
    assert "1234567" in round_trip
    assert round_trip["1234567"]["shown"] == "1.234567", round_trip["1234567"]


def test_a_blank_or_nonsense_input_stays_blank_instead_of_becoming_zero():
    """**空串是「留空」，不是 0。** 这一条从 `aiFromMillions` 那一侧看是「不写键」，
    从回填那一侧看是「显示成空框」——两个方向都要，否则「单次预算留空 = 平台按规模算」
    会被存成「0 = 一个 token 都不许花」，把分析整个锁死。
    """
    raw = _run_node()["raw"]

    for value in ("", "abc", 0, -5):
        assert raw[str(value)] == "", f"{value!r} 回填成了 {raw[str(value)]!r}，应当是空"
    # 而**字符串形态的原始值**要认：`data.prompt_char_budget` 从 JSON 来，而老行上是
    # 字符串（`resolved()` 之外的那条路）。按 `Number()` 认，不是按 `===` 认。
    assert _run_node()["roundTrip"]["560000"]["shown"] == "0.56"


def test_the_bounds_are_scaled_for_the_million_fields_and_only_those():
    """`aiRuleBounds` 是「这一栏要不要按 M」的**唯一产地**：`min`/`max` 属性、旁注、
    本地校验三处都读它。漏掉任何一处就会出现「浏览器拿 3 去和 500000 比」。

    反向的一半同样要钉：**非 M 的栏必须原样通过** —— 一个「所有栏都除以一百万」的
    实现也能让上面那条通过，而那会把「分析间隔 120 分钟」变成 0.00012。
    """
    bounds = _run_node()["bounds"]

    assert bounds["chars"] == {"min": 0.15, "max": 2, "note": "M"}, bounds["chars"]
    assert bounds["single"] == {"min": 0.5, "max": 50, "note": "M"}, bounds["single"]
    assert bounds["interval"] == {"min": 5, "max": 10_080, "note": ""}, bounds["interval"]
    # 可空那一栏没有下限（`null` 要原样传下去，不能被当成 0）。
    assert bounds["open"]["min"] is None and bounds["open"]["max"] == 1_000_000, bounds["open"]


# ==========================================================================
# 二、接线：三处（回填 / 收集 / 校验）都真的走了那两个函数
# ==========================================================================
# 「函数写得对」与「它被调用了」是两件事：只测前者的话，一个把结果算完就扔掉的实现
# 照样全绿 —— 这正是 `aiPromptCharBudgetInput` 那种「值永远填不上」的形态。


def test_the_three_fields_are_declared_as_million_fields():
    """清单本身只有一处：`AI_MILLION_FIELDS`。多一个少一个都要在这里说清。"""
    script = _script()
    match = re.search(r"const AI_MILLION_FIELDS = \[(.*?)\];", script)
    assert match, "`AI_MILLION_FIELDS` 不见了"
    fields = re.findall(r"'([a-z_]+)'", match.group(1))

    assert sorted(fields) == sorted(M_FIELDS), fields


def test_the_form_fills_the_three_fields_through_the_conversion():
    """**回填**那条路必须走 `aiToMillions` —— 直接 `setValue(id, data.x)` 会让框里
    出现 560000，而它的 `min` 是 0.15。"""
    script = _script()
    body = script[script.index("function fillAiConfigForm") : script.index("function renderAiConfigSummary")]

    for dom_id, field in M_INPUTS:
        assert f"aiToMillions(data.{field})" in body, (
            f"`{dom_id}` 的回填没有换算（`aiToMillions(data.{field})` 不在那段里）"
        )


def test_the_form_collects_the_three_fields_through_the_conversion():
    """**收集**那条路必须走 `millionValue`（内部是 `aiFromMillions`）。

    漏了它，用户填的 `0.6` 会原样提交成 0.6 个字符 —— 页面全绿、分析什么都装不下。
    """
    script = _script()
    body = script[script.index("function collectAiConfigPayload") : script.index("function aiEndpointPayload")] \
        if script.index("function collectAiConfigPayload") < script.index("function aiEndpointPayload") \
        else script[script.index("function collectAiConfigPayload"):]

    for dom_id, field in M_INPUTS:
        assert f"millionValue('{dom_id}', '{field}')" in body, (
            f"`{dom_id}` 的收集没有换算（`millionValue('{dom_id}', '{field}')` 不在那段里）"
        )


def test_the_bounds_and_the_local_check_both_read_the_one_conversion():
    """上下限属性与本地校验都读 `aiRuleBounds` —— 三处单位只能有一个判据。"""
    code = _strip_js_comments(_script())

    assert "const bounds = aiRuleBounds(rule, field);" in code, "`applyAiFieldSchema` 没走换算"
    assert "input.min = bounds.min;" in code and "input.max = bounds.max;" in code
    # 本地校验：比之前先换单位（`aiFieldProblem` 里那一处）。
    problem = code[code.index("function aiFieldProblem") : code.index("function validateAiFieldLocally")]
    assert "aiRuleBounds(rule, field)" in problem, (
        "`aiFieldProblem` 拿原始单位的上下限去比 M 的输入 —— 合法的 0.56 会被判「小于 150000」"
    )


def test_the_inputs_are_not_pinned_to_a_step_grid():
    """三个 M 输入框**不许带 `step="0.01"`**。

    那个 step 会把输入钉在一条 1 万字的网格上，而网格起点是 schema 给的 `min`
    （0.15 / 0.5）—— 平台自己的默认值（560000 → 0.56、3000000 → 3）未必落在上面，
    于是浏览器判 `:invalid`、我们自己的校验判合法，两套说法同时摆在页面上。
    """
    html = _read()

    for dom_id, _field in M_INPUTS:
        tag = html[html.index(f'id="{dom_id}"') - 400 : html.index(f'id="{dom_id}"')]
        tag = tag[tag.rindex("<input") :]
        assert 'step="any"' in tag, f"`{dom_id}` 还带着一个固定 step：{tag}"
        assert 'step="0.01"' not in tag, f"`{dom_id}` 被钉在 0.01 的网格上：{tag}"
