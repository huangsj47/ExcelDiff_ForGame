#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一张 diff 表单独渲染出来截图（无头 Chrome，真 CSS + 真模块）。

**为什么要一个这样的脚本**：表体的表现散在四份 CSS 与一个共享 JS 模块里（行号格
宽度、冻结列、上下两格行号的底色、表头块的段标题……），改完「读 CSS 猜效果」很容易
看漏；而且不同页面各自加载不同的样式表，只有真渲染才能看出最终生效的是哪一版。

它会渲染四种形态，一张图里对齐看：
  1. 普通修改行（两版行号相同）；
  2. 插过一行的修改行（两版行号不同 → 上下两格）；
  3. 表头块（第 2..N 行）有改动 / 没改动；
  4. **名称行 = 2** 的表：表头块里第 1 行是那行大标题（列名取自第 2 行，
     所以列头显示的是字段名而不是 `Unnamed: 1`）。

用法：python scripts/shot_diff_table_probe.py [输出文件名]
"""
import json
import re
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover —— 开发工具，不是项目依赖
    raise SystemExit(
        "这个脚本要 playwright（pip install playwright && playwright install chrome）。"
        "它只在**改样式时手动跑**，不参与测试与 CI。"
    )

ROOT = Path(__file__).resolve().parents[1]

CSS_FILES = (
    'static/css/style.css',
    'static/css/diff-styles.css',
    'static/css/excel-diff-new.css',
    'static/css/excel-scroll-fix.css',
    'static/css/diff-table-ux.css',
)

SHEET = {
    'headers': ['id', '名字', '备注', '仅表头有值'],
    'header_rows': [
        {'row_number': 2, 'status': 'modified', 'data': {'id': '编号', '名字': '名字', '备注': '备注', '仅表头有值': '表头专属'},
         'cell_changes': [{'column': '名字', 'old_value': '名称', 'new_value': '名字'}]},
        {'row_number': 3, 'status': 'unchanged', 'data': {'id': 'ID', '名字': 'Name', '备注': 'Desc', '仅表头有值': ''}},
    ],
    'header_changes': (
        [{'change': 'added', 'column': '室外总威胁值', 'column_index': 5},
         {'change': 'added', 'column': '室内总威胁值', 'column_index': 6}]
        + [{'change': 'removed', 'column': f'旧列{i}', 'old_name': f'旧列{i}', 'column_index': 2 + i}
           for i in range(6)]
        + [{'change': 'renamed', 'column': '名字', 'old_name': '名称', 'new_name': '名字', 'column_index': 3}]
    ),
    'rows': [
        {'row_number': 9, 'status': 'removed', 'data': {'id': '7', '名字': '删', '备注': '', '仅表头有值': ''}},
        {'row_number': 11, 'previous_row_number': 12, 'status': 'modified',
         'data': {'id': '2', '名字': '乙', '备注': 'y', '仅表头有值': ''},
         'cell_changes': [{'column': '名字', 'old_value': 'b', 'new_value': '乙'}]},
        {'row_number': 26, 'previous_row_number': 26, 'status': 'modified',
         'data': {'id': '3', '名字': '丙', '备注': '150', '仅表头有值': ''},
         'cell_changes': [{'column': '备注', 'old_value': '160', 'new_value': '150'}]},
        {'row_number': 30, 'status': 'added', 'data': {'id': '9', '名字': '新', '备注': 'z', '仅表头有值': ''}},
    ],
    'stats': {'total_rows_current': 3, 'total_rows_previous': 3, 'added': 1, 'removed': 1, 'modified': 2},
}

# 「第 1 行是大标题、第 2 行才是字段名」的表（仓库配了「名称行 = 2」）。
# 第 1 行在表头块里，列头用的是第 2 行的字段名 —— 空的那些格子显示为空，
# 而不是 pandas 起的 `Unnamed: 1`。
TITLED_SHEET = {
    'headers': ['id', '名字', '备注', 'icon'],
    'header_rows': [
        {'row_number': 1, 'status': 'modified',
         'data': {'id': '道具配置表 v2', '名字': '', '备注': '', 'icon': ''},
         'cell_changes': [{'column': 'id', 'old_value': '道具配置表', 'new_value': '道具配置表 v2'}]},
        {'row_number': 3, 'status': 'unchanged',
         'data': {'id': 'ID', '名字': 'Name', '备注': 'Desc', 'icon': 'Icon'}},
    ],
    'rows': [
        {'row_number': 5, 'previous_row_number': 5, 'status': 'modified',
         'data': {'id': '2', '名字': '乙', '备注': 'y', 'icon': 'i2'},
         'cell_changes': [{'column': '名字', 'old_value': 'b', 'new_value': '乙'}]},
    ],
    'stats': {'total_rows_current': 1, 'total_rows_previous': 1, 'added': 0, 'removed': 0, 'modified': 1},
}


def _extract(path, name):
    text = (ROOT / path).read_text(encoding='utf-8')
    match = re.search(r'function %s\s*\(' % re.escape(name), text)
    depth = 0
    for index in range(match.start(), len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[match.start():index + 1]
    raise AssertionError(name)


def _page_html() -> str:
    css = '\n'.join(f'<style>{(ROOT / name).read_text(encoding="utf-8")}</style>'
                    for name in CSS_FILES)
    module = (ROOT / 'static/js/excel_diff_table.js').read_text(encoding='utf-8')
    # 列变更提示那一份来自周版本页（三张页面逐字节一致）
    header_changes_fn = _extract('templates/weekly_version_full_diff.html', 'headerChangesHtml')
    escape_fn = _extract('templates/weekly_version_full_diff.html', 'escapeHtml')
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
{css}
</head><body style="background:#fff; padding:16px; font-family:'Segoe UI',Tahoma,sans-serif;">
<div id="notices"></div>
<div id="host"></div>
<script>{escape_fn}
{header_changes_fn}
{module}</script>
<script>
// innerHTML 与页面里一样是赋值进来的（模块自己转义）
function escapeHtml(value) {{
    return String(value === undefined || value === null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}}
window.formatCellValue = function (value) {{
    if (value === null || value === undefined) return '';
    return String(value);
}};
const sheets = {json.dumps([SHEET, TITLED_SHEET], ensure_ascii=False)};
document.getElementById('notices').innerHTML = sheets.map(headerChangesHtml).join('');
sheets.forEach(function (sheet, index) {{
    const host = document.createElement('div');
    host.style.marginTop = index ? '28px' : '0';
    document.getElementById('host').appendChild(host);
    window.ExcelDiffTable.mountSheetTable(host, sheet,
        {{sheetName: index ? '名称行=2 的表' : 'S'}});
}});
</script></body></html>"""


def main(out_name: str = 'diff_table_probe.png') -> int:
    out_dir = ROOT / '.pytest_tmp'
    out_dir.mkdir(exist_ok=True)
    page_path = out_dir / 'diff_table_probe.html'
    page_path.write_text(_page_html(), encoding='utf-8')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome')
        page = browser.new_page(viewport={'width': 1180, 'height': 900})
        page.goto(page_path.as_uri(), wait_until='networkidle')
        page.wait_for_timeout(400)
        page.screenshot(path=str(out_dir / out_name), full_page=True)
        # 窄屏：确认两格行号与表头块不把表格撑出横向滚动
        page.set_viewport_size({'width': 420, 'height': 900})
        page.wait_for_timeout(300)
        page.screenshot(path=str(out_dir / 'diff_table_probe_narrow.png'), full_page=True)
        browser.close()

    print(out_dir / out_name)
    print(out_dir / 'diff_table_probe_narrow.png')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(*(sys.argv[1:2] or [])))
