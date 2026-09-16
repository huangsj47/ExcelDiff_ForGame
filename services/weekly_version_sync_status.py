#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本同步的「显式结局」，以及结局 → 后台任务状态的映射。

## 为什么需要这个模块

`process_weekly_version_sync()` 过去在**四条完全不同的路径**上都 `return None`：

    (a) 找不到 config          → 真的配错了 / 配置被删了
    (b) config 被禁用          → 运维有意停用
    (c) 窗口内没有任何提交      → **完全正常的常态**（这周本来就没改动）
    (d) 正常跑完

调用方拿不到任何区分信息，只能无条件把 BackgroundTask 标成 `completed`。结果：
「真的失败」和「这周本来就没数据」在任务列表里长得一模一样，运维无法区分，
既会把正常空窗口当成故障去排查，也会把真正的失败当成「完成」而放过。

本模块把这四种结局变成显式常量 + 一个 `WeeklySyncOutcome` 值对象；调用方按
`outcome.task_status` 落库、按 `outcome.describe()` 记日志与 `error_message`，
于是「无数据」= skipped（非失败、不告警），「配置缺失/禁用」= failed（带中文原因）。

## 为什么不写在 weekly_version_logic.py 里

该文件受 `tests/test_todo_split_followup_round2.py` 的 1800 行预算约束，且
结局判定是纯函数、与 Flask/ORM 无关，单独成模块更容易测。

## 命名对齐

常量风格与 `services/excel_diff_cache_service.py` 的 `BG_STATUS_*` 一致：
常量值是纯字符串，调用方据此决定落库状态与日志措辞。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 一、process_weekly_version_sync 的返回值（显式结局）
# ---------------------------------------------------------------------------
WEEKLY_SYNC_COMPLETED = 'completed'                    # 全部目标文件都成功
WEEKLY_SYNC_SKIPPED_NO_COMMITS = 'skipped_no_commits'  # 窗口内无提交：正常跳过，**不是失败**
WEEKLY_SYNC_FAILED_CONFIG_MISSING = 'failed_no_config'  # 配置不存在  → 调用方应标 failed
WEEKLY_SYNC_FAILED_CONFIG_INACTIVE = 'failed_inactive'  # 配置被禁用  → 调用方应标 failed
WEEKLY_SYNC_PARTIAL_FAILED = 'partial_failed'          # 有文件失败  → 部分失败，绝不是「完成」

# ---------------------------------------------------------------------------
# 二、写入 BackgroundTask.status 的值
#
# models/task.py 的 status 列是 String(20)，下面每个常量都 <= 20 字符，
# 换成 MySQL 也不会被截断（SQLite 不校验长度，问题会拖到切库那天才炸）。
# ---------------------------------------------------------------------------
TASK_STATUS_COMPLETED = 'completed'
TASK_STATUS_SKIPPED = 'skipped'                # 有明确结论的「跳过」（无数据），不告警
TASK_STATUS_FAILED = 'failed'
TASK_STATUS_PARTIAL_FAILED = 'partial_failed'  # 有文件成功、有文件失败

# 已到达终态、不会再推进的状态。worker 只捞 status='pending'，
# 页面阻塞判定只认 pending/processing，两者都不受影响。
TERMINAL_TASK_STATUSES = (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_SKIPPED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL_FAILED,
)

# 需要把原因写进 error_message 的状态（completed 没有原因可写）。
STATUSES_WITH_REASON = (
    TASK_STATUS_FAILED,
    TASK_STATUS_SKIPPED,
    TASK_STATUS_PARTIAL_FAILED,
)

# 需要运维关注、要计入失败告警的状态。
FAILURE_TASK_STATUSES = (TASK_STATUS_FAILED, TASK_STATUS_PARTIAL_FAILED)

