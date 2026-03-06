"""
Commit Diff 引擎 — 从 app.py 拆分
包含: diff数据获取、合并diff处理、智能显示列表等核心逻辑
"""
import sys
import json
import threading
from datetime import datetime, timezone
from collections import defaultdict
from types import SimpleNamespace

from models import db, Commit, Repository, DiffCache, ExcelHtmlCache
from services.diff_service import DiffService
from utils.diff_data_utils import clean_json_data
from utils.logger import log_print

# ---------------------------------------------------------------------------
#  运行时依赖 — 通过 configure() 注入，避免循环导入
# ---------------------------------------------------------------------------
_excel_cache_service = None
_excel_html_cache_service = None
_active_git_processes = None
_add_excel_diff_task = None
_get_unified_diff_data = None
_get_git_service = None
_get_svn_service = None


def configure_commit_diff_logic(
    excel_cache_service,
    excel_html_cache_service,
    active_git_processes,
    add_excel_diff_task_func,
    get_unified_diff_data_func,
    get_git_service_func,
    get_svn_service_func,
):
    """注入运行时依赖，由 app.py 在启动时调用。"""
    global _excel_cache_service, _excel_html_cache_service
    global _active_git_processes, _add_excel_diff_task
    global _get_unified_diff_data, _get_git_service, _get_svn_service

    _excel_cache_service = excel_cache_service
    _excel_html_cache_service = excel_html_cache_service
    _active_git_processes = active_git_processes
    _add_excel_diff_task = add_excel_diff_task_func
    _get_unified_diff_data = get_unified_diff_data_func
    _get_git_service = get_git_service_func
    _get_svn_service = get_svn_service_func


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------

def _normalize_commit_operation(operation):
    """Normalize commit operation to A/M/D/R style."""
    if operation is None:
        return 'M'
    normalized = str(operation).strip().upper()
    if not normalized:
        return 'M'
    mapping = {
        'ADD': 'A', 'ADDED': 'A', 'CREATE': 'A', 'CREATED': 'A',
        'MOD': 'M', 'MODIFIED': 'M', 'UPDATE': 'M', 'UPDATED': 'M',
        'DEL': 'D', 'DELETE': 'D', 'DELETED': 'D', 'REMOVE': 'D', 'REMOVED': 'D',
        'RENAME': 'R', 'RENAMED': 'R',
    }
    return mapping.get(normalized, normalized[:1])


def _commit_sort_key_for_merge(commit):
    """Stable sort key for commit merge ordering."""
    commit_time = getattr(commit, 'commit_time', None)
    commit_ts = float('-inf')
    if isinstance(commit_time, datetime):
        try:
            if commit_time.tzinfo is None:
                commit_time = commit_time.replace(tzinfo=timezone.utc)
            commit_ts = commit_time.timestamp()
        except Exception:
            commit_ts = float('-inf')
    commit_db_id = getattr(commit, 'id', 0) or 0
    return commit_ts, commit_db_id


def _commit_time_to_iso(commit_time):
    if isinstance(commit_time, datetime):
        return commit_time.isoformat()
    return None


def _commit_id_matches(candidate_commit_id, target_commit_id):
    candidate = str(candidate_commit_id or "").strip().lower()
    target = str(target_commit_id or "").strip().lower()
    if not candidate or not target:
        return False
    return candidate == target or candidate.startswith(target) or target.startswith(candidate)


def _build_virtual_previous_commit(commit, previous_data):
    commit_id = str((previous_data or {}).get("commit_id") or "").strip()
    if not commit_id:
        return None
    return SimpleNamespace(
        id=None,
        repository_id=getattr(commit, "repository_id", None),
        repository=getattr(commit, "repository", None),
        path=getattr(commit, "path", None),
        commit_id=commit_id,
        version=commit_id[:8],
        operation=str((previous_data or {}).get("operation") or "M"),
        author=str((previous_data or {}).get("author") or ""),
        message=str((previous_data or {}).get("message") or ""),
        commit_time=(previous_data or {}).get("commit_time"),
    )


def _resolve_previous_commit_from_vcs(commit):
    repository = getattr(commit, "repository", None)
    file_path = str(getattr(commit, "path", "") or "").strip()
    if repository is None or not file_path:
        return None

    try:
        if repository.type == "git":
            service = _get_git_service(repository) if callable(_get_git_service) else None
            if service is None:
                from services.threaded_git_service import ThreadedGitService

                service = ThreadedGitService(
                    repository.url,
                    repository.root_directory,
                    repository.username,
                    repository.token,
                    repository,
                    _active_git_processes,
                )
            previous_data = service.get_previous_file_commit(file_path, commit.commit_id)
            return _build_virtual_previous_commit(commit, previous_data)

        if repository.type == "svn":
            service = _get_svn_service(repository) if callable(_get_svn_service) else None
            if service is None:
                return None
            commits_data = service.get_file_history(file_path, limit=1000) or []
            target_index = None
            for idx, item in enumerate(commits_data):
                if _commit_id_matches(item.get("commit_id"), commit.commit_id):
                    target_index = idx
                    break
            if target_index is None or target_index + 1 >= len(commits_data):
                return None
            return _build_virtual_previous_commit(commit, commits_data[target_index + 1])
    except Exception as exc:
        log_print(f"VCS 回退查找前一提交失败: {exc}", "DIFF")
    return None


