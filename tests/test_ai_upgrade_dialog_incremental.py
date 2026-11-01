#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP4：升级弹窗的「三键 + 两档代价并排」回归（契约 1.9 前端部分）。

## 这一轮改掉的是什么

两份周版本模板里，升级确认框原先**只有一个正解**（方案 §4.8 第 2 条、§4.10 第 3/4 条）：

* 没有可复用基线（`baseline.hasResult === false`）→ 「继续（按全量）/ 取消」；
* `estimate.upgrade_reason` 非空 → 同样只有「继续（按全量）/ 取消」。

于是用户点一次「增量分析」、确认一次，POST 就变成 `analysis_mode='full'`：平台升级一次，
基线被 `force_full` 抹一次，下一轮又没有基线 —— 「首次全量后一直全量」就是这么固化的。

现在两处都是三键：**继续增量（默认）/ 改跑全量 / 取消**，并且并排给出两档代价区间
（增量一次、全量一次，都来自服务端的预估端点）。

## 为什么静态断言 + node 真跑两层都要

* 「三键存在」「两条 `analysis_mode` 路径都在」是**形状**，静态断言就够；
* 「默认键真的返回 incremental」「两个区间真的都要了」「`mode_decision` 缺字段不炸」是
  **行为** —— 静态断言分不清「代码存在」与「代码跑得通」。所以把模板里**逐字那一份**
  决策代码抽出来在 node 里真跑（与 `tests/test_ai_dual_mode_and_estimate_ui.py` 同一手法）。

静态断言之前一律先剥注释：本仓库的注释习惯是把「旧写法 → 新写法」原样写在注释里，
不剥就会出现假绿/假红。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

WEEKLY = [
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
]

with_rels = pytest.mark.parametrize("rel", WEEKLY, ids=["weekly", "merged"])

# 这一轮必须出现在原因码表里的三个码（前两个由 WP2 的 `mode_decision` 产出，
# `runtime_scope_upgrade` 是运行期那一个 —— 它们原先都会印成「平台给出的原因码：xxx」）。
NEW_REASON_CODES = ("no_conclusion_baseline", "force_full_rebuild", "runtime_scope_upgrade")

THREE_CHOICE_LABELS = ("继续增量（默认）", "改跑全量", "取消")


# ---------------------------------------------------------------------------
#  取源码
# ---------------------------------------------------------------------------

def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _strip_js_comments(code: str) -> str:
    """剥掉 JS 注释，**字符串字面量里的 `//` 不动**（`'https://…'` 会被吃掉的）。"""
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


def _script(rel: str) -> str:
    """模板里那段内联 `<script>`，**已剥注释**。"""
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _read(rel), re.S)
    assert blocks, f"{rel} 里没有内联脚本"
    return _strip_js_comments("\n".join(blocks))


def _body_from(script: str, marker: str) -> str:
    """从 `marker` 处那个 `{` 开始按花括号配平，取出 `{…}` 的完整源码。"""
    brace = script.index("{", script.index(marker))
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[brace: index + 1]
        index += 1
    raise AssertionError(f"花括号没配平：{marker}")


def _function_source(script: str, name: str) -> str:
    """`[async] function <name>(…) { … }`（`async` 必须一起带上，否则 `await` 语法错误）。"""
    marker = f"function {name}("
    start = script.index(marker)
    head_start = start - len("async ") if script[:start].endswith("async ") else start
    head = script[head_start: script.index("{", start)]
    return head + _body_from(script, marker)


def _const_object_source(script: str, name: str) -> str:
    marker = f"const {name} = "
    assert marker in script, f"模板里没有 {marker!r}"
    return marker + _body_from(script, marker) + ";"


# ---------------------------------------------------------------------------
#  静态断言：形状
# ---------------------------------------------------------------------------

@with_rels
def test_both_templates_offer_the_three_choices_with_incremental_as_default(rel: str):
    """两处升级弹窗都必须是三键，默认键是「继续增量」。"""
    dialog = _function_source(_script(rel), "weeklyAiUpgradeChoiceDialog")

    for label in THREE_CHOICE_LABELS:
        assert f"label: '{label}'" in dialog, f"{rel}: 三键里没有「{label}」"
    values = re.findall(r"value: '(\w+)'", dialog)
    assert values == ["incremental", "full", "cancel"], (
        f"{rel}: 三个键的取值不是 增量/全量/取消：{values}"
    )
    assert "defaultChoice: 'incremental'" in dialog, (
        f"{rel}: 默认键不是「继续增量」（Enter / 焦点会落到别的键上）"
    )
    assert "className: 'btn-primary'" in dialog, f"{rel}: 默认键不是主按钮样式"
    # 「改跑全量」是危险动作，必须仍是红色语义，不能与默认键混成一种颜色。
    assert "label: '改跑全量', className: 'btn-danger'" in dialog, dialog


