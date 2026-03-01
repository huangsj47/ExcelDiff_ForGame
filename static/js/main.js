// 主要的JavaScript功能

$(document).ready(function() {
    // 初始化工具提示
    $('[data-bs-toggle="tooltip"]').tooltip();
    
    // 初始化弹出框
    $('[data-bs-toggle="popover"]').popover();
    
    // 表格行点击事件
    $('.table-hover tbody tr').click(function(e) {
        if (e.target.type !== 'checkbox' && e.target.tagName !== 'BUTTON' && e.target.tagName !== 'A') {
            const checkbox = $(this).find('input[type="checkbox"]');
            checkbox.prop('checked', !checkbox.prop('checked'));
        }
    });
    
    // 全选功能
    $('#selectAll').change(function() {
        const checkboxes = $('input[name="commit_ids"]');
        checkboxes.prop('checked', this.checked);
    });
    
    // 批量操作
    $('.batch-action').click(function() {
        const selectedIds = [];
        $('input[name="commit_ids"]:checked').each(function() {
            selectedIds.push($(this).val());
        });
        
        if (selectedIds.length === 0) {
            alert('请选择要操作的提交记录');
            return;
        }
        
        const action = $(this).data('action');
        if (confirm(`确定要对选中的 ${selectedIds.length} 条记录执行 ${action} 操作吗？`)) {
            // 执行批量操作
            batchUpdateCommits(selectedIds, action);
        }
    });
    
    // 自动刷新功能
    if ($('#auto-refresh').is(':checked')) {
        setInterval(function() {
            location.reload();
        }, 30000); // 30秒刷新一次
    }
    
    // 重新生成Diff缓存按钮事件已移至commit_list.html中的regenerateCache()函数
    
    // 筛选表单自动提交
});

function addCsrfToForm(form) {
    if (window.appendCsrfToken) {
        window.appendCsrfToken(form);
        return;
    }
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) return;
    const token = meta.getAttribute('content');
    if (!token) return;
    let input = form.querySelector('input[name="_csrf_token"]');
    if (!input) {
        input = document.createElement('input');
        input.type = 'hidden';
        input.name = '_csrf_token';
        form.appendChild(input);
    }
    input.value = token;
}

// 旧的regenerateDiffCache函数已移除，现在使用commit_list.html中的regenerateCache()函数

// 检查缓存状态
function checkCacheStatus(repositoryId) {
    fetch(`/repositories/${repositoryId}/cache-status`)
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const message = `缓存状态 - 已完成: ${data.completed_cache}, 失败: ${data.failed_cache}, 处理中: ${data.processing_cache}, 覆盖率: ${data.cache_coverage}`;
            showAlert('info', message);
        }
    })
    .catch(error => {
        console.error('获取缓存状态失败:', error);
    });
}

// 显示提示消息
function showAlert(type, message) {
    // 创建alert元素
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    `;
    
    // 插入到页面顶部
    const container = document.querySelector('.container-fluid');
    container.insertBefore(alertDiv, container.firstChild);
    
    // 5秒后自动消失
    setTimeout(() => {
        if (alertDiv.parentNode) {
            alertDiv.remove();
        }
    }, 5000);
}

// 批量更新提交状态
function batchUpdateCommits(ids, action) {
    const actionNormalized = String(action || '').toLowerCase();
    const isApprove = ['approve', 'confirm', 'confirmed'].includes(actionNormalized);
    const endpoint = isApprove ? '/commits/batch-approve' : '/commits/batch-reject';

    $.ajax({
        url: endpoint,
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({
            commit_ids: ids
        }),
        success: function(response) {
            alert('操作成功');
            location.reload();
        },
        error: function() {
            alert('操作失败');
        }
    });
}

// 测试仓库连接
function testRepository(repositoryId) {
    if (confirm('确定要测试此仓库的连接吗？')) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/repositories/${repositoryId}/test`;
        addCsrfToForm(form);
        document.body.appendChild(form);
        form.submit();
    }
}