def resolve_previous_commit(commit, file_commits=None):
    """优先按数据库查找前一提交；缺失时回退到 VCS 文件历史。"""
    repository = getattr(commit, "repository", None)
    if repository is None:
        return None

    previous_commit = None
    if getattr(commit, "commit_time", None) is not None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.commit_time < commit.commit_time
        ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
            Commit.id < commit.id
        ).order_by(Commit.id.desc()).first()

    if previous_commit is None and file_commits:
        current_index = None
        for i, item in enumerate(file_commits):
            if getattr(item, "id", None) == getattr(commit, "id", None):
                current_index = i
                break
        if current_index is not None and current_index + 1 < len(file_commits):
            previous_commit = file_commits[current_index + 1]

    if previous_commit is None:
        previous_commit = _resolve_previous_commit_from_vcs(commit)
    return previous_commit


def convert_hunks_to_lines(diff_data):
    """将hunks格式转换为模板期望的lines格式"""
    all_lines = []
    old_line_num = 1
    new_line_num = 1
    for hunk in diff_data.get('hunks', []):
        all_lines.append({
            'type': 'header',
            'content': hunk.get('header', ''),
            'old_line_number': None,
            'new_line_number': None
        })
        old_line_num = hunk.get('old_start', 1)
        new_line_num = hunk.get('new_start', 1)
        for line in hunk.get('lines', []):
            line_type = line.get('type', 'context')
            old_num = None
            new_num = None
            if line_type == 'removed':
                old_num = old_line_num
                old_line_num += 1
            elif line_type == 'added':
                new_num = new_line_num
                new_line_num += 1
            elif line_type == 'context':
                old_num = old_line_num
                new_num = new_line_num
                old_line_num += 1
                new_line_num += 1
            all_lines.append({
                'type': line_type,
                'content': line.get('content', ''),
                'old_line_number': old_num,
                'new_line_number': new_num
            })
    return {
        'type': 'code',
        'file_path': diff_data.get('file_path', ''),
        'lines': all_lines
    }


def get_mock_diff_data(commit):
    """获取模拟的diff数据"""
    if commit.path and (commit.path.endswith('.xlsx') or commit.path.endswith('.xls')):
        return {
            'type': 'table',
            'sheet_name': 'Sheet1',
            'changes': [
                {
                    'type': 'added', 'row': 5,
                    'data': {'A': 'ID5', 'B': 'New Item', 'C': '新增项目', 'D': '描述', 'E': '备注'}
                },
                {
                    'type': 'modified', 'row': 3,
                    'data': {'A': 'ID3', 'B': 'Modified Item', 'C': '修改项目', 'D': '新描述', 'E': '更新'}
                }
            ]
        }
    else:
        return {
            'type': 'code',
            'file_path': commit.path,
            'lines': [
                {'type': 'header', 'content': '@@ -1,3 +1,3 @@', 'old_line_number': None, 'new_line_number': None},
                {'type': 'removed', 'content': 'function oldFunction() {', 'old_line_number': 1, 'new_line_number': None},
                {'type': 'added', 'content': 'function newFunction() {', 'old_line_number': None, 'new_line_number': 1},
                {'type': 'context', 'content': '    // 函数体', 'old_line_number': 2, 'new_line_number': 2},
                {'type': 'removed', 'content': '    return "old";', 'old_line_number': 3, 'new_line_number': None},
                {'type': 'added', 'content': '    return "new";', 'old_line_number': None, 'new_line_number': 3},
                {'type': 'context', 'content': '}', 'old_line_number': 4, 'new_line_number': 4}
            ]
        }


# ---------------------------------------------------------------------------
#  generate_merged_diff_data — 周版本合并引擎入口
# ---------------------------------------------------------------------------

