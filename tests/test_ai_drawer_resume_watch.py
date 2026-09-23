# -*- coding: utf-8 -*-
"""重开抽屉（或带 `?ai_job=` 刷新页面）之后，「思考过程」必须**立刻**有内容。

## 用户报的那一幕

> 关掉抽屉再重新打开，如果这时 AI 在分析中，抽屉**不会马上刷新**分片思考信息，
> 只有**新**分片思考内容出来了才会刷新。

这句话里的每个字都对上了同一个机制：

```
resumeWeeklyAiJob()                      ← 重开抽屉 / 刷新页面附着到这次运行
 ├ startAiBudgetWatch(44)                ← 起进度轮询；watchRun 会**立刻先发一帧**
 └ subscribeWeeklyAiJob(17)
    └ closeWeeklyAiStream()
       └ stopAiBudgetWatch()             ← **把刚起来的那次轮询当场掐掉**
```

`AiStreamStatus.watchRun` 的 `stop()` 把 `stopped` 置真，而第一帧的响应回来时走的是
`if (stopped || !data || !data.success) return data || null;` —— 那一帧被丢掉，
`onProgress` **一次都没被调用**。于是：

* 「思考过程」拿到的是**空**（`setRun` 刚把上一次的 `blocks` 清掉），面板顶上还挂着
  `unwatch()` 留下的「分析已结束 / 这次运行没有留下逐轮记录」；
* 抽屉被 `AiDrawerTabs.markSettled()` 切回了「完整结论」；
* 之后**唯一**还会重画这个面板的是 SSE 的 `progress` 事件（服务端每跑完一轮推一帧）——
  也就是「只有**新**分片思考内容出来了才会刷新」。用户的描述与此逐字相符。

## 为什么这条守在「顺序」上而不是某一句文案上

真正的判据是**行为**：跑完 `resumeWeeklyAiJob()` 之后这次运行的进度轮询**还活着**
（`aiBudgetWatch` 非空），并且它的第一帧真的写进了面板（假 DOM 的 `#aiThinkLog` 里
出现了那几轮）。把模板里那两行换回原来的顺序，这条就会红 —— 见用例里那几个断言各自
盯的是哪一件事。

## 这份用例怎么跑

把模板内联脚本里**逐字的那一段**（`stopAiBudgetWatch` 到 `resumeWeeklyAiJob` 结束）
取出来放进 node 沙箱，配上真的 `ai_think_log.js` / `ai_stream_status.js` /
`ai_drawer_tabs.js` 与一个假 DOM、假 fetch。所以断言看的是**模板自己那段代码**的执行
结果，不是一份抄本。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = (
    "templates/merged_project_view.html",
    "templates/weekly_version_diff.html",
)
# 两份模板里承载这一次运行的那几个名字。**只有这两处不同**，其余逐字相同
# （合并视图把「最近结论」叫 `refreshWeeklyAiLatest`、周版本页叫 `loadWeeklyAiLatest`，
# 但这次要用的那几个函数同名）。
SLICE_START = "function stopAiBudgetWatch("
SLICE_END = "async function resumeFromWeeklyAiJob("

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const hostSetTimeout = setTimeout;

const JOB = __JOB__;
const PROGRESS = __PROGRESS__;

const out = {};

function makeEl(id) {
    var node = { id: id, hidden: false, disabled: false, children: [], _attrs: {} };
    var text = '';
    Object.defineProperty(node, 'textContent', {
        get: function () { return text; },
        set: function (v) {
            text = v === null || v === undefined ? '' : String(v);
            if (text === '') node.children.length = 0;   // 真 DOM 里 textContent='' 会清子节点
        }
    });
    node.setAttribute = function (k, v) { node._attrs[k] = String(v); };
    node.getAttribute = function (k) {
        return node._attrs[k] === undefined ? null : node._attrs[k];
    };
    node.removeAttribute = function (k) { delete node._attrs[k]; };
    node.appendChild = function (child) { node.children.push(child); return child; };
    node.addEventListener = function () {};
    node.focus = function () {};
    node._classes = [];
    node.classList = {
        add: function (c) { if (node._classes.indexOf(c) < 0) node._classes.push(c); },
        remove: function (c) {
            var at = node._classes.indexOf(c);
            if (at >= 0) node._classes.splice(at, 1);
        },
        contains: function (c) { return node._classes.indexOf(c) >= 0; }
    };
    return node;
}

var els = {};
function el(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
}

var fetchLog = [];
function jsonResponse(payload) {
    return Promise.resolve({
        ok: true, status: 200,
        json: function () { return Promise.resolve(payload); }
    });
}

// 模板里**声明在取出的那一段之前**的那些运行时状态，由这里给（它们本来就是页面级的
// 变量：`let aiBudgetWatch = null;` 之类）。断言直接读它们 —— 这一次要验的正是
// 「关 → 开」之后这几个值是什么。
var RUNTIME = {
    configId: 3,                 // 周版本页是 `let configId = {{ config.id }};`
    aiBudgetWatch: null,
    weeklyAiRunId: null,
    weeklyAiStreamSettled: false,
    weeklyAiRunActive: false,
    weeklyAiCurrentConfigId: 3,  // 合并视图用
    weeklyAiSource: null,
    weeklyAiHasCached: false
};

var timerIds = [];
// 这一轮场景要不要「读不到进度」（多进程部署：跑在别的 worker 里，快照永远是 null）。
var NO_PROGRESS = false;

function buildSandbox(fromUrl) {
    els = {}; fetchLog = []; timerIds = [];

    var sandbox = { console: console, Map: Map, Promise: Promise, JSON: JSON };
    sandbox.window = sandbox;
    Object.keys(RUNTIME).forEach(function (key) { sandbox[key] = RUNTIME[key]; });
    sandbox.aiBudgetWatch = null;
    sandbox.weeklyAiRunId = null;
    sandbox.weeklyAiStreamSettled = false;
    sandbox.weeklyAiRunActive = false;
    sandbox.weeklyAiSource = null;
    sandbox.weeklyAiHasCached = false;
    sandbox.weeklyAiCache = new Map();
    sandbox.URL = function (href) {
        this.href = href;
        this.searchParams = {
            get: function (key) { return key === 'ai_job' && fromUrl ? '17' : null; },
            set: function () {},
            delete: function () {}
        };
        this.toString = function () { return href; };
    };
    sandbox.location = { href: 'http://box/projects/2/merged-view' + (fromUrl ? '?ai_job=17' : '') };
    sandbox.history = { state: null, replaceState: function () {} };
    sandbox.setTimeout = hostSetTimeout;
    // 真定时器一律不参与：把注册的回调扣下来，测试自己决定什么时候跑它们。
    sandbox.setInterval = function (fn) { timerIds.push(timerIds.length + 1); return timerIds.length; };
    sandbox.clearInterval = function (id) { timerIds = timerIds.filter(function (item) { return item !== id; }); };
    sandbox.document = {
        getElementById: el,
        createElement: function (tag) { return makeEl(tag); },
        addEventListener: function () {}
    };
    sandbox.EventSource = function (url) {
        this.url = url;
        this.closed = false;
        this.handlers = {};
        this.addEventListener = function (type, fn) { this.handlers[type] = fn; };
        this.close = function () { this.closed = true; };
    };
    sandbox.fetch = function (url) {
        fetchLog.push(String(url));
        if (String(url).indexOf('/jobs/') >= 0) return jsonResponse(JOB);
        if (NO_PROGRESS) {
            // 多进程部署：跑在别的 worker 里，本进程永远不会有快照。
            return jsonResponse({ success: true, status: 'running', progress: null });
        }
        return jsonResponse(PROGRESS);
    };
    // 页面里那些只为显示服务的桩：这次要验的不是它们。
    sandbox.setWeeklyAiStatusBadge = function () {};
    sandbox.setWeeklyAiOutput = function () {};
    sandbox.renderAiBudget = function () {};
    sandbox.renderAiUsageLine = function () {};
    sandbox.appendWeeklyAiLine = function () {};
    sandbox.prependWeeklyAiLine = function () {};
    sandbox.updateRiskLabelByConfig = function () {};
    sandbox.AiReportExport = { track: function () {} };
    sandbox.AiContextNotice = { coverageNotice: function () { return ''; },
                                contextNotice: function () { return ''; },
                                withContextNotice: function (text) { return text; } };
    sandbox.AiBudgetNotice = { renderBanner: function () {} };
    sandbox.AiUsageLine = { fmtTokens: function (v) { return String(v); } };

    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(__THINK_LOG__, 'utf8'), sandbox);
    vm.runInContext(fs.readFileSync(__STREAM_STATUS__, 'utf8'), sandbox);
    vm.runInContext(fs.readFileSync(__DRAWER_TABS__, 'utf8'), sandbox);
    vm.runInContext(__SLICE__, sandbox, { filename: 'template-slice.js' });
    return sandbox;
}

function cards() {
    return (els['aiThinkLog'] ? els['aiThinkLog'].children : []).filter(function (node) {
        // 列表顶上那句「只列出最近 N 轮」也是一个子节点（它不是一轮）。
        return node.className === 'ai-think-round';
    }).map(function (node) {
        var head = (node.children || []).filter(function (child) {
            return child.className === 'ai-think-round-head';
        })[0];
        return head ? head.textContent : ('<' + node.className + '>');
    });
}

function moreNote() {
    var log = els['aiThinkLog'];
    if (!log) return '';
    var hit = log.children.filter(function (node) {
        return String(node.className).indexOf('ai-think-more') >= 0;
    })[0];
    return hit ? hit.textContent : '';
}

function state(sandbox) {
    return {
        inMemory: {
            budgetWatch: sandbox.aiBudgetWatch === null ? null : 'object',
            weeklyAiRunId: sandbox.weeklyAiRunId,
            runActive: sandbox.weeklyAiRunActive
        },
        think: sandbox.AiThinkLog.state(),
        tab: sandbox.AiDrawerTabs.current(),
        thinkHidden: els['aiDrawerPanelThink'] ? els['aiDrawerPanelThink'].hidden : null,
        note: els['aiThinkNote'] ? els['aiThinkNote'].textContent : '',
        cards: cards(),
        moreNote: moreNote(),
        fetches: fetchLog.slice()
    };
}

function tick() { return new Promise(function (resolve) { hostSetTimeout(resolve, 0); }); }

async function scenario(fromUrl, noProgress) {
    NO_PROGRESS = !!noProgress;
    var sandbox = buildSandbox(fromUrl);
    if (!fromUrl) {
        // 关抽屉再打开：这次点击的身份还在内存里（`rememberWeeklyAiJobId` 就是页面
        // 记下它的那一步，分析发起时调过一次）。
        sandbox.rememberWeeklyAiJobId('17');
    }
    // ① 打开抽屉、这次运行已经在跑：面板先被清成「这一次的」空状态
    //    （`openWeeklyAiDrawer` 里的 `AiThinkLog.setRun(null)`）。
    sandbox.AiThinkLog.setRun(null);
    var beforeResume = state(sandbox);
    // ② 附着上去（模板 `openAiDrawerFromRiskLabel` 那条路：`resumeWeeklyAiJob()` 返回真
    //    就不再走 `/latest`，所以「重开抽屉」这条路只有这一次机会把面板填上）。
    var resumed = await sandbox.resumeWeeklyAiJob();
    var afterSync = state(sandbox);
    afterSync.resumed = resumed;
    // ③ 第一帧回来（`watchRun` 在返回之前就发了那一帧，所以只需等微任务）。
    await tick();
    await tick();
    var afterFirstTick = state(sandbox);
    return {
        beforeResume: beforeResume,
        afterResumeSync: afterSync,
        afterFirstTick: afterFirstTick,
        // 第一帧之后还注册着的下一次轮询（3 秒后那一次）。
        pendingTimers: timerIds.length
    };
}

async function run() {
    // 两条路都要走一遍：关掉抽屉再打开（内存里还留着 `weeklyAiJobId`）与刷新页面
    // （身份在地址栏的 `?ai_job=` 里）。它们最后都落在同两句上，但入口不同。
    out.reopen = await scenario(false, false);
    out.reloaded = await scenario(true, false);
    // 第三条：附着上去时**读不到进度**（多进程部署，跑在别的 worker 里）——
    // 这一刻「才刚发起、第一帧还没出来」是**猜的**（本页不是发起方，它可能已经跑了十分钟）。
    out.unreadable = await scenario(false, true);
    process.stdout.write(JSON.stringify(out));
}

run().catch(function (err) {
    process.stdout.write(JSON.stringify({ fatal: String((err && err.stack) || err) }));
});
"""


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _slice(rel: str) -> str:
    """模板内联脚本里 `stopAiBudgetWatch` 那一段（**逐字**，不剥注释）。

    **不剥注释**是有意的：`docs` 与仓库既有的剥离器都吃过「注释里的引号把半张文件吞掉」
    的亏（`tests/test_ai_drawer_stream_state.py` 的 `_strip_js_comments` 是加强版，
    但它仍然是一段启发式）。这里要的是**能求值的源码**，原样取最安全。
    """
    import re

    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _read(rel), re.S)
    assert blocks, f"{rel} 里没有内联脚本"
    script = "\n".join(blocks)
    assert SLICE_START in script, f"{rel} 里找不到 {SLICE_START.strip()}"
    assert SLICE_END in script, f"{rel} 里找不到 {SLICE_END.strip()}"
    start = script.index(SLICE_START)
    end = script.index(SLICE_END)
    assert start < end
    return script[start:end]


