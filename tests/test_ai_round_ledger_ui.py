# -*- coding: utf-8 -*-
"""「思考过程」面板里的**逐成员账**与**被中断那一次的说法** —— 用 node 真跑
`static/js/ai_think_log.js`。

## 这一组守什么（工作包 E）

需求要两件界面上的事：

1. **每个成员、汇总、复核分别显示**输入/输出/缓存 token、工具请求/执行/失败/截断、
   耗时、候选数。这些数由服务端从逐轮事件账本归约（`round_events.member_totals`），
   前端只负责画 —— 所以这里验的是「画对了、口径没走样」；
2. **worker 被中断的那一次**：进度是服务端**从库里补的**（`source === "ledger"`，
   `run_finished` 为真）。面板必须说「没有跑完」，而且落库那份 trace 取回来是**空的**
   （它是整次跑完才批量写的）时，**手上那几轮不许被抹掉** —— 抹掉就等于把唯一的过程
   记录丢了，而那正是这次要修的缺陷。

三条口径在这里逐条钉住：`null` = **未上报**（不是 0）、候选数的 `null` = 「这个成员
没有交结论」、老载荷（没有 `members` 键）**一个节点都不多**（行为与改动前逐字一致）。

静态断言看不出来这些 —— 它们是「同一个面板在不同载荷下的不同说法」，所以按仓库既有做法
把真函数放进带假 DOM 的 node 沙箱里跑。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_think_log.js"
USAGE_LINE = PROJECT_ROOT / "static" / "js" / "ai_usage_line.js"
STREAM_STATUS = PROJECT_ROOT / "static" / "js" / "ai_stream_status.js"

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

function makeEl(id) {
    var node = { id: id, className: '', hidden: false, children: [], _attrs: {}, open: false };
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
    node.classList = {add: function () {}, remove: function () {}, contains: function () { return false; }};
    return node;
}

var els = {};
var sandbox = {
    console: console,
    document: {
        readyState: 'complete',
        getElementById: function (id) { return els[id] || null; },
        createElement: function (tag) { return makeEl(tag); },
        addEventListener: function () {}
    }
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__USAGE_LINE__, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(__STREAM_STATUS__, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiThinkLog;

var NOTE_ID = 'aiThinkNote';
var LOG_ID = 'aiThinkLog';

function seed() {
    els = {};
    els[NOTE_ID] = makeEl(NOTE_ID);
    els[LOG_ID] = makeEl(LOG_ID);
}

function dump(node) {
    return {
        cls: node.className,
        text: node.textContent,
        children: node.children.map(dump)
    };
}

function findByClass(node, cls) {
    if (node.className === cls) return node;
    for (var i = 0; i < node.children.length; i++) {
        var hit = findByClass(node.children[i], cls);
        if (hit) return hit;
    }
    return null;
}

/** 逐成员那一块画出来的东西（没有那一块时返回 null）。 */
function membersBox() {
    var box = findByClass(els[LOG_ID], 'ai-think-members');
    if (!box) return null;
    return box.children
        .filter(function (node) { return node.className === 'ai-think-member'; })
        .map(function (node) {
            return node.children.map(function (child) { return child.textContent; });
        });
}

function snap() {
    return {
        note: els[NOTE_ID].textContent,
        mode: api.state().mode,
        members: api.state().members,
        fromLedger: api.state().fromLedger,
        box: membersBox(),
        rounds: els[LOG_ID].children
            .filter(function (node) { return node.className === 'ai-think-round'; })
            .map(function (node) {
                var head = node.children[0];
                return head ? head.textContent : '';
            }),
        // 面板里**所有**子节点的 class（用来钉住「老载荷一个节点都不多」）。
        classes: els[LOG_ID].children.map(function (node) { return node.className; })
    };
}

var LIVE = __LIVE__;
var OLD = __OLD__;
var LEDGER = __LEDGER__;
var ONE = __ONE__;

var out = {};

seed();
api.watch(7);
api.applyProgress(LIVE, 'running');
out.live = snap();

// 老载荷（没有 `members` 键）：一个节点都不多、也不该有逐成员那一块。
seed();
api.watch(7);
api.applyProgress(OLD, 'running');
out.old_payload = snap();

// 上游没上报的成员 + 没有交结论的成员：`null` 不许画成 0。
seed();
api.watch(7);
api.applyProgress(ONE, 'running');
out.unreported = snap();

// 被中断的那一次：进度是服务端从库里补的（source=ledger、run_finished=true）。
seed();
api.watch(7);
api.applyProgress(LEDGER, 'failed');
api.unwatch({settled: true});
out.interrupted = snap();

// 落库那份逐轮是**空的**（trace 是整次跑完才写的）—— 手上那几轮不许被抹掉。
api.applyRounds([], {});
out.interrupted_after_empty_trace = snap();

// 换运行号：上一次的成员账与「被中断」那句话都不许跟过来。
api.setRun(8);
out.next_run = snap();

process.stdout.write(JSON.stringify(out));
"""