def generate_merged_diff_data(repository, file_path, base_commit, latest_commit, commits):
    """Generate merged diff data with real merge strategy and compatible metadata."""
    try:
        ordered_commits = sorted((commits or []), key=_commit_sort_key_for_merge)
        if not ordered_commits:
            return {
                'file_path': file_path,
                'file_type': DiffService().get_file_type(file_path),
                'base_commit': base_commit.commit_id if base_commit else None,
                'latest_commit': latest_commit.commit_id if latest_commit else None,
                'commits_count': 0, 'commit_ids': [], 'authors': [], 'operations': [],
                'time_range': {'start': None, 'end': None},
                'merge_strategy': 'empty', 'has_conflict_risk': False,
                'is_rename_suspected': False, 'contains_added': False,
                'contains_deleted': False, 'contains_modified': False,
                'diff_data': None, 'merged_diff': None,
            }

        operations = [_normalize_commit_operation(getattr(c, 'operation', None)) for c in ordered_commits]
        operation_set = set(operations)
        commit_ids = [getattr(c, 'commit_id', None) for c in ordered_commits if getattr(c, 'commit_id', None)]
        authors = sorted({(getattr(c, 'author', None) or 'Unknown') for c in ordered_commits})

        merge_strategy = 'single'
        merged_diff = None
        if len(ordered_commits) == 1:
            current_commit = ordered_commits[0]
            previous_commit = base_commit if (base_commit and base_commit.commit_id != current_commit.commit_id) else None
            if previous_commit:
                merged_diff = get_commit_pair_diff_internal(current_commit, previous_commit)
            else:
                merged_diff = _get_unified_diff_data(current_commit, None)
        else:
            if are_commits_consecutive_internal(ordered_commits):
                merge_strategy = 'consecutive'
                merged_diff = handle_consecutive_commits_merge_internal(ordered_commits)
            else:
                merge_strategy = 'segmented'
                merged_diff = handle_non_consecutive_commits_merge_internal(ordered_commits)

        # Fallback
        if not merged_diff:
            latest_for_fallback = ordered_commits[-1]
            previous_for_fallback = None
            if base_commit and base_commit.commit_id != latest_for_fallback.commit_id:
                previous_for_fallback = base_commit
            elif len(ordered_commits) > 1:
                previous_for_fallback = ordered_commits[-2]
            if previous_for_fallback:
                merged_diff = get_commit_pair_diff_internal(latest_for_fallback, previous_for_fallback)
            else:
                merged_diff = _get_unified_diff_data(latest_for_fallback, None)

        merged_diff = clean_json_data(merged_diff) if merged_diff else {
            'type': 'summary', 'file_path': file_path, 'message': 'No diff payload generated'
        }

        segment_summaries = []
        if isinstance(merged_diff, dict) and merged_diff.get('type') == 'segmented_diff':
            for index, segment in enumerate(merged_diff.get('segments', []), start=1):
                segment_info = segment.get('segment_info') or {} if isinstance(segment, dict) else {}
                segment_summaries.append({
                    'segment_index': segment_info.get('segment_index', index),
                    'current': segment_info.get('current'),
                    'previous': segment_info.get('previous'),
                })

        known_authors = [a for a in authors if a != 'Unknown']
        has_conflict_risk = (merge_strategy == 'segmented') or (len(known_authors) > 1 and len(ordered_commits) > 1)
        is_rename_suspected = ('R' in operation_set) or ('A' in operation_set and 'D' in operation_set)

        final_data = {
            'file_path': file_path,
            'file_type': DiffService().get_file_type(file_path),
            'base_commit': base_commit.commit_id if base_commit else None,
            'latest_commit': (latest_commit.commit_id if latest_commit else ordered_commits[-1].commit_id),
            'commits_count': len(ordered_commits), 'commit_ids': commit_ids,
            'authors': authors, 'operations': operations,
            'time_range': {
                'start': _commit_time_to_iso(getattr(ordered_commits[0], 'commit_time', None)),
                'end': _commit_time_to_iso(getattr(ordered_commits[-1], 'commit_time', None)),
            },
            'merge_strategy': merge_strategy, 'has_conflict_risk': has_conflict_risk,
            'is_rename_suspected': is_rename_suspected,
            'contains_added': 'A' in operation_set, 'contains_deleted': 'D' in operation_set,
            'contains_modified': 'M' in operation_set,
            'diff_data': merged_diff, 'merged_diff': merged_diff,
        }
        if segment_summaries:
            final_data['segments'] = segment_summaries
            final_data['total_segments'] = len(segment_summaries)
        return clean_json_data(final_data)
    except Exception as e:
        log_print(f"生成合并diff数据失败: {e}", 'WEEKLY', force=True)
        return {}


# ---------------------------------------------------------------------------
#  Diff 数据获取 — get_diff_data / get_real_diff_data_for_merge
# ---------------------------------------------------------------------------

_PREVIOUS_COMMIT_SENTINEL = object()


def _is_renderable_code_diff(diff_data):
    if not isinstance(diff_data, dict):
        return False
    hunks = diff_data.get("hunks")
    if isinstance(hunks, list) and len(hunks) > 0:
        return True
    patch = diff_data.get("patch")
    if isinstance(patch, str) and patch.strip():
        return True
    lines = diff_data.get("lines")
    if isinstance(lines, list) and len(lines) > 0:
        return True
    return False


def _build_diff_error_data(commit, message, detail=None):
    payload = {
        "type": "error",
        "file_path": str(getattr(commit, "path", "") or ""),
        "message": str(message or "无法获取差异数据"),
    }
    commit_id = str(getattr(commit, "commit_id", "") or "").strip()
    if commit_id:
        payload["commit_id"] = commit_id
    detail_text = str(detail or "").strip()
    if detail_text:
        payload["detail"] = detail_text
    return payload


def _get_git_code_diff_with_retry(service, commit, previous_commit):
    diagnostics = []

    def _fetch_once():
        if previous_commit:
            return service.get_commit_range_diff(previous_commit.commit_id, commit.commit_id, commit.path)
        return service.get_file_diff(commit.commit_id, commit.path)

    diff_data = _fetch_once()
    if _is_renderable_code_diff(diff_data):
        diagnostics.append("初次获取diff成功")
        return diff_data, diagnostics

    diagnostics.append("初次获取diff失败")
    try:
        update_ok, update_message = service.clone_or_update_repository()
        diagnostics.append(f"仓库更新结果: {update_message}")
        if update_ok:
            retry_data = _fetch_once()
            if _is_renderable_code_diff(retry_data):
                diagnostics.append("仓库更新后重试获取diff成功")
                return retry_data, diagnostics
            diagnostics.append("仓库更新后重试仍未拿到有效diff")
    except Exception as exc:
        diagnostics.append(f"仓库更新异常: {exc}")

    return diff_data, diagnostics


