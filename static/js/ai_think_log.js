/* 「思考过程」标签里的逐轮记录：跑的时候读进度快照，跑完之后读落库的明细。
 *
 * ---------------------------------------------------------------------------
 * 一条必须先说清的实话
 * ---------------------------------------------------------------------------
 * **平台没有逐字输出**。模型调用是非流的（一次请求吃下整份提示词，跑完才拿到回复），
 * SSE 里的 chunk 是跑完之后把报告按行推出去的。所以这里能做到的「实时」是**每轮一次**：
 * 每一轮跑完，进度快照里多一条。面板顶上那句说明必须如实这么写 —— 写成「实时思考」
 * 会让人以为能看到逐字推理，然后盯着一个几十秒不动的面板怀疑它卡住了。
 *
 * ---------------------------------------------------------------------------
 * 两种来源，一种形状
 * ---------------------------------------------------------------------------
 *   * 跑动中 → `AiStreamStatus.watchRun` 的每一帧里 `progress.rounds[]`（进程内快照）；
 *   * 跑完后 → `GET /ai-analysis/runs/<id>/usage` 的 `rounds[]`（落库的 trace）。
 *
 * 两者的键**逐字相同**（那是服务端 `trace_evidence.live_round_entry` 与 `run_usage` 的
 * 硬约定，由 tests/test_ai_live_thinking_snapshot.py 钉着），所以本文件只有一个渲染器。
 * 唯一要自己处理的是：实时那份的 `executed` 已经只留了「取不到」的条目，落库那份是全量
 * 的 —— 渲染时两边都只画 `failed` / `empty` 的那些，显示就一致。
 *
 * 「取不到」（failed，有原因）与「确实没有内容」（empty）**分开写**：合并这两者等于把
 * 「没有证据」说成「这里没问题」，与 `services/ai/trace_evidence.py` 是同一条口径。
 *
 * ---------------------------------------------------------------------------
 * 六种处境，各自的说法
 * ---------------------------------------------------------------------------
 * `mode` 不是「有没有数据」，而是**现在能诚实地说什么**：
 *
 *   live              本页正看着它跑（轮询接着）→ 「每跑完一轮多一条」；
 *                     `rounds` 为空时另说一句「还没有跑完第一轮」；
 *   loading           正去取落库的逐轮 → 「正在读取…」（取之前那一瞬间）；
 *   settled           跑完了，看的是落库的明细 → 「分析已结束，下面是逐轮记录」；
 *   unavailable       看不到它的进度：跑在别的进程 / 刷新页面时它已经在跑 / 取明细失败。
 *                     **手上已经有几轮时换一句**（「下面是已经拿到的逐轮记录」）——
 *                     已经看到的轮次是真的，不该被一次读不到抹掉，也不该说成「没有」；
 *                     `blocks` 为空才用那句「读不到进度」；
 *   empty             这个目标还没有跑过分析。
 *
 * 这几句话只在本文件里出现（模板一个字都不许自己拼 ——「同一句话在多处各自演化」是
 * 这个仓库反复出问题的地方）。
 */
