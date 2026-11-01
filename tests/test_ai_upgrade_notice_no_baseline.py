#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP23：首跑提示不许再谎称「升级全量」（审查报告 P2-3）+ `focus=code` 空范围说明（P2-2）。

## P2-3：那一句话错在哪

没有可复用的结论基线时，`run.scope` 就是 `full`（输入只能整窗读），落库时
`job.effective_mode` 跟着记成 `full`、`upgrade_reason` 归一到 `no_conclusion_baseline`
（`services/ai_analysis_service.py` 里 `mark_running(effective_mode=run.scope, ...)`）。
而 `weeklyAiUpgradeNotice` 原先只看 `requested_mode === 'incremental'` +
`effective_mode === 'full'` 两个字段，于是把这件事说成「平台把这次增量升级成了全量」——
与预检「仍为增量、只是没有历史结论、输入按整窗读」正面矛盾。

这一条**不是**「平台替用户改了模式」：用户点的仍是增量。所以：

* `no_conclusion_baseline` / 历史值 `first_run` → 只说「没有可复用的历史结论、输入按整窗
  读取」，**不许出现「升级」**；
* `force_full_rebuild`（旧结论不可复用、平台重建一次输入）→ 可以说「平台重建了整窗输入」，
  同样不许说「升级」；
* `delta_count_high` / `delta_ratio_high` / `critical_path_detected` 是**建议**原因，早已
  不产生 `effective_mode=full`；旧 job 上还挂着它们时只按「平台建议整窗重跑」说；
* 用户自己要的全量、或者根本算不上差异（`effective_mode` 不是 `full`）→ 空串。

## P2-2：空范围那句话

项目分析范围只含配置表仓库时，周版本抽屉里选「只看代码仓库」筛完是空集（服务端回
`focus_empty`）。界面原先只说「范围里没有改动」，读者会以为代码仓库这周真没改。这里钉的
是**静态说明**：它必须在两份模板里逐字出现，而且不许变成一次新的请求。

为什么是静态说明、而不是跟着失败提示走：`focus_empty` **不进** `created.message`
（`routes/ai_analysis_routes.py` 的 `_job_message` 只报「job 到哪一步」；那句人话在
`services/ai/job_service.py` 的 `WITHOUT_RUN_REASON_MESSAGES` 里，落在 job 的
`error_message` 上）。前端再判一遍原因码等于把服务端的账抄第二份，所以这句话只做静态的
一份，摆在用户选范围的地方 —— 任务要求也写的是「只在 UI 里显示，不新增后端请求」。
`merged_project_view.html` 那边没有 focus 下拉（脚本里 `const focus = 'all';`），
说明落在**项目分析范围**那一栏：用户能改「只含配置表仓库 / 全部仓库」的只有那里。

## 为什么静态断言 + node 真跑两层都要

「两模板逐字一致」「句子在抽屉里」是**形状**，静态断言就够；「这段文案真的不出现『升级』」
「旧 job 的原因码仍然说人话」是**行为** —— 只有把模板里逐字那一份 `weeklyAiUpgradeNotice`
放进 node 里真跑才分得出来。手法与 `tests/test_ai_upgrade_dialog_incremental.py` 同一套。

静态断言之前一律先剥注释：本仓库的注释习惯是把「旧写法 → 新写法」原样写在注释里。
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

#: 这一次**不许**再说成「升级」的原因码（前两个是同一件事的现名与历史名，
#: `force_full_rebuild` 是「平台重建整窗输入」，也不是升级）。
NO_UPGRADE_CODES = ("no_conclusion_baseline", "first_run", "force_full_rebuild")

#: 建议原因：`services/ai/mode_decision.py` 的 `SUGGESTION_REASONS`，它们**不产生**
#: `effective_mode=full`；旧 job 上还挂着时只能按「建议」说。
SUGGESTION_CODES = ("delta_count_high", "delta_ratio_high", "critical_path_detected")

#: 认不出来的码要原样写出那个码（编一句解释比不解释更坏）。
UNKNOWN_CODE = "weird_code"

#: 这一整句是 P2-2 的静态说明。两份模板**逐字相同**（只差缩进，比对时压掉空白）。
FOCUS_HINT = (
    "项目分析范围只含配置表仓库时，「只看代码仓库」会得到空范围；"
    "要分析代码仓库，请把项目概览页「AI 分析配置」里的分析范围改为「全部仓库」。"
)

