/**
 * Excel diff 表体渲染的**单一实现**。
 *
 * ## 为什么要有这个文件
 *
 * 「把一张工作表的 diff 渲染成表格」这件事，历史上在四份文件里各写了一遍：
 * 提交页（commit_diff.html）、周版本文件完整 diff 页（weekly_version_full_diff.html）、
 * 多版本合并页（merge_diff.html）与 static/js/diff-handlers.js。四份是**各自演化**的，
 * 于是同一个字面量在不同页面上长得不一样（有的表头是两行、有的只有一行；有的把 HTML
 * 转义做进了 formatCellValue 里，同一个值在变更行与新增行显示不同），而任何一处
 * 改动都要在四个地方各改一遍 —— 漏掉一处不会有任何报错，只是那个页面继续用旧写法。
 *
 * 这里把「表头 / 行 / 分段标题 / 工具栏 / 筛选与视图开关」收成一份。模板只保留自己的
 * 入口函数（sheetBodyHtml / showWeeklyExcelSheet / showMergedExcelSheet …），内部调
 * renderSheetTable，大表分批渲染的页面用 tableHeadHtml + tableBodyHtml。
 *
 * ## 两条不能破坏的契约（有测试钉着）
 *
 * 1. 单元格文本一律走 `window.formatCellValue(...)` —— 语义是「表里怎么写就怎么显示」，
 *    只把真正的空值（null / undefined / NaN 数字）显示为空，**不 trim、不把文本
 *    'null'/'nan' 折叠成空串**。本模块**不重新实现**它：那份口径由
 *    tests/test_excel_literal_fidelity.py 钉着（客户端四份拷贝必须逐字一致），
 *    在模块里再写一份就又变成第五份了。谁调它谁就继承那个口径。
 * 2. 转义只做一次：动态文本拼进 HTML 的那一处用 escapeHtml / escapeHtmlAttribute。
 *    历史上这里出过两个方向的 bug —— 转义两遍（页面上显示字面 `&lt;`）与一遍都没转
 *    （文件里写的 `&lt;` 被浏览器解码成 `<`），见 weekly_version_full_diff.html 里
 *    formatCellValue 附近的注释。本模块里 escapeHtml 只出现在拼 HTML 的那一行。
 *
 * 依赖：本文件必须**在页面自己的内联脚本之前**加载（页面先注册 window.formatCellValue，
 * 渲染时才会用到；但模块里的函数名不能被页面的同名函数顶掉，所以模块自己是一个
 * IIFE，只把 window.ExcelDiffTable 一个名字挂出去）。
 */
