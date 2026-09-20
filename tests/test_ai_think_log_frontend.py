# -*- coding: utf-8 -*-
"""「思考过程」面板 —— 用 node 真跑 `static/js/ai_think_log.js`。

## 为什么必须真跑

这个面板最要紧的东西不是排版，而是**顶上那句话与列表是否对得上**：

* 正在跑 → 必须说「按轮取数、每跑完一轮多一条」——**不能**让人以为是逐字输出
  （平台的模型调用是非流的，这是它最容易让人误解的地方）；
* 跑完了 → 那句话要换成「已结束」，同时**已经画出来的那几轮不许被清掉**
  （用户明确要的：跑完之后仍然看得到本次的逐轮过程）；
* 读不到进度（别的进程在跑 / 快照过期）→ 说读不到，**不显示 0、也不假装「还没跑完
  第一轮」**；
* 刚发起、第一帧进度还没出来 → 说「正在准备」，**不许诊断成「跑在别的进程」**
  （载荷里 `progress` 为 null 的两种处境形状一样、含义相反，见下面的「刚发起」一节）；
* 这个目标压根没跑过 → 说没跑过（与「读不到」是两件事）。

这些都是「同一个面板的五种处境」各自一句不同的话，而错误的样子是**沉默地说错**
（面板上照样有字，只是那句话与事实不符）——静态断言看不出来，所以按仓库既有做法
（`tests/test_ai_context_notice.py`）把真函数放进带假 DOM 的 node 沙箱里跑。

## 两种来源一种形状

实时那一份（进度快照）与落库那一份（`/runs/<id>/usage`）的键逐字相同（服务端由
`trace_evidence.live_round_entry` 与 `run_usage` 保证，`tests/test_ai_live_thinking_snapshot.py`
钉着）。这里也据此**用同一批断言**跑两种来源的同样数据 —— 两边画出来的东西必须一致。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_think_log.js"
USAGE_LINE = PROJECT_ROOT / "static" / "js" / "ai_usage_line.js"

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

function makeEl(id) {
    var node = { id: id, className: '', hidden: false, children: [], _attrs: {} };
    var text = '';
    Object.defineProperty(node, 'textContent', {
        get: function () { return text; },
        set: function (v) {
            text = v === null || v === undefined ? '' : String(v);
            // 真 DOM 里 textContent='' 会把子节点清掉 —— 渲染器靠这个重画。
            if (text === '') node.children.length = 0;
        }
    });
    node.setAttribute = function (k, v) { node._attrs[k] = String(v); };
    node.getAttribute = function (k) {
        return node._attrs[k] === undefined ? null : node._attrs[k];
    };
    node.appendChild = function (child) { node.children.push(child); return child; };
    // 监听器**要记下来**：面板靠 `toggle` 记住「用户展开了哪一轮」，而 `toggle` 是浏览器
    // 在用户点 `<details>` 时抛的。假 DOM 不实现它，所以由用例显式抛（见 `fire`），
    // 否则「展开后重画仍展开」这条根本没法验。
    node._listeners = {};
    node.addEventListener = function (type, fn) {
        (node._listeners[type] = node._listeners[type] || []).push(fn);
    };
    node.classList = { add: function () {}, remove: function () {}, contains: function () { return false; } };
    return node;
}

var els = {};
var sandbox = {
    window: {}, console: console,
    document: {
        readyState: 'complete',
        getElementById: function (id) { return els[id] || null; },
        createElement: function (tag) { return makeEl(tag); },
        addEventListener: function () {}
    }
};
// 时钟：`starting` 那句说明**有期限**（"才刚发起"只在一段时间内成立），所以要能把它
// 推过去。只替 `Date.now`（模块读的就是它），其余时间能力保持真的。
var CLOCK = 1700000000000;
var HostDate = Date;
function SandboxDate() { return new HostDate(CLOCK); }
SandboxDate.now = function () { return CLOCK; };
sandbox.Date = SandboxDate;
function advance(ms) { CLOCK += ms; }
sandbox.window = sandbox;
vm.createContext(sandbox);
// 真加载「数字口径」那个模块：token 的 k / M 写法只有它一处实现，替身会掩盖分叉。
vm.runInContext(fs.readFileSync(__USAGE_LINE__, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiThinkLog;

var NOTE_ID = 'aiThinkNote';
var LOG_ID = 'aiThinkLog';

function seed() {
    els = {};
    els[NOTE_ID] = makeEl(NOTE_ID);
    els[LOG_ID] = makeEl(LOG_ID);
}

function dumpNode(node) {
    return {
        cls: node.className,
        text: node.getAttribute('data-round-key') === null
            ? node.textContent : node.textContent,
        key: node.getAttribute('data-round-key'),
        // `open` 只在「模型这一轮返回的内容」那个 `<details>` 上有意义，其余节点恒为
        // false。断言看的就是它。
        open: node.open === true,
        children: node.children.map(dumpNode)
    };
}

/** 在当前这棵树里按 class 找第一个节点（假 DOM 没有 querySelector，自己走一遍）。 */
function findByClass(node, cls) {
    if (node.className === cls) return node;
    for (var i = 0; i < node.children.length; i++) {
        var hit = findByClass(node.children[i], cls);
        if (hit) return hit;
    }
    return null;
}

function snap() {
    return {
        note: els[NOTE_ID].textContent,
        mode: api.state().mode,
        watching: api.state().watching,
        rounds: els[LOG_ID].children.map(dumpNode)
    };
}

var PROGRESS = __PROGRESS__;
var ROUNDS = __ROUNDS__;

var OP = {
    watch: function () { api.watch(null); },
    watchWithRun: function () { api.watch(7); },
    unwatch: function () { api.unwatch(); },
    setRun: function () { api.setRun(7); },
    clearRun: function () { api.setRun(null); },
    markEmpty: function () { api.markEmpty(); },
    markExternalRun: function () { api.markExternalRun(); },
    reset: function () { api.reset(); },
    progressTwo: function () { api.applyProgress(PROGRESS.two); },
    progressEmpty: function () { api.applyProgress(PROGRESS.empty); },
    progressMissing: function () { api.applyProgress(null); },
    // 同一帧（没有进度）带上不同的运行状态 —— 「才刚发起」与「读不到」就是靠它分辨的。
    progressMissingRunning: function () { api.applyProgress(null, 'running'); },
    progressMissingPending: function () { api.applyProgress(null, 'pending'); },
    progressMissingDone: function () { api.applyProgress(null, 'succeeded'); },
    progressTwoRunning: function () { api.applyProgress(PROGRESS.two, 'running'); },
    // 把时钟推过 `STARTING_WINDOW_MS`。**故意推得远远超过任何合理的期限**：
    // 这条只验「过了期限就改口」，不验期限具体是多少。
    runLong: function () { advance(3600000); },
    roundsStored: function () { api.applyRounds(ROUNDS.stored, {}); },
    roundsSame: function () { api.applyRounds(ROUNDS.sameAsLive, {}); },
    roundsEmpty: function () { api.applyRounds([], {}); },
    // 用户点开「模型这一轮返回的内容」。
    //
    // 浏览器点一下 summary 做两件事：**同步**把 `open` 翻成 true，另**异步**派发一个
    // `toggle` 事件。被测代码只认前者（见 `captureExpanded`：靠 `toggle` 会在真浏览器里
    // 失效，因为重画等不到那个异步事件），所以这里也只做前者 —— 把 `toggle` 也抛出来
    // 反而是**替被测代码假设了一个它其实用不上的机制**，会掩盖「它其实依赖了异步事件」
    // 这类错误。
    openModel: function () {
        findByClass(els[LOG_ID], 'ai-think-model').open = true;
    },
    // 用户又把它收起来。收起之后重画**不该**再自动展开（否则就是跟用户对着干）。
    closeModel: function () {
        findByClass(els[LOG_ID], 'ai-think-model').open = false;
    }
};

var cases = __CASES__;
var results = cases.map(function (item) {
    CLOCK = 1700000000000;   // 每个用例都从同一个时刻开始（上一条推过时钟）
    seed();
    vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiThinkLog;
    var snaps = item.ops.map(function (op) { OP[op](); return snap(); });
    return { name: item.name, snaps: snaps };
});

// 懒加载：切到「思考过程」时按运行号去取落库的逐轮（只取一次；看着跑的期间不取）。
//
// 几个场景**顺序跑**（每个场景重新加载一遍模块、重新造一遍 DOM）：它们都是异步的，
// 并行跑会共用同一份 `els` 与同一个模块单例，一个场景的回调会写进另一个场景的面板里
// ——那是测试自己的串扰，不是被测代码的问题。
function reload() {
    seed();
    // 场景里给沙箱塞过 `fetch`（模拟真页面的 `window.fetch`），场景之间要清干净
    // —— 留着的话别的场景会去发一个它以为自己没发的请求。
    sandbox.fetch = undefined;
    vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiThinkLog;
}

function tick() { return new Promise(function (resolve) { setTimeout(resolve, 0); }); }

// 跑动中的真实答案：逐轮是**跑完才落库**的（服务端 `_persist_outcome`），所以这一刻
// `/usage` 必然回空表。
function emptyFetch(url) {
    return Promise.resolve({
        ok: true, status: 200,
        json: function () { return Promise.resolve({ success: true, rounds: [] }); }
    }).then(function (response) { lastUrl.push(url); return response; });
}

function okFetch(url) {
    return Promise.resolve({
        ok: true, status: 200,
        json: function () { return Promise.resolve({ success: true, rounds: ROUNDS.stored }); }
    }).then(function (response) { lastUrl.push(url); return response; });
}

var lastUrl = [];

async function lazy() {
    var out = {};

    // 1) 没有运行号：不去取（一个不知道取什么的请求）。
    reload();
    lastUrl = [];
    api.ensureLoaded(okFetch);
    out.noRun = { calls: lastUrl.slice(), mode: api.state().mode };

    // 2) 看着跑的时候：不去取（落库那份与实时那份是同一件事，取了只会来回换来源）。
    reload();
    lastUrl = [];
    api.watch(7);
    api.ensureLoaded(okFetch);
    out.whileWatching = { calls: lastUrl.slice(), mode: api.state().mode };

    // 3) 跑完了、切到「思考过程」：取一次，取到就不再取。
    reload();
    lastUrl = [];
    api.watch(7);
    api.unwatch({ settled: true });
    api.ensureLoaded(okFetch);
    out.beforeLoad = { calls: lastUrl.slice(), note: els[NOTE_ID].textContent };
    await tick();
    out.afterLoad = {
        calls: lastUrl.slice(),
        mode: api.state().mode,
        note: els[NOTE_ID].textContent,
        rounds: els[LOG_ID].children.length
    };
    api.ensureLoaded(okFetch);
    out.afterLoad.callsAfterSecond = lastUrl.length;

    // 4) 取不到：如实说，不留「正在读取」那种半截话。
    reload();
    var failed = [];
    api.setRun(7);
    api.ensureLoaded(function (url) {
        failed.push(url);
        return Promise.resolve({
            ok: false, status: 404,
            json: function () { return Promise.resolve({ success: false, message: 'Not found.' }); }
        });
    });
    await tick();
    out.failure = {
        mode: api.state().mode, note: els[NOTE_ID].textContent, calls: failed.slice()
    };

    // 5) **最后一帧读不到进度、然后跑完**：取数必须由 `unwatch` 自己发起。
    //
    // 不发起的话面板会停在那句承诺上（「跑完之后这里会显示落库的逐轮记录」）永远不动
    // —— `ensureLoaded` 挂在「切到思考过程」这个动作上，而用户一直停在这个标签页
    // （他手动点过标签，跑完不会被自动切走）就永远等不到。
    reload();
    lastUrl = [];
    api.watch(7);
    api.applyProgress(null);
    out.stuckBefore = { mode: api.state().mode, note: els[NOTE_ID].textContent };
    sandbox.fetch = okFetch;   // 真页面里这就是 window.fetch
    api.unwatch({ settled: true });
    // `ensureLoaded` 是**同步**进到「正在读取」的（取数本身是异步的），所以这一刻的
    // `mode` 就是「`unwatch` 自己发起了取数」的证据。
    out.stuckAfter = { mode: api.state().mode, note: els[NOTE_ID].textContent };
    await tick();
    out.stuckLoaded = {
        mode: api.state().mode,
        note: els[NOTE_ID].textContent,
        calls: lastUrl.slice(),
        rounds: els[LOG_ID].children.length
    };
    sandbox.fetch = undefined;

    // 6) 取的过程中换了运行号（跑完 → 用户立刻又点了「重新分析」）：**那份响应要丢掉**
    //    —— 画上去就是「这一次的运行里显示着上一次的逐轮过程」。
    reload();
    var pending = null;
    api.setRun(7);
    api.ensureLoaded(function () {
        return new Promise(function (resolve) {
            pending = function () {
                resolve({
                    ok: true, status: 200,
                    json: function () {
                        return Promise.resolve({ success: true, rounds: ROUNDS.stored });
                    }
                });
            };
        });
    });
    api.setRun(8);
    pending();
    await tick();
    out.staleResponse = {
        runId: api.state().runId,
        mode: api.state().mode,
        rounds: els[LOG_ID].children.length,
        note: els[NOTE_ID].textContent
    };

    // 7) 取的过程中本页**又看着这次运行跑了**（实时那一帧把 `watching` 认回来），而且
    //    **取数失败**了：这份失败响应同样不许写面板。上面 `.then` 那条路早就有这道闸
    //    （第 6 条验的是它），`.catch` 里以前没有 —— 同一个理由，写上去就是把「读不到
    //    这次运行的逐轮记录：…」挂在一次**正跑着**的运行上，用户会以为过程丢了。
    //
    //    认回 `watching` 的是 `applyProgress`（不是 `watch`）**这一点是有意的**：它只认
    //    `watching`，不碰 `loading` —— 见下面那半段。
    reload();
    var boom = null;
    api.setRun(7);
    api.ensureLoaded(function () {
        return new Promise(function (_resolve, reject) {
            boom = function () { reject(new Error('boom')); };
        });
    });
    // 实时那一帧：跑起来了、还没跑完第一轮（`rounds` 为空，所以面板上一条轮次都没有）。
    api.applyProgress(PROGRESS.empty);
    boom();
    await tick();
    out.failureWhileWatching = {
        mode: api.state().mode,
        watching: api.state().watching,
        note: els[NOTE_ID].textContent
    };
    // 而且 `loading` 必须跟着收掉：早退时若不收，这个残留的标志位会把 `ensureLoaded`
    // 永久挡在门外 —— 跑完那一刻「这里会显示落库的逐轮记录」那句承诺就再也没人兑现
    // （第 5 条验的正是那句承诺，这是同一条纪律的另一面）。跑完这一刻 `unwatch` 自己
    // 发起取数，取得回来才算数。
    lastUrl = [];
    sandbox.fetch = okFetch;
    api.unwatch({ settled: true });
    await tick();
    out.failureWhileWatching.afterUnwatch = {
        calls: lastUrl.slice(),
        mode: api.state().mode,
        rounds: els[LOG_ID].children.length
    };
    sandbox.fetch = undefined;

    // 8) **跑动中关抽屉，等它跑完再打开**（这条以前会把「没有留下逐轮记录」写死）。
    //
    // 逐轮是跑完之后才落库的，跑动中取回的那一份必然是空表。把它当终态收下
    // （`loaded = true`）之后，这份记录就**再也刷不出来了**：跑完再打开抽屉时
    // `setRun` 会因为运行号没变而早退，`ensureLoaded` 又被 `loaded` 挡在门外 ——
    // 「思考过程」对这次运行永远说「这次运行没有留下逐轮记录」，而明细一直在库里。
    // 所以「跑完了」与「用户不想看了」必须分开：只有前者才去兑现那句承诺。
    reload();
    lastUrl = [];
    api.watch(7);
    sandbox.fetch = emptyFetch;      // 跑动中那一刻库里还没有逐轮
    api.unwatch();                   // ① 关抽屉：不带 settled → 不该去取
    await tick();
    out.closedMidRun = {
        calls: lastUrl.slice(), note: els[NOTE_ID].textContent
    };
    sandbox.fetch = okFetch;         // ② 跑完了：这一次才是该兑现承诺的时刻
    api.unwatch({ settled: true });
    await tick();
    out.closedThenFinished = {
        calls: lastUrl.slice(),
        mode: api.state().mode,
        rounds: els[LOG_ID].children.length,
        note: els[NOTE_ID].textContent
    };

    // 8) **跑动中点一次「思考过程」**：这一刻 `/usage` 必然回空表（逐轮跑完才落库），
    //    而这份空表**不许被当成终态收下**。收下的后果：跑完那一刻的
    //    `unwatch({settled: true})` 被 `!loaded` 挡在门外、用户再点标签又被同一个标志
    //    挡住、`/latest` 那句 `setRun` 还会因为运行号没变而早退 —— 面板于是**永远**
    //    写着「这次运行没有留下逐轮记录」，而明细一直在库里。
    //    （`unwatch` 的 docstring 已经为另一扇门（关抽屉）想明白了这件事，
    //     `onShowThink` 这一扇当时没关。）
    reload();
    lastUrl = [];
    api.watch(7);
    // 多节点部署里每一帧的 `progress` 都是 null —— 正是这一帧把 `watching` 打掉，
    // `ensureLoaded` 才会真的去取（`watching` 为真时它按纪律早退）。
    api.applyProgress(null, 'running');
    api.ensureLoaded(emptyFetch);
    await tick();
    out.emptyWhileRunning = {
        mode: api.state().mode,
        note: els[NOTE_ID].textContent,
        rounds: els[LOG_ID].children.length
    };
    // 跑完了 → 同一扇门再走一次，这次取回来的就是真的（上面那次不许把它挡在门外）。
    lastUrl = [];
    sandbox.fetch = okFetch;
    api.unwatch({ settled: true });
    await tick();
    out.emptyWhileRunning.afterFinish = {
        calls: lastUrl.slice(),
        mode: api.state().mode,
        rounds: els[LOG_ID].children.length
    };
    sandbox.fetch = undefined;

    return out;
}

lazy().then(function (out) {
    process.stdout.write(JSON.stringify({
        cases: results,
        noRun: out.noRun,
        whileWatching: out.whileWatching,
        beforeLoad: out.beforeLoad,
        afterLoad: out.afterLoad,
        failure: out.failure,
        stuckBefore: out.stuckBefore,
        stuckAfter: out.stuckAfter,
        stuckLoaded: out.stuckLoaded,
        staleResponse: out.staleResponse,
        failureWhileWatching: out.failureWhileWatching,
        emptyWhileRunning: out.emptyWhileRunning,
        closedMidRun: out.closedMidRun,
        closedThenFinished: out.closedThenFinished,
        notes: api.NOTE
    }));
});
"""


