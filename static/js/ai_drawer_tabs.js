/* AI 抽屉里的两个标签：思考过程 / 完整结论。
 *
 * ---------------------------------------------------------------------------
 * 为什么是共享文件（与 ai_usage_line.js / ai_stream_status.js 同一打法）
 * ---------------------------------------------------------------------------
 * 三份模板的抽屉是逐字复制的（commit_diff_new / weekly_version_diff /
 * merged_project_view）。标签的**状态机**抄三遍必然分叉，而分叉的症状是「某一页跑完
 * 之后不会自动切到结论」——不报错，只是那一页怪怪的。
 *
 * 所以：DOM 三份逐字相同（新标签的 id 在三份里**同名**，这是共享 id 的既有约定，
 * 见 `aiUsageModal`），逻辑全部在本文件。
 *
 * ---------------------------------------------------------------------------
 * 三条口径（用户明确要的）
 * ---------------------------------------------------------------------------
 * 1. **正在跑** → 默认停在「思考过程」（那时候唯一有内容的就是它）；
 * 2. **跑完了** → 自动切回「完整结论」——但**只在用户自己没选过标签时**；
 * 3. **没在跑**（打开一个早就跑完的目标）→ 默认「完整结论」。
 *
 * 第 2 条里那个例外是刻意的：用户主动点了「思考过程」去看过程，这时候把他拽走是最
 * 招人烦的一种「贴心」。改成的做法是给「完整结论」打一个未读点，新结论依然会announce，
 * 但阅读位置不动。
 *
 * **`pinned` 的含义**：用户在**这一次运行里**自己选过标签。关抽屉时清掉（`reset()`），
 * 所以下次打开还是按第 1/3 条走。
 */
