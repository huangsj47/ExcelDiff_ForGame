/* 「历次结论」：这个目标跑过的每一次，翻看其中任意一次。

 * ---------------------------------------------------------------------------
 * 它解决的是哪个问题
 * ---------------------------------------------------------------------------
 * 用户报的原话是「AI 重新分析时无法预览旧的结论」—— 点「重新分析」之后，屏幕上那句
 * 「AI 分析进行中...」就把旧报告换掉了。而**旧结论一条都没丢**：每一次运行都落在库里
 * （`ai_analysis_run.response_text` 就是那份报告）。这里补的是读的入口。
 *
 * 所以这个面板**在跑动中也照样能打开** —— 那正是它存在的理由。
 *
 * ---------------------------------------------------------------------------
 * 三件事只在服务端做
 * ---------------------------------------------------------------------------
 * 「列哪几次」（窗口与 `/latest` 同一把尺子）、「每行那些字」（状态/风险/范围/摘要）、
 * 「这一次能不能导出」全都由 `services/ai_report_history_service.py` 与
 * `services/ai/report_document.py` 决定 —— 界面只负责摆。三份模板各拼一遍这些词，
 * 迟早会有一份说的是「已有结论」另一份说「成功」。
 *
 * ---------------------------------------------------------------------------
 * 几条界面口径
 * ---------------------------------------------------------------------------
 * 1. **当前显示的那一次要标出来** —— 否则用户分不清「我正在看的是哪一份」。
 *    当前运行号从 `AiThinkLog.currentRunId()` 现取（那就是「屏幕上这一次」的定义），
 *    点开弹层的**那一刻**读一次 —— 比在每个分支里再 push 一份状态可靠（也不会过期）。
 * 2. **选中历史条目之后，报告区上方要写明「这是 <时间> 的结论（历史）」**：一份看起来
 *    完全正常的报告，会被当成当前结论用。
 * 3. **导出的是选中的那一份**（弹层里自己那个链接），不是抽屉 footer 上那个。
 * 4. **列表只有一次结论时说实话**（「还没有可对比的历史」），不摆一张只有一行的表；
 *    一条都没有时说「还没跑过分析」。
 * 5. 报告正文走 `AiReportMarkdown.render`（先整体转义再套白名单），**不在这里拼 HTML**。
 */
