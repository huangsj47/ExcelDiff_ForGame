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
    node.addEventListener = function () {};
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
        children: node.children.map(dumpNode)
    };
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
    roundsStored: function () { api.applyRounds(ROUNDS.stored, {}); },
    roundsSame: function () { api.applyRounds(ROUNDS.sameAsLive, {}); },
    roundsEmpty: function () { api.applyRounds([], {}); }
};

var cases = __CASES__;
var results = cases.map(function (item) {
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
                "dropped": [{"kind": "request", "reason": "超出本次工具请求总预算（20 次），未执行",
                             "detail": "file_diff x"}],
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
