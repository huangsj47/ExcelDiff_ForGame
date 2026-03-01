// 现代化差异处理器 - 支持4种文件类型
// 版本: 2.0

// 文本差异处理器
function initTextDiff(diffData) {
    console.log('初始化文本差异显示:', diffData);
    
    // 添加行号点击事件
    document.querySelectorAll('.text-diff-line').forEach(line => {
        line.addEventListener('click', function() {
            this.classList.toggle('highlighted');
        });
    });
    
    // 添加代码折叠功能
    addCodeFolding();
}

// Excel差异处理器
function initExcelDiff(diffData) {
    console.log('初始化Excel差异显示:', diffData);
    
    if (!diffData || !diffData.sheets || Object.keys(diffData.sheets).length === 0) {
        console.log('没有Excel工作表数据');
        return;
    }
    
    // 生成工作表标签
    generateExcelTabs(diffData.sheets);
    
    // 生成工作表内容
    generateExcelContent(diffData.sheets);
    
    // 设置默认激活的工作表
    setDefaultActiveSheet(diffData.sheets);
}

// 图片差异处理器
function initImageDiff(diffData) {
    console.log('初始化图片差异显示:', diffData);
    
    // 添加图片缩放功能
    addImageZoom();
    
    // 添加图片对比功能
    if (diffData.previous_image && diffData.current_image) {
        addImageComparison();
    }
}

// 二进制文件差异处理器
function initBinaryDiff(diffData) {
    console.log('初始化二进制文件差异显示:', diffData);
    
    // 添加文件信息展开/折叠功能
    addBinaryInfoToggle();
}

// Excel相关函数
function generateExcelTabs(sheets) {
    const tabsContainer = document.getElementById('excel-sheet-tabs');
    if (!tabsContainer) return;
    
    const sheetNames = Object.keys(sheets);
    let tabsHtml = '';
    
    // 分析工作表变更状态
    const sheetAnalysis = sheetNames.map(name => {
        const sheet = sheets[name];
        const hasChanges = sheet.rows && sheet.rows.some(row => 
            row.status === 'added' || row.status === 'removed' || row.status === 'modified'
        );
        return { name, hasChanges };
    });
    
    // 排序：有变更的在前
    const sortedSheets = sheetAnalysis.sort((a, b) => {
        if (a.hasChanges && !b.hasChanges) return -1;
        if (!a.hasChanges && b.hasChanges) return 1;
        return a.name.localeCompare(b.name);
    });
    
    sortedSheets.forEach((sheet, index) => {
        const isActive = index === 0 ? 'active' : '';
        const hasChangesClass = sheet.hasChanges ? 'excel-tab-with-changes' : 'excel-tab-no-changes';
        const clickable = sheet.hasChanges ? `onclick="switchExcelSheet('${sheet.name}')"` : '';
        const disabled = sheet.hasChanges ? '' : 'excel-tab-disabled';
        
        tabsHtml += `
            <div class="excel-sheet-tab ${hasChangesClass} ${isActive} ${disabled}" 
                 ${clickable} data-sheet="${sheet.name}">
                ${sheet.name}
            </div>
        `;
    });
    
    tabsContainer.innerHTML = tabsHtml;
}

function generateExcelContent(sheets) {
    const contentContainer = document.getElementById('excel-content');
    if (!contentContainer) return;
    
    const sheetNames = Object.keys(sheets);
    let contentHtml = '';
    
    sheetNames.forEach((sheetName, index) => {
        const sheet = sheets[sheetName];
        const isActive = index === 0 ? 'active' : '';
        
        contentHtml += `
            <div class="excel-sheet-content ${isActive}" id="sheet-content-${sheetName}">
                ${generateExcelTable(sheetName, sheet)}
            </div>
        `;
    });
    
    contentContainer.innerHTML = contentHtml;
}

