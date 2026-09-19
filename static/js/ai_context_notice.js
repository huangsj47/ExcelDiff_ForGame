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
            + '报告里的结论**没有经过「找反证」这一道复核**，读的时候要当成未经复核的初稿。'
    };

    function str(value) {
        return typeof value === 'string' ? value : '';
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
     * 处理方式完全相反（见本模块开头的 docstring）。
     *
     * 拼接用**空行**：报告渲染器把段落内的单换行当软换行接起来，单换行会让「正文的最后
     * 一句」与「⚠️ 第一句」粘成一段。
     *
     * 所以口径改成：**提示是「这一次运行」的属性，不是某一条传输通道的属性** ——
     * 凡是把一份报告正文画到屏幕上的地方，都从这里取「正文 + 提示」的合体。
     * 服务端导出的那份 `.md` 早已自带（`services/ai/report_document.py`），这里补的是屏幕。
     */
    function withContextNotice(responseText, payload) {
        var body = str(responseText);
        var notice = contextNotice(payload);
        if (!notice) { return body; }
        return body ? body + '\n\n' + notice : notice;
    }

    global.AiContextNotice = {
        contextNotice: contextNotice,
        withContextNotice: withContextNotice,
        MEANING: MEANING
    };
})(window);
