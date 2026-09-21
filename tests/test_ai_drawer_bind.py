# -*- coding: utf-8 -*-
"""抽屉的开合：**Esc 关闭 + 焦点进出**，用 node 真跑 `static/js/ai_drawer_tabs.js`。

## 这一组盯的是什么

这段能力原先只有周版本页有 —— 是模板里手写的一段 `document.keydown`。另外两页
（提交 diff 页 `#aiDrawer`、项目总览页 `#weeklyAiDrawer`）的抽屉**没有 Esc**：抽屉
占掉半屏、背后还压着一层 overlay，键盘用户按遍所有键也出不去（WCAG 2.1.2 无键盘陷阱）。
焦点同样三页都没有处理：关掉抽屉后焦点掉回 `<body>`，要从头 Tab 一遍。

所以实现搬进了共享模块，三份模板各留**一次** `bindDrawer({...})` 调用。这一组守两件事：

1. **行为**：Esc 真的调到各页自己的 `onClose`；焦点真的进出抽屉（这几条判定写反了
   也是合法代码，静态断言看不出来，所以按仓库既有做法放进带假 DOM 的 node 沙箱跑）；
2. **接线**：三份模板都调了 `bindDrawer`，且各自传**自己**那个抽屉 id ——
   接线漏一份的症状是「这一页按 Esc 没反应」，不报错。

## 假 DOM 说明

比 `test_ai_drawer_tabs_frontend.py` 那个多三样：`document.addEventListener` /
`MutationObserver` / `document.body`（各页打开抽屉是把 `open` 类加上去，模块靠盯
class 变化知道进出）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_drawer_tabs.js"
TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)
# 每份模板各自的抽屉 id（与模板里的 `<aside class="ai-drawer" id="...">` 同名）。
DRAWER_IDS = {
    "templates/commit_diff_new.html": "aiDrawer",
    "templates/weekly_version_diff.html": "weeklyAiDrawer",
    "templates/merged_project_view.html": "weeklyAiDrawer",
}

# 整行都是注释的行：静态断言前要剥掉。注释里会**原样引用**要禁掉的写法
# （本仓库的习惯就是把符号写进解释性注释），不剥就会「注释一写、测试就红」。
_COMMENT_LINE_PREFIXES = ("//", "*", "/*")


def _code_lines(source: str) -> list:
    """只留代码行（整行注释丢掉）。行内注释保留 —— 粗剥比不剥安全。"""
    return [
        line for line in source.splitlines()
        if not line.lstrip().startswith(_COMMENT_LINE_PREFIXES)
    ]

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

var els = {};
var focusLog = [];
var docHandlers = {};
var observers = [];
var body = null;

var src = fs.readFileSync(__SCRIPT__, 'utf8');

function makeEl(id) {
    var node = { id: id, hidden: false, _attrs: {}, _classes: [] };
    node.setAttribute = function (k, v) { node._attrs[k] = String(v); };
    node.getAttribute = function (k) {
        return node._attrs[k] === undefined ? null : node._attrs[k];
    };
    node.removeAttribute = function (k) { delete node._attrs[k]; };
    node.addEventListener = function () {};
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

// **每个场景重新加载一遍脚本**：模块内部有状态（`boundDrawers` 记住已经绑过的抽屉），
// 复用同一个实例的话，第二个场景的 `bindDrawer` 会被那个幂等守卫挡掉 —— 于是观察者与
// 键盘回调还留在上一个场景的闭包里，用例会测到一个「上一个场景的模块」。
function freshApi() {
    body = {
        contains: function (node) { return !node._detached; },
        classList: { add: function () {}, remove: function () {} }
    };
    var sandbox = { window: {}, console: console };
    sandbox.window = sandbox;
    sandbox.MutationObserver = function (cb) {
        this.observe = function (target) { observers.push({ target: target, cb: cb }); };
    };
    sandbox.document = {
        body: body,
        getElementById: function (id) { return els[id] || null; },
        addEventListener: function (type, fn) { docHandlers[type] = fn; }
    };
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return sandbox.AiDrawerTabs;
}

var closes = 0;
var CURRENT = null;

function el(id) { return els[id] || null; }

function boot(drawerId) {
    els = {};
    focusLog = [];
    closes = 0;
    observers = [];
    docHandlers = {};
    els[drawerId] = makeEl(drawerId);
    // 触发抽屉的那个元素（三页分别是按钮或风险标签）。
    els['trigger'] = makeEl('trigger');
    var api = freshApi();
    api.init({ reportPanel: 'aiAnalysisOutput' });
    api.bindDrawer({ drawer: drawerId, onClose: function () { closes += 1; closeIt(); } });
    return api;
}

function openIt() { el(CURRENT).classList.add('open'); tick(); }
function closeIt() { el(CURRENT).classList.remove('open'); tick(); }
// 模拟浏览器：class 变了就通知所有观察者（模块靠这个知道进出）。
function tick() { observers.forEach(function (o) { o.cb(); }); }

function scenario(drawerId, steps) {
    CURRENT = drawerId;
    boot(drawerId);
    var out = { focusLog: [], closes: 0 };
    steps.forEach(function (step) {
        if (step === 'open') openIt();
        else if (step === 'escape') docHandlers.keydown({ key: 'Escape', preventDefault: function () {} });
        else if (step === 'escapeLower') docHandlers.keydown({ key: 'Esc', preventDefault: function () {} });
        else if (step === 'focusTrigger') { docHandlers.focusin({ target: el('trigger') }); }
        else if (step === 'focusInside') { docHandlers.focusin({ target: el(drawerId) }); }
        else if (step === 'detachTrigger') { el('trigger')._detached = true; }
    });
    out.focusLog = focusLog.slice();
    out.closes = closes;
    out.open = el(drawerId).classList.contains('open');
    return out;
}

var results = {};
results.escapeCloses = scenario('widget', ['focusTrigger', 'open', 'escape']);
results.escapeLower = scenario('widget', ['focusTrigger', 'open', 'escapeLower']);
results.escapeWhenClosed = scenario('widget', ['escape']);
results.focusRestoredToTrigger = scenario('widget', ['focusTrigger', 'open', 'escape']);
results.insideFocusIsNotATrigger = scenario('widget', ['focusTrigger', 'open', 'focusInside', 'escape']);
results.detachedTriggerIsNotFocused = scenario('widget', ['focusTrigger', 'open', 'detachTrigger', 'escape']);
results.bindsOnce = (function () {
    var api = boot('widget');
    var before = observers.length;
    api.bindDrawer({ drawer: 'widget', onClose: function () {} });
    return { before: before, after: observers.length };
})();

console.log(JSON.stringify(results));
"""


