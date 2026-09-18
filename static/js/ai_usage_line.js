/* AI 分析抽屉里的一行「本次消耗」+ 明细弹层。
 *
 * ---------------------------------------------------------------------------
 * 为什么是一个共享文件、而不是抄进三份模板
 * ---------------------------------------------------------------------------
 * 这段要在 commit_diff_new / weekly_version_diff / merged_project_view 三份模板里
 * 都出现。那三份模板的 AI 抽屉块是**逐字复制**的（仓库既有事实，见
 * tests/test_ai_drawer_css_sync.py），所以最直接的做法是把这段 JS 也抄三遍 —— 但那样
 * 一个口径改动要改三处，而漏改一处不会有任何报错，只会让某一份模板的文案与另外两份
 * 不一致（例如只有它把「未上报」渲染成 0%）。
 *
 * 所以：**DOM 片段三份逐字相同 + 逻辑集中在本文件**。模板里那一段只有
 * 四行 DOM、一个 modal 和一次 <script src>，仍然由
 * tests/test_ai_usage_fragment_sync.py 逐字比对；本文件的函数则由
 * tests/test_ai_usage_drawer_frontend.py 用 node 真跑。
 *
 * ---------------------------------------------------------------------------
 * 三条口径（与后端 services/ai/usage.py 完全一致）
 * ---------------------------------------------------------------------------
 * 1. `null` = 上游没上报，`0` = 报了且确实是 0。**「未上报」绝不渲染成 0 或 0%** ——
 *    两者对用户的含义正好相反。
 * 2. 没采集到用量（功能上线前的历史运行）显示「本次用量未采集」，同样不显示 0。
 * 3. 缓存命中指**上游 provider 的 prompt cache**，与工具结果在本次分析内的内存缓存
 *    命中是两件事，文案上不许都叫「缓存」。
 */
