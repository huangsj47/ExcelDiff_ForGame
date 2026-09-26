# -*- coding: utf-8 -*-
"""「历次结论」弹层 —— 用 node 真跑 `static/js/ai_report_history.js`。

## 为什么必须真跑

这段代码的每一处判定都「看起来对」：

* 打开时该选中哪一份（**屏幕上正在显示的那一份**，不是最新那份）；
* 列表只有一次结论时该说什么（「还没有可对比的历史」，而不是摆一张一行的表）；
* 选中历史条目之后，「导出这一份」指向的必须是**选中的那一条**，不是抽屉 footer 上那个；
* 点得快一点（连着点两条）时，先回来的那份报告不许盖住后选的那条。

静态断言证不了这些，所以按仓库既有做法把真函数放进带假 DOM 的 node 沙箱里跑，
断言**每一步之后的界面结构**（列表行、选中态、标记文案、导出链接的 href）。

## 假 DOM

只实现这段代码用到的那几样：`getElementById` / `createElement` / `createTextNode` /
`appendChild` / `textContent` / `className` / `setAttribute` / `getAttribute` /
`removeAttribute` / `addEventListener` / `focus`。`dump()` 把节点树摊成文本，断言就写在那上面
—— 比逐个属性断言更接近「用户看到的是什么」。

**每个节点带一个 `_uid`**（连续编号）与一个 `_focused` 标记：2026-09-25 起还要断言两件
文本上看不出来的事 —— 「换选中时**没有重建列表**」（重建出来的树 dump 出来一模一样，
只有节点**身份**能分辨）与「方向键走过来时焦点**真的跟着走了**」。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_report_history.js"

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

// 三份模板里那两个 id —— 硬写在这里是**故意的**：它们是模板与模块的共同契约。
var MODAL_ID = 'aiReportHistoryModal';
var BODY_ID = 'aiReportHistoryBody';
var DRAWER_FOOTER_LINK = 'aiExportMdLink';   // 抽屉 footer 那个（**不是**弹层里这个）

var uidSeq = 0;

function makeEl(tag) {
    var node = {
        tagName: tag, id: '', className: '', type: '', hidden: false,
        children: [], _attrs: {}, _handlers: {}, _text: '', _html: '',
        _uid: (uidSeq += 1), _focused: false
    };
    // **`textContent` 的 setter 必须清空子节点** —— 真 DOM 就是这样，而模块靠这条
    // 重画列表（`body.textContent = ''`）。假 DOM 少了这一句，断言会看到上一次的残留。
    Object.defineProperty(node, 'textContent', {
        get: function () { return node._text; },
        set: function (value) { node._text = String(value); node.children = []; }
    });
    Object.defineProperty(node, 'innerHTML', {
        get: function () { return node._html; },
        set: function (value) { node._html = String(value); node.children = []; }
    });
    node.appendChild = function (child) { node.children.push(child); return child; };
    node.setAttribute = function (k, v) { node._attrs[k] = String(v); };
    node.getAttribute = function (k) {
        return node._attrs[k] === undefined ? null : node._attrs[k];
    };
    node.removeAttribute = function (k) { delete node._attrs[k]; };
    node.addEventListener = function (t, fn) { node._handlers[t] = fn; };
    // 真 DOM 里 `focus()` 会把焦点从别人身上拿走。这里只记「谁被我点过」——
    // 够断言「方向键之后焦点落在新选中的那一条上」，也不必模拟 document.activeElement。
    node.focus = function () {
        clearFocus(els[BODY_ID]);
        node._focused = true;
    };
    return node;
}

function clearFocus(node) {
    if (!node) return;
    if (node._focused) node._focused = false;
    (node.children || []).forEach(clearFocus);
}

function hasClass(node, cls) {
    return (' ' + String(node.className || '') + ' ').indexOf(' ' + cls + ' ') !== -1;
}

/** 先序遍历，`visit` 返回真值就停。 */
function walk(node, visit) {
    if (!node) return null;
    var hit = visit(node);
    if (hit) return hit;
    var kids = node.children || [];
    for (var i = 0; i < kids.length; i += 1) {
        hit = walk(kids[i], visit);
        if (hit) return hit;
    }
    return null;
}

function byClass(root, cls) {
    return walk(root, function (n) { return hasClass(n, cls) ? n : null; });
}

function byId(root, id) {
    return walk(root, function (n) { return n.id === id ? n : null; });
}

function allByClass(root, cls) {
    var out = [];
    walk(root, function (n) { if (hasClass(n, cls)) out.push(n); return null; });
    return out;
}

function makeText(text) {
    return {tagName: '#text', textContent: text, children: []};
}

var els = {};
function seedEls() {
    els = {};
    els[MODAL_ID] = makeEl('div');
    els[BODY_ID] = makeEl('div');
    // 抽屉 footer 上那个链接也在页面上：弹层**不许**动它（它导的是抽屉里那份结论）。
    els[DRAWER_FOOTER_LINK] = makeEl('a');
    els[DRAWER_FOOTER_LINK].setAttribute('href', '/ai-analysis/runs/1/report.md');
}

function dump(node, depth) {
    if (!node) return '';
    depth = depth || 0;
    var pad = '  '.repeat(depth);
    if (node.tagName === '#text') return pad + '"' + node.textContent + '"';
    var body = node.textContent || node.innerHTML;
    var head = pad + '<' + node.tagName + (node.className ? ' class="' + node.className + '"' : '')
        + (node.id ? ' id="' + node.id + '"' : '')
        + (node.getAttribute('href') ? ' href="' + node.getAttribute('href') + '"' : '')
        + '>' + (body ? ' ' + body : '');
    var lines = [head];
    node.children.forEach(function (child) { lines.push(dump(child, depth + 1)); });
    return lines.join('\n');
}

seedEls();

var sandbox = { window: {}, console: console };
sandbox.window = sandbox;
sandbox.document = {
    // 与真 DOM 一样：没有的 id 返回 null（**不按需造**）。
    getElementById: function (id) { return els[id] || null; },
    createElement: function (tag) { return makeEl(tag); },
    createTextNode: function (text) { return makeText(text); }
};
// 报告渲染器：真实现会先转义再套白名单。这里只记下「它被调过、用的是哪段文本」。
sandbox.AiReportMarkdown = {
    render: function (text) { return '<rendered>' + String(text).length + '</rendered>'; }
};
// 屏幕上「这一次」是谁（真实现里由 AiThinkLog 提供）。
var currentRunId = null;
sandbox.AiThinkLog = {currentRunId: function () { return currentRunId; }};

// 页面上的 `fetch`：**整行的点击处理函数走的就是它**（那里传不了自己的实现，
// 与真页面一致）。读的是调用那一刻的 `fetchTable`（`reset` 会换掉它）。
sandbox.fetch = function (url, options) { return makeFetch(fetchTable)(url, options); };

// 原生确认框。**默认同意**（用例要拒的那一档自己把它按下去）—— 与真浏览器不同，
// 这里是可控的：`confirmLog` 记下每一次问的是什么，好断言「动手之前问过没有」。
var confirmAnswer = true;
var confirmLog = [];
sandbox.confirm = function (text) {
    confirmLog.push(String(text));
    return confirmAnswer;
};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiReportHistory;

// 假 fetch：按 URL 返回预置的响应（`__FETCH__`）。
var fetchLog = [];   // 读请求（**只记 GET**，写请求进 `postLog` —— 两类问题的判据不同）
var postLog = [];    // 写请求（删除）：地址、方法与 body
function makeFetch(table) {
    return function (url, options) {
        var method = (options && options.method) || 'GET';
        if (method === 'GET') {
            fetchLog.push(url);
        } else {
            postLog.push({url: url, method: method, body: options.body || ''});
        }
        var entry = table[url];
        if (entry === undefined) {
            return Promise.reject(new Error('没有预置这个地址：' + url));
        }
        if (entry === 'reject') {
            return Promise.reject(new Error('连接失败'));
        }
        return Promise.resolve({
            ok: entry.ok === undefined ? true : entry.ok,
            status: entry.status || 200,
            json: function () { return Promise.resolve(entry.body); }
        });
    };
}

var OP = {
    track: function (url) { api.track({historyUrl: url}); },
    open: function () { return api.open(makeFetch(fetchTable)); },
    // **不传 fetch**：走 `global.fetch`（与整行点击那条路同一条）——
    // 传自己的实现就等于把「生产路径上那个 fetch 存不存在」这件事绕过去了。
    select: function (runId) { return api.select(Number(runId)); },
    // **不 await** 的那一种：用来把「正在读这一份」那一刻截下来（报告永远不回来）。
    pending: function (runId) { api.select(Number(runId), makePendingFetch); },
    // 点整行（不是点某个按钮）：处理函数挂在这一行自己身上。
    clickItem: function (index) {
        var items = allByClass(els[BODY_ID], 'ai-history-item');
        items[Number(index)]._handlers.click({});
        // 处理函数的返回值被真 DOM 丢掉，这里也不等它 —— 等的是取数那一串
        // 微任务排干（`setTimeout` 是宏任务，排在所有微任务之后）。
        return new Promise(function (done) { setTimeout(done, 0); });
    },
    key: function (key) {
        var list = byClass(els[BODY_ID], 'ai-history-list');
        list._handlers.keydown({key: key, preventDefault: function () {}});
        return new Promise(function (done) { setTimeout(done, 0); });
    },
    // 删除：**走真路径**（`confirmDelete` 里那个 `global.confirm` 与 `global.fetch`），
    // 确认框按同意 / 拒绝两档分开。
    confirmDelete: function (runId) {
        confirmAnswer = true;
        return api.confirmDelete(Number(runId));
    },
    declineDelete: function (runId) {
        confirmAnswer = false;
        return api.confirmDelete(Number(runId));
    },
    // 直接点那个按钮（而不是调 API）：按钮只在该出现的时候出现，这条走的是真触发器。
    clickDelete: function () {
        var button = byId(els[BODY_ID], 'aiHistoryDeleteButton');
        if (!button) throw new Error('详情栏里没有删除按钮');
        confirmAnswer = true;
        button._handlers.click({});
        return new Promise(function (done) { setTimeout(done, 0); })
            .then(function () { return new Promise(function (done) { setTimeout(done, 0); }); });
    },
    // **连点两下**（不 await 第一下）：第二次必须一个请求都不发。
    doubleDelete: function (runId) {
        confirmAnswer = true;
        return Promise.all([
            api.confirmDelete(Number(runId)),
            api.confirmDelete(Number(runId))
        ]);
    },
    setCurrent: function (runId) { currentRunId = runId; }
};

var fetchTable = __FETCH__;
var HISTORY_URL = '/ai-analysis/commit/7/history';

function makePendingFetch(url) {
    fetchLog.push(url);
    return new Promise(function () {});     // 永远不 settle
}

// 场景可以只给自己那一份列表（「只有一次结论」「一次都没跑过」这两条要靠它）。
function tableFor(item) {
    if (!item.rows) return __FETCH__;
    var table = JSON.parse(JSON.stringify(__FETCH__));
    table[HISTORY_URL].body.runs = item.rows;
    table[HISTORY_URL].body.total = item.rows.length;
    table[HISTORY_URL].body.in_progress = !!item.in_progress;
    return table;
}

// 某一行的报告「读不到」（网络抖动）：列表**不许**因此被抹掉。
function withRejects(table, item) {
    var next = JSON.parse(JSON.stringify(table));
    (item.rejectRuns || []).forEach(function (runId) {
        next['/ai-analysis/runs/' + runId + '/report'] = 'reject';
    });
    // 删除被服务端拒（在途 409 / 分组的版本对不上 400）：那句话必须由服务端给，
    // 界面照原样显示 —— 前端按状态码自己编一句是另一套话术，迟早与后端分叉。
    (item.deleteFail || []).forEach(function (runId) {
        next['/ai-analysis/runs/' + runId + '/delete'] = {
            ok: false, status: item.deleteFailStatus || 409,
            body: {
                success: false, reason: 'in_flight',
                message: '这次分析还在进行中，不能删除 —— 等它跑完（或失败）再删。'
            }
        };
    });
    return next;
}

function snap() {
    var body = els[BODY_ID];
    var detail = byClass(body, 'ai-history-detail-pane');
    return {
        body: dump(body),
        // 详情区自己那一段（「正在读这一份」这类只在右栏里的话，看这一份）
        detail: dump(detail),
        // 左列**逐条的身份**：同一次打开里换选中，这些 _uid 必须一个都不变
        //（列表被重建时 dump 出来的文本一模一样，只有身份能分辨）
        itemUids: allByClass(body, 'ai-history-item').map(function (n) { return n._uid; }),
        listUid: (byClass(body, 'ai-history-list') || {})._uid || null,
        focusedUid: (walk(body, function (n) { return n._focused ? n : null; }) || {})._uid || null,
        selectedUid: (byClass(body, 'is-selected') || {})._uid || null,
        // `dump()` 只印 class/id/href，选中态的**属性**要另取（无障碍那几条靠它们）
        selectedCount: allByClass(body, 'is-selected').length,
        selectedAttrs: (function () {
            var node = byClass(body, 'is-selected');
            return node ? {
                role: node.getAttribute('role'),
                tabindex: node.getAttribute('tabindex'),
                ariaSelected: node.getAttribute('aria-selected')
            } : null;
        })(),
        optionCount: allByClass(body, 'ai-history-item').filter(function (n) {
            return n.getAttribute('role') === 'option';
        }).length,
        // roving tabindex：整张清单只有一条能 Tab 进来（`"0"`），其余都是 `-1`
        tabindexes: allByClass(body, 'ai-history-item').map(function (n) {
            return n.getAttribute('tabindex');
        }),
        listRole: (byClass(body, 'ai-history-list') || {getAttribute: function () { return null; }})
            .getAttribute('role'),
        ariaBusy: detail ? detail.getAttribute('aria-busy') : null,
        footerHref: els[DRAWER_FOOTER_LINK].getAttribute('href'),
        state: api.state(),
        // 弹层里那个导出链接（由 JS 建，带固定 id）
        historyHref: (function () {
            var found = null;
            (function walkId(node) {
                if (!node || found) return;
                if (node.id === 'aiHistoryExportMdLink') { found = node.getAttribute('href'); }
                (node.children || []).forEach(walkId);
            })(body);
            return found;
        })(),
        // 删除按钮：**在不在**（不是管理员 / 不是周版本时它不该出现）、写着什么、禁没禁用
        deleteButton: (function () {
            var node = byId(body, 'aiHistoryDeleteButton');
            if (!node) return null;
            return {
                text: (node.children || []).map(function (c) { return c.textContent; }).join(''),
                disabled: !!node.disabled
            };
        })(),
        // 删除的回执（成功 / 失败那一句），以及有没有被标成错误
        deleteNote: (function () {
            var node = byClass(body, 'ai-history-delete-note');
            if (!node) return null;
            return {text: node.textContent, isError: hasClass(node, 'is-error')};
        })(),
        // 「当前基线」标记：**逐行**给一遍（顺序与左列一致），好断言它落在哪一行上
        baselineTags: allByClass(body, 'ai-history-item').map(function (item) {
            return !!byClass(item, 'ai-history-baseline-tag');
        }),
        baselineTagText: (function () {
            var node = byClass(body, 'ai-history-baseline-tag');
            return node ? node.textContent : null;
        })(),
        confirms: confirmLog.slice(),
        posts: postLog.slice(),
        fetchLog: fetchLog.slice()
    };
}

function reset(item) {
    seedEls();
    uidSeq = 0;
    fetchLog = [];
    postLog = [];
    confirmLog = [];
    confirmAnswer = true;
    currentRunId = item.currentRunId === undefined ? null : item.currentRunId;
    fetchTable = withRejects(tableFor(item), item);
    vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiReportHistory;
}

var ROWS = __ROWS__;
var cases = __CASES__;
var results = [];

function step(item) {
    return item.ops.reduce(function (chain, op) {
        var parts = op.split(':');
        return chain.then(function () {
            return OP[parts[0]](parts.length > 1 ? parts[1] : undefined);
        }).then(function () {
            item.snaps.push(snap());
        });
    }, Promise.resolve());
}

function runAll() {
    var chain = Promise.resolve();
    cases.forEach(function (item) {
        item.snaps = [];
        chain = chain.then(function () {
            reset(item);
            return step(item);
        }).then(function () {
            results.push({name: item.name, snaps: item.snaps});
        });
    });
    return chain;
}

var pure = __PURE__.map(function (item) {
    reset({currentRunId: item.currentRunId});
    var at = item.at === undefined ? null : item.at;
    return {
        name: item.name,
        url: api.historyUrlFor(item.kind, item.id),
        exportHref: api.exportHref(item.runId),
        reportUrl: api.reportUrl(item.runId),
        listNote: api.listNote(item.payload),
        mark: api.markText(item.row, item.currentRunId),
        tone: api.statusTone(item.status),
        rowMeta: api.rowMeta(item.row || {}),
        current: api.isCurrentAt({run_id: item.runId}, item.currentRunId),
        deleteUrl: api.deleteUrl(item.runId),
        // 动手之前那句话：**基线那一档必须多一句**（它是这个功能的全部风险所在）。
        deleteAsk: item.askRow ? api.deleteConfirmText(item.askRow) : '',
        // 五个键各落哪一个下标（`-1` = 这个键不管）—— 首尾**不环绕**是这一格的判据。
        nav: ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Tab'].map(function (key) {
            return api.nextIndex(key, at, item.count);
        }).join(',')
    };
});

runAll().then(function () {
    process.stdout.write(JSON.stringify({cases: results, pure: pure, rows: ROWS}));
}).catch(function (error) {
    process.stderr.write(String(error && error.stack || error));
    process.exit(1);
});
"""