@with_rels
def test_incremental_and_full_stay_two_separate_post_paths(rel: str):
    """`analysis_mode` 只有**一个**来源：用户点的那个键。

    默认键 → `'incremental'`；只有「改跑全量」→ `'full'`。旧写法里那两处升级分支
    都直接返回 `'full'`，所以这里同时钉住「两条路径都还在、且不是同一条」。
    """
    script = _script(rel)
    assert script.count("analysis_mode:") == 1, (
        f"{rel}: `analysis_mode` 有 {script.count('analysis_mode:')} 处来源，只该有一个"
    )
    assert "analysis_mode: requestedMode," in script
    assert "const requestedMode = mode === 'full' ? 'full' : weeklyAiModeValue();" in script

    dialog = _function_source(script, "weeklyAiUpgradeChoiceDialog")
    assert "if (choice === 'incremental') return 'incremental';" in dialog, rel
    assert "if (choice === 'full') return 'full';" in dialog, rel
    assert "return null;" in dialog, f"{rel}: 取消没有落成 null（调用方会照样开跑）"


@with_rels
def test_no_single_exit_to_full_remains(rel: str):
    """两处旧分支的单一出口（「继续（按全量）」）必须都不在了。

    旧写法在两处都只有 `return choice === 'ok' ? 'full' : null;` —— 也就是说
    「点继续」= 「按全量」。现在它**只允许出现在用户自己点的全量按钮那条分支里**。
    """
    script = _script(rel)
    assert "继续（按全量）" not in script, f"{rel}: 还留着「继续（按全量）」这个单一出口"
    assert script.count("return choice === 'ok' ? 'full' : null;") == 1, (
        f"{rel}: 「只有确认/取消」的那条全量分支不止一处 —— 升级那两处必须走三键"
    )
    assert "return weeklyAiUpgradeChoiceDialog(" in script, (
        f"{rel}: 升级分支没有走三键弹窗"
    )
    # 两处升级分支（没有基线 / 平台给了原因或建议）都必须落到三键弹窗。
    choose = _function_source(script, "weeklyAiChooseMode")
    assert choose.count("return weeklyAiUpgradeChoiceDialog(") == 2, (
        f"{rel}: 升级分支只接了一处（没有基线 / 有升级原因或建议 应当各一处）：\n{choose}"
    )


@with_rels
def test_both_cost_ranges_are_requested_for_the_dialog(rel: str):
    """弹窗里必须并排给两档代价：增量一次估算、全量一次估算。"""
    choose = _function_source(_script(rel), "weeklyAiChooseMode")

    assert "fetchWeeklyAiEstimate('incremental')" in choose, f"{rel}: 没有要增量那一档的区间"
    assert "fetchWeeklyAiEstimate('full')" in choose, f"{rel}: 没有要全量那一档的区间"
    # 两个模式的请求必须真的不同（`mode` 参数决定服务端选哪批历史样本）。
    assert "params.set('mode', mode === 'full' ? 'full' : 'incremental');" in _script(rel)

    compare = _function_source(_script(rel), "weeklyAiCostCompare")
    assert "增量（只覆盖本轮新增）" in compare and "全量（整窗重跑）" in compare, (
        f"{rel}: 对照表的两个列名没有写清哪一列是哪个口径"
    )
    for metric in ("预计文件数", "预计 token", "预计耗时", "预计费用"):
        assert f"'{metric}'" in compare, f"{rel}: 对照表少了「{metric}」这一行"


@with_rels
def test_the_copy_says_what_incremental_cannot_see(rel: str):
    """给「仍然增量」必须写明后果：只覆盖本轮新增，上轮未取证的部分仍不看。"""
    dialog = _function_source(_script(rel), "weeklyAiUpgradeChoiceDialog")
    assert "只覆盖本轮新增" in dialog, rel
    assert "上轮未取证的部分仍然不看" in dialog, rel


@with_rels
def test_the_upgrade_reason_table_covers_runtime_codes(rel: str):
    """三个新码要有中文文案，而且不许再印「平台给出的原因码：xxx」。"""
    script = _script(rel)
    reasons = _const_object_source(script, "WEEKLY_AI_UPGRADE_REASONS")
    for code in NEW_REASON_CODES:
        assert f"{code}:" in reasons, f"{rel}: 原因码表里没有 {code}"
    # 原有的五个码不许在补齐时被顺手删掉（旧后端仍会回 `first_run` / `empty_total`）。
    for code in ("first_run", "delta_count_high", "delta_ratio_high",
                 "critical_path_detected", "empty_total"):
        assert f"{code}:" in reasons, f"{rel}: 原因码表丢了 {code}"

    assert "平台给出的原因码" not in script, f"{rel}: 还在印「平台给出的原因码：xxx」"


@with_rels
def test_mode_decision_is_read_with_a_fallback_to_the_old_field(rel: str):
    """`mode_decision` 的字段缺了要退回旧字段，不许报错、也不许编。"""
    script = _script(rel)
    decision = _function_source(script, "weeklyAiModeDecision")

    assert "source.mode_decision || {}" in decision, f"{rel}: 没有读 mode_decision"
    assert "pick('suggested_full', false) === true" in decision, (
        f"{rel}: suggested_full 缺省时没有当成「不建议」（真值判断会把 undefined 当成有建议）"
    )
    assert "pick('upgrade_reason', source.upgrade_reason || '')" in decision, (
        f"{rel}: upgrade_reason 缺了没有退回 estimate.upgrade_reason"
    )
    assert "pick('can_continue_incremental', true) !== false" in decision, (
        f"{rel}: can_continue_incremental 缺省时没有按「可以继续增量」处理"
    )
    # 判定必须在两处升级分支里都生效（没有基线 / 有原因或建议）。
    choose = _function_source(script, "weeklyAiChooseMode")
    assert choose.count("weeklyAiModeDecision(") >= 2, choose