#: P2-3 之前那句错话。它**不许**再出现在任何一份模板的脚本里。
OLD_SENTENCE = "平台把这次增量升级成了全量"


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


def _squash(text: str) -> str:
    """压掉空白与 Markdown 的星号，只比较「说了什么」。"""
    return re.sub(r"\s+", "", text or "")


# ---------------------------------------------------------------------------
#  静态断言：形状
# ---------------------------------------------------------------------------

@with_rels
def test_the_old_sentence_is_gone(rel: str):
    """「平台把这次增量升级成了全量」这句话本身不许再出现（注释里那处已剥掉）。"""
    assert OLD_SENTENCE not in _script(rel), (
        f"{rel}: 脚本里还留着「{OLD_SENTENCE}」—— 这个说法正是 P2-3 要修的那句错话"
    )


@with_rels
def test_the_notice_branches_on_the_reason_codes(rel: str):
    """判据必须在 `weeklyAiUpgradeNotice` 里，而且要认出这三档原因。

    分支写在外层（比如调用点）的话，「刚创建」与「刷新后接回」两条路会各说一套 ——
    这种分叉在本仓库已经出现过一次（同一个提示两个入口说不一样的话）。
    """
    notice = _function_source(_script(rel), "weeklyAiUpgradeNotice")

    for code in NO_UPGRADE_CODES + SUGGESTION_CODES:
        assert f"'{code}'" in notice, f"{rel}: 通知里没有认出 {code} 这一档"
    # 老的两条判据不许在重写里丢掉：用户点全量 / 还在等同步时，一个字都不说。
    assert "job.requested_mode !== 'incremental'" in notice, rel
    assert "job.effective_mode !== 'full'" in notice, rel
    assert "if (!job) return '';" in notice, rel


@with_rels
def test_the_reason_mapping_is_kept(rel: str):
    """`weeklyAiUpgradeReasonText` 的映射这一轮**不动**（只改「怎么说」，不删码）。

    三个和本轮分支有关的原因码文案逐字钉住：它们是「没有可复用的历史结论」那几句的
    事实来源，改一个字都会让通知与升级弹窗说不到一起。
    """
    reasons = _const_object_source(_script(rel), "WEEKLY_AI_UPGRADE_REASONS")
    expected = {
        "first_run": "这个分组还没有可复用的结论基线，首次分析按全量跑",
        "no_conclusion_baseline": "这个分组还没有可继承的结论基线（上一轮没有产出可用的结构化结论）",
        "force_full_rebuild": "这一轮被判定必须整窗重建（用户请求全量，或规则/快照整体变化）",
    }
    for code, text in expected.items():
        assert f"{code}: '{text}'" in reasons, f"{rel}: {code} 的文案被改了：{reasons}"
    # 其余原因码仍然在表里（旧 job / 历史值仍可能回它们）。
    for code in ("runtime_scope_upgrade",) + SUGGESTION_CODES + ("empty_total",):
        assert f"{code}:" in reasons, f"{rel}: 原因码表丢了 {code}"


def test_the_notice_is_byte_identical_across_the_two_templates():
    """两份周版本模板是同一逻辑的副本：这一句话只能有一份写法。"""
    blocks = [
        _function_source(_script(rel), "weeklyAiUpgradeNotice") for rel in WEEKLY
    ]
    assert blocks[0] == blocks[1], (
        "两份周版本模板的 `weeklyAiUpgradeNotice` 不一致：先改的那份要整段同步过去"
    )


# ---------------------------------------------------------------------------
#  node 探针：把模板里逐字那一份通知真跑一遍
# ---------------------------------------------------------------------------

_JOB = {"requested_mode": "incremental", "effective_mode": "full"}


def _job(reason: str, **overrides) -> dict:
    return {**_JOB, "upgrade_reason": reason, **overrides}