(function () {
    'use strict';

    // 行号列的表头文案
    var ROW_HEADER_LABEL = '行号';
    // 筛选输入的 debounce：每个按键都重建 DOM 会让大表在输入时卡住
    var SEARCH_DEBOUNCE_MS = 120;
    // 状态与事件绑定都挂在**容器元素**上（同一页可能同时有多张表：提交页逐表渲染、
    // 合并页每个提交一张表）。用元素属性而不是模块级 Map：元素被移除时状态跟着走，
    // 不会在模块里留下永远释放不掉的键。
    var STATE_KEY = '__excelDiffTableState';
    var BOUND_KEY = '__excelDiffTableBound';

    // ------------------------------------------------------------------ 转义

    // HTML 转义（文本与属性通用）。**只在把动态文本拼进 HTML 的那一处调用一次。**
    function escapeHtml(text) {
        if (text === null || text === undefined) {
            return '';
        }
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // 属性值（title= / value= / data-*）里的转义。语义与 escapeHtml 相同，
    // 分开命名是为了让调用点一眼看出「这里进的是属性」。
    function escapeHtmlAttribute(text) {
        return escapeHtml(text);
    }

    // 展示口径的唯一来源：页面自己的 window.formatCellValue。
    // 不在这里重写一遍（见文件头第 1 条契约），也不加任何兜底改写。
    function cellText(value) {
        return window.formatCellValue(value);
    }

    // ------------------------------------------------------------------ 工具

    // 0 → A，25 → Z，26 → AA（Excel 的列字母）
    function columnLetter(index) {
        var result = '';
        var current = index;
        while (current >= 0) {
            result = String.fromCharCode(65 + (current % 26)) + result;
            current = Math.floor(current / 26) - 1;
        }
        return result;
    }

    // 有单元格级变更的列名（顺序按首次出现）
    function getModifiedColumns(sheetData) {
        var modifiedColumns = [];
        var rows = (sheetData && sheetData.rows) || [];
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            if (row.status !== 'modified' || !row.cell_changes || !Array.isArray(row.cell_changes)) {
                continue;
            }
            for (var j = 0; j < row.cell_changes.length; j++) {
                var change = row.cell_changes[j];
                if (change.column && modifiedColumns.indexOf(change.column) === -1) {
                    modifiedColumns.push(change.column);
                }
            }
        }
        return modifiedColumns;
    }

    // 「这一列算不算有变更」的全集，给「只看变更列」用：
    //   1. 单元格级变更的列（getModifiedColumns）；
    //   2. 整行新增 / 整行删除的行里**有内容**的列 —— 这些行的内容铺在整行上，
    //      只按 (1) 判断的话，一张「只有整行新增」的表会被收成一列不剩。
    //      空单元格不算「有内容」：不然整行新增会把尾部那些空列一起拖进来，
    //      而「隐藏本页空列」又会把它们去掉，两个开关互相打架。
    function changedColumnNames(sheetData) {
        var names = getModifiedColumns(sheetData);
        var rows = (sheetData && sheetData.rows) || [];
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            if (row.status !== 'added' && row.status !== 'removed') {
                continue;
            }
            var data = row.data || {};
            for (var key in data) {
                if (!Object.prototype.hasOwnProperty.call(data, key)) continue;
                if (names.indexOf(key) !== -1) continue;
                if (cellText(data[key]) !== '') {
                    names.push(key);
                }
            }
        }
        return names;
    }

    // 单元格在这一行里的**原始取值**（不是渲染后的 HTML），筛选就比它
    function rowCellText(row, header) {
        return cellText(row && row.data ? row.data[header] : undefined);
    }

    // 修改行里某一格的 old/new（数组与对象两种载荷形态都认）
    function cellChangeFor(row, header) {
        var changes = row && row.cell_changes;
        if (!changes) return null;
        if (Array.isArray(changes)) {
            for (var i = 0; i < changes.length; i++) {
                if (changes[i] && changes[i].column === header) return changes[i];
            }
            return null;
        }
        return changes[header] || null;
    }

    // 改前/改后值 → {column: {old_value, new_value}}，便于按列查
    function modifiedCellsMap(row) {
        var map = {};
        var changes = row && row.cell_changes;
        if (!changes) return map;
        if (Array.isArray(changes)) {
            for (var i = 0; i < changes.length; i++) {
                var change = changes[i];
                if (!change) continue;
                map[change.column] = {old_value: change.old_value, new_value: change.new_value};
            }
            return map;
        }
        for (var key in changes) {
            if (!Object.prototype.hasOwnProperty.call(changes, key)) continue;
            map[key] = {old_value: changes[key].old_value, new_value: changes[key].new_value};
        }
        return map;
    }

    // ------------------------------------------------- 分段：删除 / 新增 / 变更

    // 变更行的分组顺序与标题。
    //
    // 表体不把所有变更按行号混在一起：**整行删除、整行新增各自成段**（评审时先看
    // 「整行没了 / 整行冒出来」），逐格修改排在最后；段内仍按 Excel 行号升序。
    // 每段前插一条横跨「行号 + 全部列」的标题行 —— 线上对照的旧面板就是这个形态。
    // 只改渲染顺序与分段标题，**不动载荷**。
    var DIFF_ROW_GROUPS = [
        {status: 'removed', label: '删除行数据'},
        {status: 'added', label: '新增行数据'},
        {status: 'modified', label: '变更行数据'}
    ];

    // 按分组顺序重排：产出 [{label, rows}, …]，空分组不出现。
    // 三段之外的状态（例如整表未变更行）不设标题，按原顺序附在最后 —— 不丢行。
    function groupChangedRows(rows) {
        var list = rows || [];
        var groups = [];
        var grouped = {};

        for (var i = 0; i < DIFF_ROW_GROUPS.length; i++) {
            var spec = DIFF_ROW_GROUPS[i];
            var members = [];
            for (var j = 0; j < list.length; j++) {
                if ((list[j].status || '') === spec.status) {
                    members.push(list[j]);
                }
            }
            grouped[spec.status] = true;
            if (!members.length) {
                continue;
            }
            members.sort(function (a, b) {
                return (a.row_number || 0) - (b.row_number || 0);
            });
            groups.push({label: spec.label, rows: members});
        }

        var rest = [];
        for (var k = 0; k < list.length; k++) {
            if (!grouped[list[k].status || '']) {
                rest.push(list[k]);
            }
        }
        if (rest.length) {
            groups.push({label: null, rows: rest});
        }
        return groups;
    }

    // 分段的计数文案：行数 + （只有变更段才有的）单元格处数。
    // 「3 行 · 4 处单元格」—— 一段里改了几行、动了几格，扫一眼就知道该看多久。
    // 参数是**将要渲染的那些行**（筛选之后），所以计数跟着筛选结果变。
    function groupCountLabel(rows) {
        var list = rows || [];
        var label = list.length + ' 行';
        var changedCells = 0;
        for (var i = 0; i < list.length; i++) {
            var changes = list[i].cell_changes;
            if (changes && changes.length) {
                changedCells += changes.length;
            }
        }
        if (changedCells) {
            label += ' · ' + changedCells + ' 处单元格';
        }
        return label;
    }

    // 分组标题行：colspan 要连行号列一起覆盖，内容是「标签：」+ 计数。
    //
    // 计数只能是标签**之后**的一个 inline 元素：「变更行数据：」这个前缀有测试按
    // 正则 `colspan="N"[^>]*>变更行数据：` 卡，插在标签与冒号之间就把它破坏了。
    function groupHeaderRowHtml(label, columnCount, count) {
        var countHtml = count
            ? '<span class="excel-group-header__count">' + escapeHtml(count) + '</span>'
            : '';
        return '<tr class="excel-row-group-header">' +
               '<td class="excel-group-header" colspan="' + (columnCount + 1) + '">' +
               escapeHtml(label) + '：' + countHtml + '</td></tr>';
    }

    // 渲染计划：把「分组标题」与「行」拉平成一串待渲染单元。
    // 分批渲染按单元切，于是「段标题 + 它那段的第一批」一起出现，
    // 不会出现「标题在一批末尾、行在下一批」。
    function buildRowRenderPlan(rows, headers) {
        var groups = groupChangedRows(rows);
        var plan = [];
        for (var i = 0; i < groups.length; i++) {
            var group = groups[i];
            if (group.label) {
                plan.push({label: group.label, count: groupCountLabel(group.rows)});
            }
            for (var j = 0; j < group.rows.length; j++) {
                plan.push({row: group.rows[j]});
            }
        }
        return plan;
    }

    // 一个待渲染单元 → HTML
    function planUnitHtml(unit, headers) {
        if (unit && unit.row) {
            return changedRowHtml(unit.row, headers);
        }
        return groupHeaderRowHtml(unit ? unit.label : '', headers.length, unit ? unit.count : '');
    }

    // 表体：接受渲染计划（buildRowRenderPlan 的产物）或原始行数组。
    // 分批渲染传的是计划的一段（不能传原始行 —— 那样每一批都会重新分段、
    // 段标题会重复出现）。
    function tableBodyHtml(units, headers, opts) {
        var list = units || [];
        if (list.length && list[0] && list[0].row === undefined && list[0].status !== undefined) {
            list = buildRowRenderPlan(list, headers);
        }
        var html = '';
        for (var i = 0; i < list.length; i++) {
            html += planUnitHtml(list[i], headers);
        }
        return html;
    }

    // 小表：一次性把整个表体（含分组标题行）渲染成 HTML
    function changedRowsGroupedHtml(rows, headers, opts) {
        return tableBodyHtml(buildRowRenderPlan(rows, headers), headers, opts);
    }

    // 单行 → HTML：两条渲染路径（小表一次性、大表分批）共用，避免各写一份漂移
    function changedRowHtml(row, headers) {
        if (row.status === 'added') {
            return createAddedRow(row, headers);
        }
        if (row.status === 'removed') {
            return createRemovedRow(row, headers);
        }
        if (row.status === 'modified') {
            return createModifiedRow(row, headers);
        }
        return createUnchangedRow(row, headers);
    }

    // ------------------------------------------------------------------ 行

    // 行号格。**行号缺了就留空**，不用 rowIndex+1 编一个：那个数字会被当成
    // Excel 里的真实行号去核对，编出来的序号比空白更误导。
    // 类名 `excel-row-number` 写死在这里：它是「第一列冻结」（CSS 的 sticky left:0）
    // 的落点，也是别的用例找行号格的锚点 —— 别改成拼出来的名字。
    function rowNumberCell(row, extraClass) {
        return '<td class="excel-row-number' + (extraClass ? ' ' + extraClass : '') + '">' +
               escapeHtml(row.row_number || '') + '</td>';
    }

    function createAddedRow(row, headers) {
        var html = '<tr class="excel-row-added">' + rowNumberCell(row, 'excel-added');
        for (var i = 0; i < headers.length; i++) {
            var cellValue = cellText(row.data && row.data[headers[i]]);
            html += '<td class="excel-cell excel-added"><span class="excel-cell-inner">' +
                    escapeHtml(cellValue) + '</span></td>';
        }
        return html + '</tr>';
    }

    function createRemovedRow(row, headers) {
        var html = '<tr class="excel-row-removed">' + rowNumberCell(row, 'excel-removed');
        for (var i = 0; i < headers.length; i++) {
            var cellValue = cellText(row.data && row.data[headers[i]]);
            html += '<td class="excel-cell excel-removed"><span class="excel-cell-inner">' +
                    escapeHtml(cellValue) + '</span></td>';
        }
        return html + '</tr>';
    }

    // 创建修改行 - 双行结构：第一行旧值、第二行新值，行号格 rowspan="2"。
    // 未变更的格子在这一对里只出现一次（rowspan="2"），否则同一格会被读两遍。
    function createModifiedRow(row, headers) {
        var map = modifiedCellsMap(row);
        var html = '<tr class="excel-row-modified-old">' +
                   '<td class="excel-row-number excel-modified" rowspan="2">' +
                   escapeHtml(row.row_number || '') + '</td>';

        var i;
        for (i = 0; i < headers.length; i++) {
            var header = headers[i];
            var change = map[header];
            if (change) {
                var oldValue = cellText(change.old_value);
                if (oldValue === '') {
                    // 旧值本来就没有内容：只留一个空的文本块，不去高亮「新值」——
                    // 高亮函数在 oldValue 为空时会返回新值（那是给整格替换用的），
                    // 放在旧值行上就等于把改后内容印在了改前那一半。
                    html += '<td class="excel-cell excel-modified-old modified-column">' +
                            '<span class="excel-cell-inner">' + escapeHtml(oldValue) +
                            '</span></td>';
                } else {
                    // 精确到参数级别的高亮：只看那几个变化的片段
                    html += '<td class="excel-cell excel-modified-old modified-column">' +
                            '<span class="excel-cell-inner">' +
                            highlightDifferences(oldValue, cellText(change.new_value), 'old') +
                            '</span></td>';
                }
            } else {
                var cellValue = cellText(row.data && row.data[header]);
                html += '<td class="excel-cell excel-unchanged" rowspan="2">' +
                        '<span class="excel-cell-inner">' + escapeHtml(cellValue) +
                        '</span></td>';
            }
        }
        html += '</tr>';

        html += '<tr class="excel-row-modified-new">';
        for (i = 0; i < headers.length; i++) {
            var newHeader = headers[i];
            var newChange = map[newHeader];
            if (!newChange) {
                // 未修改的格子已经在上一行用 rowspan="2" 处理了
                continue;
            }
            var newValue = cellText(newChange.new_value);
            if (newValue === '') {
                html += '<td class="excel-cell excel-modified-new modified-column">' +
                        '<span class="excel-cell-inner">' + escapeHtml(newValue) +
                        '</span></td>';
            } else {
                html += '<td class="excel-cell excel-modified-new modified-column">' +
                        '<span class="excel-cell-inner">' +
                        highlightDifferences(cellText(newChange.old_value), newValue, 'new') +
                        '</span></td>';
            }
        }
        return html + '</tr>';
    }

    function createUnchangedRow(row, headers) {
        var html = '<tr class="excel-row-unchanged">' + rowNumberCell(row, '');
        for (var i = 0; i < headers.length; i++) {
            var cellValue = cellText(row.data && row.data[headers[i]]);
            html += '<td class="excel-cell"><span class="excel-cell-inner">' +
                    escapeHtml(cellValue) + '</span></td>';
        }
        return html + '</tr>';
    }

    // ------------------------------------------------------------------ 高亮

    // 精确高亮差异函数 - 只高亮变更的参数。
    //
    // 与 static/js/diff-handlers.js 里那份是同一套算法（那边保留给合并页的
    // 容器渲染路径用）。**显示用原样文本、比较也用原样文本**是这里唯一的口径：
    // 早先的实现用 split + trim + join 重排过字符串，于是分隔符后面的空格被吃掉，
    // 只差一个空格的两条记录 trim 之后完全相同 —— 「有变更」却一处都不高亮，
    // 加了前一条之后两边还长得一模一样，审核者无法判断改了什么。
    function highlightDifferences(oldValue, newValue, type) {
        if (!oldValue && !newValue) return '';
        if (!oldValue) return '<span class="excel-text-bg-' + type + '">' + escapeHtml(newValue) + '</span>';
        if (!newValue) return '<span class="excel-text-bg-' + type + '">' + escapeHtml(oldValue) + '</span>';

        var oldStr = String(oldValue);
        var newStr = String(newValue);

        // 如果值完全相同，直接返回
        if (oldStr === newStr) {
            return escapeHtml(oldStr);
        }

        // 检查是否为大括号分组的参数列表 {key,value},{key,value}
        if ((oldStr.indexOf('{') !== -1 && oldStr.indexOf('}') !== -1) ||
            (newStr.indexOf('{') !== -1 && newStr.indexOf('}') !== -1)) {
            return highlightBracketParameterList(oldStr, newStr, type);
        }

        // 检查是否包含任何分隔符的参数列表
        var separators = [',', ';', '@', '$', '&', '/', '\\', '_', '|'];
        var hasSeparator = false;
        for (var i = 0; i < separators.length; i++) {
            if (oldStr.indexOf(separators[i]) !== -1 || newStr.indexOf(separators[i]) !== -1) {
                hasSeparator = true;
                break;
            }
        }
        if (hasSeparator) {
            return highlightParameterList(oldStr, newStr, type);
        }

        // 对于单个值的情况，直接高亮整个值
        if (type === 'old') {
            return '<span class="excel-text-bg-old">' + escapeHtml(oldStr) + '</span>';
        }
        return '<span class="excel-text-bg-new">' + escapeHtml(newStr) + '</span>';
    }

    // 按分隔符把值切成 [段, 分隔符, 段, 分隔符, …, 段]，**段保留原样**（不 trim）。
    // 不能用 split(sep) + join(sep)：那样会把分隔符两侧的空格吃掉，页面上显示的内容
    // 就与文件不一致。
    function splitKeepingSeparators(value, separator) {
        var parts = [];
        var rest = String(value);
        var at = rest.indexOf(separator);
        while (at >= 0) {
            parts.push(rest.slice(0, at));
            parts.push(separator);
            rest = rest.slice(at + separator.length);
            at = rest.indexOf(separator);
        }
        parts.push(rest);
        return parts;
    }

    // 高亮参数列表差异 - 支持多种分隔符
    function highlightParameterList(oldValue, newValue, type) {
        // 大括号分组的参数格式交给专门的函数
        if (oldValue.indexOf('{') !== -1 && oldValue.indexOf('}') !== -1) {
            return highlightBracketParameterList(oldValue, newValue, type);
        }

        var separators = [',', ';', '@', '$', '&', '/', '\\', '_', '|'];
        var usedSeparator = ',';
        for (var i = 0; i < separators.length; i++) {
            if (oldValue.indexOf(separators[i]) !== -1 || newValue.indexOf(separators[i]) !== -1) {
                usedSeparator = separators[i];
                break;
            }
        }

        var oldParts = splitKeepingSeparators(oldValue, usedSeparator);
        var newParts = splitKeepingSeparators(newValue, usedSeparator);
        var oldParams = [];
        var newParams = [];
        var index;
        for (index = 0; index < oldParts.length; index += 2) oldParams.push(oldParts[index]);
        for (index = 0; index < newParts.length; index += 2) newParams.push(newParts[index]);

        // 分隔后超过 1 段发生变化时，按整格变更展示，避免出现碎片化高亮
        var maxLength = Math.max(oldParams.length, newParams.length);
        var changedSegmentCount = 0;
        for (index = 0; index < maxLength; index++) {
            if (oldParams[index] !== newParams[index]) {
                changedSegmentCount++;
            }
        }
        if (changedSegmentCount > 1) {
            if (type === 'old') {
                return '<span class="excel-text-bg-old">' + escapeHtml(oldValue) + '</span>';
            }
            return '<span class="excel-text-bg-new">' + escapeHtml(newValue) + '</span>';
        }

        var targetParts = type === 'old' ? oldParts : newParts;
        var compareParams = type === 'old' ? newParams : oldParams;
        var result = [];
        for (index = 0; index < targetParts.length; index++) {
            var chunk = targetParts[index];
            // 分隔符单独成项：原样输出（也要转义 —— 分隔符可能是 `&`）
            if (index % 2 === 1) {
                result.push(escapeHtml(chunk));
                continue;
            }
            var compareParam = compareParams[index / 2];
            if (compareParam === undefined || chunk !== compareParam) {
                result.push('<span class="excel-text-bg-' + type + '">' + escapeHtml(chunk) + '</span>');
            } else {
                result.push(escapeHtml(chunk));
            }
        }
        return result.join('');
    }

    // 处理大括号分组的参数列表 {key,value},{key,value}
    //
    // 与 highlightParameterList 同一条原则：**显示用原样文本，比较也用原样文本**。
    // 早先的实现把每个键值 trim 之后重新拼成 `{key,value}`、再用 `,` 把参数对连起来，
    // 于是 `{a, b}, {c, d}` 显示成 `{a,b},{c,d}`，括号内与参数对之间的空格全被吃掉；
    // 只差空格的两条记录 trim 之后完全相同，于是「有变更」却一格都不高亮。
    // 现在按匹配位置在原串上拼接：括号、逗号、以及参数对之间的原样文本全部保留。
    function highlightBracketParameterList(oldValue, newValue, type) {
        var targetValue = String(type === 'old' ? oldValue : newValue);
        var compareValue = String(type === 'old' ? newValue : oldValue);

        function collectPairs(text) {
            var pairRegex = /\{([^,}]+),([^}]+)\}/g;
            var found = [];
            var match;
            while ((match = pairRegex.exec(text)) !== null) {
                found.push({
                    raw: match[0],
                    key: match[1],
                    value: match[2],
                    start: match.index,
                    end: match.index + match[0].length
                });
            }
            return found;
        }

        var targetPairs = collectPairs(targetValue);
        var comparePairs = collectPairs(compareValue);
        var result = [];
        var cursor = 0;

        for (var index = 0; index < targetPairs.length; index++) {
            var targetPair = targetPairs[index];
            var comparePair = comparePairs[index];

            // 上一个参数对与这一个之间的原样文本（`}, {` 里的 `, ` 就在这一段里）
            result.push(escapeHtml(targetValue.slice(cursor, targetPair.start)));
            cursor = targetPair.end;

            if (!comparePair) {
                // 比较对象没有这个参数对，整个高亮
                result.push('<span class="excel-text-bg-' + type + '">' + escapeHtml(targetPair.raw) + '</span>');
                continue;
            }

            var keySame = targetPair.key === comparePair.key;
            var valueSame = targetPair.value === comparePair.value;
            if (keySame && !valueSame) {
                // 键相同但值不同，只高亮值部分。
                // `{` / `,` / `}` 是正则里固定的定界符，原样重拼即为原文。
                result.push('{' + escapeHtml(targetPair.key) + ',' +
                    '<span class="excel-text-bg-' + type + '">' + escapeHtml(targetPair.value) + '</span>' + '}');
            } else if (!keySame || !valueSame) {
                result.push('<span class="excel-text-bg-' + type + '">' + escapeHtml(targetPair.raw) + '</span>');
            } else {
                result.push(escapeHtml(targetPair.raw));
            }
        }

        // 最后一个参数对之后的原样文本
        result.push(escapeHtml(targetValue.slice(cursor)));
        return result.join('');
    }

    // ------------------------------------------------- 表头

    // 单行表头：每列一格，格子里上小字列字母、下面字段名。
    //
    // 提交页原来用两行表头（`tr.excel-header-row` 放 A/B/C、`tr.excel-field-row`
    // 放字段名），周版本页与合并页只有一行、没有字母 —— 同一张表在不同页面长得
    // 不一样，两行表头还多占一行高度。这里统一成一行：字母降级成小字，
    // 字段名保留（title 里也给一份，列宽被压窄时能悬停看全）。
    function tableHeadRowHtml(view, sheetData, opts) {
        var modifiedColumns = getModifiedColumns(sheetData);
        var html = '<thead><tr>' +
            '<th scope="col" class="excel-row-header">' +
            escapeHtml((opts && opts.rowHeaderLabel) || ROW_HEADER_LABEL) + '</th>';

        for (var i = 0; i < view.visibleHeaders.length; i++) {
            var header = view.visibleHeaders[i];
            // 列字母用**原始列序**：隐藏了中间的列之后，剩下的列还是要报它在
            // Excel 里的真实列号（B 被隐藏，C 不能变成 B）。
            var letter = columnLetter(view.visibleIndexes[i]);
            var isModified = modifiedColumns.indexOf(header) !== -1;
            var headerClass = isModified
                ? 'excel-column-header excel-modified-column-header'
                : 'excel-column-header';
            // 表头来自 Excel 第一行（不可信）：属性与文本都要转义
            html += '<th scope="col" class="' + headerClass + '" title="' + escapeHtmlAttribute(header) + '">' +
                    '<div class="excel-column-id">' + escapeHtml(letter) + '</div>' +
                    '<div class="excel-bold-text">' + escapeHtml(header) + '</div>' +
                    '</th>';
        }
        return html + '</tr></thead>';
    }

    // 表格的**开场部分**：包装容器 + 表头，一直到 `<tbody>` 开标签为止。
    // 单独暴露是因为提交页 >100 行时要分批追加表体：先写这一段，再一批批
    // innerHTML += tableBodyHtml(计划的一段)。
    // （未闭合的标签由 HTML 解析器在片段末尾自动闭合，所以这里不需要收尾标签。）
    function tableHeadHtml(sheetData, opts) {
        opts = opts || {};
        var state = resolveState(sheetData, opts);
        var view = viewFor(sheetData, opts);
        return '<div class="excel-table-wrapper"><table class="excel-diff-table">' +
               tableHeadRowHtml(view, sheetData, opts) +
               '<tbody>';
    }

    // ------------------------------------------------- 视图：筛选 / 开关

    // 状态默认值：「隐藏本页空列」默认开（表里那些整列没内容的列通常是没填的
    // 配置列，留着只会把有用的列挤出屏幕）。
    function freshState() {
        return {filter: '', column: '', onlyChanged: false, hideEmpty: true, timer: null};
    }

    // 容器上的状态（没有就建一个）
    function ensureState(container) {
        if (!container) return null;
        if (!container[STATE_KEY]) {
            var state = freshState();
            state.sheetData = null;
            state.opts = null;
            state.noticesHtml = '';
            container[STATE_KEY] = state;
        }
        return container[STATE_KEY];
    }

    function stateOf(container) {
        return container ? (container[STATE_KEY] || null) : null;
    }

    // 渲染时用的状态：给了容器就把它那份记下来（同一页多张表互不影响，
    // 重渲染后筛选条件与开关也还在）；没给容器（拼字符串的路径）就用一份临时的默认值。
    function resolveState(sheetData, opts) {
        if (opts && opts.container) {
            var state = ensureState(opts.container);
            state.sheetData = sheetData;
            // 只记渲染需要的选项：把调用方的 opts 原样存下来会把「这一帧的 view」
            // 也一起冻住，下一次重渲染就会拿着过期的列集合去渲染。
            state.opts = {
                sheetName: opts.sheetName,
                container: opts.container,
                toolbar: opts.toolbar,
                rowHeaderLabel: opts.rowHeaderLabel
            };
            if (opts.noticesHtml !== undefined) {
                state.noticesHtml = opts.noticesHtml;
            }
            return state;
        }
        return freshState();
    }

    // 筛选：对**原始单元格值**（formatCellValue 的结果，不是渲染后的 HTML）做
    // 大小写不敏感的子串匹配。限定列时只比那一列，否则比该行所有数据列。
    //
    // 不 trim 筛选词：与展示层同一条口径 —— 表里怎么写就怎么比。搜 `a ` 与 `a`
    // 是两个不同的请求，替用户把空格吃掉只会让「明明有这一行却搜不到」。
    function filterRows(rows, headers, state) {
        var list = rows || [];
        var needle = String((state && state.filter) || '').toLowerCase();
        if (!needle) {
            return list.slice();
        }
        var column = (state && state.column) || '';
        var result = [];
        for (var i = 0; i < list.length; i++) {
            if (rowMatchesFilter(list[i], headers, column, needle)) {
                result.push(list[i]);
            }
        }
        return result;
    }

    function rowMatchesFilter(row, headers, column, needle) {
        var columns = column ? [column] : (headers || []);
        for (var i = 0; i < columns.length; i++) {
            var header = columns[i];
            if (rowCellText(row, header).toLowerCase().indexOf(needle) !== -1) {
                return true;
            }
            // 修改行里改前/改后的取值都要能搜到：表体上两行都印着，
            // 只搜其中一半会让「看得见却搜不到」。
            var change = cellChangeFor(row, header);
            if (!change) continue;
            if (cellText(change.old_value).toLowerCase().indexOf(needle) !== -1) return true;
            if (cellText(change.new_value).toLowerCase().indexOf(needle) !== -1) return true;
        }
        return false;
    }

    // 这一列在**这一页要渲染的行**里有没有内容。
    //
    // **不能只看 `row.data`。** 修改行的显示值是分两行渲染的（改前 / 改后），值取自
    // `cell_changes` 的 `old_value` / `new_value`；`row.data` 是**当前版本**那一份，
    // 而整列删除的列在当前版本里全是空串。只查 `data` 的后果是：这类列被「隐藏本页空列」
    // 当成空列去掉，而它恰恰是这一页最该看到的改动 —— 用户反馈的
    // 「`AA随机类型` / `AA随机类型.1` 这两列被删掉了，打开隐藏空列就完全看不到」。
    //
    // 口径：**修改前、修改后都没有内容**才算空列。一侧为空不是空 —— 那正是
    // 「整列被删」「整列新增」「整列被清空」三种改动的形态。
    function columnHasContent(row, header) {
        if (rowCellText(row, header) !== '') {
            return true;
        }
        var change = cellChangeFor(row, header);
        if (!change) {
            return false;
        }
        if (cellText(change.old_value) !== '') {
            return true;
        }
        return cellText(change.new_value) !== '';
    }

    // 要渲染哪些列 → {indexes: [原始列下标], hiddenCount: 因「本页没有内容」被去掉的列数}
    //
    // 口径（两个开关的叠加顺序：先「只看变更列」，再「隐藏本页空列」）：
    //   * 只看变更列：留下 getModifiedColumns 的列 ∪ 整行新增/删除行里有内容的列。
    //     行号列不是数据列（它单独一格），永远保留。
    //   * 隐藏本页空列：在**本次要渲染的这些行**里，改前与改后都没有内容的列不渲染
    //     （判据见 columnHasContent：单元格级变更里的 old_value / new_value 也算内容）。
    //     文本 'null' / 'nan' 不是空 —— 与展示层同一口径。
    function resolveVisibleColumns(sheetData, headers, rows, state) {
        var list = rows || [];
        var indexes = [];
        var i;
        for (i = 0; i < headers.length; i++) {
            indexes.push(i);
        }

        if (state && state.onlyChanged) {
            var changed = changedColumnNames(sheetData);
            var kept = [];
            for (i = 0; i < indexes.length; i++) {
                if (changed.indexOf(headers[indexes[i]]) !== -1) {
                    kept.push(indexes[i]);
                }
            }
            indexes = kept;
        }

        var hiddenCount = 0;
        if (state && state.hideEmpty && indexes.length) {
            var visible = [];
            for (i = 0; i < indexes.length; i++) {
                var header = headers[indexes[i]];
                var hasContent = false;
                for (var j = 0; j < list.length; j++) {
                    if (columnHasContent(list[j], header)) {
                        hasContent = true;
                        break;
                    }
                }
                if (hasContent) {
                    visible.push(indexes[i]);
                } else {
                    hiddenCount += 1;
                }
            }
            indexes = visible;
        }

        return {indexes: indexes, hiddenCount: hiddenCount};
    }

    // 这一帧要渲染什么：可见列 + 命中筛选的行 + 拉平的渲染计划
    function buildView(sheetData, state, opts) {
        var headers = (sheetData && sheetData.headers) || [];
        var allRows = (sheetData && sheetData.rows) || [];
        var matched = filterRows(allRows, headers, state);
        var columns = resolveVisibleColumns(sheetData, headers, matched, state);
        var visibleHeaders = [];
        var i;
        for (i = 0; i < columns.indexes.length; i++) {
            visibleHeaders.push(headers[columns.indexes[i]]);
        }
        return {
            visibleHeaders: visibleHeaders,
            visibleIndexes: columns.indexes,
            hiddenCount: columns.hiddenCount,
            matched: matched,
            units: buildRowRenderPlan(matched, visibleHeaders),
            // 「一行都不剩」只在用户真的筛了什么的时候才算空结果：
            // 载荷本来就没有行是另一回事（那种情况由页面自己的提示拦在前面）。
            isEmptyResult: !!((state && state.filter) && !matched.length)
        };
    }

    function viewFor(sheetData, opts) {
        if (opts && opts.view) {
            return opts.view;
        }
        return buildView(sheetData, resolveState(sheetData, opts || {}), opts || {});
    }

    // ------------------------------------------------- 工具栏

    function matchCountText(state, view) {
        if (!state || !state.filter) {
            return '';
        }
        return '命中 ' + view.matched.length + ' 行';
    }

    function hiddenNoteText(view) {
        // 筛选到一行不剩时不报「已隐藏 N 列」：那时所有列按口径都算「本页没有内容」，
        // 于是会显示「已隐藏 12 列」——用户要的是「没有匹配的行」这句话，
        // 而不是一个听起来像出了什么事的列数。空结果由 .excel-diff-empty-result 说明。
        if (view.isEmptyResult) {
            return '';
        }
        return view.hiddenCount ? '已隐藏 ' + view.hiddenCount + ' 列' : '';
    }

    // 工具栏 DOM。样式在 static/css/diff-table-ux.css（那里集中管表体表现），
    // 这里只负责按约定的 class 把结构拼出来。
    function toolbarHtml(sheetData, opts) {
        opts = opts || {};
        if (opts.toolbar === false) {
            return '';
        }
        var state = resolveState(sheetData, opts);
        var view = viewFor(sheetData, opts);
        var headers = (sheetData && sheetData.headers) || [];

        var html = '<div class="excel-diff-toolbar">';
        html += '<div class="excel-diff-toolbar__search">';
        // 图标用 FontAwesome（`fas`）而不是 Bootstrap Icons（`bi`）：`base.html` 每一页都
        // 加载 FontAwesome，而 bootstrap-icons 只有个别页面自己引（commit_diff_new 引了，
        // 另三页没引）—— 用 `bi` 的话工具栏在那些页面上是两个空框，且没有任何报错。
        html += '<i class="fas fa-search" aria-hidden="true"></i>';
        html += '<input type="search" class="excel-diff-search-input" placeholder="按回车搜索…" ' +
                'aria-label="筛选行" value="' + escapeHtmlAttribute(state.filter) + '">';
        html += '<select class="excel-diff-search-column" aria-label="限定在某列内搜索">';
        html += '<option value="">全部列</option>';
        for (var i = 0; i < headers.length; i++) {
            var selected = headers[i] === state.column ? ' selected' : '';
            html += '<option value="' + escapeHtmlAttribute(headers[i]) + '"' + selected + '>' +
                    escapeHtml(headers[i]) + '</option>';
        }
        html += '</select>';
        html += '<button type="button" class="excel-diff-search-clear" aria-label="清除筛选">' +
                '<i class="fas fa-times" aria-hidden="true"></i></button>';
        html += '<span class="excel-diff-match-count" role="status" aria-live="polite">' +
                escapeHtml(matchCountText(state, view)) + '</span>';
        html += '</div>';

        html += '<div class="excel-diff-toolbar__views">';
        html += '<button type="button" class="excel-diff-toggle" data-toggle="only-changed" ' +
                'aria-pressed="' + (state.onlyChanged ? 'true' : 'false') + '">只看变更列</button>';
        html += '<button type="button" class="excel-diff-toggle" data-toggle="hide-empty" ' +
                'aria-pressed="' + (state.hideEmpty ? 'true' : 'false') + '">隐藏本页空列</button>';
        html += '<span class="excel-diff-hidden-note" role="status">' +
                escapeHtml(hiddenNoteText(view)) + '</span>';
        html += '</div></div>';
        return html;
    }

    // 空结果说明。与「没有净变更」那种空态同一个位置（表格容器里）。
    function emptyNoteHtml(text) {
        return '<div class="excel-diff-empty-result">' + escapeHtml(text) + '</div>';
    }

    // 表格本身（不含工具栏）：<table> 或空态说明
    function tableViewHtml(sheetData, view, state, opts) {
        if (view.isEmptyResult) {
            return emptyNoteHtml('没有匹配「' + ((state && state.filter) || '') + '」的行');
        }
        if (!view.matched.length) {
            // 一行都没有：渲染一张只有表头的空表看起来就跟页面坏了一样
            // （线上「没有净变更」那次报障的形态），把事实说出来。
            return emptyNoteHtml('该工作表没有变更行');
        }
        return '<table class="excel-diff-table">' +
               tableHeadRowHtml(view, sheetData, opts) +
               '<tbody>' + tableBodyHtml(view.units, view.visibleHeaders, opts) + '</tbody></table>';
    }

    // 完整的一块：工具栏 + 表格（或空态说明）
    function renderSheetTable(sheetData, opts) {
        opts = opts || {};
        if (opts.container) {
            bindToolbar(opts.container);
        }
        var state = resolveState(sheetData, opts);
        var view = viewFor(sheetData, opts);
        return toolbarHtml(sheetData, opts) +
               '<div class="excel-table-wrapper">' + tableViewHtml(sheetData, view, state, opts) + '</div>';
    }

    // ------------------------------------------------- 事件

    function hasClass(node, className) {
        return !!(node && node.classList && node.classList.contains(className));
    }

    // 从事件目标往上找带某 class 的祖先（不用 Element.closest：事件可能是从
    // 按钮里的 <i> 冒上来的，而极简 DOM 桩里没有 closest）
    function closestByClass(node, className) {
        var current = node;
        while (current && current.getAttribute) {
            if (hasClass(current, className)) {
                return current;
            }
            current = current.parentNode;
        }
        return null;
    }

    // 事件委托绑在**容器**上：一张表只绑一次，之后重渲染（innerHTML 重建）也依然有效。
    // 不用 onclick= 内联属性：工作表名、单元格内容都是被审核文件里的不可信数据，
    // innerHTML 里的 on* 会被编译成可执行处理器（见 tests/test_excel_sheet_name_injection.py）。
    function bindToolbar(container) {
        if (!container || container[BOUND_KEY] || typeof container.addEventListener !== 'function') {
            return;
        }
        container[BOUND_KEY] = true;
        container.addEventListener('input', onToolbarInput);
        container.addEventListener('change', onToolbarInput);
        container.addEventListener('click', onToolbarClick);
    }

    function onToolbarInput(event) {
        var container = event.currentTarget;
        var target = event.target || {};
        var state = stateOf(container);
        if (!state) return;

        if (hasClass(target, 'excel-diff-search-input')) {
            var value = String(target.value === undefined ? '' : target.value);
            if (value === state.filter) return;   // input 与 change 会各来一发，同值不重渲染
            state.filter = value;
            scheduleRefresh(container);
            return;
        }
        if (hasClass(target, 'excel-diff-search-column')) {
            var column = String(target.value === undefined ? '' : target.value);
            if (column === state.column) return;
            state.column = column;
            refreshTable(container);              // 下拉框选完才触发一次，不用 debounce
        }
    }

    function onToolbarClick(event) {
        var container = event.currentTarget;
        var target = event.target || {};
        var state = stateOf(container);
        if (!state) return;

        // 按钮里的图标（<i class="bi …">）才是点击的落点，所以这里要往上找按钮，
        // 不能只看事件目标本身 —— 只看目标的话，点在图标上一律没反应。
        if (closestByClass(target, 'excel-diff-search-clear')) {
            if (!state.filter) return;
            state.filter = '';
            renderInto(container);                // 输入框的值也要清掉，整块重建最省事
            return;
        }
        var toggle = closestByClass(target, 'excel-diff-toggle');
        if (!toggle) return;
        var which = toggle.getAttribute('data-toggle');
        if (which === 'only-changed') {
            state.onlyChanged = !state.onlyChanged;
        } else if (which === 'hide-empty') {
            state.hideEmpty = !state.hideEmpty;
        } else {
            return;
        }
        refreshTable(container);
    }

    // 输入用 debounce：每个按键都重建 DOM，大表会边打字边卡
    function scheduleRefresh(container) {
        var state = stateOf(container);
        if (!state) return;
        if (state.timer) {
            clearTimeout(state.timer);
        }
        state.timer = setTimeout(function () {
            state.timer = null;
            refreshTable(container);
        }, SEARCH_DEBOUNCE_MS);
    }

    // 只重建表格本身，工具栏（输入框 / 下拉框）原样留着 —— 重建工具栏会把光标
    // 与输入法状态一起丢掉，每敲一个字都要重新聚焦。
    // 注意：这里一次性渲染全部命中行（不再分批）。筛选/开关是显式操作，
    // 换来的是两条渲染路径（一次性 / 分批）在交互上完全一致。
    function refreshTable(container) {
        var state = stateOf(container);
        if (!state || !state.sheetData) return;
        var view = buildView(state.sheetData, state, state.opts);
        var wrapper = container.querySelector ? container.querySelector('.excel-table-wrapper') : null;
        if (!wrapper) return;
        wrapper.innerHTML = tableViewHtml(state.sheetData, view, state, state.opts);
        updateToolbarBits(container, view, state);
    }

    function setText(container, selector, value) {
        var node = container.querySelector ? container.querySelector(selector) : null;
        if (node) {
            node.textContent = value;
        }
    }

    function updateToolbarBits(container, view, state) {
        setText(container, '.excel-diff-match-count', matchCountText(state, view));
        setText(container, '.excel-diff-hidden-note', hiddenNoteText(view));
        var toggles = container.querySelectorAll ? container.querySelectorAll('.excel-diff-toggle') : [];
        for (var i = 0; i < toggles.length; i++) {
            var which = toggles[i].getAttribute('data-toggle');
            if (which === 'only-changed') {
                toggles[i].setAttribute('aria-pressed', state.onlyChanged ? 'true' : 'false');
            } else if (which === 'hide-empty') {
                toggles[i].setAttribute('aria-pressed', state.hideEmpty ? 'true' : 'false');
            }
        }
    }

    // 整块重建（含工具栏）
    function renderInto(container) {
        var state = stateOf(container);
        if (!state || !state.sheetData) return;
        container.innerHTML = (state.noticesHtml || '') + renderSheetTable(state.sheetData, state.opts);
    }

    // 装到容器里：写内容 + 绑定事件（表级提示由调用方通过 opts.noticesHtml 传进来，
    // 重渲染时原样保留）。
    function mountSheetTable(container, sheetData, opts) {
        opts = opts || {};
        if (!container) return '';
        opts.container = container;
        var state = resolveState(sheetData, opts);
        state.noticesHtml = opts.noticesHtml || '';
        container.innerHTML = state.noticesHtml + renderSheetTable(sheetData, opts);
        bindToolbar(container);
        return container.innerHTML;
    }

    // 视图状态（筛选词 / 限定列 / 两个开关）。按容器保存，重渲染后不会丢。
    // 给外面用是为了「预置一个视图」与「读回用户当前的视图」，测试也据此驱动。
    function getViewState(container) {
        return ensureState(container);
    }

    function setViewState(container, patch) {
        var state = ensureState(container);
        if (!state || !patch) return state;
        if (patch.filter !== undefined) state.filter = String(patch.filter);
        if (patch.column !== undefined) state.column = String(patch.column);
        if (patch.onlyChanged !== undefined) state.onlyChanged = !!patch.onlyChanged;
        if (patch.hideEmpty !== undefined) state.hideEmpty = !!patch.hideEmpty;
        refreshTable(container);
        return state;
    }

    window.ExcelDiffTable = {
        // 转义
        escapeHtml: escapeHtml,
        escapeHtmlAttribute: escapeHtmlAttribute,
        // 分段
        DIFF_ROW_GROUPS: DIFF_ROW_GROUPS,
        groupChangedRows: groupChangedRows,
        groupCountLabel: groupCountLabel,
        groupHeaderRowHtml: groupHeaderRowHtml,
        changedRowHtml: changedRowHtml,
        changedRowsGroupedHtml: changedRowsGroupedHtml,
        buildRowRenderPlan: buildRowRenderPlan,
        // 行
        createAddedRow: createAddedRow,
        createRemovedRow: createRemovedRow,
        createModifiedRow: createModifiedRow,
        createUnchangedRow: createUnchangedRow,
        // 表 / 表头 / 工具栏
        columnLetter: columnLetter,
        tableHeadHtml: tableHeadHtml,
        tableBodyHtml: tableBodyHtml,
        toolbarHtml: toolbarHtml,
        renderSheetTable: renderSheetTable,
        mountSheetTable: mountSheetTable,
        // 视图（筛选与两个开关）
        resolveView: viewFor,
        filterRows: filterRows,
        resolveVisibleColumns: resolveVisibleColumns,
        getViewState: getViewState,
        setViewState: setViewState,
        getModifiedColumns: getModifiedColumns,
        changedColumnNames: changedColumnNames,
        // 高亮
        highlightDifferences: highlightDifferences
    };
})();
