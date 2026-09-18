/* AI 分析抽屉与配置弹窗里的**预算提示**：超预算时说什么、去哪儿调。
 *
 * ---------------------------------------------------------------------------
 * 为什么是一个共享文件、而不是抄进三份模板
 * ---------------------------------------------------------------------------
 * 这段要在 commit_diff_new / weekly_version_diff / merged_project_view 三份模板里都出现，
 * 而那三份的 AI 抽屉块是**逐字复制**的（仓库既有事实，见 tests/test_ai_drawer_css_sync.py）。
 * 直接抄三遍的代价不是重复，而是**漏改**：预算从一档变成两档之后，只改一处的症状是
 * 「某个页面指着项目配置让你去调一个只存在于平台页面的上限」—— 用户按那句话去找，
 * 找不到，然后以为是个 bug。
 *
 * 所以与 static/js/ai_usage_line.js 同一打法：**DOM 片段三份逐字相同 + 判定与文案集中
 * 在本文件**。本文件的函数由 tests/test_ai_budget_notice.py 用 node 真跑。
 *
 * ---------------------------------------------------------------------------
 * 三条口径（与 services/ai/analysis_budget.py 完全一致）
 * ---------------------------------------------------------------------------
 * 1. **超的是哪一档决定了去哪儿调。** 项目档在「项目概览页 → AI 分析配置」，平台档在
 *    「AI 消耗」页面 →「平台总预算」。两档都超时两处都要说 —— 只说一个的话，用户调完
 *    那处仍然跑不起来，而界面上看不出还差什么。
 * 2. **`null` 是「不限制」，不是 0。** 没配上限显示「不限制」；`over_scopes` 缺失
 *    （老数据）时按项目档说，不猜。
 * 3. **金额直接用后端给的字符串。** 后端已经是定点 2 位小数（services/ai/pricing.py
 *    的 money），前端再 `toFixed` 一次就会把 `<0.01` 这种写法弄坏。
 */
