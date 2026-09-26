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
 * 2026-09-25：从「一张宽表 + 底下接报告」改成「左列 + 右详情」
 * ---------------------------------------------------------------------------
 * 用户实测提了三件事，根子是**同一个**：列表与报告挤在同一个滚动条里。
 *
 * 1. **点了看不到正文**。列表 20 行有一千多像素高，报告接在**整张表下面** —— 点
 *    「看这一份」，屏幕上纹丝不动（变化发生在折叠线以下），用户以为没反应，要自己
 *    往下滚。现在两栏各自滚动，报告**永远在视野里**。
 * 2. **行里那句话读不了**。摘要被 `max-width: 520px` 挤成一个窄条（服务端已按 80 字
 *    截断，再挤一次就成了三行碎句）。现在整条按卡片摆：时间/状态/风险一行，摘要整句
 *    换行（**不裁剪** —— 裁了就得靠 tooltip 补，而这份摘要本来就只有 80 字）。
 * 3. **选中态分不清**。「当前显示的那一份」只靠一条 3px 的色条（**颜色单独承载语义**），
 *    「正在看的那一份」只靠底色。现在前者是一个写着「当前显示」的标签，后者是底色 +
 *    左侧色条 + `aria-selected`，两个都在文字里说得出来。
 *
 * 顺带修掉两个真缺陷（都不是排版问题）：
 *
 * * **每次选中都闪一句「这一次没有报告正文。」** —— `select()` 先把
 *   `selectedReport` 清成 null 再重画，于是「还没取回来」被画成了「取回来了但里面
 *   没有正文」。现在详情区有**自己的加载态**（`aria-busy`），那句文案只在一份真的
 *   取回来、真的没有正文时出现。
 * * **报告取不到时整张列表被抹掉** —— 原来 `catch` 走的是 `renderNote()`，它写的是
 *   `body.textContent`，也就是**整个弹层**。一次网络抖动会把用户正在翻的历史清空。
 *   现在错误只落在详情区，列表原地不动。
 *
 * ---------------------------------------------------------------------------
 * 几条界面口径
 * ---------------------------------------------------------------------------
 * 1. **当前显示的那一次要标出来** —— 否则用户分不清「我正在看的是哪一份」。
 *    当前运行号从 `AiThinkLog.currentRunId()` 现取（那就是「屏幕上这一次」的定义），
 *    点开弹层的**那一刻**读一次 —— 比在每个分支里再 push 一份状态可靠（也不会过期）。
 * 2. **选中历史条目之后，报告区上方要写明「这是 <时间> 的结论（历史）」**：一份看起来
 *    完全正常的报告，会被当成当前结论用。
 * 3. **导出的是选中的那一份**（弹层里自己那个链接），不是抽屉 footer 上那个。它的
 *    有无**只看列表行上的 `exportable`**，不等报告取回来 —— 否则每换一条都会先少一个
 *    按钮再长出来（实测里那一下闪动很容易被当成「这条不能导出」）。
 * 4. **列表只有一次结论时说实话**（「还没有可对比的历史」），不摆一张只有一行的表；
 *    一条都没有时说「还没跑过分析」。
 * 5. 报告正文走 `AiReportMarkdown.render`（先整体转义再套白名单），**不在这里拼 HTML**。
 * 6. 报告下面还挂着**结构化结论 + 处置**（`static/js/ai_anomaly_disposition.js`）：
 *    处置是「读某一次的报告」时做的事，而这个面板已经知道看的是哪一次。这里只负责
 *    建容器 `#aiAnomalyPanel` 并把运行号交给它 —— 清单怎么画、状态名怎么取，全在那个
 *    模块里（只此一份）。
 * 7. **换一次选中只重画详情区**，不重画列表：整块重画会把列表的滚动位置打回顶部，
 *    翻到第 15 条按一下方向键就跳回第 1 条。列表只在「打开 / 换目标」时重建一次。
 * 8. **方向键即选中**（`↑↓ Home End`，`role="listbox"` + roving tabindex）。这个面板
 *    的右栏是**预览**，所以「焦点走到哪一条，看的就是哪一条」比「先移动再回车」少一半
 *    动作。取数走 `state.reports` 那一层缓存，来回翻不重复请求。
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
        // 详情区自己的加载态。**与 `no_body` 分开**：一个是「还没取回来」，一个是
        // 「取回来了、里面真的没有正文」，把前者画成后者是这一批里最误导的一种假话。
        detail_loading: '正在读取这一份的报告…',
        detail_error: '读不到这一份的报告：',
        failed_mark: '这一次是失败的，没有结论。',
        no_target: '这个页面没有告诉它看哪个目标（入口少了一次 track）。',
        error: '读不到历次结论：',
        // 删除。三句话各有各的场合，**不能合成一句**：
        // * `delete_ask` 是**动手之前**问的（含「删了基线会退到哪」—— 那是这个功能的
        //   全部风险所在，用户不被告知就点下去，等于平台替他改了下一次分析的基线）；
        // * `delete_failed` 是失败后留在详情区的（列表不动）；
        // * `delete_done` 是成功后的回执。
        delete_ask: '删除这一份结论？',
        delete_ask_baseline: '它同时是**下一轮增量分析的基线**，删掉之后平台会改用更早那一条'
            + '（一条都不剩时，下一次增量分析会按全量跑）。',
        delete_ask_tail: '这一份的报告正文、逐轮轨迹与结论清单会一起删掉，删了不能恢复。',
        delete_failed: '删除失败：',
        delete_label: '删除这一份',
        deleting_label: '正在删除…'
    };

    var IDS = {
        modal: 'aiReportHistoryModal',
        body: 'aiReportHistoryBody'
    };

    // 键盘上「换一条」的那几个键。**只认这四个**：`Enter` / `Space` 由浏览器的 click
    // 兜住（每一项都是可点的），其余键一律不拦（用户还要用得着 Tab）。
    // 写成数组而不是 `'ArrowDown ArrowUp Home End'.indexOf(key)`：后者是**子串**匹配，
    // 将来加一个 `'End'`/`'Home'` 的亲戚（比如 `'PageEnd'`）就会静默多认一个键。
    var NAV_KEYS = ['ArrowDown', 'ArrowUp', 'Home', 'End'];

    var state = {
        historyUrl: null,
        loading: false,
        payload: null,
        // 屏幕上正显示的那一次（点开弹层那一刻从 `AiThinkLog` 现取）。
        currentRunId: null,
        selectedRunId: null,
        // 这个分组当前的分组键。删除请求要把它带回去（服务端拿它比对「你看到的是不是
        // 这个版本的历史」）—— 弹层是**同一个抽屉换目标**，列表没刷回来时点删除，
        // 删掉的会是上一个版本的那一条。
        targetKey: null,
        // 正在删的那一条（null = 没有在删）。详情区的删除态只认它。
        deleting: null,
        // 删除失败 / 成功的那句话。**只影响详情区**：一次网络抖动不该把用户正在翻的
        // 历史清空（同 `reports[].error` 那条既有口径）。
        deleteError: '',
        deleteNotice: '',
        // 已经取回来的那几份报告：runId → {payload} 或 {error}。
        // **一次运行的报告不会变**（它是存档），所以这个缓存只按目标清、不按时间清。
        reports: {},
        // 正在取的那一条（null = 没有在取的）。详情区的加载态只认它。
        loadingRunId: null,
        // Bootstrap 的 Modal 实例（懒建；模板里没有这个元素时保持 null）。
        modal: null,
        // 这一次渲染出来的节点（列表项、详情区那几块）。列表要能单独重画而不动别的。
        dom: null
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

    function deleteUrl(runId) {
        return '/ai-analysis/runs/' + runId + '/delete';
    }

    /** 动手之前问的那句话。**把「基线会退到哪」写在里面** —— 那是这个功能的全部风险：
     *  删掉的不只是一份存档，还是「下一轮增量拿谁做基线」那个指针的目标。
     *
     *  是**纯函数**（`row` 进、字符串出），所以两档措辞能直接断言，不必去跑一遍确认框。
     */
    function deleteConfirmText(row) {
        var when = (row && row.created_at_display) || '这一次';
        var parts = [NOTE.delete_ask + '（' + when + '）'];
        if (row && row.is_baseline) parts.push(NOTE.delete_ask_baseline);
        parts.push(NOTE.delete_ask_tail);
        return parts.join('\n');
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

    /** 状态徽章的颜色档：成功绿、失败红、**降级黄**、其余中性。
     *
     * `degraded` 要单独一档：它不是成功（流程没走完），也不是失败（报告是真的）。
     * 与成功同色会让「这一份是降级出来的」在列表里看不出来 —— 而用户重看历史时最需要
     * 知道的就是这件事。
     */
    function statusTone(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'degraded') return 'warning';
        if (status === 'failed') return 'danger';
        return 'secondary';
    }

    /**
     * 这一次运行**有没有结论**。与 `services/ai/run_cache_source.py::CONCLUDED_STATUSES`
     * 同一份口径：`degraded` 是「有结论但浅」（那份报告正文是真的），与 `succeeded`
     * 一样要渲染出来；`failed` / `running` / `pending` 才是没有结论。
     *
     * **不能写成「不是 succeeded 就算没有结论」**：`DEGRADE_MARKDOWN` 那种降级
     * （模型没按协议给 JSON）的 `result` 就是 `null`，而它的 `response_text` 是一份
     * 完整的 markdown 报告 —— 按旧判据会被显示成「这一次还没有结论。」，与列表里
     * 那一行摘要（正是从 `response_text` 里取的）自相矛盾。
     */
    function hasConclusion(status) {
        return status === 'succeeded' || status === 'degraded';
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

    /** 键盘按一下之后该落到第几个（`-1` = 这个键不管）。
     *
     * `count` = 0 时恒返回 `-1`（没有条目可落）。**首尾不环绕**：`↓` 到底就停在最后
     * 一条 —— 翻历史是「找某一次」，绕回开头会让人以为自己翻过头了。
     */
    function nextIndex(key, current, count) {
        if (count <= 0) return -1;
        var at = (current === null || current === undefined) ? -1 : Number(current);
        if (isNaN(at)) at = -1;
        if (key === 'ArrowDown') return at < 0 ? 0 : Math.min(at + 1, count - 1);
        if (key === 'ArrowUp') return at < 0 ? count - 1 : Math.max(at - 1, 0);
        if (key === 'Home') return 0;
        if (key === 'End') return count - 1;
        return -1;
    }

    /** 这一行是不是**抽屉里正显示的那一次**。`currentRunId` 由调用方给 —— 渲染那条路
     *  传 `state.currentRunId`，测试能拿它直接判，不必先走一趟 `open()`。 */
    function isCurrentAt(row, currentRunId) {
        return !!(row && currentRunId !== null && currentRunId !== undefined
            && Number(row.run_id) === Number(currentRunId));
    }

    function isCurrent(row) {
        return isCurrentAt(row, state.currentRunId);
    }

    /** 一行的 class。选中与「当前显示」是**两件事**（可能落在同一行上）：
     *  「选中」= 我正在看它的报告，「当前显示」= 抽屉里那份结论就是它。 */
    function itemClass(row, selected) {
        return 'ai-history-item'
            + (isCurrent(row) ? ' is-current' : '')
            + (selected ? ' is-selected' : '');
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

    /** 整个弹层只摆一句说明（列表为空 / 读不到 / 没告诉它看哪个目标）。 */
    function renderNote(text) {
        var body = el(IDS.body);
        if (!body) return;
        state.dom = null;
        body.textContent = '';
        var note = global.document.createElement('p');
        note.className = 'ai-history-meta';
        note.textContent = text;
        body.appendChild(note);
    }

    /** 一行结论。**整行可点**，不是只有右边那个按钮可点（那个按钮已经去掉：整行
     *  都是目标，再摆一个按钮等于把「只有这里能点」写在了脸上）。 */
    function buildItem(row) {
        var doc = global.document;
        var item = doc.createElement('div');
        item.className = itemClass(row, false);
        // `role="option"` + roving tabindex：列表是**单选**的，而右栏是它的预览。
        // 用 `<div>` 而不是 `<button>`：button 自带 button 角色，套在 listbox 里
        // 会被读成「一个按钮」，而不是「清单里的一项」。
        item.setAttribute('role', 'option');
        item.setAttribute('tabindex', '-1');
        item.setAttribute('aria-selected', 'false');

        var top = doc.createElement('div');
        top.className = 'ai-history-item-top';
        var when = doc.createElement('span');
        when.className = 'ai-history-when';
        when.textContent = row.created_at_display || '-';
        top.appendChild(when);
        addTag(top, 'badge bg-' + statusTone(row.status), row.status_label || row.status || '-');
        if (row.risk_label) addTag(top, 'ai-history-risk', '风险 ' + row.risk_label);
        // 「当前显示的那一份」原来只有一条 3px 色条（**颜色单独承载语义**）。带文字的
        // 标签与色条同时在场：色条管扫视，标签管「说不说得出来」。
        if (isCurrent(row)) addTag(top, 'ai-history-current-tag', '当前显示');
        // 「这一条是下一轮增量分析的基线」——**删掉它，基线就换人**。这件事在删除之前
        // 就得看得见：确认框里那句「删了会改用上一条」只有在用户已经知道「它本来就是
        // 基线」时才读得懂。同样是一个词，不是一条颜色（与「当前显示」同一条口径）。
        if (row.is_baseline) {
            var tag = addTag(top, 'ai-history-baseline-tag', '当前基线');
            tag.title = '下一轮增量分析会继承这一条的结论';
        }
        item.appendChild(top);

        // **摘要不裁剪**：服务端已经按 80 字截过一道，这里再挤成定高就等于把一句话
        // 砍成碎句，而它正是「一眼认出是哪一次」的唯一线索。
        var summary = doc.createElement('p');
        summary.className = 'ai-history-summary';
        summary.textContent = row.summary || '-';
        item.appendChild(summary);

        var meta = rowMeta(row);
        if (meta) {
            var metaNode = doc.createElement('p');
            metaNode.className = 'ai-history-row-meta';
            metaNode.textContent = meta;
            item.appendChild(metaNode);
        }

        item.addEventListener('click', function () { select(row.run_id); });
        return item;
    }

    /** 建左列 + 右详情两栏，并把键盘挂上。**只在打开 / 换目标时调一次**。 */
    function buildPanes(body) {
        var doc = global.document;
        var panes = doc.createElement('div');
        panes.className = 'ai-history-panes';

        var listPane = doc.createElement('div');
        listPane.className = 'ai-history-list-pane';
        var list = doc.createElement('div');
        list.className = 'ai-history-list';
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-label', '历次结论');
        list.addEventListener('keydown', onKeyDown);
        listPane.appendChild(list);
        panes.appendChild(listPane);

        var detail = doc.createElement('div');
        detail.className = 'ai-history-detail-pane';
        panes.appendChild(detail);
        body.appendChild(panes);

        state.dom = {list: list, detail: detail, items: []};
        return state.dom;
    }

    function renderItems() {
        var dom = state.dom;
        if (!dom) return;
        var runs = (state.payload && state.payload.runs) || [];
        dom.list.textContent = '';
        dom.items = [];
        runs.forEach(function (row) {
            var node = buildItem(row);
            dom.list.appendChild(node);
            dom.items.push({row: row, node: node});
        });
    }

    /** 只改选中态。**不重建列表** —— 重建会把列表的滚动位置打回顶部。 */
    function updateSelection(focus) {
        var dom = state.dom;
        if (!dom) return;
        for (var i = 0; i < dom.items.length; i += 1) {
            var entry = dom.items[i];
            var on = Number(entry.row.run_id) === Number(state.selectedRunId);
            entry.node.className = itemClass(entry.row, on);
            entry.node.setAttribute('aria-selected', on ? 'true' : 'false');
            // roving tabindex：Tab 进得来一次，进来之后用方向键走。
            entry.node.setAttribute('tabindex', on ? '0' : '-1');
            // **键盘走过来时焦点要真的跟过去**（鼠标点的不抢焦点：那会把用户从
            //  他正在滚的列表上拽走）。`focus` 不存在时（测试用的假 DOM）不报错，
            //  但**必须有真实调用点**：这个方法在真浏览器里被 `onKeyDown` 调着。
            if (on && focus && typeof entry.node.focus === 'function') entry.node.focus();
        }
    }

    function currentRow() {
        var runs = (state.payload && state.payload.runs) || [];
        for (var i = 0; i < runs.length; i += 1) {
            if (Number(runs[i].run_id) === Number(state.selectedRunId)) return runs[i];
        }
        return null;
    }

    function renderDetail() {
        var doc = global.document;
        var dom = state.dom;
        var row = currentRow();
        if (!dom || !row) return;
        var detail = dom.detail;
        detail.textContent = '';
        detail.setAttribute('aria-busy', 'false');

        var head = doc.createElement('div');
        head.className = 'ai-history-report-head';
        var mark = doc.createElement('span');
        mark.className = 'ai-history-mark';
        mark.textContent = markText(row, state.currentRunId);
        head.appendChild(mark);

        // 导出**这一份**（不是抽屉 footer 上那个 —— 那个导的是当前显示的那次）。
        // 有无只认列表行上的 `exportable`：它是服务端按同一把尺子算出来的，现在就
        // 知道，不必等报告取回来（等的话每换一条都会闪一下「没有导出按钮」）。
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

        // **删除按钮落在详情栏头部，不是列表行里**。行是 `role="option"` + 整行可点 +
        // 方向键即选中：在小格里塞一个按钮会同时打坏 ARIA（option 里不该有可交互子元素）
        // 与键盘（方向键走到哪就选中哪，按钮的 Tab 焦点与它在两条线上争）。
        // 头部本来就写着「你看的是哪一份」，一个作用于**这一份**的动作放在这里语义最正。
        if (row.deletable) {
            var busy = state.deleting !== null
                && Number(state.deleting) === Number(row.run_id);
            var remove = doc.createElement('button');
            remove.type = 'button';
            remove.className = 'btn btn-sm btn-outline-danger';
            // 固定 id（与导出那个链接一样）：node 下的假 DOM 按 id 找它，不必靠顺序猜。
            remove.id = 'aiHistoryDeleteButton';
            // **在飞的时候要禁用**（连点两次 = 第二次请求删一条已经被删掉的行，
            // 用户看到的是「删除失败」而后台其实成功了）。
            remove.disabled = state.deleting !== null;
            remove.setAttribute('aria-busy', busy ? 'true' : 'false');
            remove.appendChild(doc.createTextNode(busy ? NOTE.deleting_label : NOTE.delete_label));
            remove.addEventListener('click', function () { confirmDelete(row.run_id); });
            head.appendChild(remove);
        }
        detail.appendChild(head);

        // 上一次删除的结果（失败或成功）留在详情区顶端。**不落在列表上**：删除失败时
        // 列表必须原地不动（同「报告取不到时整张列表被抹掉」那个老缺陷）。
        if (state.deleteError || state.deleteNotice) {
            var note2 = doc.createElement('p');
            note2.className = state.deleteError
                ? 'ai-history-delete-note is-error'
                : 'ai-history-delete-note';
            note2.textContent = state.deleteError
                ? (NOTE.delete_failed + state.deleteError)
                : state.deleteNotice;
            detail.appendChild(note2);
        }

        var pending = state.loadingRunId !== null
            && Number(state.loadingRunId) === Number(row.run_id);
        var entry = state.reports[row.run_id];
        var payload = entry && entry.payload;

        var report = doc.createElement('div');
        report.className = 'ai-analysis-output';
        report.id = 'aiHistoryReportBody';
        if (pending) {
            // 「还没取回来」要长得像**在等**，不像「取回来了、里面是空的」。
            // 样式只认 `aria-busy`（一个判据一处产地）：再挂一个 `is-loading` 类
            // 就是同一件事写两遍，两处迟早对不上。
            detail.setAttribute('aria-busy', 'true');
            report.textContent = NOTE.detail_loading;
        } else if (entry && entry.error) {
            report.textContent = NOTE.detail_error + entry.error;
        } else if (payload && payload.result === null && !hasConclusion(payload.status)) {
            // 失败 / 进行中：如实说，不留一片空白（也不假装它是一份报告）。
            report.textContent = payload.status === 'failed'
                ? (NOTE.failed_mark + (payload.error_message ? '原因：' + payload.error_message : ''))
                : '这一次还没有结论。';
        } else if (payload && payload.response_text) {
            // 正文 + 覆盖段（平台补充）+ 降级提示：与三份抽屉走**同一个**渲染入口，
            // 否则「刚跑完看到的」与「历次结论里点开的」会各有一套字。
            // 变量名**不能叫 body** —— `el(IDS.body)` 那个面板元素也叫 body。
            var reportBody = payload.response_text;
            if (global.AiContextNotice && global.AiContextNotice.withContextNotice) {
                reportBody = global.AiContextNotice.withContextNotice(reportBody, payload.result);
            }
            if (global.AiReportMarkdown && global.AiReportMarkdown.render) {
                report.innerHTML = global.AiReportMarkdown.render(reportBody);
            } else {
                report.textContent = reportBody;
            }
        } else {
            report.textContent = NOTE.no_body;
        }
        detail.appendChild(report);

        // 结构化结论 + 处置：这一份报告里那些结论的**可操作形态**（逐条确认 / 忽略 /
        // 撤销）。挂在报告**下面**而不是上面 —— 报告是叙事，先读后处置，也不动用户
        // 既有的阅读位置。清单本身不在这里画：容器建好之后交给
        // `static/js/ai_anomaly_disposition.js`（它按 `#aiAnomalyPanel` 认这个容器）。
        var anomalies = doc.createElement('div');
        anomalies.className = 'ai-anomaly-panel';
        anomalies.id = 'aiAnomalyPanel';
        detail.appendChild(anomalies);

        // **先 `appendChild` 再调 `load`**：那边是按 id 找容器的，而
        // `getElementById` 只认**已经在文档里**的节点 —— detail 还没挂上去时它返回
        // null，于是这一块永远空着，而且不报错（假 DOM 与真浏览器在这条上一致）。
        // 模块没加载时（这个页面没引那个脚本）什么都不做：缺的是脚本，不是数据。
        // **还在取的时候不调**：那时右栏写的是「正在读取这一份的报告…」，先把它下面
        // 那块的结论画出来，屏幕上就同时挂着「在等报告」与一份结论清单。
        if (!pending && global.AiAnomalyDisposition && global.AiAnomalyDisposition.load) {
            global.AiAnomalyDisposition.load(row.run_id);
        }
    }

    function render() {
        var body = el(IDS.body);
        if (!body) return;
        var payload = state.payload;
        if (!payload || !(payload.runs || []).length) {
            // **走 `listNote` 而不是写死 `NOTE.empty`**：空列表有**两种**处境
            // （一次都没跑过 / 正在跑但还没跑完第一次），说哪一句由 `listNote` 决定。
            // 这里写死的话，`in_progress` 那句话就只活在本文件的常量表里 —— 一条
            // 看着在、实际永远不会出现在屏幕上的文案（单测还会因为它绿）。
            renderNote(listNote(payload));
            // 删掉**最后一条**之后列表就是空的 —— 那时用户唯一能看到的反馈就是这一句。
            // 少了它，删成功的表现与「这个目标本来就没跑过」逐字相同。
            if (state.deleteNotice) {
                var done = global.document.createElement('p');
                done.className = 'ai-history-delete-note';
                done.textContent = state.deleteNotice;
                var noteBody = el(IDS.body);
                if (noteBody) noteBody.appendChild(done);
            }
            return;
        }
        var doc = global.document;
        state.dom = null;
        body.textContent = '';
        var note = doc.createElement('p');
        note.className = 'ai-history-meta';
        note.textContent = listNote(payload);
        body.appendChild(note);
        buildPanes(body);
        renderItems();
        updateSelection(false);
        if (state.selectedRunId !== null) renderDetail();
    }

    // -----------------------------------------------------------------------
    //  键盘
    // -----------------------------------------------------------------------
    function onKeyDown(event) {
        var key = event && event.key;
        if (!key || NAV_KEYS.indexOf(key) === -1) return;
        var dom = state.dom;
        if (!dom || !dom.items.length) return;
        // 拦下方向键：不拦的话焦点会在弹层的其他控件之间跳走（Tab 走的还是原生顺序）。
        if (event.preventDefault) event.preventDefault();
        var at = -1;
        for (var i = 0; i < dom.items.length; i += 1) {
            if (Number(dom.items[i].row.run_id) === Number(state.selectedRunId)) { at = i; break; }
        }
        var next = nextIndex(key, at, dom.items.length);
        if (next < 0) return;
        // **选中跟着焦点走**：右栏是这一条的预览，先移动再回车是多余的一步。
        select(dom.items[next].row.run_id, null, {focus: true});
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

    /** 写请求。与 `fetchJson` 分开：这条**把服务端那句话原样带出来**（`err.payload`），
     *  因为拒绝的每一档有自己的话（404 / 400 / 409），而界面要显示的是服务端那一句，
     *  不是前端自己按状态码再编一句。 */
    function postJson(fetchImpl, url, body) {
        var doFetch = fetchImpl || global.fetch;
        return doFetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body || {})
        }).then(function (response) {
            return response.json().then(function (payload) {
                if (!response.ok || payload.success === false) {
                    var error = new Error(payload.message || ('HTTP ' + response.status));
                    error.payload = payload;
                    throw error;
                }
                return payload;
            });
        });
    }

    function rowById(runId) {
        var runs = (state.payload && state.payload.runs) || [];
        for (var i = 0; i < runs.length; i += 1) {
            if (Number(runs[i].run_id) === Number(runId)) return runs[i];
        }
        return null;
    }

    /**
     * 删掉一条结论：先问一句，再发请求，成功之后**用服务端回来的那份列表重画**。
     *
     * ## 为什么要用服务端回来的列表，而不是自己从旧列表里摘掉一条
     *
     * 被删的那一条可能正是**基线指针**的目标（`is_baseline`）。删完之后新指针指向哪一条
     * 只有服务端知道（那要看结论是否结构化、时间序）—— 前端猜的话，那个标记会留在
     * 一行已经不再是指针的记录上，而「基线退到哪」恰恰是这个功能的全部意义。
     *
     * ## 连点
     *
     * `state.deleting` 一非空就直接返回：第二次点击不发请求。按钮同时在飞的时候是
     * 禁用的，但键盘与快速双击都可能赶在那一次重画之前进来。
     *
     * ## 失败只落在详情区
     *
     * 409（还在跑）、400（版本对不上）、网络抖动 —— 列表一律原地不动。把整张列表换成
     * 一句错误，用户正在翻的历史就没了（这个老缺陷的说明在模块抬头的 2026-09-25 那节）。
     */
    function confirmDelete(runId, fetchImpl) {
        var row = rowById(runId);
        // 服务端说不能删就不发请求（`deletable` 与服务端的拒绝分支同源）。这一句是
        // 「按钮不该在」的兜底：按钮由同一个字段决定，两边不会各说各话。
        if (!row || !row.deletable) return Promise.resolve(null);
        if (state.deleting !== null) return Promise.resolve(null);
        // 原生 `confirm`：这个动作不可逆、代价说清了，一个 OK/取消就够了 —— 不值得为它
        // 再做一个弹层（本文件已经有一层弹层，套第二层会打乱焦点与 Esc 的处理）。
        // **没有 `confirm` 时一律不发请求**（假 DOM / 极老的环境）：默认「不发」而不是
        // 「照发」，因为这是不可逆的删除。
        var ask = deleteConfirmText(row);
        if (typeof global.confirm !== 'function' || !global.confirm(ask)) {
            return Promise.resolve(null);
        }
        state.deleting = runId;
        state.deleteError = '';
        state.deleteNotice = '';
        renderDetail();
        return postJson(fetchImpl, deleteUrl(runId), {target_key: state.targetKey})
            .then(function (payload) {
                state.deleting = null;
                state.deleteNotice = payload.message || '已删除。';
                // 被删的那一份从报告缓存里去掉：留着的话，用户再点开同一行（不会发生，
                // 它已经不在列表里了）或者运行号被复用时会取到别人的报告。
                delete state.reports[runId];
                applyHistory(payload.history);
                render();
                // 被删的那条若是**正在看的那一份**，选中会退到最新那条 —— 而它的报告
                // 一次都没取过。不补这一句，右栏会写「这一次没有报告正文。」：
                // 把「还没取回来」画成了「取回来了、里面是空的」（这条老缺陷在同文件
                // 抬头的 2026-09-25 那节写过一次）。
                if (state.selectedRunId !== null) {
                    return select(state.selectedRunId, fetchImpl).then(function () {
                        return payload;
                    });
                }
                return payload;
            })
            .catch(function (error) {
                state.deleting = null;
                state.deleteError = (error && error.message) || '未知错误';
                // **只重画详情区**：列表的成员没有变化（一条都没删成）。
                renderDetail();
                return null;
            });
    }

    /** 把一份 `/history` 响应装进 state（`open()` 与「删完之后」共用）。
     *
     * 选中项：被删的那一条没了 → 退回列表第一条（最新那份），与 `open()` 的兜底一致。
     * 剩下的照旧选中，免得删完下面一条就把用户的阅读位置弹回顶部。
     */
    function applyHistory(payload) {
        state.payload = payload || null;
        state.targetKey = (payload && payload.target_key) || state.targetKey;
        var runs = (payload && payload.runs) || [];
        var stillThere = false;
        for (var i = 0; i < runs.length; i += 1) {
            if (Number(runs[i].run_id) === Number(state.selectedRunId)) stillThere = true;
        }
        if (!stillThere) {
            state.selectedRunId = runs.length ? runs[0].run_id : null;
            state.loadingRunId = null;
        }
    }

    /** 选中某一次：去取它的报告（与 `/latest` 同一个形状）。
     *
     * `options.focus` 只在键盘走过来时为真（见 `updateSelection`）。
     */
    function select(runId, fetchImpl, options) {
        state.selectedRunId = runId;
        updateSelection(!!(options && options.focus));
        var cached = state.reports[runId];
        if (cached && cached.error) {
            // **重新点一次 = 重试**：上一次失败留在这里的只是「那一下没读到」，不是
            // 「这一份永远读不到」。不丢掉它，用户再点这一条只会看到同一个错误。
            delete state.reports[runId];
            cached = null;
        }
        if (cached) {
            // 看过的不再取第二遍：方向键来回翻时，这一条省掉的是**每一次**请求。
            state.loadingRunId = null;
            renderDetail();
            return Promise.resolve(cached.payload || null);
        }
        state.loadingRunId = runId;
        renderDetail();
        return fetchJson(fetchImpl, reportUrl(runId)).then(function (payload) {
            var report = payload.result || payload;
            // **先落缓存再判「用户是不是已经点了别的」**：取回来的是**那一条**的报告，
            // 与用户此刻在看哪一条无关。丢掉它，用户再点回来就要重取一次。
            state.reports[runId] = {payload: report};
            if (Number(state.selectedRunId) !== Number(runId)) return null;
            state.loadingRunId = null;
            renderDetail();
            return report;
        }).catch(function (error) {
            // 同上：错误也先落缓存（详情区照着它写「读不到这一份的报告：…」），
            // 换目标不改变「那一次没读到」这个事实。
            state.reports[runId] = {error: (error && error.message) || '未知错误'};
            if (Number(state.selectedRunId) !== Number(runId)) return null;
            state.loadingRunId = null;
            renderDetail();
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
        // 换目标 / 重开时把上一次的删除回执清掉：那句话说的是**上一个目标**的一次操作，
        // 留在新的详情区里就是一句没头没尾的话。
        state.deleteError = '';
        state.deleteNotice = '';
        state.deleting = null;
        return fetchJson(fetchImpl, state.historyUrl).then(function (payload) {
            state.loading = false;
            state.payload = payload;
            var runs = payload.runs || [];
            state.selectedRunId = null;
            state.loadingRunId = null;
            state.targetKey = payload.target_key || state.targetKey;
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
        // **报告缓存一起丢**：那些运行号属于上一个目标，留着只会让下个目标的历史
        // 点开时拿错一份报告（运行号跨目标不会撞，但缓存本身没有意义了）。
        state.payload = null;
        state.selectedRunId = null;
        state.reports = {};
        state.loadingRunId = null;
        state.dom = null;
        // 分组键连同列表一起丢：它是**上一个目标**的分组键，留着会被当成这个目标的
        // 带回去（服务端一比就拒，用户看到的是「这条结论不属于你正在查看的版本」——
        // 一句对他来说莫名其妙的话）。
        state.targetKey = null;
        state.deleting = null;
        state.deleteError = '';
        state.deleteNotice = '';
    }

    function state_() {
        return {
            historyUrl: state.historyUrl,
            currentRunId: state.currentRunId,
            selectedRunId: state.selectedRunId,
            loading: state.loading,
            loadingRunId: state.loadingRunId,
            // 删除那三样放在**顶层**，不放进 `rows`：`rows` 的形状被
            // `tests/test_ai_report_history_frontend.py` 逐字比过
            // （`{"run_id": …, "selected": …}`），往里加键等于改一份已经钉死的契约。
            deleting: state.deleting,
            deleteError: state.deleteError,
            deleteNotice: state.deleteNotice,
            targetKey: state.targetKey,
            cachedRuns: Object.keys(state.reports).map(Number),
            rows: ((state.payload && state.payload.runs) || []).map(function (row) {
                return {
                    run_id: row.run_id,
                    selected: Number(row.run_id) === Number(state.selectedRunId)
                };
            })
        };
    }

    global.AiReportHistory = {
        NOTE: NOTE,
        IDS: IDS,
        historyUrlFor: historyUrlFor,
        exportHref: exportHref,
        reportUrl: reportUrl,
        deleteUrl: deleteUrl,
        deleteConfirmText: deleteConfirmText,
        listNote: listNote,
        markText: markText,
        statusTone: statusTone,
        hasConclusion: hasConclusion,
        rowMeta: rowMeta,
        nextIndex: nextIndex,
        isCurrentAt: isCurrentAt,
        track: track,
        open: open,
        select: select,
        confirmDelete: confirmDelete,
        state: state_
    };
})(typeof window !== 'undefined' ? window : this);