(function (global) {
    'use strict';

    var THINK = 'think';
    var REPORT = 'report';

    // 三份模板里同名的那一组 id。**多数的 id 三份同名**（这是共享 DOM 的既有约定，
    // 见 `aiUsageModal`）；只有「完整结论」那个面板不是 —— 它就是各页原有的正文框
    // （commit 页 `aiAnalysisOutput`，周版本/合并页 `weeklyAiOutput`），所以由
    // `init({ reportPanel })` 传进来，默认值只是一个兜底。
    var IDS = {
        thinkTab: 'aiDrawerTabThink',
        reportTab: 'aiDrawerTabReport',
        thinkPanel: 'aiDrawerPanelThink',
        reportPanel: 'aiDrawerPanelReport',
        dot: 'aiDrawerTabReportDot'
    };

    var state = 'idle';
    var pinned = false;
    var current = REPORT;
    var hooks = {};
    // 「完整结论」面板的元素 id。模板用 `init({reportPanel})` 交进来。
    var reportPanelId = IDS.reportPanel;

    function el(id) {
        return global.document ? global.document.getElementById(id) : null;
    }

    /**
     * 打开抽屉（或运行状态变化）时该显示哪个标签。**纯函数**，node 下直接测。
     *
     * `pinned` 为真 = 用户自己选过 → 一律不动（他看哪儿就哪儿）。
     */
    function defaultTab(runState, isPinned, active) {
        if (isPinned) return active === THINK ? THINK : REPORT;
        return runState === 'running' ? THINK : REPORT;
    }

    /** 跑完了但用户正停在「思考过程」上：该给「完整结论」打未读点，而不是切过去。 */
    function shouldMarkUnread(runState, isPinned, active) {
        return runState !== 'running' && isPinned && active === THINK;
    }

    function paint() {
        var thinkTab = el(IDS.thinkTab);
        var reportTab = el(IDS.reportTab);
        var thinkPanel = el(IDS.thinkPanel);
        var reportPanel = el(reportPanelId);
        if (thinkTab) {
            thinkTab.setAttribute('aria-selected', current === THINK ? 'true' : 'false');
            // 漫游 tabindex：Tab 键进得来、出去得走，组内用方向键 —— 与 ARIA tabs 的
            // 标准做法一致（否则按 Tab 要按两次才越过标签条）。
            thinkTab.setAttribute('tabindex', current === THINK ? '0' : '-1');
        }
        if (reportTab) {
            reportTab.setAttribute('aria-selected', current === REPORT ? 'true' : 'false');
            reportTab.setAttribute('tabindex', current === REPORT ? '0' : '-1');
        }
        if (thinkPanel) thinkPanel.hidden = current !== THINK;
        if (reportPanel) reportPanel.hidden = current !== REPORT;
    }

    function clearUnread() {
        var dot = el(IDS.dot);
        var reportTab = el(IDS.reportTab);
        if (dot) dot.hidden = true;
        if (reportTab) {
            reportTab.classList.remove('has-unread');
            reportTab.removeAttribute('aria-label');
        }
    }

    function markUnread() {
        var dot = el(IDS.dot);
        var reportTab = el(IDS.reportTab);
        if (dot) dot.hidden = false;
        if (reportTab) {
            reportTab.classList.add('has-unread');
            reportTab.setAttribute('aria-label', '完整结论（有新结论）');
        }
    }

    /** 切到某个标签。`user` 为真 = 用户自己点的（记进 `pinned`）。 */
    function select(name, options) {
        var opts = options || {};
        var next = name === THINK ? THINK : REPORT;
        current = next;
        if (opts.user) pinned = true;
        if (next === REPORT) clearUnread();
        paint();
        if (next === THINK && hooks.onShowThink) hooks.onShowThink();
    }

    function markRunning() {
        state = 'running';
        // 又跑起来了：上一次留下的未读点是**过期**的（它指的是上一份结论）。
        clearUnread();
        if (!pinned) select(THINK);
    }

    function markSettled() {
        state = 'settled';
        if (shouldMarkUnread(state, pinned, current)) {
            markUnread();
            return;
        }
        if (!pinned) select(REPORT);
    }

    /** 关抽屉：清掉「用户选过」这件事，下次打开按默认走。 */
    function reset() {
        state = 'idle';
        pinned = false;
        clearUnread();
        select(REPORT);
    }

    function focusTab(name) {
        var node = el(name === THINK ? IDS.thinkTab : IDS.reportTab);
        if (node && node.focus) node.focus();
    }

    /* ----------------------------------------------------------------------
     * 抽屉的开合：Esc 关闭 + 焦点保存/恢复
     * ----------------------------------------------------------------------
     * 这段本来只有周版本页有（模板里手写的一段 `document.keydown`），另外两页的抽屉
     * **没有 Esc**：抽屉占掉半屏、背后还压着一层 overlay，键盘用户按遍所有键也出不去
     * （WCAG 2.1.2 无键盘陷阱）。三份模板各写一遍必然分叉 —— 已经分叉过了，所以放进
     * 这里，由 `bindDrawer()` 一处实现。
     *
     * 焦点同样：抽屉打开时焦点该进去、关掉时该回到**打开它的那个元素**上，否则关完
     * 抽屉焦点掉回 `<body>`，键盘用户要从头 Tab 一遍。模块拿不到各页 open 函数的时点
     * （它们在模板里），所以两件事都不靠调用方配合：
     *
     *   * 记住触发元素靠 `focusin` —— 抽屉关着的时候焦点落在谁身上就一直记着；
     *   * 进出抽屉靠 `MutationObserver` 盯 `class` 上的 `open`。
     *
     * **不做焦点陷阱（Tab 环绕）**：那需要额外一套「抽屉内可聚焦元素」的判定，而这
     * 三页的抽屉里面板是活的（结论正文里有链接、按钮、`details`）。已声明
     * `aria-modal="true"` 却没有陷阱，严格说仍不完整 —— 记在这里，别当成已经做完了。
     */
    var boundDrawers = {};

    function bindDrawer(options) {
        var opts = options || {};
        var drawerId = opts.drawer;
        if (!drawerId || typeof opts.onClose !== 'function') return api;
        if (boundDrawers[drawerId]) return api;
        if (!global.document) return api;
        // 裸沙箱（`tests/` 里几个把模板脚本抠进假 DOM 的用例）没有 `addEventListener`。
        // 真浏览器一定有 —— 这里只是让「架子不全」的沙箱安静地退化成不绑定，而不是
        // 在加载模板脚本时抛一个与它无关的 TypeError。
        if (typeof global.document.addEventListener !== 'function') return api;
        boundDrawers[drawerId] = true;

        var lastTrigger = null;
        var wasOpen = false;

        function drawerNode() {
            return el(drawerId);
        }

        function isOpen() {
            var node = drawerNode();
            return !!(node && node.classList && node.classList.contains('open'));
        }

        function focusIntoDrawer() {
            var node = drawerNode();
            if (node && node.focus) node.focus();
        }

        function restoreFocus() {
            var target = lastTrigger;
            lastTrigger = null;
            if (!target || typeof target.focus !== 'function') return;
            var body = global.document.body;
            // 触发它的那个元素可能已经不在了（列表重画 / 行被换掉）—— 别硬 focus 一个
            // 已经脱离文档的节点，那会让焦点落在 `<body>` 上，等于没恢复。
            if (body && typeof body.contains === 'function' && !body.contains(target)) return;
            target.focus();
        }

        /** 比对「现在开着没」，在**变化的那一刻**做进出焦点的动作。幂等。 */
        function sync() {
            var nowOpen = isOpen();
            if (nowOpen === wasOpen) return;
            wasOpen = nowOpen;
            if (nowOpen) {
                focusIntoDrawer();
            } else {
                restoreFocus();
            }
        }

        // 关着的时候，焦点落在谁身上就一直记着 —— 那就是「打开它的那个元素」。
        // 抽屉**里面**的焦点不算（否则会把抽屉内的元素记成触发点，关掉后焦点又跳回去）。
        global.document.addEventListener('focusin', function (event) {
            if (isOpen()) return;
            if (event && event.target) lastTrigger = event.target;
        });

        global.document.addEventListener('keydown', function (event) {
            var key = event.key;
            if (key !== 'Escape' && key !== 'Esc') return;
            if (!isOpen()) return;
            if (event.preventDefault) event.preventDefault();
            opts.onClose();
            // 各页的 close 函数会摘掉 `open`；但 MutationObserver 不可用时没有人回调，
            // 所以这里再显式比一次（`sync()` 幂等，重复调用不会重复恢复焦点）。
            sync();
        });

        function startWatching() {
            var node = drawerNode();
            if (!node) return false;
            if (typeof global.MutationObserver === 'function') {
                new global.MutationObserver(sync).observe(node, {
                    attributes: true,
                    attributeFilter: ['class']
                });
            }
            wasOpen = isOpen();
            return true;
        }

        // 抽屉的 DOM 在页面里，但脚本不保证已经跑到它后面 —— 拿不到就等 DOM 就绪。
        if (!startWatching() && global.document.addEventListener) {
            global.document.addEventListener('DOMContentLoaded', startWatching);
        }
        return api;
    }

    function onKeydown(event) {
        var key = event.key;
        if (key === 'ArrowRight' || key === 'ArrowLeft') {
            // 只有两个标签：左右都是「换到另一个」，不用算方向。
            event.preventDefault();
            select(current === THINK ? REPORT : THINK, { user: true });
            focusTab(current);
            return;
        }
        if (key === 'Home') {
            event.preventDefault();
            select(THINK, { user: true });
            focusTab(THINK);
        } else if (key === 'End') {
            event.preventDefault();
            select(REPORT, { user: true });
            focusTab(REPORT);
        }
    }

    /**
     * 挂上三份模板里那一段 DOM。
     *
     * `options`：
     *   * `reportPanel`：**「完整结论」那个面板的元素 id** —— 它就是各页原有的正文框，
     *     三份模板里 id 不同（`aiAnalysisOutput` / `weeklyAiOutput`），必须由调用方给；
     *   * `onShowThink`：切到「思考过程」时通知调用方（抽屉用它去**懒加载**落库的逐轮
     *     记录 —— 没打开过这个标签的人不该为它付一次请求）。
     */
    function init(options) {
        hooks = options || {};
        if (hooks.reportPanel) reportPanelId = hooks.reportPanel;
        var thinkTab = el(IDS.thinkTab);
        var reportTab = el(IDS.reportTab);
        if (thinkTab) {
            thinkTab.addEventListener('click', function () { select(THINK, { user: true }); });
            thinkTab.addEventListener('keydown', onKeydown);
        }
        if (reportTab) {
            reportTab.addEventListener('click', function () { select(REPORT, { user: true }); });
            reportTab.addEventListener('keydown', onKeydown);
        }
        paint();
        return api;
    }

    var api = {
        THINK: THINK,
        REPORT: REPORT,
        IDS: IDS,
        defaultTab: defaultTab,
        shouldMarkUnread: shouldMarkUnread,
        init: init,
        bindDrawer: bindDrawer,
        select: select,
        markRunning: markRunning,
        markSettled: markSettled,
        reset: reset,
        current: function () { return current; },
        state: function () { return state; },
        isPinned: function () { return pinned; }
    };

    global.AiDrawerTabs = api;
})(typeof window !== 'undefined' ? window : this);
