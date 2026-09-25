/**
 * 分析结果里的「这次不是正常跑完的」提示。
 *
 * ## 为什么要有这个模块
 *
 * 引擎会降级：轮次用尽、上下文索取额度用尽、模型不吐 JSON、上游以「上下文超长」拒绝 ——
 * 每种降级都会写进结果的 `degradation` / `error_message`，而**界面以前一个都不显示**。
 * 于是「模型压根没答上来」与「模型看完说没问题」在界面上长得一模一样：一个风险等级、
 * 一份报告，用户看不出差别。而这两件事的处理方式完全相反。
 *
 * 上下文压缩同理：一次「看起来正常、其实是在被压过的提示词上作答」的分析，也必须说清楚，
 * 否则「这次结论怎么这么浅」永远查不出原因。
 *
 * 唯一事实源在这里：三份抽屉（commit / weekly / merged）都调它，不在模板里各写一份文案
 * （同一个理由见 ai_budget_notice.js）。
 */
(function (global) {
    'use strict';

    // 降级原因 → 一句「这意味着什么」。键与服务端 engine.DEGRADATION_LABELS 一一对应，
    // 但这里说的不是「发生了什么」（那句由服务端给），而是**用户该据此怎么读这份报告**。
    var MEANING = {
        rounds_exhausted: '模型没有在轮次用尽前收口，结论基于它当时手上的证据。',
        requests_exhausted: '上下文索取额度用完了，还有文件没看。',
        markdown_report: '模型没有按协议输出 JSON，平台把它的正文原样保留了下来。',
        protocol_corrections_exhausted: '连续多轮都没能解析出协议要求的 JSON。',
        context_overflow: '上游拒绝了完整的提示词，平台收缩后重新问了一次。',
        // 子代理模式（services/ai/subagent.py）。这一条的读法与上面几条不同：上面说的是
        // 「这块看过了但看得不够」，这一条说的是**「这块可能没人看过」**—— 它最容易被
        // 读成「这里没问题」，所以必须把读者直接送到报告末尾的那段缺口说明上。
        subagent_gap: '有些分片没有跑成，或者它报出的结论没有进入最终报告 ——'
            + '报告末尾的「信息缺口（平台补充）」里逐条列着，那几块**不能当成「没问题」**。',
        // 对账轮（找反证）。与上一条的分别：上一条是「有一块没人看过」，这一条是
        // **「结论没经过复核」**—— 报告是完整的，只是少了一道你特意打开的把关。
        subagent_verify: '本次开着「对账轮」，但它没有跑成 ——'
            + '报告里的结论**没有经过「找反证」这一道复核**，读的时候要当成未经复核的初稿。',
        // 单次 token 硬上限到了。与上面两条「额度用尽」的分别要读清楚：那两条是额度
        // （问了几次 / 跑了几轮），这一条是**钱** —— 用户看到「轮次用尽」会去调轮次上限，
        // 而这里该动的是预算，混成一句就等于把人引到错的那个旋钮上。
        token_budget_exhausted: '这次运行的单次 token 上限到了，平台在再发起一次模型调用'
            + '**之前**就停住了 —— 已经拿到的证据照常出结论，但**没查完的部分不会体现在'
            + '报告里**，不能当成「这里没问题」。'
    };

    function str(value) {
        return typeof value === 'string' ? value : '';
    }

    /** 最多列出几条「没轮到的」。再多就归到「等 N 条」—— 一屏列二十行没人看。 */
    var REFUSED_SHOWN = 5;

    /**
     * 「上下文索取额度用尽」那一段的**细节**：缺的是哪几块、占多少、该调什么。
     *
     * 只写「还有文件没看」是不够的：用户看完仍然不知道三件该知道的事 —— 缺的是哪几块
     * （于是判不了这份结论能不能用）、占多少（是漏了一个还是漏了一半）、该动哪个设置。
     * 这三件事服务端都已经给了（`context.request_budget`），在这里拼成人话。
     *
     * **比例的分母是「模型一共索取了多少次」，不是配置里的上限。** 子代理模式下
     * `used` 是全家合计，而配置里的上限是**每个成员**的（每个成员各拿一整份，见
     * `subagent.MEMBER_BUDGET_PERCENT`）—— 拿它做分母会算出一个大于 100% 的数。
     */
    function budgetDetail(budget) {
        if (!budget || typeof budget !== 'object') { return ''; }
        var refused = Number(budget.refused);
        if (!isFinite(refused) || refused <= 0) { return ''; }
        var used = Number(budget.used);
        if (!isFinite(used) || used < 0) { used = 0; }

        var total = used + refused;
        var share = total > 0 ? Math.round((refused * 100) / total) : 0;
        var parts = [
            '模型这次一共索取 ' + num(total) + ' 次上下文，其中 ' + num(refused)
                + ' 次（' + share + '%）因为额度用尽没有执行。'
        ];

        var items = budget.refused_items;
        if (Object.prototype.toString.call(items) === '[object Array]' && items.length) {
            var shown = items.slice(0, REFUSED_SHOWN).filter(function (item) {
                return str(item).trim() !== '';
            });
            if (shown.length) {
                parts.push(
                    '没轮到的包括：' + shown.map(function (item) { return str(item); }).join('、')
                    + (refused > shown.length ? ' 等 ' + num(refused) + ' 条' : '')
                    + '。'
                );
            }
        }

        // **调哪个参数要写出来。** 这一句是整段话里唯一可操作的部分，而它原先完全缺席：
        // 用户读完只知道「平台额度不够」，不知道去哪调。
        parts.push(
            '额度由项目的「AI 分析配置 → 上下文索取上限」决定，调大它就能多要一些；'
            + '开启子代理时这个数字是给**每个分片**的额度（与分片数无关），'
            + '所以全家合计会比它大，那一档也就更早看到。'
        );
        return parts.join('');
    }

    /** 数字 → 「12,345」。与页面上的 token 数字用同一套千分位。 */
    function num(value) {
        var n = Number(value);
        if (!isFinite(n) || n <= 0) { return ''; }
        return n.toLocaleString('en-US');
    }

    /**
     * 生成要追加到 AI 结论区的那几行。没有可说的就返回空串（**不要凭空产出一句
     * 「一切正常」**：那会让用户以为平台检查过）。
     */
    function contextNotice(payload) {
        if (!payload || typeof payload !== 'object') { return ''; }
        var lines = [];
        var label = str(payload.degradation_label);
        var reason = str(payload.degradation);

        if (label) {
            lines.push('⚠️ 本次分析未完整跑完：' + label + (MEANING[reason] || ''));
        }

        var context = payload.context || {};
        // 「额度用尽」那一句展开成「缺哪几块 / 占多少 / 调哪个参数」。**跟在降级那句
        // 后面单独成段**（下面用空行 join）：它是同一件事的细节，不该挤进同一段。
        if (reason === 'requests_exhausted') {
            var detail = budgetDetail(context.request_budget);
            if (detail) { lines.push(detail); }
        }
        var compaction = context.compaction || {};
        if (compaction.overflow_recovered) {
            lines.push(
                '上游曾以「上下文超长」拒绝这次请求，平台已收缩提示词后收尾 ——'
                + '结论是从被裁剪过的上下文里得出的，报告里的「信息缺口」要看仔细。'
            );
        }
        if (num(compaction.dropped_turns)) {
            lines.push(
                '为控制上下文体积，最早的 ' + num(compaction.dropped_turns) + ' 轮历史已被压成摘要'
                + '（省下约 ' + num(compaction.dropped_chars) + ' 字），最近几轮原文保留。'
            );
        }
        // 窗口那条说明由服务端下发（含「窗口是按默认值算的」这种必须说清的事）。
        var budgetNote = str(context.budget_note);
        if (budgetNote) {
            lines.push(budgetNote);
        }
        // **空行分隔，不是单换行**：这几句是**互不相干的三件事**（降级 / 压过历史 /
        // 窗口是按默认值算的），而报告渲染器把段落内的单换行当软换行、用空格接起来
        // （见 `ai-report-markdown.js` 的 `flushPara`）—— 用单换行的话它们会连成一大句，
        // 用户一眼扫过去就跳过了。而这一块的全部意义就是**别被跳过**。
        return lines.join('\n\n');
    }

    /**
     * 「本次覆盖与缺口」那一段（这次看了多少、缺的是什么）。
     *
     * ## 为什么它必须出现在屏幕上（2026-09-21）
     *
     * 审计要求（AI-P1-03）是「报告必须显示证据覆盖与缺口」，而**抽屉才是用户第一眼
     * 看到的地方**。导出那份 `.md` 早就有了（导出路由现算账本传给报告文档），可抽屉读的
     * 是落库的 `response_payload.report_markdown` —— 真机验证时那份里「本次覆盖与缺口」
     * 那一整段的关键词（覆盖那几行、缺口那几条）**一个都没有**。
     *
     * ## 为什么这段字是服务端给的、而不是在这里拼
     *
     * 行与缺口的措辞在 `services/ai/coverage_ledger.py`（导出文档读的是同一份）：
     * 同一份数据在两处显示时必须**逐字一致**，各拼一份迟早会说不到一起（这一行按文件
     * 去重、那一行按提交去重，两个「覆盖率」没人知道差在哪）。服务端已经拼好整段中文
     * （`result_payload.coverage_notice_text`），放在结论载荷的 `coverage_notice` 键上
     * —— 这里只负责把它贴到屏幕上。
     *
     * 它**不在报告正文里**是刻意的：正文会被别的环节当字符串判据用
     * （`family_ledger.reconcile_candidates` 核对候选编号与候选文件有没有被点名，
     * 而覆盖段里恰好列着**取数失败的文件路径**），写成独立一个键，那些判据连碰都碰不到。
     */
    function coverageNotice(payload) {
        if (!payload || typeof payload !== 'object') { return ''; }
        return str(payload.coverage_notice).trim();
    }

    /**
     * 把提示行贴到一份**已经落库的报告正文**后面（没有可说的就原样返回正文）。
     *
     * ## 为什么必须单独有这一条（2026-09-19）
     *
     * 上面那个 `contextNotice` 原先只有一个调用点：SSE 的 `result` 事件里，追加到**流式
     * 缓冲区**上。而三份抽屉在同一个事件里紧接着会去拉一次 `/latest`（`loadLatestResult`
     * / `loadWeeklyAiLatest` / `refreshWeeklyAiLatest`），那一条路调的是
     * `setAiReport(result.response_text)` —— 它**整体替换**缓冲区。于是：
     *
     * * 跑完的那一次：⚠️ 那几行刚画上去就被替换掉，用户根本来不及看见；
     * * 打开页面 / 刷新：正文是从 `response_text` 渲染的，提示行一个字都不出现。
     *
     * 而它不是可有可无的装饰：`degradation` 说的是「**这块可能没人看过**」，
     * `context.budget_note` 说的是「**这次是在被压过的提示词上作答的**」—— 少了它，
     * 「模型看完说没问题」与「模型压根没答上来」在界面上长得一模一样，而这两件事的
     * 处理方式完全相反（见本模块开头的 docstring）。覆盖段同理：少了它，「范围：全量」
     * 这一个词会被读成「整个版本都看过了」。
     *
     * 拼接用**空行**：报告渲染器把段落内的单换行当软换行接起来，单换行会让「正文的最后
     * 一句」与「⚠️ 第一句」粘成一段。
     *
     * **顺序**：覆盖段在前、告警在后 —— 与导出那份一致（读的人在正文之前先建立
     * 「这份报告看了多少」这个前提），而告警是紧接着要读的那一句。
     *
     * 所以口径改成：**提示是「这一次运行」的属性，不是某一条传输通道的属性** ——
     * 凡是把一份报告正文画到屏幕上的地方，都从这里取「正文 + 提示」的合体。
     * 服务端导出的那份 `.md` 早已自带（`services/ai/report_document.py`），这里补的是屏幕。
     */
    function withContextNotice(responseText, payload) {
        var body = str(responseText);
        var extra = [coverageNotice(payload), contextNotice(payload)];
        var notice = extra.filter(function (one) { return one; }).join('\n\n');
        if (!notice) { return body; }
        return body ? body + '\n\n' + notice : notice;
    }

    global.AiContextNotice = {
        contextNotice: contextNotice,
        coverageNotice: coverageNotice,
        withContextNotice: withContextNotice,
        MEANING: MEANING
    };
})(window);
