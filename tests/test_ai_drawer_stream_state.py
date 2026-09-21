# -*- coding: utf-8 -*-
"""抽屉上的「分析中：第 N 轮 …」与「失败」不能同时为真。

## 报障形态（线上，`/projects/1/merged-view`）

抽屉里三样东西同时挂着：

    失败                                              ← 结论区的徽章
    分析中：第 2/8 轮 · 本次已用 92329 tokens（尚未落库，只含上游已上报的部分）
    AI 分析失败或连接中断                              ← 结论区的正文

第一句与第二句**不可能同时为真**。根因不是文案，是三份模板里那份逐字复制的进度轮询
（`startAiBudgetWatch` / `startBudgetWatch`）**只停表、不收字**：
`closeWeeklyAiStream()` 把定时器清掉了，留在 DOM 上的最后一帧没人擦，
于是「分析中：第 2/8 轮」一直挂在「失败」旁边。

第三句是另一件事的合称。EventSource 的 `error` 事件底下压着两种完全不同的事，
只能靠 `event.data` 有没有区分：

  * **有 data** —— 服务端主动发的 `error` 事件（预算闸门拦下 / 跑挂了），原因在里面；
  * **没有 data** —— **连接层断了**（代理把长时间没有字节流动的 SSE 掐了、网络断了、
    服务端进程没了）。

分析跑在服务端的生成器里，浏览器这头断线**不会**让它停下来：结论照常落库，
可能就差几秒。原来的实现一律打「失败」，而用户看到的那句话正是这两个状态的合称。

## 服务端那一半：原因为什么到不了界面

`_execute_analysis` 的 docstring 写着「**不抛异常**」，但那段代码里只有
`try/finally`，一个 `except` 都没有 —— 承诺靠的是「下游一处都不会抛」这个假设。
任何一处没想到的异常都会**带着「运行号早就发给界面了」**穿出生成器：浏览器那头
只看到连接静默断开（也就是上面第三句），真正的原因只留在服务端日志里，
库里那条 run 还停在 `running`（一小时后才被 `effective_status` 翻成失败）。

所以这一层也要修：凡是**已经有运行号**的失败，都必须变成带原因的结论。

## 这个文件守什么

1. **口径只有一份**：那行状态字怎么写、结束/失败/断线各说什么，全在
   `static/js/ai_stream_status.js` 里，三份模板不许再各写一句（node 真跑 + 静态守卫）。
2. **停表必须收字**：`watchRun` 返回的 `stop()` 一定把那行字擦掉 —— 这是「同时为真」
   那一幕的直接修法，用假定时器真跑一遍。
3. **断线不等于失败**：连接断了先回头问服务端「这次运行到底什么状态」，再决定说什么；
   还在跑就继续轮询（轮询读的是另一个端点），跑完了把落库的结论取回来。
4. **服务端一定以终态事件收尾**：异常也要带着原因发出去，不许静默断流。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import services.ai.job_service as job_service  # noqa: E402
import services.ai_analysis_service as ai_service  # noqa: E402
import services.task_worker_service as worker  # noqa: E402
from app import app as flask_app  # noqa: E402
from app import create_tables, db  # noqa: E402
from models import Project, Repository, WeeklyVersionConfig  # noqa: E402
from models.ai_analysis import (  # noqa: E402
    MODE_INCREMENTAL,
    STATE_SUCCEEDED,
    AiAnalysisJob,
    AiAnalysisRun,  # noqa: E402
)
from models.task import BackgroundTask  # noqa: E402
from services.ai_analysis_service import (  # noqa: E402
    end_with_a_terminal_event,
)

MODULE = "static/js/ai_stream_status.js"
TEMPLATES = (
    "templates/merged_project_view.html",
    "templates/weekly_version_diff.html",
    "templates/commit_diff_new.html",
)
# 报障的那一页（抽屉里的进度轮询就写在它里面）。
REPORTED = "templates/merged_project_view.html"

# 用户截图里那一帧：第 2/8 轮、已用 92329 tokens。
LIVE_FRAME = {
    "success": True,
    "status": "running",
    "progress": {"round": 2, "max_rounds": 8, "live_tokens": 92329},
    "budget": {"over": False},
}
REPORTED_LINE = (
    "分析中：第 2/8 轮 · 本次已用 92329 tokens"
    "（尚未落库，只含上游已上报的部分）"
)


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _strip_js_comments(code: str) -> str:
    """剥掉 JS 注释，**字符串字面量里的 `//` 不动**。

    静态断言之前必须先剥：本文件与共享模块的注释里原样引用着被断言的写法
    （「分析中：第 2/8 轮」「AI 分析失败或连接中断」这些句子本身就出现在说明里），
    不剥就会出现「实现已经改干净了，注释里那句反例却让断言为真」的假绿。
    """
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


def _template_script(rel: str) -> str:
    """模板里那段内联 `<script>`（注释已剥）。"""
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _read(rel), re.S)
    assert blocks, f"{rel} 里没有内联脚本"
    return _strip_js_comments("\n".join(blocks))


def _function_body(script: str, name: str) -> str:
    start = script.index(f"function {name}(")
    brace = script.index("{", start)
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[brace: index + 1]
        index += 1
    raise AssertionError(f"找不到 {name} 的函数体")


def _function_source(script: str, name: str) -> str:
    """`[async] function <name>(…) { … }` 的完整源码（能直接喂给 node 求值）。

    `async` 必须一起带上：少了它，函数体里的 `await` 会让整段源码语法错误，
    而那看起来像「模板写错了」，其实是取值取错了。
    """
    marker = f"function {name}("
    start = script.index(marker)
    head_start = start - len("async ") if script[:start].endswith("async ") else start
    head = script[head_start: script.index("{", start)]
    return head + _function_body(script, name)


def _handler_body(script: str, event: str) -> str:
    """`addEventListener('<event>', function (event) { … })` 的回调体（按花括号配平）。

    顺序断言只能看**这个回调**：整个文件里有十几处 `closeWeeklyAiStream()` 与
    `setWeeklyAiMeta()`，整段搜出来的先后关系没有任何意义。
    """
    marker = f"addEventListener('{event}'"
    start = script.index(marker)
    brace = script.index("{", script.index("function", start))
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[brace: index + 1]
        index += 1
    raise AssertionError(f"找不到 {event} 事件的回调体")


# ---------------------------------------------------------------------------
#  用 node 真跑共享模块
# ---------------------------------------------------------------------------

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = { console: console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'ai_stream_status.js' });
const S = sandbox.AiStreamStatus;

const A = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {};

function fakeFetch(frames) {
    const queue = frames.slice();
    return function (url) {
        const frame = queue.length > 1 ? queue.shift() : queue[0];
        if (!frame) return Promise.reject(new Error('offline'));
        return Promise.resolve({ json: function () { return Promise.resolve(frame); } });
    };
}

function makeTimers() {
    return { ticks: [], cleared: [],
             setInterval: function (fn) { this.ticks.push(fn); return this.ticks.length; },
             clearInterval: function (id) { this.cleared.push(id); } };
}

// 一次轮询的完整生命周期：第一帧 → 测试逐帧驱动（真定时器不参与）→ 停表。
// 每一步都记下**那一行字现在是什么**，这样「停表之后还留着分析中」这种缺陷
// 会直接落进断言里。
async function runWatch(spec) {
    const el = { textContent: '初始文案' };
    const timers = makeTimers();
    const budgets = [];
    const finished = [];
    const watch = S.watchRun(spec.runId, {
        metaEl: el,
        onBudget: function (budget) { budgets.push(budget); },
        onFinished: function (status) { finished.push(status); },
        fetchImpl: fakeFetch(spec.frames),
        setInterval: timers.setInterval.bind(timers),
        clearInterval: timers.clearInterval.bind(timers)
    });
    const steps = [];
    await watch.first;
    steps.push(el.textContent);
    const ticks = timers.ticks.slice();
    for (let index = 0; index < ticks.length; index += 1) {
        // 定时器回调是 `function () { tick(); }`（返回值被丢了），所以这里要等
        // 微任务跑完才能读那一行 —— 假 fetch 是立即 resolve 的。
        ticks[index]();
        await new Promise(function (resolve) { setTimeout(resolve, 0); });
        steps.push(el.textContent);
    }
    if (spec.stopAfterRun) {
        watch.stop();
    }
    return { steps: steps, afterStop: el.textContent, finished: finished,
             cleared: timers.cleared.length, budgetFrames: budgets.length };
}

// 连接断掉之后那段处理**从模板里取出来真跑**（页面全局的那些名字按同样形状给桩）。
// 按 URL 分流的假 fetch：`/waiting` 与 `/progress` 是**两个不同的端点**，
// 用一条队列喂它们会让「问了哪一个」这件事消失在测试里。
function fakeFetchByUrl(routes) {
    return function (url) {
        const key = String(url).indexOf('/waiting') >= 0 ? 'waiting' : 'status';
        const frame = routes[key];
        if (frame === undefined || frame === null) return Promise.reject(new Error('offline'));
        return Promise.resolve({ json: function () { return Promise.resolve(frame); } });
    };
}

async function runDrop(spec) {
    if ('waitingState' in spec) {
        sandbox.fetch = fakeFetchByUrl({
            waiting: spec.waitingState,
            status: spec.runStatus === null ? null : { success: true, status: spec.runStatus }
        });
    } else {
        sandbox.fetch = fakeFetch([spec.runStatus === null ? null : { success: true, status: spec.runStatus }]);
    }
    const probe = A.probe.replace(/__RUN_ID__/g, spec.runId === null ? 'null' : String(spec.runId));
    vm.runInContext(probe, sandbox, { filename: 'probe.js' });
    await sandbox.__probeDrop(null);
    return sandbox.__probeState();
}

(async function () {
    out.typeofs = {
        AiStreamStatus: typeof sandbox.AiStreamStatus,
        progressText: typeof S.progressText,
        resultOutcome: typeof S.resultOutcome,
        errorOutcome: typeof S.errorOutcome,
        interruptOutcome: typeof S.interruptOutcome,
        watchRun: typeof S.watchRun
    };
    out.constants = { POLL_INTERVAL_MS: S.POLL_INTERVAL_MS, NO_PROGRESS: S.NO_PROGRESS };
    out.isTerminal = A.statuses.map(function (s) { return S.isTerminal(s); });
    out.isRunning = A.statuses.map(function (s) { return S.isRunning(s); });
    out.roundText = A.rounds.map(function (p) { return S.roundText(p); });
    out.progressText = A.progressText.map(function (pair) { return S.progressText(pair[0], pair[1]); });
    out.resultOutcome = A.resultOutcome.map(function (p) { return S.resultOutcome(p); });
    out.errorOutcome = A.errorOutcome.map(function (pair) { return S.errorOutcome(pair[0], pair[1]); });
    out.interruptOutcome = A.interruptOutcome.map(function (pair) { return S.interruptOutcome(pair[0], pair[1]); });
    out.watches = [];
    for (const spec of A.watches) { out.watches.push(await runWatch(spec)); }
    out.drops = [];
    for (const spec of A.drops) { out.drops.push(await runDrop(spec)); }
    process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""


def _probe_source() -> str:
    """把报障那一页里真正在跑的那段「连接断了怎么办」取出来，配一份页面全局桩。

    取的是模板里**逐字的那一份**：抄一份进测试就失去意义了（改了模板测试还是绿的）。

    `fetchWeeklyAiWaitingState` 也一起取：没有运行号时那段代码会**先问服务端**
    「这次点击登记成什么了」，抄一份桩进去就等于把要验的那一步跳过。
    """
    script = _template_script(REPORTED)
    return (
        """