def get_diff_data(commit, previous_commit=_PREVIOUS_COMMIT_SENTINEL):
    """获取真实的diff数据 - 返回数据结构而非JSON响应"""
    try:
        if previous_commit is _PREVIOUS_COMMIT_SENTINEL:
            previous_commit = resolve_previous_commit(commit)
        repository = commit.repository

        log_print(f"🔍 get_diff_data - 当前提交: {commit.commit_id[:8]} ({commit.commit_time})", 'DIFF')
        if previous_commit:
            log_print(f"🔍 get_diff_data - 前一提交: {previous_commit.commit_id[:8]} ({previous_commit.commit_time})", 'DIFF')
        else:
            log_print(f"🔍 get_diff_data - 无前一提交，这是初始提交", 'DIFF')

        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, _active_git_processes)
            if commit.path and (commit.path.endswith('.xlsx') or commit.path.endswith('.xls')):
                try:
                    log_print(f"开始处理commit {commit.commit_id}的Excel diff数据...", 'EXCEL')
                    diff_data = _get_unified_diff_data(commit, previous_commit)
                    if not diff_data:
                        diff_data = service.parse_excel_diff(commit.commit_id, commit.path)
                    if diff_data:
                        diff_data = clean_json_data(diff_data)
                    if hasattr(service, 'performance_stats'):
                        log_print(f"Excel处理性能统计: {service.performance_stats}", 'EXCEL')
                    return diff_data
                except Exception as e:
                    log_print(f"获取commit {commit.commit_id} Excel diff数据时出错: {str(e)}", 'EXCEL', force=True)
                    import traceback; traceback.print_exc()
                    return {'error': str(e)}
            else:
                log_print(f"开始处理commit {commit.commit_id}的代码文件diff数据: {commit.path}", 'INFO')
                diff_data, diagnostics = _get_git_code_diff_with_retry(service, commit, previous_commit)
                if _is_renderable_code_diff(diff_data):
                    diff_data['file_path'] = commit.path
                    stats = service.get_performance_stats() if hasattr(service, "get_performance_stats") else {}
                    log_print(f"Git diff处理性能统计: {stats}", 'INFO')
                    log_print(f"成功获取diff数据，hunks数量: {len(diff_data.get('hunks', []))}", 'INFO')
                    return clean_json_data(diff_data)

                unified_data = _get_unified_diff_data(commit, previous_commit)
                if _is_renderable_code_diff(unified_data):
                    unified_data['file_path'] = commit.path
                    log_print("主路径获取失败，已回退统一diff并成功", "INFO")
                    return clean_json_data(unified_data)

                detail = "；".join([item for item in diagnostics if item])
                if previous_commit:
                    detail = f"{detail}；previous_commit={previous_commit.commit_id}"
                log_print(f"未能获取到有效的diff数据: {detail}", 'INFO', force=True)
                return _build_diff_error_data(
                    commit,
                    "无法获取代码差异（可能本地仓库缺少目标提交），请稍后重试",
                    detail=detail,
                )

        elif repository.type == 'svn':
            service = _get_svn_service(repository)
            if commit.path and (commit.path.endswith('.xlsx') or commit.path.endswith('.xls')):
                return _get_unified_diff_data(commit, None)
            else:
                diff_data = service.get_file_diff(commit.version, commit.path)
                if _is_renderable_code_diff(diff_data):
                    diff_data['file_path'] = commit.path
                    return clean_json_data(diff_data)
                return _build_diff_error_data(
                    commit,
                    "无法获取SVN代码差异",
                    detail=f"version={getattr(commit, 'version', '')}, path={getattr(commit, 'path', '')}",
                )

        log_print(f"无法获取真实diff数据，返回错误结构", 'INFO')
        return _build_diff_error_data(commit, "无法获取差异数据", detail=f"repository.type={repository.type}")
    except Exception as e:
        log_print(f"获取真实diff数据失败: {str(e)}")
        import traceback; traceback.print_exc()
        return _build_diff_error_data(commit, "获取差异数据失败", detail=str(e))


