/**
 * 工作表标签的顺序与默认选中 —— **全平台唯一一份**。
 *
 * ## 为什么要有这个文件
 *
 * 「哪些表排前面、默认打开哪一张」这件事，历史上在四个地方各写了一遍：
 * `commit_diff.html` 的 `displayExcelDiff`、`merge_diff.html` 的
 * `generateExcelTabsForContainer` 与 `generateMergedExcelTabs`、
 * `weekly_version_full_diff.html` 的 `generateWeeklyExcelTabs`。前两份是**逐字复制**
 * 的，另外两份各自演化 —— 其中一份还多出一个变量声明后从未赋值的死分支。收成一份
 * 之后，「顺序错了」只可能错在一个地方。
 *
 * ## 钉住的两条口径
 *
 * ### 1. 有变更的表在前，**组内保持工作簿原始顺序**（`originalIndex`）
 *
 * 旧实现在「有变更」这一组里排的是：
 *
 *     if (a.name === 'Sheet1') return -1;
 *     if (b.name === 'Sheet1') return 1;
 *     return a.name.localeCompare(b.name);
 *
 * 表名恰好叫 `Sheet1` 的那个工作簿里，它看起来是对的 —— 而那只对**一个**项目成立。
 * 别的项目（表名形如 `0_常规属性`、`道具表`、`Sheet2`……）这一组就退化成**按表名
 * 字母序重排**：工作簿原序被打乱，评审者点开的标签顺序与实际表序对不上。
 *
 * 为什么不能打乱：AI 侧 `lines` 参数的语义是「**第几张表**」，取值来自
 * `services/ai/platform_provider.py` 里 `book.sheetnames` 的**工作簿顺序**。
 * 页面标签一旦按别的规则重排，模型说的「第 2 张表」和评审者点开的第 2 个标签
 * 就是两张不同的表 —— 这个错位不抛任何异常、不打任何日志，只是让人机对不上，
 * 于是「模型说的现象我在那张表里找不到」变成了一个没人说得清的悬案。
 *
 * 组内排序显式用 `originalIndex`，不依赖 `Array.prototype.filter` 恰好稳定：
 * 「稳定」是实现的默认而不是契约，写出来才有人守得住。
 *
 * ### 2. 默认打开「第一张有变更的表」，一张都没有时打开第一张
 *
 * 判定只用 `hasChanges`（由调用方用 `sheetHasChanges` 算好传进来），**不按表名筛**。
 * 旧代码里那句 `!sheet.name.includes('测试配置')` 是某个项目的私事：换一个项目，
 * 这个中文子串根本不出现，过滤就退化成**一个恒为空操作** —— 看起来在起作用、
 * 实际不起作用的兜底，比没有更糟（出问题时人会先去怀疑它，然后发现它从来没生效过）。
 * 平台目前**没有**「跳过某类表」的配置位，所以这里就是**没有**过滤；真要跳过某类表，
 * 应该加一个仓库级配置传进来，不要在 JS 里写死中文子串。
 *
 * 顺带修掉一个真实缺陷：旧的提交页在「所有有变更的表都被过滤掉」时，
 * `defaultActiveSheetName` 会**保持 undefined** —— 标签渲染出来一个都不高亮，
 * 正文区也没有一张表是激活的，页面上看起来「什么都变了但什么都看不到」。
 *
 * ### 3. 「某类表不给点」仍然可以按项目传入，但**判定不在这个模块里**
 *
 * 提交页与合并页各有一条产品规则：表名含某个中文子串的表**不给点**（那个项目的
 * 「召唤物」表），并单独打一个 `excel-tab-summon-disabled` 标记。这是**用户能做
 * 什么**的约束，不是排序口径，所以它**留在了调用点**（两处各一个
 * `isSheetNotClickable`），只把「结果」作为 `analyzeSheets` 的一个可选判定参数传进来。
 *
 * 本模块**不内置任何中文子串**：不传判定 = 都可点击。这样「排序与可点击性」的机制
 * 只有一份，而那个项目特有的字面量留在它该在的地方 —— 换项目时要么删掉那两处判定，
 * 要么（正确的做法）改成按项目/仓库配置传入。
 *
 * ## 依赖与加载顺序
 *
 * 这是一个 IIFE，只把 `window.ExcelSheetOrder` 一个名字挂出去（不污染页面作用域）。
 * 页面自己的内联脚本要用它，所以**必须在内联脚本之前加载** —— 与
 * `static/js/excel_diff_table.js` 同一条规矩。
 */
(function () {
    'use strict';

    /**
     * 把「表名 → 表数据」摊成一张分析表。
     *
     * @param {string[]} sheetNames 工作簿顺序的表名（`Object.keys(diffData.sheets)`）
     * @param {Object} sheets 表名 → 表数据
     * @param {function} hasChanges 单表「有没有变更」判定（页面自己的 `sheetHasChanges`）
     * @param {function} [isNotClickable] 单表「不给点」判定；**不传就是都可点击**。
     *        某个项目特有的那条规则由调用点自己定义并传进来（见文件头第 3 节）。
     * @returns {Array<{name: string, originalIndex: number, hasChanges: boolean,
     *          notClickable: boolean}>} **按工作簿顺序**排列
     */
    function analyzeSheets(sheetNames, sheets, hasChanges, isNotClickable) {
        var analysis = [];
        for (var i = 0; i < sheetNames.length; i++) {
            var name = sheetNames[i];
            analysis.push({
                name: name,
                // 工作簿里的原始下标。组内排序用它 —— 这是「原序」唯一的事实源，
                // 不要另起一套（例如再按表名规则排一次）。
                originalIndex: i,
                hasChanges: !!hasChanges(sheets[name]),
                // 没传判定时一律 false：模块自己不认识任何表名规则。
                notClickable: isNotClickable ? !!isNotClickable(name, sheets[name]) : false
            });
        }
        return analysis;
    }

    /**
     * 有变更的在前、无变更的在后，**各自保持工作簿原始顺序**。
     *
     * @param {Array} analysis `analyzeSheets` 的结果
     * @returns {Array} 新的数组（不改动入参）
     */
    function orderSheets(analysis) {
        var changed = [];
        var unchanged = [];
        for (var i = 0; i < analysis.length; i++) {
            (analysis[i].hasChanges ? changed : unchanged).push(analysis[i]);
        }
        var byOriginalIndex = function (a, b) { return a.originalIndex - b.originalIndex; };
        changed.sort(byOriginalIndex);
        unchanged.sort(byOriginalIndex);
        return changed.concat(unchanged);
    }

    /**
     * 默认打开哪一张：第一张有变更的表；一张都没有时第一张。
     *
     * @param {Array} ordered `orderSheets` 的结果
     * @returns {string|null} 表名；一张表都没有时 `null`（调用方要能接住 null）
     */
    function defaultSheetName(ordered) {
        for (var i = 0; i < ordered.length; i++) {
            if (ordered[i].hasChanges) {
                return ordered[i].name;
            }
        }
        return ordered.length ? ordered[0].name : null;
    }

    window.ExcelSheetOrder = {
        analyzeSheets: analyzeSheets,
        orderSheets: orderSheets,
        defaultSheetName: defaultSheetName
    };
})();