var probeClosed = false;
var weeklyAiSource = { close: function () { probeClosed = true; } };
var weeklyAiRunId = __RUN_ID__;
var weeklyAiCurrentConfigId = 42;
var probeCalls = [];
function setWeeklyAiStatusBadge(text, tone) { probeCalls.push(['badge', text, tone]); }
function setWeeklyAiMeta(text) { probeCalls.push(['meta', text]); }
function setWeeklyAiOutput(text, isEmpty, variant) {
    probeCalls.push(['output', text, isEmpty, variant]);
}
function stopAiBudgetWatch() { probeCalls.push(['stopWatch']); }
function refreshWeeklyAiLatest(id) { probeCalls.push(['refresh', id]); }
function startWeeklyAiWaitingPoll() { probeCalls.push(['waitingPoll']); }
"""
        + _function_source(script, "fetchWeeklyAiWaitingState")
        + "\n"
        + _function_source(script, "handleWeeklyAiStreamDrop")
        + """
// 探针挂在 vm 上下文的全局对象上（它就是 driver 里的那个 sandbox）。
globalThis.__probeDrop = handleWeeklyAiStreamDrop;
globalThis.__probeState = function () {
    return { closed: probeClosed, released: weeklyAiSource === null, calls: probeCalls };
};
"""
    )


_CASES = {
    "statuses": ["running", "pending", "succeeded", "failed", "", None, "weird"],
    "rounds": [
        None,
        {"round": 2, "max_rounds": 8},
        {"round": 0, "max_rounds": 8},
        {"round": 1, "max_rounds": 0},
        {"round": "3", "max_rounds": "8"},
    ],
    # `[progress, status]`
    "progressText": [
        [LIVE_FRAME["progress"], "running"],
        # 读不到快照（多进程 / 别的 worker / 进程刚重启）：说「不可用」，不是 0
        [None, "running"],
        [None, "pending"],
        [None, None],
        # 到了终态：这一行必须让位（返回 null 表示「这次什么都别写」）
        [LIVE_FRAME["progress"], "succeeded"],
        [LIVE_FRAME["progress"], "failed"],
        [None, "failed"],
        # 用量没上报：只说轮次，不补一个 0
        [{"round": 2, "max_rounds": 8, "live_tokens": None}, "running"],
        # 快照在、轮次读不出来（脏数据）：不可用，不编一个「第 0 轮」
        [{"round": 0, "max_rounds": 8, "live_tokens": 100}, "running"],
        # 子代理模式：这一轮是哪个分片在跑（见 `services/ai/subagent.py`）
        [{"round": 2, "max_rounds": 4, "live_tokens": 5000,
          "agent": "S1", "agent_index": 1, "agent_total": 3}, "running"],
        [{"round": 1, "max_rounds": 4, "live_tokens": None,
          "agent": "", "agent_index": 4, "agent_total": 4}, "running"],
        # 没有分片信息（老运行 / 没开子代理 / 快照里没有这三个键）：那一行与以前一字不差
        [{"round": 3, "max_rounds": 8, "live_tokens": 100,
          "agent": "", "agent_index": 0, "agent_total": 0}, "running"],
        # **引擎开始时那一帧**（`on_start`，`round=0`）：一轮还没跑完，但这一帧存在的
        # 全部意义就是把「谁在跑」报出去 —— 汇总那一次开始跑时，快照里留的还是上一个
        # 分片，界面会一直挂着那个名字（见 subagent._call_engine 的 report）。
        [{"round": 0, "max_rounds": 4, "live_tokens": None,
          "agent": "", "agent_index": 4, "agent_total": 4}, "running"],
        [{"round": 0, "max_rounds": 4, "live_tokens": None,
          "agent": "S2", "agent_index": 2, "agent_total": 3}, "running"],
    ],
    "resultOutcome": [
        {"status": "failed", "error_message": "分析中断：ConnectionError: 上游断了"},
        {"status": "failed"},
        {"status": "succeeded", "risk_level": "high"},
        {"status": "degraded", "risk_level": "low"},
        {},
    ],
    # `[payload, started]`
    "errorOutcome": [
        [{"message": "已超出预算：本月已用 1.2M / 1.0M tokens"}, False],
        [{"message": "Project API key not configured."}, False],
        [{"message": "分析中断：RuntimeError: boom"}, True],
        [{}, True],
        [None, False],
    ],
    # `[status, started]`
    "interruptOutcome": [
        ["running", True],
        ["pending", True],
        ["succeeded", True],
        ["failed", True],
        [None, True],
        [None, False],
        ["running", False],
    ],
    "watches": [
        # 0：用户截图那一幕。跑着的时候写着「分析中：第 2/8 轮…」，
        #    服务端承认这次失败了（result 事件）→ 停表，那行字**必须消失**。
        {"runId": 7, "frames": [LIVE_FRAME, {"success": True, "status": "failed"}]},
        # 1：连接断了（模板那边会主动停表）→ 那行字同样必须消失。
        {"runId": 7, "frames": [LIVE_FRAME], "stopAfterRun": True},
        # 2：一路跑到成功。停表之后也不许留着「分析中」。
        {"runId": 7, "frames": [LIVE_FRAME, {"success": True, "status": "succeeded"}],
         "stopAfterRun": True},
        # 3：读不到进度（多进程）：说「不可用」，仍然要能停干净。
        {"runId": 7, "frames": [{"success": True, "status": "running", "progress": None}],
         "stopAfterRun": True},
        # 4：轮询也断了（帧是 null → 请求被拒）：不许把那行字改成「已用 0」。
        {"runId": 7, "frames": [LIVE_FRAME, None], "stopAfterRun": True},
    ],
    # 报障那一页「连接断了」之后真跑一遍：`status` 是回头问到的运行状态
    "drops": [
        {"runId": 7, "runStatus": "running"},
        {"runId": 7, "runStatus": "succeeded"},
        {"runId": 7, "runStatus": "failed"},
        {"runId": 7, "runStatus": None},
        {"runId": None, "runStatus": None},
        # 5：**没有运行号，但服务端说这次点击正登记着等待同步。**
        #    修复前这里会打「连接中断：没有收到运行号。」—— 与数据库里那条
        #    pending 意图相反（线上实测那一幕）。真相在服务端，问它。
        {"runId": None, "runStatus": None,
         "waitingState": {"success": True, "waiting": True, "run_id": None,
                          "message": "等待 Diff 同步完成后再分析。"}},
        # 6：登记已经转交给某一次运行 → 附着上去，不许让用户再点一次（会再花一次钱）。
        {"runId": None, "runStatus": None,
         "waitingState": {"success": True, "waiting": False, "run_id": 42,
                          "message": ""}},
        # 7：登记已经结束（同步一直没结束 / 被别的分析覆盖）→ 如实说不确定。
        {"runId": None, "runStatus": None,
         "waitingState": {"success": True, "waiting": False, "run_id": None,
                          "message": "这次登记已经结束。"}},
    ],
    "probe": "",
}

_RESULTS: dict = {}


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    cases = dict(_CASES)
    cases["probe"] = _probe_source()
    workdir = Path(PROJECT_ROOT) / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_stream_status_driver.js"
    payload = workdir / "ai_stream_status_cases.json"
    driver.write_text(_DRIVER, encoding="utf-8")
    payload.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["node", str(driver), str(PROJECT_ROOT / MODULE), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"共享模块在 node 里跑不起来（它会在浏览器里表现成整个抽屉不动）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


# ==========================================================================
#  一、那行状态字的写法（node 真跑）
# ==========================================================================


def test_the_shared_module_loads_and_exposes_the_api():
    result = _run_node()
    assert result["typeofs"]["AiStreamStatus"] == "object"
    for name in ("progressText", "resultOutcome", "errorOutcome", "interruptOutcome", "watchRun"):
        assert result["typeofs"][name] == "function", f"{name} 没挂出来"


def test_a_running_analysis_says_which_round_and_that_it_is_not_booked_yet():
    """**用户截图里那一行。** 口径两条：第几轮，以及「这个数还没落库」。"""
    line = _run_node()["progressText"][0]
    assert line == REPORTED_LINE, line
    assert "尚未落库" in line


def test_an_unreadable_snapshot_says_unavailable_not_zero():
    """多进程部署、跑在别的 worker 里时快照是 `None`。

    这时说「进度不可用」——**不许**写「第 0 轮 / 已用 0 tokens」：0 是一个结论
    （一次都没跑、一个 token 都没花），说反了比不说更糟。
    """
    lines = _run_node()["progressText"]
    for index in (1, 2, 3):
        line = lines[index]
        assert line is not None, f"用例 {index}：读不到进度却什么都不说"
        assert "进度不可用" in line, line
        assert "0 tokens" not in line and "第 0 轮" not in line, line


def test_a_snapshot_without_a_finished_round_says_it_is_still_calling():
    """快照在、但**一轮都还没跑完** —— 这**不是**「进度不可用」。

    这两件事原先共用一句话，而它们的含义**正相反**：「进度不可用」是「我们看不见它」
    （多进程 / 别的 worker），这一句是「它正在正常干活，只是第一轮还没跑完」。后者
    是一次正常分析的**起步阶段**，而且它可能是几分钟（整整一次模型调用）。

    引擎现在会在第一次模型调用之前报一帧（`on_start`，`index=0`），界面这才分得清。
    在这之前用户看到的是「看不到第几轮」，于是以为卡住了。

    「不编数字」这条纪律不变：仍然不许出现「第 0 轮」——轮次从 1 开始数，0 不是进度。
    """
    lines = _run_node()["progressText"]
    for index in (8,):
        line = lines[index]
        assert "进度不可用" not in line, f"用例 {index}：把「正在跑第一轮」说成了「看不见」"
        assert "第一轮还没跑完" in line, line
        assert "第 0 轮" not in line and "0 tokens" not in line, line


def test_a_finished_run_never_gets_a_running_line():
    """**这一条是那个矛盾的根。** 运行已经结束（成功或失败），

    进度那一行必须返回 `null`（「这次什么都别写」）—— 继续写「分析中」就是
    「失败」与「分析中：第 2/8 轮」同时挂在页面上的来源。
    """
    cases = _CASES["progressText"]
    lines = _run_node()["progressText"]
    for index, (progress, status) in enumerate(cases):
        if status not in ("succeeded", "failed"):
            continue
        assert lines[index] is None, (
            f"status={status} 时还在写进度行：{lines[index]}"
        )


def test_a_missing_usage_is_not_printed_as_zero():
    line = _run_node()["progressText"][7]
    assert line == "分析中：第 2/8 轮", line
    assert "0" not in line


def test_result_and_interruption_never_say_running():
    """收尾文案里不许留下「分析中」那半句（它是进度行的开头）。

    这一条用**同一个判据**扫所有收尾文案，比逐条抄文案更难糊弄过去：一个已经结束的
    界面上不该出现那句进度行的字头 —— 「分析中断」这种写法也一起挡掉。
    """
    result = _run_node()
    for outcome in result["resultOutcome"]:
        assert "分析中" not in outcome["meta"], outcome
        assert outcome["badge"] and outcome["tone"], outcome
    for outcome in result["errorOutcome"]:
        assert "分析中" not in outcome["meta"], outcome
    for index, outcome in enumerate(result["interruptOutcome"]):
        if outcome["keepPolling"]:
            # 还敢说「分析中」的时候，必须把「连接已经断了」一起说出来
            # （只说「分析中」就是半个真相，用户会以为一切正常）。
            if outcome["badge"] == "分析中":
                assert "连接中断" in outcome["meta"], (index, outcome)
            else:
                assert "分析中" not in outcome["meta"], (index, outcome)
        else:
            assert "分析中" not in outcome["meta"], (index, outcome)
            assert outcome["badge"] != "分析中", (index, outcome)


def test_a_failed_result_carries_the_real_reason():
    outcomes = _run_node()["resultOutcome"]
    assert outcomes[0]["failed"] is True
    assert "ConnectionError" in outcomes[0]["reason"], outcomes[0]
    assert outcomes[0]["badge"] == "失败" and outcomes[0]["tone"] == "danger"
    # 服务端没给原因时也不许编：写「原因未知」，而不是留空或说「成功」。
    assert outcomes[1]["failed"] is True
    assert outcomes[1]["reason"] == "原因未知", outcomes[1]
    # 成功那一档是「已落库」：`result` 事件在写库之后才发，这句话说得起。
    assert outcomes[2]["failed"] is False
    assert "已落库" in outcomes[2]["meta"], outcomes[2]


def test_a_server_error_before_the_run_started_is_not_called_a_failure():
    """服务端只在闸门放行、运行记录建好之后才发 `run`，那时**一个请求都还没发出去**。

    所以「没见到 `run`」= 「没发起、没花钱」——徽章不该是「失败」，
    否则用户会以为自己这次花掉的钱打了水漂。
    """
    outcomes = _run_node()["errorOutcome"]
    budget, missing_key = outcomes[0], outcomes[1]
    for outcome in (budget, missing_key):
        assert outcome["badge"] == "未开始", outcome
        assert outcome["tone"] == "warning", outcome
        assert "没有产生消耗" in outcome["meta"], outcome
    assert "预算" in budget["output"], budget
    assert missing_key["output"] == "Project API key not configured.", missing_key
    # 已经跑过一段再挂：这才是失败，原因照原样带出来。
    assert outcomes[2]["badge"] == "失败" and outcomes[2]["tone"] == "danger"
    assert "RuntimeError" in outcomes[2]["output"]


def test_a_dropped_connection_is_not_reported_as_a_failure():
    """**这一条就是「AI 分析失败或连接中断」那句话的拆解。**

    分析跑在服务端的生成器里，浏览器这头断线不会让它停下来 —— 除了服务端自己说
    「这次失败了」，一律不许打「失败」。
    """
    outcomes = _run_node()["interruptOutcome"]
    running, pending, succeeded, failed, unknown, never_started, _ = outcomes

    for outcome in (running, pending):
        assert outcome["badge"] == "分析中", outcome
        assert outcome["keepPolling"] is True, "分析还在跑，轮询不该停"
        assert outcome["refresh"] is False, outcome
        assert "仍在服务端继续" in outcome["output"], outcome

    for outcome in (succeeded, failed):
        assert outcome["badge"] != "失败", outcome
        assert outcome["refresh"] is True, "运行已经结束，该去取落库的结论"
        assert outcome["keepPolling"] is False, outcome

    assert unknown["keepPolling"] is True, "状态没读到，别放弃轮询"
    assert unknown["badge"] == "中断", unknown
    assert never_started["badge"] == "中断", never_started
    assert never_started["keepPolling"] is False, never_started


# ==========================================================================
#  二、停表必须收字（把报障那一幕真跑一遍）
# ==========================================================================


def test_the_progress_line_is_written_from_the_first_frame():
    steps = _run_node()["watches"][0]["steps"]
    assert steps[0] == REPORTED_LINE, steps


def test_stopping_the_watch_erases_the_progress_line():
    """**报障那一幕的直接修法。**

    以前 `stopAiBudgetWatch()` 只 `clearInterval`，留在 DOM 上的最后一帧没人擦 ——
    于是「失败」旁边永远挂着「分析中：第 2/8 轮」。
    """
    for index, watch in enumerate(_run_node()["watches"]):
        assert "分析中" not in watch["afterStop"], (
            f"场景 {index}：停表之后那行字还在：{watch['afterStop']!r}"
        )
        assert watch["cleared"] >= 1, f"场景 {index}：定时器没被清掉"


def test_a_finished_run_stops_the_watch_by_itself():
    """服务端说「这次失败了」时，停表与收字都不用等调用方（`onFinished` 也发出去）。

    这一条覆盖的是：SSE 断了，但轮询还活着 —— 它自己发现运行已经结束，
    界面于是不会永远停在第 2 轮。
    """
    watch = _run_node()["watches"][0]
    assert watch["finished"] == ["failed"], watch
    assert "分析中" not in watch["steps"][-1], watch["steps"]


def test_a_successful_run_also_stops_cleanly():
    watch = _run_node()["watches"][2]
    assert watch["finished"] == ["succeeded"], watch
    assert "分析中" not in watch["afterStop"], watch


def test_an_unreadable_progress_is_still_written_and_cleared():
    watch = _run_node()["watches"][3]
    assert "进度不可用" in watch["steps"][0], watch
    assert "分析中" not in watch["afterStop"], watch


def test_a_failing_poll_does_not_turn_into_a_zero():
    """轮询本身失败（网络断了）时**什么都不说**：下一帧还会试。

    这里最容易写错的是「失败就当成 0」——那会把「不知道」显示成「一次都没用」。
    """
    watch = _run_node()["watches"][4]
    assert watch["steps"][-1] == REPORTED_LINE, watch["steps"]
    assert "0 tokens" not in watch["steps"][-1]


# ==========================================================================
#  三、连接断了：先问服务端，再决定说什么（模板里那段真跑）
# ==========================================================================


def test_the_drop_path_asks_the_server_before_saying_anything():
    """断线之后的第一件事是**关掉 EventSource**。

    EventSource 默认会**自动重连**，而重连一条 SSE 等于让服务端再跑一次分析 ——
    那是要花钱的。
    """
    for index, drop in enumerate(_run_node()["drops"]):
        assert drop["closed"] is True, f"场景 {index}：连接没关，它会自己重连并再跑一次分析"
        assert drop["calls"], f"场景 {index}：断了之后什么都没说"


def test_a_still_running_analysis_keeps_polling_after_the_drop():
    drop = _run_node()["drops"][0]
    calls = dict((call[0], call) for call in drop["calls"] if call[0] != "meta")
    assert calls["badge"][1] == "分析中", drop["calls"]
    assert calls["badge"][2] == "primary", drop["calls"]
    assert "stopWatch" not in [call[0] for call in drop["calls"]], (
        "还在跑就把轮询停了：跑完的结论再也回不到界面上"
    )
    assert "refresh" not in [call[0] for call in drop["calls"]], drop["calls"]


def test_a_finished_analysis_reads_the_persisted_result():
    for index in (1, 2):
        drop = _run_node()["drops"][index]
        kinds = [call[0] for call in drop["calls"]]
        assert "refresh" in kinds, f"场景 {index}：运行结束了却没去取落库的结论：{drop['calls']}"
        badges = [call for call in drop["calls"] if call[0] == "badge"]
        assert badges[0][1] != "失败", (
            f"场景 {index}：连接断了但运行其实是好的，却报了失败：{badges}"
        )


def test_a_drop_without_a_run_id_does_not_pretend_to_know():
    """连运行号都没收到（连接在第一个事件之前就断了）：**不许猜**。"""
    drop = _run_node()["drops"][4]
    badges = [call for call in drop["calls"] if call[0] == "badge"]
    assert badges[0][1] == "中断", drop["calls"]
    output = [call for call in drop["calls"] if call[0] == "output"][0]
    assert "运行号" in output[1], output


# ==========================================================================
#  四、三份模板的接线（静态守卫）
# ==========================================================================


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_drawer_loads_the_shared_module(name):
    assert "js/ai_stream_status.js" in _read(name), f"{name} 没有引用共享的状态行模块"


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_template_writes_the_progress_line_itself(name):
    """那行状态字只许在共享模块里拼 —— 抄在模板里就一定会漂。

    判据是「模板里不许再出现那段插值」：句式一模一样地抄三遍正是这个缺陷的成因。
    """
    script = _template_script(name)
    assert "分析中：第 " not in script, f"{name} 又自己拼了一遍进度文案"
    assert "尚未落库" not in script, f"{name} 又把「尚未落库」抄了一份"
    assert "进度不可用" not in script, f"{name} 又自己写了一遍「进度不可用」"


@pytest.mark.parametrize("name", TEMPLATES)
def test_stopping_the_watch_goes_through_the_shared_stop(name):
    """停轮询必须走 `AiStreamStatus.watchRun(...)` 返回的那个 `stop`。

    它一次做完两件事：清定时器 + **擦掉那行字**。自己在模板里 `clearInterval`
    就是回到「只停表不收字」，也就是报障的那一幕。
    """
    script = _template_script(name)
    assert "AiStreamStatus.watchRun(" in script, f"{name} 没有用共享的轮询"
    stop_name = "stopBudgetWatch" if "function stopBudgetWatch()" in script else "stopAiBudgetWatch"
    stop_body = _function_body(script, stop_name)
    assert re.search(r"[Ww]atch\s*\.\s*stop\(\)", stop_body), (
        f"{name} 的停表没有走共享的 stop：{stop_body}"
    )
    assert "clearInterval" not in stop_body, (
        f"{name} 又自己 clearInterval 了一遍（就是「只停表不收字」）：{stop_body}"
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_generic_interruption_message_is_gone(name):
    """「AI 分析失败或连接中断」这句合称不许再出现在任何模板里。

    它把「服务端说失败了」与「浏览器这头断线了」合成一句，而这两件事的正确处置
    完全不同（一个要重跑，一个**绝不能**重跑）。说法现在全在共享模块里。
    """
    code = _template_script(name)
    assert "AI 分析失败或连接中断" not in code, f"{name} 里还留着那句合称"


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_error_handler_separates_the_two_kinds_of_error(name):
    """`error` 事件：有 `data` 的一律走共享的说法，没有的去问状态。"""
    script = _template_script(name)
    assert "AiStreamStatus.errorOutcome(" in script, f"{name} 没有用共享的失败口径"
    assert "AiStreamStatus.interruptOutcome(" in script, f"{name} 没有用共享的中断口径"
    assert "AiStreamStatus.fetchRunStatus(" in script, (
        f"{name} 断了之后没有回头问这次运行的状态 —— 那就只能靠猜，"
        "而原来猜出来的那句正是「AI 分析失败或连接中断」"
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_terminal_handlers_write_the_meta_line_after_closing_the_stream(name):
    """收尾的那行字必须在**关流之后**写。

    反过来的话，关流（它会擦掉那行字）会把刚写上的收尾说法一起擦掉，
    页面上就只剩空行 —— 一个「修好了但看不出来跑没跑完」的界面。
    """
    script = _template_script(name)
    for event in ("result", "error"):
        body = _handler_body(script, event)
        close = re.search(r"(closeWeeklyAiStream|cleanupStream)\(\)", body)
        assert close, f"{name} 的 {event} 出口没有关流：{body[:200]}"
        # 关流之后必须还有一次 meta 写入（结果那一路是靠 loadLatest 写入的，
        # 所以判据是「关流之后还有话要说」，而不是固定写成哪个函数）。
        tail = body[close.end():]
        assert ("setWeeklyAiMeta(" in tail or "setMeta(" in tail
                or "loadWeeklyAiLatest(" in tail or "loadLatestResult(" in tail), (
            f"{name} 的 {event} 出口关完流就没人写那行状态字了（会留一个空行）：{tail[:200]}"
        )


def test_the_reported_page_sets_its_meta_after_every_terminal_exit():
    """**用户报的那一页**：`result`（成功/失败）与两种 `error` 出口都要写 meta。

    这一页原来一个出口都没写，所以轮询留下的那一帧永远没人盖掉。
    """
    script = _template_script(REPORTED)
    assert script.count("setWeeklyAiMeta(") >= 3, (
        "报障页的收尾出口少于三处（result / 服务端 error / 连接断开）"
    )


# 「打开页面时这次运行已经在跑」那一支：三份模板的取数函数名与轮询入口各不相同。
_LATEST_LOADER = {
    "templates/merged_project_view.html": "refreshWeeklyAiLatest",
    "templates/weekly_version_diff.html": "loadWeeklyAiLatest",
    "templates/commit_diff_new.html": "loadLatestResult",
}
_WATCH_CALL = {
    "templates/merged_project_view.html": "startAiBudgetWatch(result.runId, meta)",
    "templates/weekly_version_diff.html": "startAiBudgetWatch(result.run_id, meta)",
    "templates/commit_diff_new.html": "startBudgetWatch(result.run_id)",
}


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_run_that_is_already_going_is_watched_not_just_declared_unreadable(name):
    """**报障的那一幕**：思考过程说「分析已结束」，结论标签还写着「分析中」。

    机制是两条读数据的路各自为政：

    * **结论区**只在这几个时刻读一次 `/latest`：打开抽屉、点「刷新结果」、SSE 结束时。
      页面打开时这次运行正在跑，于是徽章被写成「分析中」——然后**没有任何东西会再读
      第二次**。服务端跑完了，这页不知道；
    * **思考过程**是**点开那个标签时**才去取落库明细的（`AiThinkLog.ensureLoaded`），
      取到的是最新状态，于是它显示「分析已结束，下面是这次运行的逐轮记录。」

    两个标签就此互相矛盾，而且都不会自己好 —— 只有手动刷新页面或点「刷新结果」才行。

    修法就是让那个「进行中」的分支**接着看它**：轮询读到终态会走 `onFinished`，
    那里会去取落库的结论、把徽章换成完成/失败。判据是「那一支里调起了本页的轮询入口，
    而且看的是这一次运行」——只看形状不看行为，是因为这一支要 DOM，node 跑不起来
    （同一节里另外几条静态守卫也是这个理由）。
    """
    script = _template_script(name)
    body = _function_body(script, _LATEST_LOADER[name])

    assert "in_progress" in body, f"{name} 的 {_LATEST_LOADER[name]} 里找不到「进行中」那一支"
    branch = body[body.index("in_progress"):]
    branch = branch[: branch.index("return false;")]

    # 前提：这一支仍然如实说「读不到进度」（别为了收敛把这句删掉）
    assert "AiThinkLog.markExternalRun()" in branch, (
        f"{name} 的「进行中」那一支不再如实说「读不到进度」了：{branch[:300]}"
    )
    assert _WATCH_CALL[name] in branch, (
        f"{name} 的「进行中」那一支**只说了一句「读不到」就收工**了 —— "
        "这次运行跑完后页面上没有任何东西会再去读一次，结论区会永远停在「分析中」，"
        "而「思考过程」点开时取的是最新状态，两个标签就此矛盾（线上报障）。"
        f"期望这一句：{_WATCH_CALL[name]}"
    )


# ==========================================================================
#  五、服务端：一定以终态事件收尾
# ==========================================================================


def _sse_events(text: str) -> list:
    """把 SSE 文本拆成 `[(event, payload), …]`。

    `data` 是 `json.dumps` 出来的（中文转成 `\\uXXXX`），所以取出后必须 `json.loads`
    再搜中文 —— 直接在里面搜会永远为假，而失败信息看起来像「服务端没说话」。
    """
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, payload = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[len("data:"):].strip())
        events.append((name, payload))
    return events


def test_a_generator_that_raises_still_ends_with_a_terminal_event():
    """**服务端的兜底。** 异常要变成一条带原因的 `error` 事件，而不是静默断流。"""

    def _exploding():
        yield "event: chunk\ndata: {\"text\": \"前半段\"}\n\n"
        raise RuntimeError("boom")

    events = _sse_events("".join(end_with_a_terminal_event(_exploding())))

    assert events[0][0] == "chunk", "异常之前已经发出去的事件被吞掉了"
    assert events[-1][0] == "error", events
    assert "分析中断" in events[-1][1]["message"], events[-1]
    assert "RuntimeError" in events[-1][1]["message"], events[-1]


def test_a_healthy_stream_is_not_touched_by_the_guard():
    """反向自检：正常跑完的流**逐字**透传。

    多补一个 `error` 会把一次成功的分析显示成失败 —— 这是这层守卫最容易犯的错。
    """

    def _healthy():
        yield "event: run\ndata: {\"run_id\": 1}\n\n"
        yield "event: result\ndata: {\"status\": \"succeeded\"}\n\n"

    stream = _healthy()
    raw = "".join(stream)
    wrapped = "".join(end_with_a_terminal_event(_healthy()))
    assert wrapped == raw
    assert wrapped.count("event: error") == 0


# ---------------------------------------------------------------------------
#  五之二：把真实的周版本流驱动起来
# ---------------------------------------------------------------------------


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _fixture() -> tuple:
    """建一个项目 + 仓库 + 周版本配置 + 假密钥，返回 `(project_id, config_id)`。"""
    create_tables()
    project = Project(code=_uid("P"), name=_uid("stream"))
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id, name=_uid("code"), type="git",
        url=f"https://example.com/{_uid('r')}.git", branch="main",
        resource_type="code", clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    config = WeeklyVersionConfig(
        project_id=project.id, repository_id=repo.id, name=f"W1 - {_uid('week')}",
        description="", branch="main",
        start_time=datetime(2026, 3, 1), end_time=datetime(2026, 3, 8),
        cycle_type="custom", is_active=True, auto_sync=True, status="active",
    )
    db.session.add(config)
    db.session.flush()
    ai_service.set_project_api_key(project.id, "sk-test", updated_by="tester")
    db.session.commit()
    return project.id, config.id


def _stub_payload(project_id: int) -> None:
    """payload 的构造要读仓库/提交/缓存，这里只关心**流的收尾**。"""
    return None


def _login(client) -> None:
    """给 test_client 一个管理员会话（`/jobs/<id>/events` 按 job.project_id 判权）。"""
    with client.session_transaction() as session:
        session["is_admin"] = True
        session["admin_user"] = "drawer-stream-tester"


def _weekly_payload_stub(project_id: int, config_id: int) -> dict:
    return {
        "mode": "weekly",
        "scope": "full",
        "focus": {"key": "all", "label": ""},
        "group": {
            "project_id": project_id,
            "key": f"g-{config_id}",
            "config_ids": [config_id],
        },
        "summary": {},
    }


def _open_a_job(config_id: int):
    """建一条手工 job（P0-01 之后手工入口就是它），返回那条 job。"""
    config = db.session.get(WeeklyVersionConfig, config_id)
    created = job_service.create_or_attach_job(
        config=config, requested_mode=MODE_INCREMENTAL, trigger_source="manual"
    )
    db.session.commit()
    return created.job


def _drop_job(job_id) -> None:
    """收掉这条 job（`active_key` 非空会占着唯一索引，让别的用例建不出 job）。

    ## 必须**连它名下的任务行一起**收掉，否则会造出「幽灵任务」

    只删 job 的话，那行 `BackgroundTask`（`job_id` 指着已删的 job、状态还停在
    `pending`/`processing`）会留在库里。job 行一删，它的 id 就被 SQLite 复用 ——
    于是那行幽灵任务会挂到**一条全新的、与它毫无关系的 job** 上。

    而 `job_service._live_task_of_job(job)` 是**按裸 `BackgroundTask.job_id == job.id`
    匹配**的（它必须这样：等待意图转交出去的那条任务，`job.task_id` 上根本没有它），
    所以一条无关的新 job 会被恢复扫描判成「还有活在跑」，判据 ③ 返回「不动它」。

    实测形态 —— **单独跑绿、合跑红**：

        pytest tests/test_ai_drawer_stream_state.py \\
               tests/test_ai_job_settlement_and_scope.py::test_the_scan_settles_an_abandoned_job_with_no_task_and_no_run

        E  AssertionError: {'checked': 1, 'settled': 0, 'by_reason': {}}
        E  assert 0 >= 1

    （`checked: 1` 就是「整个库里只剩那一条新 job」，而它被幽灵任务挡住了。）
    隔离复现见 `.pytest_tmp/probe_task_leak.py` 记下的机制：老 job id=1 → 删 job →
    新 job 拿到 id=1 → `_live_task_of_job` 命中那行老任务。
    """
    row = db.session.get(AiAnalysisJob, job_id)
    if row is None:
        return
    if row.active_key is not None:
        row.active_key = None
        db.session.commit()
    # 先子后父：任务行`job_id` 指着这条 job。
    for task in BackgroundTask.query.filter_by(job_id=job_id).all():
        db.session.delete(task)
    db.session.delete(row)
    db.session.commit()


def test_a_failed_weekly_run_delivers_a_result_with_the_reason(monkeypatch):
    """**用户报的那条**：分析中途炸了，界面收到的必须是一个**带原因的结论**。

    以前这里什么都没有：异常穿出生成器 → 连接静默断掉 → 界面只剩那句合称，
    库里那条 run 还停在 `running`。

    P0-01 之后周版本那条流的入口是 `GET /ai-analysis/jobs/<id>/events`（**只订阅**），
    终结帧由 `job_service.result_payload(job)` 给 —— 所以这一条现在同时钉住
    「失败要落库」与「终态 job 的订阅流把那份失败交出去」。
    """
    with flask_app.app_context():
        project_id, config_id = _fixture()
        monkeypatch.setattr(
            ai_service, "build_weekly_payload",
            lambda *a, **k: (_weekly_payload_stub(project_id, config_id), None, None),
        )

        def _explode(*_args, **_kwargs):
            raise ConnectionError("上游把连接掐了")

        monkeypatch.setattr(ai_service, "_run_engine_and_persist", _explode)

        job = _open_a_job(config_id)
        job_id = job.id
        try:
            assert job.task_id, "job 没有排出去任务，这一跳跑不起来"
            worker._handle_weekly_ai_analysis_task(
                {"type": "weekly_ai_analysis", "config_id": config_id,
                 "task_id": job.task_id}
            )
            db.session.commit()

            with flask_app.test_client() as client:
                _login(client)
                text = client.get(
                    f"/ai-analysis/jobs/{job_id}/events"
                ).get_data(as_text=True)

            events = _sse_events(text)
            assert events[-1][0] == "result", (
                f"流没有以 result 收尾（界面只能看到「连接中断」）：{events}"
            )
            payload = events[-1][1]
            assert payload["status"] == "failed", payload
            assert "ConnectionError" in payload["error_message"], payload
            assert "上游把连接掐了" in payload["error_message"], payload

            run = db.session.get(AiAnalysisRun, payload["run_id"])
            db.session.refresh(run)
            assert run.status == "failed", "异常之后库里那条 run 还停在 running"
            assert "ConnectionError" in (run.error_message or ""), run.error_message
        finally:
            _drop_job(job_id)


def test_the_job_events_stream_ends_with_an_error_when_the_stream_explodes(monkeypatch):
    """流自己炸了：**兜底的一层要说话**。

    没有它，客户端只会看到连接断掉，而那句话在界面上就是含糊的「与服务器的连接中断了」
    —— 真实原因要跟着最后一条事件发出去。生产上包住这条流的是路由里的
    `end_with_a_terminal_event`（`_job_events_response`），所以这里走**真路由**。
    """
    with flask_app.app_context():
        _project_id, config_id = _fixture()
        job = _open_a_job(config_id)
        job_id = job.id
        try:
            # 摆成终态：终态才会去取 `result` 载荷，那一步正是要炸的地方。
            row = db.session.get(AiAnalysisJob, job_id)
            row.state = STATE_SUCCEEDED
            db.session.commit()

            def _explode(_job):
                raise RuntimeError("载荷取不出来")

            monkeypatch.setattr(job_service, "result_payload", _explode)

            with flask_app.test_client() as client:
                _login(client)
                text = client.get(
                    f"/ai-analysis/jobs/{job_id}/events"
                ).get_data(as_text=True)

            events = _sse_events(text)
            assert events[-1][0] == "error", events
            assert "载荷取不出来" in events[-1][1]["message"], events[-1]
            assert "RuntimeError" in events[-1][1]["message"], events[-1]
        finally:
            _drop_job(job_id)


def test_the_routes_wrap_both_streams():
    """两个 SSE 入口都要包上兜底 —— 只在服务层包容易漏掉新加的入口。

    **P0-01 之后这两个入口换了**：单提交那条流式分析，与**只订阅**的 job 事件流
    （`/ai-analysis/jobs/<id>/events`）。周版本那条老流式入口不再创建任何东西
    （创建在 `POST /ai-analysis/weekly/<id>/jobs`，订阅在 `/jobs/<id>/events`），
    所以它已经不在这两个入口里了 —— 那一条见 `tests/test_ai_job_protocol.py`。
    """
    source = (PROJECT_ROOT / "routes" / "ai_analysis_routes.py").read_text(encoding="utf-8")
    assert source.count("end_with_a_terminal_event(") == 2, (
        "两个 SSE 入口各要包一次（这是调用次数，import 那一行不算）"
    )
    for call in ("stream_commit_analysis(commit_id", "job_event_frames("):
        assert call in source, f"入口不见了：{call}"


def test_the_failure_path_does_not_throw_itself(monkeypatch):
    """连失败都报不出来的情况要挡住：落库炸了也只剩日志，不许再抛。"""
    with flask_app.app_context():
        project_id, _config_id = _fixture()
        run = AiAnalysisRun(
            project_id=project_id, target_type="weekly", target_id=project_id,
            target_key="k", status="running", scope="full", trigger_source="manual",
        )
        db.session.add(run)
        db.session.commit()

        def _explode(*_args, **_kwargs):
            raise RuntimeError("引擎炸了")

        def _persist_explodes(*_args, **_kwargs):
            raise RuntimeError("落库也炸了")

        monkeypatch.setattr(ai_service, "_run_engine_and_persist", _explode)
        monkeypatch.setattr(ai_service, "_persist_outcome", _persist_explodes)

        result = ai_service._execute_analysis(
            run, project_id=project_id, payload={"summary": {}},
            project_config={}, target_type="weekly", target_key="k",
        )

        assert result["status"] == "failed", result
        assert "引擎炸了" in result["error_message"], result


class TestTheSubagentProgressLine:
    """子代理模式下那一行要说出「现在是哪一片在跑」。

    只看轮次会误读：「第 1 轮」跑了两分钟，究竟是第一个分片刚起步、还是已经在汇总了，
    从字面上完全看不出来 —— 而这两种情况下用户该做的事不一样（等 vs 快好了）。
    """

    def test_a_member_is_named_with_its_position(self):
        text = _run_node()["progressText"][9]

        assert "分片 S1 (1/3)" in text, text
        assert "第 2/4 轮" in text, text
        # token 数原样带出（这个模块不做千分位；那是服务端下发的形状）
        assert "5000 tokens" in text, text

    def test_the_synthesis_is_called_what_it_is(self):
        text = _run_node()["progressText"][10]

        assert "分片 汇总 (4/4)" in text, text
        assert "tokens" not in text, "用量没上报时不许补一个 0"

    def test_without_slices_the_line_is_unchanged(self):
        """没开子代理时那一行与以前**一字不差**（这是回归的护栏）。"""
        text = _run_node()["progressText"][11]

        assert text.startswith("分析中：第 3/8 轮"), text
        assert "分片" not in text, text


def _on_finished_body(script: str) -> str:
    """`onFinished: function () { … }` 的回调体（按花括号配平）。

    它不是 `function onFinished(...)` 那种声明，而是**对象字面量里的一个属性**
    （`AiStreamStatus.watchRun(id, { onFinished: function () {…} })`），
    所以 `_function_body` 找不到它。
    """
    marker = "onFinished: function"
    start = script.index(marker)
    brace = script.index("{", start)
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[brace: index + 1]
        index += 1
    raise AssertionError("找不到 onFinished 的回调体")


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_polling_exit_also_settles_the_drawer(name):
    """轮询自己发现跑完时，抽屉也要**收摊**（切回结论 / 打未读点）。

    `stopAiBudgetWatch()` 的注释写着「停轮询必须走它…所有出口都走 `stopAiBudgetWatch`，
    在每处各写一遍必然漏一处」—— 而 `onFinished` 就是漏掉的那一处：它只调了
    `AiThinkLog.unwatch()`。后果不是报错，是**跑完了用户看不出来**：抽屉停在被手动点过
    的「思考过程」上，结论那一页也不会收到未读点。

    判据三条：这个出口要走到停表；停表要**等结论取回来之后**再做（先切标签再取结论的话，
    「完整结论」那一页会先露一下上次留下的「AI 分析进行中...」，而这次明明已经结束了）；
    而且那次停表必须挂在取结论的 `.then` 里 —— 顺序断言只看得见「谁写在前面」，
    真正保证「等到了」的是这个回调。
    """
    script = _template_script(name)
    body = _on_finished_body(script)

    settle = "stopAiBudgetWatch()" if "stopAiBudgetWatch" in script else "stopBudgetWatch()"
    loader = _LATEST_LOADER[name]

    assert settle in body, (
        f"{name} 的 `onFinished` 没有让抽屉收摊 —— 用户手动停在「思考过程」时，"
        f"这次运行跑完了既不会被切回结论、也拿不到未读点。期望一次 {settle}"
    )
    assert body.index(loader) < body.index(settle), (
        f"{name} 的 `onFinished` 把停表写在了取结论前面 —— 「完整结论」那一页会先露一下"
        "上次留下的「AI 分析进行中...」，而这次已经结束了"
    )
    then_at = body.index(".then(")
    assert body.index(settle, then_at) > then_at, (
        f"{name} 的停表没有等到结论取回来才做（不在取结论的 `.then` 里）：{body[-400:]}"
    )


# 「回读最近结论失败」那一段：三份模板的写法各不相同，但**都必须在那句错误文案之后收工**。
_RELOAD_FAILURE_TEXT = "获取最近分析失败。"
_NO_RESULT_TAIL = "暂无分析结果。"


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_failed_reread_does_not_become_no_result_at_all(name):
    """**取不到最近结论 ≠ 这个目标没跑过分析。**

    跑完那一刻 `onFinished` 会回读一次 `/latest`（用户点「刷新结果」也是同一条路）。
    这一次请求一旦失败（网络抖动、403、500），函数里那段「没有结论」的收尾就会接管：
    界面上是「暂无分析结果。」+ 徽章「待分析」，`AiThinkLog.markEmpty()` 还会把刚画出来
    的逐轮记录一起清掉 —— 用户看着报告一行行推完，界面却告诉他「这个目标还没有跑过分析」。
    更荒唐的是周版本页那句注释写着「要传 true……否则会落进函数末尾那句『暂无分析结果。』」，
    而传了 `true` 之后那段 catch 里**没有 `return`**，照样落下去。

    判据是**顺序**：把函数从「没有结论」那句收尾处切开，错误文案必须出现在**最后一个
    `return` 之前** —— 也就是「说完失败就收工」。只在 catch 里写一句错误文案、然后让控制流
    继续往下走，这条会红。
    """
    script = _template_script(name)
    body = _function_body(script, _LATEST_LOADER[name])

    assert _NO_RESULT_TAIL in body, f"前提变了：{name} 里找不到「没有结论」那段收尾"
    assert _RELOAD_FAILURE_TEXT in body, (
        f"{name} 的 {_LATEST_LOADER[name]} 里没有「回读失败」这句说明 —— "
        "读不到结论时它会说成「暂无分析结果。」，把跑成功的一次说成没跑过"
    )
    prefix = body[: body.index(_NO_RESULT_TAIL)]
    tail_return = prefix.rindex("return")
    assert tail_return > prefix.index(_RELOAD_FAILURE_TEXT), (
        f"{name} 的「回读失败」分支说完那句错误文案就**继续往下走了** —— "
        "它会被函数末尾那段「没有结论」的收尾盖掉（「暂无分析结果。」+「待分析」），"
        "刚画出来的逐轮记录也会被 `markEmpty()` 清掉。取不到与确实没有是两件事，"
        "说完失败必须 `return`。"
    )


def test_the_merged_page_tells_a_failed_fetch_from_an_empty_one():
    """合并页连那句错误文案都没有：取数函数把「失败」和「没有结论」都吞成 `null`。

    于是它无法区分这两件事，`refreshWeeklyAiLatest` 也就只能一律说「暂无分析结果。」。
    失败要用 `undefined`（并**不写缓存** —— 把「没取到」缓存下来，之后所有非 force 的读
    都会跟着说「没有分析」，而它其实一直在库里）。
    """
    script = _template_script("templates/merged_project_view.html")
    body = _function_body(script, "getWeeklyAiResult")

    assert "return undefined;" in body, (
        "取数失败仍然回 `null` —— 与「这个目标没有结论」撞成同一个值，"
        "调用方没法分开说，只能一律报「暂无分析结果。」"
    )
    catch_at = body.index("catch")
    assert "weeklyAiCache.set" not in body[catch_at:], (
        "把「没取到」写进了缓存 —— 之后所有非 force 的读都会跟着说「没有分析」"
    )
    assert "undefined" in _function_body(script, _LATEST_LOADER[
        "templates/merged_project_view.html"
    ]), "调用方没有处理「没取到」这一支"


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_run_ended_exit_tells_the_think_log_that_it_ended(name):
    """跑完那个出口必须说清「这次运行结束了」（`unwatch({settled: true})`）。

    不带的后果：`unwatch()` 分不清「跑完了」与「用户不想看了」，于是两条路都会去取一次
    落库的逐轮。而逐轮是**跑完之后才落库**的，跑动中关抽屉取回的空表会被当成终态收下，
    把「这次运行没有留下逐轮记录」写死 —— 跑完再打开抽屉就再也刷不出来（细节见
    `tests/test_ai_think_log_frontend.py::test_closing_the_drawer_mid_run_does_not_freeze_
    the_trace_as_missing`）。这条只钉住三份模板确实把那个参数传下去了。
    """
    script = _template_script(name)
    body = _on_finished_body(script)

    assert "AiThinkLog.unwatch(" in body
    assert "settled: true" in body, (
        f"{name} 的 `onFinished` 调 `AiThinkLog.unwatch()` 时没说「这次运行结束了」—— "
        "它会顺手去取一次落库的逐轮，而那一刻逐轮还没落库"
    )


def test_the_start_frame_still_names_who_is_running():
    """开始那一帧要说得出**是谁在跑**，不能只报「正在调用模型」。

    子代理模式下这一帧正是「主代理开始分析了」的唯一播报机会：下一次回调要等汇总
    那一轮跑完，可能是几分钟之后。这段时间里如果那一行不写「汇总」，用户看到的就是
    上一个分片的名字 —— 也就是「思考过程不动了，状态还停在分片」那个现象。
    """
    lines = _run_node()["progressText"]

    assert lines[12] == "分析中：分片 汇总 (4/4) · 正在调用模型（第一轮还没跑完）", lines[12]
    assert lines[13] == "分析中：分片 S2 (2/3) · 正在调用模型（第一轮还没跑完）", lines[13]


# ==========================================================================
#  五、`waiting` 之后 close 又补一个 error：不许把「等待同步」覆盖成「中断」
#
#  这是复测文档 AI-P0-01 里那一幕：点击「重新分析」→ 服务端登记等待意图并发
#  `waiting` → 处理器置真 settled 标志并 `close()` → **浏览器仍可能补派一个没有
#  `data` 的连接层 `error`** → 执行流落到「连接断了」那条路 → 页面显示
#  「连接中断：没有收到运行号。」，而数据库里那条意图好端端地 pending 着。
#
#  下面两组测试合起来才算数：静态那组保证守卫**写在**处理器的开头，行为那组保证
#  它在 node 里真的挡住了、而且**没有把整条错误路一起废掉**（反向守卫那两个场景）。
# ==========================================================================

# 三份模板里那几个页面全局的名字各不相同（两份是 weekly 系列，一份是 commit 系列）。
_RACE_NAMES = {
    "templates/merged_project_view.html": {
        "settled": "weeklyAiStreamSettled",
        "run_id": "weeklyAiRunId",
        "source": "weeklyAiSource",
        "drop": "handleWeeklyAiStreamDrop",
        "close_stream": "closeWeeklyAiStream",
        "output": "setWeeklyAiOutput",
        "badge": "setWeeklyAiStatusBadge",
        "meta": "setWeeklyAiMeta",
    },
    "templates/weekly_version_diff.html": {
        "settled": "weeklyAiStreamSettled",
        "run_id": "weeklyAiRunId",
        "source": "weeklyAiSource",
        "drop": "handleWeeklyAiStreamDrop",
        "close_stream": "closeWeeklyAiStream",
        "output": "setWeeklyAiOutput",
        "badge": "setWeeklyAiStatusBadge",
        "meta": "setWeeklyAiMeta",
    },
    "templates/commit_diff_new.html": {
        "settled": "streamSettled",
        "run_id": "runId",
        "source": "eventSource",
        "drop": "handleStreamDrop",
        "close_stream": "cleanupStream",
        "output": "setAiOutput",
        "badge": "setAiStatusBadge",
        "meta": "setMeta",
    },
}

_RACE_SETTLED_PLACEHOLDER = "__SETTLED__"


def _race_probe_source(name: str) -> str:
    """把该模板里**逐字那一份** `error` 回调取出来，配一份页面全局桩。

    与 `_probe_source()` 同一个理由：抄一份进测试就失去意义了 —— 模板里把守卫删掉，
    抄来的那份还是绿的。
    """
    names = _RACE_NAMES[name]
    script = _template_script(name)
    stub = "\n".join([
        "var raceCalls = [];",
        f"var {names['settled']} = {_RACE_SETTLED_PLACEHOLDER};",
        f"var {names['run_id']} = null;",
        "var startBtn = { disabled: false };",
        f"var {names['source']} = {{ close: function () {{ raceCalls.push(['source.close']); }} }};",
        f"var {names['output']} = function (text) {{ raceCalls.push(['output', text]); }};",
        f"var {names['badge']} = function (text) {{ raceCalls.push(['badge', text]); }};",
        f"var {names['meta']} = function (text) {{ raceCalls.push(['meta', text]); }};",
        f"var {names['close_stream']} = function () {{ raceCalls.push(['closeStream']); }};",
        f"var {names['drop']} = function () {{ raceCalls.push(['drop']); }};",
    ])
    return "\n".join([
        stub,
        f"function __raceError(event) {_handler_body(script, 'error')}",
        "globalThis.__raceState = function () { return raceCalls; };",
    ])


_RACE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = { console: console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'ai_stream_status.js' });

const A = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = [];
for (const spec of A.races) {
    const src = A.probes[spec.template].split('__SETTLED__')
        .join(spec.settled ? 'true' : 'false');
    vm.runInContext(src, sandbox, { filename: 'race_probe.js' });
    // 连接层那个 `error`：**没有 `data`**（有 data 的是服务端主动发的 error 事件）。
    sandbox.__raceError(spec.data === null ? {} : { data: spec.data });
    out.push({ template: spec.template, settled: spec.settled, data: spec.data,
               calls: sandbox.__raceState() });
}
process.stdout.write(JSON.stringify({ races: out }));
"""