def get_real_diff_data_for_merge(commit):
    """获取用于合并显示的diff数据（lines格式）"""
    try:
        log_print(f"开始获取提交{commit.id}的diff数据: {commit.path}", 'INFO')
        repository = commit.repository
        is_excel = _excel_cache_service.is_excel_file(commit.path)
        log_print(f"- 是否Excel文件: {is_excel}", 'INFO')

        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, _active_git_processes)
            if is_excel:
                log_print(f"- 处理Excel文件，优先检查缓存", 'INFO')
                db.session.expire_all()
                cached_diff = _excel_cache_service.get_cached_diff(repository.id, commit.commit_id, commit.path)
                if cached_diff:
                    log_print(f"- 从缓存获取Excel差异数据", 'INFO')
                    log_print(f"- 缓存版本: {cached_diff.diff_version} | 缓存时间: {cached_diff.created_at}", 'INFO')
                    log_print(f"- 缓存更新时间: {cached_diff.updated_at}", 'INFO')
                    log_print(f"- 缓存ID: {cached_diff.id}", 'INFO')
                    excel_diff = json.loads(cached_diff.diff_data)
                    log_print(f"- 解析后的Excel diff数据类型: {excel_diff.get('type', 'INFO') if excel_diff else 'None'}")
                    if excel_diff and excel_diff.get('sheets'):
                        log_print(f"- 解析后的工作表数量: {len(excel_diff['sheets'])}")
                        first_sheet_name = list(excel_diff['sheets'].keys())[0]
                        first_sheet_data = list(excel_diff['sheets'].values())[0]
                        log_print(f"  - 工作表 '{first_sheet_name}': {first_sheet_data.get('status', 'unknown')}, 行数: {len(first_sheet_data.get('rows', []))}")
                    else:
                        log_print(f"- ❌ 解析后的Excel diff数据无工作表", 'INFO')
                    _add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                    log_print(f"✅ 合并diff添加高优先级缓存任务: {commit.path}", 'CACHE')
                else:
                    log_print(f"- 缓存未命中，调用Git Excel diff解析", 'INFO')
                    excel_diff = service.parse_excel_diff(commit.commit_id, commit.path)
                    _add_excel_diff_task(repository.id, commit.commit_id, commit.path, priority=1)
                    log_print(f"✅ 合并diff缓存未命中，添加高优先级缓存任务: {commit.path}", 'CACHE')
                    log_print(f"- Excel工作表列表: {list(excel_diff.get('sheets', {}).keys())}")
                    if excel_diff.get('sheets'):
                        first_sheet_name = list(excel_diff['sheets'].keys())[0]
                        first_sheet = excel_diff['sheets'][first_sheet_name]
                        log_print(f"- 第一个工作表 '{first_sheet_name}' 结构: {list(first_sheet.keys())}", 'INFO')
                        if 'rows' in first_sheet:
                            log_print(f"- 工作表行数: {len(first_sheet['rows'])}")

                if excel_diff:
                    log_print(f"- 开始清理Excel diff数据中的NaN值...", 'APP')
                    try:
                        excel_diff = clean_json_data(excel_diff)
                        log_print(f"- Excel diff数据清理完成", 'APP')
                    except Exception as clean_error:
                        log_print(f"- ❌ Excel diff数据清理失败: {str(clean_error)}", force=True)
                        import traceback; traceback.print_exc()

                log_print(f"- 准备返回Excel diff数据，类型: {type(excel_diff)}", force=True)
                result = excel_diff

                def delayed_cleanup():
                    import time
                    time.sleep(2)
                    if hasattr(service, 'cleanup_thread_pool'):
                        service.cleanup_thread_pool()
                        log_print(f"- 延迟清理合并diff线程池完成", 'APP')
                cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
                cleanup_thread.start()
                return result
            else:
                log_print(f"- 调用Git文本diff解析", 'INFO')
                diff_data = service.get_file_diff(commit.commit_id, commit.path)
                if diff_data and diff_data.get('hunks'):
                    return convert_hunks_to_lines(diff_data)

        elif repository.type == 'svn':
            service = _get_svn_service(repository)
            if is_excel:
                log_print(f"- 处理SVN Excel文件，使用统一diff处理逻辑", 'INFO')
                excel_diff = _get_unified_diff_data(commit, None)
                if excel_diff:
                    try:
                        excel_diff = clean_json_data(excel_diff)
                        log_print(f"- SVN Excel diff数据清理完成", 'APP')
                    except Exception as clean_error:
                        log_print(f"- ❌ SVN Excel diff数据清理失败: {str(clean_error)}", force=True)
                return excel_diff
            else:
                log_print(f"- 调用SVN文本diff解析", 'INFO')
                diff_data = service.get_file_diff(commit.version, commit.path)
                if diff_data and diff_data.get('hunks'):
                    return convert_hunks_to_lines(diff_data)

        log_print(f"无法获取真实diff数据，返回模拟数据", 'INFO')
        return get_mock_diff_data(commit)
    except Exception as e:
        log_print(f"获取合并diff数据失败: {str(e)}")
        import traceback; traceback.print_exc()
        return get_mock_diff_data(commit)


# ---------------------------------------------------------------------------
#  合并 Diff 引擎 — get_merged_diff_data + 内部处理函数
# ---------------------------------------------------------------------------

def get_merged_diff_data(commits):
    """增强的智能合并diff数据处理"""
    if not commits:
        return None
    log_print(f"=== 增强合并diff处理开始 ===", 'INFO')
    log_print(f"提交数量: {len(commits)}")
    file_groups = defaultdict(list)
    for commit in commits:
        file_groups[commit.path].append(commit)
    log_print(f"文件分组数量: {len(file_groups)}")
    for file_path, file_commits in file_groups.items():
        log_print(f"  - {file_path}: {len(file_commits)}个提交")

    if len(file_groups) > 1:
        log_print("✓ 检测到情况1/4: 不同文件的合并diff（包括混合情况）", 'INFO')
        return handle_different_files_merge(file_groups)

    file_path = list(file_groups.keys())[0]
    file_commits = file_groups[file_path]
    file_commits.sort(key=lambda x: x.commit_time)
    log_print(f"同一文件 {file_path} 的提交处理:", 'INFO')
    for i, commit in enumerate(file_commits):
        log_print(f"  {i+1}. {commit.commit_id[:8]} - {commit.commit_time}", 'INFO')

    if are_commits_consecutive_internal(file_commits):
        log_print("✓ 检测到情况2: 相同文件连续commit的合并diff", 'INFO')
        return handle_consecutive_commits_merge_internal(file_commits)
    else:
        log_print("✓ 检测到情况3: 相同文件非连续commit的合并diff", 'INFO')
        return handle_non_consecutive_commits_merge_internal(file_commits)