def _rows() -> list:
    return [
        {
            "run_id": 42, "created_at_display": "2026-09-12 19:13:04", "status": "succeeded",
            "status_label": "已有结论", "risk_label": "高", "scope_label": "全量",
            "trigger_label": "手动", "focus_label": "", "summary": "把奖励发放改成先发后扣。",
            "anomaly_count": 2, "suppressed_count": 0, "exportable": True,
            # 删除那两个标记由服务端算（`can_delete` + `is_baseline`）。42 是基线：
            # 「删了它，下一轮增量就没有可比对的结论了」这句话要靠它才出得来。
            "deletable": True, "is_baseline": True,
        },
        {
            "run_id": 41, "created_at_display": "2026-09-10 09:00:00", "status": "failed",
            "status_label": "分析失败", "risk_label": "", "scope_label": "全量",
            "trigger_label": "定时", "focus_label": "", "summary": "失败：额度用完了",
            "anomaly_count": 0, "suppressed_count": 0, "exportable": False,
            "deletable": True, "is_baseline": False,
        },
        # 第三条：**降级**那一档（它不是成功也不是失败，报告正文是真的）。第三条也让
        # 「方向键撞到首尾」这件事测得出来 —— 两条的话「停在最后一条」与「只有一个方向
        # 可走」分辨不开。
        {
            "run_id": 40, "created_at_display": "2026-09-09 08:00:00", "status": "degraded",
            "status_label": "降级完成", "risk_label": "中", "scope_label": "增量",
            "trigger_label": "手动", "focus_label": "", "summary": "降级（上下文压缩）：这一次只有两条结论。",
            "anomaly_count": 2, "suppressed_count": 1, "exportable": True,
            "deletable": True, "is_baseline": False,
        },
    ]