(function (global) {
    'use strict';

    var UNKNOWN = '未上报';
    var NOT_COLLECTED = '本次用量未采集';

    /** token 数 → 12.3k / 1.25M；未上报返回 null。 */
    function fmtTokens(value) {
        if (value === null || value === undefined) return null;
        var num = Number(value);
        if (!isFinite(num)) return null;
        if (Math.abs(num) >= 1000000) return (num / 1000000).toFixed(num >= 10000000 ? 1 : 2) + 'M';
        if (Math.abs(num) >= 1000) return (num / 1000).toFixed(1) + 'k';
        return String(num);
    }

    /** 命中率 0.9 → "90.0%"；未上报返回 null（**不是 "0%"**）。 */
    function fmtRate(rate) {
        if (rate === null || rate === undefined) return null;
        var num = Number(rate);
        if (!isFinite(num)) return null;
        return (num * 100).toFixed(1) + '%';
    }

    function fmtDuration(ms) {
        if (ms === null || ms === undefined) return null;
        var num = Number(ms);
        if (!isFinite(num)) return null;
        if (num >= 60000) return (num / 60000).toFixed(1) + ' min';
        if (num >= 1000) return (num / 1000).toFixed(1) + ' s';
        return num + ' ms';
    }

    /** 费用：后端给的是字符串（避免前端浮点），这里只补币种符号，不做算术。
     *  不足一分钱时后端给的是 `<0.01` —— 那是一个整体，符号要插在 `<` 之后：
     *  `<¥0.01` 读作「不到一分钱」，`¥<0.01` 读起来像币种后面跟了个比较符。 */
    function fmtCost(cost) {
        if (!cost || cost.amount === null || cost.amount === undefined) return null;
        var symbol = { CNY: '¥', USD: '$', EUR: '€' }[cost.currency] || '';
        var text = String(cost.amount);
        if (text.charAt(0) === '<') {
            return symbol ? '<' + symbol + text.slice(1)
                          : '<' + text.slice(1) + ' ' + (cost.currency || '');
        }
        return symbol + text + (symbol ? '' : ' ' + (cost.currency || ''));
    }

    /** 一行文案。拆出来是为了能单独用 node 断言（含「未上报」与「0」的区别）。 */
    function buildLine(usage) {
        if (!usage) return null;
        if (!usage.collected) return NOT_COLLECTED;
        var parts = [];
        var total = fmtTokens((usage.tokens || {}).total);
        parts.push(total === null ? '本次消耗未上报' : '本次消耗 ' + total + ' tokens');
        var rate = fmtRate((usage.cache || {}).hit_rate);
        parts.push(rate === null ? '缓存命中' + UNKNOWN : '命中缓存 ' + rate);
        if (usage.rounds !== null && usage.rounds !== undefined) parts.push(usage.rounds + ' 轮');
        var duration = fmtDuration(usage.duration_ms);
        if (duration !== null) parts.push(duration);
        return parts.join(' · ');
    }

    var state = { runId: null, modal: null };

    function el(id) { return document.getElementById(id); }

    function appendRow(table, label, value) {
        var tr = document.createElement('tr');
        var th = document.createElement('th');
        th.setAttribute('scope', 'row');
        th.textContent = label;
        var td = document.createElement('td');
        td.className = 'ai-usage-num';
        td.textContent = value === null || value === undefined ? UNKNOWN : value;
        if (value === null || value === undefined) td.classList.add('ai-usage-unknown');
        tr.appendChild(th);
        tr.appendChild(td);
        table.appendChild(tr);
    }

    function renderModal(payload) {
        var body = el('aiUsageModalBody');
        if (!body) return;
        body.textContent = '';
        var run = payload.run || {};
        var usage = run.usage || {};

        if (!usage.collected) {
            var note = document.createElement('p');
            note.className = 'ai-usage-meta';
            note.textContent = NOT_COLLECTED + '（这个功能上线前的分析没有记录用量，不是 0）。';
            body.appendChild(note);
        }

        var table = document.createElement('table');
        table.className = 'ai-usage-table';
        var tbody = document.createElement('tbody');
        var tokens = usage.tokens || {};
        appendRow(tbody, '输入 token（合计）', fmtTokens(tokens.input));
        appendRow(tbody, '其中命中缓存', fmtTokens(tokens.cache_read));
        var hitRate = fmtRate((usage.cache || {}).hit_rate);
        appendRow(tbody, '缓存命中率', hitRate === null ? null : hitRate);
        appendRow(tbody, '输出 token', fmtTokens(tokens.output));
        appendRow(tbody, '合计 token', fmtTokens(tokens.total));
        appendRow(tbody, '轮次 / 上下文索取', (usage.rounds === null || usage.rounds === undefined ? UNKNOWN : usage.rounds)
            + ' / ' + (usage.requests === null || usage.requests === undefined ? UNKNOWN : usage.requests));
        appendRow(tbody, '取回的上下文字符', usage.context_chars === null || usage.context_chars === undefined
            ? null : String(usage.context_chars));
        appendRow(tbody, '耗时', fmtDuration(usage.duration_ms));
        var costText = fmtCost(usage.cost);
        if (costText !== null) {
            var notes = (usage.cost.notes || []).join('；');
            appendRow(tbody, '费用估算', costText + (notes ? '（' + notes + '）' : ''));
        }
        table.appendChild(tbody);
        body.appendChild(table);

        var caption = document.createElement('p');
        caption.className = 'ai-usage-meta';
        if (usage.cost && usage.cost.amount !== null) {
            caption.textContent = '费用按当前价格表（版本 ' + (usage.cost.price_version || '未标') + '）估算，接口只回 token 数、不回金额。';
        } else if (!payload.pricing || !payload.pricing.configured) {
            caption.textContent = '这个项目还没有配置价格表，因此没有费用数字。';
        } else {
            caption.textContent = '费用算不出来：' + ((usage.cost && usage.cost.reason) || '未配置价格表');
        }
        body.appendChild(caption);

        var rounds = payload.rounds || [];
        var head = document.createElement('h6');
        head.className = 'ai-usage-subhead';
        head.textContent = '逐轮（输入 token 是累计值：每轮都会重发上一轮的上下文）';
        body.appendChild(head);
        var roundsTable = document.createElement('table');
        roundsTable.className = 'ai-usage-table';
        var thead = document.createElement('thead');
        thead.innerHTML = '<tr><th scope="col">轮次</th><th scope="col">结局</th>'
            + '<th scope="col" class="ai-usage-num">输入</th><th scope="col" class="ai-usage-num">命中</th>'
            + '<th scope="col" class="ai-usage-num">输出</th><th scope="col" class="ai-usage-num">耗时</th></tr>';
        roundsTable.appendChild(thead);
        var roundsBody = document.createElement('tbody');
        if (!rounds.length) {
            var tr = document.createElement('tr');
            var td = document.createElement('td');
            td.colSpan = 6;
            td.className = 'ai-usage-empty';
            td.textContent = '这次运行没有逐轮记录。';
            tr.appendChild(td);
            roundsBody.appendChild(tr);
        }
        rounds.forEach(function (round) {
            var tr = document.createElement('tr');
            var values = [
                String(round.round_index),
                round.outcome || '—',
                fmtTokens(round.tokens_input) || UNKNOWN,
                fmtTokens(round.cache_read_tokens) || UNKNOWN,
                fmtTokens(round.tokens_output) || UNKNOWN,
                fmtDuration(round.duration_ms) || UNKNOWN
            ];
            values.forEach(function (text, index) {
                var td = document.createElement('td');
                if (index >= 2) td.className = 'ai-usage-num';
                td.textContent = text;
                tr.appendChild(td);
            });
            roundsBody.appendChild(tr);
        });
        roundsTable.appendChild(roundsBody);
        body.appendChild(roundsTable);

        if (payload.rounds_truncated) {
            var more = document.createElement('p');
            more.className = 'ai-usage-meta';
            more.textContent = '只列出前若干轮。';
            body.appendChild(more);
        }
    }

    function openModal() {
        var body = el('aiUsageModalBody');
        var modalEl = el('aiUsageModal');
        if (!body || !modalEl) return;
        if (state.runId === null) return;
        body.innerHTML = '<p class="ai-usage-meta">加载中...</p>';
        if (state.modal === null && global.bootstrap && global.bootstrap.Modal) {
            state.modal = new global.bootstrap.Modal(modalEl);
        }
        if (state.modal) state.modal.show();
        fetch('/ai-analysis/runs/' + state.runId + '/usage', { credentials: 'same-origin' })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || payload.success === false) {
                        throw new Error(payload.message || ('HTTP ' + response.status));
                    }
                    return payload;
                });
            })
            .then(renderModal)
            .catch(function (error) {
                body.textContent = '加载失败：' + (error && error.message ? error.message : error);
            });
    }

    /**
     * 由三份模板各自的加载器与 SSE 回调调用。
     * `usage` 为 null/undefined 时整行隐藏（例如「正在进行中」根本没有用量）。
     */
    function renderAiUsageLine(usage, runId) {
        var lineEl = el('aiUsageLine');
        var textEl = el('aiUsageText');
        var btnEl = el('aiUsageDetailBtn');
        if (!lineEl || !textEl || !btnEl) return;
        state.runId = (runId === undefined || runId === null) ? null : runId;
        if (!usage) {
            lineEl.hidden = true;
            return;
        }
        var text = buildLine(usage);
        if (text === null) {
            lineEl.hidden = true;
            return;
        }
        lineEl.hidden = false;
        textEl.textContent = text;
        // 没有 run_id 就没法拉明细（后端按运行记录取数）—— 那时不显示按钮，
        // 而不是显示一个点了没反应的按钮。
        btnEl.hidden = state.runId === null || !usage.collected;
    }

    function init() {
        var btnEl = el('aiUsageDetailBtn');
        if (btnEl) btnEl.addEventListener('click', openModal);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    global.renderAiUsageLine = renderAiUsageLine;
    // 供 node 单测调用（这三条口径必须有独立的、脱离浏览器的证据）。
    global.aiUsageInternals = {
        fmtTokens: fmtTokens,
        fmtRate: fmtRate,
        fmtDuration: fmtDuration,
        fmtCost: fmtCost,
        buildLine: buildLine
    };
})(typeof window !== 'undefined' ? window : this);