def _run_node() -> dict:
    driver = DRIVER.replace("__SCRIPT__", json.dumps(str(SCRIPT).replace("\\", "/")))
    completed = subprocess.run(
        ["node", "-e", driver],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PROJECT_ROOT),
    )
    assert completed.returncode == 0, f"node 驱动失败：{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def results():
    return _run_node()


class TestEscapeClosesTheDrawer:
    def test_escape_calls_the_pages_own_close(self, results):
        """Esc 必须调到各页自己的 `onClose` —— 它还要停流、清掉当前目标。"""
        assert results["escapeCloses"]["closes"] == 1
        assert results["escapeCloses"]["open"] is False

    def test_the_legacy_esc_key_name_works_too(self, results):
        """老浏览器 / 部分输入法报的是 `'Esc'` 而不是 `'Escape'`。"""
        assert results["escapeLower"]["closes"] == 1
        assert results["escapeLower"]["open"] is False

    def test_escape_on_a_closed_drawer_does_nothing(self, results):
        """抽屉没开时按 Esc 不能有任何副作用 —— 页面上还有别的弹窗与输入框。"""
        assert results["escapeWhenClosed"]["closes"] == 0


class TestFocusMovesInAndOut:
    def test_focus_lands_on_the_drawer_when_it_opens(self, results):
        assert "widget" in results["escapeCloses"]["focusLog"], (
            "抽屉打开时焦点没进去 —— 键盘用户的下一次 Tab 还在页面上乱走"
        )

    def test_focus_returns_to_the_element_that_opened_it(self, results):
        """关掉抽屉后焦点回到触发元素，而不是掉回 `<body>`。"""
        log = results["focusRestoredToTrigger"]["focusLog"]
        assert log[-1] == "trigger", f"焦点没有回到触发元素：{log}"

    def test_focus_inside_the_drawer_is_not_remembered_as_the_trigger(self, results):
        """抽屉**里面**的焦点不算触发元素。

        否则用户在抽屉里点了两下再按 Esc，焦点会跳回抽屉内部的某个元素 ——
        而那个元素此刻正跟着抽屉一起藏起来。
        """
        log = results["insideFocusIsNotATrigger"]["focusLog"]
        assert log[-1] == "trigger", f"焦点回到了抽屉内部而不是触发元素：{log}"

    def test_a_detached_trigger_is_not_focused(self, results):
        """触发元素可能已经不在了（列表重画 / 版本行被换掉）。

        硬 focus 一个脱离文档的节点等于没恢复（焦点会落回 `<body>`），
        所以宁可不动。
        """
        log = results["detachedTriggerIsNotFocused"]["focusLog"]
        assert log[-1] != "trigger", f"focus 了一个已经脱离文档的元素：{log}"