def _rounds() -> dict:
    """两种来源共用的那几轮（形状就是服务端 `live_round_entry` / `run_usage` 的那一份）。"""
    return {
        "stored": [
            {
                "round_index": 1, "agent": "", "agent_round": 1, "outcome": "requests",
                "parsed_ok": True, "tokens_input": 12000, "tokens_output": 800,
                "cache_read_tokens": None, "cache_write_tokens": None,
                "request_chars": 40000, "context_chars": 9000, "duration_ms": 18400,
                "error": "",
                "requests": [{"type": "file_diff", "path": "build/lua/CfgItem.lua",
                              "text": "代码差异 build/lua/CfgItem.lua"}],
                "executed": [
                    {"kind": "file_diff", "label": "代码差异 build/lua/CfgItem.lua",
                     "chars": 2000, "failed": False, "empty": False, "reason": "",
                     "truncated": False},
                    {"kind": "file_content", "label": "正文 build/lua/CfgItem.lua",
                     "chars": 0, "failed": True, "empty": False,
                     "reason": "平台读不到这个文件的内容", "truncated": False},
                    {"kind": "file_content", "label": "正文 config/item.xlsx",
                     "chars": 0, "failed": False, "empty": True, "reason": "",
                     "truncated": False},
                ],
                "dropped": [
                    {"kind": "request", "reason": "超出本次工具请求总预算（20 次），未执行",
                     "detail": "file_diff x"},
                    # 「未归类」：模型写了不在本次维度清单里的 category。平台把它**保留**
                    # 下来归进「未归类」（`protocol._coerce_anomalies`），记账原文是
                    # 「未丢弃，已归入「未归类」」—— 面板不能把它说成「未执行」。
                    {"kind": "unclassified",
                     "reason": "category 不在本次生效的维度清单内（未丢弃，已归入「未归类」）",
                     "detail": "performance"},
                ],
                "response_text": '{"status": "need_more_context", "reason": "先看战斗逻辑"}',
                "budget_notes": "有 1 个上下文请求因超出本次索取额度而未执行。",
                "correction_hint": "",
            },
            {
                "round_index": 2, "agent": "S1", "agent_round": 1, "outcome": "final",
                "parsed_ok": True, "tokens_input": 9000, "tokens_output": 1500,
                "cache_read_tokens": None, "cache_write_tokens": None,
                "request_chars": 20000, "context_chars": 12000, "duration_ms": 9000,
                "error": "", "requests": [], "executed": [], "dropped": [],
                "response_text": "# 变更理解\n\n整份报告正文",
                "budget_notes": "", "correction_hint": "",
            },
        ],
    }


