# -*- coding: utf-8 -*-
"""「结构化结论 + 处置」面板 —— 用 node 真跑 `static/js/ai_anomaly_disposition.js`。

## 为什么必须真跑

这一块的每一处判定都「看起来对」，而错了都不报错：

* 状态名（待确认 / 已确认 / 已忽略）是不是真的**从服务端那条 `dispositions` 里取**的
  —— 界面自己抄一份映射表，今天绿、服务端换个中文名就显示一个**错的**状态名；
* 点「已忽略」之后，那一行与其上的计数是不是**就地**变了（而不是要用户刷新页面）；
* 「撤销」提交的是 `pending`，且**不带备注**（带上去也不落库，用户会以为记下了）；
* 一条都没勾时批量按钮**不可用**，且真的不发请求（后端对空 ids 回 400）；
* 400 / 403 时服务端那句 `message` 有没有显示出来（吞掉的话界面上什么都没发生）；
* 同一次运行重画时**不再取第二遍**（历史面板一次选中会重画两次）；
* 清单次序是否照服务端给的走（再排一次 = 上下两处两种次序）。

静态断言证不了这些。所以按仓库既有做法，把真函数放进带假 DOM 的 node 沙箱里跑，
断言**每一步之后的界面结构**，并记下每一次 fetch 的 url / 方法 / 请求体。

## 两个驱动区

* `units`：直接驱动处置模块（`load` / 勾选 / 备注 / 点按钮），每种处境一条用例；
* `wiring`：把 `ai_report_history.js` 与它一起装进沙箱，走**真实的打开路径**
  （`track` → `open` → 选中那一次 → 报告下面的面板）。这一条盯的是接线：
  历史模块没有建容器、或者建了容器却没调 `load`（顺序反了也一样 ——
  `getElementById` 只认**已经在文档里**的节点），症状都是「这一块永远空着，且不报错」。

## 假 DOM

比 `test_ai_report_history_frontend.py` 那个多三样：`checkbox.checked` / `input.value`
（备注是敲进去的，断言要看它提交了什么）、`_listeners` 存监听器数组（点击与输入由
用例 fire），以及 `getElementById` **会走一遍已挂上的节点树** —— 真 DOM 就是这样，
而 `wiring` 那一条正是靠它才能发现「容器还没 append 就去按 id 找」。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANOMALY_SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_anomaly_disposition.js"
HISTORY_SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_report_history.js"

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

// 契约：容器 id 由历史模块建出来，处置模块按它找（两边共同的事实）。
var PANEL_ID = 'aiAnomalyPanel';
var MODAL_ID = 'aiReportHistoryModal';
var HISTORY_BODY_ID = 'aiReportHistoryBody';

function makeEl(tag) {
    var node = {
        tagName: tag, id: '', className: '', type: '', value: '', checked: false,
        disabled: false, hidden: false, children: [], _attrs: {}, _listeners: {},
        _text: '', _html: ''
    };
    // textContent 的 setter 必须清空子节点（真 DOM 就是这样，模块靠它重画）。
    Object.defineProperty(node, 'textContent', {
        get: function () { return node._text; },
        set: function (v) {
            node._text = v === null || v === undefined ? '' : String(v);
            node.children = [];
        }
    });
    Object.defineProperty(node, 'innerHTML', {
        get: function () { return node._html; },
        set: function (v) { node._html = String(v); node.children = []; }
    });
    node.appendChild = function (child) { node.children.push(child); return child; };
    node.setAttribute = function (k, v) { node._attrs[k] = String(v); };
    node.getAttribute = function (k) {
        return node._attrs[k] === undefined ? null : node._attrs[k];
    };
    node.removeAttribute = function (k) { delete node._attrs[k]; };
    node.addEventListener = function (t, fn) {
        (node._listeners[t] = node._listeners[t] || []).push(fn);
    };
    return node;
}

/** 深度优先找一个节点（假 DOM 没有 querySelector）。 */
function walk(node, fn) {
    if (!node) return null;
    var hit = fn(node);
    if (hit) return hit;
    var kids = node.children || [];
    for (var i = 0; i < kids.length; i++) {
        var found = walk(kids[i], fn);
        if (found) return found;
    }
    return null;
}

function hasClass(node, cls) {
    return (' ' + String(node.className || '') + ' ').indexOf(' ' + cls + ' ') !== -1;
}

var els = {};
var sandbox = {window: {}, console: console};
sandbox.document = {
    // 与真 DOM 一致：先看直接登记的，再走一遍**已经挂上**的节点树。
    // （不在树上的节点找不到 —— `wiring` 那一条靠的就是这条语义。）
    getElementById: function (id) {
        if (els[id]) return els[id];
        var keys = Object.keys(els);
        for (var i = 0; i < keys.length; i++) {
            var hit = walk(els[keys[i]], function (n) { return n.id === id ? n : null; });
            if (hit) return hit;
        }
        return null;
    },
    createElement: function (tag) { return makeEl(tag); },
    createTextNode: function (t) { return {tagName: '#text', textContent: t, children: []}; }
};
sandbox.AiReportMarkdown = {
    render: function (text) { return '<rendered>' + String(text).length + '</rendered>'; }
};
sandbox.AiThinkLog = {currentRunId: function () { return null; }};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__ANOMALY_SCRIPT__, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(__HISTORY_SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiAnomalyDisposition;
var history = sandbox.AiReportHistory;

// ---------------------------------------------------------------------------
//  假 fetch：按 URL 给预置响应，并把每次请求（含请求体）记下来
// ---------------------------------------------------------------------------
var calls = [];
var table = {};

function makeFetch(theTable) {
    return function (url, init) {
        init = init || {};
        calls.push({
            url: url,
            method: init.method || 'GET',
            body: init.body ? JSON.parse(init.body) : null
        });
        var entry = theTable[url];
        if (entry === undefined) {
            return Promise.reject(new Error('没有预置这个地址：' + url));
        }
        if (Array.isArray(entry)) {
            if (!entry.length) {
                return Promise.reject(new Error('这个地址没有更多预置响应了：' + url));
            }
            entry = entry.shift();
        }
        if (entry === 'reject') return Promise.reject(new Error('连接失败'));
        return Promise.resolve({
            ok: entry.ok === undefined ? true : entry.ok,
            status: entry.status || 200,
            json: function () { return Promise.resolve(entry.body); }
        });
    };
}

/** 抛事件：浏览器里点按钮/勾选框就是这个动作。返回监听器给出的 promise（有的话）。 */
function fire(node, type) {
    var fns = (node._listeners[type] || []).slice();
    var pending = null;
    fns.forEach(function (fn) {
        var result = fn();
        if (result && typeof result.then === 'function') pending = result;
    });
    return pending || Promise.resolve(null);
}

// ---------------------------------------------------------------------------
//  断言看的那个「界面」
// ---------------------------------------------------------------------------
function dump(node, depth) {
    if (!node) return '';
    depth = depth || 0;
    var pad = '  '.repeat(depth);
    if (node.tagName === '#text') return pad + '"' + node.textContent + '"';
    var bits = [];
    if (node.className) bits.push('class="' + node.className + '"');
    if (node.id) bits.push('id="' + node.id + '"');
    ['data-anomaly-id', 'data-action', 'role', 'title'].forEach(function (key) {
        var value = node.getAttribute(key);
        if (value !== null) bits.push(key + '="' + value + '"');
    });
    // 输入框的当前值、勾没勾、按钮可不可用 —— 这三样正是这一块的行为，必须看得见。
    if (node.tagName === 'input' || node.tagName === 'textarea') {
        bits.push('value="' + node.value + '"');
        if (node.checked) bits.push('checked');
    }
    if (node.disabled) bits.push('disabled');
    var body = node.textContent || node.innerHTML;
    var lines = [pad + '<' + node.tagName + (bits.length ? ' ' + bits.join(' ') : '')
        + '>' + (body ? ' ' + body : '')];
    node.children.forEach(function (child) { lines.push(dump(child, depth + 1)); });
    return lines.join('\n');
}

function panelRoot() { return els[PANEL_ID]; }

function itemNode(id) {
    var item = walk(panelRoot(), function (n) {
        return n.getAttribute('data-anomaly-id') === String(id) ? n : null;
    });
    if (!item) throw new Error('面板里没有第 ' + id + ' 条');
    return item;
}

function findIn(root, cls, action) {
    var found = walk(root, function (n) {
        if (cls && !hasClass(n, cls)) return null;
        if (action && n.getAttribute('data-action') !== action) return null;
        return n;
    });
    if (!found) {
        throw new Error('找不到 ' + (cls || '') + ' / data-action=' + (action || ''));
    }
    return found;
}

/** 按 class 收一遍节点（假 DOM 没有 querySelector）：中文名/时间显示的是哪一份，看它。 */
function collect(cls) {
    var out = [];
    (function rec(node) {
        if (!node) return;
        if (hasClass(node, cls)) {
            out.push({text: node.textContent, title: node.getAttribute('title')});
        }
        (node.children || []).forEach(rec);
    })(panelRoot());
    return out;
}

function snap() {
    return {
        panel: dump(panelRoot()),
        // 严重度 / 置信度 / 处置时间**各自显示的是哪一份**（中文名还是码值、display 还是 ISO），
        // 只看整棵树的文本是分不出来的 —— 码值可能藏在 `title` 里。
        severity: collect('ai-anomaly-item-severity'),
        confidence: collect('ai-anomaly-item-confidence'),
        when: collect('ai-anomaly-item-at'),
        state: api.state(),
        calls: calls.slice()
    };
}

// ---------------------------------------------------------------------------
//  操作
// ---------------------------------------------------------------------------
var OP = {
    // 容器重新建一个（历史面板每次重画都是整体重建 body）。
    remount: function () {
        els[PANEL_ID] = makeEl('div');
        return Promise.resolve(null);
    },
    load: function (arg) { return api.load(Number(arg)); },
    pick: function (arg) {
        var box = findIn(itemNode(arg), 'ai-anomaly-pick');
        box.checked = true;
        return fire(box, 'change');
    },
    unpick: function (arg) {
        var box = findIn(itemNode(arg), 'ai-anomaly-pick');
        box.checked = false;
        return fire(box, 'change');
    },
    // 备注是「敲进去」的：设 value 再抛 input（程序设 value 不触发 input，与真浏览器一致）。
    note: function (arg) {
        var cut = arg.indexOf('=');
        var box = findIn(itemNode(arg.slice(0, cut)), 'ai-anomaly-note');
        box.value = arg.slice(cut + 1);
        return fire(box, 'input');
    },
    batchnote: function (arg) {
        var box = findIn(panelRoot(), 'ai-anomaly-batch-note');
        box.value = arg;
        return fire(box, 'input');
    },
    act: function (arg) {
        var parts = arg.split(':');
        return fire(findIn(itemNode(parts[0]), 'ai-anomaly-act', parts[1]), 'click');
    },
    batch: function (arg) {
        return fire(findIn(panelRoot(), 'ai-anomaly-batch', arg), 'click');
    }
};

function reset(item) {
    els = {};
    els[PANEL_ID] = makeEl('div');
    calls = [];
    table = JSON.parse(JSON.stringify(item.table || {}));
    sandbox.fetch = makeFetch(table);
    // 真加载两份模块：历史面板那一份也真跑，`wiring` 与单测走的是同一份实现。
    vm.runInContext(fs.readFileSync(__ANOMALY_SCRIPT__, 'utf8'), sandbox);
    vm.runInContext(fs.readFileSync(__HISTORY_SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiAnomalyDisposition;
    history = sandbox.AiReportHistory;
}

var cases = __CASES__;

function runUnits() {
    var results = [];
    var chain = Promise.resolve();
    cases.forEach(function (item) {
        item.snaps = [];
        chain = chain.then(function () {
            reset(item);
            var inner = Promise.resolve();
            item.ops.forEach(function (op) {
                inner = inner.then(function () {
                    var cut = op.indexOf(':');
                    var name = cut === -1 ? op : op.slice(0, cut);
                    var arg = cut === -1 ? '' : op.slice(cut + 1);
                    return OP[name](arg);
                }).then(function () { item.snaps.push(snap()); });
            });
            // 驱动里出的错（比如「面板里没有第 13 条」）记下来停在这一条用例上：
            // 不记的话后面那些断言会变成「在一个空面板里没找到某样东西」，全是假绿。
            return inner.catch(function (error) {
                item.error = String((error && error.stack) || error);
            });
        }).then(function () {
            results.push({
                name: item.name, ops: item.ops,
                snaps: item.snaps, error: item.error || null
            });
        });
    });
    return chain.then(function () { return results; });
}

// ---------------------------------------------------------------------------
//  接线：历史面板打开之后，报告下面那块面板是不是真的活了
// ---------------------------------------------------------------------------
var WIRING_TABLE = __WIRING__;

function runWiring() {
    els = {};
    els[MODAL_ID] = makeEl('div');
    els[HISTORY_BODY_ID] = makeEl('div');
    calls = [];
    table = JSON.parse(JSON.stringify(WIRING_TABLE));
    sandbox.fetch = makeFetch(table);
    vm.runInContext(fs.readFileSync(__ANOMALY_SCRIPT__, 'utf8'), sandbox);
    vm.runInContext(fs.readFileSync(__HISTORY_SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiAnomalyDisposition;
    history = sandbox.AiReportHistory;
    history.track({historyUrl: '/ai-analysis/commit/7/history'});
    return history.open().then(function () {
        return {
            body: dump(els[HISTORY_BODY_ID]),
            panel: dump(els[PANEL_ID] || null),
            panelState: api.state(),
            calls: calls.slice()
        };
    });
}

var pure = __PURE__.map(function (item) {
    return {
        name: item.name,
        anomaliesUrl: api.anomaliesUrl(item.runId),
        dispositionUrl: api.dispositionUrl(item.anomalyId),
        batchUrl: api.batchUrl(item.runId),
        label: api.labelOf(item.dispositions, item.value),
        counts: api.countsText(item.payload),
        recount: api.recount(item.rows, item.dispositions),
        actionText: api.actionText(item.value, api.labelOf(item.dispositions, item.value)),
        choices: api.orderedChoices(item.dispositions).map(function (c) { return c.value; }),
        actionTitle: api.actionTitle(item.value, api.labelOf(item.dispositions, item.value)),
        tone: api.severityTone(item.severity),
        evidence: api.evidenceText(item.evidence)
    };
});

runWiring().then(function (wiring) {
    return runUnits().then(function (units) {
        process.stdout.write(JSON.stringify({units: units, pure: pure, wiring: wiring}));
    });
}).catch(function (error) {
    process.stderr.write(String((error && error.stack) || error));
    process.exit(1);
});
"""

