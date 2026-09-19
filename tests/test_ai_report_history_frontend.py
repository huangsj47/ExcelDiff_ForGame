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
`addEventListener`。`dump()` 把节点树摊成文本，断言就写在那上面 —— 比逐个属性断言
更接近「用户看到的是什么」。
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

function makeEl(tag) {
    var node = {
        tagName: tag, id: '', className: '', type: '', hidden: false,
        children: [], _attrs: {}, _handlers: {}, _text: '', _html: ''
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
    return node;
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

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiReportHistory;

// 假 fetch：按 URL 返回预置的响应（`__FETCH__`）。
var fetchLog = [];
function makeFetch(table) {
    return function (url) {
        fetchLog.push(url);
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
    select: function (runId) { return api.select(Number(runId), makeFetch(fetchTable)); },
    setCurrent: function (runId) { currentRunId = runId; }
};

var fetchTable = __FETCH__;
var HISTORY_URL = '/ai-analysis/commit/7/history';

// 场景可以只给自己那一份列表（「只有一次结论」「一次都没跑过」这两条要靠它）。
function tableFor(item) {
    if (!item.rows) return __FETCH__;
    var table = JSON.parse(JSON.stringify(__FETCH__));
    table[HISTORY_URL].body.runs = item.rows;
    table[HISTORY_URL].body.total = item.rows.length;
    table[HISTORY_URL].body.in_progress = !!item.in_progress;
    return table;
}

function snap() {
    return {
        body: dump(els[BODY_ID]),
        footerHref: els[DRAWER_FOOTER_LINK].getAttribute('href'),
        state: api.state(),
        // 弹层里那个导出链接（由 JS 建，带固定 id）
        historyHref: (function () {
            var found = null;
            (function walk(node) {
                if (!node || found) return;
                if (node.id === 'aiHistoryExportMdLink') { found = node.getAttribute('href'); }
                (node.children || []).forEach(walk);
            })(els[BODY_ID]);
            return found;
        })(),
        fetchLog: fetchLog.slice()
    };
}

function reset(item) {
    seedEls();
    fetchLog = [];
    currentRunId = item.currentRunId === undefined ? null : item.currentRunId;
    fetchTable = tableFor(item);
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
    reset({});
    return {
        name: item.name,
        url: api.historyUrlFor(item.kind, item.id),
        exportHref: api.exportHref(item.runId),
        reportUrl: api.reportUrl(item.runId),
        listNote: api.listNote(item.payload),
        mark: api.markText(item.row, item.currentRunId),
        tone: api.statusTone(item.status),
        rowMeta: api.rowMeta(item.row || {})
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
        },
        {
            "run_id": 41, "created_at_display": "2026-09-10 09:00:00", "status": "failed",
            "status_label": "分析失败", "risk_label": "", "scope_label": "全量",
            "trigger_label": "定时", "focus_label": "", "summary": "失败：额度用完了",
            "anomaly_count": 0, "suppressed_count": 0, "exportable": False,
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
            {"name": "被截断", "payload": {"runs": _rows(), "total": 40, "truncated": True,
                                          "window_days": 90}, "runId": 1},
            {"name": "当前这一份", "runId": 1,
             "row": {"run_id": 1, "created_at_display": "2026-09-12 19:13:04"},
             "currentRunId": 1},
            {"name": "历史那一份", "runId": 1,
             "row": {"run_id": 2, "created_at_display": "2026-09-10 09:00:00"},
             "currentRunId": 1},
            {"name": "成功", "status": "succeeded", "runId": 1},
            {"name": "失败", "status": "failed", "runId": 1},
            {"name": "进行中", "status": "running", "runId": 1},
            {"name": "一行的次要信息", "runId": 1,
             "row": {"scope_label": "全量", "trigger_label": "定时", "focus_label": "仅配表仓库",
                     "anomaly_count": 3}},
            {"name": "次要信息全空", "runId": 1, "row": {}},
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
    assert pure["进行中"]["tone"] == "secondary"
    assert pure["一行的次要信息"]["rowMeta"] == "全量 · 定时 · 仅配表仓库 · 异常 3 条"
    assert pure["次要信息全空"]["rowMeta"] == ""


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
