// 现代化差异处理器 - 支持3种文件类型（文本 / 图片 / 二进制）
// 版本: 2.1
//
// ## 这里为什么只剩文本/图片/二进制的交互了
//
// 本文件原先还带两套 Excel 的东西，2026 重构时都删掉了。**两者性质不同**，
// 下面分开写清楚，免得后人以为是同一次删除。
//
// ### 一、删掉的是死代码：客户端 Excel 表体渲染
//
// `generateExcelContent` → `generateExcelTable` → `createAddedRow` / `createRemovedRow`
// / `createModifiedRow` / `createUnchangedRow`，外加合并页的容器版
// `showExcelSheetInContainer` / `generateExcelTableForContainer` /
// `switchExcelSheetInContainer`，以及它们下游的高亮与转义
// （`highlightDifferences` / `highlightParameterList` / `highlightBracketParameterList`
// / `splitKeepingSeparators` / `formatCellValue` / `escapeHtml` /
// `escapeHtmlAttribute` / `getExcelColumnLetter`），外加一个全仓零调用的
// `formatFileSize`（它的调用者也在那批里）。
//
// 这条链**一次都没执行过**：`generateExcelContent` 找的容器是 `#excel-content`，
// 而全仓只有 `templates/merge_diff.html` 有那个 id，那一页又不调它的入口；
// 唯一的入口页 `templates/commit_diff_new.html` 上的容器是 partial 渲染的
// `#excel-content-area`，取不到就直接 return。
// 表体渲染的**唯一实现**是 `static/js/excel_diff_table.js`（表头 / 行 / 分段 /
// 工具栏 / 筛选与视图开关 / 高亮都在那里）。要改单元格的呈现，改那一份；不要在这里
// 再抄一份 —— 历史上正是「四份各自演化」让同一个字面量在不同页面长得不一样。
//
// ### 二、删掉的还有一处**活缺陷**：标签页重建
//
// `initExcelDiff` / `generateExcelTabs` / `setDefaultActiveSheet` /
// `switchExcelSheet` / `findTabBySheetName` 这一组**不是**死代码：它的入口
// `initExcelDiff` 确实被 `templates/commit_diff_new.html` 调用，`#excel-sheet-tabs`
// 在那页也真实存在。但它在那一页上是**净损害**：
//
//   1. `generateExcelTabs` 先 `tabsContainer.textContent = ''`，把 partial **服务端
//      渲染好的**标签整个删掉，换成 `<div>`（丢掉 `<button>` 语义、「变更」徽章、
//      `aria-current`），并把点击绑到这个文件里的 `switchExcelSheet`；
//   2. `setDefaultActiveSheet` → `switchExcelSheet` 按 `sheet-content-${name}` 找内容，
//      而那个 id 约定只有 `templates/commit_diff.html`（自己拼表的那页）成立；
//      partial 用的是 `id="sheet-${name}"`，取不到 → 它把所有
//      `.excel-sheet-content` 的 `active` 类都摘掉、却没有任何一个被加回来；
//   3. `.excel-sheet-content` 是 `display: none`（`static/css/excel-diff-new.css`），
//      `.active` 才是 `block` —— 于是**整块 Excel 表体在这一页上不显示**。
//
// 那一页的标签与表体本来就由 `diff_partials/excel_diff.html` 自己负责（服务端渲染
// `<button>` + 自带的 `addEventListener` 绑定）。所以这一组连同
// `templates/commit_diff_new.html` 里那次 `initExcelDiff(...)` 调用一起删掉，
// 恢复成「服务端说了算」；留着调用点会变成 `ReferenceError`，而它后面紧跟的是
// `initCommitAiAnalysis()`，AI 抽屉会跟着一起不初始化。
//
// 本条与上面那条的区别很重要：**死代码只是没跑，这一条是跑起来会坏事**。
//
// 注：`templates/commit_diff.html` 与 `templates/merge_diff.html` 也加载本文件，
// 但它们各自有本页版本的 `switchExcelSheet` / `showExcelSheetInContainer`，且都不调
// 本文件里的任何函数 —— 本文件在那两页上目前是纯加载（未清理，见提交说明）。

// 文本差异处理器
function initTextDiff(diffData) {
    
    // 添加行号点击事件
    document.querySelectorAll('.text-diff-line').forEach(line => {
        line.addEventListener('click', function() {
            this.classList.toggle('highlighted');
        });
    });
    
    // 添加代码折叠功能
    addCodeFolding();
}

// 图片差异处理器
function initImageDiff(diffData) {
    
    // 添加图片缩放功能
    addImageZoom();
    
    // 添加图片对比功能
    if (diffData.previous_image && diffData.current_image) {
        addImageComparison();
    }
}

// 二进制文件差异处理器
function initBinaryDiff(diffData) {
    
    // 添加文件信息展开/折叠功能
    addBinaryInfoToggle();
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

// 导出函数供全局使用
window.initTextDiff = initTextDiff;
window.initImageDiff = initImageDiff;
window.initBinaryDiff = initBinaryDiff;