@pytest.fixture(scope="module")
def run() -> dict:
    rounds = _rounds()
    same = json.loads(json.dumps(rounds["stored"]))
    cases = [
        # 1. 页面刚打开，还没跑过 → 「这个目标还没有跑过分析」。
        {"name": "没有运行过", "ops": ["markEmpty"]},
        # 2. 开跑：还没有第一轮。
        {"name": "开跑还没第一轮", "ops": ["watch", "progressEmpty"]},
        # 3. 跑了两个轮次：每轮一张卡。
        {"name": "跑动中两轮", "ops": ["watchWithRun", "progressTwo"]},
        # 4. 跑完了：那句话换成「已结束」，但列表**不动**。
        {"name": "跑完后保留列表", "ops": ["watchWithRun", "progressTwo", "unwatch"]},
        # 5. 读不到进度（别的进程在跑 / 快照过期）。
        {"name": "读不到进度", "ops": ["watchWithRun", "progressTwo", "progressMissing"]},
        # 6. 刷新页面时目标已经在跑，但本页看不到进度。
        {"name": "别处在跑", "ops": ["setRun", "markExternalRun"]},
        # 7. 落库那一份（跑完之后切到「思考过程」）。
        {"name": "落库的逐轮", "ops": ["setRun", "roundsStored"]},
        # 8. 换了运行号：上一次的列表要清掉（留着会被读成这一次的过程）。
        {"name": "换运行清列表", "ops": ["watchWithRun", "progressTwo", "clearRun"]},
        # 9. 这次运行一条逐轮都没有（老数据）。
        {"name": "没有逐轮记录", "ops": ["setRun", "roundsEmpty"]},
        # 10. 正在看着跑的时候，本页不该被打成「读不到」。
        {"name": "在跑时别处状态不改", "ops": ["watchWithRun", "markExternalRun"]},
        # 10b. **跑动中夹了一帧读不到的载荷之后**仍然如此：点「刷新结果」走的就是
        #      「进行中」那一支，那一下不该把已经画出来的过程抹掉。
        {
            "name": "读不到一帧后刷新不抹掉过程",
            "ops": ["watchWithRun", "progressTwo", "progressMissing", "markExternalRun"],
        },
        {
            "name": "读不到之后又来一帧真进度",
            "ops": ["watchWithRun", "progressMissing", "progressTwo", "markExternalRun"],
        },
        # 11. 关抽屉：不再看着它跑，但已经画出来的留着。
        {"name": "关抽屉不清列表", "ops": ["watchWithRun", "progressTwo", "reset"]},
        # 10c. **刷新页面 / 点「刷新结果」时发现它已经在跑**：模板那条路是
        #      `startAiBudgetWatch()`（内部调 `watch()`）紧跟一句 `markExternalRun()`。
        #      `watch()` 把「本页看着它开跑」的时刻记成**现在**，于是「才刚发起」的三个
        #      条件全被满足 —— 一个已经跑了十分钟的分析被说成「第一帧还没出来」。
        #      真实调用序列（模板 `loadWeeklyAiLatest` 的「进行中」那一支）。
        {
            "name": "刷新时它已经在跑",
            "ops": ["watchWithRun", "markExternalRun", "progressMissingRunning"],
        },
        # 10d. 反过来：**本页确实是发起方**（SSE 的 run 事件那条路，只调 watch，不调
        #      markExternalRun）—— 这句话是真话，不许被改掉。
        {"name": "本页看着它开跑", "ops": ["watchWithRun", "progressMissingRunning"]},
        # 11b. **SSE 收到终态**（result / error 事件）走的是 `stopAiBudgetWatch()` →
        #      `unwatch()`（**不带** settled）。这一刻本页不再看着它跑了，那句「还在准备：
        #      第一帧还没出来…每跑完一轮这里会多一条」从此不成立 —— 而它之后没有任何
        #      东西会重画（120 秒的期限只在 `paint()` 里判），会一直挂着：徽章已经写
        #      「完成」、报告已经在「完整结论」里，同一个抽屉的「思考过程」还在说「还在
        #      准备」。必须降级成同一处境里**更弱**的那句。
        {
            "name": "跑完时停在还没第一帧",
            "ops": ["watchWithRun", "progressMissingRunning", "unwatch"],
        },
        # 12. **刚发起、第一帧进度还没出来**：引擎跑完第一轮才第一次 publish，中间这段
        #     载荷里 `progress` 为 null —— 与「跑在别的进程」逐字相同。用户点完
        #     「重新分析」立刻看到「读不到（跑在别的进程）」，那句诊断是凭空来的。
        {"name": "刚发起还没第一帧", "ops": ["watchWithRun", "progressMissingRunning"]},
        # 12b. 连着两帧都没有进度：还在那一段里，**不许第二帧就改口**
        #      （第一帧会把 `watching` 打掉，只看它的话这里就变成「读不到」了）。
        {
            "name": "刚发起连两帧",
            "ops": ["watchWithRun", "progressMissingRunning", "progressMissingRunning"],
        },
        {"name": "刚发起是排队中", "ops": ["watchWithRun", "progressMissingPending"]},
        # 12c. 期限过了就改口：跑在别的进程时**永远**不会有快照，一直说「正在准备」
        #      等于把一件不会发生的事说成马上要发生。
        {
            "name": "发起太久改口",
            "ops": ["watchWithRun", "runLong", "progressMissingRunning"],
        },
        # 12d. 终态不该再等等看：都跑完了还读不到，就是读不到。
        {"name": "跑完还读不到", "ops": ["watchWithRun", "progressMissingDone"]},
        # 12e. 见过快照之后又丢了 = 「读不到最新的」，不是「还没出来」。
        {
            "name": "见过快照后丢失",
            "ops": ["watchWithRun", "progressTwoRunning", "progressMissingRunning"],
        },
        # 12f. 调用方没带运行状态 → 一律按「读不到」。拿不到状态就不能断言它还在跑。
        {"name": "没给运行状态", "ops": ["watchWithRun", "progressMissingRunning",
                                     "progressMissing"]},
        # 12g. 本页没看着它开跑（刷新时它已经在跑）→ 没有资格说「才刚发起」。
        {
            "name": "刷新时已在跑",
            "ops": ["setRun", "markExternalRun", "progressMissingRunning"],
        },
        # 13a. **展开「模型这一轮返回的内容」之后，下一帧重画必须还是展开的。**
        # 跑动中每来一帧进度就重建一次子树，而 `open` 是 DOM 属性 —— 不记状态的话
        # 新节点回到默认收起态，用户看到的就是「点开、过几秒自己收起来」，读不了。
        {
            "name": "展开模型输出后重画仍展开",
            "ops": ["watchWithRun", "progressTwoRunning", "openModel",
                    "progressTwoRunning"],
        },
        # 13b. 反方向：用户收起来之后，重画**不许**又替他展开。
        {
            "name": "收起模型输出后重画不自动展开",
            "ops": ["watchWithRun", "progressTwoRunning", "openModel",
                    "progressTwoRunning", "closeModel", "progressTwoRunning"],
        },
        # 13c. 换一次运行不许继承上一次的展开状态：键是「轮次号 + 分片」，两次运行里重名。
        {
            "name": "换运行不继承展开状态",
            "ops": ["watchWithRun", "progressTwoRunning", "openModel",
                    "clearRun", "setRun", "progressTwoRunning"],
        },
    ]
    return _drive(cases, rounds, same)