_CASES = [
    {"name": "no_conclusion_baseline", "job": _job("no_conclusion_baseline")},
    {"name": "first_run", "job": _job("first_run")},
    {"name": "force_full_rebuild", "job": _job("force_full_rebuild")},
    {"name": "runtime_scope_upgrade", "job": _job("runtime_scope_upgrade")},
    {"name": "unknown_code", "job": _job(UNKNOWN_CODE)},
    {"name": "no_reason_at_all", "job": _job("")},
    # 用户自己要的全量：不是「平台升级」，一句话都不说。
    {"name": "explicit_full",
     "job": {"requested_mode": "full", "effective_mode": "full", "upgrade_reason": ""}},
    # 归一到新语义之后的正常形状：模式仍是增量（这时也不该有「升级」通知）。
    {"name": "still_incremental",
     "job": {"requested_mode": "incremental", "effective_mode": "incremental",
             "upgrade_reason": "no_conclusion_baseline"}},
    # 还在等同步：没有口径可比。
    {"name": "waiting",
     "job": {"requested_mode": "incremental", "effective_mode": None, "upgrade_reason": ""}},
    {"name": "no_job", "job": None},
] + [
    {"name": code, "job": _job(code)} for code in SUGGESTION_CODES
]

_NOTICE_PROBE = """
__EXTRACTED__

globalThis.__probe = {
    notice: function (job) { return weeklyAiUpgradeNotice(job); },
    reasonText: function (code) { return weeklyAiUpgradeReasonText(code); }
};
"""

_NOTICE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