def _fetch(rows=None) -> dict:
    rows = rows if rows is not None else _rows()
    history = {
        "success": True, "kind": "commit", "runs": rows, "total": len(rows),
        "truncated": False, "limit": 20, "window_days": 90,
    }
    url = "/ai-analysis/commit/7/history"
    table = {url: {"body": history}}
    for row in rows:
        table[f"/ai-analysis/runs/{row['run_id']}/report"] = {
            "body": {
                "success": True, "run_id": row["run_id"],
                "result": {
                    "run_id": row["run_id"], "status": row["status"],
                    "response_text": f"报告正文 {row['run_id']}",
                    "created_at_display": row["created_at_display"],
                    "result": {"risk_level": "high"} if row["exportable"] else None,
                    "error_message": "" if row["exportable"] else "额度用完了",
                },
            }
        }
    # 删除成功：服务端回**重新取过的那份列表**（被删的那条不在了，基线标记换了一行）。
    # 前端必须用它重画 —— 自己从旧列表里摘一条的话，`is_baseline` 会留在被删掉的那行上。
    table["/ai-analysis/runs/42/delete"] = {
        "body": {
            "success": True,
            "message": "已删除 2026-09-12 19:13:04 那一次结论。",
            "run_id": 42, "runs": 1, "traces": 1, "anomalies": 2, "events": 0,
            "history": {
                "success": True, "kind": "commit",
                "runs": [row for row in rows if row["run_id"] != 42],
                "total": max(0, len(rows) - 1), "truncated": False, "limit": 20,
                "window_days": 90, "in_progress": False,
                "can_delete": True, "target_key": "g-7",
            },
        }
    }
    return table


