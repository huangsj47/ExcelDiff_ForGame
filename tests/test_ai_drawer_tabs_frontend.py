# -*- coding: utf-8 -*-
"""AI 抽屉的两个标签（思考过程 / 完整结论）—— 用 node 真跑 `static/js/ai_drawer_tabs.js`。

## 为什么必须真跑

这段代码的全部内容就是**几个判定**，而它们全都是「看起来对」的那种：

* 打开抽屉时落在哪个标签（在跑 → 思考过程；没在跑 → 完整结论）；
* 跑完了要不要自动切回结论——**用户自己点过标签时不许切**（把他从正在读的过程里
  拽走是最招人烦的一种「贴心」）；
* 切回去以后要不要打未读点；
* 关抽屉之后这次的选择该忘掉。

静态断言挡不住「判定写反了」这类错误（`=== 'running' ? THINK : REPORT` 反过来写也是
合法代码），所以按仓库既有做法（`tests/test_ai_context_notice.py`、`tests/test_ai_usage_drawer_frontend.py`）
把真函数放进一个带假 DOM 的 node 沙箱里跑，断言**每一步之后的界面状态**。

## 假 DOM 说明

只实现这段代码真正用到的那几样：`getElementById` / `classList` / `setAttribute` /
`removeAttribute` / `addEventListener` / `hidden`。`paint()` 是这里唯一碰 DOM 的地方，
标签的「当前在哪一页」完全由 `aria-selected` 与 `panel.hidden` 表达 —— 也正该拿它们断言。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_drawer_tabs.js"

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

var els = {};
var focusLog = [];

function makeEl(id) {
    var node = {
        id: id, className: '', hidden: false,
        _attrs: {}, _classes: [], _handlers: {}
    };
    node.setAttribute = function (k, v) { node._attrs[k] = String(v); };
    node.getAttribute = function (k) {
        return node._attrs[k] === undefined ? null : node._attrs[k];
    };
    node.removeAttribute = function (k) { delete node._attrs[k]; };
    node.addEventListener = function (t, fn) { node._handlers[t] = fn; };
    node.focus = function () { focusLog.push(id); };
    node.classList = {
        add: function (c) { if (node._classes.indexOf(c) < 0) node._classes.push(c); },
        remove: function (c) {
            var i = node._classes.indexOf(c); if (i >= 0) node._classes.splice(i, 1);
        },
        contains: function (c) { return node._classes.indexOf(c) >= 0; }
    };
    return node;
}

// 三份模板里同名的那几个 id —— 硬写在这里是**故意的**：它们是三份模板的共同契约，
// 改名就该让这个测试红，而不是被 `api.IDS` 悄悄跟着改。
var SHARED = {
    thinkTab: 'aiDrawerTabThink',
    reportTab: 'aiDrawerTabReport',
    thinkPanel: 'aiDrawerPanelThink',
    dot: 'aiDrawerTabReportDot'
};
var REPORT_PANEL = 'aiAnalysisOutput';

var sandbox = { window: {}, console: console };
sandbox.window = sandbox;
sandbox.document = {
    // 与真 DOM 一样：没有的 id 返回 null（**不按需造**）——这样模块要是把面板 id
    // 认错了，`paint()` 会当场炸，而不是对着一个凭空冒出来的元素安静地画。
    getElementById: function (id) { return els[id] || null; }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiDrawerTabs;

function el(id) { return els[id] || null; }

function seedEls() {
    els = {};
    Object.keys(SHARED).forEach(function (key) { els[SHARED[key]] = makeEl(SHARED[key]); });
}
seedEls();

var thinkShown = 0;
var OP = {
    init: function () {
        // 正文框是各页原有的元素，这里按「这个页面上它长这样」造出来。
        els[REPORT_PANEL] = makeEl(REPORT_PANEL);
        api.init({
            reportPanel: REPORT_PANEL,
            onShowThink: function () { thinkShown += 1; }
        });
    },
    markRunning: function () { api.markRunning(); },
    markSettled: function () { api.markSettled(); },
    reset: function () { api.reset(); },
    clickThink: function () { el(SHARED.thinkTab)._handlers.click(); },
    clickReport: function () { el(SHARED.reportTab)._handlers.click(); },
    keyRight: function () {
        el(SHARED.thinkTab)._handlers.keydown({ key: 'ArrowRight', preventDefault: function () {} });
    },
    keyLeft: function () {
        el(SHARED.reportTab)._handlers.keydown({ key: 'ArrowLeft', preventDefault: function () {} });
    },
    keyHome: function () {
        el(SHARED.reportTab)._handlers.keydown({ key: 'Home', preventDefault: function () {} });
    },
    keyEnd: function () {
        el(SHARED.thinkTab)._handlers.keydown({ key: 'End', preventDefault: function () {} });
    }
};

function snap() {
    return {
        current: api.current(),
        state: api.state(),
        pinned: api.isPinned(),
        thinkSelected: el(SHARED.thinkTab).getAttribute('aria-selected'),
        reportSelected: el(SHARED.reportTab).getAttribute('aria-selected'),
        thinkHidden: !!el(SHARED.thinkPanel).hidden,
        reportHidden: !!el(REPORT_PANEL).hidden,
        dotHidden: !!el(SHARED.dot).hidden,
        reportLabel: el(SHARED.reportTab).getAttribute('aria-label'),
        reportUnread: el(SHARED.reportTab).classList.contains('has-unread'),
        thinkTabindex: el(SHARED.thinkTab).getAttribute('tabindex'),
        reportTabindex: el(SHARED.reportTab).getAttribute('tabindex')
    };
}

var cases = __CASES__;
var results = cases.map(function (item) {
    // 每个场景从干净的模块状态开始：重新加载一遍脚本（模块内部有状态）。
    seedEls();
    focusLog = [];
    thinkShown = 0;
    vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiDrawerTabs;
    var snaps = item.ops.map(function (op) {
        OP[op]();
        return snap();
    });
    return { name: item.name, snaps: snaps, thinkShown: thinkShown, focusLog: focusLog };
});

// 纯函数：打开抽屉（或状态变化）时该落在哪个标签。
var pure = [];
__PURE__.forEach(function (item) {
    pure.push({
        name: item.name,
        defaultTab: api.defaultTab(item.state, item.pinned, item.active),
        markUnread: api.shouldMarkUnread(item.state, item.pinned, item.active)
    });
});

process.stdout.write(JSON.stringify({cases: results, pure: pure, ids: api.IDS}));
"""