ANOM_URL = "/ai-analysis/runs/2/anomalies"
BATCH_URL = "/ai-analysis/runs/2/anomalies/disposition"
HISTORY_URL = "/ai-analysis/commit/7/history"
REPORT_URL = "/ai-analysis/runs/2/report"


def _dispositions() -> list:
    return [
        {"value": "pending", "label": "待确认"},
        {"value": "confirmed", "label": "已确认"},
        {"value": "ignored", "label": "已忽略"},
    ]


def _anomalies() -> list:
    return [
        {
            "id": 13, "run_id": 2, "fingerprint": "a1b2c3",
            "title": "【道具】ID 被删除但生成文件仍在",
            "category": "config_id",
            "severity": "critical", "severity_label": "严重",
            "confidence": "very_high", "confidence_label": "很高",
            "evidence": ["config/道具表.xlsx 删除了 ID 1001", "build/lua/CfgItem.lua 里 1001 还在"],
            "commit_ref": "f0724d7d", "file_path": "config/道具表.xlsx",
            "impact": "老存档引用的道具失效", "suggestion": "确认是否有意下线",
            # 逐条断言（P0-01）。`heading` 是服务端拼好的「状态词 + 断言正文」，
            # `display` 是**标题安全**那一份（未证实的带前缀、已证实的不带）——
            # 两者都在这里给全，好让用例能钉住「界面印的是哪一份」。
            "claims": [
                {
                    "claim_id": "C1", "kind": "fact", "status": "unreadable",
                    "status_label": "证据读不到",
                    "display": "证据读不到：次数记账在批次交付之前执行。",
                    "heading": "证据读不到：次数记账在批次交付之前执行。",
                    "checked_scope": "路径 code/qz_server/RewardSvrMod.lua",
                },
                {
                    "claim_id": "C2", "kind": "fact", "status": "verified",
                    "status_label": "已证实",
                    "display": "客户端提示「部分奖励已发送至邮箱」被删除。",
                    "heading": "已证实：客户端提示「部分奖励已发送至邮箱」被删除。",
                    "checked_scope": "",
                },
            ],
            # 库里那个 naive-UTC 是 02:03:04 → 北京时间 10:03:04；服务端两个都给，
            # 界面**只许用** display 那个（测试里钉着「带 T 的那个不许出现」）。
            "disposition": "pending", "disposition_by": None,
            "disposition_at": None, "disposition_at_display": "",
            "disposition_note": None,
        },
        {
            "id": 14, "run_id": 2, "fingerprint": "d4e5f6",
            "title": "周版本合并后 CfgSkill.lua 少了一个技能",
            "category": "config_id",
            "severity": "high", "severity_label": "高",
            "confidence": "high", "confidence_label": "高",
            "evidence": ["build/lua/CfgSkill.lua 里 3002 不见了"],
            "commit_ref": "0a1b2c3d", "file_path": "build/lua/CfgSkill.lua",
            "impact": "已上线的技能失效", "suggestion": "回到上一版核对",
            "disposition": "ignored", "disposition_by": "张三",
            "disposition_at": "2026-09-20T02:03:04",
            "disposition_at_display": "2026-09-20 10:03:04",
            "disposition_note": "误报，已核对",
        },
        {
            "id": 15, "run_id": 2, "fingerprint": "778899aa",
            "title": "提交信息与改动不符",
            "category": "commit_msg",
            "severity": "high", "severity_label": "高",
            "confidence": "high", "confidence_label": "高",
            "evidence": ["提交说「修数值」，实际改的是技能表"],
            "commit_ref": "0a1b2c3d", "file_path": "config/技能表.xlsx",
            "impact": "", "suggestion": "",
            "disposition": "pending", "disposition_by": None,
            "disposition_at": None, "disposition_at_display": "",
            "disposition_note": None,
        },
    ]