@with_rels
def test_the_compare_block_is_rendered_by_the_shared_dialog(rel: str):
    """对照表要由共用弹窗真的画出来（算了不画 = 用户还是只看到一个数）。"""
    dialog = _function_source(_script(rel), "weeklyAiChoiceDialog")

    assert "if (spec.compare) appendCompare(box, spec.compare);" in dialog, rel
    assert "weekly-ai-dialog__compare" in dialog, rel
    # 弹窗里两个默认键之外的按钮仍要能渲染（mismatch 那条分支在用）。
    assert "spec.choices && spec.choices.length" in dialog, rel


def test_the_new_block_is_byte_identical_across_the_two_templates():
    """两份模板是同一逻辑的副本 —— 这一轮新加的那几段也必须逐字相同。

    `tests/test_ai_dual_mode_and_estimate_ui.py` 已经比对过旧的六个成员；这里只钉
    **本轮新加**的四个，防止「改一份、忘了另一份」恰好落在新代码上。
    """
    blocks = []
    for rel in WEEKLY:
        script = _script(rel)
        blocks.append("\n".join([
            _const_object_source(script, "WEEKLY_AI_UPGRADE_REASONS"),
            _function_source(script, "weeklyAiUpgradeReasonText"),
            _function_source(script, "weeklyAiModeDecision"),
            _function_source(script, "weeklyAiMoneyRangeText"),
            _function_source(script, "weeklyAiCostCompare"),
            _function_source(script, "weeklyAiUpgradeChoiceDialog"),
        ]))
    assert blocks[0] == blocks[1], (
        "两份周版本模板的新增代码块不一致：先改的那份要把整段同步过去"
    )


# ---------------------------------------------------------------------------
#  node 探针：把模板里逐字那一份决策代码真跑一遍
# ---------------------------------------------------------------------------

_INCREMENTAL_ESTIMATE = {
    "mode": "incremental",
    "planned_files": 35,
    "tokens": {"low": 300000, "high": 900000, "unit": "token"},
    "duration_ms": {"low": 120000, "high": 300000},
    "cost": {"computable": True, "currency": "CNY",
             "low": {"amount": "0.50"}, "high": {"amount": "1.20"}},
    "delta_files": 35,
    "compensation_files": 20,
    "baseline_run": {"run_id": 22, "created_at": "2026-09-21T09:30:00", "scope": "incremental"},
    "upgrade_reason": "",
    "notes": [],
}

_FULL_ESTIMATE = {
    "mode": "full",
    "planned_files": 1012,
    "tokens": {"low": 1200000, "high": 3400000, "unit": "token"},
    "duration_ms": {"low": 600000, "high": 1500000},
    "cost": {"computable": True, "currency": "CNY",
             "low": {"amount": "2.00"}, "high": {"amount": "5.25"}},
    "delta_files": 35,
    "compensation_files": 20,
    "baseline_run": {"run_id": None, "not_applicable": "full", "label": "全量不使用基线"},
    "upgrade_reason": "",
    "notes": [],
}

_BASELINE_OK = {"hasResult": True, "mismatch": False, "note": "", "createdAt": "2026-09-20 10:00:00"}
_BASELINE_MISSING = {"hasResult": False, "mismatch": False, "note": "", "createdAt": ""}


def _incremental(**overrides) -> dict:
    return {**_INCREMENTAL_ESTIMATE, **overrides}


