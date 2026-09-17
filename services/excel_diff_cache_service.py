#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Excel diff cache service extracted from app.py."""

import json
import time
import threading
import traceback
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_, func, or_, text

from services.performance_metrics_service import get_perf_metrics_service
from services.commit_diff_logic import resolve_page_previous_commit

app = None
db = None
# 这里的值只是**未 configure 时的兜底**；configure_excel_diff_cache_service() 会用
# app.py 的值覆盖它（见下方 global 赋值）。但它是仓库里 DIFF_LOGIC_VERSION 的
# **第三份字面量**，必须与 app.py / config.py 保持一致：
# cleanup_old_cache() 用它判定「diff_version 不匹配 → 当作过期缓存删掉」，
# 一旦它比真实版本旧，就会把**刚生成的当前版本缓存**当成过期数据清掉。
# 一致性由 tests/test_diff_service_fidelity.py::TestDiffLogicVersionSingleSource
# 扫描全部三处锁定（tests/test_diff_logic_version_single_source.py 只覆盖 app.py / config.py）。
DIFF_LOGIC_VERSION = "1.16.0"
DiffCache = None
OperationLog = None
Commit = None
Repository = None
get_unified_diff_data = None

# ---------------------------------------------------------------------------
#  process_excel_diff_background() 的返回状态
#
#  历史行为：成功与失败**都返回 None**，于是调用方（services/task_worker_service.py
#  的 _handle_excel_diff_task）没有任何判断依据，只能无条件把任务标成 completed ——
#  「仓库已删」「提交查不到」这类什么都没做的情况也被记为成功，与真的生成成功在
#  任务表里完全同形（管理员无法区分「已完成」和「根本没做」）。
#  现在显式区分，调用方据此决定 completed / failed。
# ---------------------------------------------------------------------------
BG_STATUS_COMPLETED = 'completed'                        # 真的算完并落了缓存
BG_STATUS_SKIPPED_IN_PROGRESS = 'skipped_in_progress'    # 同一 (repo,commit,file) 已在别的线程处理中
BG_STATUS_REPOSITORY_MISSING = 'repository_missing'      # 仓库不存在 → 调用方应标 failed
BG_STATUS_COMMIT_MISSING = 'commit_missing'              # 提交记录不存在 → 调用方应标 failed
BG_STATUS_ERROR = 'error'                                # 未预期异常 → 调用方应标 failed

# ---------------------------------------------------------------------------
#  缓存状态
#
#  CACHE_STATUS_TRUNCATED：diff_data 超过 MAX_DIFF_DATA_BYTES 时只保存了摘要
#  （行明细被丢弃）。历史行为是**仍然写 completed**，于是页面上每个 sheet 都渲染
#  「工作表 "X" 没有数据或无变更」（templates/diff_partials/excel_diff.html：空 rows
#  分支）—— 用户以为「这个文件真的没改动」，实际是「太大没比完」。
#  现在单独用一个状态，统计/清理/界面都能把它和「真的没有变更」区分开。
# ---------------------------------------------------------------------------
CACHE_STATUS_COMPLETED = 'completed'
CACHE_STATUS_TRUNCATED = 'truncated'

# 界面上应该显示的提示语（渲染层直接用它，别再自己拼字符串）
TRUNCATED_NOTICE = '文件过大，未完整比对'

TRUNCATED_PAYLOAD_MARKER = '"truncated"'

# ---------------------------------------------------------------------------
#  基线（previous_commit_id）—— 必须进缓存键
#
#  同一条 (repository_id, commit_id, file_path) 的缓存记录，「和谁比」在仓库里
#  至少有 5 条**互不相同**的解析路径：
#
#   1) 后台缓存任务：本文件 process_excel_diff_background()。先按
#      (commit_time, id) 双键取前一条，取不到退到 id 单键，最后回退
#      resolve_previous_commit()（可能走 VCS 文件历史）。
#   2) 提交 diff 页/新页：services/commit_diff_view_service.py、
#      services/commit_diff_page_service.py、services/commit_diff_new_page_service.py。
#      统一用 resolve_previous_commit(commit, file_commits=...)，而那里的
#      file_commits 是该文件全部提交**按 commit_time 单键降序**（同秒提交的先后
#      由数据库返回顺序决定），与 (1) 的 id 次序键并不等价。
#   3) 「重新计算差异」刷新接口：commit_diff_page_service 里那段
#      `Commit.query.filter(commit_time < commit.commit_time).order_by(commit_time.desc())`
#      —— 没有同秒 id 兜底、也没有 VCS 回退；同秒提交会算出 previous=None，
#      于是整份文件被当成「新增」。
#   4) 显式传入的 previous：services/vcs_content_service.get_unified_diff_data(commit,
#      previous_commit) 的入参，来自 services/commit_diff_logic.py 的区间基准
#      （get_commit_pair_diff_internal 的 base_commit、
#       `file_commits[1]`、`all_file_commits[pos + 1]` 等）——「区间起点」与
#      「紧邻前一条」通常不是同一个提交。
#   5) 周版本窗口起点：services/weekly_version_logic.generate_weekly_merged_diff()，
#      按 `commit_time < 窗口起点(UTC)` 取前一条；且新缓存建完后若 base 为空，
#      还会被 get_real_base_commit_from_vcs() 二次改写成 VCS 的真实基准。
#   6) 增量同步记录的 Repository.last_sync_commit_id：
#      services/incremental_cache_system.py 的 get_existing_cached_files() 用它当
#      「这个提交有没有缓存」的判据（它过去还按一个不存在的列过滤，见该文件注释）。
#
#  在这之前缓存键只有 (repository_id, commit_id, file_path, diff_version)：
#  上面任意一条路径先写入，其它路径就会**直接命中别人基线算出来的结果** ——
#  结果错、但不报错、也不重算（第二个人看到的是第一个人的基线）。
#  所以现在：
#    * 读：get_cached_diff() 把 previous_commit_id 纳入过滤条件，
#      基线不一致 = 未命中 → 调用方重算；
#    * 写：save_cached_diff() 把 previous_commit_id 纳入「是否已存在」判定，
#      不同基线各占一行，不再互相覆盖。
# ---------------------------------------------------------------------------

# 调用方「没传基线」与「明确要求无基线（新增文件）」是两件事，用哨兵区分：
# 不传 → 服务自己解析权威基线；传 None → 就是要 previous 为 NULL 的那一行。
BASELINE_UNSET = object()