def _by_id(anomaly_id: int) -> dict:
    return next(row for row in _anomalies() if row["id"] == anomaly_id)


def _counts(anomalies: list, dispositions: list) -> dict:
    counts = {item["value"]: 0 for item in dispositions}
    for row in anomalies:
        key = row["disposition"]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _read_body(anomalies=None, dispositions=None, ok=True, status=200, message=None) -> dict:
    anomalies = _anomalies() if anomalies is None else anomalies
    dispositions = _dispositions() if dispositions is None else dispositions
    if not ok:
        return {"ok": False, "status": status, "body": {"success": False, "message": message}}
    return {
        "body": {
            "success": True, "run_id": 2, "anomalies": anomalies,
            "counts": _counts(anomalies, dispositions), "total": len(anomalies),
            "dispositions": dispositions,
        }
    }


def _settled(anomaly_id: int, disposition: str, *, note=None, truncated=False) -> dict:
    """单条写接口的响应：一条已经处置过的行（与 `to_dict()` 同形）。"""
    row = dict(_by_id(anomaly_id))
    if disposition == "pending":
        # 撤销：服务端把处置人/时间/备注三个一起清空（display 那个也回到空串）。
        row.update(disposition="pending", disposition_by=None, disposition_at=None,
                   disposition_at_display="", disposition_note=None)
    else:
        row.update(disposition=disposition, disposition_by="张三",
                   disposition_at="2026-09-20T02:03:04",
                   disposition_at_display="2026-09-20 10:03:04", disposition_note=note)
    return {
        "body": {
            "success": True, "changed": 1, "note_truncated": truncated, "anomaly": row,
        }
    }