def handle_different_files_merge(file_groups):
    """情况1&4: 处理不同文件的合并diff（包括混合情况）"""
    log_print("处理多文件的合并diff...", 'INFO')
    diff_sections = []
    for file_path, file_commits in file_groups.items():
        log_print(f"处理文件: {file_path} ({len(file_commits)}个提交)", force=True)
        try:
            file_commits.sort(key=lambda x: x.commit_time)
            if len(file_commits) == 1:
                log_print(f"  - 单个提交处理: {file_commits[0].commit_id[:8]}", 'APP')
                try:
                    log_print(f"  - 调用get_unified_diff_data函数...", 'APP')
                    previous_commit = file_commits[1] if len(file_commits) > 1 else None
                    diff_data = _get_unified_diff_data(file_commits[0], previous_commit)
                    log_print(f"  - 函数调用完成，返回值类型: {type(diff_data)}", force=True)
                except Exception as get_error:
                    log_print(f"  - ❌ 获取diff_data时出错: {str(get_error)}", force=True)
                    import traceback; traceback.print_exc()
                    diff_data = None
                    continue
                if diff_data:
                    log_print(f"  - diff_data类型: {diff_data.get('type', 'unknown')}", force=True)
                    if diff_data.get('type') == 'excel':
                        sheets = diff_data.get('sheets', {})
                        if not sheets:
                            log_print(f"  - ⚠️ Excel文件无工作表数据，跳过: {file_path}", 'APP')
                        else:
                            has_content = any(s.get('rows') and len(s['rows']) > 0 for s in sheets.values())
                            if not has_content:
                                log_print(f"  - ⚠️ Excel文件所有工作表都为空，但仍添加到结果中: {file_path}", 'APP')
                    try:
                        commit_time_str = None
                        try:
                            commit_time_str = file_commits[0].commit_time.isoformat()
                        except Exception:
                            commit_time_str = str(file_commits[0].commit_time)
                        diff_sections.append({
                            'file_path': file_path, 'diff_type': 'single_commit',
                            'diff_data': diff_data,
                            'commits': [{'id': file_commits[0].commit_id, 'time': commit_time_str}],
                            'description': f"单个提交 {file_commits[0].commit_id[:8]}"
                        })
                        log_print(f"  - ✅ 成功添加diff段: {file_path}", 'APP')
                    except Exception as append_error:
                        log_print(f"  - ❌ 添加diff段时出错: {file_path} - {str(append_error)}", force=True)
                        import traceback; traceback.print_exc()
                else:
                    log_print(f"  - ❌ 未获取到diff数据: {file_path}", 'APP')
            else:
                if are_commits_consecutive_internal(file_commits):
                    log_print(f"  - 连续提交合并: {file_commits[0].commit_id[:8]}..{file_commits[-1].commit_id[:8]}", 'APP')
                    diff_data = handle_consecutive_commits_merge_internal(file_commits)
                    if diff_data:
                        diff_data = clean_json_data(diff_data)
                        diff_sections.append({
                            'file_path': file_path, 'diff_type': 'consecutive_merge',
                            'diff_data': diff_data,
                            'commits': [{'id': c.commit_id, 'time': c.commit_time.isoformat()} for c in file_commits],
                            'description': f"连续提交合并 {file_commits[0].commit_id[:8]}..{file_commits[-1].commit_id[:8]}"
                        })
                        log_print(f"  - ✅ 成功添加连续合并diff段: {file_path}", 'APP')
                else:
                    log_print(f"  - 非连续提交分段处理: {len(file_commits)}个提交", force=True)
                    diff_data = handle_non_consecutive_commits_merge_internal(file_commits)
                    if diff_data:
                        diff_data = clean_json_data(diff_data)
                        diff_sections.append({
                            'file_path': file_path, 'diff_type': 'segmented',
                            'diff_data': diff_data,
                            'commits': [{'id': c.commit_id, 'time': c.commit_time.isoformat()} for c in file_commits],
                            'description': f"非连续提交分段 ({diff_data.get('total_segments', 0)}段)"
                        })
                        log_print(f"  - ✅ 成功添加分段diff段: {file_path}", 'APP')
        except Exception as e:
            log_print(f"  - ❌ 处理文件时出错: {file_path} - {str(e)}", force=True)
            import traceback; traceback.print_exc()

    log_print(f"生成了 {len(diff_sections)} 个diff段", force=True)
    return {
        'type': 'multiple_files', 'sections': diff_sections,
        'total_files': len(file_groups), 'total_sections': len(diff_sections)
    }