def _run(cases: list, pure: list, rows: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的历次结论断言")
    driver = (
        DRIVER.replace("__SCRIPT__", json.dumps(str(SCRIPT)))
        .replace("__CASES__", json.dumps(cases, ensure_ascii=False))
        .replace("__PURE__", json.dumps(pure, ensure_ascii=False))
        .replace("__ROWS__", json.dumps(rows, ensure_ascii=False))
        .replace("__FETCH__", json.dumps(_fetch(rows), ensure_ascii=False))
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def run() -> dict:
    return _run(
        [
            # 1. 抽屉里正显示第 42 次 → 打开弹层默认选中它，标记说「当前显示的结论」。
            {"name": "默认选中当前这次", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open"]},
            # 2. 屏幕上是一次失败（或全新页面、没有当前运行）→ 退回最新那条。
            {"name": "没有当前那次就选最新", "currentRunId": None,
             "ops": ["track:/ai-analysis/commit/7/history", "open"]},
            # 3. 选中历史那一条：标记变「历史」，导出的必须是**那一条**。
            {"name": "翻到历史那一条", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "select:41"]},
            # 4. 只有一次结论的目标。
            {"name": "只有一次结论", "currentRunId": 42, "rows": _rows()[:1],
             "ops": ["track:/ai-analysis/commit/7/history", "open"]},
            # 5. 一条都没有（没用过分析的目标）。
            {"name": "一次都没有", "currentRunId": None, "rows": [],
             "ops": ["track:/ai-analysis/commit/7/history", "open"]},
            # 6. 页面忘了 track（入口少了一次调用）→ 如实说，且**不发请求**。
            {"name": "没有目标地址", "currentRunId": None, "ops": ["open"]},
            # 7. 正在跑、还没有任何结论：**这条走的是真渲染路径**。只断纯函数
            #    `listNote()` 是不够的 —— `render()` 里曾经写死过 `NOTE.empty`，
            #    于是那句正确的话只活在常量表里，单测照样全绿。
            {"name": "正跑着且名单为空", "currentRunId": None, "rows": [], "in_progress": True,
             "ops": ["track:/ai-analysis/commit/7/history", "open"]},
            # 8. 报告**还没回来**那一档：右栏要写「正在读这一份」，不许写成「里面没有正文」。
            {"name": "报告还没回来", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "pending:41"]},
            # 9. 某一份的报告**读不到**（网络抖一下）：那一条的详情说读不到，
            #    但**列表必须原样还在**（原来这条路径会把整个弹层抹成一句话）。
            {"name": "报告读不到", "currentRunId": 42, "rejectRuns": [41],
             "ops": ["track:/ai-analysis/commit/7/history", "open", "select:41", "select:41"]},
            # 10. 点整行（不是点某个按钮）就换选中。
            {"name": "点整行就选中", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "clickItem:1"]},
            # 11. 来回翻：列表**不重建**（左列逐条的身份不变）、看过的**不重取**。
            {"name": "来回翻不重建列表", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "select:41",
                     "select:42", "select:41"]},
            # 12. 方向键：↓ 到底停住、↑ 到顶停住、Home/End 直达（三行才测得出来）。
            {"name": "方向键换选中", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open",
                     "key:ArrowDown", "key:ArrowDown", "key:ArrowDown",
                     "key:ArrowUp", "key:Home", "key:End", "key:Tab"]},
            # 13. 删掉正在看的那一条：**先问再删**，删完用服务端回的列表重画，
            #     选中退到还在的第一条，右栏读的是**那一条**的报告（不是一句
            #     「这一次没有报告正文。」—— 那是把「还没取回来」画成了「里面是空的」）。
            {"name": "删掉正在看的那一条", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "clickDelete"]},
            # 14. 用户在确认框里点了取消 → **一个请求都不发**，列表原样。
            {"name": "确认框里点了取消", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "declineDelete:42"]},
            # 15. 连点两下 → 只发一次请求（第二次删的是一条已经不存在的记录）。
            {"name": "连点两下只删一次", "currentRunId": 42,
             "ops": ["track:/ai-analysis/commit/7/history", "open", "doubleDelete:42"]},
            # 16. 服务端拒绝（还在跑）→ 那句错误只落在详情区，**列表一行都不许少**。
            {"name": "服务端拒绝删除", "currentRunId": 42, "deleteFail": [41],
             "ops": ["track:/ai-analysis/commit/7/history", "open", "confirmDelete:41"]},
            # 17. 不是管理员：**详情栏里根本没有那个按钮**（服务端给的 `deletable` 说是
            #     不能删），API 也不发请求。
            {"name": "不能删的人看不到按钮", "currentRunId": 42,
             "rows": [dict(row, deletable=False) for row in _rows()],
             "ops": ["track:/ai-analysis/commit/7/history", "open", "confirmDelete:42"]},
        ],
        [
            {"name": "提交的历史地址", "kind": "commit", "id": 7, "runId": 42},
            {"name": "周版本的历史地址", "kind": "weekly", "id": 3, "runId": 42},
            {"name": "不认识的目标类型", "kind": "bogus", "id": 3, "runId": 42},
            {"name": "空列表", "payload": {"runs": [], "total": 0}, "runId": 1},
            {"name": "正跑着且还没有结论",
             "payload": {"runs": [], "total": 0, "in_progress": True}, "runId": 1},
            {"name": "只有一次", "payload": {"runs": _rows()[:1], "total": 1}, "runId": 1},
            {"name": "全部列出", "payload": {"runs": _rows(), "total": 2}, "runId": 1},
            {"name": "被截断", "payload": {"runs": _rows()[:2], "total": 40, "truncated": True,
                                          "window_days": 90}, "runId": 1},
            {"name": "当前这一份", "runId": 1,
             "row": {"run_id": 1, "created_at_display": "2026-09-12 19:13:04"},
             "currentRunId": 1},
            {"name": "历史那一份", "runId": 1,
             "row": {"run_id": 2, "created_at_display": "2026-09-10 09:00:00"},
             "currentRunId": 1},
            {"name": "成功", "status": "succeeded", "runId": 1},
            {"name": "失败", "status": "failed", "runId": 1},
            {"name": "降级", "status": "degraded", "runId": 1},
            {"name": "进行中", "status": "running", "runId": 1},
            {"name": "一行的次要信息", "runId": 1,
             "row": {"scope_label": "全量", "trigger_label": "定时", "focus_label": "仅配表仓库",
                     "anomaly_count": 3}},
            {"name": "次要信息全空", "runId": 1, "row": {}},
            # 「这一行是不是抽屉里正显示的那一份」（`is-current` 那半边的判据）
            {"name": "就是抽屉里那份", "runId": 7, "currentRunId": 7},
            {"name": "不是抽屉里那份", "runId": 7, "currentRunId": 9},
            {"name": "没有当前那一次", "runId": 7},
            # 方向键落点：没选中时（-1）、首、中、尾、空名单
            {"name": "还没选中", "count": 3, "at": None},
            {"name": "落在第一条", "count": 3, "at": 0},
            {"name": "落在最后一条", "count": 3, "at": 2},
            {"name": "名单是空的", "count": 0, "at": 0},
            # 删除前那句话：**基线那一档要多一句**（删了它，下一轮增量就没有可比对的
            # 结论了），非基线那一档不许多说。
            {"name": "要删的是基线那一条", "runId": 42,
             "askRow": {"run_id": 42, "created_at_display": "2026-09-12 19:13:04",
                        "is_baseline": True}},
            {"name": "要删的不是基线", "runId": 41,
             "askRow": {"run_id": 41, "created_at_display": "2026-09-10 09:00:00",
                        "is_baseline": False}},
        ],
        _rows(),
    )


def _by_name(run: dict) -> dict:
    return {item["name"]: item for item in run["cases"]}


def _pure(run: dict) -> dict:
    return {item["name"]: item for item in run["pure"]}


# --------------------------------------------------------------------------
#  纯函数
# --------------------------------------------------------------------------
def test_the_history_urls_are_the_ones_the_routes_serve(run):
    pure = _pure(run)
    assert pure["提交的历史地址"]["url"] == "/ai-analysis/commit/7/history"
    assert pure["周版本的历史地址"]["url"] == "/ai-analysis/weekly/3/history"
    assert pure["不认识的目标类型"]["url"] is None


def test_the_export_link_points_at_the_run_it_is_showing(run):
    pure = _pure(run)
    assert pure["提交的历史地址"]["exportHref"] == "/ai-analysis/runs/42/report.md"
    assert pure["提交的历史地址"]["reportUrl"] == "/ai-analysis/runs/42/report"


def test_the_list_note_tells_the_truth_about_how_many_there_are(run):
    pure = _pure(run)
    assert pure["空列表"]["listNote"] == "这个目标还没有跑过分析 —— 没有可以翻看的历次结论。"
    assert pure["正跑着且还没有结论"]["listNote"] == (
        "这个目标正在分析，还没有结论可翻。跑完之后这里会列出每一次。"
    ), "正在跑却说他没跑过 —— 那是一句假话"
    assert "只有一次结论" in pure["只有一次"]["listNote"]
    assert pure["全部列出"]["listNote"] == "这个目标跑过 2 次，新的在最上面。"
    assert "只列出最近 2 次" in pure["被截断"]["listNote"]
    assert "90 天" in pure["被截断"]["listNote"], "要说清更早的为什么没有"


