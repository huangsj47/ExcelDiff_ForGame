# -*- coding: utf-8 -*-
"""WP19：AI 配置面板的「分析范围」（`analysis_profile`）+ 预检回退提示。

## 先说清现实边界（免得下一个人照着计划去找不存在的东西）

WP19 的计划里写的是「三个模板的 AI 配置面板加这一栏」，但仓库里**只有一个** AI 配置
面板：`templates/merged_project_view.html` 的 `aiConfigCard` / `aiConfigModal` /
`AI_FIELD_DOM`。另外两份 AI 抽屉模板（`weekly_version_diff.html`、
`commit_diff_new.html`）没有配置面板，它们的配置入口是抽屉里那句「请去项目概览页的
『AI 分析配置』里补全…」；`commit_diff_new.html` 连预检 / `estimate` 路径都没有
（grep `estimate` 零命中）。所以：

* 「分析范围」下拉**只可能**落在这一个面板里 —— 不是漏改另外两份，是它们没有这张表；
* 回退提示（`profile_fallback`）落在**真有预检**的两份周版本模板的确认框 / 升级弹窗里。

计划里「三个模板都含该字段」的那条验收与仓库结构冲突，这里按真实结构断言，并把
「为什么只有一份」写进用例名与注释，而不是伪造两份不存在的面板。

## 为什么这些断言要分层

* **静态断言**守「字段存在、选项文案、默认值、读写接线」——这些是形状；
* **node 真跑**守「缺键不报错、只在有值时才显示」——这是行为。静态断言分不清
  「读了 `profile_fallback`」与「缺键就抛错 / 不管有没有值都显示一句」。本仓库的
  升级弹窗测试（`tests/test_ai_upgrade_dialog_incremental.py`）已是同一手法。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 唯一有 AI 配置面板的模板（见模块 docstring）。
PANEL_TEMPLATE = "templates/merged_project_view.html"
# 有预检 / 升级弹窗的两份周版本模板（提交页没有 estimate 路径）。
WEEKLY_TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)
ALL_DRAWER_TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
    "templates/commit_diff_new.html",
)

# 与 test_ai_config_template.py / test_ai_config_panel_design.py 同一组切片锚点。
MODAL_START = '<div class="card mb-4" id="aiConfigCard">'
MODAL_END = "{% if not repositories %}"
JS_START = "// AI 分析配置：页面摘要行 + 居中模态框"
JS_END = "function normalizeRiskLevel(level) {"

PROFILE_FIELD = "analysis_profile"
PROFILE_DOM_ID = "aiAnalysisProfileSelect"
PROFILE_LABEL = "分析范围"
OPTION_TABLES = "仅配置表仓库（含仓库内代码/产物，推荐）"
OPTION_ALL = "全部仓库"
# 预检里那句回退提示。**一个字都不许改**：它是用户唯一能知道「这次看的不是配表范围」
# 的地方（payload 里的 profile_fallback 用户看不到）。
FALLBACK_TEXT = "项目没有配置表仓库，本次已回退为全部分析"


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _modal_html() -> str:
    content = _read(PANEL_TEMPLATE)
    return content[content.index(MODAL_START) : content.index(MODAL_END)]


def _ai_script() -> str:
    content = _read(PANEL_TEMPLATE)
    return content[content.index(JS_START) : content.index(JS_END)]


def _field_dom_map() -> dict:
    """从 JS 的 `AI_FIELD_DOM` 表里解析出「字段名 → DOM id」（与既有测试同一手法）。"""
    block = re.search(r"const AI_FIELD_DOM = \{(.*?)\n\};", _ai_script(), re.S)
    assert block, "AI_FIELD_DOM 表不见了"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def _select_options() -> list:
    html = _modal_html()
    tag = re.search(
        rf'<select[^>]*id="{PROFILE_DOM_ID}"[^>]*>(.*?)</select>', html, re.S
    )
    assert tag, f"配置面板里没有 {PROFILE_DOM_ID} 这个下拉"
    return re.findall(r'<option value="([^"]+)"([^>]*)>(.*?)</option>', tag.group(1), re.S)


# ==========================================================================
# 1. 面板里的这一栏：选项、默认值、label / help / error
# ==========================================================================


def test_the_panel_offers_the_analysis_profile_select():
    """「分析范围」必须是一个真实的下拉，两个选项的文案都要在。

    面板是**手写字段**（不按 field_schema 自动渲染），所以选项文案也得手写：
    `tables` / `all` 这种机器值直接给用户看，等于让他去猜这两个词是什么意思。
    """
    options = _select_options()
    values = [value for value, _attrs, _text in options]
    assert values == ["tables", "all"], f"两个选项的取值不对：{values}"
    texts = [text.strip() for _value, _attrs, text in options]
    assert texts == [OPTION_TABLES, OPTION_ALL], f"两个选项的文案不对：{texts}"


def test_the_default_is_tables():
    """默认必须是 `tables`（与 `project_config.resolved()` / `FIELD_DEFAULTS` 一致）。

    默认值写错的后果是静默的：用户什么都没改，平台却按 `all` 把代码仓库一起分析，
    账单与耗时都变了，而配置界面上看不出他做过任何选择。
    """
    options = _select_options()
    selected = [(value, attrs) for value, attrs, _text in options if "selected" in attrs]
    assert len(selected) == 1 and selected[0][0] == "tables", (
        f"HTML 里的默认选项不是 tables：{selected}"
    )
    # JS 回填也要有兜底：老库这一列是 NULL、或者旧服务端根本没这个键。
    assert "setValue('aiAnalysisProfileSelect', data.analysis_profile || 'tables');" in _ai_script(), (
        "读取时没有把缺键 / null 兜底成 tables"
    )


def test_the_backend_choices_and_this_dropdown_do_not_drift():
    """选项取值与默认值必须与 `endpoint_service.FIELD_RULES` 逐字一致。

    「值域有几份」是本仓库踩过的坑（`test_ai_models_and_migration.py` 里专门有一条
    防漂移用例）：界面写 `all`、后端只认 `every` 的话，保存会被 400 打回来，而界面
    上没有任何预兆。
    """
    from services.ai.endpoint_service import FIELD_DEFAULTS, FIELD_RULES

    rule = FIELD_RULES[PROFILE_FIELD]
    assert rule.label == PROFILE_LABEL, f"后端 label 与面板不一致：{rule.label}"
    assert tuple(rule.choices) == ("tables", "all"), rule.choices
    assert FIELD_DEFAULTS[PROFILE_FIELD] == "tables"
    assert [value for value, _attrs, _text in _select_options()] == list(rule.choices)


def test_the_field_has_a_label_help_and_error_slot_like_every_other_field():
    """新增一栏必须补齐 label / help / error 三件套。

    缺 help 的一栏只说了「分析范围」，用户不知道选哪一档；
    缺 error 槽的后果更隐蔽：服务端回的错误会落进顶部摘要，而这一栏旁边什么都没有。
    """
    html = _modal_html()
    label = re.search(r'<label[^>]*for="aiAnalysisProfileSelect"[^>]*>(.*?)</label>', html, re.S)
    assert label and PROFILE_LABEL in label.group(1), "这一栏没有 label"

    tag = re.search(rf'<select[^>]*id="{PROFILE_DOM_ID}"[^>]*>', html, re.S)
    assert tag, f"找不到 {PROFILE_DOM_ID}"
    described = re.search(r'aria-describedby="([^"]*)"', tag.group(0))
    tokens = described.group(1).split() if described else []
    for expected in (f"{PROFILE_DOM_ID}Help", f"{PROFILE_DOM_ID}Error"):
        assert expected in tokens, f"{PROFILE_DOM_ID} 的 aria-describedby 少了 {expected}"

    help_text = re.search(
        rf'id="{PROFILE_DOM_ID}Help"[^>]*>(.*?)</div>', html, re.S
    )
    assert help_text, "这一栏没有帮助文案"
    # 计划里指定的那句：说清「tables 并不是不看代码，只是不看代码仓库」。
    assert "默认只看配置表仓库；仓库内的代码/产物也会分析" in help_text.group(1), (
        "帮助文案没有说清 tables 的真实范围"
    )
    assert f'id="{PROFILE_DOM_ID}Error"' in html, "这一栏没有内联错误位置"


def test_the_field_sits_in_the_strategy_group_before_the_switches():
    """位置：进「分析策略」组，且排在自动分析 / 子代理这些开关之前。

    分析范围决定「这一轮看什么」，比「跑不跑、要不要分片」更靠前；塞在预算那几栏
    后面的话，用户会先被一堆数字吸引，改完预算才看见范围其实收窄着。
    """
    html = _modal_html()
    strategy = html[html.index('id="aiSectionStrategy"'):]
    strategy = strategy[: strategy.index("</fieldset>")]
    select_at = strategy.index(f'id="{PROFILE_DOM_ID}"')
    assert select_at < strategy.index('id="aiWeeklyAutoToggle"'), "分析范围排在了开关之后"
    assert select_at < strategy.index('id="aiSubagentToggle"'), "分析范围排在了子代理开关之后"


# ==========================================================================
# 2. 读写接线：走既有配置 API，不另开一条路
# ==========================================================================


def test_the_field_is_in_the_field_table_so_errors_and_readonly_cover_it():
    """进 `AI_FIELD_DOM` 才有字段级报错与只读锁定。

    `initAiConfig` 的只读态是拿 `Object.values(AI_FIELD_DOM)` 逐个 disable 的；
    这一栏不进表，只读用户就还能改它 —— 而页面上看不出任何异常。
    """
    dom = _field_dom_map()
    assert dom.get(PROFILE_FIELD) == PROFILE_DOM_ID, (
        f"字段表里没有 {PROFILE_FIELD} → {PROFILE_DOM_ID}：服务端的字段错误落不到这一栏"
    )
    script = _ai_script()
    assert "const fieldEls = Object.values(AI_FIELD_DOM).map(aiEl).filter(Boolean);" in script, (
        "只读锁定不再遍历字段表 —— 新字段会漏出只读态"
    )


def test_the_select_is_loaded_and_saved_through_the_existing_config_api():
    """读：`/config` 的 `analysis_profile` 回填；写：进 `collectAiConfigPayload`。

    两处缺一处的症状不同：只读不写 = 改了不生效（保存成功但下一轮还是老范围）；
    只写不读 = 打开配置永远显示默认值，用户以为没存上，再点一次保存又覆盖掉真实值。
    """
    script = _ai_script()
    assert f"setValue('{PROFILE_DOM_ID}', data.{PROFILE_FIELD} || 'tables');" in script, (
        "读取配置时没有回填这一栏"
    )
    assert f"payload.{PROFILE_FIELD} = analysisProfile.value;" in script, (
        "保存时没有提交这一栏"
    )
    assert f"const analysisProfile = aiEl('{PROFILE_DOM_ID}');" in script
    # 提交体走的是既有的 collectAiConfigPayload → /config，不是另开的端点。
    assert "collectAiConfigPayload()" in script


# ==========================================================================
# 3. 回退提示：只在 `profile_fallback == no_table_repository` 时出现
# ==========================================================================


@pytest.mark.parametrize("rel", WEEKLY_TEMPLATES, ids=["weekly", "merged"])
def test_the_precheck_reads_profile_fallback_defensively(rel: str):
    """`estimate.mode_decision.profile_fallback` 要用缺键不炸的读法。

    旧服务端（WP16 之前）没有这个字段；`estimate` 本身也可能读不到（`fetchWeeklyAiEstimate`
    失败时回 null）。直接解引用会让整个确认框抛异常，用户连「开始分析」都点不了。

    读法收在 `weeklyAiModeDecision` 里 —— 它本来就是「预检载荷里的模式裁决」唯一读点
    （函数自述写着同一句话），回退原因属于同一份裁决。
    """
    source = _read(rel)
    assert "profile_fallback: String(pick('profile_fallback', '') || '')," in source, (
        f"{rel}: mode_decision 没有带 profile_fallback（或没有做缺键兜底）"
    )
    # `pick` 的兜底就是 JS 侧的「.get」：undefined / null 都回空串，不是抛错。
    assert "return raw[key] === undefined || raw[key] === null ? fallback : raw[key];" in source


@pytest.mark.parametrize("rel", WEEKLY_TEMPLATES, ids=["weekly", "merged"])
def test_the_fallback_line_is_wired_into_both_precheck_dialogs(rel: str):
    """回退提示要进**预检**（`weeklyAiEstimateNote`）与**升级弹窗**（`weeklyAiUpgradeChoiceDialog`）。

    只进其中一处的话，另一种路径上的用户仍然不知道「这次看的是全部仓库」——
    而两种路径用的都是同一份 `estimate`，没有理由只在一处说。
    """
    source = _read(rel)
    # 普通预检：插到 notes 最前面。
    assert "const fallback = weeklyAiModeDecision(estimate).profile_fallback === 'no_table_repository'" in source, (
        f"{rel}: 普通预检没有判 profile_fallback"
    )
    assert "if (fallback) notes.unshift(fallback);" in source, (
        f"{rel}: 普通预检没有把回退提示排在最前"
    )
    # 升级弹窗：进 note 最前面。
    assert (
        "const fallbackNote = weeklyAiModeDecision(incrementalEstimate).profile_fallback"
        " === 'no_table_repository'"
    ) in source, f"{rel}: 升级弹窗没有判 profile_fallback"
    assert "if (fallbackNote) note.push(fallbackNote);" in source


@pytest.mark.parametrize("rel", ALL_DRAWER_TEMPLATES, ids=["weekly", "merged", "commit"])
def test_the_fallback_sentence_is_not_invented_elsewhere(rel: str):
    """三份模板都不许**另写一句**「可能回退了」的猜测文案。

    这条守的是「没有证据就不说」：`profile_fallback` 只有真的回退时才存在，
    模板里若出现一句无条件的「可能回退了」，用户会一直看到它。

    两份周版本模板各出现 **2 次**：普通预检与升级弹窗各一条路径（同一句话，
    两处都只在这一个原因码成立时才会被赋出去）。提交页没有预检，出现就是错。
    """
    source = _read(rel)
    occurrences = source.count(FALLBACK_TEXT)
    if rel in WEEKLY_TEMPLATES:
        assert occurrences == 2, (
            f"{rel}: 回退文案出现了 {occurrences} 次 —— 预检与升级弹窗各一次，不该多也不该少"
        )
    else:
        assert occurrences == 0, (
            f"{rel}: 这份模板没有预检路径，不该出现回退文案（出现了也没有 estimate 可读）"
        )


# --------------------------------------------------------------------------
#  node 探针：把模板里逐字那一份回退逻辑真跑一遍
# --------------------------------------------------------------------------

_PROBE_TEMPLATE = r"""
var probeCalls = [];

