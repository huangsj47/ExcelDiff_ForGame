/* 「导出 md」链接的状态：**这一次展示的结论能不能导出、导的是哪一次**。
 *
 * ---------------------------------------------------------------------------
 * 为什么状态要单独一个模块
 * ---------------------------------------------------------------------------
 * 「当前展示的是哪一次运行的结论」在抽屉里已经被算了三遍（meta 行、消耗那一行、
 * 「思考过程」面板），现在再挂一个导出链接 —— 如果在每个分支里各写一遍 `href`，三份模板
 * × 六七个分支 = 二十来处，改一处就会漏一处，而漏掉的表现是「下载下来的是上一次的结论」
 * 这种**看着像成功**的错。所以：状态在模块里，模板只在一处调用 `track()`。
 *
 * ---------------------------------------------------------------------------
 * 两条规矩
 * ---------------------------------------------------------------------------
 * 1. **没有可导出的结论时把链接藏起来**，不显示一个点了没反应的按钮（与「明细」按钮
 *    同一条口径：拿不到运行号时它是隐藏的，那不是「没采集」，是按钮没了）。
 *    「可导出」= 这一次跑成了**而且**有报告正文 —— 判定与
 *    `services/ai/report_document.py::is_exportable` 一致（那边是最终裁决，这边只管
 *    界面别给一个必然 409 的链接）。
 * 2. **不给 `<a>` 加 `download` 属性。** 响应带 `Content-Disposition: attachment` 与
 *    服务端拼好的中文文件名；加一个空的 `download` 会让浏览器**按 URL 末段命名**
 *    （`report.md`），把我们那个 `AI分析报告-<项目>-<目标>-20260912.md` 盖掉。
 *
 * 跑动中不导出（结论还没出来），也没有「导出上一次」这回事 —— 那是「历次结论」那个
 * 入口的事。
 */
(function (global) {
    'use strict';

    // 与 `services/ai/report_document.py::EXPORTABLE_STATUSES` 对应。
    var EXPORTABLE = {succeeded: true};

    var linkId = null;
    // 当前可导出的运行号；null = 没有可导出的结论（链接隐藏）。
    var runId = null;

    function canExport(status) {
        return EXPORTABLE[String(status === undefined || status === null ? '' : status)] === true;
    }

    function hrefFor(id) {
        return '/ai-analysis/runs/' + id + '/report.md';
    }

    function link() {
        return global.document && linkId ? global.document.getElementById(linkId) : null;
    }

    function paint() {
        var node = link();
        if (!node) return;
        if (runId === null) {
            // 连 `href` 一起撤掉：留着一个上一次的地址，右键「复制链接」拿到的是别人的结论。
            node.removeAttribute('href');
            node.hidden = true;
            return;
        }
        node.setAttribute('href', hrefFor(runId));
        node.hidden = false;
    }

    function init(options) {
        linkId = (options && options.linkId) || null;
        paint();
    }

    /**
     * 这一次展示的结论是谁。**每个「结论变了」的分支都要调它**，包括
     * 「没有结论」「正在跑」「失败了」这三种（那时传 status 或 runId 为空的形态）。
     */
    function track(state) {
        var next = null;
        if (state && state.runId !== undefined && state.runId !== null && canExport(state.status)) {
            next = state.runId;
        }
        if (next === runId) return;
        runId = next;
        paint();
    }

    function currentRunId() {
        return runId;
    }

    function state() {
        return {linkId: linkId, runId: runId};
    }

    global.AiReportExport = {
        canExport: canExport,
        hrefFor: hrefFor,
        init: init,
        track: track,
        currentRunId: currentRunId,
        state: state
    };
})(typeof window !== 'undefined' ? window : this);