def test_the_mark_says_whether_this_is_the_one_on_screen(run):
    pure = _pure(run)
    assert pure["当前这一份"]["mark"] == "这是当前显示的结论（2026-09-12 19:13:04）"
    assert pure["历史那一份"]["mark"] == "这是 2026-09-10 09:00:00 的结论（历史）"


def test_the_status_colors_and_the_meta_line(run):
    pure = _pure(run)
    assert pure["成功"]["tone"] == "success"
    assert pure["失败"]["tone"] == "danger"
    # 降级**单独一档**：它不是成功（流程没走完），也不是失败（那份报告是真的）。
    # 与成功同色的话，「这一份是降级出来的」在列表里就看不出来。
    assert pure["降级"]["tone"] == "warning"
    assert pure["进行中"]["tone"] == "secondary"
    assert pure["一行的次要信息"]["rowMeta"] == "全量 · 定时 · 仅配表仓库 · 异常 3 条"
    assert pure["次要信息全空"]["rowMeta"] == ""


# --------------------------------------------------------------------------
#  两栏（左列 + 右详情）：这是 2026-09-25 改版的核心
# --------------------------------------------------------------------------
def test_the_report_sits_next_to_the_list_not_under_it(run):
    """用户实测的原话是「**没有跳到正文**」。

    原来是「一张宽表 + 报告接在整张表下面」：20 行表格一千多像素高，报告落在折叠线
    以下，点「看这一份」屏幕上纹丝不动（变化全在折叠线底下）。改版后**两栏各自滚动**，
    报告永远在视野里。

    **文档顺序分辨不出这两者**（改版前改版后，报告都在列表文字之后）—— 分开它们的是
    「两栏是同一个网格的两个格子、各自滚」这一条 CSS。所以这里断到样式表上：
    少一条 `overflow-y` / `max-height`，两栏就变成一栏，而那在 JS 的 dump 里完全看不出来。
    """
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]
    assert "ai-history-panes" in last["body"]
    assert "aiHistoryReportBody" in last["detail"], "报告没在右栏里"
    assert "aiAnomalyPanel" in last["detail"]

    css = (PROJECT_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    panes = _css_block(css, ".ai-history-panes {")
    assert "grid-template-columns" in panes, "两栏不是网格：报告会退回列表下面"
    assert panes.count("minmax(0, 1fr)") == 1 and "360px" in panes
    for selector in (".ai-history-list-pane,", ".ai-history-detail-pane {"):
        block = _css_block(css, selector)
        assert "overflow-y: auto" in block, f"{selector} 不能自己滚"
        # **要断到值上**：只写 `max-height` 三个字的话，`max-height: none` 也照样通过
        # （变异验证当场验过这条守卫是假绿的）。
        assert "max-height: calc(" in block, f"{selector} 没有封顶：报告会被推出视野"
    # 改版前的那个「报告接在表下面」的分隔块必须已经不在（留着它会多一条横线与间距）
    assert ".ai-history-report {" not in css


def _css_block(css: str, selector: str) -> str:
    start = css.index(selector)
    return css[start:css.index("}", start)]


def test_the_two_panes_are_siblings(run):
    """两栏必须是**同一个父节点下**的兄弟。

    分成两处渲染（比如列表在 body 里、报告挂到别处）看起来也「有那两栏」，
    但窄屏的上下堆叠、`gap`、以及「两栏各自滚动」全都不会生效。
    """
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]
    lines = last["body"].splitlines()
    pane_lines = [i for i, line in enumerate(lines) if "ai-history-panes" in line]
    assert pane_lines, "没有两栏的容器"
    top = pane_lines[0]
    # 容器下面紧跟的两个同级节点就是那两栏（dump 的缩进 = 树深）
    indent = len(lines[top]) - len(lines[top].lstrip())
    siblings = [
        line for line in lines[top + 1:]
        if (len(line) - len(line.lstrip())) == indent + 2 and line.strip().startswith("<div")
    ]
    assert len(siblings) >= 2, "两栏不是同级兄弟：\n" + "\n".join(lines[top:top + 8])
    assert "ai-history-list-pane" in siblings[0]
    assert "ai-history-detail-pane" in siblings[1]


def test_switching_never_rebuilds_the_list(run):
    """换选中只重画右栏。**整块重画会把左列的滚动位置打回顶部** ——
    翻到第 15 条按一下方向键就跳回第 1 条，而这类毛病在 dump 出来的文本上
    **一个字都看不出来**（重建出来的树长得一模一样），只有节点身份能分辨。
    """
    snaps = _by_name(run)["来回翻不重建列表"]["snaps"]
    # ops = [track, open, select:41, select:42, select:41] → 每执行完一个 op 记一张
    assert not snaps[0]["itemUids"], "track 之后不该有列表（构造没生效）"
    opened, after_first, after_second, after_third = snaps[1], snaps[2], snaps[3], snaps[4]

    assert len(opened["itemUids"]) == 3
    assert after_first["itemUids"] == opened["itemUids"], "换选中把左列重建了"
    assert after_second["itemUids"] == opened["itemUids"]
    assert after_third["itemUids"] == opened["itemUids"]
    assert after_third["listUid"] == opened["listUid"], "连列表容器都换了"
    # 反过来也要成立：这一次里确实**换过**选中（不然上面几条是「什么都没发生」的假绿）
    assert after_first["state"]["selectedRunId"] == 41
    assert after_third["state"]["selectedRunId"] == 41
    # 而右栏**确实**重画了（详情区换了内容）
    assert after_first["detail"] != opened["detail"]


def test_a_report_already_read_is_not_fetched_again(run):
    """看过的报告不取第二遍。方向键逐条翻时，这一条省掉的是**每一次**请求。"""
    last = _by_name(run)["来回翻不重建列表"]["snaps"][-1]
    assert last["fetchLog"] == [
        "/ai-analysis/commit/7/history",
        "/ai-analysis/runs/42/report",
        "/ai-analysis/runs/41/report",
    ], last["fetchLog"]
    assert sorted(last["state"]["cachedRuns"]) == [41, 42]


# --------------------------------------------------------------------------
#  打开：列表 + 默认选中
# --------------------------------------------------------------------------
def test_opening_shows_every_run_and_marks_the_current_one(run):
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]

    assert "2026-09-12 19:13:04" in last["body"]
    assert "2026-09-10 09:00:00" in last["body"], "每一条都要列出来"
    assert "已有结论" in last["body"] and "分析失败" in last["body"]
    assert "把奖励发放改成先发后扣。" in last["body"], "摘要要显示出来"
    assert last["state"]["selectedRunId"] == 42
    # 当前那一行有 is-current，选中的那一行有 is-selected（两件事，可能同一行）
    assert "is-current" in last["body"]


def test_opening_without_a_current_run_picks_the_newest(run):
    """屏幕上没有结论（刚打开页面 / 这次是失败的中间态）→ 退回最新那一条。"""
    last = _by_name(run)["没有当前那次就选最新"]["snaps"][-1]
    assert last["state"]["selectedRunId"] == 42


def test_opening_fetches_the_report_of_the_selected_run(run):
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]
    assert last["fetchLog"] == [
        "/ai-analysis/commit/7/history", "/ai-analysis/runs/42/report",
    ]
    assert "<rendered>" in last["body"], "报告要交给 Markdown 渲染器，不许拼 HTML"


def test_the_drawer_footer_export_link_is_not_touched(run):
    """弹层里也有一个「导出 md」，但它导的是**选中的那一份**；抽屉 footer 那个归
    `AiReportExport` 管，弹层不许改它。"""
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]
    assert last["footerHref"] == "/ai-analysis/runs/1/report.md"