// 升级弹窗的依赖全部换成桩：这一条只测「回退提示进没进 note」。
function weeklyAiUpgradeReasonText(code) { return code ? ('原因：' + code) : ''; }
function weeklyAiEstimateLines() { return []; }
function weeklyAiSplitEstimateLines() { return { primary: [], detail: [] }; }
function weeklyAiCostCompare() { return null; }
function weeklyAiChoiceDialog(spec) { probeCalls.push(spec); return Promise.resolve(null); }

__EXTRACTED__

globalThis.__probe = {
    run: async function (cases) {
        const out = [];
        for (const item of cases) {
            probeCalls = [];
            const entry = { name: item.name };
            entry.profileFallback = weeklyAiModeDecision(item.estimate).profile_fallback;
            entry.estimateNote = weeklyAiEstimateNote(item.estimate);
            await weeklyAiUpgradeChoiceDialog(
                { suggested_full: false, upgrade_reason: '' }, item.estimate, null, ''
            );
            entry.dialogNote = probeCalls.length ? probeCalls[0].note : null;
            out.push(entry);
        }
        return out;
    }
};
"""

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

(async function () {
    const result = {};
    for (const rel of Object.keys(payload.probes)) {
        try {
            const sandbox = { console: console };
            sandbox.window = sandbox;
            vm.createContext(sandbox);
            vm.runInContext(payload.probes[rel], sandbox, { filename: 'wp19_probe.js' });
            result[rel] = await sandbox.__probe.run(payload.cases);
        } catch (err) {
            result[rel] = { error: String((err && err.stack) || err) };
        }
    }
    process.stdout.write(JSON.stringify(result));
})().catch(function (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""

_CASES = [
    # 估不出来 / 旧服务端没有字段：一个字都不许显示，也不许抛错。
    {"name": "missing_estimate", "estimate": None},
    {"name": "no_mode_decision", "estimate": {"notes": []}},
    {"name": "empty_mode_decision", "estimate": {"notes": [], "mode_decision": {}}},
    {"name": "other_fallback_reason",
     "estimate": {"notes": [], "mode_decision": {"profile_fallback": "something_else"}}},
    # 真的回退了：预检与升级弹窗都要说，而且排在最前。
    {"name": "fallback_no_table_repository",
     "estimate": {"notes": ["**历史样本不足**"],
                  "mode_decision": {"profile_fallback": "no_table_repository"}}},
]

_RESULTS: dict = {}


def _function_source(source: str, name: str) -> str:
    """按花括号配平抠出一个 function 声明（`async` 修饰符一起带上）。"""
    marker = f"function {name}("
    assert marker in source, f"模板里找不到 {name}"
    start = source.index(marker)
    if source[:start].endswith("async "):
        start -= len("async ")
    brace = source.index("{", source.index(marker))
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} 的大括号不配对")


def _probe_source(rel: str) -> str:
    source = _read(rel)
    extracted = "\n".join(
        _function_source(source, name)
        for name in (
            "weeklyAiModeDecision",
            "weeklyAiEstimateNote",
            "weeklyAiUpgradeChoiceDialog",
        )
    )
    return _PROBE_TEMPLATE.replace("__EXTRACTED__", extracted)


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_config_profile_driver.js"
    payload = workdir / "ai_config_profile_cases.json"
    driver.write_text(_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps(
            {"probes": {rel: _probe_source(rel) for rel in WEEKLY_TEMPLATES},
             "cases": _CASES},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"回退逻辑在 node 里跑不起来：\n{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


def _case(rel: str, name: str) -> dict:
    result = _run_node()[rel]
    assert "error" not in result, f"{rel}: 回退逻辑抛异常：{result['error']}"
    return next(item for item in result if item["name"] == name)


@pytest.mark.parametrize("rel", WEEKLY_TEMPLATES, ids=["weekly", "merged"])
def test_a_missing_fallback_produces_no_text_at_all(rel: str):
    """缺字段 / 别的回退原因 / estimate 为 null → 两处都不出现那句话。

    原因码本身可能是个前端还不认识的新值（`other_fallback_reason`）：它原样透传，
    但**不产生文案** —— 文案只属于 `no_table_repository` 这一个已知原因。
    """
    for name in ("missing_estimate", "no_mode_decision", "empty_mode_decision",
                 "other_fallback_reason"):
        item = _case(rel, name)
        assert item["profileFallback"] != "no_table_repository", (
            f"{rel}/{name}: 没有回退却读成了回退原因：{item['profileFallback']!r}"
        )
        assert FALLBACK_TEXT not in item["estimateNote"], (
            f"{rel}/{name}: 普通预检里凭空出现了回退提示：{item['estimateNote']!r}"
        )
        assert FALLBACK_TEXT not in (item["dialogNote"] or ""), (
            f"{rel}/{name}: 升级弹窗里凭空出现了回退提示：{item['dialogNote']!r}"
        )


@pytest.mark.parametrize("rel", WEEKLY_TEMPLATES, ids=["weekly", "merged"])
def test_the_fallback_reason_shows_the_sentence_first(rel: str):
    """`no_table_repository` → 预检与升级弹窗的 note 都以那句话开头。

    排在最前是**有意的**：它是「这次看的是全部仓库」这个前提，比后面的精确度说明
    和平台建议都更该先被读到。
    """
    item = _case(rel, "fallback_no_table_repository")
    assert item["profileFallback"] == "no_table_repository", item["profileFallback"]
    assert item["estimateNote"].startswith(FALLBACK_TEXT), item["estimateNote"]
    assert item["dialogNote"] and item["dialogNote"].startswith(FALLBACK_TEXT), item["dialogNote"]
    # 原有的「不是精确值」说明不能因为前面插了一句就丢掉。
    assert "不是精确值" in item["estimateNote"]
    assert "不是精确值" in item["dialogNote"]