def _read_payload(anomalies=None, dispositions=None) -> dict:
    """读接口的**响应体本身**（`_read_body` 是外面那层 `{"body": ...}`，两种都要）。"""
    anomalies = _anomalies() if anomalies is None else anomalies
    dispositions = _dispositions() if dispositions is None else dispositions
    return {
        "success": True, "run_id": 2, "anomalies": anomalies,
        "counts": _counts(anomalies, dispositions), "total": len(anomalies),
        "dispositions": dispositions,
    }


def _cases() -> list:
    """每条用例自带它这一次要用的假响应表。"""
    return [
        # 1. 正常载入：三条都画出来，按服务端次序。
        {"name": "载入清单", "table": {ANOM_URL: _read_body()},
         "ops": ["load:2"]},

        # 2. 服务端给的次序**原样渲染**（喂进去的次序故意不是 severity 降序）。
        #    界面若「顺手再排一次」，这里就会红。
        {"name": "次序照服务端的走",
         "table": {ANOM_URL: _read_body(anomalies=[_by_id(15), _by_id(13), _by_id(14)])},
         "ops": ["load:2"]},

        # 3. 同一次运行重画（历史面板一次选中会重画两次）：不再取第二遍，
        #    而且用户已经敲进去的备注与勾选**不许丢**。
        {"name": "同一次运行重画不再取",
         "table": {ANOM_URL: _read_body()},
         "ops": ["load:2", "note:13=误报已核对", "pick:13", "remount", "load:2"]},

        # 4. 单条忽略：备注跟着这一条走，就地更新（不再取一次清单）。
        {"name": "单条忽略",
         "table": {
             ANOM_URL: _read_body(),
             "/ai-analysis/anomalies/13/disposition": _settled(13, "ignored", note="误报，已核对"),
         },
         "ops": ["load:2", "note:13=误报，已核对", "act:13:ignored"]},

        # 5. 撤销：提交 pending，且**不带备注**（带了也不落库，用户会以为记下了）。
        {"name": "撤销",
         "table": {
             ANOM_URL: _read_body(),
             "/ai-analysis/anomalies/14/disposition": _settled(14, "pending"),
         },
         "ops": ["load:2", "note:14=这一段不该被带走", "act:14:pending"]},

        # 6. 批量：ids 按清单次序、带上批量备注；返回的是**权威数据**，直接拿它重画。
        #    这里的 counts / total 故意与本地那三条**对不上**（10 条 vs 3 条）——
        #    只有这样才分得清「用了响应里的 counts」与「本地重算了一遍」。
        {"name": "批量处置",
         "table": {
             ANOM_URL: _read_body(),
             BATCH_URL: {"body": {
                 "success": True, "changed": 2, "note_truncated": False,
                 "anomalies": [
                     dict(_by_id(13), disposition="ignored", disposition_by="张三",
                          disposition_at="2026-09-20T02:03:04",
                          disposition_at_display="2026-09-20 10:03:04",
                          disposition_note="一次性核对"),
                     dict(_by_id(14)),
                     dict(_by_id(15)),
                 ],
                 "counts": {"pending": 0, "confirmed": 0, "ignored": 10},
                 "total": 10, "dispositions": _dispositions(),
             }},
         },
         "ops": ["load:2", "pick:13", "pick:14", "batchnote:一次性核对", "batch:ignored"]},

        # 7. 一条都没勾：批量按钮不可用，点了也不发请求（后端对空 ids 回 400）。
        {"name": "没勾选就不发批量请求",
         "table": {ANOM_URL: _read_body()},
         "ops": ["load:2", "batch:ignored"]},

        # 8. 写请求被拒（400）：服务端那句 message 要显示出来，且**本地状态不动** ——
        #    悄悄把那一行画成「已忽略」，用户会以为成功了。
        {"name": "写被拒",
         "table": {
             ANOM_URL: _read_body(),
             "/ai-analysis/anomalies/13/disposition": {
                 "ok": False, "status": 400,
                 "body": {"success": False,
                          "message": "处置状态只能是 pending、confirmed、ignored 之一"},
             },
         },
         "ops": ["load:2", "act:13:ignored"]},

        # 9. 读请求被拒（403）：同样要说出来，不留一片空白。
        {"name": "读被拒",
         "table": {ANOM_URL: _read_body(ok=False, status=403, message="Access denied.")},
         "ops": ["load:2"]},

        # 10. 这一次没有落库的结论（失败的运行、或模型确实什么都没报）。
        {"name": "空清单", "table": {ANOM_URL: _read_body(anomalies=[])}, "ops": ["load:2"]},

        # 11. 备注被服务端截断：要说出来（不说的话用户以为整段都存下了）。
        {"name": "备注被截断",
         "table": {
             ANOM_URL: _read_body(),
             "/ai-analysis/anomalies/13/disposition": _settled(
                 13, "ignored", note="很长很长的一大段", truncated=True),
         },
         "ops": ["load:2", "act:13:ignored"]},

        # 12. **状态名只认服务端**：换一套中文名（同一个码值），界面必须跟着换。
        {"name": "服务端换一套中文名",
         "table": {ANOM_URL: _read_body(dispositions=[
             {"value": "pending", "label": "还没看"},
             {"value": "confirmed", "label": "已核实"},
             {"value": "ignored", "label": "已作废"},
         ])},
         "ops": ["load:2"]},

        # 13. **反自检：`*_label` 缺失时不许回落到码值。**
        #     这一条只有码值（没有 `severity_label` / `confidence_label` /
        #     `disposition_at_display`，模拟前后端版本没对齐）。界面若写
        #     `row.severity_label || row.severity`，这里就会把 `weird` / `odd` /
        #     裸 ISO 印出来 —— 那是「平台没算」被伪装成「平台认识这个码」。
        {"name": "只有码值没有中文名",
         "table": {ANOM_URL: _read_body(anomalies=[{
             "id": 13, "run_id": 2, "fingerprint": "a1b2c3",
             "title": "【道具】ID 被删除但生成文件仍在", "category": "config_id",
             "severity": "weird", "confidence": "odd",
             "evidence": ["config/道具表.xlsx 删除了 ID 1001"],
             "file_path": "config/道具表.xlsx",
             "disposition": "ignored", "disposition_by": "张三",
             "disposition_at": "2026-09-20T02:03:04", "disposition_note": "误报，已核对",
         }])},
         "ops": ["load:2"]},

        # 14. **认不出的码值**：服务端原样把它回在 `*_label` 里（`report_document._label`
        #     的兜底），界面照印 —— 这与上一条（字段缺失）是两件事，必须分得开。
        {"name": "认不出的码值照印",
         "table": {ANOM_URL: _read_body(anomalies=[{
             "id": 14, "run_id": 2, "fingerprint": "d4e5f6",
             "title": "周版本合并后 CfgSkill.lua 少了一个技能", "category": "config_id",
             "severity": "weird", "severity_label": "weird",
             "confidence": "odd", "confidence_label": "odd",
             "evidence": ["build/lua/CfgSkill.lua 里 3002 不见了"],
             "file_path": "build/lua/CfgSkill.lua",
             "disposition": "ignored", "disposition_by": "张三",
             "disposition_at": "2026-09-20T02:03:04",
             "disposition_at_display": "2026-09-20 10:03:04",
             "disposition_note": "误报，已核对",
         }])},
         "ops": ["load:2"]},
    ]