# --------------------------------------------------------------------------
#  翻历史：标记与导出都跟着选中的那一条走
# --------------------------------------------------------------------------
def test_selecting_an_older_run_moves_the_mark_and_the_export_link(run):
    last = _by_name(run)["翻到历史那一条"]["snaps"][-1]

    assert last["state"]["selectedRunId"] == 41
    assert "这是 2026-09-10 09:00:00 的结论（历史）" in last["body"]
    # 失败的那一条没有正文，也没有可导出的东西 —— 不给一个点了必然 409 的链接
    assert last["historyHref"] is None
    assert "这一次是失败的，没有结论。原因：额度用完了" in last["body"], (
        "失败的那一条要说清「没有结论」以及为什么 —— 不能留一片空白"
    )


def test_selecting_a_failed_run_does_not_offer_an_export(run):
    last = _by_name(run)["翻到历史那一条"]["snaps"][-1]
    assert last["historyHref"] is None


# --------------------------------------------------------------------------
#  边界：只有一次 / 一次都没有
# --------------------------------------------------------------------------
def test_a_target_with_a_single_conclusion_says_so(run):
    last = _by_name(run)["只有一次结论"]["snaps"][-1]
    assert "只有一次结论" in last["body"]


def test_a_target_that_never_ran_says_that_instead(run):
    last = _by_name(run)["一次都没有"]["snaps"][-1]
    assert "还没有跑过分析" in last["body"]


def test_a_running_target_with_an_empty_list_says_it_is_running_on_screen(run):
    """**断言的是屏幕上那句话**，不是纯函数。

    `listNote()` 说得对但 `render()` 里写死了另一句，是这一批里最典型的一种错：
    单测绿、屏幕上永远看不到那句话。
    """
    last = _by_name(run)["正跑着且名单为空"]["snaps"][-1]

    assert "正在分析" in last["body"], last["body"]
    assert "还没有跑过分析" not in last["body"], "正在跑却说他没跑过 —— 那是一句假话"


def test_without_a_target_url_it_says_so_and_fires_no_request(run):
    """入口忘了 `track` 时：不许去请求一个叫 `null` 的地址（那是一条谁也解释不了的日志）。"""
    last = _by_name(run)["没有目标地址"]["snaps"][-1]
    assert "没有告诉它看哪个目标" in last["body"]
    assert last["fetchLog"] == []


# --------------------------------------------------------------------------
#  选中与「当前显示」是两件事（原来只靠两种颜色，说不出来）
# --------------------------------------------------------------------------
def test_the_two_marks_are_spelled_out_not_just_colored(run):
    """「当前显示的那一份」原来只有一条 3px 色条 —— **颜色单独承载语义**。

    「正在看的那一份」也只有底色。两件事都改成了**说得出来**的东西：前者多一个
    写着「当前显示」的标签，后者多一个 `aria-selected`。它们可能落在同一行上
    （默认打开时就是这样），所以两个标记必须能同时在场。
    """
    pure = _pure(run)
    assert pure["就是抽屉里那份"]["current"] is True
    assert pure["不是抽屉里那份"]["current"] is False
    assert pure["没有当前那一次"]["current"] is False, (
        "屏幕上没有结论时，不许把随便哪一条说成「当前显示」"
    )

    # 两个标记**同时在场**：默认打开时选中的就是抽屉里那份，两个 class 都要在
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]
    assert "ai-history-item is-current is-selected" in last["body"], last["body"]
    assert "当前显示" in last["body"], "「当前」只剩颜色了"
    # 清单是**单选**的：`role=listbox` + 每项 `role=option`，且只有一项说自己是选中的
    assert last["listRole"] == "listbox"
    assert last["optionCount"] == 3
    assert last["selectedCount"] == 1
    assert last["selectedAttrs"] == {
        "role": "option", "tabindex": "0", "ariaSelected": "true",
    }
    assert last["state"]["rows"][0] == {"run_id": 42, "selected": True}
    # roving tabindex：Tab 只进得来一次（选中那条），进来之后靠方向键走
    assert last["tabindexes"] == ["0", "-1", "-1"]

    # 换到历史那一条：`is-selected` 跟着走，`is-current` **留在原地**（两件事）
    moved = _by_name(run)["翻到历史那一条"]["snaps"][-1]
    assert "ai-history-item is-current" in moved["body"], "「当前显示」跟着选中跑了"
    assert "ai-history-item is-selected" in moved["body"]
    assert moved["selectedCount"] == 1, "同时有两条说自己被选中"
    assert moved["selectedAttrs"]["ariaSelected"] == "true"
    assert moved["tabindexes"] == ["-1", "0", "-1"], "roving tabindex 没跟着走"


def test_clicking_anywhere_on_the_row_selects_it(run):
    """整行可点 —— 原来只有右边那个「看这一份」按钮可点。

    按钮已经去掉：整行都是目标，再摆一个按钮等于把「只有这里能点」写在脸上。
    """
    last = _by_name(run)["点整行就选中"]["snaps"][-1]
    assert last["state"]["selectedRunId"] == 41, "点了第二行（run 41）却没换过去"
    assert "这是 2026-09-10 09:00:00 的结论（历史）" in last["detail"]
    assert "看这一份" not in last["body"], "那个按钮还在"


# --------------------------------------------------------------------------
#  键盘：焦点走到哪一条，看的就是哪一条
# --------------------------------------------------------------------------
def test_the_arrow_keys_move_the_selection(run):
    """`↓` 到底停住、`↑` 到顶停住、`Home`/`End` 直达。

    这一格是 `nextIndex` 的纯函数判据（`-1` = 这个键不管）。首尾**不环绕**：
    翻历史是「找某一次」，绕回开头会让人以为自己翻过头了。
    """
    pure = _pure(run)
    assert pure["还没选中"]["nav"] == "0,2,0,2,-1"
    assert pure["落在第一条"]["nav"] == "1,0,0,2,-1"
    assert pure["落在最后一条"]["nav"] == "2,1,0,2,-1", "到底了还往下走 = 绕回去了"
    assert pure["名单是空的"]["nav"] == "-1,-1,-1,-1,-1"


def test_the_arrow_keys_actually_walk_the_list_and_move_focus(run):
    """**接线**：上面那条只证明算得对，这条证明屏幕上真的在动。

    三行名单，从 run 42（当前，第一条）开始：`↓↓↓` 停在最后一条（40）、
    `↑` 回到 41、`Home` 回 42、`End` 到 40、`Tab` 不拦（它得留给浏览器）。
    """
    snaps = _by_name(run)["方向键换选中"]["snaps"]
    # ops = [track, open, ↓, ↓, ↓, ↑, Home, End, Tab] → 每执行完一个 op 记一张
    assert snaps[1]["state"]["selectedRunId"] == 42, "打开之后没选中当前那一次"
    #                                    ↓  ↓  ↓  ↑  Home End
    assert [snap["state"]["selectedRunId"] for snap in snaps[2:8]] == [41, 40, 40, 41, 42, 40]

    # 焦点**跟着选中走**：只改颜色不算 —— 按 Tab 进来的人会看不出焦点在哪一条
    for snap in snaps[2:8]:
        assert snap["focusedUid"] is not None, "方向键之后没有任何一条拿到焦点"
        assert snap["focusedUid"] == snap["selectedUid"], (
            "焦点落在的那一条不是刚选中的那一条：\n" + snap["body"]
        )
        assert snap["selectedUid"] in snap["itemUids"], "选中的节点已经不在列表里了"
    # `Tab` 那一下什么都不许发生（它要留给浏览器去走下一个控件）
    assert snaps[8]["state"]["selectedRunId"] == 40
    assert snaps[8]["focusedUid"] == snaps[7]["focusedUid"], "Tab 被拦下来改了选中"