# 这一次运行的进度载荷：**真跑过的形状**（run 44 的最后一帧，8 条正好是实时那份的窗口）。
# `agent_index` / `agent_total` 与 `agent_round` 是 `services/ai/run_progress.publish`
# 给每一条补上的家族位次（少了它们，界面写不出「分片 S5 (5/7)」，也排不出家族顺序）。
_PROGRESS = {
    "success": True,
    "status": "running",
    "progress": {
        "round": 3, "max_rounds": 23, "rounds_seen": 34, "rounds_truncated": True,
        "rounds": [
            {"round_index": 8, "agent": "S5", "agent_index": 5, "agent_total": 7,
             "agent_round": 8, "outcome": "final", "requests": [], "executed": [],
             "dropped": [], "response_text": "", "budget_notes": "", "error": ""},
            {"round_index": 1, "agent": "", "agent_index": 6, "agent_total": 7,
             "agent_round": 1, "outcome": "requests", "requests": [], "executed": [],
             "dropped": [], "response_text": "看战斗结算", "budget_notes": "", "error": ""},
            {"round_index": 2, "agent": "", "agent_index": 6, "agent_total": 7,
             "agent_round": 2, "outcome": "requests", "requests": [], "executed": [],
             "dropped": [], "response_text": "", "budget_notes": "", "error": ""},
            {"round_index": 3, "agent": "", "agent_index": 6, "agent_total": 7,
             "agent_round": 3, "outcome": "requests", "requests": [], "executed": [],
             "dropped": [], "response_text": "", "budget_notes": "", "error": ""},
            {"round_index": 4, "agent": "", "agent_index": 6, "agent_total": 7,
             "agent_round": 4, "outcome": "final", "requests": [], "executed": [],
             "dropped": [], "response_text": '{"status":"final","report_markdown":"x"}',
             "budget_notes": "", "error": ""},
            {"round_index": 1, "agent": "V1", "agent_index": 7, "agent_total": 7,
             "agent_round": 1, "outcome": "requests", "requests": [], "executed": [],
             "dropped": [], "response_text": "", "budget_notes": "", "error": ""},
            {"round_index": 2, "agent": "V1", "agent_index": 7, "agent_total": 7,
             "agent_round": 2, "outcome": "requests", "requests": [], "executed": [],
             "dropped": [], "response_text": "", "budget_notes": "", "error": ""},
            {"round_index": 3, "agent": "V1", "agent_index": 7, "agent_total": 7,
             "agent_round": 3, "outcome": "final", "requests": [], "executed": [],
             "dropped": [], "response_text": "", "budget_notes": "", "error": ""},
        ]
    }
}
_JOB = {"success": True, "job": {"job_id": 17, "state": "running", "run_id": 44}}


