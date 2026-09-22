#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI-P0-03：双入口（增量 / 全量）+ 事前代价 + provenance 决策。

## 这一组测试为什么这么分

* **一致性**（三棵抽屉按钮 / 颜色 / 帮助 / 参数）只能用静态断言：它要问的是「三份模板
  是不是同一套语法」，跑起来反而看不出来；
* **决策逻辑**（弹不弹、弹了给什么选项、取消之后是不是什么都不做）**必须用 node 真跑**
  模板里逐字那一份代码：静态断言分不清「弹窗」与「弹窗的代码存在」——
  「provenance 没变时不弹窗」这条反向用例尤其如此，写成静态断言的话，
  一个「每次点击都弹」的实现照样能通过。

静态断言之前**一律先剥注释**：本文件与模板的注释里原样引用着被断言的写法
（错 → 对、旧 → 新都写在注释里），不剥就会出现假绿/假红。

## 已知的、本文件**不**覆盖的两条

1. `AiAnalysisJob.baseline_provenance_mismatch` **今天没有任何生产写入方**
   （`job_service.create_or_attach_job` 不收这个意图）——所以「用户选继续增量时把这件事
   记在 job 上」这一条**前端发不出去**，测试里只钉「页面自己会说这句限制」；
2. 「平台在执行中把增量升级为全量时，**事前**让用户选」——升级决策在 worker 里
   （`scope_sampling._decide_scope` 唯一的生产调用点 `build_weekly_payload`），
   做不到事前问人。本文件钉的是「事前能看见的那一种（没有基线）」与「事后说清原因」。
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


def _script(rel: str, *, keep_comments: bool = False) -> str:
    """模板里那段内联 `<script>`。默认**已剥注释**（见模块 docstring）。"""
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _read(rel), re.S)
    assert blocks, f"{rel} 里没有内联脚本"
    joined = "\n".join(blocks)
    return joined if keep_comments else _strip_js_comments(joined)


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
    """`[async] function <name>(…) { … }`（`async` 必须一起带上，否则 `await` 会语法错误）。"""
    marker = f"function {name}("
    start = script.index(marker)
    head_start = start - len("async ") if script[:start].endswith("async ") else start
    head = script[head_start: script.index("{", start)]
    return head + _body_from(script, marker)


def _const_object_source(script: str, name: str) -> str:
    """`const <name> = { … };` —— 对象字面量按花括号配平取出来。"""
    marker = f"const {name} = "
    assert marker in script, f"模板里没有 {marker!r}"
    return marker + _body_from(script, marker) + ";"


# ---------------------------------------------------------------------------
#  一致性（三棵抽屉）
# ---------------------------------------------------------------------------

def test_the_two_weekly_drawers_carry_a_byte_identical_p0_03_block():
    """两份周版本抽屉是**逐字复制**的关系，这一块也必须逐字相同。

    两份各自演化过（比如只有一份加了「可复用基线」那一行）是这一轮最可能出的错，
    而它在真实使用中表现为「同一个功能在两个入口说不一样的话」。
    """
    blocks = []
    for rel in WEEKLY:
        script = _script(rel)
        blocks.append(
            _const_object_source(script, "WEEKLY_AI_UPGRADE_REASONS")
            + "\n" + _function_source(script, "weeklyAiChooseMode")
            + "\n" + _function_source(script, "weeklyAiEstimateLines")
            + "\n" + _function_source(script, "weeklyAiUpgradeNotice")
            + "\n" + _function_source(script, "weeklyAiChoiceDialog")
        )
    assert blocks[0] == blocks[1], "两份周版本模板的 AI-P0-03 代码块不一致"


@with_rels
def test_the_default_action_is_a_green_incremental_button(rel: str):
    """**默认动作是增量**：绿色主按钮（`btn-success`）、文案「增量分析」。"""
    html = _read(rel)
    match = re.search(r'<button[^>]*id="weeklyAiStartBtn"[^>]*>([^<]*)</button>', html)
    assert match, "找不到增量那个按钮"
    tag, label = match.group(0), match.group(1).strip()
    assert "btn-success" in tag, f"默认动作不是绿色的：{tag}"
    assert label == "增量分析", f"默认按钮的文案是 {label!r}，不是「增量分析」"
    assert "btn-danger" not in tag, f"默认动作被做成了危险色：{tag}"
    script = _script(rel)
    assert "startBtn.addEventListener('click'" in script, "绿色按钮没有绑定点击"