(function (global) {
    'use strict';

    var NOTE = {
        live: '分析进行中：平台按轮取数，每跑完一轮这里才会多一条 —— 不是逐字输出。',
        running_no_rounds: '分析进行中：还没有跑完第一轮。每跑完一轮，这里会多一条。',
        loading: '正在读取这次运行的逐轮记录…',
        settled: '分析已结束，下面是这次运行的逐轮记录。',
        no_trace: '这次运行没有留下逐轮记录（这个功能上线前的分析没有记逐轮明细）。',
        unavailable: '读不到这次运行的逐轮进度（跑在别的进程，或快照已过期）。'
            + '跑完之后这里会显示落库的逐轮记录。',
        // 已经画出几轮、又读不到最新进度时用这一句：**手上那几轮照样是真的**（它们是
        // 这一次运行的过程），不能说成「没有」，也不能说成「正在跑、马上会多」。
        unavailable_stale: '读不到这次运行的最新进度（跑在别的进程，或快照已过期）。'
            + '下面是已经拿到的逐轮记录。',
        empty: '这个目标还没有跑过分析。'
    };

    // 结局那几个词与 `ai_usage_service` 的读法一致；`unparsable` 单独说，因为它是
    // 「模型这一轮没给出能解析的东西」——不是「没要上下文」。
    var OUTCOME = {
        requests: '索取上下文',
        final: '给出结论',
        unparsable: '返回无法解析',
        transport_error: '调用失败'
    };

    var mode = 'empty';
    var runId = null;
    var watching = false;
    var loaded = false;
    var loading = false;
    // 当前画着的那几块（`buildRoundBlocks` 的产物）。留住它是为了让「只改一句说明」的
    // 场景（跑完那一刻）能重画，而不必重新算一遍或把列表留在旧状态。
    var blocks = [];

    function el(id) {
        return global.document ? global.document.getElementById(id) : null;
    }

    function internals() {
        // 数字口径只有一处：`ai_usage_line.js` 的 fmtTokens / fmtDuration。
        // 这里不另写一套 k / M —— 同一个数在两个地方显示成两个样子是最容易被发现的
        // 那种不一致。
        return global.aiUsageInternals || null;
    }

    function fmtTokens(value) {
        var api = internals();
        var text = api && api.fmtTokens ? api.fmtTokens(value) : null;
        if (text) return text;
        var num = Number(value);
        // 0 与「未上报」都不写：0 个 token 是一个不可能的数，写出来只是噪音。
        return isFinite(num) && num > 0 ? String(num) : '';
    }

    function fmtDuration(ms) {
        var api = internals();
        var text = api && api.fmtDuration ? api.fmtDuration(ms) : null;
        if (text) return text;
        var num = Number(ms);
        return isFinite(num) && num > 0 ? String(num) : '';
    }

    /** 分片前缀。有完整位次时交给共享模块（那一行的口径与 meta 行一致），只有名字时自己说。 */
    function shardText(entry) {
        var name = entry && typeof entry.agent === 'string' ? entry.agent : '';
        if (!name) return '';
        var withPosition = global.AiStreamStatus && global.AiStreamStatus.agentText;
        if (withPosition) {
            var text = withPosition({
                agent: name,
                agent_index: entry.agent_index,
                agent_total: entry.agent_total
            });
            if (text) return text;
        }
        // 落库那份没有家族位次（`run_usage` 的逐轮行只有 `agent`），所以只能说名字 ——
        // **不编一个 (1/3)**：编出来的位次会被当成事实。
        return '分片 ' + name;
    }

    /**
     * 一条轮次记录 → 要画的那几个块。**纯函数**（node 下真跑）。
     */
    function buildRoundBlocks(rounds, meta) {
        var total = meta && meta.max_rounds ? Number(meta.max_rounds) : 0;
        var blocks = [];
        (rounds || []).forEach(function (entry, position) {
            if (!entry || typeof entry !== 'object') return;
            var index = Number(entry.round_index);
            if (!isFinite(index) || index <= 0) index = position + 1;
            var roundNo = Number(entry.agent_round) > 0 ? Number(entry.agent_round) : index;
            var shard = shardText(entry);
            var head = (shard ? shard + ' · ' : '') + '第 ' + roundNo
                + (total > 0 ? '/' + total : '') + ' 轮';
            var outcome = OUTCOME[entry.outcome] || entry.outcome || '';
            if (outcome) head += ' · ' + outcome;
            var tokens = fmtTokens(entry.tokens_input);
            if (tokens) head += ' · 输入 ' + tokens + ' tokens';
            var cost = fmtDuration(entry.duration_ms);
            if (cost) head += ' · ' + cost;

            var missing = [];
            var empty = [];
            (entry.executed || []).forEach(function (item) {
                if (!item || typeof item !== 'object') return;
                var label = item.label || item.kind || '';
                if (item.failed) {
                    missing.push({ label: label, reason: item.reason || '' });
                } else if (item.empty) {
                    empty.push({ label: label });
                }
            });

            blocks.push({
                key: String(index) + '-' + (entry.agent || ''),
                head: head,
                requests: (entry.requests || []).map(function (item) {
                    return (item && item.text) || '';
                }).filter(Boolean),
                missing: missing,
                empty: empty,
                dropped: (entry.dropped || []).map(function (item) {
                    return {
                        detail: (item && (item.detail || item.kind)) || '',
                        reason: (item && item.reason) || ''
                    };
                }),
                // 模型这一轮的原话：结论那一轮（final）的原文就是整份报告，不在这里重复贴
                // —— 报告在「完整结论」那个标签里，完整、且是渲染过的。
                modelText: entry.outcome === 'final' ? '' : (entry.response_text || ''),
                // 于是结论那一轮会是一张「只有头一行」的卡，看着像空的 —— 说一句它为什么
                // 是空的，并指明去哪儿看（这一段话也只能有一份，所以它长在这里）。
                hint: entry.outcome === 'final'
                    ? '这一轮返回的就是完整结论，内容在「完整结论」标签里。' : '',
                budgetNotes: entry.budget_notes || '',
                error: entry.error || ''
            });
        });
        return blocks;
    }

    function noteFor(current, count) {
        if (current === 'live') return count ? NOTE.live : NOTE.running_no_rounds;
        if (current === 'loading') return NOTE.loading;
        if (current === 'settled') return count ? NOTE.settled : NOTE.no_trace;
        if (current === 'unavailable') return count ? NOTE.unavailable_stale : NOTE.unavailable;
        return NOTE.empty;
    }

    function addLine(parent, className, text) {
        if (!text) return;
        var node = global.document.createElement('p');
        node.className = className;
        node.textContent = text;
        parent.appendChild(node);
    }

    function paint() {
        var log = el('aiThinkLog');
        var note = el('aiThinkNote');
        // 顶上那句说明每次重画都跟着当前 `mode` 更新：跑完那一刻（`unwatch`）**必须**把
        // 「分析进行中」换掉，否则列表已经六轮了、那句话还挂着，两句话不能同时为真。
        if (note) note.textContent = noteFor(mode, blocks.length);
        if (!log) return;
        log.textContent = '';
        if (!blocks.length) return;
        var doc = global.document;
        blocks.forEach(function (block) {
            var card = doc.createElement('div');
            card.className = 'ai-think-round';
            card.setAttribute('data-round-key', block.key);

            var head = doc.createElement('div');
            head.className = 'ai-think-round-head';
            head.textContent = block.head;
            card.appendChild(head);

            if (block.requests.length) {
                var label = doc.createElement('p');
                label.className = 'ai-think-label';
                label.textContent = '这一轮点名要看的东西';
                card.appendChild(label);
                var list = doc.createElement('ul');
                list.className = 'ai-think-list';
                block.requests.forEach(function (text) {
                    var li = doc.createElement('li');
                    li.textContent = text;
                    list.appendChild(li);
                });
                card.appendChild(list);
            }

            block.missing.forEach(function (item) {
                addLine(card, 'ai-think-missing',
                         '取不到：' + item.label + (item.reason ? '（' + item.reason + '）' : ''));
            });
            block.empty.forEach(function (item) {
                addLine(card, 'ai-think-empty', '确实没有内容：' + item.label);
            });
            block.dropped.forEach(function (item) {
                addLine(card, 'ai-think-dropped',
                         '未执行：' + item.detail + (item.reason ? '（' + item.reason + '）' : ''));
            });
            if (block.budgetNotes) addLine(card, 'ai-think-note-line', block.budgetNotes);
            if (block.hint) addLine(card, 'ai-think-note-line', block.hint);
            if (block.error) addLine(card, 'ai-think-missing', block.error);

            if (block.modelText) {
                var details = doc.createElement('details');
                details.className = 'ai-think-model';
                var summary = doc.createElement('summary');
                summary.textContent = '模型这一轮返回的内容';
                details.appendChild(summary);
                var body = doc.createElement('div');
                body.className = 'ai-think-model-body';
                // 模型输出 → 走既有的渲染器（它先整体转义再套白名单，见
                // static/js/ai-report-markdown.js），不在这里自己拼 HTML。
                if (global.AiReportMarkdown && global.AiReportMarkdown.render) {
                    body.innerHTML = global.AiReportMarkdown.render(block.modelText);
                } else {
                    body.textContent = block.modelText;
                }
                details.appendChild(body);
                card.appendChild(details);
            }
            log.appendChild(card);
        });
    }

    /** 本页开始看着这次运行（新一轮开始 / 轮询已接上）。`id` 可以还不知道（先跑再发运行号）。 */
    function watch(id) {
        // 运行号还没发过来时把上一次的**一并忘掉**：留着它，面板在「看的是哪一次」这件事
        // 上就会说谎（`ensureLoaded` 还会按着一个过期的号去取明细）。
        applyRun((id === undefined || id === null) ? null : id);
        watching = true;
        loading = false;
        mode = 'live';
        blocks = [];
        paint();
    }

    /**
     * 本页不再看着它跑。已经画出来的那几轮**不动**：它们是这次运行的过程，跑完之后仍然
     * 该看得到（用户明确要的）—— 换掉的只是顶上那句说明。
     *
     * `options.settled` = **这次运行真的结束了**（轮询读到终态 / SSE 收到终态事件）。
     * 它只影响一件事：要不要顺手去取一次落库的逐轮。
     *
     * **这个参数是必须的**，因为「跑完了」与「用户不想看了」是两件事，而它们以前共用
     * 这一个函数。逐轮是**跑完之后才落库**的（服务端 `_persist_outcome`），所以跑动中
     * 取回的那一份必然是空表 —— 把它当终态收下（`loaded = true`）之后，这份记录就**再也
     * 刷不出来了**：跑完再打开抽屉时 `setRun` 会因为运行号没变而早退，`ensureLoaded` 又被
     * `loaded` 挡在门外。表现是「思考过程」对这次运行**永远**说「这次运行没有留下逐轮记录」，
     * 而明细一直在库里。触发序列很日常：跑起来 → 关抽屉 → 等它跑完 → 再打开抽屉。
     */
    function unwatch(options) {
        var opts = options || {};
        watching = false;
        if (mode === 'live') mode = 'settled';
        paint();
        // 手上一条都没画出来，而且还没问过落库那份 → **在这里发起取数**。
        //
        // 这句话是承诺性质的：`unavailable` 那句写着「跑完之后这里会显示落库的逐轮记录」，
        // 而跑完这一刻正是它说的那个时刻。不在这里发起的话，面板会**永远停在那句承诺上**：
        // 唯一会取数的 `ensureLoaded` 挂在「切到思考过程」这个动作上，而用户一直停在这个
        // 标签页里（他手动点过标签，跑完就不会被自动切走）就永远等不到。
        //
        // 已经画出几轮时不取：那些轮次是真的，而"再看一眼落库那份"要等用户切标签
        // （懒加载那条口径见 `ensureLoaded`）；跑动中也不取（下一句 `applyProgress`
        // 会把 `watching` 认回来）。
        if (opts.settled && !blocks.length && !loaded && runId !== null) ensureLoaded();
    }

    /** 跑动中的一帧（`progress.rounds`）。**整份替换**，不做增量 —— 每帧本来就是全量。 */
    function applyProgress(progress) {
        if (!progress) {
            // 这一帧的载荷里没有进度（别的进程在跑、或快照已过期）。**这是「读不到」，
            // 不是「还没跑到第一轮」** —— 后者是 `progress` 在、`rounds` 为空。
            //
            // **已经画出来的那几轮不清**：它们是这一次运行真实跑过的轮次（`setRun` 换了
            // 运行号才会清），一次读不到就抹掉等于把看到的证据收回去；而下一帧往往就
            // 恢复正常了。清掉的话用户看到的是过程**闪一下没了**。
            watching = false;
            mode = 'unavailable';
            paint();
            return;
        }
        // 拿到真进度 = 本页确实连在这一次运行上。**在这里重新认一次**，而不是只靠
        // 开跑那一刻的 `watch()`：中间可能夹着一帧读不到的载荷（快照过期、跑在别的
        // worker 里），那一帧会把 `watching` 打掉，而后面几帧又恢复正常 —— 不重新认，
        // 面板就会在「读不到」与「在跑」之间来回跳，且 `ensureLoaded` 会在跑动中去取
        // 落库的明细，把实时的过程换成一份「分析已结束」。
        watching = true;
        mode = 'live';
        blocks = buildRoundBlocks(progress.rounds, { max_rounds: progress.max_rounds });
        paint();
    }

    /** 落库的逐轮（`/runs/<id>/usage` 的 `rounds`）。 */
    function applyRounds(rounds, meta) {
        loaded = true;
        loading = false;
        mode = 'settled';
        blocks = buildRoundBlocks(rounds, meta || {});
        paint();
    }

    /** 记住这是哪个运行。**换了运行就把上一次的列表清掉**：留在那儿会被读成「这一次的过程」。 */
    function setRun(nextRunId) {
        var value = (nextRunId === undefined || nextRunId === null) ? null : nextRunId;
        if (value === runId) return;
        applyRun(value);
    }

    function applyRun(value) {
        runId = value;
        loaded = false;
        loading = false;
        mode = value === null ? 'empty' : (watching ? 'live' : 'settled');
        blocks = [];
        paint();
    }

    /** 这个目标没跑过分析（`/latest` 说没有结果）。 */
    function markEmpty() {
        // **不转手给 `setRun(null)`**：这个目标从没跑过时 `runId` 本来就是 null，
        // `setRun` 会在「值没变」那一步早退，于是面板顶上是一片空白 —— 而那句
        // 「这个目标还没有跑过分析」正是靠这次重画写上去的。
        applyRun(null);
    }

    /**
     * 有一次运行在跑，但**本页看不到它的进度**（刷新页面时它已经在跑了、或者跑在别的
     * 进程里）。如实说读不到 —— 不假装「还没有跑完第一轮」。
     */
    function markExternalRun() {
        // 本页正看着它跑（或以轮询的帧为准）→ 别把 live 打成读不到。
        if (watching) return;
        // 已经画着这一次的逐轮 = 本页其实看得到它。再清一次等于**把已经显示出来的过程
        // 抹掉**，而调用方（`loadLatestResult` 的「进行中」那一支）是在用户点「刷新结果」
        // 时进来的 —— 那一下不该让面板变空。
        if (blocks.length) return;
        mode = 'unavailable';
        paint();
    }

    function currentRunId() {
        return runId;
    }

    function isWatching() {
        return watching;
    }

    /**
     * 用户切到「思考过程」时调它：**没有实时数据就按运行号去取落库的逐轮**。
     *
     * 懒加载是刻意的：没打开过这个标签的人不该为它多付一次请求（这个面板只是看一眼）。
     * 看着跑的期间不取（落库的 trace 与实时那份是同一件事，取了只会来回换来源）；
     * 已经取过的也不取。**取不到不是错误**，如实换一句话说（见 mode 'unavailable'）。
     */
    function ensureLoaded(fetchImpl) {
        if (loaded || loading || watching || runId === null) return;
        var doFetch = fetchImpl || global.fetch;
        if (!doFetch) return;
        // 记下**为哪个运行号取的**：取的过程中可能已经换了运行（跑完 → 用户立刻又点了
        // 「重新分析」，或者上一句 `applyRun` 把号换了）。旧的那份画上去就是
        // 「这一次的运行里显示着上一次的逐轮过程」，而且 `mode` 还会被写成「分析已结束」
        // —— 分析正跑着，界面上挂着「已结束」。
        var askedFor = runId;
        loading = true;
        mode = 'loading';
        paint();
        doFetch('/ai-analysis/runs/' + askedFor + '/usage', { cache: 'no-store' })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || payload.success === false) {
                        throw new Error(payload.message || ('HTTP ' + response.status));
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                // 换了运行号 / 又看着它跑了 → 丢掉这份响应。**`loading` 不用在这里收**：
                // 把它设成 false 的那两处（`applyRun` / `watch`）都已经跑过了。
                if (runId !== askedFor || watching) return;
                applyRounds(payload.rounds, {});
            })
            .catch(function (error) {
                if (runId !== askedFor) return;
                loading = false;
                // **与上面 `.then` 里同一道闸**：取数途中本页又看着这次运行了
                // （新一轮开跑、或轮询重新接上）—— 这份**失败**的响应同样不许写面板。
                // 写上去就是一句「读不到这次运行的逐轮记录：…」挂在一次正跑着的运行上，
                // 而上面那条路早就想明白了这件事（那里的 `watching` 就是为它写的）。
                //
                // `loading` 上面那行**必须先收**：认回 `watching` 的是 `applyProgress`，
                // 它不碰 `loading` —— 不收的话这个残留的标志位会把 `ensureLoaded` 永久
                // 挡在门外，面板就停在「跑完之后这里会显示落库的逐轮记录」那句承诺上，
                // 而那句话正是 `unwatch` 自己发起取数要兑现的。
                if (watching) return;
                mode = 'unavailable';
                blocks = [];
                paint();
                var note = el('aiThinkNote');
                if (note) {
                    note.textContent = '读不到这次运行的逐轮记录：'
                        + ((error && error.message) || '未知错误');
                }
            });
    }

    /** 关抽屉/离开这一页：把「看着它跑」这件事清掉，已经画出来的内容留着。 */
    function reset() {
        watching = false;
        loading = false;
        // 不再看着它跑，就不能再说「分析进行中」——那两句话不能同时为真。
        if (mode === 'live') mode = 'settled';
        paint();
    }

    function state() {
        return {mode: mode, runId: runId, watching: watching, loaded: loaded};
    }

    global.AiThinkLog = {
        NOTE: NOTE,
        OUTCOME: OUTCOME,
        buildRoundBlocks: buildRoundBlocks,
        shardText: shardText,
        watch: watch,
        unwatch: unwatch,
        applyProgress: applyProgress,
        applyRounds: applyRounds,
        setRun: setRun,
        markEmpty: markEmpty,
        markExternalRun: markExternalRun,
        currentRunId: currentRunId,
        isWatching: isWatching,
        ensureLoaded: ensureLoaded,
        reset: reset,
        state: state
    };
})(typeof window !== 'undefined' ? window : this);
