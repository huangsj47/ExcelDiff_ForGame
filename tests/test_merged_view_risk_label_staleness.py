#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并视图卡片上那个风险标签的「（旧）」标记。

用户 2026-09-23 报的：刷新页面看到的是「版本AI分析：高风险（旧）」，**点开抽屉再关掉**
就变成「版本AI分析：高风险」—— 看起来像刚重新分析过一样。

两个渲染入口的意图是相反的，而它们原先共用了一个「无条件清掉（旧）」的函数：

* `refreshRiskLabels()`（页面加载 / 容器重渲染）：从 `/latest` 拿 `stale`，
  正确地带上「（旧）」；
* `refreshWeeklyAiLatest()`（打开抽屉、关抽屉、「刷新结果」）：读的也是**落库的那一份**，
  它同样可能是旧结论，但它调的 `updateRiskLabelByConfig(configId, level)` 只有两个参数、
  函数里写死「刚跑完的结论一定是当前规则下的，把『旧』的痕迹清掉」。
  于是开关一次抽屉就把标记抹了。

**这句话只对另一个调用点成立**：流里的 `result` 事件（那次分析刚刚在本页跑完）。
所以修法是让「旧/新」由调用方说，而不是由函数猜。

这一组既真跑那两个函数（行为），也钉住两个调用点各自传了什么（接线）——
只钉其中一边都拦不住这个 bug：函数本身怎么改都挑不出错，是**调用点传错了**。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = "templates/merged_project_view.html"
NODE = shutil.which("node")


def _read() -> str:
    with open(os.path.join(PROJECT_ROOT, TEMPLATE), encoding="utf-8") as handle:
        return handle.read()


def _script() -> str:
    """模板里那段内联脚本（**已剥注释**）。

    注释里原样引用着被断言的写法（「不能直接调 X」「把旧的痕迹清掉」都写在注释里），
    不剥就会假失败/假通过。
    """
    text = _read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
    assert blocks, "模板里没有内联脚本"
    code = "\n".join(blocks)
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


def _extract(script: str, marker: str) -> str:
    """从 `marker` 处按大括号配平抠出完整源码（`async` 一起带上，否则 `await` 语法错误）。"""
    start = script.index(marker)
    if script[:start].endswith("async "):
        start -= len("async ")
    brace = script.index("{", script.index(marker))
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
        index += 1
    raise AssertionError(f"花括号没配平：{marker}")


def _extract_const(script: str, name: str) -> str:
    marker = f"const {name} = "
    assert marker in script, f"模板里没有 {marker!r}"
    brace = script.index("{", script.index(marker))
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return marker + script[brace : index + 1] + ";"
        index += 1
    raise AssertionError(f"花括号没配平：{marker}")


_MINI_DOM = r"""
function makeEl() {
    return {
        textContent: '', className: '', attrs: {}, classes: [],
        classList: {
            add(name) { this._owner.classes.push(name); },
            remove() {},
            toggle() {},
            contains() { return false; }
        },
        setAttribute(name, value) { this.attrs[name] = value; },
        removeAttribute(name) { delete this.attrs[name]; },
        getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
    };
}

function labeledEl(configId) {
    const el = makeEl();
    el.dataset = { configId: configId };
    el.classList._owner = el;
    return el;
}

const registry = {};
function register(configId, el) { registry[configId] = registry[configId] || []; registry[configId].push(el); }
const document = {
    querySelectorAll(selector) {
        const id = String(selector).replace(/[^0-9]/g, '');
        return registry[id] || [];
    }
};
"""