def handle_consecutive_commits_merge_internal(file_commits):
    """情况2: 处理相同文件连续commit的合并diff"""
    log_print("处理连续提交的合并diff...", 'INFO')
    earliest_commit = file_commits[0]
    latest_commit = file_commits[-1]
    log_print(f"最早提交: {earliest_commit.commit_id[:8]}", 'INFO')
    log_print(f"最新提交: {latest_commit.commit_id[:8]}", 'INFO')
    repository = earliest_commit.repository
    try:
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            service = ThreadedGitService(repository.url, repository.root_directory,
                               repository.username, repository.token, repository, _active_git_processes)
            is_excel = _excel_cache_service.is_excel_file(earliest_commit.path)
            if is_excel:
                log_print(f"🔍 处理Excel连续提交合并diff", 'APP')
                parent_commit_id = service.get_parent_commit(earliest_commit.commit_id)
                if parent_commit_id:
                    log_print(f"🎯 计算Excel范围diff: {parent_commit_id[:8]}..{latest_commit.commit_id[:8]}", 'APP')
                    try:
                        virtual_previous_commit = Commit()
                        virtual_previous_commit.commit_id = parent_commit_id
                        virtual_previous_commit.repository = repository
                        virtual_previous_commit.path = earliest_commit.path
                        diff_data = _get_unified_diff_data(latest_commit, virtual_previous_commit)
                        if diff_data:
                            diff_data['commit_range'] = f"{earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]}"
                            diff_data['is_merged'] = True
                            return clean_json_data(diff_data)
                        else:
                            log_print("❌ Excel范围diff计算返回空数据", 'APP')
                    except Exception as e:
                        log_print(f"❌ Excel范围diff计算异常: {e}", 'APP')
                        import traceback; traceback.print_exc()
                else:
                    log_print(f"❌ 无法获取最早提交的父提交: {earliest_commit.commit_id[:8]}", 'APP')
                log_print("⚠️ 范围diff失败，回退到单个提交diff", 'APP')
                previous_commit = file_commits[1] if len(file_commits) > 1 else None
                diff_data = _get_unified_diff_data(latest_commit, previous_commit)
                return clean_json_data(diff_data) if diff_data else None
            else:
                parent_commit_id = service.get_parent_commit(earliest_commit.commit_id)
                if parent_commit_id:
                    diff_data = service.get_commit_range_diff(parent_commit_id, latest_commit.commit_id, earliest_commit.path)
                    if diff_data:
                        diff_data['commit_range'] = f"{earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]}"
                        diff_data['is_merged'] = True
                        return diff_data

        elif repository.type == 'svn':
            service = _get_svn_service(repository)
            parent_version = str(int(earliest_commit.version) - 1)
            diff_data = service.get_version_range_diff(parent_version, latest_commit.version, earliest_commit.path)
            if diff_data:
                diff_data['version_range'] = f"{parent_version}..{latest_commit.version}"
                diff_data['is_merged'] = True
                return diff_data
    except Exception as e:
        log_print(f"获取连续提交diff失败: {str(e)}")
    return None


def handle_non_consecutive_commits_merge_internal(file_commits):
    """情况3: 处理相同文件非连续commit的合并diff"""
    log_print("处理非连续提交的合并diff...", 'INFO')
    repository = file_commits[0].repository
    file_path = file_commits[0].path
    all_file_commits = db.session.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.path == file_path
    ).order_by(Commit.commit_time.desc()).all()
    log_print(f"文件 {file_path} 的完整提交历史: {len(all_file_commits)}个")

    selected_commit_ids = {commit.commit_id for commit in file_commits}
    selected_positions = []
    for i, commit in enumerate(all_file_commits):
        if commit.commit_id in selected_commit_ids:
            selected_positions.append((i, commit))
    log_print(f"选中提交的位置: {[pos[0] for pos in selected_positions]}", 'INFO')

    diff_segments = []
    for i, (pos, commit) in enumerate(selected_positions):
        log_print(f"处理提交段 {i+1}: {commit.commit_id[:8]} (位置: {pos}, 'INFO')")
        if pos + 1 < len(all_file_commits):
            previous_commit = all_file_commits[pos + 1]
            log_print(f"  前一提交: {previous_commit.commit_id[:8]}", 'INFO')
            diff_data = get_commit_pair_diff_internal(commit, previous_commit)
            if diff_data:
                diff_data['segment_info'] = {
                    'current': commit.commit_id[:8], 'previous': previous_commit.commit_id[:8],
                    'segment_index': i + 1, 'total_segments': len(selected_positions)
                }
                diff_segments.append(diff_data)
        else:
            log_print("  这是最早的提交，与初始版本比较", 'INFO')
            diff_data = _get_unified_diff_data(commit, None)
            if diff_data:
                diff_data['segment_info'] = {
                    'current': commit.commit_id[:8], 'previous': 'initial',
                    'segment_index': i + 1, 'total_segments': len(selected_positions)
                }
                diff_segments.append(diff_data)

    if diff_segments:
        return {
            'type': 'segmented_diff', 'segments': diff_segments,
            'file_path': file_path, 'total_segments': len(diff_segments)
        }
    return None


# ---------------------------------------------------------------------------
#  智能显示列表 & 缓存检查
# ---------------------------------------------------------------------------