// 同步仓库数据
function syncRepository(repositoryId) {
    if (confirm('确定要同步此仓库的数据吗？')) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/repositories/${repositoryId}/sync`;
        addCsrfToForm(form);
        document.body.appendChild(form);
        form.submit();
    }
}

// 删除项目
function deleteProject(projectId) {
    if (confirm('确定要删除这个项目吗？此操作不可恢复，将删除项目下的所有仓库和提交记录。')) {
        $.ajax({
            url: `/projects/${projectId}/delete`,
            method: 'POST',
            success: function(response) {
                alert('删除成功');
                location.reload();
            },
            error: function() {
                alert('删除失败');
            }
        });
    }
}

// 重试克隆仓库
function retryClone(repositoryId) {
    if (confirm('确定要重试克隆此仓库吗？')) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = `/repositories/${repositoryId}/retry-clone`;
        addCsrfToForm(form);
        document.body.appendChild(form);
        form.submit();
    }
}

// 删除仓库
function deleteRepository(repoId) {
    if (confirm('确定要删除这个仓库吗？此操作不可恢复，将删除仓库下的所有提交记录。')) {
        $.ajax({
            url: `/repositories/${repoId}/delete`,
            method: 'POST',
            success: function(response) {
                alert('删除成功');
                location.reload();
            },
            error: function() {
                alert('删除失败');
            }
        });
    }
}

// 显示diff详情
function showDiffDetail(commitId) {
    window.open(`/commits/${commitId}/diff`, '_blank');
}

// 快速确认/拒绝
function quickUpdateStatus(commitId, status) {
    const actionNormalized = String(status || '').toLowerCase();
    const targetStatus = ['confirm', 'confirmed', 'approve'].includes(actionNormalized) ? 'confirmed' : 'rejected';

    $.ajax({
        url: `/commits/${commitId}/status`,
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify({
            status: targetStatus
        }),
        success: function(response) {
            // 更新页面状态显示
            const row = $(`tr[data-commit-id="${commitId}"]`);
            const statusCell = row.find('.status-cell');
            const statusClass = targetStatus === 'confirmed' ? 'success' : 'danger';
            const statusText = targetStatus === 'confirmed' ? '已确认' : '已拒绝';
            statusCell.html(`<span class="badge bg-${statusClass}">${statusText}</span>`);
            
            // 禁用操作按钮
            row.find('.action-buttons').html('<span class="text-muted">已处理</span>');
        },
        error: function() {
            alert('操作失败');
        }
    });
}

// 表格排序功能
function sortTable(columnIndex, tableId) {
    const table = document.getElementById(tableId);
    const tbody = table.getElementsByTagName('tbody')[0];
    const rows = Array.from(tbody.getElementsByTagName('tr'));
    
    rows.sort((a, b) => {
        const aText = a.getElementsByTagName('td')[columnIndex].textContent.trim();
        const bText = b.getElementsByTagName('td')[columnIndex].textContent.trim();
        
        // 尝试数字比较
        const aNum = parseFloat(aText);
        const bNum = parseFloat(bText);
        
        if (!isNaN(aNum) && !isNaN(bNum)) {
            return aNum - bNum;
        }
        
        // 字符串比较
        return aText.localeCompare(bText);
    });
    
    // 重新插入排序后的行
    rows.forEach(row => tbody.appendChild(row));
}

// 导出功能
function exportData(format) {
    const selectedIds = [];
    $('input[name="commit_ids"]:checked').each(function() {
        selectedIds.push($(this).val());
    });
    
    if (selectedIds.length === 0) {
        alert('请选择要导出的数据');
        return;
    }
    
    const url = `/export/${format}?ids=${selectedIds.join(',')}`;
    window.open(url, '_blank');
}

// 搜索高亮
function highlightSearch(searchTerm) {
    if (!searchTerm) return;
    
    $('.table tbody td').each(function() {
        const text = $(this).text();
        if (text.toLowerCase().includes(searchTerm.toLowerCase())) {
            const highlightedText = text.replace(
                new RegExp(searchTerm, 'gi'),
                `<mark>$&</mark>`
            );
            $(this).html(highlightedText);
        }
    });
}

// 键盘快捷键
$(document).keydown(function(e) {
    // Ctrl+A 全选
    if (e.ctrlKey && e.keyCode === 65) {
        e.preventDefault();
        $('#selectAll').click();
    }
    
    // Ctrl+R 刷新
    if (e.ctrlKey && e.keyCode === 82) {
        e.preventDefault();
        location.reload();
    }
    
    // ESC 取消选择
    if (e.keyCode === 27) {
        $('input[type="checkbox"]').prop('checked', false);
    }
});

// 实时搜索
let searchTimeout;
$('#quick-search').on('input', function() {
    const searchTerm = $(this).val();
    
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(function() {
        if (searchTerm.length >= 2) {
            filterTableRows(searchTerm);
        } else {
            showAllTableRows();
        }
    }, 300);
});

function filterTableRows(searchTerm) {
    $('.table tbody tr').each(function() {
        const rowText = $(this).text().toLowerCase();
        if (rowText.includes(searchTerm.toLowerCase())) {
            $(this).show();
        } else {
            $(this).hide();
        }
    });
}

function showAllTableRows() {
    $('.table tbody tr').show();
}

// 页面加载完成后的初始化
$(window).on('load', function() {
    // 隐藏加载动画
    $('.loading-overlay').fadeOut();
    
    // 显示统计信息
    updateStatistics();
});

// 更新统计信息
function updateStatistics() {
    const totalRows = $('.table tbody tr').length;
    const selectedRows = $('input[name="commit_ids"]:checked').length;
    const pendingCount = $('.badge:contains("待确认")').length;
    const confirmedCount = $('.badge:contains("已确认")').length;
    
    $('#total-count').text(totalRows);
    $('#selected-count').text(selectedRows);
    $('#pending-count').text(pendingCount);
    $('#confirmed-count').text(confirmedCount);
}