# --------------------------------------------------------------------------
#  右栏自己的加载态 / 读不到（两个都曾经把「还没回来」画成别的）
# --------------------------------------------------------------------------
def test_a_report_still_in_flight_says_it_is_reading(run):
    """**原来是闪一句「这一次没有报告正文。」**

    `select()` 先把 `selectedReport` 清成 null 再重画，于是「还没取回来」被画成了
    「取回来了、里面没有正文」—— 用户每次换一条都会看到它闪一下。
    """
    last = _by_name(run)["报告还没回来"]["snaps"][-1]
    assert "正在读取这一份的报告" in last["detail"], last["detail"]
    assert "没有报告正文" not in last["detail"], "把「还没回来」画成了「里面没有」"
    assert last["ariaBusy"] == "true", "在等的时候要有 aria-busy（读屏软件才知道）"
    # 列表**不受影响**：左列照旧列着三条
    assert "2026-09-12 19:13:04" in last["body"]
    assert len(last["itemUids"]) == 3


def test_a_report_that_cannot_be_read_keeps_the_list(run):
    """读不到某一份的报告时：**只有右栏说读不到，左列原地不动**。

    原来是 `renderNote()` 那一句（它写的是整个弹层），一次网络抖动就把用户正在翻的
    历史**整张抹掉**，换成一行「读不到历次结论：连接失败」—— 而历次结论明明读到了。
    """
    last = _by_name(run)["报告读不到"]["snaps"][-1]
    assert "读不到这一份的报告" in last["detail"]
    assert "连接失败" in last["detail"]
    # 左列三条一条都不许少
    assert len(last["itemUids"]) == 3
    assert "2026-09-12 19:13:04" in last["body"]
    assert "2026-09-09 08:00:00" in last["body"]
    assert "读不到历次结论" not in last["body"], "把「这一份读不到」说成了「历次结论读不到」"
    # 重试：再点同一条要**重新发请求**（失败留在缓存里的话，用户永远看到同一个错误）
    assert last["fetchLog"].count("/ai-analysis/runs/41/report") == 2, last["fetchLog"]


# --------------------------------------------------------------------------
#  模板那一侧：弹层 DOM 与按钮
# --------------------------------------------------------------------------
TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)
_BUTTON_IDS = {
    "templates/commit_diff_new.html": "aiHistoryBtn",
    "templates/weekly_version_diff.html": "weeklyAiHistoryBtn",
    "templates/merged_project_view.html": "weeklyAiHistoryBtn",
}


