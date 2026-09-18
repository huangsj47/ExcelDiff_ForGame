"""
Excel HTML缓存服务
提供Excel差异结果的HTML缓存功能，包括HTML内容和CSS样式
"""
import hashlib
import json
import threading
import traceback
from datetime import datetime, timedelta
from html import escape
from typing import Any, Dict, Optional, Tuple

from flask import render_template
from sqlalchemy import func

from services.model_loader import get_runtime_models
from utils.diff_data_utils import format_cell_value

# 与 services/excel_diff_cache_service.py 的 TRUNCATED_NOTICE 保持一致：
# 那边负责「把截断结果单独标记出来」，这边负责「把它显示出来」。
# 两边一旦不同，用户看到的就是两套说法，所以文案只在这里再声明一份常量并写清来源。
TRUNCATED_NOTICE = '文件过大，未完整比对'

# HTML 缓存元数据里记录基线的键（previous_commit_id 是缓存键的一部分，
# 见 services/excel_diff_cache_service.py 文件头的「基线」注释块）
BASELINE_METADATA_KEY = 'previous_commit_id'

# 元数据里没有基线键 / 基线解析不出来时的哨兵：含义是「本次不校验」。
# 它必须区别于 None —— None 是「这个 commit 没有基线（新增文件）」，
# 是**要求** previous 为空的有效取值。
BASELINE_UNKNOWN = object()

# 调用方**没有交代**基线（老调用方）时的哨兵：这时才自己去解析权威基线。
# 语义与 services/excel_diff_cache_service.py 的 BASELINE_UNSET 一一对应。
BASELINE_UNSET = object()


def payload_is_truncated(diff_data) -> bool:
    """diff_data 是否只是「文件过大」的摘要（行明细已被丢弃）。

    截断负载的 sheets 里 rows 是空的，直接渲染就会落到模板的
    「工作表 "X" 没有数据或无变更」分支 —— 把「没比完」显示成「没改动」。
    """
    if not isinstance(diff_data, dict):
        return False
    return bool(diff_data.get('truncated'))


