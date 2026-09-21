/* AI 分析抽屉的**状态行**：跑到哪了、结束了没有、连接断了算谁的。
 *
 * ---------------------------------------------------------------------------
 * 为什么要有这个文件
 * ---------------------------------------------------------------------------
 * 这一段（轮询进度 → 写 meta 那一行 → 结束时收尾）在 commit_diff_new /
 * weekly_version_diff / merged_project_view 三份模板里各有一份逐字复制的实现。
 * 线上反馈的那一幕就是这三份一起有的毛病：
 *
 *     失败                                             ← 结论区（SSE 的 result / error）
 *     分析中：第 2/8 轮 · 本次已用 92329 tokens（尚未落库…）    ← meta 行（轮询写的）
 *
 * 两句话不可能同时为真。根因不是文案，是**没人在结束时把那行字收掉**：
 * `closeWeeklyAiStream()` 停掉了定时器，却把最后一帧留在了 DOM 上。
 * 所以这里把「谁写那一行」和「谁负责擦」绑成同一个对象（`watchRun` 返回的 `stop`）。
 *
 * 顺带把另外两件一直混在一起的事分开：
 *
 *  1. **「连接断了」不是「分析失败」。** 分析跑在服务端的生成器里，浏览器这头断线
 *     不会让服务端停下来 —— 结论照常落库（可能就差几秒）。原来的实现在这种情况下
 *     直接打「失败」，而用户看到的那句「AI 分析失败或连接中断」正是这两个状态的合称。
 *  2. **服务端主动发的 `error` 事件与连接层断开，在 EventSource 里是同一个事件名**，
 *     只能靠 `event.data` 有没有来区分（有 = 服务端说的话，没有 = 连接层出的事）。
 *     这两条路的收尾方式完全不同，必须分开走（见 `errorOutcome` / `interruptOutcome`）。
 *
 * 本文件的函数由 tests/test_ai_drawer_stream_state.py 用 node 真跑。
 *
 * ---------------------------------------------------------------------------
 * 三条口径（与 services/ai/run_progress.py 的模块 docstring 同一套）
 * ---------------------------------------------------------------------------
 * 1. **读不到进度不是 0。** 多进程部署、跑在别的 worker 里时快照是 `null`，界面说
 *    「进度不可用」，**不显示「第 0 轮 / 已用 0 tokens」** —— 0 是一个结论（一次都没
 *    跑、一个 token 都没花），说反了比不说更糟。
 * 2. **用量是下界**（只含上游已上报的部分），所以那一行必须带上
 *    「尚未落库，只含上游已上报的部分」，不能写成一个像账本一样的数。
 * 3. **没跑完就不许说「完成」**：只有 `result` 事件（结论已落库）才换徽章，
 *    连接断了、状态读不到，都只能说「不知道」。
 *
 * ---------------------------------------------------------------------------
 * 那个数是**哪一个**量：job 级累计，不是当前分片的局部量
 * ---------------------------------------------------------------------------
 * 子代理模式下一家子有几个成员，**每个成员一个引擎实例**，各自从 0 开始报累计
 * （见 `services/ai/subagent.py`）。所以快照里有三个含义不同的量，谁也不许顶替谁：
 *
 *   * `jobTokens(progress)` —— **跨成员**的本次分析累计，单调不减（缺成员的按 0
 *     入账，是**已知下界**）。**顶部那一行读的是它**：左边已经写着当前是哪个分片
 *     （`agentText`），读数却只算那一个分片的话，换个成员就会从 493,531 掉回
 *     34,960 —— 那是报障的那一幕。
 *   * `memberTokens(progress)` —— **当前成员**的局部量（`live_tokens`）。它有用，
 *     但不能当总数用，所以那一行只把它放在「其中本分片 …」里。
 *   * 落库那一份（用量面板的「已完成历史聚合」）不在这里，也不该在这里。
 */