def normalize_baseline_id(value):
    """基线标识归一化：None / 空串 / 纯空白统一成 None（= 没有基线）。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def payload_is_truncated(payload):
    """diff_data 负载是不是「文件过大只存了摘要」。

    先做一次廉价的子串预筛再 json.loads：命中截断时负载已经被换成摘要（很小），
    但正常负载可能接近 20MB，不该为每条缓存命中都付一次完整解析。
    我们的写入方（save_cached_diff）总把 'truncated' 放在 JSON 前几个键，
    因此只看前 4096 字符足够；预筛只是「可能命中」，最终以顶层标记为准。
    """
    if not payload:
        return False
    if isinstance(payload, dict):
        return bool(payload.get('truncated'))
    try:
        text = str(payload)
    except Exception:
        return False
    if TRUNCATED_PAYLOAD_MARKER not in text[:4096]:
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return bool(isinstance(parsed, dict) and parsed.get('truncated'))


def _noop_log(*args, **kwargs):
    """Fallback no-op logger when log_print is not configured."""
    pass

log_print = _noop_log


def configure_excel_diff_cache_service(*, app_instance, db_instance, diff_logic_version, diff_cache_model, operation_log_model, commit_model, repository_model, log_print_func, unified_diff_func):
    """Wire runtime dependencies for ExcelDiffCacheService."""
    global app, db, DIFF_LOGIC_VERSION, DiffCache, OperationLog, Commit, Repository, log_print, get_unified_diff_data
    app = app_instance
    db = db_instance
    DIFF_LOGIC_VERSION = diff_logic_version
    DiffCache = diff_cache_model
    OperationLog = operation_log_model
    Commit = commit_model
    Repository = repository_model
    log_print = log_print_func
    get_unified_diff_data = unified_diff_func


def find_previous_commit_in_db(repository_id, commit, file_path):
    """在数据库里找「紧邻的前一条提交」（基线）。

    这是路径 (1)(2)(3) 共用的**数据库内**基线定义：
      * 优先按 (commit_time, id) 双键降序取前一条 —— 同秒提交靠自增 id 定序，
        避免同一秒内多条提交的前后关系随数据库返回顺序漂移；
      * 取不到再退到纯 id 单键（commit_time 缺失的历史数据）。
    只查数据库，不触发 VCS 子进程 —— 缓存读路径会高频调用它。

    注意：process_excel_diff_background() 里保留了**同样逻辑的内联查询**
    （tests/test_excel_diff_performance_safety.py 的
    test_background_excel_previous_commit_query_has_time_and_id_tiebreak 直接断言
    那个函数体里必须有 `Commit.commit_time == commit.commit_time` 与
    `Commit.id < commit.id`）。两处必须保持一致：本函数是读缓存时的基线口径，
    那个函数是写缓存时的基线口径，不一致就等于缓存键白加。
    """
    if Commit is None or commit is None:
        return None
    previous_commit = None
    if commit.commit_time is not None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository_id,
            Commit.path == file_path,
            or_(
                Commit.commit_time < commit.commit_time,
                and_(Commit.commit_time == commit.commit_time, Commit.id < commit.id),
            ),
        ).order_by(Commit.commit_time.desc(), Commit.id.desc()).first()
    if previous_commit is None:
        previous_commit = Commit.query.filter(
            Commit.repository_id == repository_id,
            Commit.path == file_path,
            Commit.id < commit.id,
        ).order_by(Commit.id.desc()).first()
    return previous_commit


def resolve_authoritative_baseline(repository_id, commit_id, file_path):
    """解析「权威基线」，返回 (是否解析成功, previous_commit_id)。

    只做数据库解析（见 find_previous_commit_in_db），**不**走 VCS 回退：缓存读路径
    不该为了校验一个基线去起 git 子进程。数据库判不出来时返回 (False, None)，
    调用方必须退化为「不校验基线」而不是当成「基线为空」—— 后者会让所有缓存
    永远不命中、每次访问都全量重算。
    """
    if Commit is None or db is None:
        return False, None
    try:
        commit = Commit.query.filter_by(
            repository_id=repository_id,
            commit_id=commit_id,
            path=file_path,
        ).first()
        if commit is None:
            # 少数调用方只按 commit_id 建缓存（path 可能带/不带前缀），放宽一次
            commit = Commit.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
            ).first()
        if commit is None:
            return False, None
        previous_commit = find_previous_commit_in_db(repository_id, commit, file_path)
        if previous_commit is None:
            # 解析成功，结论就是「该文件在这条提交之前没有更早的提交」。
            # 此时后台路径可能仍会通过 VCS 回退拿到一个真实基线，两边会不一致；
            # 那种情况留给调用方显式传 previous_commit_id（见 get_cached_diff）。
            return True, None
        return True, normalize_baseline_id(getattr(previous_commit, 'commit_id', None))
    except Exception as exc:
        log_print(f"⚠️ 解析缓存基线失败 repo={repository_id}, commit={commit_id}: {exc}", 'CACHE')
        return False, None

class ExcelDiffCacheService:
    """Excel文件差异缓存服务"""
    
    def __init__(self):
        self._processing_commits = set()  # 正在处理的提交ID集合
        self._processing_lock = threading.Lock()  # 线程安全锁
        self._log_write_count = 0  # 日志写入计数器（用于控制清理频率）
        self.operation_logs = []  # 操作日志列表ID集合
        self.max_cache_count = 1000  # 最大缓存数量
        self.long_processing_threshold = 10.0  # 长处理时间阈值（秒）
        self.long_processing_expire_days = 90  # 长处理文件缓存保留天数（3个月）
        # ===== 日志聚合缓冲区（1分钟窗口）=====
        self._log_buffer = {}       # { project_code: { 'success': count, 'error': count, 'files': set(), 'last_flush': datetime } }
        self._log_buffer_lock = threading.Lock()
        self._LOG_FLUSH_INTERVAL = 60  # 聚合窗口：60秒
        
    def is_excel_file(self, file_path):
        """检查文件是否为Excel文件"""
        excel_extensions = ['.xlsx', '.xls', '.xlsm', '.xlsb', '.csv']
        return any(file_path.lower().endswith(ext) for ext in excel_extensions)
    
    def _get_project_code(self, repository_id):
        """根据 repository_id 获取项目编号（如 G119），带简易缓存"""
        if repository_id is None:
            return None
        # 简易进程内缓存，避免重复查询
        cache_attr = '_project_code_cache'
        if not hasattr(self, cache_attr):
            setattr(self, cache_attr, {})
        cache = getattr(self, cache_attr)
        if repository_id in cache:
            return cache[repository_id]
        try:
            # 直接SQL查询，避免关系延迟加载导致拿不到项目编号
            sql = text(
                "SELECT p.code FROM repository r "
                "JOIN project p ON p.id = r.project_id "
                "WHERE r.id = :repository_id LIMIT 1"
            )
            code = db.session.execute(sql, {"repository_id": repository_id}).scalar()
            if code:
                cache[repository_id] = str(code)
                return str(code)
        except Exception as e:
            log_print(f"获取项目编号失败 repository_id={repository_id}: {e}", 'ERROR')
        cache[repository_id] = None
        return None

    @staticmethod
    def _ensure_project_prefix(message, project_code):
        msg = str(message or "")
        if msg.startswith("【"):
            return msg
        code = project_code or "UNKNOWN"
        return f"【{code}】{msg}"

    def _flush_log_buffer(self, project_code, force=False):
        """刷新指定项目的日志缓冲区，将聚合摘要写入数据库"""
        with self._log_buffer_lock:
            buf = self._log_buffer.get(project_code)
            if not buf:
                return
            now = time.time()
            elapsed = now - buf.get('last_flush', 0)
            if not force and elapsed < self._LOG_FLUSH_INTERVAL:
                return  # 窗口未到，不刷新
            # 构建聚合摘要
            success_cnt = buf.get('success', 0)
            error_cnt = buf.get('error', 0)
            total = success_cnt + error_cnt
            files = buf.get('files', set())
            resolved_project_code = buf.get('project_code') or project_code
            summary_parts = []
            if success_cnt > 0:
                summary_parts.append(f"成功 {success_cnt} 个")
            if error_cnt > 0:
                summary_parts.append(f"失败 {error_cnt} 个")
            summary_msg = self._ensure_project_prefix(
                f"缓存批量统计: {'，'.join(summary_parts)}（共 {total} 个文件）",
                resolved_project_code,
            )
            log_type = 'success' if error_cnt == 0 else ('error' if success_cnt == 0 else 'warning')
            # 重置缓冲区
            self._log_buffer[project_code] = {
                'success': 0, 'error': 0, 'files': set(),
                'last_flush': now, 'repository_id': buf.get('repository_id'),
                'project_code': resolved_project_code,
            }
        # 在锁外写入数据库
        try:
            log_entry = OperationLog(
                log_type=log_type,
                message=summary_msg,
                source='excel_cache',
                repository_id=buf.get('repository_id'),
                file_path=None
            )
            db.session.add(log_entry)
            self._log_write_count += 1
            if self._log_write_count % 50 == 0:
                self._cleanup_old_logs()
            db.session.commit()
            # 内存日志
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            self.operation_logs.append({'time': timestamp, 'message': summary_msg, 'type': log_type})
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]
        except Exception as e:
            log_print(f"刷新聚合日志失败: {e}", 'ERROR')

    def log_cache_operation(self, message, log_type='info', repository_id=None, file_path=None):
        """记录缓存操作日志到数据库。
        对于高频的 success/error 类型日志，使用 1 分钟聚合窗口批量写入。
        其他类型（info/warning）立即写入并附带项目编号前缀。
        """
        try:
            # 获取项目编号前缀
            project_code = self._get_project_code(repository_id)

            # 高频日志聚合：success 和 error 类型进入缓冲区
            if log_type in ('success', 'error') and repository_id is not None:
                buf_key = project_code or f"repo_{repository_id}"
                with self._log_buffer_lock:
                    if buf_key not in self._log_buffer:
                        self._log_buffer[buf_key] = {
                            'success': 0, 'error': 0, 'files': set(),
                            'last_flush': time.time(), 'repository_id': repository_id,
                            'project_code': project_code,
                        }
                    buf = self._log_buffer[buf_key]
                    buf[log_type] = buf.get(log_type, 0) + 1
                    if not buf.get('project_code') and project_code:
                        buf['project_code'] = project_code
                    if file_path:
                        buf['files'].add(file_path)
                # 检查是否需要刷新
                self._flush_log_buffer(buf_key)
                return

            # 非聚合日志：立即写入，添加项目编号前缀
            prefixed_message = self._ensure_project_prefix(message, project_code)

            log_entry = OperationLog(
                log_type=log_type,
                message=prefixed_message,
                source='excel_cache',
                repository_id=repository_id,
                file_path=file_path
            )
            db.session.add(log_entry)

            # 每50次写入清理一次旧日志 (#34)
            self._log_write_count += 1
            if self._log_write_count % 50 == 0:
                self._cleanup_old_logs()

            db.session.commit()

            # 同时保持内存中的日志（用于向后兼容），使用北京时间
            from utils.timezone_utils import now_beijing
            timestamp = now_beijing().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': prefixed_message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            # 保持最多100条内存日志
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]

        except Exception as e:
            # 如果数据库操作失败，至少保持内存日志
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
            memory_log = {
                'time': timestamp,
                'message': message,
                'type': log_type
            }
            self.operation_logs.append(memory_log)
            if len(self.operation_logs) > 100:
                self.operation_logs = self.operation_logs[-100:]
            log_print(f"记录操作日志到数据库失败: {e}", 'ERROR')

    def _cleanup_old_logs(self):
        """清理超过200条的旧日志（批量DELETE，避免逐行删除）"""
        try:
            # 获取当前日志总数
            total_count = OperationLog.query.filter_by(source='excel_cache').count()

            if total_count > 200:
                # 使用子查询批量删除，避免将对象加载到Python内存
                excess_count = total_count - 200
                subquery = (
                    db.session.query(OperationLog.id)
                    .filter_by(source='excel_cache')
                    .order_by(OperationLog.created_at.asc())
                    .limit(excess_count)
                    .subquery()
                )
                OperationLog.query.filter(
                    OperationLog.id.in_(db.session.query(subquery))
                ).delete(synchronize_session=False)

        except Exception as e:
            log_print(f"清理旧操作日志失败: {e}", 'ERROR')
    
    def _resolve_expected_baseline(self, repository_id, commit_id, file_path, previous_commit_id):
        """决定这次查询要求缓存记录带哪个基线。

        返回 (是否校验基线, 期望的 previous_commit_id)。
        「不校验」只发生在两种情况下：调用方明确交出了决定权而数据库又判不出来。
        这两种情况下宁可退回历史行为（取最新一行），也不要误判成「基线不一致」
        —— 后者会让缓存永远不命中、每次访问都重算整份大文件。
        """
        if previous_commit_id is not BASELINE_UNSET:
            return True, normalize_baseline_id(previous_commit_id)
        resolved, baseline = resolve_authoritative_baseline(repository_id, commit_id, file_path)
        if not resolved:
            log_print(
                f"⚠️ 无法解析缓存基线（数据库里找不到对应提交），"
                f"本次不校验 previous_commit_id: repo={repository_id}, commit={commit_id}, file={file_path}",
                'CACHE'
            )
        return resolved, baseline

    @staticmethod
    def _build_cache_query(repository_id, commit_id, file_path, expected_baseline, diff_version=BASELINE_UNSET,
                           statuses=(CACHE_STATUS_COMPLETED, CACHE_STATUS_TRUNCATED)):
        """按 (仓库, 提交, 文件, 状态[, 版本][, 基线]) 构造缓存查询。

        基线是**缓存键的一部分**：expected_baseline 传 BASELINE_UNSET 表示本次不校验，
        传 None 表示要求 previous_commit_id IS NULL（新增文件），传字符串则要求精确匹配。
        `Column == None` 在 SQLAlchemy 里渲染成 IS NULL，正是我们要的语义。
        """
        query = (
            DiffCache.query
            .populate_existing()
            .filter(DiffCache.repository_id == repository_id)
            .filter(DiffCache.commit_id == commit_id)
            .filter(DiffCache.file_path == file_path)
            .filter(DiffCache.cache_status.in_(statuses))
        )
        if diff_version is not BASELINE_UNSET:
            query = query.filter(DiffCache.diff_version == diff_version)
        if expected_baseline is not BASELINE_UNSET:
            query = query.filter(DiffCache.previous_commit_id == expected_baseline)
        return query

    def get_cached_diff(self, repository_id, commit_id, file_path, previous_commit_id=BASELINE_UNSET):
        """获取缓存的差异数据，检查**版本号 + 基线**匹配。

        previous_commit_id：
          * 不传（BASELINE_UNSET）：服务自己解析权威基线再校验（推荐，老调用方无需改动）；
          * 传 None：明确要「无基线」的缓存（新增文件）；
          * 传字符串：明确要该基线的缓存（调用方自己知道和谁比，例如区间合并 diff）。
        基线不一致 → 当未命中，让调用方重算。绝不返回「别人基线算出来的」diff，
        因为那种结果**看不出错**（同样的表格、同样格式，只是比错了对象）。
        """
        try:
            log_print(f"🔍 查询缓存: repo={repository_id}, commit={commit_id[:8]}, file={file_path}", 'CACHE')

            baseline_checked, expected_baseline = self._resolve_expected_baseline(
                repository_id, commit_id, file_path, previous_commit_id
            )
            baseline_filter = expected_baseline if baseline_checked else BASELINE_UNSET

            cache = self._build_cache_query(
                repository_id, commit_id, file_path, baseline_filter, DIFF_LOGIC_VERSION
            ).order_by(DiffCache.updated_at.desc()).first()

            if cache:
                log_print(f"✅ 缓存命中: {file_path} | 版本: {cache.diff_version} | 创建时间: {cache.created_at}", 'CACHE')
                log_print(f"📊 缓存数据大小: {len(cache.diff_data)} 字符 | 处理时间: {cache.processing_time:.2f}秒", 'CACHE')
                if baseline_filter is not BASELINE_UNSET:
                    log_print(f"🧷 基线校验通过: previous={expected_baseline or '(无)'}", 'CACHE')
                if cache.cache_status == CACHE_STATUS_TRUNCATED or payload_is_truncated(cache.diff_data):
                    # 明确喊出来：这条缓存**不是**「没有变更」，而是「太大没比完」。
                    log_print(
                        f"⚠️ 命中截断缓存（{TRUNCATED_NOTICE}）: {file_path} | 状态={cache.cache_status}",
                        'CACHE', force=True
                    )
                return cache

            # 未命中：区分「根本没有」和「有但基线/版本对不上」，后者要清掉以免反复被查到
            stale = self._build_cache_query(
                repository_id, commit_id, file_path, baseline_filter
            ).order_by(DiffCache.updated_at.desc()).first()

            if stale is not None:
                log_print(
                    f"⚠️ 发现基线或版本不匹配的缓存: {file_path} | "
                    f"缓存版本: {stale.diff_version} → 当前版本: {DIFF_LOGIC_VERSION} | "
                    f"缓存基线: {stale.previous_commit_id or '(无)'} → 本次要求: "
                    f"{expected_baseline if baseline_checked else '(不校验)'}",
                    'CACHE', force=True
                )
                if stale.diff_version != DIFF_LOGIC_VERSION:
                    # 标记旧缓存为过期，稍后清理
                    stale.cache_status = 'outdated'
                    db.session.commit()
                    log_print(f"🗑️ 旧版本缓存已标记为过期", 'CACHE')
                else:
                    log_print("🧷 基线不一致 → 视为未命中，交给调用方重算", 'CACHE', force=True)
            else:
                log_print(f"❌ 缓存未命中: {file_path} | 需要重新计算diff", 'CACHE')

            return None
        except Exception as e:
            log_print(f"❌ 获取缓存差异失败: {e}", 'CACHE', force=True)
            # 上面可能执行过 `stale.cache_status = 'outdated'` 的写操作：
            # 出错不 rollback 会把脏事务留在 session 里，影响后续查询。
            try:
                db.session.rollback()
            except Exception as rollback_error:
                log_print(f"获取缓存差异失败后回滚也失败: {rollback_error}", 'CACHE', force=True)
            return None

    @staticmethod
    def is_truncated_cache(cache_row):
        """这条缓存是不是「文件过大只存了摘要」。

        渲染层/API 层应当据此显示 TRUNCATED_NOTICE，而不是把空 rows 当「无变更」。
        兼容历史数据：过去截断记录的状态是 completed，只能看负载里的 truncated 标记。
        """
        if cache_row is None:
            return False
        if getattr(cache_row, 'cache_status', None) == CACHE_STATUS_TRUNCATED:
            return True
        return payload_is_truncated(getattr(cache_row, 'diff_data', None))

    def optimize_diff_data(self, diff_data):
        """优化diff数据，只保留有变更的行"""
        if not diff_data or diff_data.get('type') != 'excel':
            return diff_data
        
        try:
            optimized_data = dict(diff_data)
            original_size = 0
            optimized_size = 0

            sheets = diff_data.get('sheets') or {}
            optimized_sheets = {}
            for sheet_name, sheet_data in sheets.items():
                if not isinstance(sheet_data, dict):
                    optimized_sheets[sheet_name] = sheet_data
                    continue
                new_sheet_data = dict(sheet_data)
                if 'rows' in sheet_data:
                    original_rows = sheet_data.get('rows') or []
                    original_size += len(original_rows)

                    # 只保留有变更的行（added, removed, modified）
                    changed_rows = [
                        row for row in original_rows
                        if row.get('status') in ['added', 'removed', 'modified']
                    ]

                    new_sheet_data['rows'] = changed_rows
                    optimized_size += len(changed_rows)
                optimized_sheets[sheet_name] = new_sheet_data

            optimized_data['sheets'] = optimized_sheets
            
            log_print(f"🗜️ diff数据优化: {original_size} 行 → {optimized_size} 行 (减少 {original_size - optimized_size} 行)", 'CACHE')
            return optimized_data
            
        except Exception as e:
            log_print(f"❌ diff数据优化失败: {e}", 'CACHE', force=True)
            return diff_data

    def _collect_excel_diff_metrics(self, diff_data):
        """收集Excel diff简要指标，用于性能观测日志。"""
        metrics = {
            'sheet_count': 0,
            'changed_rows': 0,
            'summary': {},
        }
        if not isinstance(diff_data, dict) or diff_data.get('type') != 'excel':
            return metrics
        try:
            sheets = diff_data.get('sheets') or {}
            metrics['sheet_count'] = len(sheets)
            changed_rows = 0
            for sheet_data in sheets.values():
                rows = sheet_data.get('rows') or []
                changed_rows += len(rows)
            metrics['changed_rows'] = changed_rows
            metrics['summary'] = diff_data.get('summary') or {}
        except Exception:
            pass
        return metrics
    
    # diff_data 序列化后最大允许 20MB (#40)
    MAX_DIFF_DATA_BYTES = 20 * 1024 * 1024

    def save_cached_diff(self, repository_id, commit_id, file_path, diff_data, processing_time=0.0, file_size=0, previous_commit_id=BASELINE_UNSET, commit_time=None):
        """保存差异数据到缓存，支持智能缓存策略。

        previous_commit_id 现在是**缓存键的一部分**（见文件头「基线」注释块）：
          * 不传（BASELINE_UNSET）：服务自己解析权威基线后落库（老调用方无需改动）；
          * 传 None：这条提交没有基线（新增文件）；
          * 传具体值：调用方自己知道和谁比（例如区间合并 diff 的起点）。
        「是否已存在」的判定也带基线，因此两个不同基线各占一行，不会互相覆盖 ——
        否则后者会把前者刚算出来的、属于别的基线结果直接改写成自己的。
        """
        try:
            log_print(f"💾 保存缓存: repo={repository_id}, commit={commit_id[:8]}, file={file_path}", 'CACHE')

            if previous_commit_id is BASELINE_UNSET:
                baseline_known, resolved_baseline = resolve_authoritative_baseline(
                    repository_id, commit_id, file_path
                )
                # 解析不出来时落 NULL：读路径同样解析不出来 → 同样不校验，两边一致。
                previous_commit_id = resolved_baseline if baseline_known else None
            else:
                previous_commit_id = normalize_baseline_id(previous_commit_id)

            # 统一在保存入口执行轻量化，确保所有调用方行为一致
            normalized_diff_data = diff_data
            if isinstance(diff_data, dict) and diff_data.get('type') == 'excel':
                normalized_diff_data = self.optimize_diff_data(diff_data)

            # --- #40: 序列化并检查大小限制 ---
            diff_json = json.dumps(normalized_diff_data)
            diff_bytes = len(diff_json.encode('utf-8'))
            payload_mb = diff_bytes / (1024 * 1024)
            is_truncated = diff_bytes > self.MAX_DIFF_DATA_BYTES
            if is_truncated:
                log_print(
                    f"⚠️ diff_data 超出大小限制: {file_path} | "
                    f"{payload_mb:.2f}MB > {self.MAX_DIFF_DATA_BYTES / (1024*1024):.0f}MB，"
                    f"仅保存摘要信息", 'CACHE', force=True)
                # 用精简占位数据替代，保留统计但丢弃行详情
                summary_data = {
                    'type': normalized_diff_data.get('type', 'excel'),
                    'truncated': True,
                    # 显式写清楚「为什么只有摘要」，让渲染层不必靠猜
                    'truncation_reason': 'payload_too_large',
                    'notice': TRUNCATED_NOTICE,
                    'original_size_mb': round(payload_mb, 2),
                    'max_size_mb': round(self.MAX_DIFF_DATA_BYTES / (1024 * 1024), 2),
                    'error': f'数据过大({payload_mb:.1f}MB)，已截断。请在线查看差异。',
                }
                if 'sheets' in normalized_diff_data:
                    summary_data['sheets'] = {}
                    for sheet_name, sheet_info in normalized_diff_data['sheets'].items():
                        summary_data['sheets'][sheet_name] = {
                            'stats': sheet_info.get('stats', {}),
                            'rows': [],            # 清空行数据
                            'headers': sheet_info.get('headers', []),
                        }
                diff_json = json.dumps(summary_data)
                log_print(f"📦 已替换为摘要数据: {len(diff_json)} 字符", 'CACHE')

            # 截断结果绝不能标成 completed：那会让界面上把「太大没比完」显示成
            # 「工作表没有数据或无变更」，用户以为文件真的没改动。
            cache_status = CACHE_STATUS_TRUNCATED if is_truncated else CACHE_STATUS_COMPLETED

            # 判断是否为长处理时间文件
            is_long_processing = processing_time > self.long_processing_threshold

            # 计算过期时间
            expire_at = None
            if is_long_processing:
                # 长处理文件保存3个月
                expire_at = datetime.now(timezone.utc) + timedelta(days=self.long_processing_expire_days)
                log_print(f"⏱️ 长处理文件({processing_time:.2f}s)，缓存3个月至: {expire_at.strftime('%Y-%m-%d')}", 'CACHE')
            else:
                # 普通文件根据1000条限制管理，不设置过期时间
                log_print(f"⚡ 普通处理文件({processing_time:.2f}s)，按1000条限制管理", 'CACHE')

            # 检查是否已存在（**带基线**：不同基线是不同缓存，不互相覆盖）
            existing_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path,
                previous_commit_id=previous_commit_id,
            ).order_by(DiffCache.updated_at.desc()).first()

            if existing_cache:
                # 更新现有缓存（使用已序列化的 diff_json）
                existing_cache.diff_data = diff_json
                existing_cache.processing_time = processing_time
                existing_cache.file_size = file_size
                existing_cache.cache_status = cache_status
                existing_cache.diff_version = DIFF_LOGIC_VERSION
                existing_cache.is_long_processing = is_long_processing
                existing_cache.expire_at = expire_at
                existing_cache.updated_at = datetime.now(timezone.utc)
                if commit_time:
                    existing_cache.commit_time = commit_time
                log_print(f"🔄 更新现有缓存: {file_path}", 'CACHE')
            else:
                # 创建新缓存（使用已序列化的 diff_json）
                new_cache = DiffCache(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    previous_commit_id=previous_commit_id,
                    diff_data=diff_json,
                    processing_time=processing_time,
                    file_size=file_size,
                    cache_status=cache_status,
                    diff_version=DIFF_LOGIC_VERSION,
                    commit_time=commit_time,
                    is_long_processing=is_long_processing,
                    expire_at=expire_at
                )
                db.session.add(new_cache)
                log_print(f"💾 创建新缓存: {file_path}", 'CACHE')

            db.session.commit()

            # 保存后检查是否需要清理旧缓存 (#33: 移除冗余验证查询)
            if not is_long_processing:
                self._cleanup_old_cache(repository_id)

            final_bytes = len(diff_json.encode('utf-8'))
            log_print(
                f"✅ 缓存保存成功: {file_path} | 状态: {cache_status} | 基线: {previous_commit_id or '(无)'} | "
                f"处理时间: {processing_time:.2f}秒 | payload={final_bytes / 1024:.1f}KB",
                'CACHE'
            )
            if is_truncated:
                log_print(
                    f"📢 该缓存只保存了摘要（{TRUNCATED_NOTICE}），状态记作 {CACHE_STATUS_TRUNCATED}，"
                    f"界面必须显示「{TRUNCATED_NOTICE}」而不是「无变更」: {file_path}",
                    'CACHE', force=True
                )
            return True

        except Exception as e:
            log_print(f"❌ 保存缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return False

    def cache_diff_error(self, repository_id, commit_id, file_path, error_message):
        """缓存错误信息"""
        try:
            existing_cache = DiffCache.query.filter_by(
                repository_id=repository_id,
                commit_id=commit_id,
                file_path=file_path
            ).first()
            
            if existing_cache:
                existing_cache.cache_status = 'failed'
                existing_cache.error_message = error_message
                existing_cache.updated_at = datetime.now(timezone.utc)
            else:
                cache = DiffCache(
                    repository_id=repository_id,
                    commit_id=commit_id,
                    file_path=file_path,
                    diff_data='{}',
                    cache_status='failed',
                    error_message=error_message
                )
                db.session.add(cache)
            
            db.session.commit()
        except Exception as e:
            log_print(f"缓存错误信息失败: {e}", 'CACHE', force=True)
            db.session.rollback()
    
    def get_recent_excel_commits(self, repository, limit=1000):
        """获取最近的Excel文件提交（从最近1000条提交中筛选）"""
        try:
            # 先获取最近1000条提交记录，应用仓库起始日期过滤
            query = Commit.query.filter(Commit.repository_id == repository.id)
            
            # 应用仓库配置的起始日期过滤
            # repository.start_date 是**北京墙钟**（纯文本日期输入框），
            # Commit.commit_time 是 naive-UTC 墙钟 —— 必须先换算，
            # 否则起始日 00:00–08:00（北京）的提交会被排除在缓存预热之外。
            if repository.start_date:
                from utils.timezone_utils import beijing_wallclock_to_utc_naive
                start_date_utc = beijing_wallclock_to_utc_naive(repository.start_date)
                query = query.filter(Commit.commit_time >= start_date_utc)
            
            recent_commits = query.order_by(Commit.commit_time.desc()).limit(limit).all()
            
            # 从中筛选出Excel文件
            excel_commits = []
            for commit in recent_commits:
                if (commit.path.endswith('.xlsx') or 
                    commit.path.endswith('.xls') or 
                    commit.path.endswith('.xlsm') or 
                    commit.path.endswith('.xlsb') or 
                    commit.path.endswith('.csv')):
                    excel_commits.append(commit)
            
            commits = excel_commits
            
            # 进一步过滤符合仓库正则条件的文件
            filtered_commits = []
            if repository.path_regex:
                import re
                pattern = re.compile(repository.path_regex)
                for commit in commits:
                    if pattern.search(commit.path):
                        filtered_commits.append(commit)
            else:
                filtered_commits = commits
            
            log_print(f"📊 从最近{limit}条提交中筛选出{len(filtered_commits)}个Excel文件", 'CACHE')
            return filtered_commits
        except Exception as e:
            log_print(f"获取最近Excel提交失败: {e}", 'CACHE', force=True)
            return []
    
    def process_excel_diff_background(self, repository_id, commit_id, file_path):
        """后台处理Excel文件差异。

        返回值（BG_STATUS_*）供调用方判断任务该标 completed 还是 failed。
        历史行为是「成功与失败都返回 None」，导致调用方只能无条件标 completed。
        """
        perf_metrics_service = get_perf_metrics_service()
        try:
            log_print(f"开始后台处理Excel差异: repo={repository_id}, commit={commit_id}, file={file_path}", 'EXCEL')

            # 检查是否已在处理中（线程安全）
            task_key = f"{repository_id}_{commit_id}_{file_path}"
            with self._processing_lock:
                if task_key in self._processing_commits:
                    log_print(f"任务已在处理中，跳过: {task_key}", 'EXCEL')
                    return BG_STATUS_SKIPPED_IN_PROGRESS
                self._processing_commits.add(task_key)
            
            # 确保在Flask应用上下文中执行
            with app.app_context():
                try:
                    query_start = time.time()
                    repository = db.session.get(Repository, repository_id)
                    if not repository:
                        log_print(f"仓库不存在: {repository_id}", 'EXCEL', force=True)
                        return BG_STATUS_REPOSITORY_MISSING
                    project_id = getattr(repository, "project_id", "") or ""
                    project_code = ""
                    try:
                        repository_project = getattr(repository, "project", None)
                        if repository_project:
                            project_code = (getattr(repository_project, "code", "") or "").strip()
                    except Exception:
                        project_code = ""
                    
                    # 获取提交信息
                    commit = Commit.query.filter_by(
                        repository_id=repository_id,
                        commit_id=commit_id,
                        path=file_path
                    ).first()
                    
                    if not commit:
                        log_print(f"提交不存在: {commit_id}, {file_path}", 'EXCEL', force=True)
                        return BG_STATUS_COMMIT_MISSING
                    
                    # 与页头/正文/接口**共用同一个**「前一提交」解析。
                    # 后台任务以前自己写了一条按 (commit_time, id) 的查询、解析不出来
                    # 才回退，于是它写进缓存的差异可以属于另一条基线，而这份缓存会被
                    # 页面和接口读走（线上 6767 的接口 HTML 就是这么错的）。
                    previous_commit = resolve_page_previous_commit(commit)
                    query_time = time.time() - query_start
                    
                    diff_start = time.time()
                    
                    # 使用统一差异服务处理
                    diff_data = get_unified_diff_data(commit, previous_commit)
                    
                    processing_time = time.time() - diff_start
                    
                    if diff_data and diff_data.get('type') == 'excel':
                        metrics = self._collect_excel_diff_metrics(diff_data)
                        cache_start = time.time()
                        # 缓存成功的差异数据
                        self.save_cached_diff(
                            repository_id=repository_id,
                            commit_id=commit_id,
                            file_path=file_path,
                            diff_data=diff_data,
                            previous_commit_id=previous_commit.commit_id if previous_commit else None,
                            processing_time=processing_time
                        )
                        cache_time = time.time() - cache_start
                        total_time = time.time() - query_start
                        log_print(f"💾 Excel差异缓存成功: {file_path} | 版本: {DIFF_LOGIC_VERSION} | 耗时: {processing_time:.2f}秒", 'EXCEL')
                        log_print(
                            f"📈 后台Excel diff指标: sheets={metrics['sheet_count']}, "
                            f"rows={metrics['changed_rows']}, summary={metrics['summary']} | "
                            f"query={query_time:.2f}s, diff={processing_time:.2f}s, cache={cache_time:.2f}s, total={total_time:.2f}s",
                            'EXCEL'
                        )
                        perf_metrics_service.record(
                            "background_excel_cache",
                            success=True,
                            metrics={
                                "total_ms": total_time * 1000,
                                "query_ms": query_time * 1000,
                                "diff_ms": processing_time * 1000,
                                "cache_ms": cache_time * 1000,
                                "sheet_count": metrics["sheet_count"],
                                "changed_rows": metrics["changed_rows"],
                            },
                            tags={
                                "source": "background_excel",
                                "repository_id": repository_id,
                                "project_id": project_id,
                                "project_code": project_code,
                                "file_path": file_path,
                            },
                        )
                        
                        # 记录到操作日志
                        self.log_cache_operation(f"✅ 缓存生成成功: {file_path}", 'success', repository_id=repository_id, file_path=file_path)
                    else:
                        # 缓存错误信息
                        error_msg = diff_data.get('error', '处理失败') if diff_data else '处理返回空结果'
                        self.cache_diff_error(repository_id, commit_id, file_path, error_msg)
                        log_print(f"❌ Excel差异处理失败: {file_path} | 错误: {error_msg}", 'EXCEL', force=True)
                        perf_metrics_service.record(
                            "background_excel_cache",
                            success=False,
                            metrics={
                                "total_ms": (time.time() - query_start) * 1000,
                                "query_ms": query_time * 1000,
                                "diff_ms": processing_time * 1000,
                            },
                            tags={
                                "source": "background_diff_failed",
                                "repository_id": repository_id,
                                "project_id": project_id,
                                "project_code": project_code,
                                "file_path": file_path,
                            },
                        )
                        
                        # 记录到操作日志
                        self.log_cache_operation(f"❌ 缓存生成失败: {file_path} - {error_msg}", 'error', repository_id=repository_id, file_path=file_path)
                        
                except Exception as inner_e:
                    error_type = type(inner_e).__name__
                    error_message = str(inner_e).replace('\n', ' ').strip()
                    if len(error_message) > 240:
                        error_message = f"{error_message[:237]}..."
                    stack_text = traceback.format_exc()
                    log_print(
                        f"处理Excel差异时出错[{error_type}]: {error_message} | "
                        f"repo={repository_id}, commit={commit_id}, file={file_path}\n{stack_text}",
                        'EXCEL',
                        force=True
                    )
                    perf_metrics_service.record(
                        "background_excel_cache",
                        success=False,
                        metrics={
                            "total_ms": (time.time() - query_start) * 1000,
                            "query_ms": (query_time * 1000) if "query_time" in locals() else 0.0,
                        },
                        tags={
                            "source": "background_inner_exception",
                            "repository_id": repository_id,
                            "project_id": project_id if "project_id" in locals() else "",
                            "project_code": project_code if "project_code" in locals() else "",
                            "file_path": file_path,
                            "error_type": error_type,
                            "error_message": error_message or "unknown_error",
                        },
                    )
                finally:
                    with self._processing_lock:
                        self._processing_commits.discard(task_key)

            # 走到这里说明上下文内的处理没有抛异常 → 真的算完并落了缓存。
            # 显式返回而不是靠「掉出函数末尾得到 None」——后者与失败无法区分。
            return BG_STATUS_COMPLETED

        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e).replace('\n', ' ').strip()
            if len(error_message) > 240:
                error_message = f"{error_message[:237]}..."
            log_print(
                f"后台处理Excel差异异常[{error_type}]: {error_message} | "
                f"repo={repository_id}, commit={commit_id}, file={file_path}\n{traceback.format_exc()}",
                'EXCEL',
                force=True
            )
            perf_metrics_service.record(
                "background_excel_cache",
                success=False,
                metrics={},
                tags={
                    "source": "background_outer_exception",
                    "repository_id": repository_id,
                    "project_id": project_id if "project_id" in locals() else "",
                    "project_code": project_code if "project_code" in locals() else "",
                    "file_path": file_path,
                    "error_type": error_type,
                    "error_message": error_message or "unknown_error",
                },
            )
            return BG_STATUS_ERROR

    def cleanup_expired_cache(self):
        """清理已过期的缓存记录（expire_at 已过期 + 状态为 outdated/failed 的记录）

        返回：清理条数（int）；**失败返回 None**（理由见 cleanup_old_cache 的说明）。
        """
        try:
            from sqlalchemy import or_
            now = datetime.now(timezone.utc)

            expired_count = DiffCache.query.filter(
                or_(
                    db.and_(DiffCache.expire_at.isnot(None), DiffCache.expire_at < now),   # 过期时间已到
                    DiffCache.cache_status == 'outdated',                                   # 标记为过期
                    DiffCache.cache_status == 'failed',                                     # 失败的记录
                )
            ).delete(synchronize_session=False)

            db.session.commit()
            log_print(f"🗑️ 清理了 {expired_count} 条过期/失败缓存", 'CACHE')
            return expired_count
        except Exception as e:
            log_print(f"❌ 清理过期缓存失败: {e}", 'CACHE', force=True)
            try:
                db.session.rollback()
            except Exception as rollback_error:
                log_print(f"清理过期缓存失败后回滚也失败: {rollback_error}", 'CACHE', force=True)
            return None

    def cleanup_old_cache(self, days=30):
        """清理超过指定天数的缓存数据和旧版本缓存（合并OR查询，#39）

        返回：清理条数（int）；**失败返回 None**。

        为什么失败不返回 0：调用方（services/task_worker_service.py 的 cleanup_cache
        分支、routes/cache_management_routes.py 的清理接口）原先把返回值直接当成
        「清理了 N 条」展示，于是「执行失败」与「本来就没东西可清」在管理界面上
        完全同形 —— 缓存表可能长期只增不减而无人察觉。返回 None 让调用方必须区分。
        """
        try:
            from sqlalchemy import or_
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            # 合并为一条 OR 查询，减少3次独立全表扫描为1次 (#39)
            total_count = DiffCache.query.filter(
                or_(
                    DiffCache.created_at < cutoff_date,                                       # 超期缓存
                    DiffCache.cache_status == 'outdated',                                     # 标记过期
                    db.and_(DiffCache.diff_version != DIFF_LOGIC_VERSION,
                            DiffCache.cache_status.in_((
                                CACHE_STATUS_COMPLETED, CACHE_STATUS_TRUNCATED))),        # 版本不匹配
                )
            ).delete(synchronize_session=False)
            
            db.session.commit()
            log_print(f"🗑️ 合并清理了 {total_count} 条过期/过旧/版本不匹配缓存")
            return total_count
        except Exception as e:
            log_print(f"清理缓存失败: {e}", 'CACHE', force=True)
            try:
                db.session.rollback()
            except Exception as rollback_error:
                # 原为裸 `except Exception: pass`，回滚失败完全没痕迹
                log_print(f"清理缓存失败后回滚也失败: {rollback_error}", 'CACHE', force=True)
            return None
    
    def cleanup_version_mismatch_cache(self):
        """专门清理版本号不匹配的缓存（批量DELETE）"""
        try:
            # 批量删除所有版本号不匹配的缓存
            count = DiffCache.query.filter(
                DiffCache.diff_version != DIFF_LOGIC_VERSION
            ).delete(synchronize_session=False)
            
            db.session.commit()
            log_print(f"清理了 {count} 条版本不匹配的缓存", 'CACHE')
            return count
        except Exception as e:
            log_print(f"清理版本不匹配缓存失败: {e}", 'CACHE', force=True)
            db.session.rollback()
            return 0
    
    def _cleanup_old_cache(self, repository_id=None):
        """清理超过1000条的旧缓存（不包括长处理文件）- 使用子查询批量DELETE

        返回：清理条数（int）；**失败返回 None**。调用方（cache_management_routes 的
        清理接口）据此区分「执行失败」与「本来就没东西可清」。
        """
        try:
            base_filter = [
                # 截断缓存同样占配额：它不参与「版本不匹配」清理，漏掉会让表只增不减
                DiffCache.cache_status.in_((CACHE_STATUS_COMPLETED, CACHE_STATUS_TRUNCATED)),
                DiffCache.is_long_processing == False  # 不清理长处理文件
            ]
            
            if repository_id:
                base_filter.append(DiffCache.repository_id == repository_id)
            
            total_count = DiffCache.query.filter(*base_filter).count()
            if total_count > self.max_cache_count:
                # 用子查询选出要保留的最新N条ID之外的记录
                excess_count = total_count - self.max_cache_count
                subquery = (
                    db.session.query(DiffCache.id)
                    .filter(*base_filter)
                    .order_by(DiffCache.created_at.asc())
                    .limit(excess_count)
                    .subquery()
                )
                
                deleted_count = DiffCache.query.filter(
                    DiffCache.id.in_(db.session.query(subquery))
                ).delete(synchronize_session=False)
                
                db.session.commit()
                log_print(f"🗑️ 清理了 {deleted_count} 条超限缓存", 'CACHE')
                return deleted_count
            else:
                log_print(f"📊 当前缓存数量 {total_count}，无需清理", 'CACHE')
                return 0
                
        except Exception as e:
            log_print(f"❌ 清理旧缓存失败: {e}", 'CACHE', force=True)
            try:
                db.session.rollback()
            except Exception as rollback_error:
                log_print(f"清理旧缓存失败后回滚也失败: {rollback_error}", 'CACHE', force=True)
            return None
    
    def get_cache_statistics(self, repository_id=None):
        """获取缓存统计信息"""
        try:
            query = DiffCache.query
            if repository_id:
                query = query.filter(DiffCache.repository_id == repository_id)
            
            total_count = query.count()
            completed_count = query.filter(DiffCache.cache_status == 'completed').count()
            processing_count = query.filter(DiffCache.cache_status == 'processing').count()
            failed_count = query.filter(DiffCache.cache_status == 'failed').count()
            outdated_count = query.filter(DiffCache.cache_status == 'outdated').count()
            # 截断（文件过大只存摘要）必须与 completed 分开统计：
            # 否则管理界面上「已完成」包含了「根本没比完」的缓存。
            truncated_count = query.filter(DiffCache.cache_status == CACHE_STATUS_TRUNCATED).count()

            # 获取当前版本的缓存数量
            current_version_count = query.filter(
                DiffCache.diff_version == DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).count()

            # 获取长处理文件数量
            long_processing_count = query.filter(
                DiffCache.is_long_processing == True,
                DiffCache.cache_status == 'completed'
            ).count()

            # 计算普通处理文件数量
            normal_processing_count = completed_count - long_processing_count

            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'outdated_count': outdated_count,
                'truncated_count': truncated_count,
                'current_version_count': current_version_count,
                'long_processing_count': long_processing_count,
                'normal_processing_count': normal_processing_count,
                'version': DIFF_LOGIC_VERSION
            }
        except Exception as e:
            log_print(f"❌ 获取缓存统计失败: {e}", 'CACHE', force=True)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'outdated_count': 0,
                'truncated_count': 0,
                'current_version_count': 0,
                'long_processing_count': 0,
                'normal_processing_count': 0,
                'version': DIFF_LOGIC_VERSION
            }
    
    def get_cache_statistics_by_repositories(self, repository_ids):
        """获取指定仓库列表的缓存统计信息"""
        try:
            log_print(f"🔍 获取仓库缓存统计: repository_ids={repository_ids}", 'CACHE')
            
            if not repository_ids:
                log_print("⚠️ 仓库ID列表为空，返回零值统计", 'CACHE')
                return {
                    'total_count': 0,
                    'completed_count': 0,
                    'processing_count': 0,
                    'failed_count': 0,
                    'outdated_count': 0,
                    'truncated_count': 0,
                    'current_version_count': 0,
                    'long_processing_count': 0,
                    'normal_processing_count': 0,
                    'version': DIFF_LOGIC_VERSION
                }
            
            query = DiffCache.query.filter(DiffCache.repository_id.in_(repository_ids))
            
            total_count = query.count()
            completed_count = query.filter(DiffCache.cache_status == 'completed').count()
            processing_count = query.filter(DiffCache.cache_status == 'processing').count()
            failed_count = query.filter(DiffCache.cache_status == 'failed').count()
            outdated_count = query.filter(DiffCache.cache_status == 'outdated').count()
            # 截断（文件过大只存摘要）与 completed 分开统计，避免管理界面把
            # 「根本没比完」算进「已完成」。
            truncated_count = query.filter(DiffCache.cache_status == CACHE_STATUS_TRUNCATED).count()

            # 获取当前版本的缓存数量
            current_version_count = query.filter(
                DiffCache.diff_version == DIFF_LOGIC_VERSION,
                DiffCache.cache_status == 'completed'
            ).count()

            # 获取长处理文件数量
            long_processing_count = query.filter(
                DiffCache.is_long_processing == True,
                DiffCache.cache_status == 'completed'
            ).count()
            
            # 详细调试信息
            log_print(f"📊 缓存统计详细结果:", 'CACHE')
            log_print(f"   - 总缓存数: {total_count}", 'CACHE')
            log_print(f"   - 已完成: {completed_count}", 'CACHE')
            log_print(f"   - 处理中: {processing_count}", 'CACHE')
            log_print(f"   - 失败: {failed_count}", 'CACHE')
            log_print(f"   - 过期: {outdated_count}", 'CACHE')
            log_print(f"   - 当前版本: {current_version_count}", 'CACHE')
            log_print(f"   - 长处理: {long_processing_count}", 'CACHE')
            
            # 查看最近的几条缓存记录
            recent_caches = query.order_by(DiffCache.created_at.desc()).limit(3).all()
            if recent_caches:
                log_print(f"📋 最近的缓存记录:", 'CACHE')
                for cache in recent_caches:
                    log_print(f"   - ID:{cache.id}, 仓库:{cache.repository_id}, 文件:{cache.file_path}, 状态:{cache.cache_status}, 版本:{cache.diff_version}", 'CACHE')
            else:
                log_print(f"❌ 没有找到任何缓存记录", 'CACHE')
            
            # 计算普通处理文件数量
            normal_processing_count = completed_count - long_processing_count
            
            return {
                'total_count': total_count,
                'completed_count': completed_count,
                'processing_count': processing_count,
                'failed_count': failed_count,
                'outdated_count': outdated_count,
                'truncated_count': truncated_count,
                'current_version_count': current_version_count,
                'long_processing_count': long_processing_count,
                'normal_processing_count': normal_processing_count,
                'version': DIFF_LOGIC_VERSION
            }
        except Exception as e:
            log_print(f"❌ 获取仓库缓存统计失败: {e}", 'CACHE', force=True)
            return {
                'total_count': 0,
                'completed_count': 0,
                'processing_count': 0,
                'failed_count': 0,
                'outdated_count': 0,
                'truncated_count': 0,
                'current_version_count': 0,
                'long_processing_count': 0,
                'normal_processing_count': 0,
                'version': DIFF_LOGIC_VERSION
            }