@pytest.fixture(scope="module", params=TEMPLATES)
def resume(request) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过重开抽屉那一段的真跑断言")
    driver = (
        DRIVER.replace("__THINK_LOG__", json.dumps(str(PROJECT_ROOT / "static/js/ai_think_log.js")))
        .replace("__STREAM_STATUS__", json.dumps(str(PROJECT_ROOT / "static/js/ai_stream_status.js")))
        .replace("__DRAWER_TABS__", json.dumps(str(PROJECT_ROOT / "static/js/ai_drawer_tabs.js")))
        .replace("__SLICE__", json.dumps(_slice(request.param)))
        .replace("__JOB__", json.dumps(_JOB, ensure_ascii=False))
        .replace("__PROGRESS__", json.dumps(_PROGRESS, ensure_ascii=False))
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout)
    assert "fatal" not in payload, payload["fatal"]
    payload["template"] = request.param
    return payload


# 两条入口都跑：`reopen` = 关掉抽屉再打开（身份在内存里 `weeklyAiJobId`），
# `reloaded` = 带 `?ai_job=` 刷新页面（身份在地址栏）。它们最后都落在同两句上，
# 而**重开抽屉那条最要紧**：`resumeWeeklyAiJob()` 返回真就不再走 `/latest`，
# 所以这一次是面板唯一的填充机会。
_ENTRIES = ("reopen", "reloaded")