_SCENARIOS = [
    # 没有可复用的结论基线：三键，默认继续增量
    {"name": "no_baseline_incremental", "requested": "incremental", "choices": ["incremental"],
     "baseline": _BASELINE_MISSING,
     "estimates": {"incremental": _incremental(), "full": _FULL_ESTIMATE}},
    {"name": "no_baseline_full", "requested": "incremental", "choices": ["full"],
     "baseline": _BASELINE_MISSING,
     "estimates": {"incremental": _incremental(), "full": _FULL_ESTIMATE}},
    {"name": "no_baseline_cancel", "requested": "incremental", "choices": [None],
     "baseline": _BASELINE_MISSING,
     "estimates": {"incremental": _incremental(), "full": _FULL_ESTIMATE}},
    # 平台给了真正的升级原因（旧字段路径）
    {"name": "upgrade_reason_incremental", "requested": "incremental", "choices": ["incremental"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental(upgrade_reason="no_conclusion_baseline"),
                   "full": _FULL_ESTIMATE}},
    {"name": "upgrade_reason_full", "requested": "incremental", "choices": ["full"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental(upgrade_reason="runtime_scope_upgrade"),
                   "full": _FULL_ESTIMATE}},
    # WP2 的 mode_decision：只是「建议整窗重跑」时也要三键，默认仍是增量
    {"name": "suggested_full_incremental", "requested": "incremental", "choices": ["incremental"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental(mode_decision={
         "requested": "incremental", "effective": "incremental", "suggested_full": True,
         "suggestion_reason": "delta_ratio_high", "upgrade_reason": "",
         "can_continue_incremental": True}), "full": _FULL_ESTIMATE}},
    # mode_decision 里是真正的升级原因：文案必须是中文，不许把码印出来
    {"name": "mode_decision_upgrade", "requested": "incremental", "choices": ["incremental"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental(mode_decision={
         "requested": "incremental", "effective": "incremental", "suggested_full": False,
         "suggestion_reason": "", "upgrade_reason": "force_full_rebuild",
         "can_continue_incremental": True}), "full": _FULL_ESTIMATE}},
    # 平台判定只能全量：默认键置灰，但**键还在**
    {"name": "cannot_continue_incremental", "requested": "incremental", "choices": ["full"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental(mode_decision={
         "upgrade_reason": "force_full_rebuild", "can_continue_incremental": False}),
         "full": _FULL_ESTIMATE}},
    # 字段缺失 / 为 null：退回旧逻辑、不报错（普通预检，不是三键）
    {"name": "malformed_mode_decision", "requested": "incremental", "choices": ["ok"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental(mode_decision={
         "suggested_full": None, "suggestion_reason": None,
         "upgrade_reason": None, "can_continue_incremental": None})}},
    # 没有任何升级/建议：普通预检仍然只有确认/取消两个默认键
    {"name": "plain_incremental", "requested": "incremental", "choices": ["ok"],
     "baseline": _BASELINE_OK,
     "estimates": {"incremental": _incremental()}},
    # 用户自己点的全量按钮：这条仍是「确认 / 取消」，不该冒出第三键
    {"name": "full_button_ok", "requested": "full", "choices": ["ok"],
     "estimates": {"full": _FULL_ESTIMATE}},
    {"name": "full_button_cancel", "requested": "full", "choices": [None],
     "estimates": {"full": _FULL_ESTIMATE}},
]

_NOTICE_SCENARIOS = [
    {"name": f"notice_{code}", "action": "upgradeNotice",
     "job": {"requested_mode": "incremental", "effective_mode": "full",
             "upgrade_reason": code}}
    for code in NEW_REASON_CODES
]

_PROBE_TEMPLATE = """
var probeDialogs = [];
var probeChoices = [];
var probeEstimates = null;
var probeEstimateModes = [];
var probeBaseline = null;

function weeklyAiChoiceDialog(options) {
    probeDialogs.push({
        title: options.title, intro: options.intro, note: options.note,
        lines: options.lines, choices: options.choices, okText: options.okText,
        compare: options.compare, defaultChoice: options.defaultChoice
    });
    var value = probeChoices.length ? probeChoices.shift() : null;
    return Promise.resolve(value);
}
async function fetchWeeklyAiEstimate(mode) {
    probeEstimateModes.push(mode);
    return probeEstimates && probeEstimates[mode] ? probeEstimates[mode] : null;
}
async function fetchWeeklyAiBaselineState() { return probeBaseline; }

__EXTRACTED__

globalThis.__probe = {
    reset: function () {
        probeDialogs = []; probeChoices = []; probeEstimateModes = [];
        weeklyAiMismatchAccepted = false; weeklyAiUpgradeNoticeText = '';
    },
    load: function (spec) {
        // **必须拷一份**：`shift()` 会把数组吃掉，而场景表在两份模板之间是同一个对象。
        probeChoices = (spec.choices || []).slice();
        probeEstimates = spec.estimates || null;
        probeBaseline = spec.baseline === undefined ? null : spec.baseline;
    },
    choose: function (requested) { return weeklyAiChooseMode(requested); },
    state: function () {
        return { dialogs: probeDialogs, estimateModes: probeEstimateModes,
                 upgradeNoticeText: weeklyAiUpgradeNoticeText };
    },
    upgradeNotice: function (job) { return weeklyAiUpgradeNotice(job); },
    reasonText: function (code) { return weeklyAiUpgradeReasonText(code); }
};
"""

_PROBE_FUNCTIONS = (
    "weeklyAiUpgradeReasonText",
    "weeklyAiModeDecision",
    "weeklyAiUpgradeNotice",
    "weeklyAiFormatInt",
    "weeklyAiFormatDuration",
    "weeklyAiTokensText",
    "weeklyAiDurationText",
    "weeklyAiBaselineText",
    "weeklyAiMoneyText",
    "weeklyAiMoneyRangeText",
    "weeklyAiCostCompare",
    "weeklyAiEstimateLines",
    "weeklyAiSplitEstimateLines",
    "weeklyAiEstimateNote",
    "weeklyAiNoteMismatchAccepted",
    "weeklyAiComparabilityLine",
    "weeklyAiUpgradeChoiceDialog",
    "weeklyAiChooseMode",
)

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