(function (global) {
    'use strict';

    var SCOPE_PROJECT = 'project';
    var SCOPE_PLATFORM = 'platform';

    var UNLIMITED = '不限制';
    var UNKNOWN = '未上报';

    // 「去哪儿调」的两句话。**唯一事实源**，三份模板与配置弹窗都从这里取。
    var PROJECT_HOWTO =
        '项目概览页 →「AI 分析配置」→ 预算周期 / 周期内 token 上限 / 周期内费用上限';
    var PLATFORM_HOWTO =
        '「AI 消耗」页面 →「平台总预算」→ 周期 / token 上限 / 费用上限';

    function scopesOf(budget) {
        if (!budget || !budget.over) return [];
        var scopes = budget.over_scopes;
        if (scopes && scopes.length) {
            return Array.prototype.slice.call(scopes);
        }
        // 没有 `over_scopes` 的旧响应：按项目档说。**不猜成两档都超** —— 那会让用户
        // 去一个与本次超支无关的页面。
        return [SCOPE_PROJECT];
    }

    /** 超了没有。 */
    function isOver(budget) {
        return !!(budget && budget.over);
    }

    /** 「调整方式：…」。没超时返回空串（调用方据此不显示这一段）。 */
    function guidance(budget) {
        var scopes = scopesOf(budget);
        if (!scopes.length) return '';
        var platform = scopes.indexOf(SCOPE_PLATFORM) >= 0;
        var project = scopes.indexOf(SCOPE_PROJECT) >= 0;
        var parts = [];
        if (platform) parts.push(PLATFORM_HOWTO);
        if (project) parts.push(PROJECT_HOWTO);
        return '调整方式：' + parts.join('；');
    }

    /** 一句话说清「超的是哪一档」。 */
    function overTitle(budget) {
        var scopes = scopesOf(budget);
        if (scopes.length > 1) return '已超出平台与项目预算';
        if (scopes.indexOf(SCOPE_PLATFORM) >= 0) return '已超出平台总预算';
        if (scopes.length) return '已超出项目预算';
        return '';
    }

    /** 金额/上限 → 文本。`null` 是「不限制」，空串当没配。 */
    function moneyText(value) {
        if (value === null || value === undefined || value === '') return null;
        return String(value);
    }

    function limitText(value) {
        var text = moneyText(value);
        return text === null ? UNLIMITED : text;
    }

    /**
     * 一档预算的一行摘要：`本月 12.3k / 100.0k`。
     *
     * `used` 里哪一部分是 `null` 就写「未上报」—— **不许当 0**：那两件事对用户的含义
     * 正好相反（一个是「没花」，一个是「不知道花了多少」）。整档都没上报时返回 null，
     * 调用方据此不显示这一行。
     */
    function scopeLine(scope, fmtTokens) {
        if (!scope || !scope.limited) return null;
        var fmt = fmtTokens || function (value) { return value === null || value === undefined ? null : String(value); };
        var limits = scope.limits || {};
        var used = scope.used || {};
        var pieces = [];

        if (limits.tokens !== null && limits.tokens !== undefined) {
            var usedTokens = fmt(used.tokens);
            pieces.push((usedTokens === null ? UNKNOWN : usedTokens) + ' / ' + fmt(limits.tokens));
        }
        var limitCost = moneyText(limits.cost);
        if (limitCost !== null) {
            var symbol = currencySymbol(limits.currency);
            var usedCost = moneyText(used.cost);
            pieces.push(
                symbol + (usedCost === null ? UNKNOWN : usedCost) +
                ' / ' + symbol + limitCost
            );
        }
        if (!pieces.length) return null;
        var label = scope.period_label ? scope.period_label + ' ' : '';
        return label + pieces.join(' · ');
    }

    function currencySymbol(currency) {
        var text = String(currency || '');
        if (text === 'CNY') return '¥';
        if (text === 'USD') return '$';
        if (text === 'EUR') return '€';
        return text ? text + ' ' : '';
    }

    /**
     * 抽屉里那行 meta 的完整文案。
     *
     * 超了 → `原因 + 调整方式`；没超但配了上限 → 两档的用量摘要；完全没配 → 空串
     * （调用方保持它原来的文案，不要用「未配置预算」这种话去占位）。
     */
    function metaText(budget, fmtTokens) {
        if (!budget) return '';
        if (isOver(budget)) {
            var reason = String(budget.reason || '').trim() || '已超出预算，AI 分析已暂停。';
            var howto = guidance(budget);
            return howto ? reason + ' ' + howto + '。' : reason;
        }
        var lines = [];
        var projectLine = scopeLine(budget, fmtTokens);
        if (projectLine) lines.push(projectLine);
        var platformLine = scopeLine(budget.platform, fmtTokens);
        if (platformLine) lines.push('平台 ' + platformLine);
        return lines.join(' · ');
    }

    /**
     * 渲染超预算横幅。`budget` 没超时**清空并隐藏**（而不是留着上一次的内容）。
     *
     * 返回 1 表示显示了横幅、0 表示隐藏了 —— 调用方能用它做断言，也省得再查 DOM。
     */
    function renderBanner(host, budget) {
        if (!host) return 0;
        if (!isOver(budget)) {
            host.hidden = true;
            host.textContent = '';
            return 0;
        }
        host.hidden = false;
        host.innerHTML = '';
        host.className = 'ai-budget-banner';

        var title = document.createElement('strong');
        title.className = 'ai-budget-banner__title';
        title.textContent = overTitle(budget);
        host.appendChild(title);

        var reason = document.createElement('p');
        reason.className = 'ai-budget-banner__reason';
        reason.textContent = String(budget.reason || '').trim() || '已超出预算。';
        host.appendChild(reason);

        var howto = guidance(budget);
        if (howto) {
            var hint = document.createElement('p');
            hint.className = 'ai-budget-banner__howto';
            hint.textContent = howto + '。';
            host.appendChild(hint);
        }
        return 1;
    }

    global.AiBudgetNotice = {
        PLATFORM_HOWTO: PLATFORM_HOWTO,
        PROJECT_HOWTO: PROJECT_HOWTO,
        UNLIMITED: UNLIMITED,
        UNKNOWN: UNKNOWN,
        currencySymbol: currencySymbol,
        guidance: guidance,
        isOver: isOver,
        limitText: limitText,
        metaText: metaText,
        overTitle: overTitle,
        renderBanner: renderBanner,
        scopeLine: scopeLine,
        scopesOf: scopesOf
    };
})(typeof window !== 'undefined' ? window : globalThis);