def _template(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_every_drawer_has_a_history_button_outside_the_drawer():
    for name, button_id in _BUTTON_IDS.items():
        source = _template(name)
        assert f'id="{button_id}"' in source, f"{name} 没有「历次结论」按钮"
        assert "历次结论" in source
        # 按钮在抽屉 footer 里（用户拉开抽屉就能看到），弹层在 `</aside>` 外面
        assert source.index(f'id="{button_id}"') < source.index("</aside>")
        assert source.index('id="aiReportHistoryModal"') > source.index("</aside>")


def test_the_history_button_is_never_disabled():
    """**跑动中正是最需要它的时候**（用户要边等边看旧结论）—— 不许给它加 disabled。"""
    for name, button_id in _BUTTON_IDS.items():
        source = _template(name)
        button_line = next(
            line for line in source.splitlines() if f'id="{button_id}"' in line
        )
        assert "disabled" not in button_line


def test_every_drawer_loads_the_history_module():
    for name in TEMPLATES:
        assert "js/ai_report_history.js" in _template(name), f"{name} 没有引历次结论那个模块"


def test_the_history_module_is_loaded_with_a_cache_buster():
    """脚本与样式**必须带 `?v=`**，否则用户看到的还是上一版。

    这条不是形式主义：这次改版把**同一件事拆到了两个文件**里 —— 这个模块建出
    `.ai-history-item` 那一套 DOM，`style.css` 才有得可样式。浏览器命中缓存的旧 JS
    配上新的 CSS，屏幕上就是一堆没有任何排版的裸文字（比改版前更糟）。`base.html`
    里给报告渲染器写 `?v=2` 时踩的就是这个坑（那里留了注释）。

    **它拦不住「改了却没升版本号」**（那个只能靠人），拦的是「新加引用时忘了带」。
    """
    for name in TEMPLATES:
        source = _template(name)
        line = next(
            line for line in source.splitlines()
            if "js/ai_report_history.js" in line and "<script" in line
        )
        assert "?v=" in line, f"{name} 引历次结论模块时没有带 ?v=：{line.strip()}"
    base = (PROJECT_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    css_line = next(
        line for line in base.splitlines()
        if "css/style.css" in line and "<link" in line
    )
    assert "?v=" in css_line, f"style.css 没有带 ?v=：{css_line.strip()}"


def test_the_merged_view_resets_the_export_link_when_the_target_changes():
    """合并视图是**同一个抽屉换目标**：换目标那一刻屏幕上那份结论就换人了。

    不重设的话，看着版本 B 的抽屉点导出，拿到的是**版本 A** 的结论（文件名与内容都是 A）。
    只数 `track(` 的个数拦不住这件事 —— 少了这一处，个数照样够。
    """
    source = _template("templates/merged_project_view.html")
    start = source.index("function openWeeklyAiDrawer(")
    end = source.index("\n}", start)
    body = source[start:end]

    assert "AiReportExport.track({ runId: null })" in body, (
        "换目标时没有把「导出 md」收起来"
    )
    assert "AiThinkLog.setRun(null)" in body, "同一处的逐轮明细也要清（既有行为）"


def test_every_drawer_tracks_its_targets_history_url():
    """地址不跟着目标走，列出来的就是**别的目标的**结论（合并视图是同一个抽屉换目标）。"""
    assert "AiReportHistory.historyUrlFor('commit', commitId)" in _template(
        "templates/commit_diff_new.html"
    )
    assert "AiReportHistory.historyUrlFor('weekly', configId)" in _template(
        "templates/weekly_version_diff.html"
    )
    merged = _template("templates/merged_project_view.html")
    assert "AiReportHistory.historyUrlFor('weekly', configId)" in merged
    assert merged.count("AiReportHistory.track(") >= 1


# --------------------------------------------------------------------------
#  删除一条历次结论
# --------------------------------------------------------------------------
def test_deleting_the_open_one_asks_first_then_redraws_from_the_server(run):
    """删掉正在看的那一条：**先问、再发一次请求、用服务端回的列表重画**。

    ## 为什么必须用服务端那份列表

    被删的那一条可能正是**基线指针**（`is_baseline`）。删完之后新基线指向哪一条只有
    服务端算得出来 —— 前端自己从旧列表里摘一条的话，那个标记会留在已经不再是指针的
    记录上，而「基线退到哪」正是这个功能的全部意义。

    ## 为什么右栏要重新读一次

    被删的若是正在看的那一份，选中会退到最新那条，而它的报告**一次都没取过**。少了
    那一步，右栏会写「这一次没有报告正文。」—— 把「还没取回来」画成了「取回来了、
    里面是空的」（这个老缺陷在本文件 2026-09-25 那一节里修过一次）。
    """
    last = _by_name(run)["删掉正在看的那一条"]["snaps"][-1]

    assert last["confirms"], "删除之前没有问过一句"
    assert "删除这一份结论？" in last["confirms"][0]
    assert "基线" in last["confirms"][0], (
        "要删的这条是基线，确认框里必须说清「删了下一次增量会改用它上面那一条」—— "
        "这是这个动作的全部风险"
    )
    assert len(last["posts"]) == 1, f"删除该只发一次请求：{last['posts']}"
    assert last["posts"][0]["url"] == "/ai-analysis/runs/42/delete"
    assert last["posts"][0]["method"] == "POST"
    assert '"target_key"' in last["posts"][0]["body"], (
        "请求里没带分组键 —— 弹层是同一个抽屉换目标，服务端就靠它判「你看的是不是这个版本」"
    )
    # 列表按**服务端回的那份**重画：42 不在了，只剩两条。
    assert last["optionCount"] == 2
    assert last["state"]["selectedRunId"] == 41, "删完该退到还在的第一条"
    assert last["state"]["deleteNotice"], "删成功之后详情区没有任何回执"
    # 退到的那一条是**失败**记录（41），所以右栏该说「这一次是失败的」并带上原因 ——
    # 那句话只有取回报告才可能显示出来。**不能只断「有一份报告」**：没取的时候那里
    # 写的是「这一次没有报告正文。」，与「取回来了、里面是空的」是同一种假话。
    assert "这一次没有报告正文。" not in last["body"], (
        "删完右栏没有去读新选中的那一份 —— 屏幕上会写着「这一次没有报告正文。」"
    )
    assert "额度用完了" in last["body"], "右栏显示的不是那一条自己的报告"
    assert last["state"]["deleteError"] == ""


def test_cancelling_the_confirm_sends_nothing(run):
    """确认框里点了取消 → **一个写请求都不发**，列表与选中原样不动。"""
    last = _by_name(run)["确认框里点了取消"]["snaps"][-1]

    assert last["confirms"], "连问都没问"
    assert last["posts"] == [], "用户点了取消，请求还是发出去了"
    assert last["optionCount"] == 3
    assert last["state"]["selectedRunId"] == 42
    assert last["state"]["deleting"] is None
    assert last["state"]["deleteNotice"] == ""
    assert last["state"]["deleteError"] == ""


def test_double_clicking_deletes_only_once(run):
    """连点两下只发一次请求（第二次删的是一条已经不存在的记录）。

    按钮在飞的时候是禁用的，但键盘与快速双击都可能赶在那一次重画之前进来 —— 所以
    `state.deleting` 那道闸门不能只有按钮的 `disabled` 撑着。
    """
    last = _by_name(run)["连点两下只删一次"]["snaps"][-1]

    assert len(last["posts"]) == 1, f"连点两下发了 {len(last['posts'])} 次请求"
    assert last["optionCount"] == 2


def test_a_refused_delete_keeps_the_list_intact(run):
    """服务端拒绝（还在跑 / 版本对不上）→ 那句错误**只落在详情区**，列表一行都不许少。

    这条与「报告读不到时整张列表被抹掉」是同一个老缺陷的另一面：一次失败的网络调用
    不该把用户正在翻的历史清空。
    """
    last = _by_name(run)["服务端拒绝删除"]["snaps"][-1]

    assert len(last["posts"]) == 1
    assert last["optionCount"] == 3, "删除被拒之后列表少了一行"
    assert last["state"]["selectedRunId"] == 42
    assert last["state"]["deleteError"], "详情区没有把服务端那句话显示出来"
    assert last["deleteNote"] and last["deleteNote"]["isError"] is True
    assert "不能删除" in last["deleteNote"]["text"], last["deleteNote"]
    assert last["state"]["deleting"] is None, "失败之后「正在删除」没有解除，按钮会一直禁用"


def test_an_undeletable_row_has_no_button_and_sends_nothing(run):
    """不能删的人（不是项目管理员）**根本看不到那个按钮**，API 也不发请求。

    判据来自服务端的 `deletable`（与后端的拒绝分支同源）—— 界面上「摆了按钮再拒绝」
    比「不摆」更糟：用户点下去会以为是自己没权限，而不是这条记录本来就不给删。
    """
    last = _by_name(run)["不能删的人看不到按钮"]["snaps"][-1]

    assert last["deleteButton"] is None, "不能删的人却看到了删除按钮"
    assert last["posts"] == [], "`deletable` 是假，请求还是发出去了"
    assert last["confirms"] == [], "不该问就问了"
    assert last["optionCount"] == 3


def test_the_delete_button_reports_its_own_state(run):
    """能删的那一档：按钮在场、写着「删除这一份」、**没有** disabled（在飞才禁用）。"""
    last = _by_name(run)["删掉正在看的那一条"]["snaps"][-1]

    # 删完之后 42 没了、选中退到 41，41 也是能删的 —— 按钮跟着那一行重新长出来。
    assert last["deleteButton"] is not None
    assert last["deleteButton"]["text"] == "删除这一份"
    assert last["deleteButton"]["disabled"] is False


def test_the_confirm_sentence_depends_on_whether_it_is_the_baseline(run):
    """确认框那句话是**纯函数**：基线那一档多一句，其余不多说。

    `deleteConfirmText` 进的是**两个不同**的 row，所以这两条必须给出不同的结果 ——
    只测「有一句话」是测不出这个分叉的。
    """
    pure = _pure(run)
    baseline = pure["要删的是基线那一条"]["deleteAsk"]
    plain = pure["要删的不是基线"]["deleteAsk"]

    assert baseline != plain
    assert "下一轮增量分析的基线" in baseline
    assert "下一轮增量分析的基线" not in plain
    assert "不能恢复" in baseline and "不能恢复" in plain, "两档都要说清这件事不可逆"
    assert "2026-09-12 19:13:04" in baseline, "确认框里要写清删的是哪一次"


def test_the_delete_url_is_the_route_that_really_exists(run):
    """前端拼出来的删除地址，必须是**后端真的注册了那条路由**。

    这条挡的是「前端 POST 到一个不存在的路径」：那种错在浏览器里表现为一句
    「删除失败」（前端把 404 的 HTML 当成服务端的 message），而后端日志里什么都没有 ——
    查起来要来回好几轮。

    判据直接取 `app.url_map`（**不是**再拼一个字符串跟自己比：那种断言恒真，
    它只证明了「split 之后还能拼回去」）。
    """
    from app import app

    rule = "/ai-analysis/runs/<int:run_id>/delete"
    assert rule in {str(r.rule) for r in app.url_map.iter_rules()}, (
        f"前端拼的地址在后端没有对应的路由：{rule}"
    )
    # 前端那一份模板字符串也要与它对得上（唯一的产地是 `deleteUrl`）。
    assert _pure(run)["提交的历史地址"]["deleteUrl"] == "/ai-analysis/runs/42/delete"


def test_the_baseline_row_says_so_in_words(run):
    """「当前基线」是一个**词**，不是一条颜色（与「当前显示」同一条口径）。

    删除这个功能靠它才读得懂：「删了下一次增量会改用上一条」这句话，只有在用户已经
    知道「它本来就是基线」时才成立。所以标记必须**落在基线那一行**上。

    「当前显示」与「当前基线」是**两件事**，默认打开时恰好落在同一行（42 既是基线
    又是屏幕上正显示的那次）—— 换一条选中之后，基线标记**留在原地**。
    """
    last = _by_name(run)["默认选中当前这次"]["snaps"][-1]
    assert last["baselineTags"] == [True, False, False], (
        f"「当前基线」没落在基线那一行上：{last['baselineTags']}"
    )
    assert last["baselineTagText"] == "当前基线"

    # 换到不是基线的那一条：基线标记留着（它是那一行的属性，不是选中态的另一个说法）。
    moved = _by_name(run)["翻到历史那一条"]["snaps"][-1]
    assert moved["baselineTags"] == [True, False, False], (
        "翻到别的行之后基线标记跟着跑了"
    )
    assert moved["state"]["selectedRunId"] == 41