async function runOne(probeSource, scenarios) {
    const sandbox = { console: console };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(probeSource, sandbox, { filename: 'wp4_probe.js' });
    const P = sandbox.__probe;
    const out = {};
    for (const spec of scenarios) {
        P.reset();
        P.load(spec);
        if (spec.action === 'upgradeNotice') {
            out[spec.name] = { notice: P.upgradeNotice(spec.job) };
            continue;
        }
        const mode = await P.choose(spec.requested);
        const state = P.state();
        out[spec.name] = { mode: mode, dialogs: state.dialogs,
                           estimateModes: state.estimateModes,
                           upgradeNoticeText: state.upgradeNoticeText };
    }
    return out;
}

(async function () {
    const result = {};
    for (const template of Object.keys(payload.probes)) {
        result[template] = await runOne(payload.probes[template], payload.scenarios);
    }
    process.stdout.write(JSON.stringify(result));
})().catch(function (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""

_RESULTS: dict = {}


def _probe_source(rel: str) -> str:
    script = _script(rel)
    extracted = "\n".join(
        [_const_object_source(script, "WEEKLY_AI_UPGRADE_REASONS")]
        + [_function_source(script, name) for name in _PROBE_FUNCTIONS]
        + ["var weeklyAiMismatchAccepted = false;", "var weeklyAiUpgradeNoticeText = '';"]
    )
    return _PROBE_TEMPLATE.replace("__EXTRACTED__", extracted)


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_upgrade_dialog_driver.js"
    payload = workdir / "ai_upgrade_dialog_cases.json"
    driver.write_text(_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps({
            "probes": {rel: _probe_source(rel) for rel in WEEKLY},
            "scenarios": _SCENARIOS + _NOTICE_SCENARIOS,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"模板里的升级弹窗代码在 node 里跑不起来：\n{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


def _case(rel: str, name: str) -> dict:
    return _run_node()[rel][name]


def _dialog(rel: str, name: str) -> dict:
    item = _case(rel, name)
    assert len(item["dialogs"]) == 1, f"{rel}/{name}: 应当只弹一个框：{item['dialogs']}"
    return item["dialogs"][0]


def _choices(dialog: dict) -> list[dict]:
    return dialog.get("choices") or []


# --- 三键的行为 --------------------------------------------------------------

@with_rels
def test_the_default_key_keeps_the_run_incremental(rel: str):
    """没有基线时选默认键 → `'incremental'`，不会被改写成 full。"""
    item = _case(rel, "no_baseline_incremental")
    assert item["mode"] == "incremental", item
    assert item["estimateModes"] == ["incremental", "full"], (
        f"{rel}: 弹窗前没有把两个口径的区间都要一次：{item['estimateModes']}"
    )

    dialog = _dialog(rel, "no_baseline_incremental")
    labels = [choice["label"] for choice in _choices(dialog)]
    assert labels == list(THREE_CHOICE_LABELS), f"{rel}: 三键文案/顺序不对：{labels}"
    assert dialog["defaultChoice"] == "incremental", dialog
    assert all(choice.get("disabled") is not True for choice in _choices(dialog)), (
        f"{rel}: 默认键被置灰了（用户点不了「继续增量」）"
    )


@with_rels
def test_the_upgrade_reason_path_also_keeps_incremental_by_default(rel: str):
    """带 `upgrade_reason` 的那条分支同样是三键，选默认键 → 仍发增量。"""
    item = _case(rel, "upgrade_reason_incremental")
    assert item["mode"] == "incremental", item
    assert item["estimateModes"] == ["incremental", "full"], item["estimateModes"]

    dialog = _dialog(rel, "upgrade_reason_incremental")
    assert [choice["label"] for choice in _choices(dialog)] == list(THREE_CHOICE_LABELS)
    # 原因要翻成中文（这个码的文案里有「上一轮没有产出可用的结构化结论」）。
    assert "上一轮没有产出可用的结构化结论" in dialog["intro"], dialog["intro"]
    assert "no_conclusion_baseline" not in dialog["intro"], (
        f"{rel}: 原因码原样印给用户了：{dialog['intro']}"
    )


@with_rels
def test_choosing_full_from_the_new_dialog_really_runs_full(rel: str):
    item = _case(rel, "upgrade_reason_full")
    assert item["mode"] == "full", item
    dialog = _dialog(rel, "upgrade_reason_full")
    # 全量那一列的原因文案也要是人话（`runtime_scope_upgrade`）。
    assert "范围升级成了全量" in dialog["intro"], dialog["intro"]
    assert "runtime_scope_upgrade" not in dialog["intro"]


@with_rels
def test_cancelling_the_new_dialog_creates_nothing(rel: str):
    assert _case(rel, "no_baseline_cancel")["mode"] is None


@with_rels
def test_the_two_cost_ranges_are_side_by_side(rel: str):
    """对照表：同一个指标一行，增量一列、全量一列，数字各来自自己那一档的预估。"""
    dialog = _dialog(rel, "no_baseline_incremental")
    compare = dialog.get("compare")
    assert compare, f"{rel}: 弹窗没有并排代价对照：{dialog}"
    assert len(compare["columns"]) == 3, compare["columns"]
    assert compare["columns"][0] == "代价对照"
    assert "增量" in compare["columns"][1] and "全量" in compare["columns"][2], compare

    rows = {row[0]: row for row in compare["rows"]}
    assert list(rows) == ["预计文件数", "预计 token", "预计耗时", "预计费用"], list(rows)
    assert rows["预计文件数"][1] == "35 个", rows["预计文件数"]
    assert rows["预计文件数"][2] == "1,012 个", rows["预计文件数"]
    assert "300,000" in rows["预计 token"][1] and "1,200,000" in rows["预计 token"][2]
    assert rows["预计费用"][1] == "0.50 ~ 1.20 CNY", rows["预计费用"]
    assert rows["预计费用"][2] == "2.00 ~ 5.25 CNY", rows["预计费用"]

    # 对照表已经把那四个数并排摆过了：事实表里**不许再重复一遍**（框会变长），
    # 「升级原因」也已经写在 intro 里。
    fact_labels = [row[0] for row in dialog["lines"]]
    for duplicated in ("预计文件数", "预计 token", "预计耗时", "预计费用", "升级原因"):
        assert duplicated not in fact_labels, f"{rel}: 弹窗里「{duplicated}」出现了两次"
    for required in ("本次增量文件数", "补偿文件数", "基线 run"):
        assert required in fact_labels, f"{rel}: 事实表里少了「{required}」：{fact_labels}"


@with_rels
def test_the_copy_states_what_incremental_leaves_unseen(rel: str):
    dialog = _dialog(rel, "no_baseline_incremental")
    assert "只覆盖本轮新增" in dialog["intro"], dialog["intro"]
    assert "上轮未取证的部分仍然不看" in dialog["intro"], dialog["intro"]


# --- WP2 的 mode_decision ----------------------------------------------------

@with_rels
def test_suggested_full_is_only_a_suggestion(rel: str):
    """`suggested_full` 只出提示：标题说「建议」，默认键仍是增量、且真的发增量。"""
    item = _case(rel, "suggested_full_incremental")
    assert item["mode"] == "incremental", item
    dialog = _dialog(rel, "suggested_full_incremental")
    assert "建议整窗重跑" in dialog["title"], dialog["title"]
    assert "delta_ratio_high" not in dialog["intro"], (
        f"{rel}: 建议原因码原样印出来了：{dialog['intro']}"
    )
    assert "本次变化的文件占比超过了全量阈值" in dialog["intro"], dialog["intro"]
    assert "只是平台按变化量给的建议" in dialog["note"], dialog["note"]


@with_rels
def test_a_real_mode_decision_upgrade_reason_is_shown_in_chinese(rel: str):
    item = _case(rel, "mode_decision_upgrade")
    assert item["mode"] == "incremental", item
    dialog = _dialog(rel, "mode_decision_upgrade")
    assert "必须整窗重建" in dialog["intro"], dialog["intro"]
    assert "force_full_rebuild" not in dialog["intro"], dialog["intro"]


@with_rels
def test_a_forbidden_incremental_keeps_the_key_but_greys_it(rel: str):
    """平台判定不能增量时，第三键仍在框里（只是不可点），选全量照样能走。"""
    item = _case(rel, "cannot_continue_incremental")
    assert item["mode"] == "full", item
    dialog = _dialog(rel, "cannot_continue_incremental")
    choices = {choice["value"]: choice for choice in _choices(dialog)}
    assert list(choices) == ["incremental", "full", "cancel"], choices
    assert choices["incremental"].get("disabled") is True, choices["incremental"]
    assert "不能只做增量" in dialog["note"], dialog["note"]


@with_rels
def test_missing_or_null_mode_decision_fields_fall_back_without_error(rel: str):
    """字段缺失 / 为 null → 退回旧逻辑：不抛异常，也不冒充「平台有意见」。"""
    item = _case(rel, "malformed_mode_decision")
    assert item["mode"] == "incremental", item
    dialog = _dialog(rel, "malformed_mode_decision")
    assert dialog.get("choices") is None, (
        f"{rel}: 没有升级原因/建议却弹了三键决策框：{dialog['choices']}"
    )
    assert dialog["okText"] == "开始增量分析", dialog
    assert item["estimateModes"] == ["incremental"], (
        f"{rel}: 普通增量不该额外要一次全量区间：{item['estimateModes']}"
    )


# --- 普通增量与用户自己点的全量：不许被三键污染 ------------------------------

@with_rels
def test_the_plain_precheck_stays_read_only(rel: str):
    item = _case(rel, "plain_incremental")
    assert item["mode"] == "incremental", item
    dialog = _dialog(rel, "plain_incremental")
    assert dialog.get("choices") is None, dialog["choices"]
    assert dialog["okText"] == "开始增量分析", dialog
    assert dialog.get("compare") is None, "普通预检不该带两档代价对照（它不做这个决定）"


@with_rels
def test_the_explicit_full_button_still_asks_once(rel: str):
    assert _case(rel, "full_button_ok")["mode"] == "full"
    assert _case(rel, "full_button_cancel")["mode"] is None
    dialog = _dialog(rel, "full_button_ok")
    assert dialog.get("choices") is None, (
        f"{rel}: 用户已明确点全量，却又被问了「要不要改成增量」：{dialog['choices']}"
    )
    assert dialog["okText"] == "开始全量分析", dialog


# --- 运行期原因码的通知 ------------------------------------------------------

@with_rels
@pytest.mark.parametrize("code", NEW_REASON_CODES)
def test_a_runtime_upgrade_code_is_explained_in_words(rel: str, code: str):
    """`weeklyAiUpgradeNotice` 对三个新码也要说人话（不印码）。"""
    notice = _case(rel, f"notice_{code}")["notice"]
    assert notice, f"{rel}: {code} 这个升级原因一个字都没说"
    assert code not in notice, f"{rel}: 原因码直接印给用户看了：{notice}"
    assert "全量" in notice, notice
    # P2-3（WP23）：`no_conclusion_baseline` 是「没有可复用的历史结论、输入按整窗读」，
    # `force_full_rebuild` 是「平台重建了整窗输入」—— 两个都**不是**升级。只有运行期
    # 真的扩了范围（`runtime_scope_upgrade`）才允许出现「升级」这两个字。
    # 这两档的完整措辞另见 `tests/test_ai_upgrade_notice_no_baseline.py`。
    if code != "runtime_scope_upgrade":
        assert "升级" not in notice, f"{rel}: {code} 被说成了升级：{notice}"


# ---------------------------------------------------------------------------
#  DOM 探针：对照表与三个键**真的画出来**（上面那一层把 `weeklyAiChoiceDialog` 换成了桩）
# ---------------------------------------------------------------------------

# 桩 document 只实现弹窗真正用到的那几个 API：createElement / body / 事件注册、
# appendChild / setAttribute / querySelector(`[data-weekly-ai-choice="…"]`) / lastChild。
_DOM_PROBE_TEMPLATE = r"""
var probeFocused = null;

function makeElement(tag) {
    const element = {
        tagName: tag,
        children: [],
        parentNode: null,
        attributes: {},
        className: '',
        textContent: '',
        type: '',
        disabled: false,
        handlers: {},
        appendChild: function (child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        setAttribute: function (name, value) {
            element.attributes[name] = String(value);
        },
        addEventListener: function (name, handler) {
            (element.handlers[name] = element.handlers[name] || []).push(handler);
        },
        removeChild: function (child) {
            const index = element.children.indexOf(child);
            if (index >= 0) element.children.splice(index, 1);
        },
        focus: function () { probeFocused = element; },
        querySelector: function (selector) { return findFirst(element, selector); }
    };
    Object.defineProperty(element, 'lastChild', {
        get: function () { return element.children[element.children.length - 1] || null; }
    });
    return element;
}

function eachElement(root, visit) {
    (function walk(node) {
        visit(node);
        (node.children || []).forEach(walk);
    })(root);
}

function findFirst(root, selector) {
    const match = /^\[data-weekly-ai-choice="(.+)"\]$/.exec(selector);
    if (!match) return null;
    let found = null;
    eachElement(root, function (node) {
        if (!found && node.attributes
            && node.attributes['data-weekly-ai-choice'] === match[1]) {
            found = node;
        }
    });
    return found;
}

function snapshot(node) {
    return {
        tag: node.tagName,
        className: node.className,
        text: node.textContent,
        choice: node.attributes ? node.attributes['data-weekly-ai-choice'] : undefined,
        disabled: !!node.disabled,
        children: (node.children || []).map(snapshot)
    };
}

function describe(element) {
    if (!element) return null;
    return {
        className: element.className,
        choice: element.attributes ? element.attributes['data-weekly-ai-choice'] : undefined,
        text: element.textContent
    };
}

var document = {
    body: makeElement('body'),
    createElement: function (tag) { return makeElement(tag); },
    addEventListener: function () {},
    removeEventListener: function () {}
};

__EXTRACTED__

globalThis.__dom = {
    run: async function (specs) {
        const out = {};
        for (const spec of specs) {
            probeFocused = null;
            const promise = weeklyAiChoiceDialog(spec.spec);
            // 快照要**点之前**取：`close()` 会把整个遮罩从 body 上摘掉。
            const tree = snapshot(document.body);
            const focused = describe(probeFocused);
            const button = findFirst(document.body, '[data-weekly-ai-choice="'
                + spec.click + '"]');
            if (button) button.handlers.click[0]();
            out[spec.name] = {
                value: await promise,
                tree: tree,
                focused: focused,
                found: !!button
            };
            document.body.children.length = 0;
        }
        return out;
    }
};
"""

_DOM_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

(async function () {
    const result = {};
    for (const template of Object.keys(payload.probes)) {
        const sandbox = { console: console };
        sandbox.window = sandbox;
        vm.createContext(sandbox);
        try {
            vm.runInContext(payload.probes[template], sandbox, { filename: 'wp4_dom.js' });
            result[template] = await sandbox.__dom.run(payload.specs);
        } catch (err) {
            result[template] = { error: String((err && err.stack) || err) };
        }
    }
    process.stdout.write(JSON.stringify(result));
})().catch(function (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""

_COMPARE_SPEC = {
    "columns": ["代价对照", "增量（只覆盖本轮新增）", "全量（整窗重跑）"],
    "rows": [
        ["预计文件数", "35 个", "1,012 个"],
        ["预计 token", "300,000 ~ 900,000 token", "1,200,000 ~ 3,400,000 token"],
        ["预计耗时", "2.0 分钟 ~ 5.0 分钟", "10 分钟 ~ 25 分钟"],
        ["预计费用", "0.50 ~ 1.20 CNY", "2.00 ~ 5.25 CNY"],
    ],
}

_DOM_SPECS = [
    {"name": "three_keys", "click": "incremental", "spec": {
        "title": "这次增量的口径需要你确认",
        "intro": "「继续增量」只覆盖本轮新增的文件，上轮未取证的部分仍然不看。",
        "compare": _COMPARE_SPEC,
        "lines": [["基线 run", "#22（2026-09-21 09:30，incremental）"]],
        "note": "两个区间都来自历史实测，不是精确值。",
        "defaultChoice": "incremental",
        "choices": [
            {"value": "incremental", "label": "继续增量（默认）", "className": "btn-primary"},
            {"value": "full", "label": "改跑全量", "className": "btn-danger"},
            {"value": "cancel", "label": "取消", "className": "btn-outline-secondary"},
        ],
    }},
    {"name": "greyed_incremental", "click": "full", "spec": {
        "title": "这次增量的口径需要你确认",
        "compare": _COMPARE_SPEC,
        "defaultChoice": "incremental",
        "choices": [
            {"value": "incremental", "label": "继续增量（默认）", "className": "btn-primary",
             "disabled": True},
            {"value": "full", "label": "改跑全量", "className": "btn-danger"},
            {"value": "cancel", "label": "取消", "className": "btn-outline-secondary"},
        ],
    }},
]

_DOM_RESULTS: dict = {}


def _dom_probe_source(rel: str) -> str:
    script = _script(rel)
    extracted = _function_source(script, "weeklyAiChoiceDialog")
    return _DOM_PROBE_TEMPLATE.replace("__EXTRACTED__", extracted)


def _run_dom_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过渲染断言")
    if _DOM_RESULTS:
        return _DOM_RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_upgrade_dialog_dom.js"
    payload = workdir / "ai_upgrade_dialog_dom_cases.json"
    driver.write_text(_DOM_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps({"probes": {rel: _dom_probe_source(rel) for rel in WEEKLY},
                    "specs": _DOM_SPECS}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"弹窗在 node 里渲染不起来：\n{result.stdout}\n{result.stderr}"
    )
    _DOM_RESULTS.update(json.loads(result.stdout))
    return _DOM_RESULTS


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _dom_case(rel: str, name: str) -> dict:
    result = _run_dom_node()[rel]
    assert "error" not in result, f"{rel}: 弹窗渲染抛异常：{result['error']}"
    return result[name]


@with_rels
def test_the_dialog_really_renders_the_side_by_side_comparison(rel: str):
    """对照表必须是真的 DOM：表头两列 + 每个指标一行，增量列在前。"""
    item = _dom_case(rel, "three_keys")
    assert item["value"] == "incremental", (
        f"{rel}: 点默认键没有解析成 incremental：{item['value']}"
    )
    assert item["found"], f"{rel}: 框里没有「继续增量」那个按钮"

    compare = next(
        (node for node in _walk(item["tree"])
         if node["className"] == "weekly-ai-dialog__compare"),
        None,
    )
    assert compare, f"{rel}: 弹窗里没有并排对照表"
    head, *rows = compare["children"]
    assert [cell["text"] for cell in head["children"]] == list(_COMPARE_SPEC["columns"])
    assert len(rows) == len(_COMPARE_SPEC["rows"]), rows
    assert [cell["text"] for cell in rows[0]["children"]] == ["预计文件数", "35 个", "1,012 个"]
    assert [cell["text"] for cell in rows[3]["children"]] == ["预计费用", "0.50 ~ 1.20 CNY",
                                                             "2.00 ~ 5.25 CNY"]


@with_rels
def test_the_dialog_really_renders_three_buttons_with_the_default_focused(rel: str):
    item = _dom_case(rel, "three_keys")
    buttons = [node for node in _walk(item["tree"]) if node.get("choice")]
    assert [(button["choice"], button["text"]) for button in buttons] == [
        ("incremental", "继续增量（默认）"),
        ("full", "改跑全量"),
        ("cancel", "取消"),
    ], buttons
    assert item["focused"] and item["focused"]["choice"] == "incremental", (
        f"{rel}: 默认焦点不在「继续增量」上：{item['focused']}"
    )
    assert "btn-primary" in item["focused"]["className"], item["focused"]


@with_rels
def test_a_greyed_default_key_moves_the_focus_to_the_next_actionable_one(rel: str):
    """平台判定不能增量时：键仍在（disabled），焦点落到「改跑全量」。"""
    item = _dom_case(rel, "greyed_incremental")
    assert item["value"] == "full", item["value"]
    buttons = {node["choice"]: node for node in _walk(item["tree"]) if node.get("choice")}
    assert set(buttons) == {"incremental", "full", "cancel"}, buttons
    assert buttons["incremental"]["disabled"] is True, buttons["incremental"]
    assert item["focused"] and item["focused"]["choice"] == "full", item["focused"]