def _run_label(cases: dict) -> dict:
    """真跑 `applyRiskLabel` / `updateRiskLabelByConfig`，返回渲染出来的文案与 title。"""
    if NODE is None:
        pytest.skip("本机没有 node，跳过")
    script = _script()
    harness = "\n".join([
        _extract_const(script, "RISK_LEVEL_MAP"),
        _extract(script, "function normalizeRiskLevel("),
        _extract(script, "function applyRiskLabel("),
        _extract(script, "function updateRiskLabelByConfig("),
        _MINI_DOM,
        """
function probe(spec) {
    const el = labeledEl(spec.id);
    register(spec.id, el);
    if (spec.viaConfig) {
        updateRiskLabelByConfig(spec.id, spec.level, spec.stale, spec.staleNote);
    } else {
        applyRiskLabel(el, spec.level, spec.stale, spec.staleNote);
    }
    return { text: el.textContent, title: el.getAttribute('title') };
}
""",
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(harness + f"\nconst INPUT = {json.dumps(cases)};\n")
        handle.write("""
const out = {};
for (const key of Object.keys(INPUT)) { out[key] = probe(INPUT[key]); }
process.stdout.write(JSON.stringify(out));
""")
        path = handle.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        assert proc.returncode == 0, f"node 跑失败：{proc.stderr[:800]}"
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


class TestTheStaleMarkSurvivesTheDrawer:
    def test_a_stale_conclusion_keeps_the_mark_when_rendered_through_the_config_id(self):
        """**这一条就是用户报的那个场景**：经 `updateRiskLabelByConfig` 渲染时必须还在。"""
        item = _run_label({"one": {
            "viaConfig": True, "id": "7", "level": "high",
            "stale": True, "staleNote": "规则已更新，这是旧版评审规程产出的结论",
        }})["one"]
        assert item["text"] == "版本AI分析：高风险（旧）", item["text"]
        assert item["title"], "「（旧）」的原因没有挂到 title 上"

    def test_a_current_conclusion_has_no_mark(self):
        item = _run_label({"one": {
            "viaConfig": True, "id": "7", "level": "high", "stale": False, "staleNote": "",
        }})["one"]
        assert item["text"] == "版本AI分析：高风险", item["text"]
        assert item["title"] is None, "当前结论不该带着旧结论的说明"

    def test_the_direct_render_path_still_marks_stale(self):
        """另一条路（`refreshRiskLabels` 走的那条）本来就是对的，别在重构里弄丢。"""
        item = _run_label({"one": {
            "viaConfig": False, "id": "9", "level": "medium",
            "stale": True, "staleNote": "上一轮没跑完",
        }})["one"]
        assert item["text"] == "版本AI分析：中风险（旧）", item["text"]
        assert item["title"] == "上一轮没跑完", item["title"]

    def test_an_unknown_level_says_so_instead_of_showing_a_blank(self):
        item = _run_label({"one": {
            "viaConfig": False, "id": "9", "level": None, "stale": False, "staleNote": "",
        }})["one"]
        assert item["text"] == "版本AI分析：未分析", item["text"]


class TestTheTwoCallSitesSayWhichOneTheyAre:
    def test_reading_the_stored_conclusion_passes_the_stale_flag_through(self):
        """`refreshWeeklyAiLatest` 读的是落库那一份 —— 可能正是旧结论，必须原样传。"""
        script = _script()
        body = _extract(script, "async function refreshWeeklyAiLatest(")
        call = re.search(r"updateRiskLabelByConfig\(([^;]*)\);", body)
        assert call, "取结论那一步没有更新风险标签了？"
        args = call.group(1)
        assert "result.stale" in args, (
            f"读落库结论时没有把 stale 传下去（实参是 `{args.strip()}`）——"
            f"开关一次抽屉就会把「（旧）」抹掉"
        )

    def test_the_stream_result_handler_clears_it_because_that_run_just_finished(self):
        """流里的 `result` 事件 = 这次分析刚在本页跑完 → 显式清掉「（旧）」。

        「显式」不是洁癖：默认值是 `false`，但写出来才说得出「这里传 false 是**有意的**」，
        否则下一个人看到别处传了 stale，会以为这里只是漏了。
        """
        script = _script()
        handler = script[script.index("addEventListener('result'"):]
        call = re.search(r"updateRiskLabelByConfig\(([^;]*)\);", handler)
        assert call, "流结果里没有更新风险标签了？"
        args = call.group(1)
        assert re.search(r",\s*false\s*,", args), (
            f"刚跑完的那一次没有显式清掉「（旧）」（实参是 `{args.strip()}`）"
        )


# ---------------------------------------------------------------------------
#  同一个「风险等级」有两条出口，而它们要的口径是**相反**的
#
#  用户 2026-09-23 报的第二条：「高风险（旧）」点开抽屉之后变成「未分析」。
#  根因不在「（旧）」，而在**喂给标签的那个值本身**：
#
#    * 卡片上的标签把它拿去 `RISK_LEVEL_MAP` **查表** → 要**码值**（`high`）；
#    * 上面那行摘要（`最近分析：… | 风险等级 …`）是写给人看的 → 要**中文**。
#
#  602d4b8 为了让摘要行不再直出 `mid_high` 这种码值，把这一处整个换成了服务端给的
#  `risk_label`（中文），而同一个变量还喂着标签 —— 于是标签拿「高」去查码值表，
#  查不到，画成「版本AI分析：未分析」。`refreshRiskLabels()`（刷新页面那条路）读的是
#  `result.result.risk_level`（码值），所以**刷新页面是对的、点开抽屉就不对**。
#
#  所以这一组拿**服务端真函数产出的 payload**（`_conclusion_payload`，不手写字典）
#  喂给**真的 `refreshWeeklyAiLatest`**，两条出口各断言一次 —— 往任何一边合并都会红。
# ---------------------------------------------------------------------------

_REFRESH_HARNESS = r"""
// 只考「哪个值喂给了标签」，其余依赖一律静默桩。
let weeklyAiHasCached = false;
let weeklyAiRunActive = false;
function setWeeklyAiOutput() {}
function setWeeklyAiStatusBadge() {}
function setWeeklyAiReport() {}
function renderAiUsageLine() {}
function startAiBudgetWatch() {}
const AiReportExport = { track() {} };
const AiThinkLog = { setRun() {}, markEmpty() {} };
const AiContextNotice = { withContextNotice(text) { return text; } };

let CURRENT = null;
async function getWeeklyAiResult() { return CURRENT; }
let META = null;
document.getElementById = function (id) {
    return id === 'weeklyAiMeta' ? META : null;
};
"""

# 驱动**必须排在 `const INPUT` 之后**：`INPUT` 是 `const`（暂时性死区），IIFE 在它
# 初始化之前跑会抛 `Cannot access 'INPUT' before initialization`。
_REFRESH_DRIVER = r"""
(async () => {
    const out = {};
    for (const key of Object.keys(INPUT)) {
        const spec = INPUT[key];
        CURRENT = spec.payload;
        META = makeEl();
        const el = labeledEl(spec.id);
        registry[spec.id] = [el];
        await refreshWeeklyAiLatest(spec.id);
        out[key] = {
            label: el.textContent,
            title: el.getAttribute('title'),
            meta: META.textContent,
            classes: el.classes
        };
    }
    process.stdout.write(JSON.stringify(out));
})();
"""


def _server_payload(*, risk_level: str = "high", stale: bool = False,
                    stale_note: str = "") -> dict:
    """`/ai-analysis/weekly/<id>/latest` 的响应体，**由服务端真函数产出**。

    手写一个字典当夹具的话，生产改了 `_conclusion_payload` 的字段/口径，这里会继续
    拿一份过时的形状喂前端 —— 而这一组的全部意义正是「前端与服务端口径对不上」。
    `stale` / `stale_note` 是 `_read_latest_result` 在读回落库结论那条路上补的，
    这里照同一处补（它们不在 `_conclusion_payload` 里）。
    """
    from models.ai_analysis.analysis_run import AiAnalysisRun
    from services.ai.conclusion_view import _conclusion_payload

    run = AiAnalysisRun(
        project_id=1, target_type="weekly", target_id=1, status="succeeded",
        scope="full", trigger_source="manual", response_mode="streaming",
        response_payload=json.dumps({"risk_level": risk_level, "anomalies": []}),
        response_text="# 结论\n\n这是一份落库的结论。\n",
    )
    run.id = 1
    run.created_at = datetime(2026, 9, 20, 2, 0, 0)
    payload = _conclusion_payload(run)
    # 顶层 `run_id` 由路由补（`ai_weekly_latest` 的 `result.get("run_id")`）。
    payload["run_id"] = run.id
    if stale:
        payload["stale"] = True
        payload["stale_note"] = stale_note
    return payload


def _run_refresh_latest(cases: dict) -> dict:
    """真跑模板里的 `refreshWeeklyAiLatest`，返回渲染出来的标签、title 与摘要行。"""
    if NODE is None:
        pytest.skip("本机没有 node，跳过")
    script = _script()
    harness = "\n".join([
        _extract_const(script, "RISK_LEVEL_MAP"),
        _extract(script, "function normalizeRiskLevel("),
        _extract(script, "function applyRiskLabel("),
        _extract(script, "function updateRiskLabelByConfig("),
        _extract(script, "async function refreshWeeklyAiLatest("),
        _MINI_DOM,
        _REFRESH_HARNESS,
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(harness + f"\nconst INPUT = {json.dumps(cases)};\n" + _REFRESH_DRIVER)
        path = handle.name
    try:
        proc = subprocess.run([NODE, path], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        assert proc.returncode == 0, f"node 跑失败：{proc.stderr[:800]}"
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


class TestTheLabelGetsTheCodeAndTheSummaryGetsTheChinese:
    def test_the_server_sends_a_chinese_risk_label(self):
        """**这一组的前提**，单独钉住：`risk_label` 是中文，`result.risk_level` 才是码值。

        哪天服务端把 `risk_label` 也改成码值，这里会红 —— 那是在说「前提变了，
        下面两条的构造已经不代表线上」，而不是「前端错了」。
        """
        payload = _server_payload(risk_level="high")
        assert payload["risk_label"] == "高", payload["risk_label"]
        assert payload["result"]["risk_level"] == "high", payload["result"]

    def test_opening_the_drawer_keeps_the_level_and_the_stale_mark(self):
        """**这条就是用户报的场景**：读一份旧结论，标签必须还是「高风险（旧）」。"""
        item = _run_refresh_latest({"one": {"id": "7", "payload": _server_payload(
            risk_level="high", stale=True,
            stale_note="该结论由旧版评审规程产出（提示词/规则/模型已更新）",
        )}})["one"]
        assert item["label"] == "版本AI分析：高风险（旧）", (
            f"标签画成了 {item['label']!r} —— 喂给它的多半是中文 `risk_label` 而不是码值"
        )
        assert item["title"] == "该结论由旧版评审规程产出（提示词/规则/模型已更新）", item["title"]

    def test_the_summary_line_still_says_the_level_in_chinese(self):
        """另一半出口：摘要行要中文（602d4b8 修的就是它），别在修标签时改回码值。"""
        item = _run_refresh_latest({"one": {"id": "7", "payload": _server_payload(
            risk_level="mid_high", stale=True, stale_note="上一轮没跑完",
        )}})["one"]
        assert "风险等级 中高" in item["meta"], item["meta"]
        assert item["label"] == "版本AI分析：中高风险（旧）", item["label"]

    @pytest.mark.parametrize("level,label", [
        ("low", "低风险"), ("mid_low", "中低风险"), ("medium", "中风险"),
        ("mid_high", "中高风险"), ("high", "高风险"),
    ])
    def test_every_level_the_server_can_send_lands_on_a_level_not_on_unanalyzed(
            self, level, label):
        """服务端 `RISK_LABELS` 的每一个码值都要落在某一档上，一个都不许掉进「未分析」。"""
        item = _run_refresh_latest({"one": {"id": "7", "payload": _server_payload(
            risk_level=level)}})["one"]
        assert item["label"] == f"版本AI分析：{label}", item["label"]
        assert "risk-unknown" not in item["classes"], item["classes"]