const result = {};
for (const template of Object.keys(payload.probes)) {
    const sandbox = { console: console };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(payload.probes[template], sandbox, { filename: 'wp23_probe.js' });
    const P = sandbox.__probe;
    const out = {};
    for (const spec of payload.cases) {
        out[spec.name] = { notice: P.notice(spec.job) };
    }
    out.__reasons = {};
    for (const code of payload.codes) out.__reasons[code] = P.reasonText(code);
    result[template] = out;
}
process.stdout.write(JSON.stringify(result));
"""

_RESULTS: dict = {}


def _probe_source(rel: str) -> str:
    script = _script(rel)
    extracted = "\n".join([
        _const_object_source(script, "WEEKLY_AI_UPGRADE_REASONS"),
        _function_source(script, "weeklyAiUpgradeReasonText"),
        _function_source(script, "weeklyAiUpgradeNotice"),
    ])
    return _NOTICE_PROBE.replace("__EXTRACTED__", extracted)


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_upgrade_notice_no_baseline_driver.js"
    payload = workdir / "ai_upgrade_notice_no_baseline_cases.json"
    driver.write_text(_NOTICE_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps({
            "probes": {rel: _probe_source(rel) for rel in WEEKLY},
            "cases": _CASES,
            "codes": list(NO_UPGRADE_CODES + SUGGESTION_CODES + ("runtime_scope_upgrade",)),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"模板里的 `weeklyAiUpgradeNotice` 在 node 里跑不起来：\n{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


def _notice(rel: str, name: str) -> str:
    return _run_node()[rel][name]["notice"]


# --- P2-3 的核心：没有结论基线不是升级 ---------------------------------------

@with_rels
def test_no_baseline_does_not_claim_an_upgrade(rel: str):
    """`no_conclusion_baseline`：说「没有可复用的历史结论 / 整窗读取」，不说升级。"""
    notice = _notice(rel, "no_conclusion_baseline")

    assert notice, f"{rel}: 这一档一个字都没说"
    assert "升级" not in notice, f"{rel}: 又把它说成了升级：{notice}"
    assert "没有可复用的历史结论" in notice, notice
    assert "整窗读取" in notice, notice
    # 「不是平台改成了全量」这半句必须留着：它是与预检对齐的那一句。
    assert "不是平台把这次增量改成了全量" in notice, notice
    assert "no_conclusion_baseline" not in notice, f"{rel}: 原因码印给用户了：{notice}"


@with_rels
def test_the_historical_first_run_reason_reads_the_same_way(rel: str):
    """历史值 `first_run` 说的是同一件事：不许因为码不同就冒出一句「升级」。"""
    notice = _notice(rel, "first_run")

    assert notice, f"{rel}: 这一档一个字都没说"
    assert "升级" not in notice, f"{rel}: 历史值被说成了升级：{notice}"
    assert "没有可复用的历史结论" in notice, notice
    assert "整窗读取" in notice, notice
    # 「首次」得留着：升级弹窗那一侧一直用这个词，两条路说的要是同一件事。
    assert "首次" in notice, notice
    assert "first_run" not in notice, f"{rel}: 原因码印给用户了：{notice}"


@with_rels
def test_force_full_rebuild_says_rebuilt_not_upgraded(rel: str):
    """`force_full_rebuild`：平台**重建**了整窗输入，但不是「升级」。"""
    notice = _notice(rel, "force_full_rebuild")

    assert notice, f"{rel}: 这一档一个字都没说"
    assert "升级" not in notice, f"{rel}: 重建被说成了升级：{notice}"
    assert "重建了整窗输入" in notice, notice
    assert "必须整窗重建" in notice, notice
    assert "force_full_rebuild" not in notice, f"{rel}: 原因码印给用户了：{notice}"


@with_rels
@pytest.mark.parametrize("code", SUGGESTION_CODES)
def test_a_suggestion_reason_is_only_a_suggestion(rel: str, code: str):
    """建议原因（变化多 / 占比高 / 关键路径）**不产生**全量，更不是升级。"""
    notice = _notice(rel, code)

    assert notice, f"{rel}: {code} 这一档一个字都没说"
    assert "平台建议整窗重跑" in notice, notice
    assert "升级" not in notice, f"{rel}: 建议被说成了升级：{notice}"
    assert code not in notice, f"{rel}: 原因码印给用户了：{notice}"


@with_rels
def test_a_runtime_scope_upgrade_still_says_what_happened(rel: str):
    """`runtime_scope_upgrade` 是运行期真的扩了范围：照实说，但不印码。"""
    notice = _notice(rel, "runtime_scope_upgrade")

    assert notice, rel
    assert "全量" in notice, notice
    assert "runtime_scope_upgrade" not in notice, f"{rel}: 原因码印给用户了：{notice}"


@with_rels
def test_an_unknown_code_is_quoted_not_explained_away(rel: str):
    """认不出来的码如实写出来 —— 编一句解释比不解释更坏。"""
    notice = _notice(rel, "unknown_code")
    assert UNKNOWN_CODE in notice, notice


@with_rels
def test_nothing_is_announced_when_nothing_changed(rel: str):
    """用户自己要的全量 / 仍是增量 / 还在等同步 / 没有 job → 空串（不许无话找话）。"""
    for name in ("explicit_full", "still_incremental", "waiting", "no_job"):
        assert _notice(rel, name) == "", f"{rel}: {name} 不该说一句话"


@with_rels
def test_no_notice_ever_prints_a_reason_code(rel: str):
    """所有分支都不许把原因码原样印给用户（旧兜底那句「原因码：xxx」已经删了）。"""
    result = _run_node()[rel]
    for name, item in result.items():
        if name.startswith("__"):
            continue
        notice = item["notice"]
        for code in ("no_conclusion_baseline", "first_run", "force_full_rebuild",
                     "runtime_scope_upgrade") + SUGGESTION_CODES:
            assert code not in notice, f"{rel}/{name}: 原因码印出来了：{notice}"


@with_rels
def test_a_known_reason_still_reads_in_chinese(rel: str):
    """`weeklyAiUpgradeReasonText` 仍然把码翻成人话（映射保持，不许回退成码）。"""
    reasons = _run_node()[rel]["__reasons"]
    for code in ("no_conclusion_baseline", "first_run", "force_full_rebuild",
                 "runtime_scope_upgrade") + SUGGESTION_CODES:
        text = reasons[code]
        assert text, f"{rel}: {code} 翻不出人话"
        assert code not in text, f"{rel}: {code} 的译文里还带着码：{text}"
        assert re.search(r"[\u4e00-\u9fff]", text), f"{rel}: {code} 的译文不是中文：{text}"


# ---------------------------------------------------------------------------
#  P2-2：`focus=code` 在 tables 范围下会是空的 —— 静态说明必须在
# ---------------------------------------------------------------------------

def _focus_hint_text(rel: str) -> str:
    """取出模板里那句静态说明的正文（两份模板的 div id 不同，句子必须一样）。"""
    html = _read(rel)
    pattern = r'id="(?:weeklyAiFocusHelp|aiAnalysisProfileSelectFocusHint)"[^>]*>(.*?)</div>'
    match = re.search(pattern, html, re.S)
    assert match, f"{rel}: 没有那句「只看代码仓库会得到空范围」的静态说明"
    return match.group(1)


@with_rels
def test_the_focus_empty_hint_is_static_markup(rel: str):
    """说明必须是**页面里的静态文字**：不靠请求、不靠任何后端字段。"""
    html = _read(rel)
    hint = _squash(_focus_hint_text(rel))

    assert hint == _squash(FOCUS_HINT), f"{rel}: 说明的措辞被改了：{hint}"
    # 关键信息一个都不能少：前提（范围只含配表）、后果（空范围）、去哪改（全部仓库）。
    for piece in ("项目分析范围只含配置表仓库时", "「只看代码仓库」会得到空范围",
                  "要分析代码仓库", "项目概览页", "分析范围改为「全部仓库」"):
        assert piece in hint, f"{rel}: 说明里少了「{piece}」：{hint}"
    # 它不在 JS 里：说明不是拼出来的，也不该跟着某次请求出现/消失。
    assert _squash(FOCUS_HINT) not in _squash(_script(rel)), (
        f"{rel}: 说明被搬进脚本里了 —— 那会变成「有结果才出现」的一句话"
    )
    # 模板里没有 `focus_empty` 这个后端原因码：前端不重新判一遍范围（不改后端）。
    assert "focus_empty" not in _script(rel), (
        f"{rel}: 前端开始自己判 `focus_empty` 了 —— 那是服务端的账，不该抄第二份"
    )
    assert "weeklyAiFocusHelp" in html or "aiAnalysisProfileSelectFocusHint" in html


def test_the_focus_empty_hint_is_identical_across_the_two_templates():
    """两份周版本模板的说明**逐字相同**（只差 HTML 缩进）。"""
    texts = [_squash(_focus_hint_text(rel)) for rel in WEEKLY]
    assert texts[0] == texts[1], "两份模板的「空范围」说明不一致"


def test_the_weekly_hint_sits_next_to_the_range_select():
    """weekly：说明必须紧跟 `weeklyAiFocus` 那个下拉（用户选范围的地方）。

    放到预算横幅之后、或者抽屉外面，用户会在「看到那句话」与「知道该改哪儿」之间
    多绕一步 —— 而这一条的全部价值就是那一步。
    """
    html = _read("templates/weekly_version_diff.html")
    select_at = html.index('id="weeklyAiFocus"')
    hint_at = html.index('id="weeklyAiFocusHelp"')
    banner_at = html.index('id="aiBudgetBanner"')

    assert select_at < hint_at < banner_at, "weekly: 说明没有紧跟在范围下拉后面"
    # 可访问性：下拉要把它算作自己的描述（读屏用户同样需要这句）。
    select_tag = html[select_at - 200: html.index(">", select_at)]
    assert 'aria-describedby="weeklyAiFocusHelp"' in select_tag, (
        "weekly: 范围下拉没有把说明挂进 aria-describedby"
    )


def test_the_merged_hint_sits_next_to_the_profile_select():
    """merged：`focus` 是写死 `'all'` 的（没有那个下拉），所以说明落在**项目分析范围**
    那一栏 —— 用户能改「只含配置表仓库 / 全部仓库」的只有这里。
    """
    html = _read("templates/merged_project_view.html")
    select_at = html.index('id="aiAnalysisProfileSelect"')
    hint_at = html.index('id="aiAnalysisProfileSelectFocusHint"')
    auto_toggle_at = html.index('id="aiWeeklyAutoToggle"')

    assert select_at < hint_at < auto_toggle_at, "merged: 说明没有落在分析范围那一栏里"
    assert "aiAnalysisProfileSelectFocusHint" in html[select_at: hint_at], (
        "merged: 分析范围下拉没有把说明挂进 aria-describedby"
    )
    # merged 的抽屉自己不带范围选择（`focus` 恒为 all）：这一条不是「同步不过来」，
    # 是那份模板本来就没有这个控件 —— 说明因此只能落在项目分析范围上。
    script = _script("templates/merged_project_view.html")
    assert "const focus = 'all';" in script
    assert "weeklyAiFocusValue()" not in script