def _run(cases: list, pure: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的标签状态机断言")
    driver = (
        DRIVER.replace("__SCRIPT__", json.dumps(str(SCRIPT)))
        .replace("__CASES__", json.dumps(cases, ensure_ascii=False))
        .replace("__PURE__", json.dumps(pure, ensure_ascii=False))
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
            # 1. 页面刚打开：什么都没跑过 → 完整结论，思考面板藏着。
            {"name": "初始", "ops": ["init"]},
            # 2. 点「重新分析」：切到思考过程（那时唯一有内容的标签就是它）。
            {"name": "开跑", "ops": ["init", "markRunning"]},
            # 3. 跑完了、用户没自己点过标签 → 自动切回完整结论。
            {"name": "跑完自动切回", "ops": ["init", "markRunning", "markSettled"]},
            # 4. 用户自己点了「思考过程」再跑完 → **不许把他拽走**，只给结论打未读点。
            {
                "name": "用户自己选的标签不被抢走",
                "ops": ["init", "markRunning", "clickThink", "markSettled"],
            },
            # 5. 未读点在他切回结论时消失。
            {
                "name": "回到结论时未读点消失",
                "ops": ["init", "markRunning", "clickThink", "markSettled", "clickReport"],
            },
            # 6. 关抽屉清掉「自己选过」：下次打开还是按默认走。
            {
                "name": "关抽屉后不再记住",
                "ops": ["init", "markRunning", "clickThink", "reset", "markSettled"],
            },
            # 7. 键盘：只有两个标签，所以左右键都是「换到另一个」。
            {"name": "方向键从结论到过程", "ops": ["init", "keyRight"]},
            {"name": "方向键从过程回结论", "ops": ["init", "clickThink", "keyRight"]},
            {"name": "左方向键同样换标签", "ops": ["init", "clickThink", "keyLeft"]},
            {"name": "Home 回过程", "ops": ["init", "keyEnd", "keyHome"]},
            {"name": "End 到结论", "ops": ["init", "keyHome", "keyEnd"]},
            # 8. 没在跑的时候打开一个早跑完的目标：停在结论。
            {"name": "非跑动打开", "ops": ["init", "markSettled"]},
        ],
        [
            {"name": "在跑/未固定", "state": "running", "pinned": False, "active": "report"},
            {"name": "在跑/固定过程", "state": "running", "pinned": True, "active": "think"},
            {"name": "跑完/未固定", "state": "settled", "pinned": False, "active": "think"},
            {"name": "跑完/固定过程", "state": "settled", "pinned": True, "active": "think"},
            {"name": "跑完/固定结论", "state": "settled", "pinned": True, "active": "report"},
        ],
    )