function generateExcelTable(sheetName, sheetData) {
    console.log('🔧 generateExcelTable called for:', sheetName);
    console.log('📊 sheetData:', sheetData);

    if (!sheetData.headers || !sheetData.rows) {
        console.log('❌ 工作表数据不完整');
        return `
            <div class="p-4 text-center text-muted">
                <p>工作表 "${sheetName}" 数据不完整</p>
            </div>
        `;
    }

    // 调试：分析行状态
    const rowStatusCounts = {};
    sheetData.rows.forEach((row, index) => {
        const status = row.status || 'unchanged';
        rowStatusCounts[status] = (rowStatusCounts[status] || 0) + 1;
        if (status === 'added') {
            console.log(`🟢 Found added row ${index}:`, row);
        }
    });
    console.log('📊 行状态统计:', rowStatusCounts);
    
    let html = `
        <div class="excel-table-wrapper">
            <table class="excel-diff-table">
                <thead>
                    <tr class="excel-header-row">
                        <th class="excel-row-header">行号</th>
    `;
    
    // 添加列标题 (A, B, C...)
    sheetData.headers.forEach((header, index) => {
        const columnLetter = getExcelColumnLetter(index);
        html += `<th class="excel-column-header">${columnLetter}</th>`;
    });
    
    html += `
                    </tr>
                    <tr class="excel-field-row">
                        <th class="excel-row-header">字段</th>
    `;
    
    // 添加字段名
    sheetData.headers.forEach(header => {
        html += `<th class="excel-field-header" title="${header}">${header}</th>`;
    });
    
    html += `
                    </tr>
                </thead>
                <tbody>
    `;
    
    // 处理数据行 - 只渲染有变更的行
    const changedRows = sheetData.rows.filter(row => 
        row.status === 'added' || row.status === 'removed' || row.status === 'modified'
    );
    
    if (changedRows.length === 0) {
        html += `<tr><td colspan="${sheetData.headers.length + 1}" class="no-changes-message">此工作表没有变更</td></tr>`;
    } else {
        changedRows.forEach(row => {
            if (row.status === 'added') {
                html += createAddedRow(row, sheetData.headers);
            } else if (row.status === 'removed') {
                html += createRemovedRow(row, sheetData.headers);
            } else if (row.status === 'modified') {
                html += createModifiedRow(row, sheetData.headers);
            }
        });
    }
    
    html += `
                </tbody>
            </table>
        </div>
    `;
    
    return html;
}

function createAddedRow(row, headers) {
    console.log('🟢 Creating added row:', row);
    let html = `<tr class="excel-row-added">
        <td class="excel-row-number excel-added">${row.row_number || ''}</td>`;

    headers.forEach(header => {
        const cellValue = formatCellValue(row.data && row.data[header]);
        html += `<td class="excel-cell excel-added">${escapeHtml(cellValue)}</td>`;
    });

    html += '</tr>';
    console.log('🟢 Generated added row HTML:', html);
    return html;
}

function createRemovedRow(row, headers) {
    let html = `<tr class="excel-row-removed">
        <td class="excel-row-number excel-removed">${row.row_number || ''}</td>`;
    
    headers.forEach(header => {
        const cellValue = formatCellValue(row.data && row.data[header]);
        html += `<td class="excel-cell excel-removed">${escapeHtml(cellValue)}</td>`;
    });
    
    html += '</tr>';
    return html;
}

function createModifiedRow(row, headers) {
    let html = '';
    
    // Convert cell_changes array to a map for easier lookup
    const modifiedCellsMap = {};
    if (row.cell_changes && Array.isArray(row.cell_changes)) {
        row.cell_changes.forEach(change => {
            modifiedCellsMap[change.column] = {
                old_value: change.old_value,
                new_value: change.new_value
            };
        });
    }
    
    // 第一行显示旧值
    html += `<tr class="excel-row-modified-old">
        <td class="excel-row-number excel-modified" rowspan="2">${row.row_number || ''}</td>`;
    
    headers.forEach(header => {
        const cellChange = modifiedCellsMap[header];
        
        if (cellChange) {
            const oldValue = formatCellValue(cellChange.old_value);
            const newValue = formatCellValue(cellChange.new_value);
            const highlightedOldValue = highlightDifferences(oldValue, newValue, 'old');
            console.log('🔥 About to insert OLD HTML:', JSON.stringify(highlightedOldValue));
            html += `<td class="excel-cell excel-modified-old modified-column">
                ${highlightedOldValue}
            </td>`;
        } else {
            const cellValue = formatCellValue(row.data && row.data[header]);
            html += `<td class="excel-cell excel-unchanged" rowspan="2">${escapeHtml(cellValue)}</td>`;
        }
    });
    
    html += '</tr>';
    
    // 第二行显示新值
    html += '<tr class="excel-row-modified-new">';
    
    headers.forEach(header => {
        const cellChange = modifiedCellsMap[header];
        
        if (cellChange) {
            const oldValue = formatCellValue(cellChange.old_value);
            const newValue = formatCellValue(cellChange.new_value);
            const highlightedNewValue = highlightDifferences(oldValue, newValue, 'new');
            console.log('🔥 About to insert NEW HTML:', JSON.stringify(highlightedNewValue));
            html += `<td class="excel-cell excel-modified-new modified-column">
                ${highlightedNewValue}
            </td>`;
        }
    });
    
    html += '</tr>';
    
    return html;
}