def _entry(resume: dict, path: str) -> dict:
    assert resume.get(path), f"驱动没有产出 {path} 那一条路"
    return resume[path]


def test_the_progress_watch_survives_attaching_to_a_running_job(resume):
    """**这一条就是修复本身。** 附着到一条正在跑的 job 之后，进度轮询必须还活着。

    `aiBudgetWatch` 为 `None` 说明它被 `subscribeWeeklyAiJob` 里的
    `closeWeeklyAiStream()` 掐掉了（`startAiBudgetWatch` 写在订阅**之前**时就是这个结果）。
    """
    for path in _ENTRIES:
        entry = _entry(resume, path)
        assert entry["afterResumeSync"]["inMemory"]["budgetWatch"] is not None, (
            f"[{path}] 附着上去之后这次运行的进度轮询已经没了 —— 它被订阅那一步"
            "（`subscribeWeeklyAiJob` 里的 `closeWeeklyAiStream()`）掐掉了。"
            "后果是「思考过程」一帧都收不到，只有下一轮跑完才重画。"
        )
        assert entry["afterResumeSync"]["inMemory"]["weeklyAiRunId"] == 44, path
        assert entry["afterResumeSync"]["inMemory"]["runActive"] is True, path


def test_the_first_frame_reaches_the_panel_without_waiting_for_a_new_round(resume):
    """第一帧（`watchRun` 立刻发的那一次）必须**真的写进面板**。

    这是用户报的那句「不会马上刷新分片思考信息」的判据：不修复时第一帧的响应在
    `if (stopped …) return` 处被丢掉，`onProgress` 一次都不被调用，面板停在
    `unwatch()` 留下的那句「分析已结束 / 没有留下逐轮记录」上，`blocks` 一条都没有。
    """
    for path in _ENTRIES:
        first = _entry(resume, path)["afterFirstTick"]

        assert first["think"]["watching"] is True, f"[{path}] 本页没有在看着这次运行跑"
        assert first["think"]["mode"] == "live", (
            f"[{path}] 第一帧之后面板不认为这次运行在跑：mode={first['think']['mode']}，"
            f"顶上那句是 {first['note']!r}"
        )
        assert len(first["cards"]) == 8, (
            f"[{path}] 第一帧到手了，面板上却不是那 8 轮（实际 {len(first['cards'])} 张）："
            f"{first['cards']}"
        )
        assert "分析已结束" not in first["note"], path
        assert "没有留下逐轮记录" not in first["note"], (
            f"[{path}] 面板还挂着 `unwatch()` 留下的那句 —— 用户读到的就是"
            "「这次运行没有过程可看」"
        )


