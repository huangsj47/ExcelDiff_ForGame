/* 「结构化结论 + 人工处置」：把一次运行落库的结论逐条列出来，确认 / 忽略 / 撤销。
 *
 * ---------------------------------------------------------------------------
 * 它解决的是哪个问题
 * ---------------------------------------------------------------------------
 * `AiAnalysisAnomaly.disposition` 此前**整棵树没有写入口**，于是那一列永远是建表时
 * 的 `pending`：用户找不到地方说「这条我确认过 / 这条不用管」，模型下一轮把同一条
 * 原样再报一遍。后端（`services/ai/anomaly_disposition.py` + 三个接口）补了读与写，
 * 这里补的是**界面** —— 没有界面，这套能力对用户等于不存在。
 *
 * 挂在「历次结论」弹层里（`static/js/ai_report_history.js` 建好容器 `#aiAnomalyPanel`
 * 之后调 `load`）：人正是在「读某一次的报告」时做处置的，而那个面板已经知道
 * 「现在看的是哪一次」。位置在报告正文**下面** —— 报告是叙事，这份清单是同一批结论
 * 的可操作形态：先读后处置，也不动用户既有的阅读位置。
 *
 * ---------------------------------------------------------------------------
 * 四条口径
 * ---------------------------------------------------------------------------
 * 1. **中文名只认服务端。** 结论的状态名（`dispositions`）、严重度（`severity_label`）、
 *    置信度（`confidence_label`）全都取服务端算好的那一份，界面**一个字都不译**。
 *    界面里再抄一份 `pending → 待确认` / `critical → 严重` 的映射表，就是同一个事实的
 *    第二份抄本：服务端加一个状态、改一个中文名时，界面显示的是一个**错的**名字 ——
 *    而用户正是按这些名字理解「我那条结论算什么、有多严重」。
 *    **服务端没给的空值不回落到码值**（见 `shownText`）：码值只留在悬停提示里。
 *    （「撤销」「批量」是动作词，不在服务端那张表里，所以只有这两个词写在界面里。）
 * 2. **次序照服务端给的走**，不再排一次。`anomalies_of_run` 已按严重度、置信度排好；
 *    界面再排一次会让上下两处对同一次运行给出两种次序，读的人会以为看的是两份清单。
 *    只有**按钮的排布**是界面定的（撤销排最后，见 `orderedChoices`）。
 * 3. **一次取全量，不按 `?disposition=` 过滤。** 单条接口只回那一行、**不回 counts**，
 *    所以界面上那几个数字要本地重算（`recount`）；一旦按处置状态过滤，手里就只剩
 *    子集，重算出来的「待确认 8」会随筛选跳动 —— 那几个数字说的是**这一次运行**。
 * 4. **时间也只认服务端。** 处置时间显示 `disposition_at_display`（服务端按北京时间
 *    算好的字符串），**不显示**库里那个 naive-UTC 的 `disposition_at`，界面也**不做**
 *    `T→空格`、`new Date()`、`+8` 这些「顺手美化」：`new Date()` 会把不带偏移的串按
 *    **浏览器本地时区**解析，在 UTC+8 的机器上再多错 8 小时（`tests/
 *    test_ai_analysis_time_display.py` 记的就是这个缺陷）；自己 +8 则是把「北京时间」
 *    这条口径抄成第二处。空串（没有处置）时那一行整个不显示。
 */