@with_rels
def test_the_full_button_is_red_and_says_full_reanalysis(rel: str):
    """全量那一个是**红色危险语义**，而且文案要写明「全量重新分析」。"""
    html = _read(rel)
    match = re.search(r'<button[^>]*id="weeklyAiFullBtn"[^>]*>([^<]*)</button>', html)
    assert match, "找不到全量那个按钮"
    tag, label = match.group(0), match.group(1).strip()
    assert "btn-danger" in tag, f"全量按钮不是危险色：{tag}"
    assert label == "全量重新分析", f"全量按钮的文案是 {label!r}，不是「全量重新分析」"
    script = _script(rel)
    assert "fullBtn.addEventListener('click'" in script, "红色按钮没有绑定点击"


def test_the_commit_drawer_has_one_red_rerun_button_and_the_reason_is_written_down():
    """单提交抽屉**没有**增量按钮 —— 而且「为什么没有」必须写在代码里。

    一份提交没有增量基线（`AiAnalysisRun.target_type=commit` 不分增量/全量），
    造一个增量按钮出来就是给用户一个按下去会说「没有这个概念」的控件。
    """
    rel = "templates/commit_diff_new.html"
    html = _read(rel)
    match = re.search(r'<button[^>]*id="aiAnalyzeBtn"[^>]*>([^<]*)</button>', html)
    assert match, "单提交抽屉的重新分析按钮不见了"
    tag, label = match.group(0), match.group(1).strip()
    assert "btn-danger" in tag, (
        f"单提交的「重新分析」没有与周版本的「全量重新分析」对齐颜色：{tag}"
    )
    assert label == "重新分析", f"文案是 {label!r}"
    assert "aiFullBtn" not in html and "aiIncrementalBtn" not in html, (
        "单提交抽屉里出现了增量/全量两个按钮 —— 它没有这个概念"
    )
    # 「为什么这里只有一个按钮」必须写在按钮旁边（不是留在某次对话里）。
    around = html[max(0, match.start() - 900): match.start()]
    assert "没有增量基线" in around, "没有写下单提交为什么只有一个入口"


@with_rels
def test_the_create_request_takes_its_mode_from_the_button_not_from_the_words(rel: str):
    """**接口参数**：`analysis_mode` 来自用户点的那个按钮，而不是从按钮文案里猜。

    验收原文：「后端不能仅凭前端按钮文案推断模式」——前端这一侧对应的是
    「按钮 → 模式」这一步要有唯一的一条映射，不能出现第二个 `analysis_mode:` 来源。
    """
    script = _script(rel)
    assert script.count("analysis_mode:") == 1, (
        f"`analysis_mode` 有 {script.count('analysis_mode:')} 处来源，只该有一个"
    )
    assert "analysis_mode: requestedMode," in script
    assert "const requestedMode = mode === 'full' ? 'full' : weeklyAiModeValue();" in script
    # 两个入口都要把自己的模式传进决策，不能都写死 incremental。
    assert "weeklyAiChooseMode('incremental')" in script, "绿色按钮没有声明自己是增量"
    assert "weeklyAiChooseMode('full')" in script, "红色按钮没有声明自己是全量"


# ---------------------------------------------------------------------------
#  节点探针：把模板里逐字那一份决策代码真跑一遍
# ---------------------------------------------------------------------------