class ExcelHtmlCacheService:
    """Excel HTML缓存服务类"""
    
    def __init__(self, db, diff_logic_version):
        self.db = db
        self.diff_logic_version = diff_logic_version
        self.current_version = diff_logic_version
        self.processing_cache = set()
        self._lock = threading.Lock()
        # 缓存动态导入的模型引用，避免每个方法都重复调用 get_runtime_models (#38)
        self._models_cache: Dict[str, Any] = {}

    def _log_exception(self, context: str, exc: Exception, category: str = "CACHE"):
        """统一异常日志：输出异常类型、消息和完整堆栈。"""
        detail = f"{context}: {type(exc).__name__}: {exc}"
        stack = traceback.format_exc()
        try:
            from utils.logger import log_print
            log_print(f"{detail}\n{stack}", category, force=True)
        except Exception:
            print(f"[ERROR] {detail}\n{stack}")

    def _log_message(self, message: str, category: str = "CACHE"):
        """无异常对象的日志出口（与 _log_exception 走同一个 log_print）。"""
        try:
            from utils.logger import log_print
            log_print(message, category, force=True)
        except Exception:
            print(f"[WARN] {message}")

    def _resolve_baseline_for_cache(self, repository_id: int, commit_id: str, file_path: str):
        """解析权威基线（与 ExcelDiffCacheService 同源），失败返回 BASELINE_UNKNOWN。

        HTML 缓存同样不能跨基线复用：同一 commit 用不同 previous 渲染出的 HTML
        长得一样、但内容完全不同。解析不出来时必须「放弃校验」而不是当成
        「基线为空」，否则 HTML 缓存会永远不命中。
        """
        try:
            # 延迟导入：避免 excel_diff_cache_service → commit_diff_logic 这条
            # 较重的依赖链在模块导入期被拉进来（也规避潜在循环导入）。
            from services.excel_diff_cache_service import (
                normalize_baseline_id,
                resolve_authoritative_baseline,
            )
        except Exception as exc:
            self._log_exception("导入基线解析器失败，本次不校验HTML缓存基线", exc)
            return BASELINE_UNKNOWN
        try:
            resolved, baseline = resolve_authoritative_baseline(repository_id, commit_id, file_path)
        except Exception as exc:
            self._log_exception("解析HTML缓存基线失败，本次不校验基线", exc)
            return BASELINE_UNKNOWN
        if not resolved:
            return BASELINE_UNKNOWN
        return normalize_baseline_id(baseline)

    # 与 diff 缓存同源的基线归一化（None / 空串 → None）
    @staticmethod
    def _normalize_baseline(value):
        if value is None or value is BASELINE_UNKNOWN:
            return value
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_model_results(names, resolved):
        """规范化 get_runtime_models 返回值，兼容单模型 tuple/plain object。"""
        if len(names) == 1:
            if isinstance(resolved, (tuple, list)):
                if len(resolved) != 1:
                    raise RuntimeError(
                        f"get_runtime_models 单对象返回数量异常: expected=1 actual={len(resolved)} names={names}"
                    )
                return (resolved[0],)
            return (resolved,)

        if not isinstance(resolved, (tuple, list)):
            raise RuntimeError(
                f"get_runtime_models 多对象返回类型异常: expected tuple/list actual={type(resolved).__name__} names={names}"
            )
        if len(resolved) != len(names):
            raise RuntimeError(
                f"get_runtime_models 多对象返回数量异常: expected={len(names)} actual={len(resolved)} names={names}"
            )
        return tuple(resolved)

    def _get_model(self, *names):
        """获取并缓存动态模型引用 (#38)"""
        try:
            missing = [n for n in names if n not in self._models_cache]
            if missing:
                models = get_runtime_models(*missing)
                normalized_models = self._normalize_model_results(missing, models)
                for name, model in zip(missing, normalized_models):
                    self._models_cache[name] = model
            if len(names) == 1:
                return self._models_cache[names[0]]
            return tuple(self._models_cache[n] for n in names)
        except Exception as e:
            self._log_exception(f"加载运行时模型失败 names={names}", e)
            raise

    def generate_cache_key(self, repository_id: int, commit_id: str, file_path: str) -> str:
        """生成缓存键（SHA-256）"""
        key_data = f"{repository_id}:{commit_id}:{file_path}:{self.diff_logic_version}"
        return hashlib.sha256(key_data.encode('utf-8')).hexdigest()
    
    def get_cached_html(self, repository_id: int, commit_id: str, file_path: str,
                        previous_commit_id=BASELINE_UNSET) -> Optional[Dict[str, Any]]:
        """获取缓存的HTML内容。

        `previous_commit_id` 是**调用方这次实际要用的基线**。传了就以它为准做校验；
        没传才退回「自己解析权威基线」。少了这个参数，接口会拿到别的基线渲染的 HTML
        （线上 6767：页面同一时刻是对的，接口那条链却渲染出只存在于更晚版本的旧值）。
        """
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                # 这里按 (repository_id, commit_id, file_path, diff_version) 查，
                # 不用 generate_cache_key()：那个 key 在**写入**侧才用得上，
                # 早先这里算过一遍但从没被读过（死赋值）。
                cache_record = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version,
                    cache_status='completed'
                ).first()
            
                if cache_record:
                    # 在 session 上下文内提取所有属性，避免 DetachedInstanceError
                    metadata = json.loads(cache_record.cache_metadata) if cache_record.cache_metadata else {}
                    # 基线校验：同一条 (repo, commit, file) 用不同 previous 渲染出来的
                    # HTML 完全不同，却会命中同一行缓存 —— 必须比对元数据里记录的基线。
                    stored_baseline = metadata.get(BASELINE_METADATA_KEY, BASELINE_UNKNOWN)
                    expected_baseline = (
                        self._normalize_baseline(previous_commit_id)
                        if previous_commit_id is not BASELINE_UNSET
                        else self._resolve_baseline_for_cache(repository_id, commit_id, file_path)
                    )
                    if (
                        stored_baseline is not BASELINE_UNKNOWN
                        and expected_baseline is not BASELINE_UNKNOWN
                        and self._normalize_baseline(stored_baseline)
                        != self._normalize_baseline(expected_baseline)
                    ):
                        self._log_message(
                            "⚠️ HTML缓存基线不一致，按未命中处理（避免复用别的基线渲染的HTML）: "
                            f"file={file_path}, commit={commit_id}, "
                            f"缓存基线={stored_baseline or '(无)'}, 本次要求={expected_baseline or '(无)'}"
                        )
                        return None
                    result = {
                        'html_content': cache_record.html_content,
                        'css_content': cache_record.css_content,
                        'js_content': cache_record.js_content,
                        'metadata': metadata,
                        'created_at': cache_record.created_at,
                        'from_cache': True
                    }
                    return result
                else:
                    return None
                
        except Exception as e:
            self._log_exception(
                f"获取HTML缓存失败 repository_id={repository_id}, commit_id={commit_id}, file_path={file_path}",
                e
            )
            return None
    
    def save_html_cache(self, repository_id: int, commit_id: str, file_path: str,
                       html_content: str, css_content: str = "", js_content: str = "",
                       metadata: Dict[str, Any] = None,
                       previous_commit_id=BASELINE_UNSET) -> bool:
        """保存HTML缓存。

        元数据里记的必须是**渲染这份 HTML 时真正用的那个基线**（调用方传进来）。
        历史上这里自己解析「权威基线」再记下来 —— 渲染用的却是调用方给的那一个，
        两者不一致时校验形同虚设：错配的 HTML 会被当成「基线正确」长期命中。
        """
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                cache_key = self.generate_cache_key(repository_id, commit_id, file_path)

                # 把「这份 HTML 是按哪个基线渲染的」写进元数据，供 get_cached_html 校验。
                # 不改调用方传进来的 dict（它们可能复用同一个 dict），复制一份再补键。
                persisted_metadata = dict(metadata) if metadata else {}
                baseline = (
                    self._normalize_baseline(previous_commit_id)
                    if previous_commit_id is not BASELINE_UNSET
                    else self._resolve_baseline_for_cache(repository_id, commit_id, file_path)
                )
                if baseline is not BASELINE_UNKNOWN:
                    persisted_metadata[BASELINE_METADATA_KEY] = baseline
                metadata_json = json.dumps(persisted_metadata) if persisted_metadata else None

                existing_cache = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_version=self.diff_logic_version
                ).first()

                if existing_cache:
                    existing_cache.html_content = html_content
                    existing_cache.css_content = css_content
                    existing_cache.js_content = js_content
                    existing_cache.cache_metadata = metadata_json
                    existing_cache.cache_status = 'completed'
                    existing_cache.updated_at = datetime.utcnow()
                else:
                    new_cache = ExcelHtmlCache(
                        repository_id=repository_id,
                        commit_id=commit_id,
                        file_path=file_path,
                        cache_key=cache_key,
                        html_content=html_content,
                        css_content=css_content,
                        js_content=js_content,
                        cache_metadata=metadata_json,
                        cache_status='completed',
                        diff_version=self.diff_logic_version
                    )
                    self.db.session.add(new_cache)
                    
                self.db.session.commit()
                return True
            
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception(
                f"保存HTML缓存失败 repository_id={repository_id}, commit_id={commit_id}, file_path={file_path}",
                e
            )
            return False
    
    def generate_excel_html(self, diff_data: Dict[str, Any]) -> Tuple[str, str, str]:
        """根据Excel差异数据生成HTML内容"""
        try:
            if not diff_data or diff_data.get('type') != 'excel':
                raise ValueError("无效的Excel差异数据")

            html_content = self._render_excel_diff_html(diff_data)
            css_content = self._generate_excel_diff_css()
            js_content = self._generate_excel_diff_js()

            return html_content, css_content, js_content

        except Exception as e:
            self._log_exception("生成Excel HTML失败", e)
            raise

    def _render_truncated_notice_html(self, diff_data: Dict[str, Any]) -> str:
        """截断负载的专用渲染：明确说「文件过大，未完整比对」。

        不再把空 rows 交给模板 —— 那样每个 sheet 都会显示「没有数据或无变更」，
        与「文件真的没改动」在界面上完全同形，用户会被误导。
        """
        original_size = diff_data.get('original_size_mb')
        max_size = diff_data.get('max_size_mb')
        size_hint = ''
        if original_size:
            size_hint = f'（原始 diff 约 {original_size} MB'
            if max_size:
                size_hint += f'，超过 {max_size} MB 的缓存上限'
            size_hint += '）'
        detail = diff_data.get('error') or ''
        return (
            '<div class="excel-diff-wrapper">'
            '<div class="alert alert-warning excel-truncated-notice" data-truncated="true">'
            f'<i class="bi bi-exclamation-triangle me-2"></i>'
            f'<strong>{TRUNCATED_NOTICE}</strong>{size_hint}'
            f'<div class="mt-1 small">{detail}</div>'
            '</div>'
            '</div>'
        )

    def _render_excel_diff_html(self, diff_data: Dict[str, Any]) -> str:
        """渲染Excel差异HTML模板"""
        try:
            # 截断负载走专用分支：绝不能落到「没有数据或无变更」的空表格分支
            if payload_is_truncated(diff_data):
                return self._render_truncated_notice_html(diff_data)

            from flask import current_app
            
            with current_app.app_context():
                html_content = render_template(
                    'diff_partials/excel_diff.html',
                    diff_data=diff_data,
                    file_path=diff_data.get('file_path', ''),
                    sheets=diff_data.get('sheets', {}),
                    summary=diff_data.get('summary', {})
                )
                return html_content
                
        except Exception as e:
            self._log_exception("渲染Excel差异模板失败，回退简单HTML", e)
            # 如果模板渲染失败，生成简单的HTML结构
            return self._generate_simple_excel_html(diff_data)
    
    def _generate_simple_excel_html(self, diff_data: Dict[str, Any]) -> str:
        """生成简单的Excel差异HTML结构。

        这是 `render_template('diff_partials/excel_diff.html')` 抛异常时的**兜底**路径。
        它拼的是 HTML 字符串，所以两件事必须和模板一致：

        * **转义**：表名、列名、单元格值全部来自被审核的 Excel，是不可信内容。
          原实现直接 `f'<td>{value}</td>'` —— 单元格里写 `<img src=x onerror=…>`
          就会被 innerHTML 当标签执行；写 `<b>` 则表头/正文直接错位。
        * **展示口径**：单元格走 `format_cell_value`（与主路径同一个函数），
          否则同一个单元格在正常渲染与兜底渲染下显示不同 —— 兜底路径本来就是
          「主路径坏了」的时候才走，再显示成另一个样子会让审核者更难判断。
        """
        html_parts = ['<div class="excel-diff-container">']

        file_path = format_cell_value(diff_data.get('file_path', ''))
        html_parts.append(f'<div class="file-header"><h3>Excel文件差异: {escape(file_path)}</h3></div>')

        summary = diff_data.get('summary', {})
        if summary:
            html_parts.append('<div class="diff-summary">')
            html_parts.append(f'<span class="added">新增: {summary.get("added", 0)}</span>')
            html_parts.append(f'<span class="removed">删除: {summary.get("removed", 0)}</span>')
            html_parts.append(f'<span class="modified">修改: {summary.get("modified", 0)}</span>')
            html_parts.append('</div>')

        sheets = diff_data.get('sheets', {})
        for sheet_name, sheet_data in sheets.items():
            safe_sheet = escape(format_cell_value(sheet_name))
            html_parts.append(f'<div class="sheet-container" data-sheet="{safe_sheet}">')
            html_parts.append(f'<h4 class="sheet-title">工作表: {safe_sheet}</h4>')

            header_rows = sheet_data.get('header_rows') or []
            # 表头行也算「有内容」：只改表头的一次提交里 rows 是空的，只按 rows 判的话
            # 整张表会被跳过 —— 兜底路径下这次改动就完全看不见了。
            if (sheet_data.get('rows') if 'rows' in sheet_data else None) or header_rows:
                html_parts.append('<div class="table-container">')
                html_parts.append('<table class="excel-diff-table">')

                headers = sheet_data.get('headers', [])
                if headers:
                    html_parts.append('<thead><tr>')
                    html_parts.append('<th>行号</th><th>状态</th>')
                    for header in headers:
                        html_parts.append(f'<th>{escape(format_cell_value(header))}</th>')
                    html_parts.append('</tr></thead>')

                html_parts.append('<tbody>')
                # 表头块排在最前面，与主模板/前端模块的位置一致，单元格写法也一致
                # （`format_cell_value` + `escape`，见本方法 docstring）。
                #
                # 行号按**块里实际的行号**写，不写死「第 2 行起」：配了「名称行 = 2」的
                # 仓库，表头块里第一行是物理第 1 行（那行大标题），块里的行号是 1、3…
                if header_rows:
                    numbers = [row.get('row_number') for row in header_rows]
                    span = (f'第 {numbers[0]} 行起' if len(numbers) == 1
                            else f'第 {numbers[0]}–{numbers[-1]} 行')
                    html_parts.append(
                        f'<tr class="header-block-title"><td colspan="{len(headers) + 2}">'
                        f'表头（{span}，共 {len(header_rows)} 行）</td></tr>')
                for row in list(header_rows) + list(sheet_data['rows']):
                    status = row.get('status', 'unchanged')
                    row_number = row.get('row_number', '')
                    data = row.get('data', {})
                    # 修改行把上一版本的行号一并写出来（插/删一行之后两版行号会不同）。
                    # 这一份是「模板渲染失败」时的兜底，没有上下两行的结构，所以跟
                    # 另一个兜底形态一样写成 `旧 → 新`。
                    previous_number = row.get('previous_row_number')
                    number_text = format_cell_value(row_number)
                    if previous_number not in (None, '') and str(previous_number) != str(row_number):
                        number_text = f'{format_cell_value(previous_number)} → {number_text}'

                    html_parts.append(f'<tr class="row-{escape(str(status))}">')
                    html_parts.append(f'<td>{escape(number_text)}</td>')
                    html_parts.append(
                        f'<td class="status-{escape(str(status))}">{escape(str(status))}</td>')

                    for header in headers:
                        value = format_cell_value(data.get(header, ''))
                        html_parts.append(f'<td>{escape(value)}</td>')

                    html_parts.append('</tr>')

                html_parts.append('</tbody></table>')
                html_parts.append('</div>')

            html_parts.append('</div>')

        html_parts.append('</div>')
        return ''.join(html_parts)
    
    def _generate_excel_diff_css(self) -> str:
        """生成Excel差异的CSS样式"""
        return """
        .excel-diff-container {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 20px 0;
        }
        .file-header h3 {
            color: #333;
            border-bottom: 2px solid #007bff;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }
        .diff-summary {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: flex;
            gap: 20px;
        }
        .diff-summary .added { color: #28a745; font-weight: bold; }
        .diff-summary .removed { color: #dc3545; font-weight: bold; }
        .diff-summary .modified { color: #ffc107; font-weight: bold; }
        .sheet-container {
            margin-bottom: 30px;
            border: 1px solid #dee2e6;
            border-radius: 5px;
            overflow: hidden;
        }
        .sheet-title {
            background: #007bff;
            color: white;
            margin: 0;
            padding: 15px;
            font-size: 16px;
        }
        .table-container {
            overflow-x: auto;
            max-height: 600px;
            overflow-y: auto;
        }
        .excel-diff-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        .excel-diff-table th,
        .excel-diff-table td {
            border: 1px solid #dee2e6;
            padding: 8px 12px;
            text-align: left;
            vertical-align: top;
        }
        .excel-diff-table th {
            background: #f8f9fa;
            font-weight: 600;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        .row-added { background-color: #d4edda; }
        .row-removed { background-color: #f8d7da; }
        .row-modified { background-color: #fff3cd; }
        .row-unchanged { color: #6c757d; }
        /* 表头块的分节标题（见 _generate_simple_excel_html）：与数据行分开显示 */
        .header-block-title td {
            background: #eef1f4;
            color: #495057;
            font-weight: 600;
            font-size: 13px;
        }
        .status-added { color: #155724; font-weight: bold; }
        .status-removed { color: #721c24; font-weight: bold; }
        .status-modified { color: #856404; font-weight: bold; }
        .excel-diff-table tr:hover {
            background-color: rgba(0, 123, 255, 0.1);
        }
        .excel-diff-table td {
            max-width: 200px;
            word-wrap: break-word;
            word-break: break-all;
        }
        """
    
    def _generate_excel_diff_js(self) -> str:
        """生成Excel差异的JavaScript代码"""
        return """
        document.addEventListener('DOMContentLoaded', function() {
            const rows = document.querySelectorAll('.excel-diff-table tbody tr');
            rows.forEach(row => {
                row.addEventListener('click', function() {
                    rows.forEach(r => r.classList.remove('selected'));
                    this.classList.add('selected');
                });
            });
            
            const sheetContainers = document.querySelectorAll('.sheet-container');
            if (sheetContainers.length > 1) {
                sheetContainers.forEach((container, index) => {
                    if (index > 0) {
                        container.style.display = 'none';
                    }
                });
                
                const tabContainer = document.createElement('div');
                tabContainer.className = 'sheet-tabs';
                tabContainer.innerHTML = '<style>.sheet-tabs{margin:10px 0;}.sheet-tab{display:inline-block;padding:8px 16px;margin-right:5px;background:#f8f9fa;border:1px solid #dee2e6;cursor:pointer;border-radius:3px;}.sheet-tab.active{background:#007bff;color:white;}</style>';
                
                sheetContainers.forEach((container, index) => {
                    const sheetName = container.getAttribute('data-sheet');
                    const tab = document.createElement('span');
                    tab.className = 'sheet-tab' + (index === 0 ? ' active' : '');
                    tab.textContent = sheetName;
                    tab.addEventListener('click', function() {
                        sheetContainers.forEach(c => c.style.display = 'none');
                        container.style.display = 'block';
                        document.querySelectorAll('.sheet-tab').forEach(t => t.classList.remove('active'));
                        this.classList.add('active');
                    });
                    tabContainer.appendChild(tab);
                });
                
                sheetContainers[0].parentNode.insertBefore(tabContainer, sheetContainers[0]);
            }
        });
        
        const style = document.createElement('style');
        style.textContent = '.excel-diff-table tbody tr.selected { background-color: rgba(0, 123, 255, 0.2) !important; }';
        document.head.appendChild(style);
        """
    
    def cleanup_old_version_cache(self):
        """清理旧版本的HTML缓存"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                count = ExcelHtmlCache.query.filter(
                    ExcelHtmlCache.diff_version != self.current_version
                ).delete(synchronize_session=False)
                
                self.db.session.commit()
                return count
            
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception("清理旧版本HTML缓存失败", e)
            return 0
    
    def cleanup_expired_cache(self):
        """清理过期的HTML缓存（基于创建时间，超过7天的缓存）"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                expire_time = datetime.utcnow() - timedelta(days=7)
                
                count = ExcelHtmlCache.query.filter(
                    ExcelHtmlCache.created_at < expire_time
                ).delete(synchronize_session=False)
                
                if count > 0:
                    self.db.session.commit()
                
                return count
            
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception as rollback_error:
                self._log_exception("清理过期HTML缓存失败后回滚也失败", rollback_error)
            self._log_exception("清理过期HTML缓存失败", e)
            # 返回 None 而不是 0：0 是「本来就没东西可清」，两者在管理界面上
            # 原先是同一个「清理了 0 条」，无法区分（见 cache_management_routes 的清理接口）。
            return None
    
    def get_cache_statistics(self, repository_id=None):
        """获取HTML缓存统计信息"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                query = ExcelHtmlCache.query
                if repository_id:
                    query = query.filter(ExcelHtmlCache.repository_id == repository_id)
                
                total_count = query.count()
                completed_count = query.filter(ExcelHtmlCache.cache_status == 'completed').count()
                current_version_count = query.filter(ExcelHtmlCache.diff_version == self.current_version).count()

                completed_query = query.filter(ExcelHtmlCache.cache_status == 'completed')
                total_size = (
                    completed_query.with_entities(
                        func.coalesce(
                            func.sum(
                                func.length(func.coalesce(ExcelHtmlCache.html_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.css_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.js_content, ''))
                            ),
                            0,
                        )
                    ).scalar()
                    or 0
                )
                
                return {
                    'total_count': total_count,
                    'completed_count': completed_count,
                    'current_version_count': current_version_count,
                    'old_version_count': total_count - current_version_count,
                    'total_size_mb': round(total_size / (1024 * 1024), 2),
                    'current_version': self.current_version
                }
            
        except Exception as e:
            self._log_exception(f"获取HTML缓存统计失败 repository_id={repository_id}", e)
            return {
                'total_count': 0,
                'completed_count': 0,
                'current_version_count': 0,
                'old_version_count': 0,
                'total_size_mb': 0,
                'current_version': self.current_version
            }
    
    def get_cache_statistics_by_repositories(self, repository_ids):
        """获取指定仓库列表的HTML缓存统计信息"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            if not repository_ids:
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'current_version_count': 0,
                    'old_version_count': 0,
                    'total_size_mb': 0.0,
                    'current_version': self.current_version
                }
            
            with flask_app.app_context():
                query = ExcelHtmlCache.query.filter(ExcelHtmlCache.repository_id.in_(repository_ids))
                
                total_count = query.count()
                completed_count = query.filter(ExcelHtmlCache.cache_status == 'completed').count()
                current_version_count = query.filter(ExcelHtmlCache.diff_version == self.current_version).count()

                completed_query = query.filter(ExcelHtmlCache.cache_status == 'completed')
                total_size = (
                    completed_query.with_entities(
                        func.coalesce(
                            func.sum(
                                func.length(func.coalesce(ExcelHtmlCache.html_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.css_content, ''))
                                + func.length(func.coalesce(ExcelHtmlCache.js_content, ''))
                            ),
                            0,
                        )
                    ).scalar()
                    or 0
                )
                
                return {
                    'total_count': total_count,
                    'completed_count': completed_count,
                    'current_version_count': current_version_count,
                    'old_version_count': total_count - current_version_count,
                    'total_size_mb': round(total_size / (1024 * 1024), 2),
                    'current_version': self.current_version
                }
            
        except Exception as e:
            self._log_exception(f"获取仓库列表HTML缓存统计失败 repository_ids={repository_ids}", e)
            return {
                'total_count': 0,
                'completed_count': 0,
                'current_version_count': 0,
                'old_version_count': 0,
                'total_size_mb': 0.0,
                'current_version': self.current_version
            }
    
    def delete_html_cache(self, repository_id: int, commit_id: str, file_path: str) -> int:
        """删除指定的HTML缓存"""
        try:
            ExcelHtmlCache, flask_app = self._get_model("ExcelHtmlCache", "app")
            
            with flask_app.app_context():
                deleted_count = ExcelHtmlCache.query.filter_by(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path
                ).delete()
                
                if deleted_count > 0:
                    self.db.session.commit()
                
                return deleted_count
                
        except Exception as e:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            self._log_exception(
                f"删除HTML缓存失败 repository_id={repository_id}, commit_id={commit_id}, file_path={file_path}",
                e
            )
            return 0