_RACE_RESULTS: dict = {}


def _race_cases() -> list:
    """三个场景 × 三份模板：已 settled 的迟到 error / 未 settled 的断线 / 服务端 error。"""
    return (
        [{"template": name, "settled": True, "data": None} for name in TEMPLATES]
        + [{"template": name, "settled": False, "data": None} for name in TEMPLATES]
        + [{"template": name, "settled": False, "data": '{"message": "已超出预算"}'}
           for name in TEMPLATES]
    )


def _run_race_node() -> list:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RACE_RESULTS:
        return _RACE_RESULTS["races"]
    workdir = Path(PROJECT_ROOT) / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_settled_race_driver.js"
    payload = workdir / "ai_settled_race_cases.json"
    driver.write_text(_RACE_DRIVER, encoding="utf-8")
    payload.write_text(
        json.dumps({"probes": dict((n, _race_probe_source(n)) for n in TEMPLATES),
                    "races": _race_cases()}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(driver), str(PROJECT_ROOT / MODULE), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"模板里的 error 回调在 node 里跑不起来：\n{result.stdout}\n{result.stderr}"
    )
    _RACE_RESULTS.update(json.loads(result.stdout))
    return _RACE_RESULTS["races"]


def _race_of(scene: int, name: str) -> dict:
    return _run_race_node()[scene * len(TEMPLATES) + TEMPLATES.index(name)]


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_error_handler_bails_out_once_the_stream_is_settled(name):
    """守卫必须在处理器的**最开头**，而且要挡在「连接断了」那条路之前。

    只挡住「有 `data`」那一支是不够的：要修的那一幕里 `event.data` 是空的。
    """
    script = _template_script(name)
    settled = _RACE_NAMES[name]["settled"]
    drop = _RACE_NAMES[name]["drop"]
    body = _handler_body(script, "error")

    guard = re.search(rf"if \({settled}\)\s*return;", body)
    assert guard, (
        f"{name} 的 error 处理器没有 settled 守卫 —— `waiting` 里 close 之后浏览器补的"
        f"那个无 data 的 error 会把「等待同步」覆盖成「连接中断：没有收到运行号」"
    )
    fallthrough = re.search(rf"\b{drop}\(", body)
    assert fallthrough, f"{name} 的 error 处理器没有落到 {drop}"
    assert guard.start() < fallthrough.start(), (
        f"{name} 的 settled 守卫排在 {drop} 之后，挡不住那一幕"
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_late_connection_error_writes_nothing_after_a_terminal_event(name):
    """**报障那一幕真跑一遍**：settled 之后再来的连接层 error 不许写任何东西。

    判据是「什么都没调」—— 包括不许去问运行状态（`fetchRunStatus` 会被
    `interruptOutcome` 变成那句「没有收到运行号」）。
    """
    race = _race_of(0, name)
    assert race["settled"] is True and race["data"] is None, race
    assert race["calls"] == [], (
        f"{name}：收到终结事件之后又被那个连接层 error 写了一遍 —— {race['calls']}"
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_connection_error_without_a_terminal_event_still_asks_the_server(name):
    """反过来那一条：**没 settled** 的断线照旧要处理（守卫不是把整条路废掉）。"""
    race = _race_of(1, name)
    assert race["settled"] is False and race["data"] is None, race
    kinds = [call[0] for call in race["calls"]]
    assert "drop" in kinds, (
        f"{name}：没 settled 的断线被吞掉了（分析还在跑，界面却什么都不说）—— {race['calls']}"
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_server_side_error_still_goes_through_the_shared_wording(name):
    """带 `data` 的服务端 `error` 走共享口径，**不落到断线那条路**。"""
    race = _race_of(2, name)
    kinds = [call[0] for call in race["calls"]]
    assert "drop" not in kinds, (
        f"{name}：服务端发的 error 被当成断线处理了 —— {race['calls']}"
    )
    assert "badge" in kinds and "meta" in kinds, (
        f"{name}：服务端发的 error 没有写出失败口径 —— {race['calls']}"
    )


# ==========================================================================
#  六、没有运行号时**先问服务端**，别猜（复测文档那一幕的真正失效模式）
#
#  真浏览器实测（三份探针，见报告）：正常路径下 `waiting` 事件会到达处理器、
#  而 `waiting` 处理器里同步调用 `close()` 之后浏览器**不再**补发 `error` ——
#  也就是说，只要那条事件到了，页面就不会被覆盖。
#
#  反过来说，**那条事件没到**的时候（代理截断、标签页挂起、连接被中间设备抢先掐断），
#  `weeklyAiRunId` 是 null，页面只能猜，而猜出来的那句
#  「连接中断：没有收到运行号」与真相相反：库里那次点击正 pending 着等同步。
#
#  真相在服务端（`describe_waiting_analysis`）。下面三条就钉「先问再说话」。
# ==========================================================================


def test_a_drop_without_a_run_id_asks_the_server_about_the_waiting_intent():
    """**报障那一幕**：没有运行号 + 服务端说「正登记着等同步」→ 显示等待，不许说中断。

    修复前这条会红：`interruptOutcome(null, false)` 打出
    「连接中断：没有收到运行号。」，把一次已经成功登记的点击说成中断。
    """
    drop = _run_node()["drops"][5]
    calls = dict((call[0], call) for call in drop["calls"] if call[0] != "meta")
    assert "badge" in calls, drop["calls"]
    assert calls["badge"][1] == "等待同步", (
        f"服务端说这次点击正等着同步，页面却说成别的：{drop['calls']}"
    )
    metas = [call[1] for call in drop["calls"] if call[0] == "meta"]
    assert any("等待" in (text or "") for text in metas), drop["calls"]
    assert not any("连接中断" in (text or "") for text in metas), (
        f"页面把「等同步」说成了「连接中断」—— 正是要修的那一幕：{drop['calls']}"
    )
    # 登记还在等 → 必须接着轮询登记状态（否则它转交的那一刻没人接得上）。
    assert "waitingPoll" in [call[0] for call in drop["calls"]], drop["calls"]


def test_a_drop_without_a_run_id_attaches_when_the_intent_was_handed_off():
    """登记已经转交给某一次运行 → **附着上去**，不是让用户再点一次（会再花一次钱）。"""
    drop = _run_node()["drops"][6]
    kinds = [call[0] for call in drop["calls"]]
    assert "refresh" in kinds, (
        f"服务端说登记已经转交给运行 42 了，页面却什么都没做（用户会再点一次）：{drop['calls']}"
    )
    refresh = [call for call in drop["calls"] if call[0] == "refresh"][0]
    assert refresh[1] == 42 or refresh[1] is None, refresh
    assert not any(
        "连接中断" in (call[1] or "") for call in drop["calls"] if call[0] == "meta"
    ), drop["calls"]


def test_a_drop_without_a_run_id_stays_honest_when_the_intent_ended():
    """登记确实结束了（不是等待、也没转交）→ 照旧如实说不确定，**不许编一个结论**。"""
    drop = _run_node()["drops"][7]
    metas = [call[1] for call in drop["calls"] if call[0] == "meta"]
    assert metas, drop["calls"]
    # 「不知道」仍然要说成不知道（那句「没有收到运行号」在这里是**对的**）。
    assert any("运行号" in (text or "") or "中断" in (text or "") for text in metas), (
        f"登记已结束，页面却没说清这次到底有没有跑起来：{drop['calls']}"
    )