_ESTIMATE = {
    "mode": "full",
    "planned_files": 19,
    "shard_count": 4,
    "baseline": {"reusable": False, "note": "没有命中可复用基线，这次会真的跑一遍模型。"},
    "tokens": {"low": 1200000, "high": 3400000, "unit": "token"},
    "duration_ms": {"low": 600000, "high": 1500000},
    "cost": {"computable": False, "reason": "没有配置模型单价", "low": None, "high": None},
    "last_actual": {
        "run_id": 7, "created_at": "2026-09-20T10:00:00", "files": 12,
        "tokens": 900000, "duration_ms": 480000, "rounds": 5, "shards": 4, "cost": None,
    },
    "basis": {"source": "files", "runs": 3, "same_mode_runs": 3,
              "mode_samples_missing": False, "files": {"low": 8, "high": 40},
              "shards": 4, "run_ids": [1, 2, 3]},
    "notes": ["按最近 3 次同类运行的单文件用量折算（每文件 100,000 ~ 200,000 token，目标 19 个文件）。"],
}

_PROBE_TEMPLATE = """
var probeDialogs = [];
var probeChoices = [];
var probeEstimate = null;
var probeEstimateModes = [];
var probeBaseline = null;
var probeAppended = [];

function weeklyAiChoiceDialog(options) {
    probeDialogs.push({
        title: options.title, intro: options.intro, lines: options.lines,
        note: options.note, choices: options.choices, okText: options.okText
    });
    var value = probeChoices.length ? probeChoices.shift() : null;
    return Promise.resolve(value);
}
async function fetchWeeklyAiEstimate(mode) { probeEstimateModes.push(mode); return probeEstimate; }
async function fetchWeeklyAiBaselineState() { return probeBaseline; }
function prependWeeklyAiLine(text) { probeAppended.push(text); }

__EXTRACTED__

globalThis.__probe = {
    reset: function () {
        probeDialogs = []; probeChoices = []; probeEstimateModes = [];
        probeAppended = []; weeklyAiMismatchAccepted = false; weeklyAiUpgradeNoticeText = '';
    },
    set: function (spec) {
        // **必须拷一份**：`shift()` 会把传进来的数组吃掉，而场景表在两份模板之间是
        // 同一个对象 —— 不拷的话第二份模板拿到的永远是空队列（结果全是「取消」）。
        probeChoices = (spec.choices || []).slice();
        probeEstimate = spec.estimate === undefined ? null : spec.estimate;
        probeBaseline = spec.baseline === undefined ? null : spec.baseline;
    },
    choose: function (mode) { return weeklyAiChooseMode(mode); },
    state: function () {
        return { dialogs: probeDialogs, estimateModes: probeEstimateModes,
                 mismatchAccepted: weeklyAiMismatchAccepted,
                 upgradeNoticeText: weeklyAiUpgradeNoticeText, appended: probeAppended };
    },
    estimateLines: function (payload) { return weeklyAiEstimateLines(payload); },
    estimateNote: function (payload) { return weeklyAiEstimateNote(payload); },
    upgradeNotice: function (job) { return weeklyAiUpgradeNotice(job); },
    comparabilityLine: function () { return weeklyAiComparabilityLine(); },
    noteMismatch: function () { weeklyAiNoteMismatchAccepted(); }
};
"""

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