class TestBindDrawer:
    def test_binding_twice_does_not_double_bind(self, results):
        """同一页可能因为脚本重复加载而绑两次 —— 不能变成两个观察者。"""
        assert results["bindsOnce"]["after"] == results["bindsOnce"]["before"]


# ==========================================================================
# 三份模板的接线：静态断言
# ==========================================================================


class TestEveryTemplateWiresItUp:
    """三份模板都必须调 `bindDrawer`，且传**自己**那个抽屉 id。

    漏一份的症状是「这一页按 Esc 没反应」—— 不报错、不影响别的页，只能靠断言拦。
    """

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_the_template_calls_bind_drawer(self, name):
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "AiDrawerTabs.bindDrawer(" in source, (
            f"{name} 没有接抽屉的 Esc / 焦点 —— 这一页的抽屉键盘关不掉"
        )
        assert f"drawer: '{DRAWER_IDS[name]}'" in source, (
            f"{name} 的 bindDrawer 没传自己的抽屉 id"
        )

    @pytest.mark.parametrize("name", TEMPLATES)
    def test_the_drawer_container_can_hold_focus(self, name):
        """`<aside>` 要能接住焦点（`tabindex="-1"`）。

        没有它，模块调 `.focus()` 是个静默的空操作 —— 焦点没进去，而不报错。
        """
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        drawer_id = DRAWER_IDS[name]
        assert f'class="ai-drawer" id="{drawer_id}"' in source
        line = next(
            line for line in source.splitlines()
            if f'class="ai-drawer" id="{drawer_id}"' in line
        )
        assert 'tabindex="-1"' in line, f"{name} 的抽屉容器没有 tabindex=-1，焦点进不去"

    def test_no_template_hand_rolls_its_own_escape_handler(self):
        """抽屉的 Esc 只许有一份（在共享模块里）；模板自己的**模态框**可以有自己的。

        ## 这条为什么从「一律禁止」改成「必须拦住冒泡」

        原先这条是 `assert "event.key !== 'Escape'" not in source` —— 一律禁止。
        本轮 `weekly_version_diff.html` 加了一个**自定义确认框**（`weekly-ai-dialog`，
        「取消这次全量」用它），它自己 `addEventListener('keydown', onKey, true)`
        捕获阶段处理 Esc，并且**显式 `stopPropagation()`**：

            // Esc 只关这个框：抽屉自己也认 Esc（`AiDrawerTabs.bindDrawer`），
            // 不拦住的话「取消这次全量」会顺手把抽屉也关掉。

        那是一个**与抽屉无关**的 Esc 处理器，一律禁止就是**假失败**，会逼着后来者
        要么把这条测试删掉、要么把模态框写坏。所以判据收窄到它真正保护的不变量：

        * 模板里若出现手写的 Esc 判定，它**必须**拦冒泡 —— 因为一个不拦冒泡的模板级
          Esc 处理器会与抽屉那一份互相干扰（这正是当初那个 bug 的形态：周版本页手写了
          一段，两处判定各自演化）；
        * 抽屉那一份仍然由 `test_the_template_calls_bind_drawer` 钉住（每个模板都必须
          调 `AiDrawerTabs.bindDrawer` 并传自己的抽屉 id）。

        **这条仍然拦不住的**：一个既拦冒泡、又去关抽屉的手写处理器。那种写法现在没有，
        而它比原来的 bug 更刻意 —— 真出现时靠 code review，不靠这条。
        """
        for name in TEMPLATES:
            lines = _code_lines((PROJECT_ROOT / name).read_text(encoding="utf-8"))
            for index, line in enumerate(lines):
                if "event.key !== 'Escape'" not in line:
                    continue
                # 手写的 Esc 处理器很短（判定 + 拦冒泡 + 关闭），看后面几行够了。
                window = "\n".join(lines[index:index + 8])
                assert "stopPropagation" in window, (
                    f"{name}:{index + 1} 有一段手写的 Esc 判定却**没有拦住冒泡** —— "
                    "它会和抽屉那一份互相干扰。抽屉的 Esc 请用 AiDrawerTabs.bindDrawer；"
                    "模板自己的模态框要在处理器里 event.stopPropagation()。"
                )