def test_the_drawer_stays_on_the_think_tab_while_the_run_is_going(resume):
    """抽屉也要停在「思考过程」上（正在跑的时候唯一有内容的就是它）。

    被掐掉时 `stopAiBudgetWatch()` 会走 `AiDrawerTabs.markSettled()`，把抽屉切回
    「完整结论」—— 用户重开抽屉看到的是上一次的结论页，过程一栏是空的。
    """
    for path in _ENTRIES:
        first = _entry(resume, path)["afterFirstTick"]

        assert first["tab"] == "think", f"[{path}] 重开抽屉之后停在「完整结论」上了"
        assert first["thinkHidden"] is False, path


def test_the_rounds_are_labelled_and_ordered_by_the_family_rule(resume):
    """排序与分片标签：**这就是「乱序」那一条**。

    真跑过的窗口是 `[S5 r8, 汇总 r1..r4, V1 r1..r3]`，而实时那份原先**不带家族位次**
    （`agent_index`/`agent_total`），于是：
      * 汇总那几轮 `agent` 为空 → 卡片上**一个字都没有**，紧跟在「分片 S5 · 第 8 轮」
        下面写着「第 1 轮」—— 编号看着往回跳；
      * 分片那几轮只能说「分片 S5」（写不出 `(5/7)`）。
    两条合起来就是用户读到的「分片信息的排序没有按预期顺序规则，是乱序」。

    预期规则（依据都在仓库里，不是这里推断的）：
      * 顺序 = 家族顺序：成员按 `agent_index`（`subagent.run_family` 的 docstring
        「顺序跑 N 个成员 + 1 次汇总」）→ 成员内轮次升序（`agent_round`）；
      * 每张卡要说清是哪个成员的第几轮，位次按 `ai_stream_status.agentText` 的口径写
        （「分片 S5 (5/7)」；汇总那一次靠 `agent_index === agent_total` 认出来）。
    """
    for path in _ENTRIES:
        heads = _entry(resume, path)["afterFirstTick"]["cards"]

        assert heads[0].startswith("分片 S5 (5/7) · 第 8/23 轮"), heads
        assert heads[1].startswith("分片 主代理 (6/7) · 第 1/23 轮"), (
            f"[{path}] 汇总那一轮没有分片标签（或位次不对）：{heads[1]!r} —— 它就是"
            "「第 8 轮下面写着第 1 轮」那一幕"
        )
        assert heads[4].startswith("分片 主代理 (6/7) · 第 4/23 轮"), heads
        assert heads[5].startswith("分片 V1 (7/7) · 第 1/23 轮"), heads
        assert heads[7].startswith("分片 V1 (7/7) · 第 3/23 轮"), heads
        # 家族顺序：位次单调不减，同一个成员内轮次严格递增。
        positions = [head.split(" (")[1].split("/")[0] for head in heads]
        assert positions == sorted(positions), f"[{path}] 成员位次不是单调递增的：{positions}"