def _pure() -> list:
    custom = [
        {"value": "pending", "label": "还没看"},
        {"value": "confirmed", "label": "已核实"},
        {"value": "ignored", "label": "已作废"},
    ]
    return [
        {"name": "默认中文名", "runId": 2, "anomalyId": 13, "value": "ignored",
         "dispositions": _dispositions(), "severity": "critical",
         "rows": _anomalies(), "payload": _read_payload()},
        {"name": "服务端换过名字", "runId": 2, "anomalyId": 13, "value": "pending",
         "dispositions": custom, "severity": "bogus",
         "rows": _anomalies(), "payload": _read_payload(dispositions=custom)},
        {"name": "认不出的码值", "runId": 2, "anomalyId": 13, "value": "whatever",
         "dispositions": _dispositions(), "severity": "", "rows": [], "payload": {}},
        {"name": "证据不是字符串", "runId": 2, "anomalyId": 13, "value": "pending",
         "dispositions": _dispositions(), "severity": "high", "rows": [],
         "evidence": {"file": "a.xlsx", "line": 12}, "payload": {}},
        {"name": "证据是空值", "runId": 2, "anomalyId": 13, "value": "pending",
         "dispositions": _dispositions(), "severity": "high", "rows": [],
         "evidence": None, "payload": {}},
    ]


def _wiring_table() -> dict:
    return {
        HISTORY_URL: {"body": {
            "success": True, "kind": "commit", "total": 1, "truncated": False,
            "limit": 20, "window_days": 90,
            "runs": [{
                "run_id": 2, "created_at_display": "2026-09-12 19:13:04",
                "status": "succeeded", "status_label": "已有结论", "risk_label": "高",
                "scope_label": "全量", "trigger_label": "手动", "focus_label": "",
                "summary": "把奖励发放改成先发后扣。", "anomaly_count": 3,
                "suppressed_count": 0, "exportable": True,
            }],
        }},
        REPORT_URL: {"body": {
            "success": True, "run_id": 2,
            "result": {"run_id": 2, "status": "succeeded", "response_text": "报告正文",
                       "created_at_display": "2026-09-12 19:13:04"},
        }},
        ANOM_URL: {"body": _read_payload()},
    }