def _run_driver(members_live: list[dict], old_payload: dict, one_member: list[dict]) -> dict:
    live = {
        "round": 2,
        "max_rounds": 8,
        "rounds_seen": 2,
        "rounds_truncated": False,
        "members": members_live,
        "rounds": [
            {"round_index": 1, "agent": "S1", "agent_round": 1, "agent_index": 1,
             "agent_total": 3, "outcome": "requests", "tokens_input": 100,
             "duration_ms": 1000, "requests": [], "executed": [], "dropped": []},
            {"round_index": 2, "agent": "S1", "agent_round": 2, "agent_index": 1,
             "agent_total": 3, "outcome": "final", "tokens_input": 200,
             "duration_ms": 2000, "requests": [], "executed": [], "dropped": []},
        ],
    }
    ledger = dict(live)
    ledger["source"] = "ledger"
    ledger["run_status"] = "failed"
    ledger["run_finished"] = True
    ledger["status"] = "requests"
    led = {
        "round": 1,
        "max_rounds": 0,
        "rounds_seen": 1,
        "rounds_truncated": False,
        "members": one_member,
        "source": "ledger",
        "run_status": "failed",
        "run_finished": True,
        "rounds": [
            {"round_index": 1, "agent": "S1", "agent_round": 1, "agent_index": 1,
             "agent_total": 3, "outcome": "requests", "tokens_input": 100,
             "duration_ms": 1000, "requests": [], "executed": [], "dropped": [],
             # 事件账本里那个 `entry_json` 就是 `live_round_entry` 的产物，**带这一键**
             # （见 `services/ai/trace_evidence.py`）—— 手写的替身也要带，否则面板上
             # 那一轮会写成「结束方式未上报」，与真机不符。
             "finish_reason": "stop"},
        ],
    }
    script = DRIVER.replace("__USAGE_LINE__", json.dumps(str(USAGE_LINE)))
    script = script.replace("__STREAM_STATUS__", json.dumps(str(STREAM_STATUS)))
    script = script.replace("__SCRIPT__", json.dumps(str(SCRIPT)))
    script = script.replace("__LIVE__", json.dumps(live, ensure_ascii=False))
    script = script.replace("__OLD__", json.dumps(old_payload, ensure_ascii=False))
    script = script.replace("__LEDGER__", json.dumps(led, ensure_ascii=False))
    script = script.replace("__ONE__", json.dumps(
        {"round": 1, "max_rounds": 8, "rounds_seen": 1, "rounds_truncated": False,
         "members": one_member, "rounds": live["rounds"][:1]}, ensure_ascii=False))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _member(**overrides) -> dict:
    base = {
        "member": "S1", "member_index": 1, "member_total": 3, "role": "subagent",
        "rounds": 2, "last_status": "final",
        "tokens_input": 300, "tokens_output": 50,
        "cache_read_tokens": 110, "cache_write_tokens": 0,
        "tokens_reported": True, "duration_ms": 3000,
        "candidates": 1, "candidates_reported": True,
        "tool_requests": 5, "tool_executed": 4, "tool_failed": 1, "tool_truncated": 1,
        "tool_refused": 0, "tool_dropped": 0, "counts_reported": True,
        "context_chars": 1000,
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def run():
    return _run_driver(
        members_live=[
            _member(),
            _member(member="", member_index=2, role="synthesis", rounds=1,
                    tokens_input=400, tokens_output=50, cache_read_tokens=None,
                    candidates=2, tool_requests=0, tool_executed=0, tool_failed=0,
                    tool_truncated=0),
            _member(member="V1", member_index=3, role="verify", rounds=1,
                    tokens_input=500, tokens_output=60, candidates=0,
                    tool_requests=4, tool_executed=3, tool_failed=2),
        ],
        old_payload={"round": 1, "max_rounds": 8, "rounds_seen": 1,
                     "rounds_truncated": False,
                     "rounds": [{"round_index": 1, "agent": "S1", "agent_round": 1,
                                 "agent_index": 1, "agent_total": 3,
                                 "outcome": "requests", "tokens_input": 100,
                                 "duration_ms": 1000, "requests": [],
                                 "executed": [], "dropped": []}]},
        one_member=[_member(member="S1", rounds=1, tokens_input=None, tokens_output=None,
                            tokens_reported=False, candidates=None,
                            candidates_reported=False, counts_reported=False,
                            # 缓存读未上报、缓存写报了个 0 —— 同一行里两个口径并排，
                            # 「未上报」不许被画成 0 这件事才有对照。
                            cache_read_tokens=None, cache_write_tokens=0,
                            tool_requests=None, tool_executed=None, tool_failed=None,
                            tool_truncated=None)],
    )


# ==========================================================================
#  一、逐成员那一块
# ==========================================================================


def test_each_member_gets_a_line_written_from_the_server_ledger(run):
    """分片 / 汇总 / 复核各一条，且**每个成员的数字是自己的**（不串门）。"""
    box = run["live"]["box"]
    assert box is not None, "逐成员那一块没有画出来"
    assert len(box) == 3
    assert box[0][0] == "分片 S1 · 2 轮"
    assert box[1][0] == "汇总 · 1 轮"
    assert box[2][0] == "复核（对账） V1 · 1 轮"
    # 第一条：输入/输出/缓存、工具四个数、耗时、候选数
    line = " ".join(box[0])
    assert "输入 300" in line and "输出 50" in line
    assert "缓存读 110" in line and "缓存写 0" in line
    assert "请求 5 / 执行 4 / 失败 1 / 截断 1" in line
    assert "模型耗时 3.0 s" in line, line
    assert "候选结论 1 条" in line
    # 第三条（复核）：它的数字与第一条不同 —— 抄第一条就会在这里红
    assert "输入 500" in " ".join(box[2])
    assert "请求 4 / 执行 3 / 失败 2" in " ".join(box[2])


def test_an_unreported_number_says_so_instead_of_zero(run):
    """`null` = 未上报 → 写「未上报」；`0` = 确实是 0 → 写 0。两句话不许混。"""
    box = run["unreported"]["box"]
    assert box is not None
    line = " ".join(box[0])
    assert "未上报" in line, line
    assert "输入 0" not in line, "把「上游没报」画成了「一个 token 都没花」"
    # 缓存读是 `null`（未上报）而缓存写这一条用的是既有 fixture 里的 0（确实是 0）
    assert "缓存读 未上报" in line
    # 合计不全 → 必须说明这是下界
    assert "已知下界" in line, line
    # 没有交结论的成员：候选数那句话与「0 条」是两件事
    assert "这个成员没有交结论" in line, line


def test_a_member_that_reported_zero_candidates_says_zero(run):
    """复核那一条 `candidates = 0`（交了结论、一条都没报）→ 写「候选结论 0 条」。"""
    line = " ".join(run["live"]["box"][2])
    assert "候选结论 0 条" in line, line


# ==========================================================================
#  二、老载荷与实时载荷的形状不变
# ==========================================================================


def test_an_old_payload_without_members_adds_no_node(run):
    """没有 `members` 键的载荷（老服务端 / 测试替身）：面板一个节点都不多。"""
    old = run["old_payload"]
    assert old["box"] is None
    assert old["members"] == 0
    assert old["classes"] == ["ai-think-round"], old["classes"]
    assert old["mode"] == "live"


def test_switching_run_forgets_the_previous_members(run):
    """换了运行号：上一次的成员账与「被中断」那句话都不许跟过来。"""
    nxt = run["next_run"]
    assert nxt["members"] == 0
    assert nxt["box"] is None
    assert nxt["fromLedger"] is False


# ==========================================================================
#  三、被中断的那一次
# ==========================================================================


def test_an_interrupted_run_says_it_did_not_finish_and_keeps_its_rounds(run):
    """**验收条**：worker 被杀之后重新打开抽屉 —— 轮次还在、说法是「没有跑完」。"""
    interrupted = run["interrupted"]
    assert interrupted["fromLedger"] is True
    assert interrupted["mode"] == "ledger_interrupted"
    assert "没有跑完" in interrupted["note"], interrupted["note"]
    assert "没有重跑" in interrupted["note"], interrupted["note"]
    assert interrupted["rounds"] == [
        "分片 S1 (1/3) · 第 1 轮 · 索取上下文 · 输入 100 tokens · 1.0 s · 正常结束"
    ]
    # 逐成员那一块照样在（账就是从这里来的）
    assert interrupted["box"] is not None and len(interrupted["box"]) == 1


def test_an_empty_persisted_trace_does_not_wipe_the_ledger_rounds(run):
    """落库那份是**空的**（trace 整次跑完才写）—— 手上那几轮不许被抹掉。"""
    after = run["interrupted_after_empty_trace"]
    assert after["rounds"] == run["interrupted"]["rounds"], (
        "取回空的落库明细就把逐轮过程清空了 —— 那正是被中断那一次唯一的记录"
    )
    assert after["mode"] == "ledger_interrupted"
    assert after["box"] is not None