// 精确高亮差异函数 - 只高亮变更的参数
function highlightDifferences(oldValue, newValue, type) {
    if (!oldValue && !newValue) return '';
    if (!oldValue) return `<span class="excel-text-bg-${type}">${escapeHtml(newValue)}</span>`;
    if (!newValue) return `<span class="excel-text-bg-${type}">${escapeHtml(oldValue)}</span>`;
    
    const oldStr = String(oldValue);
    const newStr = String(newValue);
    
    // 如果值完全相同，直接返回
    if (oldStr === newStr) {
        return escapeHtml(oldStr);
    }
    
    // 检查是否为大括号分组的参数列表 {key,value},{key,value}
    if ((oldStr.includes('{') && oldStr.includes('}')) || (newStr.includes('{') && newStr.includes('}'))) {
        console.log('🔍 Detected bracket parameter format, calling highlightBracketParameterList');
        console.log('📥 Input oldStr:', JSON.stringify(oldStr));
        console.log('📥 Input newStr:', JSON.stringify(newStr));
        console.log('📥 Type:', type);
        const result = highlightBracketParameterList(oldStr, newStr, type);
        console.log('📤 highlightBracketParameterList returned:', JSON.stringify(result));
        console.log('📤 Result length:', result.length);
        return result;
    }
    
    // 检查是否包含任何分隔符的参数列表
    const separators = [',', ';', '@', '$', '&', '/', '\\', '_', '|'];
    const hasSeparator = separators.some(sep => oldStr.includes(sep) || newStr.includes(sep));
    
    if (hasSeparator) {
        console.log('Detected parameter format with separators, calling highlightParameterList');
        return highlightParameterList(oldStr, newStr, type);
    }
    
    // 对于单个值的情况，直接高亮整个值
    if (type === 'old') {
        return `<span class="excel-text-bg-old">${escapeHtml(oldStr)}</span>`;
    } else {
        return `<span class="excel-text-bg-new">${escapeHtml(newStr)}</span>`;
    }
}

// 高亮参数列表差异 - 支持{key,value}格式
function highlightParameterList(oldValue, newValue, type) {
    // 检查是否为大括号分组的参数格式
    if (oldValue.includes('{') && oldValue.includes('}')) {
        return highlightBracketParameterList(oldValue, newValue, type);
    }
    
    // 智能分割参数 - 支持多种分隔符
    const separators = [',', ';', '@', '$', '&', '/', '\\', '_', '|'];
    
    // 找到实际使用的分隔符
    let usedSeparator = ','; // 默认逗号
    for (const sep of separators) {
        if (oldValue.includes(sep) || newValue.includes(sep)) {
            usedSeparator = sep;
            break;
        }
    }
    
    // 使用找到的分隔符分割参数
    const oldParams = oldValue.split(usedSeparator).map(p => p.trim());
    const newParams = newValue.split(usedSeparator).map(p => p.trim());
    const targetParams = type === 'old' ? oldParams : newParams;
    const compareParams = type === 'old' ? newParams : oldParams;
    
    const result = [];
    
    for (let i = 0; i < targetParams.length; i++) {
        const param = targetParams[i];
        const compareParam = compareParams[i];
        
        if (compareParam === undefined || param !== compareParam) {
            // 参数发生变化，高亮显示
            result.push(`<span class="excel-text-bg-${type}">${escapeHtml(param)}</span>`);
        } else {
            // 参数未变化，正常显示
            result.push(escapeHtml(param));
        }
    }
    
    return result.join(usedSeparator);
}