def _by_name(run: dict) -> dict:
    return {item["name"]: item for item in run["cases"]}


# --------------------------------------------------------------------------
# 三条口径（用户明确要的）
# --------------------------------------------------------------------------


def test_opening_a_settled_target_lands_on_the_report(run):
    """没在跑（打开一个早就跑完的目标）→ 完整结论那一页。"""
    last = _by_name(run)["初始"]["snaps"][-1]

    assert last["current"] == "report"
    assert last["reportSelected"] == "true" and last["thinkSelected"] == "false"
    assert last["reportHidden"] is False and last["thinkHidden"] is True


def test_a_running_analysis_lands_on_the_think_tab(run):
    """正在跑 → 思考过程（那时候唯一有内容的就是它）。"""
    last = _by_name(run)["开跑"]["snaps"][-1]

    assert last["current"] == "think"
    assert last["thinkHidden"] is False and last["reportHidden"] is True


def test_it_switches_back_to_the_report_when_the_run_ends(run):
    """跑完 → 自动切回完整结论。"""
    snaps = _by_name(run)["跑完自动切回"]["snaps"]

    assert snaps[-2]["current"] == "think", "开跑那一刻应当在思考过程上"
    assert snaps[-1]["current"] == "report"
    assert snaps[-1]["reportHidden"] is False


# --------------------------------------------------------------------------
# 那条「别把读者拽走」的例外
# --------------------------------------------------------------------------


def test_it_does_not_yank_a_reader_who_picked_the_think_tab(run):
    """用户自己点了「思考过程」，跑完时**留在原地**，只给结论打未读点。"""
    last = _by_name(run)["用户自己选的标签不被抢走"]["snaps"][-1]

    assert last["current"] == "think", "用户自己选的标签不该被抢走"
    assert last["thinkHidden"] is False
    assert last["reportHidden"] is True, "结论面板还是藏着的（他没切过去）"
    # 但新结论要能announce：未读点 + 可访问的名字。
    assert last["dotHidden"] is False
    assert last["reportUnread"] is True
    assert last["reportLabel"] == "完整结论（有新结论）"


def test_the_unread_dot_clears_when_the_reader_goes_back(run):
    last = _by_name(run)["回到结论时未读点消失"]["snaps"][-1]

    assert last["current"] == "report"
    assert last["dotHidden"] is True
    assert last["reportUnread"] is False
    assert last["reportLabel"] is None, "未读的说法要跟着点一起摘掉"


def test_a_new_run_clears_a_stale_dot(run):
    """又跑起来了：上一次留下的未读点指的是**上一份**结论，留着是假信息。"""
    last = _by_name(run)["用户自己选的标签不被抢走"]["snaps"][2]

    assert last["dotHidden"] is True, "开跑那一刻不该有上一轮的未读点"


def test_closing_the_drawer_forgets_the_choice(run):
    """关抽屉 = 这次阅读结束：下次打开按默认走（这里模拟「跑完」→ 应当切回结论）。"""
    snaps = _by_name(run)["关抽屉后不再记住"]["snaps"]

    assert snaps[2]["pinned"] is True, "用户点过标签"
    assert snaps[3]["pinned"] is False, "reset 之后忘掉"
    assert snaps[-1]["current"] == "report", "不再被上一次的选择固定住"


