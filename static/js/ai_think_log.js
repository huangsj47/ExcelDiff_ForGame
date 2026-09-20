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
 *   starting          **才刚发起、第一帧进度还没出来**（见下面那段）；
 *   loading           正去取落库的逐轮 → 「正在读取…」（取之前那一瞬间）；
 *   settled           跑完了，看的是落库的明细 → 「分析已结束，下面是逐轮记录」；
 *   unavailable       看不到它的进度：跑在别的进程 / 刷新页面时它已经在跑 / 取明细失败。
 *                     **手上已经有几轮时换一句**（「下面是已经拿到的逐轮记录」）——
 *                     已经看到的轮次是真的，不该被一次读不到抹掉，也不该说成「没有」；
 *                     `blocks` 为空才用那句「读不到进度」；
 *   empty             这个目标还没有跑过分析。
 *
 * ---------------------------------------------------------------------------
 * 为什么「刚开始跑」要单独一句话
 * ---------------------------------------------------------------------------
 * 引擎**跑完第一轮**才第一次 `run_progress.publish`，而 `on_start` 那一帧要等它真正
 * 开始执行（见 services/ai_analysis_service.py 的 on_round / on_start）。中间这段
 * （周版本分析里可以是几十秒：装项目包、取变更集）快照是空的，载荷里 `progress` 为
 * `null` —— 与「跑在别的进程」逐字相同。
 *
 * 两件事的形状一样、含义相反，而原先它们共用一句话：用户点完「重新分析」，界面上
 * 立刻写着「读不到这次运行的逐轮进度（**跑在别的进程**，或快照已过期）」—— 运行明明
 * 就在眼前刚发起来，这句的**诊断是凭空来的**。所以刚发起那一段单独说一句，且**不带
 * 任何诊断**（"第一帧还没出来"是事实，"跑在别的进程"是猜测）。
 *
 * 这段说法**有期限**（`STARTING_WINDOW_MS`）：多节点部署里分析派给 Agent 节点执行，
 * 本进程**永远**不会有快照 —— 一直说「正在准备」等于把它说成一件马上会发生的事，
 * 而在那个部署下它一次都不会发生。过了期限就改口说读不到（那才是实话）。
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
        // 才刚发起、第一帧进度还没出来。**不带诊断**：这一刻能确定的事只有「还没报出
        // 第一帧」，说它跑在哪儿都是猜（见文件头那段）。
        starting: '分析已发起，还在准备：第一帧逐轮进度还没出来。'
            + '平台把第一轮派下去之后，这里每跑完一轮会多一条。',
        unavailable: '这次运行的逐轮进度读不到（它可能跑在别的进程，或快照已过期）。'
            + '这不影响分析本身；跑完之后这里会显示落库的逐轮记录。',
        // 已经画出几轮、又读不到最新进度时用这一句：**手上那几轮照样是真的**（它们是
        // 这一次运行的过程），不能说成「没有」，也不能说成「正在跑、马上会多」。
        unavailable_stale: '读不到这次运行的最新进度（它可能跑在别的进程，或快照已过期）。'
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
    // 本页**看着它开跑**的那一刻（`watch()` 记的），以及有没有见过快照。这两个合起来
    // 才是 `starting` 的判据（见 `startingNow`）——单看 `watching` 不行：读不到的那一帧
    // 会把 `watching` 打掉，于是第二帧就改口了，而它其实还在「刚发起」那一段里。
    var watchingSince = null;
    var sawSnapshot = false;
    // 最近一次知道的**这次运行的状态**（`applyProgress` 的 `status`）。它只服务一件事：
    // 分辨「取回落库的逐轮是空的」是因为**还没跑完**（逐轮是跑完才落库的，
    // 见服务端 `_persist_outcome`），还是因为这次运行真的没留下记录。
    // 少了它，「跑动中点一次思考过程」就会把空表当成终态收下（`loaded = true`），
    // 明细此后再也刷不出来。
    var lastRunStatus = null;
    // 这次运行**已经确认结束**了（SSE 收到终态、或轮询读到终态）。与 `lastRunStatus`
    // 是两件事：`status` 是「最近一次知道的状态」，而这个是「有人明确说过它完了」。
    // 逐轮是跑完才落库的，所以「取回来是空的」要按它分辨（见 `applyRounds`）。
    var runSettled = false;
    // 当前画着的那几块（`buildRoundBlocks` 的产物）。留住它是为了让「只改一句说明」的
    // 场景（跑完那一刻）能重画，而不必重新算一遍或把列表留在旧状态。
    var blocks = [];

    // 哪几轮的「模型这一轮返回的内容」是展开的（轮次的 key → true）。
    //
    // **必须由模块自己记，不能指望 DOM 留着。** 那一块是个 `<details>`，`open` 是
    // **DOM 属性**；而跑动中每来一帧进度（几秒一次）`applyProgress` 就会
    // `buildRoundBlocks` + `paint()` 重建整棵子树 —— 新节点回到默认的收起态。
    // 用户看到的现象正是「点开、过几秒自己收起来」，根本读不了。
    //
    // 值**在重画前从 DOM 上现读**（`captureExpanded`），不靠 `toggle` 事件 —— 理由写在
    // 那个函数上：`toggle` 是异步派发的，点开之后紧接着的那一帧根本等不到它。
    //
    // 换一次运行要清空（见 `applyRun`）：键是「轮次号 + 分片」，两次运行里会重名，
    // 留着它就成了「这一次的运行号 + 上一次的展开状态」。
    var expandedModelRounds = {};

    // 运行「还没结束」的那些状态（服务端 `AiAnalysisRun.status` 的取值）。只有它们才谈得上
    // 「才刚发起、第一帧还没出来」；终态（成功 / 失败 / 中断）不该再等等看 —— 都跑完了
    // 还读不到，就是读不到。
    var LIVE_STATUS = {pending: true, running: true};

    // 「才刚发起」这个说法**只在一段时间内成立**。过了就改口：分析派给别的进程执行时
    // （多节点部署），本进程永远不会有快照 —— 一直说「正在准备」是把一件不会发生的事
    // 说成马上就要发生。两分钟足够覆盖单机部署里装项目包、取变更集那一段。
    var STARTING_WINDOW_MS = 120000;

    function now() {
        return global.Date && global.Date.now ? global.Date.now() : 0;
    }

    /**
     * 现在能不能说「才刚发起，第一帧还没出来」。三个条件缺一不可：
     *
     *   * **本页看着它开跑**（`watchingSince` 有值）—— 刷新页面时它已经在跑的话，
     *     本页不知道它刚发起还是已经跑了十分钟，那就没有资格说这句话；
     *   * **从没见过快照** —— 见过又丢了是「读不到最新的」，不是「还没出来」；
     *   * **状态还是活的、且没过期限** —— 见 `STARTING_WINDOW_MS`。
     *
     * `status` 由调用方从同一帧的载荷里带进来（`data.status`）。**缺了它一律不算
     * starting**：拿不到状态就不能断言它还在跑，宁可说读不到（那是个更弱的说法）。
     */
    function startingNow(status) {
        if (sawSnapshot) return false;
        if (watchingSince === null) return false;
        if (!LIVE_STATUS[status]) return false;
        return now() - watchingSince < STARTING_WINDOW_MS;
    }

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
                        kind: (item && item.kind) || '',
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

    function noteFor(current, count) {        if (current === 'live') return count ? NOTE.live : NOTE.running_no_rounds;
        if (current === 'starting') return NOTE.starting;
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

    /** 在一棵子树里按 class 找第一个节点。走 `.children`，真 DOM 与假 DOM 都有这个。 */
    function findIn(node, cls) {
        var kids = node.children || [];
        for (var i = 0; i < kids.length; i++) {
            var child = kids[i];
            if (!child) continue;
            if (child.className === cls) return child;
            var hit = findIn(child, cls);
            if (hit) return hit;
        }
        return null;
    }

    /**
     * 现在 DOM 上哪几轮的「模型这一轮返回的内容」是展开的（轮次 key → true）。
     *
     * ## 为什么是**现读 DOM**，而不是监听 `toggle`
     *
     * 第一版监听的是 `details` 的 `toggle` 事件，靠它维护一份状态。**在真浏览器里不工作**：
     * Chrome 把 `<details>` 的 `toggle` 派发成一个**异步任务**（排队等当前任务跑完），
     * 而重画是同步发生的 —— 用户点开之后紧接着来的那一帧进度里，这个事件还没跑，
     * 读到的状态是空的，于是新画出来的还是收起的。更糟的是那个事件随后会在**已经被摘掉的
     * 旧节点**上跑，把过期的值写回状态里。
     *
     * 而 DOM 上的 `open` 是**同步**更新的：点开的那一刻就已经是 true。重画前现读一遍，
     * 拿到的永远是最新的那一个。
     *
     * 这个差别假 DOM 测不出来（stub 里的 `toggle` 是我自己同步抛的），是真浏览器复核
     * 才暴露的 —— 所以这一条也有真渲染的复核脚本，见提交说明。
     */
    function captureExpanded(log) {
        var open = {};
        var cards = log.children || [];
        for (var i = 0; i < cards.length; i++) {
            var card = cards[i];
            if (!card || typeof card.getAttribute !== 'function') continue;
            var key = card.getAttribute('data-round-key');
            if (!key) continue;
            var box = findIn(card, 'ai-think-model');
            if (box && box.open) open[key] = true;
        }
        return open;
    }

    function paint() {
        var log = el('aiThinkLog');
        var note = el('aiThinkNote');
        // 顶上那句说明每次重画都跟着当前 `mode` 更新：跑完那一刻（`unwatch`）**必须**把
        // 「分析进行中」换掉，否则列表已经六轮了、那句话还挂着，两句话不能同时为真。
        if (note) note.textContent = noteFor(mode, blocks.length);
        if (!log) return;
        // 清空之前先把「哪几轮是展开的」记下来（见 `captureExpanded`：必须现读 DOM，
        // `toggle` 事件是异步的，等不到）。下一帧重画时按这份状态恢复。
        expandedModelRounds = captureExpanded(log);
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
                // **「未执行」是这句话里唯一有信息量的部分，而它对 `unclassified` 是错的。**
                // 平台把「模型给了不在本次维度清单里的 category」的条目**保留下来**并归进
                // 「未归类」（`protocol._coerce_anomalies`），它的记账是
                // 「未丢弃，已归入「未归类」」—— 前缀写成「未执行」之后，那一行读作
                // 「未执行：performance（未丢弃，已归入「未归类」）」，一句自相矛盾的话：
                // 用户想查「为什么这次只报了两条」时，看到的是「请求没跑」。
                // 这一条的 `reason` 本来就自带否定，所以按 `kind` 分开说。
                if (item.kind === 'unclassified') {
                    addLine(card, 'ai-think-note-line',
                             '未归类（内容保留，只是没归到维度上）：'
                             + item.detail + (item.reason ? '（' + item.reason + '）' : ''));
                    return;
                }
                addLine(card, 'ai-think-dropped',
                         '未执行：' + item.detail + (item.reason ? '（' + item.reason + '）' : ''));
            });
            if (block.budgetNotes) addLine(card, 'ai-think-note-line', block.budgetNotes);
            if (block.hint) addLine(card, 'ai-think-note-line', block.hint);
            if (block.error) addLine(card, 'ai-think-missing', block.error);

            if (block.modelText) {
                var details = doc.createElement('details');
                details.className = 'ai-think-model';
                // 按上一帧末尾读下来的状态恢复展开态。**不挂 `toggle` 监听**：那个事件
                // 是异步派发的（见 `captureExpanded`），既赶不上这一帧，又会在节点被摘掉
                // 之后把过期的值写回来。用户之后的开合由下一次重画前的现读负责。
                details.open = !!expandedModelRounds[block.key];
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
        // 记下「本页看着它开跑」的时刻 —— `starting` 那句说明的期限从这一刻起算。
        watchingSince = now();
        // **这一次是新的一次运行**：上一条命的「已结束」不许留着，否则跑动中取回的
        // 空明细会被当成「确实没有记录」（见 `applyRounds`）。
        runSettled = false;
        lastRunStatus = null;
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
        if (opts.settled) runSettled = true;
        if (mode === 'live') {
            mode = 'settled';
        } else if (mode === 'starting') {
            // **一句有时限的话不许挂在这儿过期。** 「才刚发起」的判据里第一条就是
            // 「本页看着它开跑」（`startingNow`），而这一句之后本页不再看着它了 ——
            // 那句话从此不再成立，可它印在面板上、且此后**没有任何东西会重画**
            // （120 秒的期限只在 `paint()` 里判），于是它会一直挂着：徽章已经写「完成」、
            // 报告已经在「完整结论」里，同一个抽屉的「思考过程」还写着「还在准备，
            // 每跑完一轮这里会多一条」。降级成 `unavailable`——那是同一处境的**更弱**
            // 说法，也是这一刻唯一还成立的那句。
            mode = 'unavailable';
        }
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

    /**
     * 跑动中的一帧（`progress.rounds`）。**整份替换**，不做增量 —— 每帧本来就是全量。
     *
     * `status` 是**同一次运行**的状态（`data.status`，取值 pending / running / 终态）。
     * 它只用来分辨一个处境：载荷里没有进度时，是「才刚发起、第一帧还没出来」还是
     * 「读不到」（见 `startingNow` 与文件头那段）。**调用方必须把它带进来** ——
     * 缺了它一律按「读不到」处理，行为与从前逐字一致。
     */
    function applyProgress(progress, status) {
        // **状态先记下来**（不论这一帧有没有进度）：`applyRounds` 要用它分辨
        // 「取回来是空的」是「还没落库」还是「确实没有记录」。
        if (status) lastRunStatus = status;
        if (!progress) {
            // 这一帧的载荷里没有进度。三个处境，**各自的实话不同**：
            //
            //  * 才刚发起（运行是活的、本页看着它开跑、还没见过快照）→ 「正在准备」，
            //    不带任何诊断；
            //  * 手上已经画着几轮 → 「读不到最新的」；
            //  * 其余（跑在别的进程、快照过期、刷新页面时它已经在跑）→ 「读不到」。
            //
            // **已经画出来的那几轮不清**：它们是这一次运行真实跑过的轮次（`setRun` 换了
            // 运行号才会清），一次读不到就抹掉等于把看到的证据收回去；而下一帧往往就
            // 恢复正常了。清掉的话用户看到的是过程**闪一下没了**。
            watching = false;
            mode = startingNow(status) ? 'starting' : 'unavailable';
            paint();
            return;
        }
        // 拿到真进度 = 本页确实连在这一次运行上。**在这里重新认一次**，而不是只靠
        // 开跑那一刻的 `watch()`：中间可能夹着一帧读不到的载荷（快照过期、跑在别的
        // worker 里），那一帧会把 `watching` 打掉，而后面几帧又恢复正常 —— 不重新认，
        // 面板就会在「读不到」与「在跑」之间来回跳，且 `ensureLoaded` 会在跑动中去取
        // 落库的明细，把实时的过程换成一份「分析已结束」。
        watching = true;
        sawSnapshot = true;
        mode = 'live';
        blocks = buildRoundBlocks(progress.rounds, { max_rounds: progress.max_rounds });
        paint();
    }

    /** 落库的逐轮（`/runs/<id>/usage` 的 `rounds`）。 */
    function applyRounds(rounds, meta) {
        var list = rounds || [];
        // 逐轮是**跑完之后**才落库的（服务端 `_persist_outcome`）。所以「取回来是空的」
        // 有两种意思，必须分开：
        //
        //   * **这次运行还在跑** → 只是还没落库，不是「没有留下记录」。这时既不能说
        //     「分析已结束」，也不能把 `loaded` 认下来 —— 认下来这份记录就**再也刷不
        //     出来**：跑完那一刻的 `unwatch({settled: true})` 被 `!loaded` 挡在门外，
        //     用户再点「思考过程」又被同一个标志挡住，`/latest` 那句 `setRun` 还会
        //     因为运行号没变而早退。面板于是**永远**写着「这次运行没有留下逐轮记录」，
        //     而明细一直在库里。触发序列很日常：跑起来 → 点一次「思考过程」→ 等它跑完
        //     → 再看。这正是 `unwatch` 的 docstring 要防的那件事，只是那一扇门
        //     （`onShowThink`）当时没关。
        //   * **已经结束** → 那才是真话：这次运行没留下记录（功能上线前的分析）。
        var pending = !list.length && !runSettled && !!LIVE_STATUS[lastRunStatus];
        loaded = !pending;
        loading = false;
        mode = pending ? 'live' : 'settled';
        blocks = buildRoundBlocks(list, meta || {});
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
        // 换了运行，「见过快照」与「看着它开跑的时刻」都属于上一次 —— 留着它们会让
        // 新的一次运行继承上一次的处境（`starting` 那句说明的期限就是从这里算的）。
        sawSnapshot = false;
        // 展开状态同理：键是「轮次号 + 分片」，两次运行里必然重名，留着就是拿上一次的
        // 展开状态去开这一次的某一轮。
        expandedModelRounds = {};
        watchingSince = null;
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
        // **先说「本页没有看着它开跑」。** 调用方是在「刷新页面 / 点刷新结果时发现它
        // 已经在跑」的处境下来的（模板 `loadWeeklyAiLatest` 的「进行中」那一支），而那一支
        // 紧跟的 `startAiBudgetWatch` 会调 `watch()` 把 `watchingSince` 记成**现在** ——
        // 于是 `startingNow` 的三个条件全被满足，一个已经跑了十分钟的分析被说成
        // 「分析已发起，还在准备：第一帧逐轮进度还没出来」，用户会以为自己的点击没生效
        // 或分析刚重启，很可能再点一次「重新分析」（多花一次钱）。
        //
        // 撤掉那个时刻，「才刚发起」就再也说不出口（它三个条件里第一条就是
        // 「本页看着它开跑」）。**这不影响「本页正连着它」**：`watching` 不动。
        watchingSince = null;
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
        // 不再看着它跑，就不能再说「分析进行中」——那两句话不能同时为真。同理，
        // 「才刚发起」也是「本页看着它开跑」才成立的说法。
        if (mode === 'live') mode = 'settled';
        watchingSince = null;
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