// 处理大括号分组的参数列表 {key,value},{key,value}
function highlightBracketParameterList(oldValue, newValue, type) {
    const targetValue = type === 'old' ? oldValue : newValue;
    const compareValue = type === 'old' ? newValue : oldValue;
    
    // 解析参数对
    const targetPairs = parseParameterPairs(targetValue);
    const comparePairs = parseParameterPairs(compareValue);
    
    const result = [];
    
    // 确保两个数组长度一致，处理可能的长度差异
    const maxLength = Math.max(targetPairs.length, comparePairs.length);
    
    for (let index = 0; index < maxLength; index++) {
        const targetPair = targetPairs[index];
        const comparePair = comparePairs[index];
        
        if (!targetPair) {
            // 目标没有这个参数对，跳过
            continue;
        }
        
        if (!comparePair) {
            // 比较对象没有这个参数对，整个高亮 - 不转义
            result.push(`<span class="excel-text-bg-${type}">{${targetPair.key},${targetPair.value}}</span>`);
        } else if (targetPair.key === comparePair.key && targetPair.value !== comparePair.value) {
            // 键相同但值不同，只高亮值部分 - 不转义HTML标签
            const highlightedPair = `{${targetPair.key},<span class="excel-text-bg-${type}">${targetPair.value}</span>}`;
            console.log(`Generated value-only highlight: ${highlightedPair}`);
            result.push(highlightedPair);
        } else if (targetPair.key !== comparePair.key || targetPair.value !== comparePair.value) {
            // 键或值都不同，整个参数对高亮 - 不转义
            result.push(`<span class="excel-text-bg-${type}">{${targetPair.key},${targetPair.value}}</span>`);
        } else {
            // 参数对完全相同，正常显示 - 不转义
            result.push(`{${targetPair.key},${targetPair.value}}`);
        }
    }
    
    const finalResult = result.join(',');
    console.log('Final result:', finalResult);
    console.log('Final result HTML:', finalResult);
    return finalResult;
}

// 解析参数对 {key,value},{key,value} -> [{key, value}, {key, value}]
function parseParameterPairs(value) {
    console.log('parseParameterPairs input:', value);
    const pairs = [];
    const regex = /\{([^,}]+),([^}]+)\}/g;
    let match;
    
    while ((match = regex.exec(value)) !== null) {
        const pair = {
            key: match[1].trim(),
            value: match[2].trim()
        };
        console.log('Parsed pair:', pair);
        pairs.push(pair);
    }
    
    console.log('All parsed pairs:', pairs);
    return pairs;
}

// 高亮字符级别差异
function highlightCharacterDifferences(oldValue, newValue, type) {
    const targetValue = type === 'old' ? oldValue : newValue;
    const compareValue = type === 'old' ? newValue : oldValue;
    
    // 简单的字符差异检测
    const result = [];
    const maxLength = Math.max(targetValue.length, compareValue.length);
    
    for (let i = 0; i < targetValue.length; i++) {
        const char = targetValue[i];
        const compareChar = compareValue[i];
        
        if (compareChar === undefined || char !== compareChar) {
            // 字符发生变化，高亮显示
            result.push(`<span class="excel-text-bg-${type}">${escapeHtml(char)}</span>`);
        } else {
            // 字符未变化，正常显示
            result.push(escapeHtml(char));
        }
    }
    
    return result.join('');
}

function createUnchangedRow(row, headers) {
    let html = `<tr class="excel-row-unchanged">
        <td class="excel-row-number">${row.row_number || ''}</td>`;
    
    headers.forEach(header => {
        const cellValue = formatCellValue(row.data && row.data[header]);
        html += `<td class="excel-cell">${escapeHtml(cellValue)}</td>`;
    });
    
    html += '</tr>';
    return html;
}

function setDefaultActiveSheet(sheets) {
    const sheetNames = Object.keys(sheets);
    if (sheetNames.length > 0) {
        // 找到第一个有变更的工作表
        const sheetWithChanges = sheetNames.find(name => {
            const sheet = sheets[name];
            return sheet.rows && sheet.rows.some(row => 
                row.status === 'added' || row.status === 'removed' || row.status === 'modified'
            );
        });
        
        const defaultSheet = sheetWithChanges || sheetNames[0];
        switchExcelSheet(defaultSheet);
    }
}

function switchExcelSheet(sheetName) {
    // 隐藏所有内容
    document.querySelectorAll('.excel-sheet-content').forEach(content => {
        content.classList.remove('active');
    });
    
    // 移除所有标签的active状态
    document.querySelectorAll('.excel-sheet-tab').forEach(tab => {
        tab.classList.remove('active');
    });
    
    // 显示选中的内容
    const targetContent = document.getElementById(`sheet-content-${sheetName}`);
    if (targetContent) {
        targetContent.classList.add('active');
    }
    
    // 激活选中的标签
    const targetTab = document.querySelector(`.excel-sheet-tab[data-sheet="${sheetName}"]`);
    if (targetTab) {
        targetTab.classList.add('active');
    }
}