_OUTCOME_TO_TASK_STATUS = {
    WEEKLY_SYNC_COMPLETED: TASK_STATUS_COMPLETED,
    WEEKLY_SYNC_SKIPPED_NO_COMMITS: TASK_STATUS_SKIPPED,
    WEEKLY_SYNC_FAILED_CONFIG_MISSING: TASK_STATUS_FAILED,
    WEEKLY_SYNC_FAILED_CONFIG_INACTIVE: TASK_STATUS_FAILED,
    WEEKLY_SYNC_PARTIAL_FAILED: TASK_STATUS_PARTIAL_FAILED,
}

FAILED_DETAIL_LIMIT = 5  # 消息/日志里最多列举前几个失败文件


class WeeklySyncOutcome:
    """一次周版本同步的结局。

    - `status`：WEEKLY_SYNC_* 之一
    - `message`：给人和界面看的中文原因（会写进 BackgroundTask.error_message）
    - `total_files`：本次同步的目标文件数
    - `failed_files`：失败文件数（**完整**计数，不受明细条数上限影响）
    - `failed_details`：((file_path, reason), ...) 明细，最多保留 FAILED_DETAIL_LIMIT 条

    刻意用普通类而不是 dict：调用方写错字段名会立刻 AttributeError，
    而不是静默拿到 None 后把失败又标成 completed。
    """

    __slots__ = ('status', 'message', 'total_files', 'failed_files', 'failed_details')

    def __init__(self, status, message, *, total_files=0, failed_files=0, failed_details=()):
        self.status = status
        self.message = message
        self.total_files = int(total_files or 0)
        self.failed_files = int(failed_files or 0)
        self.failed_details = tuple(failed_details or ())

    @property
    def task_status(self):
        """应写入 BackgroundTask.status 的值。未知结局一律按 failed（不放过失败）。"""
        return _OUTCOME_TO_TASK_STATUS.get(self.status, TASK_STATUS_FAILED)

    @property
    def is_failure(self):
        """True 表示需要运维关注。与「窗口内无数据」严格区分。"""
        return self.task_status in FAILURE_TASK_STATUSES

    @property
    def is_skipped(self):
        """True 表示正常跳过（无数据），不应产生失败告警。"""
        return self.task_status == TASK_STATUS_SKIPPED

    def describe(self):
        """写进日志 / error_message 的一行中文描述。"""
        return self.message

    def __repr__(self):
        return (f'<WeeklySyncOutcome {self.status} '
                f'files={self.total_files} failed={self.failed_files}>')


def failed_files_summary(failed_details, limit=FAILED_DETAIL_LIMIT):
    """把 ((path, reason), ...) 压成「a.lua（错误原因）、b.lua（错误原因）」。

    只列前 `limit` 条：一个周版本可能有上千个文件，全列会把 error_message
    撑成几十 KB，界面上也没人看。完整数量由 `outcome.failed_files` 承载。
    """
    parts = []
    for item in list(failed_details)[:limit]:
        try:
            path, reason = item
        except (TypeError, ValueError):
            path, reason = item, ''
        parts.append(f'{path}（{reason}）')
    return '、'.join(parts)


def build_partial_failure_outcome(total_files, failed_details):
    """构造「部分失败」结局。

    措辞上刻意**不含「完成」二字**：循环结束后无论失败多少都打「周版本同步完成」
    正是本次要修的问题之一 —— 日志说完成、任务标 completed，而事实是有一部分
    文件压根没有缓存。
    """
    failed_files = len(failed_details)
    message = (
        f'周版本同步部分失败：共 {total_files} 个文件，失败 {failed_files} 个'
        f'（成功 {int(total_files or 0) - failed_files} 个）'
    )
    summary = failed_files_summary(failed_details)
    if summary:
        message = f'{message}；失败明细（最多列 {FAILED_DETAIL_LIMIT} 个）：{summary}'
    return WeeklySyncOutcome(
        WEEKLY_SYNC_PARTIAL_FAILED,
        message,
        total_files=total_files,
        failed_files=failed_files,
        failed_details=failed_details,
    )


