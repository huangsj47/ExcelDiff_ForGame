
"""Diff rendering helpers extracted from app.py."""

from __future__ import annotations

import json
import difflib
import html

from services.model_loader import get_runtime_model


def log_print(message, log_type='INFO', force=False):
    """Best-effort runtime logger proxy."""
    try:
        runtime_log = get_runtime_model('log_print')
        runtime_log(message, log_type, force=force)
    except Exception:
        pass


def render_excel_diff_html(merged_diff_data, file_path):
    """渲染Excel diff数据为HTML - 完全使用合并diff的样式和结构"""
    try:
        if not merged_diff_data or not merged_diff_data.get('sheets'):
            return "<div class='alert alert-warning'>Excel文件无变更数据</div>"

        sheets = merged_diff_data['sheets']
        # 使用与合并diff完全相同的HTML结构
        excel_html = f"""
        <!-- 引入合并diff的CSS文件 -->
        <link rel="stylesheet" href="/static/css/excel-diff-new.css?v=2.0">
        <link rel="stylesheet" href="/static/css/excel-scroll-fix.css?v=2.0">
        <!-- Excel合并diff显示 - 使用与单文件diff相同的结构 -->
        <div class="excel-diff-wrapper">
            <!-- Excel工作表标签容器 -->
            <div class="excel-sheet-tabs-container">
                <div id="excel-sheet-tabs" class="excel-sheet-tabs"></div>
            </div>
            <!-- Excel表格内容容器 -->
            <div id="excel-content" class="excel-content-area">
                <div class="excel-sheet-content active">
                    <div class="excel-table-container">
                        <!-- 表格内容将通过JavaScript动态生成 -->
                    </div>
                </div>
            </div>
        </div>
        <script>
        // 存储Excel diff数据到全局变量
        window.weeklyExcelDiffData = """ + json.dumps(merged_diff_data) + """;
        // 标记数据已准备好
        window.weeklyExcelDiffDataReady = true;
        console.log('📊 Excel数据已设置到window.weeklyExcelDiffData');
        console.log('📊 数据内容:', window.weeklyExcelDiffData);
        </script>
        <!-- 将初始化逻辑移到单独的script标签，确保在DOM插入后执行 -->
        <script>
        // 通知父页面数据已准备好，可以开始初始化
        if (typeof window.initWeeklyExcelDiffWhenReady === 'function') {
            window.initWeeklyExcelDiffWhenReady();
        }
        </script>
        """
        return excel_html

    except Exception as e:
        log_print(f"渲染Excel diff HTML失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染Excel diff失败: {str(e)}</div>"

def render_git_diff_content(diff_content, file_path, base_commit_id, latest_commit_id, config=None, diff_cache=None):
    """渲染Git diff内容为HTML，与现有单文件diff界面保持一致"""
    try:
        if not diff_content:
            return "<div class='alert alert-info'>文件无变更</div>"

        # 检查是否为删除文件（所有行都是删除行）
        is_deleted = is_deleted_file(diff_content)
        if is_deleted:
            # 渲染删除文件内容
            github_diff_html = render_deleted_file_content(diff_content, file_path, config, diff_cache)
        else:
            # 生成GitHub风格的diff内容
            github_diff_html = render_github_style_diff(diff_content)
        diff_html = f"""
        <div class="weekly-diff-content">
            <div class="file-diff-container">
                <div class="file-header">
                    <i class="fas fa-file-code me-2"></i>{file_path}
                </div>
                <div class="diff-content-wrapper">
                    {github_diff_html}
                </div>
            </div>
        </div>
        <style>
        .weekly-diff-content {{
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 13px;
        }}
        .file-diff-container {{
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
        }}
        .file-header {{
            background-color: #f6f8fa;
            padding: 8px 16px;
            border-bottom: 1px solid #d0d7de;
            font-weight: 600;
            font-size: 14px;
        }}
        .diff-content-wrapper {{
            background-color: #ffffff;
            max-height: 70vh;
            overflow-y: auto;
        }}
        /* 确保diff内容使用标准字体大小 */
        .weekly-diff-content .diff-container {{
            font-size: 13px;
        }}
        .weekly-diff-content .diff-line-content {{
            font-size: 13px;
            line-height: 22px;
        }}
        .weekly-diff-content .diff-line-number {{
            font-size: 12px;
        }}
        .weekly-diff-content .diff-line-sign {{
            color: #8b949e;
            width: 22px;
            min-width: 22px;
        }}
        .weekly-diff-content .text-diff-container {{
            font-size: 13px;
        }}
        .weekly-diff-content .text-diff-line {{
            font-size: 13px;
            line-height: 22px;
        }}
        .weekly-diff-content .text-diff-line-content {{
            font-size: 13px;
        }}
        .weekly-diff-content .text-diff-line-number {{
            font-size: 12px;
        }}
        </style>
        """
        return diff_html

    except Exception as e:
        log_print(f"渲染Git diff内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染diff失败: {str(e)}</div>"

def _build_inline_change_html(old_text, new_text):
    old_raw = str(old_text or "")
    new_raw = str(new_text or "")
    matcher = difflib.SequenceMatcher(None, old_raw, new_raw)
    old_parts = []
    new_parts = []
    changed = False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_seg = html.escape(old_raw[i1:i2])
        new_seg = html.escape(new_raw[j1:j2])
        if tag == 'equal':
            old_parts.append(old_seg)
            new_parts.append(new_seg)
        elif tag == 'delete':
            if old_seg:
                changed = True
                old_parts.append(f'<span class="diff-inline-removed">{old_seg}</span>')
        elif tag == 'insert':
            if new_seg:
                changed = True
                new_parts.append(f'<span class="diff-inline-added">{new_seg}</span>')
        elif tag == 'replace':
            if old_seg:
                changed = True
                old_parts.append(f'<span class="diff-inline-removed">{old_seg}</span>')
            if new_seg:
                changed = True
                new_parts.append(f'<span class="diff-inline-added">{new_seg}</span>')

    return ''.join(old_parts), ''.join(new_parts), changed


def _render_diff_content_cell(sign, code_html):
    safe_sign = html.escape(sign)
    return (
        f'<span class="diff-line-sign">{safe_sign}</span>'
        f'<span class="diff-line-code">{code_html}</span>'
    )


def _render_diff_row(row_class, old_num, new_num, sign, code_html):
    return (
        f'<tr class="diff-line {row_class}">'
        f'<td class="diff-line-number diff-line-number-old">{old_num}</td>'
        f'<td class="diff-line-number diff-line-number-new">{new_num}</td>'
        f'<td class="diff-line-content">{_render_diff_content_cell(sign, code_html)}</td>'
        f'</tr>'
    )


def render_github_style_diff(diff_content):
    """渲染GitHub风格的diff内容（含行内字符级高亮）。"""
    try:
        if not diff_content:
            return "<div class='alert alert-info'>文件无变更</div>"

        import re

        lines = diff_content.split('\n')
        html_content = []
        old_line_num = 0
        new_line_num = 0
        removed_buf = []
        added_buf = []

        def flush_buffers():
            nonlocal removed_buf, added_buf
            if not removed_buf and not added_buf:
                return

            pair_count = min(len(removed_buf), len(added_buf))
            pair_markup = []
            for idx in range(pair_count):
                old_html, new_html, changed = _build_inline_change_html(
                    removed_buf[idx][1],
                    added_buf[idx][1],
                )
                pair_markup.append((old_html, new_html, changed))

            for idx, (line_no, text) in enumerate(removed_buf):
                if idx < pair_count and pair_markup[idx][2]:
                    code_html = pair_markup[idx][0]
                else:
                    code_html = html.escape(text)
                html_content.append(
                    _render_diff_row("diff-line-removed", line_no, "", "-", code_html)
                )

            for idx, (line_no, text) in enumerate(added_buf):
                if idx < pair_count and pair_markup[idx][2]:
                    code_html = pair_markup[idx][1]
                else:
                    code_html = html.escape(text)
                html_content.append(
                    _render_diff_row("diff-line-added", "", line_no, "+", code_html)
                )

            removed_buf = []
            added_buf = []

        for line in lines:
            if line.startswith('@@'):
                flush_buffers()
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                    new_line_num = int(match.group(2)) - 1
                html_content.append(
                    '<tr class="diff-line diff-hunk-header">'
                    '<td class="diff-line-number diff-line-number-old"></td>'
                    '<td class="diff-line-number diff-line-number-new"></td>'
                    f'<td class="diff-line-content">{html.escape(line)}</td>'
                    '</tr>'
                )
            elif line.startswith('-'):
                old_line_num += 1
                removed_buf.append((old_line_num, line[1:]))
            elif line.startswith('+'):
                new_line_num += 1
                added_buf.append((new_line_num, line[1:]))
            elif line.startswith(' ') or (not line.startswith(('@@', '+', '-', '\\'))):
                flush_buffers()
                old_line_num += 1
                new_line_num += 1
                text = line[1:] if line.startswith(' ') else line
                html_content.append(
                    _render_diff_row("", old_line_num, new_line_num, " ", html.escape(text))
                )

        flush_buffers()

        return f"""
        <div class="diff-container">
            <table class="diff-table">
                <tbody>
                    {''.join(html_content)}
                </tbody>
            </table>
        </div>
        <style>
        .diff-container {{
            background: #fff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 13px;
        }}
        .diff-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}
        .diff-line-number {{
            width: 1%;
            min-width: 50px;
            padding: 0 8px;
            text-align: right;
            vertical-align: top;
            color: #656d76;
            background: #f6f8fa;
            border-right: 1px solid #d0d7de;
            user-select: none;
            font-size: 12px;
            line-height: 22px;
        }}
        .diff-line-content {{
            padding: 0 8px 0 6px;
            vertical-align: top;
            line-height: 22px;
            font-size: 13px;
            white-space: pre;
            display: flex;
            align-items: baseline;
        }}
        .diff-line-sign {{
            width: 22px;
            min-width: 22px;
            text-align: center;
            color: #8b949e;
            user-select: none;
            flex-shrink: 0;
        }}
        .diff-line-code {{
            flex: 1;
            color: #24292f;
            white-space: pre;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }}
        .diff-inline-added {{
            background: #2f6f44;
            color: #ffffff;
            border-radius: 2px;
        }}
        .diff-inline-removed {{
            background: #9a313c;
            color: #ffffff;
            border-radius: 2px;
        }}
        .diff-line-added {{
            background-color: #dafbe1 !important;
        }}
        .diff-line-added .diff-line-number {{
            background-color: #ccf2d4 !important;
            color: #24292f !important;
        }}
        .diff-line-removed {{
            background-color: #ffebe9 !important;
        }}
        .diff-line-removed .diff-line-number {{
            background-color: #ffd7d5 !important;
            color: #24292f !important;
        }}
        .diff-hunk-header {{
            background-color: #f1f8ff !important;
        }}
        .diff-hunk-header .diff-line-content {{
            color: #0969da;
            font-weight: 600;
        }}
        .diff-hunk-header .diff-line-sign {{
            color: transparent;
        }}
        </style>
        """
    except Exception as e:
        log_print(f"渲染GitHub风格diff失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染GitHub风格diff失败: {str(e)}</div>"

def is_deleted_file(diff_content):
    """检查是否为删除文件（所有内容行都是删除行）"""
    if not diff_content:
        return False

    lines = diff_content.split('\n')
    content_lines = []
    for line in lines:
        # 跳过hunk头部和其他元数据
        if line.startswith('@@') or line.startswith('\\') or not line.strip():
            continue

        # 收集内容行
        if line.startswith('+') or line.startswith('-') or line.startswith(' '):
            content_lines.append(line)
    # 如果没有内容行，不是删除文件
    if not content_lines:
        return False

    # 检查是否所有内容行都是删除行
    non_deleted_lines = []
    for line in content_lines:
        if not line.startswith('-'):
            non_deleted_lines.append(line)
    return len(non_deleted_lines) == 0

def render_deleted_file_content(diff_content, file_path, config=None, diff_cache=None):
    """渲染删除文件提示为HTML，显示文件已删除的信息"""
    try:
        # 解析diff内容获取基本信息
        lines = diff_content.split('\n') if diff_content else []
        deleted_lines_count = 0
        # 统计删除的行数
        for line in lines:
            if line.startswith('-') and not line.startswith('---'):
                deleted_lines_count += 1
        # 构建查看上一版本的URL
        previous_version_url = ""
        if config and diff_cache and diff_cache.base_commit_id:
            # 构建查看基准版本文件的URL
            previous_version_url = f"/weekly-version-config/{config.id}/file-previous-version?file_path={file_path}&commit_id={diff_cache.base_commit_id}"
        # 获取文件扩展名用于显示合适的图标
        file_extension = file_path.split('.')[-1].lower() if '.' in file_path else ''
        file_icon = get_file_icon(file_extension)
        # 获取删除的内容预览（前几行）
        deleted_content_preview = []
        for line in lines:
            if line.startswith('-') and not line.startswith('---'):
                deleted_content_preview.append(line[1:])  # 去掉'-'前缀
                if len(deleted_content_preview) >= 15:  # 显示前15行
                    break

        return f"""
        <div class="deleted-file-container">
            <!-- 主要删除提示区域 -->
            <div class="deleted-file-main">
                <div class="deleted-file-icon-wrapper">
                    <div class="deleted-file-icon">
                        <i class="fas fa-trash-alt"></i>
                    </div>
                    <div class="file-type-icon">
                        <i class="{file_icon}"></i>
                    </div>
                </div>
                <div class="deleted-file-info">
                    <h3 class="deleted-title">文件已删除</h3>
                    <p class="deleted-subtitle">该文件在此版本中被完全删除</p>
                    <div class="deleted-stats">
                        <div class="stat-item">
                            <i class="fas fa-file-alt me-2"></i>
                            <span class="stat-label">文件名：</span>
                            <code class="stat-value">{file_path.split('/')[-1]}</code>
                        </div>
                        <div class="stat-item">
                            <i class="fas fa-minus-circle me-2"></i>
                            <span class="stat-label">删除行数：</span>
                            <span class="stat-value text-danger">{deleted_lines_count} 行</span>
                        </div>
                    </div>
                </div>
            </div>
            <!-- 操作按钮区域 -->
            <div class="deleted-file-actions">
                <div class="action-buttons">
                    {f'''
                    <a href="{previous_version_url}" class="btn btn-primary btn-lg" target="_blank">
                        <i class="fas fa-history me-2"></i>查看上一版本
                    </a>
                    ''' if previous_version_url else '''
                    <button type="button" class="btn btn-primary btn-lg" disabled title="无法获取上一版本信息">
                        <i class="fas fa-history me-2"></i>查看上一版本
                    </button>
                    '''}
                    <button type="button" class="btn btn-outline-secondary btn-lg" onclick="showDeletedContent()">
                        <i class="fas fa-eye me-2"></i>显示删除内容
                    </button>
                </div>
                <div class="action-hint">
                    <i class="fas fa-info-circle me-2"></i>
                    点击"查看上一版本"可以查看删除前的完整文件内容
                </div>
            </div>
            <!-- 删除内容详情（默认隐藏） -->
            <div id="deletedContentDetails" style="display: none;" class="deleted-content-details">
                <div class="content-header">
                    <h5><i class="fas fa-code me-2"></i>删除的内容预览</h5>
                    <small class="text-muted">显示前 {min(len(deleted_content_preview), 15)} 行删除的内容</small>
                </div>
                <div class="deleted-content-preview">
                    <div class="code-container">
                        {''.join([f'<div class="code-line deleted-line"><div class="line-number">{i+1}</div><div class="line-text">{line if line.strip() else " "}</div></div>' for i, line in enumerate(deleted_content_preview)])}
                    </div>
                    {f'<div class="more-content-hint"><i class="fas fa-ellipsis-h me-2"></i>还有 {deleted_lines_count - len(deleted_content_preview)} 行内容被删除</div>' if deleted_lines_count > len(deleted_content_preview) else ''}
                </div>
            </div>
        </div>
        <style>
        .deleted-file-container {{
            background: linear-gradient(135deg, #fff9e6 0%, #fef7e0 100%);
            border: 1px solid #f0d000;
            border-radius: 8px;
            padding: 0;
            margin: 15px 0;
            box-shadow: 0 2px 8px rgba(240, 208, 0, 0.1);
            overflow: hidden;
            width: 100%;
        }}
        .deleted-file-main {{
            padding: 20px 15px;
            text-align: center;
            border-bottom: 1px solid rgba(240, 208, 0, 0.3);
        }}
        .deleted-file-icon-wrapper {{
            position: relative;
            display: inline-block;
            margin-bottom: 15px;
        }}
        .deleted-file-icon {{
            font-size: 1.5rem;
            color: #dc3545;
            margin-bottom: 8px;
            animation: pulse 2s infinite;
        }}
        .file-type-icon {{
            position: absolute;
            bottom: -3px;
            right: -6px;
            font-size: 0.72rem;
            color: #6c757d;
            background: white;
            border-radius: 50%;
            padding: 4px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.1);
        }}
        @keyframes pulse {{
            0% {{ transform: scale(1); }}
            50% {{ transform: scale(1.05); }}
            100% {{ transform: scale(1); }}
        }}
        .deleted-file-info {{
            max-width: 500px;
            margin: 0 auto;
        }}
        .deleted-title {{
            color: #dc3545;
            font-weight: 700;
            font-size: 1.4rem;
            margin-bottom: 8px;
        }}
        .deleted-subtitle {{
            color: #6c757d;
            font-size: 0.95rem;
            margin-bottom: 15px;
        }}
        .deleted-stats {{
            display: flex;
            justify-content: center;
            gap: 15px;
            flex-wrap: wrap;
        }}
        .stat-item {{
            display: flex;
            align-items: center;
            background: rgba(255, 255, 255, 0.7);
            padding: 8px 12px;
            border-radius: 6px;
            border: 1px solid rgba(240, 208, 0, 0.3);
            font-size: 0.9rem;
        }}
        .stat-item i {{
            color: #f0d000;
        }}
        .stat-label {{
            font-weight: 600;
            color: #495057;
            margin-right: 8px;
        }}
        .stat-value {{
            font-weight: 700;
        }}
        .deleted-file-actions {{
            padding: 15px 20px;
            text-align: center;
            background: rgba(255, 255, 255, 0.5);
        }}
        .action-buttons {{
            margin-bottom: 15px;
        }}
        .action-buttons .btn {{
            margin: 0 8px;
            padding: 8px 16px;
            font-weight: 600;
            border-radius: 6px;
            transition: all 0.3s ease;
            font-size: 0.9rem;
        }}
        .action-buttons .btn:hover:not(:disabled) {{
            transform: translateY(-1px);
            box-shadow: 0 3px 8px rgba(0,0,0,0.15);
        }}
        .action-hint {{
            color: #6c757d;
            font-size: 0.9rem;
            font-style: italic;
        }}
        .deleted-content-details {{
            background: rgba(255, 255, 255, 0.9);
            border-top: 1px solid rgba(240, 208, 0, 0.3);
            padding: 25px 30px;
        }}
        .content-header {{
            margin-bottom: 15px;
            text-align: left;
        }}
        .content-header h5 {{
            color: #495057;
            margin-bottom: 5px;
        }}
        .deleted-content-preview {{
            background: #fff;
            border: 1px solid #dee2e6;
            border-radius: 6px;
            padding: 0;
            max-height: 400px;
            overflow-y: auto;
            text-align: left;
        }}
        .deleted-content-preview .code-container {{
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 13px;
            line-height: 18.2px;
        }}
        .deleted-content-preview .code-line {{
            display: flex;
            align-items: stretch;
            min-height: 18.2px;
            background: #ffeef0;
            border-left: 3px solid #dc3545;
        }}
        .deleted-content-preview .code-line:hover {{
            background: #ffdddf;
        }}
        .deleted-content-preview .line-number {{
            background: #f8f9fa;
            color: #6c757d;
            padding: 0 8px;
            text-align: right;
            min-width: 40px;
            border-right: 1px solid #dee2e6;
            user-select: none;
            flex-shrink: 0;
        }}
        .deleted-content-preview .line-text {{
            padding: 0 8px;
            flex: 1;
            white-space: pre;
            color: #dc3545;
            overflow-x: auto;
        }}
        .more-content-hint {{
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid #dee2e6;
            color: #6c757d;
            font-style: italic;
            text-align: center;
        }}
        /* 响应式设计 */
        @media (max-width: 768px) {{
            .deleted-file-main {{
                padding: 30px 20px;
            }}
            .deleted-stats {{
                flex-direction: column;
                gap: 15px;
            }}
            .action-buttons .btn {{
                display: block;
                width: 100%;
                margin: 5px 0;
            }}
        }}
        </style>
        """
    except Exception as e:
        log_print(f"渲染删除文件内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染删除文件内容失败: {str(e)}</div>"

def get_file_icon(file_extension):
    """根据文件扩展名返回合适的图标"""
    icon_map = {
        'lua': 'fas fa-code',
        'py': 'fab fa-python',
        'js': 'fab fa-js-square',
        'html': 'fab fa-html5',
        'css': 'fab fa-css3-alt',
        'json': 'fas fa-brackets-curly',
        'xml': 'fas fa-code',
        'txt': 'fas fa-file-alt',
        'md': 'fab fa-markdown',
        'yml': 'fas fa-cog',
        'yaml': 'fas fa-cog',
        'sql': 'fas fa-database',
        'sh': 'fas fa-terminal',
        'bat': 'fas fa-terminal',
        'exe': 'fas fa-cog',
        'dll': 'fas fa-cog',
        'png': 'fas fa-image',
        'jpg': 'fas fa-image',
        'jpeg': 'fas fa-image',
        'gif': 'fas fa-image',
        'pdf': 'fas fa-file-pdf',
        'doc': 'fas fa-file-word',
        'docx': 'fas fa-file-word',
        'xls': 'fas fa-file-excel',
        'xlsx': 'fas fa-file-excel',
    }
    return icon_map.get(file_extension, 'fas fa-file')

def render_deleted_content_details(diff_content):
    """渲染删除文件的详细内容，用于在点击时显示"""
    try:
        if not diff_content:
            return "<div class='alert alert-info'>无删除内容</div>"

        lines = diff_content.split('\n')
        html_content = []
        old_line_num = 0
        for line in lines:
            if line.startswith('@@'):
                # 解析hunk头部信息
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                # 渲染hunk头部
                html_content.append(f"""
                    <tr class="diff-line diff-hunk-header">
                        <td class="diff-line-number diff-line-number-old"></td>
                        <td class="diff-line-number diff-line-number-new"></td>
                        <td class="diff-line-content">{line}</td>
                    </tr>
                """)
            elif line.startswith('-'):
                old_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_content.append(f"""
                    <tr class="diff-line diff-line-removed">
                        <td class="diff-line-number diff-line-number-old">{old_line_num}</td>
                        <td class="diff-line-number diff-line-number-new"></td>
                        <td class="diff-line-content">-{line_content}</td>
                    </tr>
                """)
        return f"""
        <div class="diff-container">
            <table class="diff-table">
                <tbody>
                    {''.join(html_content)}
                </tbody>
            </table>
        </div>
        <style>
        .diff-container {{
            background: #fff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}
        .diff-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}
        .diff-line-number {{
            width: 1%;
            min-width: 50px;
            padding: 0 8px;
            text-align: right;
            vertical-align: top;
            color: #656d76;
            background: #f6f8fa;
            border-right: 1px solid #d0d7de;
            user-select: none;
            font-size: 12px;
            line-height: 20px;
        }}
        .diff-line-content {{
            padding: 0 8px;
            vertical-align: top;
            white-space: pre;
            word-wrap: break-word;
            overflow-wrap: break-word;
            line-height: 20px;
            font-size: 12px;
        }}
        .diff-line-removed {{
            background-color: #ffebe9 !important;
        }}
        .diff-line-removed .diff-line-number {{
            background-color: #ffd7d5 !important;
            color: #24292f !important;
        }}
        .diff-hunk-header {{
            background-color: #f1f8ff !important;
        }}
        .diff-hunk-header .diff-line-content {{
            color: #0969da;
            font-weight: 600;
        }}
        </style>
        """
    except Exception as e:
        return f"<div class='alert alert-danger'>渲染删除内容详情失败: {str(e)}</div>"

def render_new_file_content(file_content, file_path, commit_id):
    """渲染新文件内容为HTML，使用GitHub风格"""
    try:
        if not file_content:
            return "<div class='alert alert-info'>文件为空</div>"

        lines = file_content.split('\n')
        html_content = []
        for i, line in enumerate(lines, 1):
            # HTML转义
            line_content = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            html_content.append(f"""
                <tr class="diff-line diff-line-added">
                    <td class="diff-line-number diff-line-number-old"></td>
                    <td class="diff-line-number diff-line-number-new">{i}</td>
                    <td class="diff-line-content">{line_content}</td>
                </tr>
            """)
        diff_html = f"""
        <div class="weekly-diff-content">
            <div class="file-diff-container">
                <div class="file-header">
                    <i class="fas fa-file-plus me-2"></i>{file_path}
                </div>
                <div class="diff-content-wrapper">
                    <div class="diff-container">
                        <table class="diff-table">
                            <tbody>
                                {''.join(html_content)}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
        <style>
        .weekly-diff-content {{
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}
        .file-diff-container {{
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
        }}
        .file-header {{
            background-color: #f6f8fa;
            padding: 8px 16px;
            border-bottom: 1px solid #d0d7de;
            font-weight: 600;
            font-size: 14px;
        }}
        .diff-content-wrapper {{
            background-color: #ffffff;
            max-height: 70vh;
            overflow-y: auto;
        }}
        .diff-container {{
            background: #fff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 12px;
        }}
        .diff-table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}
        .diff-line-number {{
            width: 1%;
            min-width: 50px;
            padding: 0 8px;
            text-align: right;
            vertical-align: top;
            color: #656d76;
            background: #f6f8fa;
            border-right: 1px solid #d0d7de;
            user-select: none;
            font-size: 12px;
            line-height: 20px;
        }}
        .diff-line-content {{
            padding: 0 8px;
            vertical-align: top;
            white-space: pre;
            word-wrap: break-word;
            overflow-wrap: break-word;
            line-height: 20px;
            font-size: 12px;
        }}
        .diff-line-added {{
            background-color: #dafbe1 !important;
        }}
        .diff-line-added .diff-line-number {{
            background-color: #ccf2d4 !important;
            color: #24292f !important;
        }}
        .diff-line-removed {{
            background-color: #ffebe9 !important;
        }}
        .diff-line-removed .diff-line-number {{
            background-color: #ffd7d5 !important;
            color: #24292f !important;
        }}
        </style>
        """
        return diff_html

    except Exception as e:
        log_print(f"渲染新文件内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>渲染失败: {str(e)}</div>"

def parse_and_render_diff(diff_content):
    """解析并渲染diff内容"""
    try:
        lines = diff_content.split('\n')
        html_lines = []
        old_line_num = 0
        new_line_num = 0
        for line in lines:
            if line.startswith('@@'):
                # 解析hunk头部信息
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                    new_line_num = int(match.group(2)) - 1
                html_lines.append(f"""
                    <div class="diff-line diff-hunk-header">
                        <span class="line-number"></span>
                        <span class="line-number"></span>
                        <span class="line-content">{line}</span>
                    </div>
                """)
            elif line.startswith('-'):
                old_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_lines.append(f"""
                    <div class="diff-line diff-removed">
                        <span class="line-number">{old_line_num}</span>
                        <span class="line-number"></span>
                        <span class="line-content">-{line_content}</span>
                    </div>
                """)
            elif line.startswith('+'):
                new_line_num += 1
                line_content = line[1:].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_lines.append(f"""
                    <div class="diff-line diff-added">
                        <span class="line-number"></span>
                        <span class="line-number">{new_line_num}</span>
                        <span class="line-content">+{line_content}</span>
                    </div>
                """)
            elif line.startswith(' ') or (not line.startswith(('@@', '+', '-', '\\'))):
                old_line_num += 1
                new_line_num += 1
                line_content = line[1:] if line.startswith(' ') else line
                line_content = line_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                html_lines.append(f"""
                    <div class="diff-line diff-context">
                        <span class="line-number">{old_line_num}</span>
                        <span class="line-number">{new_line_num}</span>
                        <span class="line-content"> {line_content}</span>
                    </div>
                """)
        return f"""
        <div class="diff-content">
            {''.join(html_lines)}
        </div>
        <style>
        .diff-content {{
            font-size: 13px;
        }}
        .diff-line {{
            display: flex;
            line-height: 20px;
            min-height: 20px;
        }}
        .diff-line:hover {{
            background-color: rgba(255, 255, 0, 0.1);
        }}
        .line-number {{
            background-color: #f6f8fa;
            color: #656d76;
            padding: 0 8px;
            text-align: right;
            min-width: 50px;
            border-right: 1px solid #d1d9e0;
            user-select: none;
            font-size: 12px;
        }}
        .line-content {{
            padding: 0 8px;
            flex: 1;
            white-space: pre;
        }}
        .diff-added {{
            background-color: #e6ffed;
        }}
        .diff-removed {{
            background-color: #ffeef0;
        }}
        .diff-context {{
            background-color: #ffffff;
        }}
        .diff-hunk-header {{
            background-color: #f1f8ff;
            color: #0366d6;
            font-weight: bold;
        }}
        </style>
        """
    except Exception as e:
        log_print(f"解析diff内容失败: {e}", 'ERROR', force=True)
        return f"<div class='alert alert-danger'>解析diff失败: {str(e)}</div>"

def generate_side_by_side_diff(current_content, previous_content):
    """生成Git风格的并排diff数据"""
    import difflib
    if not current_content:
        current_content = ""
    if not previous_content:
        previous_content = ""
    current_lines = current_content.splitlines()
    previous_lines = previous_content.splitlines()
    # 使用SequenceMatcher进行更精确的diff
    matcher = difflib.SequenceMatcher(None, previous_lines, current_lines)
    left_lines = []  # 左侧（前一版本）
    right_lines = []  # 右侧（当前版本）
    left_line_num = 1
    right_line_num = 1
    # 处理所有的操作块
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            # 相同的行
            for i in range(i1, i2):
                left_lines.append({
                    'line_num': left_line_num,
                    'content': previous_lines[i],
                    'type': 'context'
                })
                right_lines.append({
                    'line_num': right_line_num,
                    'content': current_lines[j1 + (i - i1)],
                    'type': 'context'
                })
                left_line_num += 1
                right_line_num += 1
        elif tag == 'delete':
            # 删除的行（只在左侧显示）
            for i in range(i1, i2):
                left_lines.append({
                    'line_num': left_line_num,
                    'content': previous_lines[i],
                    'type': 'removed'
                })
                right_lines.append({
                    'line_num': None,
                    'content': '',
                    'type': 'empty'
                })
                left_line_num += 1
        elif tag == 'insert':
            # 插入的行（只在右侧显示）
            for j in range(j1, j2):
                left_lines.append({
                    'line_num': None,
                    'content': '',
                    'type': 'empty'
                })
                right_lines.append({
                    'line_num': right_line_num,
                    'content': current_lines[j],
                    'type': 'added'
                })
                right_line_num += 1
        elif tag == 'replace':
            # 替换的行
            max_lines = max(i2 - i1, j2 - j1)
            for k in range(max_lines):
                # 左侧（删除的行）
                if k < (i2 - i1):
                    left_lines.append({
                        'line_num': left_line_num,
                        'content': previous_lines[i1 + k],
                        'type': 'removed'
                    })
                    left_line_num += 1
                else:
                    left_lines.append({
                        'line_num': None,
                        'content': '',
                        'type': 'empty'
                    })
                # 右侧（添加的行）
                if k < (j2 - j1):
                    right_lines.append({
                        'line_num': right_line_num,
                        'content': current_lines[j1 + k],
                        'type': 'added'
                    })
                    right_line_num += 1
                else:
                    right_lines.append({
                        'line_num': None,
                        'content': '',
                        'type': 'empty'
                    })
    return {
        'left_lines': left_lines,
        'right_lines': right_lines
    }