def _drive(cases: list, rounds: dict, same: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的思考面板断言")
    progress = {
        "empty": {"round": 0, "max_rounds": 8, "rounds": [], "rounds_seen": 0},
        "two": {"round": 2, "max_rounds": 8, "rounds": rounds["stored"],
                "rounds_seen": 2, "rounds_truncated": False},
    }
    driver = (
        DRIVER.replace("__SCRIPT__", json.dumps(str(SCRIPT)))
        .replace("__USAGE_LINE__", json.dumps(str(USAGE_LINE)))
        .replace("__CASES__", json.dumps(cases, ensure_ascii=False))
        .replace("__PROGRESS__", json.dumps(progress, ensure_ascii=False))
        .replace("__ROUNDS__", json.dumps(
            {"stored": rounds["stored"], "sameAsLive": same}, ensure_ascii=False))
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _by_name(run: dict) -> dict:
    return {item["name"]: item for item in run["cases"]}


def _texts(node: dict, out: list) -> list:
    """把一棵卡片树里所有非空文本按顺序摊平（断言文案用）。"""
    if node.get("text"):
        out.append(node["text"])
    for child in node.get("children", []):
        _texts(child, out)
    return out


def _flat(snap: dict) -> list:
    out = []
    for card in snap["rounds"]:
        _texts(card, out)
    return out


# --------------------------------------------------------------------------
# 顶上那句话：五种处境五句话
# --------------------------------------------------------------------------


def test_a_target_that_never_ran_says_so(run):
    last = _by_name(run)["没有运行过"]["snaps"][-1]

    assert last["mode"] == "empty"
    assert last["note"] == "这个目标还没有跑过分析。"


def test_a_run_without_rounds_yet_is_not_the_same_as_unreadable(run):
    """「还没跑到第一轮」与「读不到进度」是两件事 —— 混起来就是撒谎。"""
    no_rounds = _by_name(run)["开跑还没第一轮"]["snaps"][-1]
    unreadable = _by_name(run)["读不到进度"]["snaps"][-1]

    assert "还没有跑完第一轮" in no_rounds["note"]
    assert unreadable["note"] != no_rounds["note"]
    assert "读不到" in unreadable["note"]
    assert unreadable["mode"] == "unavailable"


def test_the_live_note_states_plainly_that_it_is_per_round(run):
    """**最要紧的一句**：平台按轮取数、不是逐字输出。它必须逐字出现在面板上。"""
    note = _by_name(run)["跑动中两轮"]["snaps"][-1]["note"]

    assert "按轮取数" in note
    assert "不是逐字输出" in note


def test_the_note_switches_when_the_run_ends(run):
    snaps = _by_name(run)["跑完后保留列表"]["snaps"]

    assert "分析进行中" in snaps[-2]["note"]
    assert "分析已结束" in snaps[-1]["note"]
    assert snaps[-1]["rounds"] == snaps[-2]["rounds"], (
        "跑完只是换一句话，已经画出来的那几轮必须原样留着（用户明确要的）"
    )


def test_a_frame_that_cannot_be_read_does_not_wipe_what_we_already_have(run):
    """**已经画出来的那几轮不许被一次「读不到」抹掉。**

    它们是这一次运行真实跑过的轮次（换运行号才会清），下一帧往往就恢复正常了 ——
    清掉的表现是过程**闪一下没了**。而说的话也要跟着换：手上还有几轮时说的是
    「下面是已经拿到的逐轮记录」，不是「读不到进度」（那会读成「什么都没有」）。
    """
    snaps = _by_name(run)["读不到一帧后刷新不抹掉过程"]["snaps"]

    assert snaps[1]["rounds"], "进度那一帧本来有货"
    assert snaps[2]["rounds"] == snaps[1]["rounds"], "读不到的那一帧不该把它清掉"
    assert snaps[2]["mode"] == "unavailable"
    assert "已经拿到" in snaps[2]["note"], "手上有几轮时不能只说「读不到」"


def test_a_refresh_mid_run_does_not_wipe_the_rounds(run):
    """跑动中点一次「刷新结果」走的是「进行中」那一支，那一下不该让面板变空。"""
    snaps = _by_name(run)["读不到一帧后刷新不抹掉过程"]["snaps"]

    assert snaps[-1]["rounds"] == snaps[1]["rounds"]
    assert snaps[-1]["mode"] == "unavailable", "读不到就说读不到，别假装还在跑"


def test_a_real_frame_after_an_unreadable_one_re_arms_watching(run):
    """来了一帧真进度 = 本页确实连在这一次运行上 —— `watching` 要跟着回来。

    否则 `ensureLoaded` 会在跑动中去取落库的明细，把实时的过程换成「分析已结束」。
    """
    last = _by_name(run)["读不到之后又来一帧真进度"]["snaps"][-1]

    assert last["watching"] is True
    assert last["mode"] == "live"
    assert len(last["rounds"]) == 2


def test_a_run_elsewhere_says_it_cannot_be_read(run):
    last = _by_name(run)["别处在跑"]["snaps"][-1]

    assert last["mode"] == "unavailable"
    assert "读不到" in last["note"]


def test_watching_beats_the_elsewhere_marker(run):
    """本页正看着它跑时，「别处在跑」不该把 live 打成「读不到」。"""
    last = _by_name(run)["在跑时别处状态不改"]["snaps"][-1]

    assert last["mode"] == "live"


def test_refreshing_into_a_running_analysis_is_not_a_just_started_run(run):
    """**刷新页面时它已经在跑** —— 本页没有资格说「才刚发起，第一帧还没出来」。

    模板那条路是 `startAiBudgetWatch()`（内部调 `AiThinkLog.watch()`）紧跟一句
    `markExternalRun()`。`watch()` 会把「本页看着它开跑」的时刻记成**现在**，于是
    `startingNow` 的三个条件全被满足 —— 一个已经跑了十分钟的分析被说成「第一帧逐轮
    进度还没出来」，用户会以为自己的点击没生效或分析刚重启，很可能再点一次
    「重新分析」（多花一次钱）。这正是 a14d157 那句「刷新页面时它已经在跑的话，本页
    不知道它刚发起还是已经跑了十分钟，那就没有资格说这句话」要防的。
    """
    last = _by_name(run)["刷新时它已经在跑"]["snaps"][-1]

    assert last["mode"] == "unavailable"
    assert "跑在别的进程" in last["note"]


def test_a_run_this_page_started_keeps_the_just_started_note(run):
    """反向自检：**本页确实是发起方**时（SSE 的 run 事件那条路只调 `watch`），
    那句话是真话 —— 撤掉它就是把一句真话改坏。"""
    last = _by_name(run)["本页看着它开跑"]["snaps"][-1]

    assert last["mode"] == "starting"
    assert last["note"] == run["notes"]["starting"]


def test_an_unclassified_item_is_not_reported_as_not_executed(run):
    """「未归类」被**保留**着，不是「没执行」。

    平台把「模型给了不在本次维度清单里的 category」的条目留下来、归进「未归类」
    （`protocol._coerce_anomalies`），记账原文是「未丢弃，已归入「未归类」」。前缀写成
    「未执行」之后，那一行读作「未执行：performance（未丢弃，已归入「未归类」）」——
    一句自相矛盾的话。而用户翻「思考过程」正是为了查「为什么这次只报了两条」，
    「未执行」会让他以为请求没跑。
    """
    lines = _flat(_by_name(run)["落库的逐轮"]["snaps"][-1])

    unclassified = [line for line in lines if "未归类" in line]
    assert unclassified, "归进「未归类」的那一条在面板上一个字都没有"
    assert not any(line.startswith("未执行：performance") for line in lines), (
        "「未归类」是保留下来的内容，不是「没执行」"
    )
    assert any(line.startswith("未执行：file_diff x") for line in lines), (
        "反向自检：真正没执行的那一条仍然要说「未执行」"
    )


def test_an_empty_fetch_mid_run_is_not_a_final_answer(run):
    """**跑动中点一次「思考过程」不该把明细永久写死。**

    这一刻 `/usage` 回空表是**必然**的（逐轮跑完才落库，服务端 `_persist_outcome`）。
    把它当终态收下（`loaded = true`）之后，跑完那一刻的 `unwatch({settled: true})` 被
    `!loaded` 挡在门外、用户再点标签又被同一个标志挡住、`/latest` 的 `setRun` 还会因为
    运行号没变而早退 —— 面板于是**永远**写着「这次运行没有留下逐轮记录」，而明细一直
    在库里。用户会把「本页读不到实时快照」读成「平台没留记录」。
    """
    mid = run["emptyWhileRunning"]

    assert mid["mode"] == "live", "还没落库 ≠ 已结束"
    assert "还没有跑完第一轮" in mid["note"]
    assert mid["rounds"] == 0
    # 跑完之后同一扇门再走一次 —— 这次必须取得到（上面那次没把它挡在门外）。
    after = mid["afterFinish"]
    assert after["calls"], "跑完之后本页再没有去取过落库的逐轮"
    assert after["mode"] == "settled"
    assert after["rounds"] > 0


def test_stopping_the_watch_retires_the_just_started_note(run):
    """SSE 收到终态走的是 `unwatch()`（**不带** settled）—— 那句「还在准备」从此不成立。

    它之后**没有任何东西会重画**（120 秒的期限只在 `paint()` 里判），于是会一直挂着：
    徽章已经写「完成」、报告已经在「完整结论」里，同一个抽屉的「思考过程」还在说
    「每跑完一轮这里会多一条」—— 两句话不能同时为真。
    """
    last = _by_name(run)["跑完时停在还没第一帧"]["snaps"][-1]

    assert last["mode"] == "unavailable"
    assert last["note"] == run["notes"]["unavailable"]


# --------------------------------------------------------------------------
# 刚发起那一段：不许诊断成「跑在别的进程」
# --------------------------------------------------------------------------


def test_a_just_started_run_is_not_diagnosed_as_running_elsewhere(run):
    """**用户报的就是这一条。**

    引擎跑完第一轮才第一次 `run_progress.publish`，所以从点下「重新分析」到第一轮跑完
    之间，载荷里 `progress` 为 null —— 与「跑在别的进程」逐字相同。原先这个处境说的是
    「读不到这次运行的逐轮进度（跑在别的进程，或快照已过期）」，而那一刻运行**就在
    眼前**刚发起来：诊断是凭空来的。

    所以这一段要单独一句话，而且**不带任何诊断**（「第一帧还没出来」是事实，
    「跑在别的进程」是猜测）。
    """
    last = _by_name(run)["刚发起还没第一帧"]["snaps"][-1]

    assert last["mode"] == "starting"
    assert last["note"] == run["notes"]["starting"]
    assert "跑在别的进程" not in last["note"], "这一刻没有任何依据说它跑在哪儿"


def test_the_just_started_note_survives_more_frames_without_progress(run):
    """连着两帧都没有进度时**不许改口**。

    第一帧读不到会把 `watching` 打掉，若判据只看它，第二帧就变成「读不到」——
    面板会在「正在准备」与「读不到」之间来回跳，而它其实一直在同一个处境里。
    """
    snaps = _by_name(run)["刚发起连两帧"]["snaps"]

    assert [item["mode"] for item in snaps[1:]] == ["starting", "starting"]


def test_the_just_started_note_is_used_when_the_run_is_only_queued(run):
    """排队中（还没被 worker 捡走）同样属于这一段。"""
    last = _by_name(run)["刚发起是排队中"]["snaps"][-1]

    assert last["mode"] == "starting"


def test_the_just_started_note_expires(run):
    """**这句话有期限。**

    多节点部署里分析派给 Agent 节点执行，本进程**永远**不会有快照 —— 一直说
    「正在准备」等于把一件不会发生的事说成马上就要发生。过了期限就改口说读不到。
    """
    last = _by_name(run)["发起太久改口"]["snaps"][-1]

    assert last["mode"] == "unavailable"
    assert "第一帧" not in last["note"]


def test_a_finished_run_is_never_called_just_started(run):
    """终态不该再等等看：都跑完了还读不到，那就是读不到。"""
    last = _by_name(run)["跑完还读不到"]["snaps"][-1]

    assert last["mode"] == "unavailable"


def test_a_snapshot_we_saw_and_lost_is_not_just_started(run):
    """见过快照又丢了 = 「读不到最新的」，不是「还没出来」——手上那几轮照样是真的。"""
    last = _by_name(run)["见过快照后丢失"]["snaps"][-1]

    assert last["mode"] == "unavailable"
    assert last["note"] == run["notes"]["unavailable_stale"]
    assert len(last["rounds"]) == 2, "已经拿到的轮次不能被抹掉"


def test_without_a_run_status_it_falls_back_to_unreadable(run):
    """调用方没带运行状态 → 一律按「读不到」。

    模块拿不到状态就不能断言这次运行还在跑，宁可说一个更弱的说法。这也让
    `applyProgress(progress)` 的旧调用行为**逐字不变**（少给一个参数不会让它变乐观）。
    """
    last = _by_name(run)["没给运行状态"]["snaps"][-1]

    assert last["mode"] == "unavailable"
    assert last["note"] == run["notes"]["unavailable"]


def test_a_page_that_did_not_watch_it_start_never_claims_it_just_did(run):
    """刷新页面时它已经在跑：本页不知道它刚发起还是已经跑了十分钟，没有资格说这句话。"""
    last = _by_name(run)["刷新时已在跑"]["snaps"][-1]

    assert last["mode"] == "unavailable"


def test_the_unreadable_note_does_not_assert_where_it_runs(run):
    """「读不到」那句也不许把猜测写成事实。

    它说的是「**可能**跑在别的进程，或快照已过期」—— 这是个可能，不是诊断；并且要
    回答用户真正的担心（这算不算出问题了），所以得说清它不影响分析本身。
    """
    note = run["notes"]["unavailable"]

    assert "可能" in note
    assert "不影响分析本身" in note
    assert "跑完之后这里会显示落库的逐轮记录" in note, "那句承诺是 ensureLoaded 的兑现目标"


# --------------------------------------------------------------------------
# 每一轮画了什么
# --------------------------------------------------------------------------


def test_a_round_card_carries_who_what_and_how_much(run):
    flat = _flat(_by_name(run)["跑动中两轮"]["snaps"][-1])

    head = [line for line in flat if line.startswith("第 ")]
    assert head and "第 1/8 轮" in head[0], "轮次要带总数（快照里有 max_rounds）"
    assert "索取上下文" in head[0], "这一轮的结局要说出来"
    assert "输入 12.0k tokens" in head[0], "token 数走 ai_usage_line.js 的 k/M 口径"
    assert "18.4 s" in head[0]


def test_the_shard_prefix_comes_from_the_progress(run):
    """子代理模式下「这是哪个分片」由进度对象补上（引擎自己不知道）。"""
    flat = _flat(_by_name(run)["跑动中两轮"]["snaps"][-1])

    assert any("分片 S1" in line for line in flat), flat
    assert not any(line.startswith("分片  ·") for line in flat), "没有分片的那一轮不该有前缀"


def test_missing_and_empty_are_written_differently(run):
    """**取不到**（有原因）与**确实没有内容**是两件事 —— 合成一句就是把
    「没有证据」说成「这里没问题」。"""
    flat = _flat(_by_name(run)["跑动中两轮"]["snaps"][-1])

    missing = [line for line in flat if line.startswith("取不到：")]
    empty = [line for line in flat if line.startswith("确实没有内容：")]
    assert missing and "平台读不到这个文件的内容" in missing[0]
    assert empty and "config/item.xlsx" in empty[0]


def test_the_requested_context_and_the_dropped_items_are_listed(run):
    flat = _flat(_by_name(run)["跑动中两轮"]["snaps"][-1])

    assert "代码差异 build/lua/CfgItem.lua" in flat
    assert any(line.startswith("未执行：") and "超出本次工具请求总预算" in line
               for line in flat), flat
    assert any("超出本次索取额度" in line for line in flat), "预算说明要带上"


def test_the_final_round_does_not_repeat_the_whole_report(run):
    """结论那一轮的原文就是整份报告 —— 报告在「完整结论」标签里，不在这里再贴一遍。"""
    rounds = _by_name(run)["跑动中两轮"]["snaps"][-1]["rounds"]

    assert len(rounds) == 2
    assert "整份报告正文" not in json.dumps(rounds[1], ensure_ascii=False)
    # 但先要上下文那一轮的模型原话要看得到（那是「它在想什么」）。
    assert "先看战斗逻辑" in json.dumps(rounds[0], ensure_ascii=False)


def test_the_final_round_says_where_the_report_went(run):
    """结论那一轮不重复贴报告，所以那张卡只剩头一行 —— 要说清它为什么是空的。

    「看着像空的卡片」与「这一轮什么都没干」在界面上长得一样，而这是两件事。
    """
    flat = _flat(_by_name(run)["跑动中两轮"]["snaps"][-1])
    final_card = _by_name(run)["跑动中两轮"]["snaps"][-1]["rounds"][1]
    final_lines = []
    _texts(final_card, final_lines)

    assert any("完整结论" in line and "标签" in line for line in final_lines), final_lines
    assert "整份报告正文" not in json.dumps(final_card, ensure_ascii=False)
    # 先要上下文那一轮的卡不该带这句（它没有报告可指）。
    first_lines = []
    _texts(_by_name(run)["跑动中两轮"]["snaps"][-1]["rounds"][0], first_lines)
    assert not any("这一轮返回的就是完整结论" in line for line in first_lines)
    assert flat


def test_a_round_with_nothing_to_show_still_gets_a_card(run):
    """一轮只报了个结局也要有一张卡：跳过它等于把「它跑了这一轮」藏起来。"""
    rounds = _by_name(run)["落库的逐轮"]["snaps"][-1]["rounds"]

    assert len(rounds) == 2
    assert len(rounds[0]["children"]) >= 1


def test_switching_runs_clears_the_previous_rounds(run):
    snaps = _by_name(run)["换运行清列表"]["snaps"]

    assert snaps[-2]["rounds"], "换之前有几轮"
    assert snaps[-1]["rounds"] == [], "换了运行号还留着上一次的过程 = 假信息"
    assert snaps[-1]["mode"] == "empty"


def test_closing_the_drawer_keeps_the_rounds(run):
    last = _by_name(run)["关抽屉不清列表"]["snaps"][-1]

    assert last["rounds"], "关抽屉只是不再看着它跑，过程不该被抹掉"
    assert last["mode"] == "settled"


def test_no_trace_is_said_in_words_not_as_an_empty_box(run):
    last = _by_name(run)["没有逐轮记录"]["snaps"][-1]

    assert "没有留下逐轮记录" in last["note"]


def test_the_three_notes_are_all_distinct(run):
    """五句话必须真的不同 —— 「复用一句话」是这个面板最容易犯的错。"""
    notes = run["notes"]
    values = [notes[key] for key in ("live", "running_no_rounds", "settled", "no_trace",
                                     "unavailable", "unavailable_stale", "empty",
                                     "loading")]
    assert len(set(values)) == len(values)
    assert "0" not in notes["running_no_rounds"], "不许拿 0 冒充「还没跑完第一轮」"


# --------------------------------------------------------------------------
# 懒加载：切过去才取、跑完那一刻要兑现承诺、取一次就够
# --------------------------------------------------------------------------


def test_it_does_not_fetch_without_a_run(run):
    assert run["noRun"]["calls"] == []


def test_it_does_not_fetch_while_watching(run):
    assert run["whileWatching"]["calls"] == [], "看着跑的期间不必去取落库那份"
    assert run["whileWatching"]["mode"] == "live"


def test_it_fetches_the_stored_rounds_when_the_tab_is_opened(run):
    assert run["beforeLoad"]["note"] == "正在读取这次运行的逐轮记录…", (
        "取之前那一瞬间要说「正在读取」，不能留一句与事实无关的话"
    )
    assert run["afterLoad"]["calls"] == ["/ai-analysis/runs/7/usage"]
    assert run["afterLoad"]["mode"] == "settled"
    assert run["afterLoad"]["rounds"] == 2
    assert run["afterLoad"]["note"] == "分析已结束，下面是这次运行的逐轮记录。"


def test_it_only_fetches_once(run):
    assert run["afterLoad"]["callsAfterSecond"] == 1, "已经取过的不该再请求一次"


def test_a_failed_fetch_is_said_out_loud(run):
    """取不到不是错误（可能没权限、也可能这次没有 trace），但**必须说出来**。"""
    assert run["failure"]["mode"] == "unavailable"
    assert "读不到" in run["failure"]["note"]
    assert "Not found." in run["failure"]["note"], "原因要带上，别只说「失败」"


def test_the_promise_of_stored_rounds_is_kept_when_the_run_ends(run):
    """**一句承诺就要有人兑现。**

    最后一帧读不到进度时，面板上写着「跑完之后这里会显示落库的逐轮记录」。跑完这一刻
    正是它说的那个时刻 —— 而取数只挂在「切到思考过程」这个动作上，用户若一直停在这个
    标签页（他手动点过标签，跑完不会被自动切走），那句承诺就永远挂在那儿，
    列表一直是空的。
    """
    assert run["stuckBefore"]["mode"] == "unavailable"
    assert "跑完之后这里会显示落库的逐轮记录" in run["stuckBefore"]["note"], (
        "前提：读不到那一帧留下的是一句承诺"
    )

    assert run["stuckAfter"]["mode"] == "loading", (
        "跑完那一刻 `unwatch` 必须自己发起取数（`mode` 同步变成「正在读取」就是证据）"
    )
    assert run["stuckLoaded"]["calls"] == ["/ai-analysis/runs/7/usage"], (
        "取的是这次运行那一份"
    )
    assert run["stuckLoaded"]["mode"] == "settled"
    assert run["stuckLoaded"]["rounds"] == 2, "取到了就要画出来"
    assert "分析已结束" in run["stuckLoaded"]["note"]
    assert run["stuckLoaded"]["note"] != run["stuckAfter"]["note"] != "", "那句话必须被换掉"


def test_a_response_for_the_previous_run_is_thrown_away(run):
    """取的过程中换了运行号：旧的那份**不许画上去**。

    画上去就是「这一次的运行里显示着上一次的逐轮过程」，而且 `mode` 会被写成
    「分析已结束」—— 分析正跑着，界面上挂着「已结束」。
    """
    stale = run["staleResponse"]

    assert stale["runId"] == 8, "前提：号已经换到 8 了"
    assert stale["rounds"] == 0, "7 的那几轮不许出现在 8 的面板上"
    assert stale["mode"] == "settled"


def test_a_failed_fetch_does_not_overwrite_a_run_we_are_watching(run):
    """取数的**失败**响应与成功响应受**同一道闸**：本页又在看着它跑，就不许写面板。

    `.then` 里有 `watching` 那道闸（上一条验的是它），`.catch` 里以前没有。后果不是
    报错，是**面板在说谎**：把「读不到这次运行的逐轮记录：…」挂到一次正跑着的运行上，
    而同一块面板的实时那一半马上又要画第 N 轮 —— 两句话不能同时为真。

    第二条断言盯着 `loading`：这道闸**不能只是早退**。`watching` 被认回来是
    `applyProgress` 干的，它不碰 `loading` —— 早退时若不把 `loading` 收掉，这个残留的
    标志位会把 `ensureLoaded` 永久挡在门外，跑完那一刻「这里会显示落库的逐轮记录」
    那句承诺就再也没人兑现（`test_the_promise_of_stored_rounds_is_kept_when_the_run_ends`
    验的就是它，两者是同一条纪律的两面）。
    """
    case = run["failureWhileWatching"]

    assert case["watching"] is True, "前提：本页确实又看着它跑了"
    assert case["mode"] == "live", (
        "取数失败把一次正跑着的运行打成了「读不到」—— 面板会说「读不到这次运行的逐轮"
        f"记录：boom」，而它其实正在看着这次运行。实际说明句：{case['note']!r}"
    )
    assert "读不到" not in case["note"], case["note"]

    after = case["afterUnwatch"]
    assert after["calls"] == ["/ai-analysis/runs/7/usage"], (
        "`loading` 没收掉 —— 跑完之后 `unwatch` 发起的取数被残留的标志位挡在了门外"
    )
    assert after["rounds"] == 2, "取到了就要画出来"
    assert after["mode"] == "settled"


def test_closing_the_drawer_mid_run_does_not_freeze_the_trace_as_missing(run):
    """**跑动中关抽屉，不能把「这次运行没有留下逐轮记录」写死。**

    逐轮是**跑完之后**才落库的（服务端 `_persist_outcome` 是全仓唯一写 `AiAnalysisTrace`
    的地方），所以跑动中取回的那一份必然是空表。把空表当终态收下（`loaded = true`）之后，
    这份记录就**再也刷不出来了**：跑完再打开抽屉时 `setRun` 会因为运行号没变而早退，
    `ensureLoaded` 又被 `loaded` 挡在门外。表现是「思考过程」对这次运行**永远**说
    「这次运行没有留下逐轮记录」，而明细一直在库里 —— 只有刷新页面才能恢复。

    触发序列很日常：跑起来 → 关抽屉 → 等它跑完 → 再打开抽屉。

    根因是 `unwatch()` 把「跑完了」与「用户不想看了」这两种语义混在一个函数里，而两者
    都会走到那句「没画出来就去取落库那份」。修法是让**只有知道运行结束的那一方**带
    `settled: true` —— 跑完之后这一次才是「这里会显示落库的逐轮记录」说的那个时刻。
    """
    mid = run["closedMidRun"]
    assert mid["calls"] == [], (
        "跑动中关抽屉就去取了落库的逐轮 —— 那一刻逐轮还没落库，取回的空表会被当成"
        "终态收下（`loaded = true`），跑完再打开时这条路就再也刷不出来了"
    )

    after = run["closedThenFinished"]
    assert after["calls"] == ["/ai-analysis/runs/7/usage"], (
        "跑完了却不再去取落库的逐轮 —— 面板会停在那句承诺上"
    )
    assert after["rounds"] == 2, "跑完之后那次取数没把逐轮画出来"
    assert after["mode"] == "settled"
    assert after["note"] == "分析已结束，下面是这次运行的逐轮记录。"


# --------------------------------------------------------------------------
# 「模型这一轮返回的内容」：展开之后不能被下一帧重画收起来
# --------------------------------------------------------------------------


def _model_box(snap: dict):
    """这一帧里「模型这一轮返回的内容」那个 `<details>` 的状态。取不到返回 None。"""
    def walk(node):
        if node.get("cls") == "ai-think-model":
            return node
        for child in node.get("children", []):
            hit = walk(child)
            if hit is not None:
                return hit
        return None

    for card in snap["rounds"]:
        hit = walk(card)
        if hit is not None:
            return hit
    return None


def test_an_expanded_model_output_survives_the_next_frame(run):
    """**用户点开「模型这一轮返回的内容」，下一帧重画之后必须还是展开的。**

    跑动中每来一帧进度（几秒一次）`applyProgress` 就重建整棵子树，而 `open` 是
    **DOM 属性** —— 新节点回到默认收起态。用户看到的现象就是「点开、过几秒自己收起来」，
    内容根本读不了（这个面板的正文通常有几百上千字，几秒读不完）。

    这里连看两帧：第 3 帧是用户刚点开的（`openModel`），第 4 帧是紧接着的一次重画。
    """
    snaps = _by_name(run)["展开模型输出后重画仍展开"]["snaps"]

    opened = _model_box(snaps[2])
    assert opened is not None and opened["open"] is True, (
        f"用户点开之后那一帧就该是展开的，实际 {opened and opened['open']}"
    )

    after = _model_box(snaps[3])
    assert after is not None, "重画之后那一块不见了"
    assert after["open"] is True, (
        "重画之后又收起来了 —— 用户点开的内容过几秒就自己合上，读不了。"
        "展开状态必须由模块记着（`expandedModelRounds`），不能指望 DOM 留着。"
    )


def test_a_collapsed_model_output_is_not_reopened_behind_the_users_back(run):
    """反方向：用户收起来之后重画不许又替他展开（那是跟用户对着干）。"""
    snaps = _by_name(run)["收起模型输出后重画不自动展开"]["snaps"]

    assert _model_box(snaps[3])["open"] is True, "点开那一帧应当是展开的"
    assert _model_box(snaps[4])["open"] is False, "用户收起之后那一帧应当是收起的"
    assert _model_box(snaps[5])["open"] is False, "重画又把它展开了"


def test_changing_the_run_does_not_inherit_the_expanded_state(run):
    """换一次运行不许继承上一次的展开状态。

    记的键是「轮次号 + 分片」，两次运行里必然重名 —— 不清空的话，新一次的某一轮会
    莫名其妙是展开的，而用户从没点过它。
    """
    snaps = _by_name(run)["换运行不继承展开状态"]["snaps"]

    assert _model_box(snaps[2])["open"] is True, "点开那一帧应当是展开的"
    assert _model_box(snaps[5])["open"] is False, (
        "换了运行之后仍然是展开的 —— 展开状态跨运行漏了过来（键在两个运行里重名）"
    )