def task_status_and_message(outcome):
    """(BackgroundTask.status, 说明文本)。

    兼容旧调用方/测试桩返回 None 的情况：None 携带不了任何信息，只能按 completed
    处理，但会给出显式说明，避免「None 又被当成完成」被误读为「一切正常」。
    """
    if outcome is None:
        return TASK_STATUS_COMPLETED, '同步结束（调用方未返回状态，按完成处理）'
    if isinstance(outcome, WeeklySyncOutcome):
        return outcome.task_status, outcome.describe()
    # 兼容历史返回字符串的实现，映射不到就按 failed，绝不静默成功。
    return _OUTCOME_TO_TASK_STATUS.get(str(outcome), TASK_STATUS_FAILED), str(outcome)


# ---------------------------------------------------------------------------
# 四、结局构造函数
#
# 放在这里而不是 services/weekly_version_logic.py：那个文件受 1800 行预算约束，
# 而这些只是拼中文原因串，与业务逻辑无关。
# ---------------------------------------------------------------------------
def config_missing_outcome(config_id):
    """配置不存在（被删了/配错了）→ 调用方应标 failed，并保留中文原因。"""
    return WeeklySyncOutcome(
        WEEKLY_SYNC_FAILED_CONFIG_MISSING,
        f'周版本配置不存在（config_id={config_id}），无法执行同步',
    )


def config_inactive_outcome(config):
    """配置被禁用 → 仍要明确原因，否则用户只会看到「暂无数据」。"""
    return WeeklySyncOutcome(
        WEEKLY_SYNC_FAILED_CONFIG_INACTIVE,
        f'周版本配置已禁用（{getattr(config, "name", "")}），本次同步不执行',
    )


def no_commits_outcome(config):
    """窗口内没有任何提交 —— 常态，不是失败。调用方标 skipped，不产生失败告警。"""
    return WeeklySyncOutcome(
        WEEKLY_SYNC_SKIPPED_NO_COMMITS,
        f'时间窗口内无提交数据（窗口 {getattr(config, "start_time", None)} ~ '
        f'{getattr(config, "end_time", None)}），本次同步跳过（正常情况，不是失败）',
    )


def completed_outcome(config, total_files):
    """全部目标文件都成功。"""
    return WeeklySyncOutcome(
        WEEKLY_SYNC_COMPLETED,
        f'周版本同步完成: {getattr(config, "name", "")}，处理了 {total_files} 个文件',
        total_files=total_files,
    )


def latest_weekly_sync_tasks(background_task_model, configs):
    """{str(config_id): BackgroundTask} —— 每个配置最近一次 weekly_sync 任务。

    按 id 升序遍历后覆盖，最终留下 id 最大的那条。供配置页显示
    「为什么这个周版本没有数据」（任务上的 error_message / status）。
    查不到（或整表查询失败）时返回空 dict：模板对缺失是容错的，不该因为一块
    提示信息把整个配置页拖成 500。
    """
    config_ids = [str(getattr(config, 'id', '') or '') for config in (configs or [])]
    config_ids = [cid for cid in config_ids if cid]
    if not config_ids:
        return {}
    try:
        rows = background_task_model.query.filter(
            background_task_model.task_type == 'weekly_sync',
            background_task_model.commit_id.in_(config_ids),
        ).order_by(background_task_model.id.asc()).all()
    except Exception:
        return {}
    latest = {}
    for row in rows:
        latest[str(getattr(row, 'commit_id', '') or '')] = row
    return latest