function getExcelColumnLetter(index) {
    let result = "";
    while (index >= 0) {
        result = String.fromCharCode(65 + (index % 26)) + result;
        index = Math.floor(index / 26) - 1;
    }
    return result;
}

// 辅助功能函数
function addCodeFolding() {
    // 为文本差异添加代码折叠功能
    document.querySelectorAll('.text-diff-hunk-header').forEach(header => {
        header.style.cursor = 'pointer';
        header.addEventListener('click', function() {
            const hunk = this.parentElement;
            const lines = hunk.querySelector('.text-diff-lines');
            if (lines) {
                lines.style.display = lines.style.display === 'none' ? 'block' : 'none';
                this.classList.toggle('collapsed');
            }
        });
    });
}

function addImageZoom() {
    // 为图片添加缩放功能
    document.querySelectorAll('.image-diff-image').forEach(img => {
        img.style.cursor = 'zoom-in';
        img.addEventListener('click', function() {
            if (this.style.transform === 'scale(2)') {
                this.style.transform = 'scale(1)';
                this.style.cursor = 'zoom-in';
            } else {
                this.style.transform = 'scale(2)';
                this.style.cursor = 'zoom-out';
            }
        });
    });
}

function addImageComparison() {
    // 添加图片对比滑块功能（可选）
    console.log('图片对比功能已初始化');
}

function addBinaryInfoToggle() {
    // 为二进制文件信息添加展开/折叠功能
    const infoSection = document.querySelector('.binary-diff-info');
    if (infoSection) {
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'btn btn-sm btn-outline-secondary mb-3';
        toggleBtn.innerHTML = '<i class="bi bi-chevron-down"></i> 显示详细信息';
        
        infoSection.style.display = 'none';
        infoSection.parentElement.insertBefore(toggleBtn, infoSection);
        
        toggleBtn.addEventListener('click', function() {
            if (infoSection.style.display === 'none') {
                infoSection.style.display = 'block';
                this.innerHTML = '<i class="bi bi-chevron-up"></i> 隐藏详细信息';
            } else {
                infoSection.style.display = 'none';
                this.innerHTML = '<i class="bi bi-chevron-down"></i> 显示详细信息';
            }
        });
    }
}