def _run(cases: list, pure: list, wiring: dict) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的处置面板断言")
    driver = (
        DRIVER.replace("__ANOMALY_SCRIPT__", json.dumps(str(ANOMALY_SCRIPT)))
        .replace("__HISTORY_SCRIPT__", json.dumps(str(HISTORY_SCRIPT)))
        .replace("__CASES__", json.dumps(cases, ensure_ascii=False))
        .replace("__PURE__", json.dumps(pure, ensure_ascii=False))
        .replace("__WIRING__", json.dumps(wiring, ensure_ascii=False))
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def run() -> dict:
    return _run(_cases(), _pure(), _wiring_table())


def _case(run: dict, name: str) -> dict:
    item = next(entry for entry in run["units"] if entry["name"] == name)
    # 驱动里出的错（找不到节点之类）会记在这里。不先看这一条，后面的断言会变成
    # 「在一个空面板里没找到某样东西」—— 全绿，但什么都没验。
    assert not item["error"], f"{name} 的驱动报错：{item['error']}"
    assert item["snaps"], f"{name} 一条快照都没有"
    return item


def _last(run: dict, name: str) -> dict:
    return _case(run, name)["snaps"][-1]


def _before(run: dict, name: str) -> dict:
    """最后一步**之前**的那张快照（对比「操作前 / 操作后」用）。"""
    snaps = _case(run, name)["snaps"]
    assert len(snaps) >= 2, f"{name} 的快照不够对比"
    return snaps[-2]


def _loads(snap: dict, url: str) -> list:
    return [call for call in snap["calls"] if call["url"] == url]


def _pure_by_name(run: dict) -> dict:
    return {item["name"]: item for item in run["pure"]}


# --------------------------------------------------------------------------
#  纯函数：地址与口径
# --------------------------------------------------------------------------
def test_the_urls_are_the_ones_the_routes_serve(run):
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["anomaliesUrl"] == "/ai-analysis/runs/2/anomalies"
    assert pure["默认中文名"]["dispositionUrl"] == "/ai-analysis/anomalies/13/disposition"
    # 批量与读只差一个后缀，且都挂在**运行**上（不是运行里的某一条）。
    assert pure["默认中文名"]["batchUrl"] == "/ai-analysis/runs/2/anomalies/disposition"


def test_the_state_name_always_comes_from_the_server(run):
    """界面里**没有**那张 `pending → 待确认` 的表：换一套名字，取出来的就是那一套。"""
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["label"] == "已忽略"
    assert pure["服务端换过名字"]["label"] == "还没看"
    # 认不出来的码值回落成码值 —— 不编一个中文名出来。
    assert pure["认不出的码值"]["label"] == "whatever"


def test_the_action_words_follow_the_server_label(run):
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["actionText"] == "已忽略"
    # 「撤销」是动作名，但**撤到哪个状态**必须用服务端那个名字说清楚。
    assert pure["服务端换过名字"]["actionText"] == "撤销"
    assert "还没看" in pure["服务端换过名字"]["actionTitle"]


def test_the_undo_button_is_laid_out_last(run):
    """三个按钮的排布：**撤销在最后**（服务端那份名单里 `pending` 是第一个）。

    照抄服务端次序的话，一条待确认的结论上第一个按钮就是「撤销」—— 读起来像
    「这一条现在要做的事是撤销」。
    """
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["choices"] == ["confirmed", "ignored", "pending"]


def test_the_counts_line_is_built_from_the_server_labels(run):
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["counts"] == "共 3 条 · 待确认 2 · 已确认 0 · 已忽略 1"
    # 次序也取服务端那份清单的次序（不是界面自己定的）。
    assert pure["服务端换过名字"]["counts"] == "共 3 条 · 还没看 2 · 已核实 0 · 已作废 1"
    # 没有 dispositions 时不编一行「待确认 0」出来。
    assert pure["认不出的码值"]["counts"] == "共 0 条"


def test_the_local_recount_counts_every_row(run):
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["recount"] == {"pending": 2, "confirmed": 0, "ignored": 1}
    assert pure["认不出的码值"]["recount"] == {"pending": 0, "confirmed": 0, "ignored": 0}


def test_severity_only_gets_a_color_from_the_code(run):
    """颜色档来自**码值**（服务端那条 `severity`），中文名来自 `severity_label`。

    这两件事在渲染里是分开的：颜色是界面的事（只是一档颜色，不是名字），
    名字一个字都不许界面自己写。
    """
    pure = _pure_by_name(run)
    assert pure["默认中文名"]["tone"] == "danger"       # critical
    assert pure["证据不是字符串"]["tone"] == "warning"   # high
    assert pure["认不出的码值"]["tone"] == "secondary"   # 认不出来


def test_structural_evidence_is_shown_as_text_not_as_object_object(run):
    pure = _pure_by_name(run)
    assert pure["证据不是字符串"]["evidence"] == '{"file":"a.xlsx","line":12}'
    assert pure["证据是空值"]["evidence"] == ""


# --------------------------------------------------------------------------
#  清单的渲染
# --------------------------------------------------------------------------
def test_the_list_shows_what_a_decision_needs(run):
    """每一条都要有：严重度、标题、文件、置信度、逐条证据、当前处置与处置人。

    严重度 / 置信度显示的是**服务端算好的中文名**（`severity_label` /
    `confidence_label`），码值只在 `title` 里（对照模型原文用）。
    """
    last = _last(run, "载入清单")
    body = last["panel"]

    # 三条：严重 / 高 / 高（中文名），不是 critical / high。
    assert [item["text"] for item in last["severity"]] == ["严重", "高", "高"]
    assert [item["title"] for item in last["severity"]] == ["critical", "high", "high"], (
        "码值要留在 title 里 —— 读的人得能拿它对上报告正文里模型原文的措辞"
    )
    assert "【道具】ID 被删除但生成文件仍在" in body
    assert "config/道具表.xlsx" in body
    assert [item["text"] for item in last["confidence"]] == ["置信度 很高", "置信度 高", "置信度 高"]
    assert "config/道具表.xlsx 删除了 ID 1001" in body
    assert "build/lua/CfgItem.lua 里 1001 还在" in body, "证据要逐条列出来"

    # 有处置的那一条：状态 + 处置人 + 时间 + 备注。
    assert "已忽略" in body
    assert "处置人：张三" in body
    assert "备注：误报，已核对" in body
    # 还没处置的那一条不该凭空多出这三样。
    assert body.count("处置人：") == 1, "只有真的处置过的那一条才显示处置人"


def test_the_claim_lines_print_the_status_word_once(run):
    """逐条断言那一行印的是服务端的 `heading`：**状态词只说一次**。

    从前这里拼的是 `status_label + ' —— ' + display`，而 `display` 自己就带着状态前缀
    （「证据读不到：…」）—— 界面上于是印成「证据读不到 —— 证据读不到：次数记账在批次
    交付之前执行。」（报告那一节同款，2026-09-24 run 63 一起修的）。
    """
    body = _last(run, "载入清单")["panel"]

    assert "[C1] 证据读不到：次数记账在批次交付之前执行。" in body, (
        "断言那一行没有照 `heading` 印（服务端拼好的状态 + 正文）"
    )
    assert "证据读不到 —— " not in body, "状态词印了两遍 —— 界面又在自己拼 status_label"
    assert "[C2] 已证实：客户端提示「部分奖励已发送至邮箱」被删除。" in body, (
        "已证实的那一条丢了状态词 —— `display` 对它是裸正文，界面必须读 `heading`"
    )
    assert "（查过：路径 code/qz_server/RewardSvrMod.lua）" in body


def test_the_disposition_time_is_the_server_rendered_one(run):
    """时间只用 `disposition_at_display`（服务端算好的北京时间）。

    界面自己 `T→空格` / `new Date()` / `+8` 都不许：前两个会让 UTC 的钟点长得像
    北京时间（`tests/test_ai_analysis_time_display.py` 记的就是这个缺陷）。
    """
    last = _last(run, "载入清单")

    assert [item["text"] for item in last["when"]] == ["时间：2026-09-20 10:03:04"], (
        "时间没有用服务端那个显示串（或者那条没处置的也显示了时间）"
    )
    # 库里那个 naive-UTC 的裸 ISO **一个字都不许出现**（它在响应里是另一个字段）。
    assert "2026-09-20T02:03:04" not in last["panel"], (
        "把 disposition_at 那个裸 ISO 印出来了 —— 它是 UTC，读起来却像北京时间"
    )
    assert "T02:03:04" not in last["panel"]


def test_a_missing_display_time_shows_no_time_line(run):
    """没有 `disposition_at_display`（空串 / 字段缺失）→ 那一行整个不显示，
    不许回落去印 `disposition_at`。"""
    last = _last(run, "只有码值没有中文名")

    assert last["when"] == [], last["when"]
    assert "时间：" not in last["panel"]
    assert "2026-09-20T02:03:04" not in last["panel"], (
        "display 缺失时回落去印了那个 naive-UTC 的裸 ISO"
    )


def test_a_missing_label_is_not_replaced_by_the_code(run):
    """**反自检**：`severity_label` / `confidence_label` 缺失时不许回落到码值。

    回落会让「平台没算这一项」与「平台认识这个码」长得一模一样 —— 而后者才是
    `severity_label === severity` 的含义（服务端原样回码值，界面照样直接印）。
    """
    last = _last(run, "只有码值没有中文名")
    body = last["panel"]

    assert last["severity"] == [], f"标签缺失时印了东西出来：{last['severity']}"
    assert last["confidence"] == [], last["confidence"]
    assert "weird" not in body, "把 severity 的码值当成中文名印出来了"
    assert "odd" not in body, "把 confidence 的码值当成中文名印出来了"
    # 那一条的其他部分照常显示（缺的是标签，不是整条结论）。
    assert "【道具】ID 被删除但生成文件仍在" in body
    assert "处置人：张三" in body and "备注：误报，已核对" in body


def test_an_unknown_code_is_printed_as_the_server_sent_it(run):
    """认不出的码由**服务端**原样回在 `*_label` 里，界面照印。

    这与上一条（字段缺失、什么都不印）是**两件不同的事**：
    这一条是「服务端说：我不认识这个码」，界面照它说的印；上一条是「服务端没算」。
    界面若写成 `row.severity_label || row.severity`，两条就长得一模一样了。
    """
    last = _last(run, "认不出的码值照印")

    assert [item["text"] for item in last["severity"]] == ["weird"]
    assert [item["text"] for item in last["confidence"]] == ["置信度 odd"]
    assert [item["text"] for item in last["when"]] == ["时间：2026-09-20 10:03:04"]


def test_the_actions_are_laid_out_in_the_rendered_row(run):
    """渲染出来的那一条里，按钮的次序也要对（纯函数对了、渲染反了是另一回事）。"""
    body = _last(run, "载入清单")["panel"]
    item = body[body.index('data-anomaly-id="13"'):body.index('data-anomaly-id="14"')]
    assert item.index('data-action="confirmed"') < item.index('data-action="ignored"')
    assert item.index('data-action="ignored"') < item.index('data-action="pending"'), item


def test_the_list_follows_the_order_the_server_gave(run):
    """**不许再排一次**：界面按 id 或按严重度重排，上下两处就会是两种次序。"""
    body = _last(run, "次序照服务端的走")["panel"]

    first = body.index("提交信息与改动不符")          # 服务端把它放在了第一条
    second = body.index("【道具】ID 被删除但生成文件仍在")
    third = body.index("周版本合并后 CfgSkill.lua 少了一个技能")
    assert first < second < third, "渲染次序与响应里的次序不一致（有人又排了一遍？）"


def test_the_counts_and_the_selection_start_from_the_response(run):
    last = _last(run, "载入清单")
    assert "共 3 条 · 待确认 2 · 已确认 0 · 已忽略 1" in last["panel"]
    assert "已选 0 条" in last["panel"]
    assert last["state"]["ids"] == [13, 14, 15]
    assert last["state"]["counts"] == {"pending": 2, "confirmed": 0, "ignored": 1}


def test_the_batch_buttons_are_disabled_until_something_is_checked(run):
    """**没选任何条目时批量按钮必须不可用** —— 后端会拒绝空 ids，界面不该让人走到那一步。"""
    before = _before(run, "没勾选就不发批量请求")
    assert "已选 0 条" in before["panel"]
    assert before["panel"].count("disabled") == 3, "三个批量按钮在一开始都该是禁用的"


def test_an_empty_list_says_so_instead_of_showing_a_bare_table(run):
    last = _last(run, "空清单")
    assert "这一次没有落库的结构化结论条目。" in last["panel"]


# --------------------------------------------------------------------------
#  取数：一次就够
# --------------------------------------------------------------------------
def test_the_same_run_is_not_fetched_twice(run):
    """历史面板一次选中会重画两次 → 不认这一条，每翻一次就取两遍。"""
    last = _last(run, "同一次运行重画不再取")
    assert len(_loads(last, ANOM_URL)) == 1, "同一次运行被取了两次"
    # 重画之后内容还在（新容器按 state 重画），用户敲进去的备注与勾选也还在。
    assert "【道具】ID 被删除但生成文件仍在" in last["panel"]
    assert "误报已核对" in last["panel"], "重画把用户已经敲进去的备注弄丢了"
    assert "已选 1 条" in last["panel"], "重画把勾选弄丢了"


# --------------------------------------------------------------------------
#  单条处置
# --------------------------------------------------------------------------
def test_a_single_disposition_posts_the_note_and_updates_in_place(run):
    case = _case(run, "单条忽略")
    after = case["snaps"][-1]
    posts = _loads(after, "/ai-analysis/anomalies/13/disposition")

    assert len(posts) == 1, "点一次「已忽略」应该只发一次请求"
    assert posts[0]["method"] == "POST"
    assert posts[0]["body"] == {"disposition": "ignored", "note": "误报，已核对"}

    # 就地更新：不再取一次清单。
    assert len(_loads(after, ANOM_URL)) == 1, "处置完之后又去取了一遍清单"
    assert "已忽略 2" in after["panel"], "计数没有跟着那条的处置状态动"
    assert "处置人：张三" in after["panel"]
    assert "备注：误报，已核对" in after["panel"]
    assert after["state"]["counts"] == {"pending": 1, "confirmed": 0, "ignored": 2}


def test_undo_posts_pending_and_leaves_the_note_behind(run):
    """撤销 = 提交 `pending`，且**不带备注**（服务端会把三个伴随字段一起清空）。"""
    case = _case(run, "撤销")
    after = case["snaps"][-1]
    posts = _loads(after, "/ai-analysis/anomalies/14/disposition")

    assert len(posts) == 1
    assert posts[0]["body"] == {"disposition": "pending", "note": ""}, (
        "撤销把备注一起带上去了 —— 那段字不会落库，而用户会以为记下了"
    )
    assert after["state"]["counts"]["pending"] == 3
    # 撤销之后那一条不该再挂着上一轮的处置人 / 备注。
    assert "处置人：" not in after["panel"], "撤销之后还挂着上一轮的处置人"
    assert "备注：误报，已核对" not in after["panel"], "撤销之后还挂着上一轮的备注"


# --------------------------------------------------------------------------
#  批量处置
# --------------------------------------------------------------------------
def test_batch_posts_the_checked_ids_and_uses_the_returned_counts(run):
    case = _case(run, "批量处置")
    posts = _loads(case["snaps"][-1], BATCH_URL)
    assert len(posts) == 1
    assert posts[0]["method"] == "POST"
    # ids 按**清单里的次序**（也就是用户看到的那一列）。
    assert posts[0]["body"] == {
        "ids": [13, 14], "disposition": "ignored", "note": "一次性核对"
    }

    after = case["snaps"][-1]
    assert len(_loads(after, ANOM_URL)) == 1, "批量处置完之后又去取了一遍清单"
    # 用的是**响应里那份 counts**（故意与本地那三条对不上：10 vs 3）。
    assert "共 10 条" in after["panel"], "没有用响应里的 total"
    assert "已忽略 10" in after["panel"], "没有用响应里的 counts（本地重算了一遍？）"
    assert after["state"]["total"] == 10


def test_batch_with_nothing_checked_sends_nothing(run):
    case = _case(run, "没勾选就不发批量请求")
    after = case["snaps"][-1]
    assert _loads(after, BATCH_URL) == [], "一条都没勾却发了批量请求"
    assert "已选 0 条" in after["panel"]


def test_checking_a_row_enables_the_batch_buttons(run):
    """勾上之后必须**能用**：一直禁用的话，「不可用」这条判定就没有反面了。"""
    snaps = _case(run, "批量处置")["snaps"]
    after_first_pick = snaps[1]
    assert "已选 1 条" in after_first_pick["panel"]
    assert after_first_pick["panel"].count("disabled") == 0

    after_second_pick = snaps[2]
    assert "已选 2 条" in after_second_pick["panel"]
    # 取消勾选要能退回去（勾错了不至于只能刷新页面）。
    assert after_second_pick["state"]["selected"] == [13, 14]


# --------------------------------------------------------------------------
#  失败：把服务端那句 message 显示出来
# --------------------------------------------------------------------------
def test_a_rejected_write_shows_the_server_message_and_changes_nothing(run):
    case = _case(run, "写被拒")
    after = case["snaps"][-1]

    assert "处置状态只能是 pending、confirmed、ignored 之一" in after["panel"], (
        "服务端那句 message 被吞掉了 —— 用户看到的是「点了没反应」"
    )
    assert after["state"]["error"]
    # **本地状态不许动**：把那一行悄悄画成「已忽略」，用户会以为成功了。
    # （第 14 条本来就处置过，所以这里比的是「处置人只出现一次」而不是「一次都没有」。）
    assert after["panel"].count("处置人：") == 1, "被拒的那一条被本地画成了已处置"
    assert after["state"]["counts"] == {"pending": 2, "confirmed": 0, "ignored": 1}
    # 清单还在（不是被一句错误换掉了）。
    assert "【道具】ID 被删除但生成文件仍在" in after["panel"]


def test_a_rejected_read_shows_the_server_message(run):
    after = _last(run, "读被拒")
    assert "Access denied." in after["panel"]
    assert after["state"]["error"]


def test_a_truncated_note_is_disclosed(run):
    """备注被截断是**静默**的数据变化 —— 必须说出来。"""
    after = _last(run, "备注被截断")
    assert "截断" in after["panel"]
    assert "处置人：张三" in after["panel"], "截图之外，那次处置本身是成功的"


# --------------------------------------------------------------------------
#  中文名只认服务端（换一套名字，界面必须跟着换）
# --------------------------------------------------------------------------
def test_a_renamed_state_reaches_every_place_it_appears(run):
    """**这条是「不许写死中文名」的判据。**

    界面里抄一份 `pending → 待确认`，这条就红：面板上会出现服务端从来没给过的字。
    """
    body = _last(run, "服务端换一套中文名")["panel"]

    assert "还没看" in body            # 每一条的状态徽章
    assert "已作废" in body            # 按钮
    assert "已核实" in body            # 按钮
    for hardcoded in ("待确认", "已确认", "已忽略"):
        assert hardcoded not in body, f"界面自己写死了「{hardcoded}」"


# --------------------------------------------------------------------------
#  接线：面板由历史弹层挂出来
# --------------------------------------------------------------------------
def test_the_history_modal_mounts_and_fills_the_panel(run):
    """打开「历次结论」→ 选中那一次 → 报告**下面**那块面板真的有清单。

    只断模块自己（`units` 那些用例）拦不住「历史模块没建容器 / 建了但没调 load /
    容器还没 append 就去按 id 找」—— 那三种的症状都是「这一块永远空着，且不报错」。
    """
    wiring = run["wiring"]
    body = wiring["body"]

    assert 'id="aiAnomalyPanel"' in body, "弹层里没有处置面板的容器"
    assert "结构化结论与处置" in body, "容器在，但里面什么都没有（没接上 load？）"
    assert "【道具】ID 被删除但生成文件仍在" in body
    assert "已选 0 条" in body
    assert "共 3 条 · 待确认 2 · 已确认 0 · 已忽略 1" in body
    # 面板挂在**报告下面**（报告是叙事，清单是它的可操作形态）。
    assert body.index("aiHistoryReportBody") < body.index('id="aiAnomalyPanel"')
    assert wiring["panelState"]["ids"] == [13, 14, 15]

    urls = [call["url"] for call in wiring["calls"]]
    # 三次取数，各一次：历次结论的列表、这一次的报告、这一次的结论行。
    # （先取结论行后取报告 —— 面板挂在报告里，取数不等报告回来。）
    assert urls[0] == HISTORY_URL
    assert sorted(urls) == sorted([HISTORY_URL, REPORT_URL, ANOM_URL]), urls


# --------------------------------------------------------------------------
#  模板那一侧：三份抽屉都要引这个模块
# --------------------------------------------------------------------------
TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def test_every_drawer_loads_the_disposition_module():
    """脚本漏引一份，症状是「这一页的报告下面永远空着」，而且不报错。"""
    for name in TEMPLATES:
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "js/ai_anomaly_disposition.js" in source, f"{name} 没有引处置面板那个模块"
        # 必须在历史模块**之后**（它建容器、历史模块调 load）——
        # 顺序反了不报错，只是这一块永远空着。
        assert source.index("ai_anomaly_disposition.js") > source.index(
            "js/ai_report_history.js"
        ), f"{name} 里处置模块引在了历史模块前面"