(function (global) {
    'use strict';

    var NOTE = {
        loading: '正在读取历次结论…',
        empty: '这个目标还没有跑过分析 —— 没有可以翻看的历次结论。',
        // 「一次都没跑过」与「正在跑、还没跑完第一次」是两件事：后者这么说，是因为
        // 这个弹层最常被打开的时刻就是「正在跑、想看看上一次」。
        empty_running: '这个目标正在分析，还没有结论可翻。跑完之后这里会列出每一次。',
        single: '这个目标目前只有一次结论，还没有可对比的历史。',
        // 列表被截断时如实说：让人知道「更早的没列出来」是保留策略，不是平台没跑过。
        truncated: '共 %TOTAL% 次，这里只列出最近 %SHOWN% 次；更早的超出保留窗口（%DAYS% 天）。',
        all: '这个目标跑过 %TOTAL% 次，新的在最上面。',
        no_body: '这一次没有报告正文。',
        failed_mark: '这一次是失败的，没有结论。',
        no_target: '这个页面没有告诉它看哪个目标（入口少了一次 track）。',
        error: '读不到历次结论：'
    };

    var IDS = {
        modal: 'aiReportHistoryModal',
        body: 'aiReportHistoryBody'
    };

    var state = {
        historyUrl: null,
        loading: false,
        payload: null,
        // 屏幕上正显示的那一次（点开弹层那一刻从 `AiThinkLog` 现取）。
        currentRunId: null,
        selectedRunId: null,
        selectedReport: null,
        // Bootstrap 的 Modal 实例（懒建；模板里没有这个元素时保持 null）。
        modal: null
    };

    function el(id) {
        return global.document ? global.document.getElementById(id) : null;
    }

    // -----------------------------------------------------------------------
    //  纯函数（node 下真跑）
    // -----------------------------------------------------------------------
    function historyUrlFor(kind, id) {
        if (kind === 'weekly') return '/ai-analysis/weekly/' + id + '/history';
        if (kind === 'commit') return '/ai-analysis/commit/' + id + '/history';
        return null;
    }

    function exportHref(runId) {
        return '/ai-analysis/runs/' + runId + '/report.md';
    }

    function reportUrl(runId) {
        return '/ai-analysis/runs/' + runId + '/report';
    }

    /** 列表顶上那句说明。`payload` 就是 `/history` 的响应。 */
    function listNote(payload) {
        var data = payload || {};
        var runs = data.runs || [];
        var total = Number(data.total || 0);
        if (!runs.length) return data.in_progress ? NOTE.empty_running : NOTE.empty;
        if (total <= 1 && runs.length <= 1) return NOTE.single;
        var shown = runs.length;
        if (data.truncated || total > shown) {
            return NOTE.truncated
                .replace('%TOTAL%', String(total))
                .replace('%SHOWN%', String(shown))
                .replace('%DAYS%', String(data.window_days || ''));
        }
        return NOTE.all.replace('%TOTAL%', String(total));
    }

    /** 报告区上方那行标记：**这一份是不是屏幕上正在显示的那一份**。 */
    function markText(row, currentRunId) {
        var when = (row && row.created_at_display) || '未知时间';
        var isCurrent = row && currentRunId !== null && currentRunId !== undefined
            && Number(row.run_id) === Number(currentRunId);
        return isCurrent ? ('这是当前显示的结论（' + when + '）')
                         : ('这是 ' + when + ' 的结论（历史）');
    }

    /** 状态徽章的颜色档：成功绿、失败红、其余中性。 */
    function statusTone(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'failed') return 'danger';
        return 'secondary';
    }

    /** 一行运行的「次要信息」：范围 / 触发 / 焦点 / 异常条数 —— 空的不摆。 */
    function rowMeta(row) {
        var parts = [];
        if (row.scope_label) parts.push(row.scope_label);
        if (row.trigger_label) parts.push(row.trigger_label);
        if (row.focus_label) parts.push(row.focus_label);
        if (row.anomaly_count) parts.push('异常 ' + row.anomaly_count + ' 条');
        return parts.join(' · ');
    }

    // -----------------------------------------------------------------------
    //  渲染
    // -----------------------------------------------------------------------
    function addTag(parent, className, text) {
        var node = global.document.createElement('span');
        node.className = className;
        node.textContent = text;
        parent.appendChild(node);
        return node;
    }

    function renderNote(text) {
        var body = el(IDS.body);
        if (!body) return;
        body.textContent = '';
        var note = global.document.createElement('p');
        note.className = 'ai-history-meta';
        note.textContent = text;
        body.appendChild(note);
    }

    function renderRow(tbody, row, currentRunId) {
        var doc = global.document;
        var tr = doc.createElement('tr');
        tr.className = 'ai-history-row';
        if (Number(row.run_id) === Number(state.selectedRunId)) {
            tr.className += ' is-selected';
        }
        if (currentRunId !== null && currentRunId !== undefined
            && Number(row.run_id) === Number(currentRunId)) {
            tr.className += ' is-current';
        }

        var when = doc.createElement('td');
        when.className = 'ai-history-when';
        when.textContent = row.created_at_display || '-';
        tr.appendChild(when);

        var status = doc.createElement('td');
        addTag(status, 'badge bg-' + statusTone(row.status), row.status_label || row.status || '-');
        tr.appendChild(status);

        var risk = doc.createElement('td');
        risk.textContent = row.risk_label || '-';
        tr.appendChild(risk);

        var detail = doc.createElement('td');
        detail.className = 'ai-history-summary';
        detail.textContent = row.summary || '-';
        var meta = rowMeta(row);
        if (meta) {
            var metaNode = doc.createElement('span');
            metaNode.className = 'ai-history-row-meta';
            metaNode.textContent = meta;
            detail.appendChild(metaNode);
        }
        tr.appendChild(detail);

        var actions = doc.createElement('td');
        actions.className = 'ai-history-actions';
        var button = doc.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-sm btn-outline-secondary';
        button.textContent = '看这一份';
        button.addEventListener('click', function () { select(row.run_id); });
        actions.appendChild(button);
        tr.appendChild(actions);

        tbody.appendChild(tr);
    }

    function renderReport() {
        var doc = global.document;
        var body = el(IDS.body);
        var row = currentRow();
        if (!body || !row) return;
        var wrap = doc.createElement('div');
        wrap.className = 'ai-history-report';

        var head = doc.createElement('div');
        head.className = 'ai-history-report-head';
        var mark = doc.createElement('span');
        mark.className = 'ai-history-mark';
        mark.textContent = markText(row, state.currentRunId);
        head.appendChild(mark);

        // 导出**这一份**（不是抽屉 footer 上那个 —— 那个导的是当前显示的那次）。
        if (row.exportable) {
            var link = doc.createElement('a');
            link.className = 'btn btn-sm btn-outline-secondary';
            link.id = 'aiHistoryExportMdLink';
            // 不给 `download`：文件名归服务端的 `Content-Disposition`（中文名）。
            link.setAttribute('href', exportHref(row.run_id));
            var icon = doc.createElement('i');
            icon.className = 'fas fa-file-arrow-down';
            icon.setAttribute('aria-hidden', 'true');
            link.appendChild(icon);
            link.appendChild(doc.createTextNode(' 导出这一份 md'));
            head.appendChild(link);
        }
        wrap.appendChild(head);

        var report = doc.createElement('div');
        report.className = 'ai-analysis-output';
        report.id = 'aiHistoryReportBody';
        var payload = state.selectedReport;
        if (payload && payload.result === null && payload.status !== 'succeeded') {
            // 失败 / 进行中：如实说，不留一片空白（也不假装它是一份报告）。
            report.textContent = payload.status === 'failed'
                ? (NOTE.failed_mark + (payload.error_message ? '原因：' + payload.error_message : ''))
                : '这一次还没有结论。';
        } else if (payload && payload.response_text) {
            if (global.AiReportMarkdown && global.AiReportMarkdown.render) {
                report.innerHTML = global.AiReportMarkdown.render(payload.response_text);
            } else {
                report.textContent = payload.response_text;
            }
        } else {
            report.textContent = NOTE.no_body;
        }
        wrap.appendChild(report);
        body.appendChild(wrap);
    }

    function currentRow() {
        var runs = (state.payload && state.payload.runs) || [];
        for (var i = 0; i < runs.length; i += 1) {
            if (Number(runs[i].run_id) === Number(state.selectedRunId)) return runs[i];
        }
        return null;
    }

    function render() {
        var doc = global.document;
        var body = el(IDS.body);
        if (!body) return;
        body.textContent = '';
        var payload = state.payload;
        if (!payload || !(payload.runs || []).length) {
            // **走 `listNote` 而不是写死 `NOTE.empty`**：空列表有**两种**处境
            // （一次都没跑过 / 正在跑但还没跑完第一次），说哪一句由 `listNote` 决定。
            // 这里写死的话，`in_progress` 那句话就只活在本文件的常量表里 —— 一条
            // 看着在、实际永远不会出现在屏幕上的文案（单测还会因为它绿）。
            renderNote(listNote(payload));
            return;
        }
        renderNote(listNote(payload));

        var table = doc.createElement('table');
        table.className = 'ai-history-list';
        var thead = doc.createElement('thead');
        var headRow = doc.createElement('tr');
        ['时间', '状态', '风险', '这一次说了什么', ''].forEach(function (text) {
            var th = doc.createElement('th');
            th.textContent = text;
            headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);

        var tbody = doc.createElement('tbody');
        var runs = payload.runs;
        for (var i = 0; i < runs.length; i += 1) {
            renderRow(tbody, runs[i], state.currentRunId);
        }
        table.appendChild(tbody);
        body.appendChild(table);

        if (state.selectedRunId !== null) renderReport();
    }

    // -----------------------------------------------------------------------
    //  取数
    // -----------------------------------------------------------------------
    function fetchJson(fetchImpl, url) {
        var doFetch = fetchImpl || global.fetch;
        return doFetch(url, {cache: 'no-store'}).then(function (response) {
            return response.json().then(function (payload) {
                if (!response.ok || payload.success === false) {
                    throw new Error(payload.message || ('HTTP ' + response.status));
                }
                return payload;
            });
        });
    }

    /** 选中某一次：去取它的报告（与 `/latest` 同一个形状）。 */
    function select(runId, fetchImpl) {
        state.selectedRunId = runId;
        state.selectedReport = null;
        render();
        return fetchJson(fetchImpl, reportUrl(runId)).then(function (payload) {
            // 取回来的时候用户可能已经点了别的：那就丢掉这一份，别把旧结果盖上去。
            if (Number(state.selectedRunId) !== Number(runId)) return null;
            state.selectedReport = payload.result || payload;
            render();
            return state.selectedReport;
        }).catch(function (error) {
            if (Number(state.selectedRunId) !== Number(runId)) return null;
            renderNote(NOTE.error + ((error && error.message) || '未知错误'));
            return null;
        });
    }

    /**
     * 打开弹层：拉列表，并默认选中**屏幕上正显示的那一次**（选不中就是最新那条）。
     */
    function open(fetchImpl) {
        var body = el(IDS.body);
        var modalEl = el(IDS.modal);
        if (!body || !modalEl) return null;
        if (!state.historyUrl) {
            // 页面没告诉我们看哪个目标（例如新加的入口忘了调 `track`）。**不发请求**：
            // `fetch(null)` 会去请求一个叫 "null" 的地址，那是一条谁也解释不了的日志。
            renderNote(NOTE.error + NOTE.no_target);
            return null;
        }
        if (state.modal === null && global.bootstrap && global.bootstrap.Modal) {
            state.modal = new global.bootstrap.Modal(modalEl);
        }
        if (state.modal) state.modal.show();
        // **点开的这一刻**现取「屏幕上这一次是谁」：在跑动中它刚被换成新的运行号，
        // 而弹层要能说清「你看的是哪一份」。
        state.currentRunId = (global.AiThinkLog && global.AiThinkLog.currentRunId)
            ? global.AiThinkLog.currentRunId() : null;
        renderNote(NOTE.loading);
        state.loading = true;
        return fetchJson(fetchImpl, state.historyUrl).then(function (payload) {
            state.loading = false;
            state.payload = payload;
            var runs = payload.runs || [];
            state.selectedRunId = null;
            for (var i = 0; i < runs.length; i += 1) {
                if (Number(runs[i].run_id) === Number(state.currentRunId)) {
                    state.selectedRunId = runs[i].run_id;
                    break;
                }
            }
            if (state.selectedRunId === null && runs.length) {
                state.selectedRunId = runs[0].run_id;
            }
            render();
            if (state.selectedRunId !== null) return select(state.selectedRunId, fetchImpl);
            return null;
        }).catch(function (error) {
            state.loading = false;
            renderNote(NOTE.error + ((error && error.message) || '未知错误'));
            return null;
        });
    }

    /** 记住这个目标的历史地址（换目标时要重设 —— 合并视图是同一个抽屉换目标）。 */
    function track(options) {
        var next = (options && options.historyUrl) || null;
        if (next === state.historyUrl) return;
        state.historyUrl = next;
        // 换了目标就把上一个目标的列表丢掉：留着会被当成这个目标的历次结论。
        state.payload = null;
        state.selectedRunId = null;
        state.selectedReport = null;
    }

    function state_() {
        return {
            historyUrl: state.historyUrl,
            currentRunId: state.currentRunId,
            selectedRunId: state.selectedRunId,
            loading: state.loading,
            rows: (state.payload && state.payload.runs) || []
        };
    }

    global.AiReportHistory = {
        NOTE: NOTE,
        IDS: IDS,
        historyUrlFor: historyUrlFor,
        exportHref: exportHref,
        reportUrl: reportUrl,
        listNote: listNote,
        markText: markText,
        statusTone: statusTone,
        rowMeta: rowMeta,
        track: track,
        open: open,
        select: select,
        state: state_
    };
})(typeof window !== 'undefined' ? window : this);