def test_the_panel_says_when_the_list_is_only_the_last_few_rounds(resume):
    """实时那份只有**最近 8 轮**（`run_progress.MAX_LIVE_ROUNDS`），必须说出来。

    不说的话列表看起来就是「从第 3 轮开始」—— 那同样被读成「顺序不对」。
    这句话也是 `run_progress` 模块 docstring 自己的承诺（「超出时如实标
    `rounds_truncated`」），而渲染器一直没读那个键。
    """
    for path in _ENTRIES:
        more = _entry(resume, path)["afterFirstTick"]["moreNote"]

        assert more, f"[{path}] 一共 34 轮、只列了 8 条，面板上一个字都没说"
        assert "34" in more and "8" in more, more


def test_it_keeps_polling_after_the_first_frame(resume):
    """第二帧也得有人在发：第一帧到手之后轮询必须**注册着**下一次（3 秒后那次）。

    只修「第一帧」是不够的：`stop()` 之后 `tick` 自己也不再排下一次，面板会停在第一帧上
    —— 过程就再也不会长了。
    """
    for path in _ENTRIES:
        assert _entry(resume, path)["pendingTimers"] >= 1, (
            f"[{path}] 第一帧之后没有下一次轮询了（它被 stop 掉了）"
        )


def test_attaching_to_a_run_we_cannot_read_does_not_claim_it_just_started(resume):
    """附着上去时读不到进度：**不许说「才刚发起、第一帧还没出来」**。

    多进程部署里分析派给别的 worker 执行，本进程的快照**永远**是 `null` —— 那句
    「分析已发起，还在准备」在那里一次都不会成真，而用户读到的是「我的点击刚刚生效」
    或「分析刚重启」，很可能再点一次「重新分析」（多花一次钱）。

    「本页看着它开跑」这句话的资格来自 `AiThinkLog.watch()`，而附着这条路会调它 ——
    所以必须在它之后撤掉那个时刻（`markExternalRun`），与 `/latest` 的「进行中」那一支
    同一口径。`ai_drawer_stream_state` 里那条静态守卫钉的是**那一支**，这一条钉的是
    附着这条路。
    """
    entry = resume["unreadable"]

    assert entry["afterFirstTick"]["think"]["mode"] == "unavailable", (
        f"读不到进度却说 {entry['afterFirstTick']['think']['mode']!r}："
        f"{entry['afterFirstTick']['note']!r}"
    )
    assert "还在准备" not in entry["afterFirstTick"]["note"]
    assert "跑在别的进程" in entry["afterFirstTick"]["note"], entry["afterFirstTick"]["note"]