# --------------------------------------------------------------------------
# 键盘与 ARIA
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case,expected",
    [
        ("方向键从结论到过程", "think"),
        ("方向键从过程回结论", "report"),
        ("左方向键同样换标签", "report"),
        ("Home 回过程", "think"),
        ("End 到结论", "report"),
    ],
)
def test_the_keyboard_moves_between_the_two_tabs(run, case, expected):
    last = _by_name(run)[case]["snaps"][-1]

    assert last["current"] == expected
    assert last["pinned"] is True, "键盘换标签也算「用户自己选的」"


def test_the_keyboard_lands_on_the_tab_it_selected(run):
    """换标签之后焦点要跟过去，否则方向键按了等于没按（焦点还在原地）。"""
    result = _by_name(run)["方向键从过程回结论"]

    assert result["focusLog"], "换标签要把焦点带过去"
    assert result["focusLog"][-1] == "aiDrawerTabReport"


def test_roving_tabindex_follows_the_selection(run):
    """组内只有当前那个是可 Tab 到的，否则按 Tab 要按两次才越过标签条。"""
    for name in ("初始", "开跑", "方向键从过程回结论"):
        last = _by_name(run)[name]["snaps"][-1]
        on_think = last["current"] == "think"
        assert last["thinkTabindex"] == ("0" if on_think else "-1")
        assert last["reportTabindex"] == ("-1" if on_think else "0")


def test_the_panels_are_mutually_exclusive(run):
    """**核心不变量**：任何一步之后，两个面板都不可能同时露着或同时藏着。"""
    for case in run["cases"]:
        for index, snap in enumerate(case["snaps"]):
            assert snap["thinkHidden"] != snap["reportHidden"], (
                f"{case['name']} 第 {index + 1} 步：两个面板的显隐相同了"
                f"（thinkHidden={snap['thinkHidden']}）"
            )


def test_switching_to_the_think_tab_notifies_the_caller(run):
    """切过去时通知调用方 —— 抽屉靠它去**懒加载**落库的逐轮明细。"""
    assert _by_name(run)["初始"]["thinkShown"] == 0, "没切过去时不该去取"
    assert _by_name(run)["开跑"]["thinkShown"] >= 1


# --------------------------------------------------------------------------
# 纯函数：三条口径本身
# --------------------------------------------------------------------------


def test_the_default_tab_rule(run):
    by_name = {item["name"]: item for item in run["pure"]}

    assert by_name["在跑/未固定"]["defaultTab"] == "think"
    assert by_name["跑完/未固定"]["defaultTab"] == "report"
    # 用户自己选过 → 一律不动（他看哪儿就哪儿）。
    assert by_name["在跑/固定过程"]["defaultTab"] == "think"
    assert by_name["跑完/固定过程"]["defaultTab"] == "think"
    assert by_name["跑完/固定结论"]["defaultTab"] == "report"


def test_the_unread_rule_only_fires_when_the_reader_stayed_behind(run):
    by_name = {item["name"]: item for item in run["pure"]}

    assert by_name["跑完/固定过程"]["markUnread"] is True
    assert by_name["跑完/固定结论"]["markUnread"] is False, "他就在结论页上，不需要提醒"
    assert by_name["在跑/固定过程"]["markUnread"] is False, "还在跑，没有「新结论」"
    assert by_name["跑完/未固定"]["markUnread"] is False, "没固定就会自动切过去"


def test_the_shared_ids_are_the_ones_the_three_templates_render(run):
    """模块认的那组 id 必须就是三份模板里那一段 DOM 的 id（改名 = 标签整块失灵）。"""
    assert run["ids"] == {
        "thinkTab": "aiDrawerTabThink",
        "reportTab": "aiDrawerTabReport",
        "thinkPanel": "aiDrawerPanelThink",
        "reportPanel": "aiDrawerPanelReport",
        "dot": "aiDrawerTabReportDot",
    }
