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
        context_overflow: '上游拒绝了完整的提示词，平台收缩后重新问了一次。'
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
            lines.push('\n⚠️ 本次分析未完整跑完：' + label + (MEANING[reason] || ''));
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
        return lines.join('\n');
    }

    global.AiContextNotice = {
        contextNotice: contextNotice,
        MEANING: MEANING
    };
})(window);