# ---------------------------------------------------------------------------
# 三、初始缓存遮罩：全部目标文件都成功才放行
# ---------------------------------------------------------------------------
def collect_target_file_paths(commit_model, repository_id, window_start_utc, window_end_utc, limit=5000):
    """窗口内有提交的文件路径集合 —— 就是 `process_weekly_version_sync` 会处理的目标文件。

    这是「目标文件」唯一可信的来源：缓存行是同步过程中逐个创建的，用**已有的
    缓存行**推断目标集合，会把「压根没来得及创建缓存行的文件」漏掉。
    """
    if window_start_utc is None or window_end_utc is None:
        return set()
    rows = (
        commit_model.query
        .filter(
            commit_model.repository_id == repository_id,
            commit_model.commit_time >= window_start_utc,
            commit_model.commit_time <= window_end_utc,
        )
        .with_entities(commit_model.path)
        .distinct()
        .limit(limit)
        .all()
    )
    paths = set()
    for row in rows:
        # `with_entities` 返回的是 Row 而不是裸标量：SQLAlchemy 2.x 的 Row 不是 tuple
        # 子类，直接 str(row) 会得到 "('config/a.lua',)" 这种带括号的脏值。
        if isinstance(row, str):
            value = row
        else:
            try:
                value = row[0]
            except (TypeError, KeyError, IndexError):
                value = row
        text = str(value or '').strip()
        if text:
            paths.add(text)
    return paths


def initial_cache_readiness(target_paths, caches):
    """判断「初始缓存是否就绪」，返回 (ready, detail)。

    ## 为什么不能用 any()

    旧实现是 `any(cache.last_sync_time is not None for cache in diff_caches)`：
    只要**任意一个**文件成功就认为就绪。首轮同步是逐个文件跑完才写缓存的，
    所以同步跑到第 1 个文件时页面就解锁了 —— 用户看到的是「大部分文件还没缓存」
    的半成品列表，而且没有任何提示说明它不完整，很容易被当成完整结果确认掉。

    ## 现在的判定依据（写进日志的 detail 就是它）

    目标集合 = 窗口内有提交的文件 ∪ 已有缓存行的文件；
    就绪 = 目标集合非空 **且** 其中每个文件都有 `cache_status == 'completed'`
    且 `last_sync_time` 非空（零失败）。

    目标集合为空（窗口内无提交、也没有任何缓存行）时判为**未就绪**：此时列表本来
    就是空的，交给上层去触发首轮同步，而不是「空就等于就绪」。

    ## 兜底：为什么允许传空 target_paths

    调用方可能枚举不出目标文件（例如仓库刚建、Commit 还没入库）。那种情况下退化为
    「已有缓存行全部完成」，比旧实现的 any() 严格，但比「永久阻塞页面」安全。
    """
    targets = set()
    for path in target_paths or ():
        text = str(path or '').strip()
        if text:
            targets.add(text)

    ready_paths = set()
    for cache in caches or ():
        path = str(getattr(cache, 'file_path', '') or '').strip()
        if not path:
            continue
        # 缓存行本身就是目标之一（它代表「这个文件被处理过」）。
        targets.add(path)
        # 兼容没有 cache_status 属性的历史/桩对象：缺省视为 completed（与列默认值一致）。
        cache_status = str(getattr(cache, 'cache_status', TASK_STATUS_COMPLETED) or TASK_STATUS_COMPLETED)
        if getattr(cache, 'last_sync_time', None) is not None and cache_status == TASK_STATUS_COMPLETED:
            ready_paths.add(path)

    if not targets:
        return False, '目标文件集合为空（窗口内无提交、也没有任何缓存行），按未就绪处理'

    pending = sorted(targets - ready_paths)
    detail = (
        f'目标文件 {len(targets)} 个，已完成 {len(ready_paths)} 个，未完成 {len(pending)} 个'
    )
    if pending:
        detail = f'{detail}（未完成示例：{"、".join(pending[:3])}）'
    return (not pending), detail


# 同一 config 只在「就绪结论发生翻转」时记一次日志：
# weekly_version_files_api 在首轮同步期间会被前端每隔几秒轮询一次。
_mask_log_memo = {}
_MASK_LOG_MEMO_LIMIT = 2000


def should_log_mask_decision(config_id, ready):
    """结论翻转（或首次）返回 True。避免轮询把日志刷成一片。"""
    if len(_mask_log_memo) > _MASK_LOG_MEMO_LIMIT:
        _mask_log_memo.clear()
    previous = _mask_log_memo.get(config_id)
    current = bool(ready)
    _mask_log_memo[config_id] = current
    return previous is None or previous != current