def build_smart_display_list(commits):
    """构建智能显示列表：合并连续提交，分离不同文件"""
    file_groups = defaultdict(list)
    for commit in commits:
        file_groups[commit.path].append(commit)
    display_list = []
    for file_path, file_commits in file_groups.items():
        log_print(f"处理文件显示: {file_path} ({len(file_commits)}个提交)", 'INFO')
        file_commits.sort(key=lambda x: x.commit_time)
        if len(file_commits) == 1:
            commit = file_commits[0]
            cache_available = check_commit_cache_available(commit)
            display_list.append({
                'type': 'single_commit', 'commit': commit, 'commit_id': commit.id,
                'diff_data': None, 'cache_available': cache_available,
                'display_title': f"📄 {commit.path}",
                'display_subtitle': f"提交 {commit.commit_id[:8]}"
            })
        else:
            if are_commits_consecutive_internal(file_commits):
                latest_commit = file_commits[-1]
                earliest_commit = file_commits[0]
                merged_commit = create_merged_commit_display(file_commits)
                cache_available = check_commit_cache_available(latest_commit)
                display_list.append({
                    'type': 'consecutive_merge', 'commit': merged_commit,
                    'commit_id': latest_commit.id, 'diff_data': None,
                    'cache_available': cache_available,
                    'display_title': f"📄 {file_path}",
                    'display_subtitle': f"合并提交 {earliest_commit.commit_id[:8]}..{latest_commit.commit_id[:8]} ({len(file_commits)}个连续提交)",
                    'merged_commits': file_commits,
                    'start_commit': earliest_commit, 'end_commit': latest_commit
                })
            else:
                for i, commit in enumerate(file_commits):
                    cache_available = check_commit_cache_available(commit)
                    display_list.append({
                        'type': 'individual_commit', 'commit': commit,
                        'commit_id': commit.id, 'diff_data': None,
                        'cache_available': cache_available,
                        'display_title': f"📄 {commit.path}",
                        'display_subtitle': f"提交 {commit.commit_id[:8]} (第{i+1}个)",
                        'sequence': i + 1
                    })
    log_print(f"智能显示列表构建完成: {len(display_list)}个显示单元", 'INFO')
    return display_list


def check_commit_cache_available(commit):
    """检查提交的缓存是否可用"""
    if _excel_cache_service.is_excel_file(commit.path):
        cached_diff = _excel_cache_service.get_cached_diff(commit.repository_id, commit.commit_id, commit.path)
        return cached_diff is not None
    return False


def create_merged_commit_display(commits):
    """创建合并提交的显示对象"""
    if not commits:
        return None

    class MergedCommitDisplay:
        def __init__(self, commits):
            self.commits = commits
            self.latest = commits[-1]
            self.earliest = commits[0]
        @property
        def id(self):
            return self.latest.id
        @property
        def commit_id(self):
            return self.latest.commit_id
        @property
        def path(self):
            return self.latest.path
        @property
        def message(self):
            return f"合并了{len(self.commits)}个连续提交: {self.earliest.commit_id[:8]}..{self.latest.commit_id[:8]}"
        @property
        def author(self):
            authors = list(set(c.author for c in self.commits if c.author))
            if len(authors) == 1:
                return authors[0]
            elif len(authors) > 1:
                return f"{authors[0]} 等{len(authors)}人"
            else:
                return "未知"
        @property
        def commit_time(self):
            return self.latest.commit_time
        @property
        def version(self):
            return f"{self.earliest.version}..{self.latest.version}"
        @property
        def status(self):
            return self.latest.status
        @property
        def repository(self):
            return self.latest.repository
        @property
        def repository_id(self):
            return self.latest.repository_id

    return MergedCommitDisplay(commits)


def are_commits_consecutive_internal(commits):
    """检查提交是否在文件历史中连续"""
    if len(commits) <= 1:
        return True
    repository = commits[0].repository
    file_path = commits[0].path
    all_commits = db.session.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.path == file_path
    ).order_by(Commit.commit_time.desc()).all()
    commit_positions = {commit.commit_id: i for i, commit in enumerate(all_commits)}
    selected_positions = []
    for commit in commits:
        if commit.commit_id in commit_positions:
            selected_positions.append(commit_positions[commit.commit_id])
    selected_positions.sort()
    for i in range(1, len(selected_positions)):
        if selected_positions[i] - selected_positions[i-1] != 1:
            return False
    return True


def get_commit_pair_diff_internal(current_commit, previous_commit):
    """获取两个提交之间的diff"""
    try:
        repository = current_commit.repository
        if repository.type == 'git':
            from services.threaded_git_service import ThreadedGitService
            service = ThreadedGitService(repository.url, repository.root_directory, repository.username, repository.token, repository, _active_git_processes)
            if _excel_cache_service.is_excel_file(current_commit.path):
                return _get_unified_diff_data(current_commit, previous_commit)
            else:
                return service.get_commit_range_diff(previous_commit.commit_id, current_commit.commit_id, current_commit.path)
        elif repository.type == 'svn':
            service = _get_svn_service(repository)
            if _excel_cache_service.is_excel_file(current_commit.path):
                log_print(f"SVN Excel文件比较: {current_commit.path}", 'WEEKLY', force=True)
                return _get_unified_diff_data(current_commit, previous_commit)
            else:
                return service.get_version_range_diff(previous_commit.version, current_commit.version, current_commit.path)
    except Exception as e:
        log_print(f"获取提交对diff失败: {str(e)}")
        return None