// 全局工具函数
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatCellValue(value) {
    // 处理空值、null值、undefined值和NaN值
    if (value === null || value === undefined) {
        return '';
    }

    // 处理NaN值
    if (typeof value === 'number' && isNaN(value)) {
        return '';
    }

    // 转换为字符串并去除多余空格
    const strValue = String(value).trim();

    // 检查是否为字符串形式的NaN、null等
    if (strValue.toLowerCase() === 'nan' ||
        strValue.toLowerCase() === 'null' ||
        strValue.toLowerCase() === 'undefined' ||
        strValue.toLowerCase() === 'none' ||
        strValue === '') {
        return '';
    }

    return strValue;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// 合并diff页面专用函数
function showExcelSheetInContainer(diffData, containerId) {
    console.log('🔍 showExcelSheetInContainer called with:', {
        diffData: diffData,
        containerId: containerId,
        hasSheets: diffData && diffData.sheets ? Object.keys(diffData.sheets).length : 0
    });
    
    const container = document.getElementById(containerId);
    if (!container) {
        console.error('❌ Container not found:', containerId);
        return;
    }
    
    console.log('✅ Container found:', container.id);
    
    if (!diffData || !diffData.sheets || Object.keys(diffData.sheets).length === 0) {
        container.innerHTML = '<div class="alert alert-warning">没有Excel工作表数据</div>';
        return;
    }
    
    const sheets = diffData.sheets;
    const sheetNames = Object.keys(sheets);
    
    // 生成工作表标签和内容
    let html = '';
    
    // 如果有多个工作表，显示标签
    if (sheetNames.length > 1) {
        html += '<div class="excel-sheet-tabs">';
        sheetNames.forEach((sheetName, index) => {
            const isActive = index === 0 ? 'active' : '';
            html += `<div class="excel-sheet-tab ${isActive}" onclick="switchExcelSheetInContainer('${sheetName}', '${containerId}')" data-sheet="${sheetName}">${sheetName}</div>`;
        });
        html += '</div>';
    }
    
    // 生成工作表内容
    html += '<div class="excel-content">';
    sheetNames.forEach((sheetName, index) => {
        const sheet = sheets[sheetName];
        const isActive = index === 0 ? 'active' : '';
        
        html += `<div class="excel-sheet-content ${isActive}" id="sheet-content-${containerId}-${sheetName}">`;
        html += generateExcelTableForContainer(sheetName, sheet);
        html += '</div>';
    });
    html += '</div>';
    
    console.log('📝 Generated HTML length:', html.length);
    console.log('📝 HTML preview:', html.substring(0, 200) + '...');
    
    container.innerHTML = html;
    
    console.log('✅ HTML inserted into container:', containerId);
    console.log('📊 Final container HTML length:', container.innerHTML.length);
}

function generateExcelTableForContainer(sheetName, sheetData) {
    console.log('🔧 generateExcelTableForContainer called for:', sheetName);
    console.log('📊 sheetData:', sheetData);

    if (!sheetData.headers || !sheetData.rows) {
        console.log('❌ 容器工作表数据不完整');
        return `<div class="p-4 text-center text-muted"><p>工作表 "${sheetName}" 数据不完整</p></div>`;
    }

    // 调试：分析行状态
    const rowStatusCounts = {};
    sheetData.rows.forEach((row, index) => {
        const status = row.status || 'unchanged';
        rowStatusCounts[status] = (rowStatusCounts[status] || 0) + 1;
        if (status === 'added') {
            console.log(`🟢 Found added row ${index} in container:`, row);
        }
    });
    console.log('📊 容器行状态统计:', rowStatusCounts);
    
    let html = `
        <div class="excel-table-wrapper">
            <table class="excel-diff-table">
                <thead>
                    <tr class="excel-header-row">
                        <th class="excel-row-header">行号</th>
    `;
    
    // 添加列标题
    sheetData.headers.forEach((header, index) => {
        const columnLetter = getExcelColumnLetter(index);
        html += `<th class="excel-column-header">${columnLetter}</th>`;
    });
    
    html += `</tr><tr class="excel-field-row"><th class="excel-row-header">字段</th>`;
    
    // 添加字段名
    sheetData.headers.forEach(header => {
        html += `<th class="excel-field-header" title="${header}">${header}</th>`;
    });
    
    html += `</tr></thead><tbody>`;
    
    // 处理数据行 - 只渲染有变更的行
    const changedRows = sheetData.rows.filter(row => 
        row.status === 'added' || row.status === 'removed' || row.status === 'modified'
    );
    
    if (changedRows.length === 0) {
        html += `<tr><td colspan="${sheetData.headers.length + 1}" class="no-changes-message">此工作表没有变更</td></tr>`;
    } else {
        changedRows.forEach(row => {
            if (row.status === 'added') {
                html += createAddedRow(row, sheetData.headers);
            } else if (row.status === 'removed') {
                html += createRemovedRow(row, sheetData.headers);
            } else if (row.status === 'modified') {
                html += createModifiedRow(row, sheetData.headers);
            }
        });
    }
    
    html += `</tbody></table></div>`;
    return html;
}

function switchExcelSheetInContainer(sheetName, containerId) {
    // 隐藏该容器内所有工作表内容
    document.querySelectorAll(`#${containerId} .excel-sheet-content`).forEach(content => {
        content.classList.remove('active');
    });
    
    // 移除该容器内所有标签的active状态
    document.querySelectorAll(`#${containerId} .excel-sheet-tab`).forEach(tab => {
        tab.classList.remove('active');
    });
    
    // 显示选中的内容
    const targetContent = document.getElementById(`sheet-content-${containerId}-${sheetName}`);
    if (targetContent) {
        targetContent.classList.add('active');
    }
    
    // 激活选中的标签
    const targetTab = document.querySelector(`#${containerId} .excel-sheet-tab[data-sheet="${sheetName}"]`);
    if (targetTab) {
        targetTab.classList.add('active');
    }
}

// 导出函数供全局使用
window.initTextDiff = initTextDiff;
window.initExcelDiff = initExcelDiff;
window.initImageDiff = initImageDiff;
window.initBinaryDiff = initBinaryDiff;
window.switchExcelSheet = switchExcelSheet;
window.showExcelSheetInContainer = showExcelSheetInContainer;
window.switchExcelSheetInContainer = switchExcelSheetInContainer;