(function (global) {
    'use strict';

    // 容器 id：由 `ai_report_history.js` 在报告正文下面建出来（那是模板与模块的共同契约）。
    var PANEL_ID = 'aiAnomalyPanel';
    // 「撤销」提交的是这个状态码（服务端 `DEFAULT_DISPOSITION`）。
    var UNDO = 'pending';

    var NOTE = {
        loading: '正在读取这一次的结构化结论…',
        empty: '这一次没有落库的结构化结论条目。',
        load_error: '读不到结构化结论：',
        save_error: '处置没有成功：',
        // 截断的上限是服务端的常量（`DISPOSITION_NOTE_MAX_CHARS`），响应里只回一个
        // `note_truncated`。**不把那个数字写进这句话**：服务端一调上限，这里就成了
        // 一句假话，而它是唯一一处告诉用户「你写的东西没全存下」的地方。
        truncated: '备注太长，服务端只保存了前一段（已截断）。',
        unknown: '未知错误'
    };

    // 只映射**颜色**档，不映射状态名/严重度名（见文件头口径 1）。
    // `medium` 是平台赋值的等级（口径①「证据不足降一档」：`high` → `medium`），模型写不出
    // 它，但落库的值就是它 —— 缺这一档时 `severityTone` 回落到 `secondary`（灰），
    // 一条被降过档的结论看起来与「平台不认识这个值」一模一样。
    var SEVERITY_TONE = {critical: 'danger', high: 'warning', medium: 'info'};
    var ACTION_TONE = {
        pending: 'btn-outline-secondary',
        confirmed: 'btn-outline-success',
        ignored: 'btn-outline-warning'
    };

    var state = {
        runId: null,
        // 读接口的响应原样留着：`anomalies` / `counts` / `total` / `dispositions`。
        payload: null,
        loading: false,
        // 勾选与备注都留在 state 里，**不靠 DOM 记**：每次重画都会新建节点，
        // 靠 DOM 记的话，用户在别的条目上点一下，这边刚敲进去的备注就没了。
        selected: {},
        notes: {},
        batchNote: '',
        busy: false,
        error: null,
        notice: null
    };

    // 重画时要单独更新的那几个节点（整表重画会把用户正在输入的框换掉，所以工具条只刷新
    // 那两个数字与按钮的可用状态）。
    var refs = {picked: null, batchButtons: []};

    function doc() {
        return global.document;
    }

    function panel() {
        return global.document ? global.document.getElementById(PANEL_ID) : null;
    }

    // -----------------------------------------------------------------------
    //  纯函数（node 下真跑）
    // -----------------------------------------------------------------------
    function anomaliesUrl(runId) {
        return '/ai-analysis/runs/' + runId + '/anomalies';
    }

    function dispositionUrl(anomalyId) {
        return '/ai-analysis/anomalies/' + anomalyId + '/disposition';
    }

    function batchUrl(runId) {
        return anomaliesUrl(runId) + '/disposition';
    }

    /**
     * 状态码 → 中文名，**只从服务端给的 `dispositions` 里取**。
     * 取不到就回落成码值（`pending`）：难看，但不会说错 —— 编一个「待处理」出来，
     * 用户会拿它去对照报告里的措辞。
     */
    function labelOf(dispositions, value) {
        var list = dispositions || [];
        for (var i = 0; i < list.length; i += 1) {
            if (list[i] && list[i].value === value) {
                return list[i].label === undefined || list[i].label === null
                    ? String(value) : String(list[i].label);
            }
        }
        return value === null || value === undefined ? '' : String(value);
    }

    /** 顶上那句「共 10 条 · 待确认 8 · 已确认 1 · 已忽略 1」。次序也取服务端的。 */
    function countsText(payload) {
        var data = payload || {};
        var counts = data.counts || {};
        var parts = [];
        (data.dispositions || []).forEach(function (item) {
            if (!item) return;
            parts.push(labelOf(data.dispositions, item.value) + ' ' + (counts[item.value] || 0));
        });
        var total = data.total === undefined || data.total === null
            ? ((data.anomalies || []).length) : data.total;
        return '共 ' + total + ' 条' + (parts.length ? ' · ' + parts.join(' · ') : '');
    }

    /**
     * 本地重算计数：单条接口只回那一行，**不回 counts**，而面板上那几个数字必须跟着动。
     *
     * 能本地重算的前提是「手里是这一次运行的全部行」—— 读接口不带 `?disposition=`
     * 过滤。一旦加了筛选，口径就从「全部」变成「筛选出来的子集」，见文件头口径 3。
     */
    function recount(anomalies, dispositions) {
        var counts = {};
        (dispositions || []).forEach(function (item) {
            if (item) counts[item.value] = 0;
        });
        (anomalies || []).forEach(function (row) {
            var key = (row && row.disposition) || '';
            counts[key] = (counts[key] || 0) + 1;
        });
        return counts;
    }

    /** 按钮上的字：状态名取服务端 label；「撤销」是动作名（那个状态本身叫「待确认」）。 */
    function actionText(value, label) {
        return value === UNDO ? '撤销' : label;
    }

    /**
     * 三个按钮的排列：**「撤销」排在最后**，其余照服务端那份 `dispositions` 的次序。
     *
     * 服务端那份清单里 `pending` 是第一个（它是默认值），照抄过来就是
     * 「撤销 / 已确认 / 已忽略」—— 读起来像「这一条现在要做的事是撤销」。
     * 顶部那行统计仍然照服务端的次序（那里比的是「各有多少条」，`pending` 打头最顺）。
     * 这里换的只是**动作的排布**，状态名一个都没动。
     */
    function orderedChoices(dispositions) {
        var list = (dispositions || []).filter(function (item) { return !!item; });
        return list.filter(function (item) { return item.value !== UNDO; })
            .concat(list.filter(function (item) { return item.value === UNDO; }));
    }

    /** 悬停说明。撤销这条也把服务端那个状态名带上，否则「撤销」撤到哪儿去没人知道。 */
    function actionTitle(value, label) {
        return value === UNDO ? ('撤销处置，改回「' + label + '」') : ('标成「' + label + '」');
    }

    function severityTone(severity) {
        return SEVERITY_TONE[String(severity || '')] || 'secondary';
    }

    /**
     * 服务端算好的那几个字符串（`severity_label` / `confidence_label` /
     * `disposition_at_display`）——**原样印，空了就什么都不印**。
     *
     * **空了不许回落到码值**：`severity_label === severity` 是服务端在说「平台不认识
     * 这个码」（`report_document._label` 就这么兜的），而空串是「服务端没算这一项」
     * （字段缺失 —— 前后端版本没对齐时才会遇到）。回落会把前者伪装成后者，也会让
     * 界面重新长出一张自己的映射表。码值只留在悬停提示里（`title`），读的人要拿它去
     * 对照模型原文时还找得到。
     */
    function shownText(value) {
        return value === null || value === undefined ? '' : String(value);
    }

    /** 证据元素未必都是字符串（模型偶尔回结构化的一条），直接 textContent 会得到 `[object Object]`。 */
    function evidenceText(item) {
        if (item === null || item === undefined) return '';
        if (typeof item === 'string') return item;
        try {
            return JSON.stringify(item);
        } catch (error) {
            return String(item);
        }
    }

    // -----------------------------------------------------------------------
    //  渲染
    // -----------------------------------------------------------------------
    function addTag(parent, className, text) {
        var node = doc().createElement('span');
        node.className = className;
        node.textContent = text;
        parent.appendChild(node);
        return node;
    }

    function addLine(parent, className, text) {
        var node = doc().createElement('div');
        node.className = className;
        node.textContent = text;
        parent.appendChild(node);
        return node;
    }

    /** 处置状态的颜色档（与严重度同一个道理：只上色，名字取服务端）。 */
    function dispositionTone(value) {
        if (value === 'confirmed') return 'success';
        if (value === 'ignored') return 'secondary';
        return 'light text-dark';
    }

    function renderItem(row, data) {
        var item = doc().createElement('div');
        item.className = 'ai-anomaly-item';
        item.setAttribute('data-anomaly-id', String(row.id));

        var head = doc().createElement('div');
        head.className = 'ai-anomaly-item-head';

        // 勾选框只在条目上，**没有「全选」**：表头上多一个全选框，多一次误点的代价是
        // 整份报告的结论被一次性处置掉。
        var pick = doc().createElement('input');
        pick.type = 'checkbox';
        pick.className = 'form-check-input ai-anomaly-pick';
        pick.checked = !!state.selected[row.id];
        pick.setAttribute('aria-label', '选中这一条以便批量处置');
        pick.addEventListener('change', function () { toggle(row.id, pick.checked); });
        head.appendChild(pick);

        // 严重度：**显示服务端那个中文名**（严重 / 高），颜色档仍然由码值决定 ——
        // 码值只进 `title`（读的人要拿它对照报告正文里模型原文的措辞）。
        var severityText = shownText(row.severity_label);
        if (severityText) {
            var severity = addTag(
                head, 'badge bg-' + severityTone(row.severity) + ' ai-anomaly-item-severity',
                severityText);
            if (row.severity) severity.setAttribute('title', String(row.severity));
        }
        var title = doc().createElement('span');
        title.className = 'ai-anomaly-item-title';
        title.textContent = row.title || '';
        head.appendChild(title);
        item.appendChild(head);

        var meta = doc().createElement('div');
        meta.className = 'ai-anomaly-item-meta';
        if (row.file_path) addTag(meta, 'ai-anomaly-item-file', row.file_path);
        var confidenceText = shownText(row.confidence_label);
        if (confidenceText) {
            var confidence = addTag(meta, 'ai-anomaly-item-confidence', '置信度 ' + confidenceText);
            if (row.confidence) confidence.setAttribute('title', String(row.confidence));
        }
        if (row.category) addTag(meta, 'ai-anomaly-item-category', row.category);
        item.appendChild(meta);

        var evidence = row.evidence || [];
        if (evidence.length) {
            var list = doc().createElement('ul');
            list.className = 'ai-anomaly-evidence';
            evidence.forEach(function (raw) {
                var li = doc().createElement('li');
                li.textContent = evidenceText(raw);
                list.appendChild(li);
            });
            item.appendChild(list);
        }
        // **逐条断言与各自的裁决**（P0-01）。它是这一条「凭什么算核实过了」的答案，
        // 而上面那个标题只是模型的概括。文案全部来自服务端（`display` / `status_label`
        // / `checked_scope` 都是算好的）—— 这里只排版，不另说一句话。
        //
        // 待核查的排在最前（服务端已经排好了次序）：这一段的用途是让人一眼看到
        // 「哪几条还没立住」，而不是从头读一遍。
        var claims = row.claims || [];
        if (claims.length) {
            var claimsList = doc().createElement('ul');
            claimsList.className = 'ai-anomaly-claims';
            claims.forEach(function (claim) {
                if (!claim || !claim.display) return;
                var li = doc().createElement('li');
                li.className = 'ai-anomaly-claim ai-anomaly-claim-'
                    + String(claim.status || 'unverified');
                li.textContent = (claim.claim_id ? '[' + claim.claim_id + '] ' : '')
                    + (claim.status_label || '') + ' —— ' + claim.display
                    + (claim.checked_scope ? '（查过：' + claim.checked_scope + '）' : '');
                claimsList.appendChild(li);
            });
            if (claimsList.childNodes.length) item.appendChild(claimsList);
        }
        if (row.impact) addLine(item, 'ai-anomaly-item-impact', '影响：' + row.impact);
        if (row.suggestion) addLine(item, 'ai-anomaly-item-suggestion', '建议：' + row.suggestion);

        // 当前处置状态：**只有真的有才显示**处置人 / 时间 / 备注。撤销之后这三个字段
        // 会被服务端一起清空（`set_disposition`），所以「待确认」那条不会挂着上一轮的备注。
        // 时间印的是服务端算好的 `disposition_at_display`（北京时间），**不是**库里那个
        // naive-UTC 的 `disposition_at`。空串（没处置）与「没处置人」同一条分支。
        var current = doc().createElement('div');
        current.className = 'ai-anomaly-item-disposition';
        addTag(current, 'badge bg-' + dispositionTone(row.disposition) + ' ai-anomaly-item-state',
               labelOf(data.dispositions, row.disposition));
        if (row.disposition_by) addTag(current, 'ai-anomaly-item-by', '处置人：' + row.disposition_by);
        var whenText = shownText(row.disposition_at_display);
        if (whenText) addTag(current, 'ai-anomaly-item-at', '时间：' + whenText);
        if (row.disposition_note) addTag(current, 'ai-anomaly-item-note', '备注：' + row.disposition_note);
        item.appendChild(current);

        var actions = doc().createElement('div');
        actions.className = 'ai-anomaly-item-actions';
        var note = doc().createElement('input');
        note.type = 'text';
        note.className = 'form-control form-control-sm ai-anomaly-note';
        note.setAttribute('placeholder', '备注（可选）：为什么确认 / 忽略');
        note.setAttribute('aria-label', '给这一条写一句备注');
        note.value = state.notes[row.id] || '';
        note.addEventListener('input', function () { state.notes[row.id] = note.value; });
        actions.appendChild(note);

        orderedChoices(data.dispositions).forEach(function (choice) {
            var label = labelOf(data.dispositions, choice.value);
            var button = doc().createElement('button');
            button.type = 'button';
            button.className = 'btn btn-sm ' + (ACTION_TONE[choice.value] || 'btn-outline-secondary')
                + ' ai-anomaly-act';
            button.setAttribute('data-action', choice.value);
            button.textContent = actionText(choice.value, label);
            button.setAttribute('title', actionTitle(choice.value, label));
            button.addEventListener('click', function () {
                return submitSingle(row.id, choice.value).catch(noop);
            });
            actions.appendChild(button);
        });
        item.appendChild(actions);
        return item;
    }

    function renderToolbar(data) {
        refs.batchButtons = [];
        var bar = doc().createElement('div');
        bar.className = 'ai-anomaly-toolbar';

        addTag(bar, 'ai-anomaly-counts', countsText(data));
        refs.picked = addTag(bar, 'ai-anomaly-picked', '');

        var note = doc().createElement('input');
        note.type = 'text';
        note.className = 'form-control form-control-sm ai-anomaly-batch-note';
        note.setAttribute('placeholder', '批量备注（可选）');
        note.setAttribute('aria-label', '给这批处置写一句备注');
        note.value = state.batchNote || '';
        note.addEventListener('input', function () { state.batchNote = note.value; });
        bar.appendChild(note);

        orderedChoices(data.dispositions).forEach(function (choice) {
            var label = labelOf(data.dispositions, choice.value);
            var button = doc().createElement('button');
            button.type = 'button';
            button.className = 'btn btn-sm ' + (ACTION_TONE[choice.value] || 'btn-outline-secondary')
                + ' ai-anomaly-batch';
            button.setAttribute('data-action', choice.value);
            // 「批量」是动作词（状态名仍然是服务端那个 label）—— 「批量：撤销」与
            // 单条那个「撤销」是同一个动作，只是作用在勾选的那些条目上。
            button.textContent = '批量：' + actionText(choice.value, label);
            button.setAttribute('title', '把选中的条目' + actionTitle(choice.value, label));
            button.addEventListener('click', function () {
                return submitBatch(choice.value).catch(noop);
            });
            refs.batchButtons.push(button);
            bar.appendChild(button);
        });

        paintToolbar();
        return bar;
    }

    /**
     * 只刷新工具条上会变的两处：勾选条数与批量按钮的可用状态。
     * **不整表重画**：勾选是高频动作，整表重画会把用户正在输入的那个备注框（以及
     * 它的焦点）换掉。
     */
    function paintToolbar() {
        var picked = selectedIds().length;
        if (refs.picked) refs.picked.textContent = '已选 ' + picked + ' 条';
        (refs.batchButtons || []).forEach(function (button) {
            // **一条都没选时批量按钮必须不可用**：后端对空 `ids` 回 400，让人先点一下
            // 再读一条错误消息，是把服务端的校验当界面校验用。
            button.disabled = picked === 0;
        });
    }

    function render() {
        var node = panel();
        if (!node) return;
        node.textContent = '';
        if (state.runId === null || state.runId === undefined) return;
        if (state.error) node.appendChild(alertBox('ai-anomaly-error alert alert-danger', state.error));
        if (state.notice) node.appendChild(alertBox('ai-anomaly-notice alert alert-warning', state.notice));
        if (state.loading) {
            addLine(node, 'ai-anomaly-meta', NOTE.loading);
            return;
        }
        var data = state.payload;
        if (!data) return;
        node.appendChild(renderHeader());
        var list = data.anomalies || [];
        if (!list.length) {
            addLine(node, 'ai-anomaly-meta', NOTE.empty);
            return;
        }
        node.appendChild(renderToolbar(data));
        var items = doc().createElement('div');
        items.className = 'ai-anomaly-list';
        list.forEach(function (row) { items.appendChild(renderItem(row, data)); });
        node.appendChild(items);
    }

    function renderHeader() {
        var head = doc().createElement('div');
        head.className = 'ai-anomaly-header';
        var title = doc().createElement('h6');
        title.className = 'ai-anomaly-header-title';
        title.textContent = '结构化结论与处置';
        head.appendChild(title);
        addTag(head, 'ai-anomaly-header-hint',
               '处置会记下是谁、什么时候，并影响下一轮还会不会再报这一条。');
        return head;
    }

    function alertBox(className, text) {
        var node = doc().createElement('div');
        node.className = className;
        // 出错时这行字是**唯一**的反馈（列表可能已经滚出视野），所以按告警播报，
        // 而不是让它静静地待在文档流里。
        node.setAttribute('role', 'alert');
        node.textContent = text;
        return node;
    }

    // -----------------------------------------------------------------------
    //  勾选
    // -----------------------------------------------------------------------
    function selectedIds() {
        var data = state.payload || {};
        var out = [];
        // 按**清单里的次序**收集：批量请求体里 ids 的顺序因此是确定的（也就是用户
        // 看到的那一列的顺序），换一个遍历顺序会得到同一件事的两种写法。
        (data.anomalies || []).forEach(function (row) {
            if (row && state.selected[row.id]) out.push(row.id);
        });
        return out;
    }

    function toggle(anomalyId, checked) {
        if (checked) state.selected[anomalyId] = true;
        else delete state.selected[anomalyId];
        paintToolbar();
    }

    // -----------------------------------------------------------------------
    //  取数 / 写
    // -----------------------------------------------------------------------
    function fetchJson(fetchImpl, url) {
        var doFetch = fetchImpl || global.fetch;
        return doFetch(url, {cache: 'no-store'}).then(readJson);
    }

    /**
     * POST 不带 CSRF 头：`templates/base.html` 里那个全局 fetch 包装器会补
     * （`X-CSRF-Token` 与 `X-Requested-With`）。在这里自己拼一份，等于把 token 的
     * 取法抄成第二处 —— 服务端换了 cookie 名或 header 名，这里静默失败。
     */
    function postJson(fetchImpl, url, body) {
        var doFetch = fetchImpl || global.fetch;
        return doFetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        }).then(readJson);
    }

    function readJson(response) {
        // body 不是 JSON 时（比如反代回的 HTML 错误页）也要给出一句能读的话：
        // 直接把 `response.json()` 的解析错误抛出去，用户看到的是
        // 「Unexpected token < in JSON at position 0」。
        return response.json().catch(function () { return null; }).then(function (payload) {
            if (!response.ok || !payload || payload.success === false) {
                throw new Error((payload && payload.message) || ('HTTP ' + response.status));
            }
            return payload;
        });
    }

    function noop() {}

    function reset(runId) {
        state.runId = runId;
        state.payload = null;
        state.loading = runId !== null && runId !== undefined;
        // 换一次运行就是换一份清单：勾选与备注都不许跟过来（它们描述的是上一批条目）。
        state.selected = {};
        state.notes = {};
        state.batchNote = '';
        state.error = null;
        state.notice = null;
        state.busy = false;
    }

    /**
     * 载入某一次运行的结论清单。**同一份数据只取一次**：历史面板的 body 每次重画都是
     * 整体重建的（容器是新节点），而一次选中会触发两次重画 —— 不认这一条，每翻一次
     * 就会取两遍，第二遍还把第一遍的结果原样盖回去。
     */
    function load(runId, fetchImpl) {
        if (runId === null || runId === undefined) {
            reset(null);
            render();
            return Promise.resolve(null);
        }
        if (Number(runId) === Number(state.runId) && (state.payload || state.loading)) {
            render();
            return Promise.resolve(null);
        }
        reset(runId);
        if (!panel()) return Promise.resolve(null);
        render();
        return fetchJson(fetchImpl, anomaliesUrl(runId)).then(function (payload) {
            // 取回来时用户可能已经翻到别的运行了：丢掉这一份，别把别人的清单画上去。
            if (Number(runId) !== Number(state.runId)) return null;
            state.loading = false;
            state.payload = payload;
            render();
            return payload;
        }, function (error) {
            if (Number(runId) !== Number(state.runId)) return null;
            state.loading = false;
            // **不吞**：403 / 404 / 400 的服务端 message 原样显示出来。
            state.error = NOTE.load_error + ((error && error.message) || NOTE.unknown);
            render();
            return null;
        });
    }

    /**
     * 发一次写请求。失败时：错误画在面板上、按钮恢复可用，然后把错误继续抛出去
     * （调用方是按钮的监听器，它接住只是为了不在控制台留下一条没人接的红字）。
     */
    function send(url, body, fetchImpl) {
        state.busy = true;
        state.error = null;
        state.notice = null;
        return postJson(fetchImpl, url, body).then(function (payload) {
            state.busy = false;
            return payload;
        }, function (error) {
            state.busy = false;
            state.error = NOTE.save_error + ((error && error.message) || NOTE.unknown);
            render();
            throw error;
        });
    }

    function submitSingle(anomalyId, disposition, fetchImpl) {
        // 连点两下会发两次 POST（后一次把前一次的结果盖掉）。用一个「在飞」的标记挡住，
        // 而不是把按钮置灰 —— 置灰要在每个重画分支里记得恢复，漏一处就是永久禁用。
        if (state.busy) return Promise.resolve(null);
        return send(dispositionUrl(anomalyId), {
            disposition: disposition,
            // 撤销时**不带备注**：服务端会把处置人/时间/备注三个一起清空
            // （`set_disposition`），带了也不落库 —— 而用户会以为那句话记下了。
            note: disposition === UNDO ? '' : (state.notes[anomalyId] || '')
        }, fetchImpl).then(function (payload) {
            mergeAnomaly(payload.anomaly);
            state.notice = payload.note_truncated ? NOTE.truncated : null;
            render();
            return payload;
        });
    }

    function submitBatch(disposition, fetchImpl) {
        if (state.busy) return Promise.resolve(null);
        var ids = selectedIds();
        // 按钮在没勾选时就是禁用的（`paintToolbar`），这里只是不让自己造出一个空 ids
        // 的请求 —— 后端会回 400，而那是把服务端的校验当成界面校验用。
        if (!ids.length) return Promise.resolve(null);
        return send(batchUrl(state.runId), {
            ids: ids,
            disposition: disposition,
            note: disposition === UNDO ? '' : (state.batchNote || '')
        }, fetchImpl).then(function (payload) {
            // 批量接口回的是**一份权威数据**（`anomalies` + `counts` + `total`），直接用它
            // 重画。**勾选保留**：用户刚点错一个按钮时，反手点「批量：撤销」正是他要做的，
            // 清空勾选会让他重新勾一遍。
            var list = payload.anomalies || [];
            state.payload = {
                anomalies: list,
                counts: payload.counts || {},
                total: payload.total === undefined || payload.total === null
                    ? list.length : payload.total,
                dispositions: payload.dispositions
                    || (state.payload && state.payload.dispositions) || []
            };
            state.notice = payload.note_truncated ? NOTE.truncated : null;
            render();
            return payload;
        });
    }

    /**
     * 把单条写的结果并回清单。
     *
     * 单条接口只回那一行、**不回 counts**，所以计数在本地按同一份清单重算 ——
     * 重算的口径与「不按状态过滤」是一件事，见文件头口径 3。
     */
    function mergeAnomaly(anomaly) {
        var data = state.payload;
        if (!data || !anomaly) return;
        var list = data.anomalies || [];
        for (var i = 0; i < list.length; i += 1) {
            if (Number(list[i].id) === Number(anomaly.id)) {
                list[i] = anomaly;
                break;
            }
        }
        data.counts = recount(list, data.dispositions);
        data.total = list.length;
    }

    function state_() {
        var data = state.payload || {};
        return {
            runId: state.runId,
            loading: state.loading,
            busy: state.busy,
            error: state.error,
            notice: state.notice,
            selected: selectedIds(),
            ids: (data.anomalies || []).map(function (row) { return row.id; }),
            counts: data.counts || null,
            total: data.total === undefined ? null : data.total
        };
    }

    global.AiAnomalyDisposition = {
        NOTE: NOTE,
        PANEL_ID: PANEL_ID,
        UNDO: UNDO,
        anomaliesUrl: anomaliesUrl,
        dispositionUrl: dispositionUrl,
        batchUrl: batchUrl,
        labelOf: labelOf,
        countsText: countsText,
        recount: recount,
        actionText: actionText,
        orderedChoices: orderedChoices,
        actionTitle: actionTitle,
        severityTone: severityTone,
        evidenceText: evidenceText,
        load: load,
        toggle: toggle,
        state: state_
    };
})(typeof window !== 'undefined' ? window : this);