async function runOne(probeSource, scenarios) {
    const sandbox = { console: console };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(probeSource, sandbox, { filename: 'p003_probe.js' });
    const P = sandbox.__probe;
    const out = {};
    for (const spec of scenarios) {
        P.reset();
        P.set(spec);
        let mode = undefined;
        if (spec.action === 'choose') mode = await P.choose(spec.requested);
        const state = P.state();
        const item = { mode: mode, dialogs: state.dialogs, estimateModes: state.estimateModes,
                       mismatchAccepted: state.mismatchAccepted,
                       upgradeNoticeText: state.upgradeNoticeText };
        if (spec.action === 'estimateLines') item.lines = P.estimateLines(spec.payload);
        if (spec.action === 'estimateNote') item.note = P.estimateNote(spec.payload);
        if (spec.action === 'upgradeNotice') item.notice = P.upgradeNotice(spec.job);
        if (spec.action === 'comparability') {
            P.noteMismatch();
            item.line = P.comparabilityLine();
        }
        out[spec.name] = item;
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

_SCENARIOS = [
    # provenance 没变 + 基线在 → **一个字都不弹**（验收第 4 条的反向用例）
    {"name": "unchanged", "action": "choose", "requested": "incremental",
     "baseline": {"hasResult": True, "mismatch": False, "note": "", "createdAt": "2026-09-20 10:00:00"}},
    # 基线状态读不到（网络抖）→ 也不弹假警告
    {"name": "unreadable", "action": "choose", "requested": "incremental", "baseline": None},
    # provenance 变了：列出这件事、推荐全量、**仍允许继续增量**
    {"name": "mismatch_continue", "action": "choose", "requested": "incremental", "choices": ["incremental"],
     "baseline": {"hasResult": True, "mismatch": True,
                  "note": "该结论由旧版评审规程产出（提示词/规则/模型已更新）",
                  "createdAt": "2026-09-19 09:00:00"}},
    {"name": "mismatch_full", "action": "choose", "requested": "incremental", "choices": ["full"],
     "baseline": {"hasResult": True, "mismatch": True, "note": "旧规程", "createdAt": "2026-09-19 09:00:00"}},
    {"name": "mismatch_cancel", "action": "choose", "requested": "incremental", "choices": [None],
     "baseline": {"hasResult": True, "mismatch": True, "note": "旧规程", "createdAt": "2026-09-19 09:00:00"}},
    # 还没有基线 → 会被升级为全量：**事前**把代价摆出来（这是唯一能事前看见的升级原因）
    {"name": "first_run", "action": "choose", "requested": "incremental", "choices": ["ok"],
     "baseline": {"hasResult": False, "mismatch": False, "note": "", "createdAt": ""},
     "estimate": _ESTIMATE},
    {"name": "first_run_cancel", "action": "choose", "requested": "incremental", "choices": [None],
     "baseline": {"hasResult": False, "mismatch": False, "note": "", "createdAt": ""},
     "estimate": _ESTIMATE},
    # 红色按钮：**先给代价**才发起
    {"name": "full_ok", "action": "choose", "requested": "full", "choices": ["ok"], "estimate": _ESTIMATE},
    {"name": "full_cancel", "action": "choose", "requested": "full", "choices": [None], "estimate": _ESTIMATE},
    {"name": "estimate_lines_full", "action": "estimateLines", "payload": _ESTIMATE},
    {"name": "estimate_lines_nopricing", "action": "estimateLines",
     "payload": {**_ESTIMATE, "cost": {"computable": False, "reason": "没有配置模型单价"}}},
    {"name": "estimate_lines_priced", "action": "estimateLines",
     "payload": {**_ESTIMATE, "cost": {
         "computable": True, "currency": "CNY",
         "low": {"amount": "2.00", "amount_exact": "2", "currency": "CNY"},
         "high": {"amount": "5.25", "amount_exact": "5.25", "currency": "CNY"},
     }}},
    {"name": "estimate_lines_nohistory", "action": "estimateLines",
     "payload": {"mode": "full", "planned_files": None, "tokens": {"low": None, "high": None},
                 "duration_ms": {"low": None, "high": None},
                 "cost": {"computable": False, "reason": "还没有可参照的历史运行，估不出 token，也就估不出费用"},
                 "baseline": {"reusable": None, "note": "这次能不能复用基线还没有判定。"},
                 "last_actual": None, "basis": {"source": "none", "runs": 0}, "notes": []}},
    {"name": "estimate_note", "action": "estimateNote", "payload": _ESTIMATE},
    {"name": "upgrade_first_run", "action": "upgradeNotice",
     "job": {"requested_mode": "incremental", "effective_mode": "full", "upgrade_reason": "first_run"}},
    {"name": "upgrade_delta_count", "action": "upgradeNotice",
     "job": {"requested_mode": "incremental", "effective_mode": "full", "upgrade_reason": "delta_count_high"}},
    {"name": "upgrade_unknown_code", "action": "upgradeNotice",
     "job": {"requested_mode": "incremental", "effective_mode": "full", "upgrade_reason": "weird_code"}},
    {"name": "upgrade_not_an_upgrade", "action": "upgradeNotice",
     "job": {"requested_mode": "full", "effective_mode": "full", "upgrade_reason": ""}},
    {"name": "upgrade_waiting", "action": "upgradeNotice",
     "job": {"requested_mode": "incremental", "effective_mode": None, "upgrade_reason": ""}},
    {"name": "comparability", "action": "comparability"},
]

_RESULTS: dict = {}


def _probe_source(rel: str) -> str:
    """把模板里**逐字那一份**决策代码抽出来，配一份页面全局桩（抄一份进测试就没意义了）。"""
    script = _script(rel)
    extracted = "\n".join([
        _const_object_source(script, "WEEKLY_AI_UPGRADE_REASONS"),
        _function_source(script, "weeklyAiUpgradeNotice"),
        _function_source(script, "weeklyAiFormatInt"),
        _function_source(script, "weeklyAiFormatDuration"),
        _function_source(script, "weeklyAiTokensText"),
        _function_source(script, "weeklyAiDurationText"),
        _function_source(script, "weeklyAiBaselineText"),
        _function_source(script, "weeklyAiMoneyText"),
        _function_source(script, "weeklyAiEstimateLines"),
        _function_source(script, "weeklyAiEstimateNote"),
        _function_source(script, "weeklyAiNoteMismatchAccepted"),
        _function_source(script, "weeklyAiComparabilityLine"),
        _function_source(script, "weeklyAiChooseMode"),
        "var weeklyAiMismatchAccepted = false;",
        "var weeklyAiUpgradeNoticeText = '';",
        "var configId = 42;",
    ])
    return _PROBE_TEMPLATE.replace("__EXTRACTED__", extracted)


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_p003_driver.js"
    payload = workdir / "ai_p003_cases.json"
    driver.write_text(_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps({"probes": {rel: _probe_source(rel) for rel in WEEKLY},
                    "scenarios": _SCENARIOS}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"模板里的 AI-P0-03 决策代码在 node 里跑不起来：\n{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


def _case(rel: str, name: str) -> dict:
    return _run_node()[rel][name]


# --- 验收 4 的反向用例：provenance 没变时不弹窗 ------------------------------

@with_rels
def test_an_unchanged_provenance_pops_nothing_and_goes_incremental(rel: str):
    """**provenance 没变时默认增量不弹窗**（验收第 4 条）。

    这一条只有真跑才算数：静态断言下「每次点击都弹一个框」的实现照样绿灯。
    """
    item = _case(rel, "unchanged")
    assert item["mode"] == "incremental", item
    assert item["dialogs"] == [], f"provenance 没变却弹了框：{item['dialogs']}"
    assert item["mismatchAccepted"] is False, "没弹框却把「与基线不可比」记上了"
    assert item["estimateModes"] == [], (
        f"provenance 没变也去要了估算（多一次请求）：{item['estimateModes']}"
    )


@with_rels
def test_an_unreadable_baseline_state_does_not_fake_a_warning(rel: str):
    """基线状态**读不到**（网络抖 / 403）时不弹假警告 —— 假警告会让人学会不看警告。"""
    item = _case(rel, "unreadable")
    assert item["mode"] == "incremental", item
    assert item["dialogs"] == [], item["dialogs"]


# --- 验收 3：provenance 变了 → 列出来、推荐全量、仍允许增量 --------------------

@with_rels
def test_a_changed_provenance_is_explained_and_full_is_recommended(rel: str):
    item = _case(rel, "mismatch_continue")
    assert item["mode"] == "incremental", "选了「仍然只做增量」却没有按增量走"
    assert len(item["dialogs"]) == 1, item["dialogs"]
    dialog = item["dialogs"][0]
    values = [choice["value"] for choice in dialog["choices"]]
    assert values == ["full", "incremental"], f"选项不是「全量（推荐）/ 继续增量」：{dialog['choices']}"
    labels = [choice["label"] for choice in dialog["choices"]]
    assert "全量" in labels[0] and "推荐" in labels[0], labels
    assert "增量" in labels[1], labels
    # **服务端那句话要原样转述**：列不出逐字段明细是事实，不许编。
    facts = dict((pair[0], pair[1]) for pair in dialog["lines"])
    assert "旧版评审规程" in facts["服务端说明"], facts
    assert "不可比" in facts["这次增量会怎样"], facts
    assert item["mismatchAccepted"] is True, "选了继续增量却没记下「与基线不可比」"


@with_rels
def test_the_incremental_choice_leaves_a_note_the_report_can_carry(rel: str):
    """「与基线不可比」这件事必须在页面留下痕迹（报告顶部那一行）。"""
    item = _run_node()[rel]["comparability"]
    assert "不可比" in item["line"], item
    assert "全量重新分析" in item["line"], item


@with_rels
def test_choosing_full_from_the_prompt_runs_full(rel: str):
    item = _case(rel, "mismatch_full")
    assert item["mode"] == "full", item
    assert len(item["dialogs"]) == 1, item["dialogs"]


@with_rels
def test_cancelling_the_prompt_creates_nothing(rel: str):
    """取消 → 返回 `null`，调用方**什么都不做**（不建 job、不建 run、不花钱）。"""
    item = _case(rel, "mismatch_cancel")
    assert item["mode"] is None, item
    assert item["mismatchAccepted"] is False, item


# --- 验收 2：全量确认框里必须有那四样 ----------------------------------------

@with_rels
def test_the_full_button_shows_the_estimate_before_creating_anything(rel: str):
    assert _case(rel, "full_cancel")["mode"] is None, "取消之后还往下走了"
    assert _case(rel, "full_ok")["mode"] == "full"
    assert _case(rel, "full_ok")["estimateModes"] == ["full"], "全量那一次没按 full 要估算"


@with_rels
def test_the_confirm_dialog_covers_the_four_required_facts(rel: str):
    """确认框必须显示：预计文件数、预计 token 区间、最近一次实际耗时、是否命中可复用基线。"""
    labels = [pair[0] for pair in _case(rel, "estimate_lines_full")["lines"]]
    for required in ("预计文件数", "预计 token", "最近一次实际耗时", "可复用基线"):
        assert required in labels, f"确认框里没有「{required}」：{labels}"
    facts = dict((pair[0], pair[1]) for pair in _case(rel, "estimate_lines_full")["lines"])
    assert "19" in facts["预计文件数"], facts
    assert "1,200,000" in facts["预计 token"] and "3,400,000" in facts["预计 token"], facts
    assert "8.0 分钟" in facts["最近一次实际耗时"], facts
    assert "没有命中可复用基线" in facts["可复用基线"], facts


@with_rels
def test_the_estimate_never_invents_a_price_when_the_price_table_is_missing(rel: str):
    """**价格没配置时只给 token 与时间，不伪造费用。**"""
    facts = dict((pair[0], pair[1]) for pair in _case(rel, "estimate_lines_nopricing")["lines"])
    assert facts["预计费用"] == "未配置模型单价，只能给 token 与时间", facts["预计费用"]
    assert "token" in facts["预计 token"] and "分钟" in facts["最近一次实际耗时"], facts


@with_rels
def test_the_estimate_formats_the_money_objects_instead_of_object_object(rel: str):
    """费用端点是结构化金额对象；确认框必须取展示金额，不能隐式转字符串。"""
    facts = dict((pair[0], pair[1]) for pair in _case(rel, "estimate_lines_priced")["lines"])
    assert facts["预计费用"] == "2.00 ~ 5.25 CNY", facts["预计费用"]
    assert "[object Object]" not in facts["预计费用"]


@with_rels
def test_the_estimate_says_so_when_there_is_no_history_to_reference(rel: str):
    """一次历史都没有时**不给一个凭空的数字**，如实说估不出来。"""
    facts = dict((pair[0], pair[1]) for pair in _case(rel, "estimate_lines_nohistory")["lines"])
    assert facts["预计 token"] == "没有可参照的历史运行，估不出 token 区间", facts
    assert "估不出耗时" in facts["预计耗时"], facts
    assert "还没有可参照的历史运行" in facts["最近一次实际耗时"], facts
    assert facts["预计费用"] == "未配置模型单价，只能给 token 与时间", facts


@with_rels
def test_the_estimate_note_keeps_the_qualifier_and_drops_markdown(rel: str):
    """区间必须带着它的前提一起出现（服务端 `notes`），且不许把 `**` 原样印到页面上。"""
    note = _case(rel, "estimate_note")["note"]
    assert note.strip(), "确认框没有带任何前提说明"
    assert "**" not in note, f"markdown 的星号漏到页面上：{note!r}"
    assert "不是精确值" in note, note


# --- 验收 5：平台升级的原因要说给用户听 --------------------------------------

@with_rels
def test_a_platform_upgrade_is_explained_in_words(rel: str):
    """`effective_mode=full` + `upgrade_reason` → 一句人话，而不是一个原因码。"""
    notice = _case(rel, "upgrade_first_run")["notice"]
    assert notice, "平台把增量升成了全量，页面一个字都没说"
    assert "全量" in notice and "首次" in notice, notice
    assert "first_run" not in notice, f"原因码直接印给用户看了：{notice}"
    other = _case(rel, "upgrade_delta_count")["notice"]
    assert "文件数" in other and "delta_count_high" not in other, other
    unknown = _case(rel, "upgrade_unknown_code")["notice"]
    assert "weird_code" in unknown, (
        f"认不出来的原因码要如实写出来（编一句解释更坏）：{unknown}"
    )


@with_rels
def test_only_a_platform_upgrade_is_announced(rel: str):
    """用户**自己要的**全量不是「平台升级」，不许无话找话。"""
    assert _case(rel, "upgrade_not_an_upgrade")["notice"] == ""
    assert _case(rel, "upgrade_waiting")["notice"] == ""


@with_rels
def test_the_upgrade_notice_reaches_the_page(rel: str):
    """那句话必须真的被写到页面上（不是只算出来放在变量里）。"""
    script = _script(rel)
    assert "weeklyAiUpgradeNoticeText = weeklyAiUpgradeNotice(created.job || created);" in script
    assert "if (weeklyAiUpgradeNoticeText) {" in script
    assert "setWeeklyAiMeta(weeklyAiUpgradeNoticeText);" in script


def test_the_mismatch_limit_is_written_on_top_without_html():
    """报告顶部那一行限制：**用文本节点插入**，不拼 HTML —— 正文是模型给的。"""
    for rel in WEEKLY:
        script = _script(rel)
        assert "output.insertAdjacentText('afterbegin', text + " in script, (
            f"{rel}: 报告顶部那一行不是用文本节点插的（拼 HTML 会把模型正文当标记解析）"
        )
        assert "prependWeeklyAiLine(limit)" in script, rel
        # 写早了会被「取结论」那一步的整段重渲染盖掉 → 必须接在它后面。
        assert re.search(
            r"Promise\.resolve\((?:loadWeeklyAiLatest\(true\)"
            r"|refreshWeeklyAiLatest\(weeklyAiCurrentConfigId\))\)"
            r"\.then\(function \(\) \{[\s\S]{0,160}?prependWeeklyAiLine\(limit\);",
            script,
        ), f"{rel}: 限制那一行没有接在取结论之后（会被重渲染盖掉）"


# --- P0-01 的止血不许在重构里丢掉 --------------------------------------------

@with_rels
def test_the_settled_guard_survives_the_dual_button_refactor(rel: str):
    """`if (streamSettled) return;` 必须在 `error` 处理器的**最开头**（P0-01 的止血）。

    这一条本轮由 `tests/test_ai_drawer_stream_state.py` 真跑覆盖；这里再钉一遍是因为
    P0-03 恰好重写了同一个抽屉的按钮与结果收尾 —— 重构里最容易顺手弄丢的就是它。
    """
    script = _script(rel)
    start = script.index("addEventListener('error'")
    brace = script.index("{", script.index("function", start))
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    handler = script[brace: index + 1]
    first_line = handler[1:].strip().splitlines()[0].strip()
    assert first_line == "if (weeklyAiStreamSettled) return;", (
        f"{rel}: settled 守卫不在处理器开头，第一句是：{first_line!r}"
    )