(function (global) {
    'use strict';

    // 进度那一行的后缀。**不许省**：它是「这个数不是账」的唯一说明（口径 2）。
    var LIVE_SUFFIX = '（尚未落库，只含上游已上报的部分）';

    // 同上，用在 **job 级**那一份后面：它还**不含正在跑的那次调用**（引擎是跑完一轮
    // 才报的）。这一层能说清「为什么这个数比真实花费小」的只有这一句。
    var JOB_PENDING_SUFFIX = '（尚未落库，只含上游已上报的部分；不含正在跑的这次调用）';

    // job 级那个数是**已知下界**时的说明：有成员整块没上报（上游一次都没给这个成员的
    // token 数）。**不含这半句会被读成「已经花完了这么多」** —— 缺口是「不知道」。
    var PARTIAL_NOTE = '（有分片没上报用量，这是已知下界）';

    // 读不到进度快照时说的话。**不写 0、不写「第 0 轮」**（口径 1）。
    var NO_PROGRESS = '分析中：进度不可用（看不到第几轮，不影响分析）';

    // 快照在、但**一轮都还没跑完**时说的话（引擎的 `on_start` 报的那一帧）。
    //
    // 它与上面那句必须分开：两句的含义**正相反**。「进度不可用」是「我们看不见它」，
    // 而这一句是「它正在正常干活，只是第一轮还没跑完」。原先两句共用一句 —— 于是
    // 一次正常分析刚起步时，界面显示的是「看不到第几轮」，用户以为卡住了。
    //
    // 说「正在调用模型」而不是「第 0 轮」：轮次是从 1 开始数的，0 不是进度。
    var FIRST_ROUND = '正在调用模型（第一轮还没跑完）';

    var POLL_INTERVAL_MS = 3000;

    // 「这次运行已经结束了」的三种状态。`effective_status` 只会给这四个值里的一个
    // （models/ai_analysis/analysis_run.py）：僵尸 running 会被它翻成 failed，
    // 所以「跑了一小时还没动静」在这里会自己收尾，不会永远转下去。
    //
    // **`degraded` 必须在里面。** 它回答的是「这次交付是什么形态」，而不是「模型读全了
    // 没有」：`degraded` = 有结论但浅（有报告正文、有结构化结论），与 `succeeded` 同等
    // 对待 —— 跑完了。漏掉它的后果是这一层**最严重的一种错**：`watchRun` 读不到终态，
    // 于是抽屉对降级运行**永远转圈**、`onFinished` 永不触发（落库的结论再也取不回来）。
    // 实测库里 13 条完成运行里 **12 条**是 degraded，也就是绝大多数运行都停在这一幕上。
    var TERMINAL = ['succeeded', 'degraded', 'failed'];
    var RUNNING = ['running', 'pending'];

    function isTerminal(status) {
        return TERMINAL.indexOf(String(status || '')) >= 0;
    }

    function isRunning(status) {
        return RUNNING.indexOf(String(status || '')) >= 0;
    }

    /**
     * 取一个「数」：`null` / `undefined` / 空串 / 不是数的东西一律**返回 `null`**。
     *
     * 一律 `null`（**不是 0**）是口径 1、2 的落点：调用方拿到 `null` 就什么都不说。
     */
    function _amount(value) {
        if (value === null || value === undefined || value === '') return null;
        var num = Number(value);
        return isFinite(num) ? num : null;
    }

    /** **当前成员**（这一份运行）已消耗的 token。读不到就是 `null`。 */
    function memberTokens(progress) {
        if (!progress) return null;
        return _amount(progress.live_tokens);
    }

    /**
     * **job 级**累计 token：跨成员、单调不减（`run_progress._JobLedger` 算的）。
     *
     * 快照里没有 `job_tokens` 这个键时退回成员局部量 —— 那是**老调用方 / 测试替身**
     * 的快照（这个键总是随 `to_dict()` 发出来，哪怕是 `null`），这时二者逐字同义，
     * 那一行也就与以前一字不差。
     */
    function jobTokens(progress) {
        if (!progress) return null;
        if (progress.job_tokens !== undefined) return _amount(progress.job_tokens);
        return memberTokens(progress);
    }

    /** 第几轮。快照在、但轮次读不出来（脏数据）时返回 `null` —— 由调用方说「不可用」。 */
    function roundText(progress) {
        if (!progress) return null;
        var round = Number(progress.round);
        if (!isFinite(round) || round <= 0) return null;
        var max = Number(progress.max_rounds);
        return '第 ' + round + (isFinite(max) && max > 0 ? '/' + max : '') + ' 轮';
    }

    /**
     * 子代理模式下**这一段是谁在跑**。没开子代理时返回空串（那一行就与以前一字不差）。
     *
     * 口径与 `services/ai/subagent.py` 一致：`agent` 是 `S1`/`S2`…（分片），
     * 汇总那一次的 `agent` 为空、靠 `agent_index === agent_total` 认出来。
     * 两个都要写「第几个/共几个」，因为**只看轮次会误读**：「第 1 轮」跑了两分钟，
     * 究竟是第一个分片刚起步，还是已经在汇总了，完全看不出来。
     */
    function agentText(progress) {
        if (!progress) return '';
        var total = Number(progress.agent_total);
        var index = Number(progress.agent_index);
        if (!isFinite(total) || total <= 1 || !isFinite(index) || index <= 0) return '';
        var name = typeof progress.agent === 'string' ? progress.agent : '';
        if (!name) {
            // `agent` 空 = 主代理自己那几轮。**只有确实是最后那一段才叫它「汇总」**：
            // `agent_index < total` 且没有名字，是数据自相矛盾，这时宁可只报位次。
            name = index >= total ? '汇总' : '主代理';
        }
        return '分片 ' + name + ' (' + index + '/' + total + ')';
    }

    /**
     * 轮询到的一帧 → meta 那一行。`null` 表示**这次什么都别写**。
     *
     * 到了终态就返回 `null`：那一行接下来由「落库的结论」接管（调用方去
     * `refreshLatest`），继续写「分析中」就是这次要修的那个矛盾本身。
     */
    function progressText(progress, status) {
        if (isTerminal(status)) return null;
        var round = roundText(progress);
        if (!round) {
            // **快照读不到**与**一轮还没跑完**是两件事（见 FIRST_ROUND 那段）。
            if (!progress) return NO_PROGRESS;
            var early = agentText(progress);
            return '分析中：' + (early ? early + ' · ' : '') + FIRST_ROUND;
        }
        var agent = agentText(progress);
        var where = agent ? agent + ' · ' + round : round;
        // **数字取 job 级那一份**（跨成员、单调不减），不是当前这个分片的局部量：
        // 左边已经写着「分片 S3 (3/4)」，右边再只算这一个分片的话，换个成员就回退。
        var tokens = jobTokens(progress);
        if (tokens === null) {
            // 用量没上报：只说轮次，**不补一个 0**（口径 1、2）。
            return '分析中：' + where;
        }
        if (progress.job_tokens === undefined) {
            // 老调用方 / 测试替身的快照里没有 job 那个键：这时它只有一个「本次运行」的
            // 局部量，文案与以前一字不差（那时这两个量本来就是一回事）。
            return '分析中：' + where + ' · 本次已用 ' + tokens + ' tokens' + LIVE_SUFFIX;
        }
        // 新的一路：说清这是**整次分析**的数（不是左边那个分片的），再把另外两个
        // 含义不同的量分开说 —— 当前分片的局部量、以及「这是个已知下界」。
        var member = memberTokens(progress);
        var extra = member !== null && member !== tokens ? '（其中本分片 ' + member + '）' : '';
        if (progress.job_tokens_partial === true) extra += PARTIAL_NOTE;
        return (
            '分析中：' + where + ' · 本次分析已用 ' + tokens + ' tokens' + extra
            + (progress.job_tokens_pending_call === true ? JOB_PENDING_SUFFIX : LIVE_SUFFIX)
        );
    }

    /**
     * `result` 事件（服务端说「这次跑完了，结论/失败都已落库」）。
     *
     * 返回 `{failed, reason, badge, tone, meta}`：`reason` 交给结论区写正文
     * （模板那边还要拼自己的前后缀），`meta` 是状态行的收尾文案 ——
     * **它一定不含「分析中」**，这是本文件最要紧的一条。
     */
    function resultOutcome(payload) {
        var data = payload || {};
        if (data.status === 'failed') {
            return {
                failed: true,
                reason: data.error_message || '原因未知',
                badge: '失败',
                tone: 'danger',
                meta: '本次分析失败，没有产出结论。'
            };
        }
        return {
            failed: false,
            reason: '',
            badge: '完成',
            tone: 'success',
            // 「已落库」是能说的：`result` 事件在 `_persist_outcome` 之后才发。
            meta: '分析完成，本次用量已落库。'
        };
    }

    /**
     * 服务端**主动发的** `error` 事件（连接还活着，原因就在事件里）。
     *
     * `started` = 界面有没有收到过 `run` 事件。服务端只在闸门放行、运行记录建好之后
     * 才发 `run`，而那时**一个请求都还没发出去** —— 所以「没见到 `run`」可以确定
     * 「这次分析没有发起、没有产生消耗」，徽章就不该是「失败」。
     */
    function errorOutcome(payload, started) {
        var message = (payload && payload.message) || '服务端没有给出原因。';
        if (!started) {
            return {
                badge: '未开始',
                tone: 'warning',
                output: message,
                meta: '分析没有发起，没有产生消耗。'
            };
        }
        return {
            badge: '失败',
            tone: 'danger',
            output: message,
            // **不写「分析中断」**：「分析中」是那句进度行的开头，用户（以及任何
            // 按字符串找「分析中」的人）会在一个已经结束的界面上搜到它。
            meta: '分析失败，没有产出结论。'
        };
    }

    /**
     * **连接层断了**（EventSource 的 `error` 事件没有 `data`）。
     *
     * `status` 是我们回头问服务端要到的这次运行的状态（读不到就是 `null`）。
     * 这一条的关键是：**除了「服务端自己说失败了」，都不许打「失败」**。
     *
     * 返回里的 `keepPolling` / `refresh` 是给调用方的动作：
     *   * `keepPolling`：继续轮询进度（那是另一个端点，不受这条连接影响）——
     *     分析还在跑，等它跑完由轮询去把结论取回来，用户不必重新发起
     *     （**重新发起会再花一次钱**）。
     *   * `refresh`：这次运行已经结束了，去取落库的那条结论。
     */
    function interruptOutcome(status, started) {
        if (!started) {
            return {
                badge: '中断',
                tone: 'warning',
                output: '与服务器的连接中断了，而且没有收到这次分析的运行号 —— '
                    + '无法确认它有没有跑起来。刷新页面看最近一次运行的状态。',
                meta: '连接中断：没有收到运行号。',
                keepPolling: false,
                refresh: false
            };
        }
        if (isRunning(status)) {
            return {
                badge: '分析中',
                tone: 'primary',
                output: '与服务器的连接中断了，但分析仍在服务端继续跑；'
                    + '跑完后结果会自动显示，不必重新发起（重新发起会再花一次钱）。',
                meta: '与服务器的连接中断，分析仍在服务端继续。',
                keepPolling: true,
                refresh: false
            };
        }
        if (isTerminal(status)) {
            // `degraded` 也走这一支（它跑完了、结论已落库），**不许**落到下面那句
            // 「读不到这次运行的状态」上 —— 那会让界面一直等一个已经结束的运行。
            return {
                badge: '中断',
                tone: 'warning',
                output: '与服务器的连接中断了，但这次分析已经在服务端跑完；正在读取落库的结果…',
                meta: '连接已中断，正在读取这次运行的结果。',
                keepPolling: false,
                refresh: true
            };
        }
        return {
            badge: '中断',
            tone: 'warning',
            output: '与服务器的连接中断了，暂时也读不到这次运行的状态；'
                + '仍在自动重试，恢复后会显示结果。',
            meta: '连接中断：读不到这次运行的状态。',
            keepPolling: true,
            refresh: false
        };
    }

    /** 问服务端「这次运行是什么状态」。读不到（404 / 网络断 / 没权限）返回 `null`。 */
    function fetchRunStatus(runId, fetchImpl) {
        var doFetch = fetchImpl || global.fetch;
        if (!runId || !doFetch) return Promise.resolve(null);
        return doFetch('/ai-analysis/runs/' + runId + '/progress', { cache: 'no-store' })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (!data || !data.success) return null;
                return data.status || null;
            })
            .catch(function () { return null; });
    }

    /**
     * 按运行号轮询进度，**并把「那一行字」的所有权一起接过来**。
     *
     * 这就是修那个矛盾的地方：`stop()` 一定把那行字擦掉。于是任何一条出口
     * （结论到了 / 失败 / 连接断了 / 用户关抽屉）之后，页面上都不可能再留着
     * 一句「分析中：第 2/8 轮」。要显示什么由调用方在 `stop()` 之后自己写。
     *
     * `options`：
     *   * `metaEl`：写那一行的元素（可以没有）；
     *   * `onBudget(budget)`：每一帧的预算判定（超没超是实时变的，见 analysis_budget.py）；
     *   * `onProgress(data)`：每一帧的整条状态（**包括终态那一帧**）。「思考过程」标签
     *     用它把这一轮的明细画出来（`data.progress.rounds`，见 static/js/ai_think_log.js）。
     *     拿不到进度时也会调（`data.progress` 为 null）—— 由调用方决定说什么，
     *     这一层不替它判断「读不到」该显示成什么；
     *   * `onFinished(status)`：轮询**自己发现**这次运行已经结束了（连接断了之后
     *     分析其实跑完了，走的就是这条路）；
     *   * `fetchImpl` / `setInterval` / `clearInterval`：注入用（测试里真跑）。
     */
    function watchRun(runId, options) {
        var opts = options || {};
        var metaEl = opts.metaEl || null;
        var doFetch = opts.fetchImpl || global.fetch;
        var setTimer = opts.setInterval || global.setInterval;
        var clearTimer = opts.clearInterval || global.clearInterval;
        var timer = null;
        var stopped = false;

        function stop() {
            stopped = true;
            if (timer !== null) {
                clearTimer(timer);
                timer = null;
            }
            // **这一句才是修复本身**：停表的时候那行字必须一起收掉。留着它的后果
            // 是页面上同时挂着「失败」与「分析中：第 2/8 轮」两句话。
            if (metaEl) metaEl.textContent = '';
        }

        function tick() {
            if (stopped) return Promise.resolve(null);
            return doFetch('/ai-analysis/runs/' + runId + '/progress', { cache: 'no-store' })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (stopped || !data || !data.success) return data || null;
                    if (opts.onBudget) opts.onBudget(data.budget);
                    // 明细先给出去，再判终态：终态那一帧里往往带着最后一轮的记录，
                    // 顺序反了它会成为**唯一漏掉**的那一轮。
                    if (opts.onProgress) opts.onProgress(data);
                    if (isTerminal(data.status)) {
                        // 这次运行结束了（结论或失败都已经落库）。停表，再交给调用方
                        // 去取那条落库的结果 —— 顺序不能反：先停表才能保证那行字
                        // 一定被擦掉，否则取结果的这段时间里它还挂在页面上。
                        stop();
                        if (opts.onFinished) opts.onFinished(data.status);
                        return data;
                    }
                    var text = progressText(data.progress, data.status);
                    if (text && metaEl) metaEl.textContent = text;
                    return data;
                })
                .catch(function () {
                    // 轮询失败不影响分析本身：下一次 tick 还会试。
                    return null;
                });
        }

        var first = tick();
        timer = setTimer(function () { tick(); }, POLL_INTERVAL_MS);
        // 第一帧也返回出去：调用方（与测试）要能知道「这一帧已经落地了」。
        return {stop: stop, first: first};
    }

    global.AiStreamStatus = {
        LIVE_SUFFIX: LIVE_SUFFIX,
        JOB_PENDING_SUFFIX: JOB_PENDING_SUFFIX,
        NO_PROGRESS: NO_PROGRESS,
        PARTIAL_NOTE: PARTIAL_NOTE,
        POLL_INTERVAL_MS: POLL_INTERVAL_MS,
        agentText: agentText,
        errorOutcome: errorOutcome,
        fetchRunStatus: fetchRunStatus,
        interruptOutcome: interruptOutcome,
        isRunning: isRunning,
        isTerminal: isTerminal,
        jobTokens: jobTokens,
        memberTokens: memberTokens,
        progressText: progressText,
        resultOutcome: resultOutcome,
        roundText: roundText,
        watchRun: watchRun
    };
})(typeof window !== 'undefined' ? window : globalThis);
