"""任务行的创建、装载与队列维护（worker 的「生产端」）。

## 为什么单独一个文件

`task_worker_service.py` 里有两件事容易混：**任务怎么进队列**（本文件）和
**任务怎么被执行**（worker 主循环 + `task_worker_task_handlers.py`）。这里收的是前者：
建 `BackgroundTask` 行、按 `status='pending'` 去重、进程启动时把库里残留的任务装回
内存队列、以及配表缓存的重建与清理。搬出来之后，「为什么这次同步没排上队」这类问题
只需要看这一个文件。

搬出来的直接原因仍然是 `scripts/check_file_length.py --strict`：`task_worker_service.py`
贴着 2000 行的 ERROR 门槛。

## 与 `task_worker_service.py` 的耦合方式

见 `task_worker_task_handlers.py` 抬头：模块级槽位一律 `worker.X` 现取，不 `from ... import`。
本文件里尤其关键的是 `create_auto_sync_task` / `check_and_create_auto_sync_tasks` ——
它们既被 `services/repository_*_handlers.py` 用 `get_runtime_model()` 按**模块属性**
取用，又被测试 monkeypatch 掉，所以本文件内部互相调用时也走 `worker.`，
保证「补丁打在哪，调用就从哪走」。
"""

from __future__ import annotations

# ruff: noqa: I001 —— `task_worker_service` 必须先导入；两模块互相引用，字母序会在本模块
# 尚未初始化完成时让 worker 取这里的名字。测试文件也固定使用同一导入顺序。

import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import services.task_worker_service as worker
# 优先级常量的**单一来源**（见该模块 docstring）。本文件里一律从这里取，不写字面量。
from services.task_worker_priority import (
    AUTO_SYNC as _PRIORITY_AUTO_SYNC,
    AUTO_SYNC_FORCE_RETRY as _PRIORITY_AUTO_SYNC_FORCE_RETRY,
    CLEANUP_CACHE as _PRIORITY_CLEANUP_CACHE,
    DEFAULT_TASK_PRIORITY as _PRIORITY_DEFAULT,
    WAITING_INTENT as _PRIORITY_WAITING_INTENT,
    WEEKLY_AI_ANALYSIS as _PRIORITY_WEEKLY_AI_ANALYSIS,
    WEEKLY_AI_MANUAL as _PRIORITY_WEEKLY_AI_MANUAL,
    backdate_wrapper,
)
# 触发来源 / 请求模式的归一化，以及「已入队」账本（本文件是叶模块，不 import worker）。
from services.task_worker_weekly_handlers import (
    TRIGGER_SOURCE_MANUAL,
    TRIGGER_SOURCE_SCHEDULED,
    enqueue_weekly_ai_analysis_task as _enqueue_weekly_ai_analysis_task,
    is_weekly_ai_task_enqueued as _is_weekly_ai_task_enqueued,
    manual_wins as _manual_wins,
    normalize_requested_mode as _normalize_requested_mode,
    normalize_trigger_source as _normalize_trigger_source,
)
# 同步闸门：唤醒意图时**必须**走同一道判据（手工与后台那两道用的就是它）。
# 这里按模块取用（`sync_gate.X`）而不是 `from … import X`：测试要能按属性打补丁。
from services.ai import weekly_sync_gate as sync_gate


def _analysis_task_queue():
    """运行态隔离 AI 队列；未启动 worker 的维护脚本沿用原队列。"""
    if getattr(worker, "background_task_running", False):
        return getattr(worker, "ai_task_queue", worker.background_task_queue)
    return worker.background_task_queue

# ---------------------------------------------------------------------------
#  平台侧租约
#
#  `AgentTask.lease_expires_at` 管的是「哪个节点领走了这条下发任务」，是**另一套**机制。
#  这一套管的是「本进程的 worker 有没有在跑它」，专治平台侧原先那三处各管一摊：
#
#  * `started_at` 只证明「被取走过」，判不出执行者还在不在；
#  * 定时清理的阈值与判据两个任务类型各不相同（300s / 7200s，一个有账本一个没有）；
#  * 启动恢复一刀切 —— 把所有 `processing` 无条件收回 `pending`（见 `load_pending_tasks`），
#    多 worker（或上一个进程还没死透）时会把别人正拿在手里的任务抢走。
#
#  租约把这三处收敛成一个判据：`processing` 且租约已过期 = 执行者已经死了。
# ---------------------------------------------------------------------------

# 一次任务最长允许没人续租多久。取 3600 秒与既有的
# `WEDGED_SYNC_PROCESSING_SECONDS` 同量级：比最长的分析（实测 6~8 分钟）与大仓同步
# （340~420 秒）都宽得多，误判的代价是「一个还在跑的同步被另一个 worker 抢走重跑」。
#
# **为什么是「一次续租都不做」的兜底值，而不是「任务最长能跑多久」**：真的在跑的任务
# 由 `renew_inflight_task_leases` 在运行期间不断刷新租约（见下面那一节），所以合法运行
# **不会**撞上这个界；它只剩一个含义 —— 执行者（进程）死了之后多久回收。
TASK_LEASE_SECONDS = max(60, int(os.environ.get("TASK_LEASE_SECONDS", "3600") or 3600))

# 续租间隔：租约的 1/6。留这么多余量是为了「连续几次续租失败也不会让别人抢走」——
# 续租是一条按 id 的 UPDATE，间隔越小越安全，但没必要到秒级。
TASK_LEASE_RENEW_INTERVAL_SECONDS = max(30, TASK_LEASE_SECONDS // 6)

# 同一条任务被「租约到期」收回几次之后不再重试（改为 failed）。
# 没有它，一条每次都把 worker 弄死的任务会永远在 pending/processing 之间来回。
MAX_TASK_LEASE_RECLAIMS = 3

# `weekly_ai_analysis` 在 worker 认领任务后、创建 Run 前还要冻结快照与组装输入；这段
# 合法空窗里数据库会短暂呈现「task=processing、run=不存在」。只在空窗持续超过 5 分钟
# 后才用跨层状态判断孤儿，避免把正在准备大快照的另一个 worker 误判为死亡。
ORPHANED_AI_TASK_GRACE_SECONDS = max(
    60, int(os.environ.get("ORPHANED_AI_TASK_GRACE_SECONDS", "300") or 300)
)


def lease_deadline(now=None):
    """这次取走任务的租约到期时刻（naive-UTC，与库里的 `created_at` 同口径）。"""
    return _now_utc_naive(now) + timedelta(seconds=TASK_LEASE_SECONDS)


def stamp_task_lease(db_task, now=None):
    """任务被 worker 取走时起租（由 `update_task_status_with_retry` 在写 processing 时调用）。

    **已有的有效租约不会被覆盖 —— 起租只发生一次。** 同一个执行者取走任务之后还会再走
    一次这里（worker 主循环里的抢占写一次、handler 里的
    `update_task_status_with_retry(task_id, 'processing')` 又写一次）。如果第二次把租约
    换成另一个时刻，本进程续租名单里记着的那个值就对不上了 —— 续租用的是 CAS
    （`lease_expires_at == 我们写下的那个值`，见 `renew_inflight_task_leases`），
    值一换续租就全部落空，长任务反而会被自己的续租机制坑死。
    """
    if db_task is None:
        return None
    current = _now_utc_naive(now)
    existing = _naive_utc(getattr(db_task, "lease_expires_at", None))
    if existing is not None and existing > current:
        return existing
    db_task.lease_expires_at = lease_deadline(current)
    return db_task.lease_expires_at


def clear_task_lease(db_task):
    """跑完（不论成败）就退租。"""
    if db_task is None:
        return None
    db_task.lease_expires_at = None
    return None


def lease_is_expired(db_task, *, now=None, timeout_seconds=None):
    """这条任务的主人还在不在：**平台侧「谁还活着」只有这一条判据**。

    判据按证据强弱依次取，前面的能判就不看后面的：

    1. **租约**（`lease_expires_at`）：执行者取走任务时自己写的，运行期间还会被
       `renew_inflight_task_leases` 不断刷新 —— 它过期 = 执行者不再刷它了。
    2. **起跑时刻**（`started_at`）：无租约的老行走这条（`models/task.py` 里写明的口径：
       无租约按「无租约」处理，不让老行永远占着位子）。它只是「被取走过」的记号，
       所以只在 `timeout_seconds` 之内算数。
    3. **建行时刻**（`created_at`）：连 `started_at` 都没有的行再退一步。**这一步不能省**：
       `create_weekly_sync_task` 的去重就靠这条判据回答「正在跑的那条还有没有人管」
       （`_is_wedged_processing_sync_task`），少这一步会把一条没人标记起跑时刻的
       `processing` 立刻判死 —— 那是「每个 tick 都另起一个同步、两个任务抢着写同一份
       周版本缓存」的另一头。

    三者都没有 → 按「主人已经不在了」处理（没有任何活着的证据）。

    `timeout_seconds` 给不同类型的陈旧界留口子（默认 `TASK_LEASE_SECONDS`）。
    """
    if db_task is None:
        return True
    bound = TASK_LEASE_SECONDS if timeout_seconds is None else int(timeout_seconds)
    current = _now_utc_naive(now)
    expires = _naive_utc(getattr(db_task, "lease_expires_at", None))
    if expires is not None:
        return expires <= current
    started = _naive_utc(getattr(db_task, "started_at", None))
    if started is None:
        started = _naive_utc(getattr(db_task, "created_at", None))
    if started is None:
        return True
    return (current - started).total_seconds() >= bound


def _processing_ai_task_is_orphaned(db_task, *, now=None):
    """跨层判断一条仍在有效租约内的 AI 执行体是否已经失去执行者。

    平台重启恢复会先把旧进程留下的活动 Run 置为 failed。若后台任务的租约仍有几十分钟，
    单看租约会让这条执行体继续占位；新 Job 随后附着到它上面，只能永久停在 queued。

    活动 Run 是最强的反证：存在就不碰。没有活动 Run 时，保护期内也不碰（worker 可能仍在
    冻结快照）；超过保护期后，如果关联 Job 仍是 running，也视为执行者存活。其余组合都已
    违反 task/job/run 状态机，应当收回。导入放在函数内，避免 models 与 worker 的环依赖。
    """
    if (
        db_task is None
        or getattr(db_task, "task_type", None) != "weekly_ai_analysis"
        or getattr(db_task, "status", None) != "processing"
    ):
        return False
    current = _now_utc_naive(now)
    started = _naive_utc(getattr(db_task, "started_at", None))
    if started is None:
        started = _naive_utc(getattr(db_task, "created_at", None))
    if started is None or (current - started).total_seconds() < ORPHANED_AI_TASK_GRACE_SECONDS:
        return False

    from models.ai_analysis import AiAnalysisJob, AiAnalysisRun

    group_key = str(getattr(db_task, "file_path", "") or "")
    live_run = AiAnalysisRun.query.filter(
        AiAnalysisRun.target_type == "weekly",
        AiAnalysisRun.target_key == group_key,
        AiAnalysisRun.status.in_(("pending", "running")),
    ).first()
    if live_run is not None:
        return False

    job_id = getattr(db_task, "job_id", None)
    if job_id is not None:
        job = worker._db.session.get(AiAnalysisJob, job_id)
        if job is not None and str(getattr(job, "state", "") or "") == "running":
            return False
    return True


def reclaim_expired_task_leases(*, now=None, requeue=True):
    """定时扫描：把 `processing` 且租约到期的任务放回 `pending`（或按重试次数置 failed）。

    返回各项计数（用于日志与测试断言）。
    """
    outcome = {"checked": 0, "requeued": 0, "failed": 0}
    try:
        processing_tasks = worker._BackgroundTask.query.filter_by(status="processing").all()
    except worker.SQLAlchemyError as exc:
        worker.log_print(f"⚠️ 扫描过期租约失败: {exc}", "TASK")
        return outcome

    for row in processing_tasks:
        outcome["checked"] += 1
        if not lease_is_expired(row, now=now):
            continue
        attempts = int(getattr(row, "retry_count", 0) or 0) + 1
        row.retry_count = attempts
        row.completed_at = None
        clear_task_lease(row)
        if attempts >= MAX_TASK_LEASE_RECLAIMS:
            row.status = "failed"
            row.completed_at = datetime.now(timezone.utc)
            row.error_message = (
                f"租约连续 {attempts} 次到期（每次 {TASK_LEASE_SECONDS} 秒没人续），"
                "已放弃重试"
            )
            worker._db.session.commit()
            outcome["failed"] += 1
            worker.log_print(
                f"⛔ 任务 {row.id}（{row.task_type}）租约连续到期 {attempts} 次，已置 failed",
                "TASK",
                force=True,
            )
            continue
        row.status = "pending"
        row.started_at = None
        worker._db.session.commit()
        outcome["requeued"] += 1
        worker.log_print(
            f"♻️ 任务 {row.id}（{row.task_type}）租约到期，已放回待处理（第 {attempts} 次）",
            "TASK",
            force=True,
        )
        if requeue:
            _requeue_row_for_execution(row)
    return outcome


def _requeue_row_for_execution(db_task):
    """把一行已被放回 `pending` 的任务重新交给**执行侧**（单机=内存队列 / 平台=Agent 派发）。"""
    if worker._use_agent_dispatch():
        return worker._ensure_agent_dispatch_for_background_task(db_task)
    return _enqueue_pending_row(db_task)


def claim_task_row_for_execution(task_id, *, now=None):
    """worker 从内存队列取到一条任务之后，用**条件 UPDATE** 把库里的那一行占下来。

    返回 `True` = 这次由本 worker 执行；`False` = 别人正拿着它（或它已经结束了），
    这次跳过。

    ## 为什么必须是条件 UPDATE + rowcount

    「先 SELECT 看 status 是不是 pending，是就 UPDATE 成 processing」在**查与改之间有一条
    缝**：双 worker（第二波的部署方案）下两个进程会各自读到 pending、各自 UPDATE，
    于是同一条任务被跑两遍 —— 同一次同步往缓存里写两遍、同一份输入付两次费。
    把判断交给数据库（`WHERE status = 'pending'`），`rowcount == 1` 的那个才是抢到的。

    ## 为什么「没有这一行」返回 True

    载荷里的 `task_id` 可能压根不在库里（畸形载荷、Agent 回传、测试桩）。那种情况必须照旧
    交给处理器 —— 处理器会把「没法弄」写成 failed，而在这里拦掉会让那一行**永远没人碰**，
    那正是这一轮要修的另一个病（永久 pending）。

    ## 为什么 `processing` 一律不抢（哪怕租约已经过期）

    这里是**互斥**判据：`WHERE status = 'pending'` + rowcount 是「只有一个能赢」的全部依据。
    把 `processing` 也放进 WHERE 里，第二个 worker 的条件更新照样会命中（第一个刚把它
    改成了 processing）—— 互斥当场失效，而且是**静默**失效（两边都以为自己抢到了）。
    租约过期那种行交给定时回收（`reclaim_expired_task_leases` 先放回 pending 再重投），
    职责分开：一个负责抢，一个负责收。

    ## 为什么这里自己 push app context

    调用点是 `background_task_worker` 的主循环 —— **裸线程、没有 ambient app context**
    （bootstrap 里那个 `with app.app_context()` 只包住了「启动线程」那一瞬间）。
    不自己 push，第一次读库就抛 `RuntimeError: Working outside of application context.`，
    而它不在任何 worker 循环的 except 里：**任务已经被从内存队列取走（弹掉了）、却一行
    都没执行**，症状是「任务莫名消失、库里还是 pending」。与
    `wake_waiting_analysis_intents_safely` 踩过的是同一个坑。
    """
    if task_id is None:
        return True
    app = getattr(worker, "_app", None)
    if app is None:
        # 还没注入 app（测试桩/单元用法）：没有库可读，照旧交给处理器。
        return True
    try:
        with app.app_context():
            return _claim_task_row_locked(task_id, now=now)
    except (worker.SQLAlchemyError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
        # 抢不动不是「执行失败」：照旧交给处理器（它自己会写终态），
        # 在这里拦掉会让这一行没人管。
        worker.log_print(f"⚠️ 任务 {task_id} 抢占时报错（照旧执行）: {exc}", 'TASK', force=True)
        return True


def _claim_task_row_locked(task_id, *, now=None):
    """条件 UPDATE 的实际动作（调用方负责 app context 与异常兜底）。"""
    row = worker._db.session.get(worker._BackgroundTask, task_id)
    if row is None:
        return True
    if getattr(row, 'status', None) == 'processing':
        return False
    deadline = lease_deadline(now)
    claimed = (
        worker._db.session.query(worker._BackgroundTask)
        .filter(
            worker._BackgroundTask.id == task_id,
            worker._BackgroundTask.status == 'pending',
        )
        .update(
            {
                'status': 'processing',
                'started_at': datetime.now(timezone.utc),
                'lease_expires_at': deadline,
            },
            synchronize_session=False,
        )
    )
    worker._db.session.commit()
    if claimed == 1:
        # 抢到了 = 本进程开始执行它，进续租名单（长任务在跑的时候靠值班线程延长租约，
        # 见 `renew_inflight_task_leases`）。这里登记的必须是**刚写进库里的那个值**，
        # 不是重新算一个：续租要靠它做 CAS 认出「这行还是我的」，差一微秒都认不出来。
        register_inflight_task(task_id, deadline)
        return True
    worker.log_print(
        f"⚠️ 任务 {task_id} 抢占失败（有条件更新没命中，rowcount={claimed}）",
        'TASK',
        force=True,
    )
    return False


def create_auto_sync_task(repository_id, extra_payload=None):
    """为仓库创建自动数据分析任务"""
    try:
        payload = dict(extra_payload or {})
        force_retry = bool(payload.get("force_reclone") or payload.get("force_repair_update"))
        existing_task = worker._BackgroundTask.query.filter_by(
            repository_id=repository_id,
            task_type='auto_sync',
            status='pending'
        ).first()
        if existing_task and not force_retry:
            worker.log_print(f"仓库 {repository_id} 已存在待处理的自动同步任务", 'SYNC')
            return existing_task.id
        if existing_task and force_retry:
            worker.log_print(
                f"仓库 {repository_id} 存在待处理 auto_sync，手动重试将创建新任务并附加重试策略",
                'SYNC',
            )

        new_task = worker._BackgroundTask(
            task_type='auto_sync',
            repository_id=repository_id,
            priority=_PRIORITY_AUTO_SYNC_FORCE_RETRY if force_retry else _PRIORITY_AUTO_SYNC,
            status='pending'
        )
        worker._db.session.add(new_task)
        worker._db.session.flush()
        enqueue_payload = {"repository_id": repository_id}
        enqueue_payload.update(payload)
        worker._enqueue_agent_task_from_background_task(
            new_task,
            extra_payload=enqueue_payload,
        )
        worker._db.session.commit()
        if not worker._use_agent_dispatch():
            task_data = {
                'type': 'auto_sync',
                'repository_id': repository_id,
                'task_id': new_task.id
            }
            task_data.update(payload)
            task_counter = int(time.time() * 1000000)
            tw = worker.TaskWrapper(_PRIORITY_AUTO_SYNC, task_counter, task_data)
            worker.background_task_queue.put(tw)
        worker.log_print(f"✅ 为仓库 {repository_id} 创建自动数据分析任务 (ID: {new_task.id})", 'SYNC')
        return new_task.id
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"❌ 创建自动同步任务数据库失败: {e}", 'SYNC', force=True)
        return None
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker.log_print(f"❌ 创建自动同步任务失败: {e}", 'SYNC', force=True)
        return None


def dispatch_auto_sync_task_when_agent_mode(repository_id, extra_payload=None):
    """Agent 模式下派发 auto_sync；single 模式直接返回未处理。"""
    if not worker._use_agent_dispatch():
        return False, None
    task_id = worker.create_auto_sync_task(repository_id, extra_payload=extra_payload)
    return True, task_id


def check_and_create_auto_sync_tasks():
    """检查已克隆但未分析的仓库，自动创建数据分析任务"""
    try:
        repositories = worker._Repository.query.filter_by(clone_status='completed').all()
        created_tasks = 0
        for repo in repositories:
            commit_count = worker._Commit.query.filter_by(repository_id=repo.id).count()
            if commit_count == 0:
                worker.log_print(f"🔍 发现已克隆但未分析的仓库: {repo.name} (ID: {repo.id})", 'SYNC')
                task_id = worker.create_auto_sync_task(repo.id)
                if task_id:
                    created_tasks += 1
        if created_tasks > 0:
            worker.log_print(f"✅ 为 {created_tasks} 个仓库创建了自动数据分析任务", 'SYNC')
        else:
            worker.log_print("ℹ️ 没有发现需要自动分析的仓库", 'SYNC')
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"❌ 检查自动同步任务数据库失败: {e}", 'SYNC', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker.log_print(f"❌ 检查自动同步任务失败: {e}", 'SYNC', force=True)


def reclaim_expired_task_leases_safely():
    """定时回收过期租约。**绝不把调度线程打死**（它跑在 `schedule.run_pending()` 里）。

    `run_scheduled_tasks` 只接住三种异常，而 `reclaim_expired_task_leases` 只接住了
    `SQLAlchemyError`；把别的漏出去等于让 `scheduler_thread` 停在一行日志上（此后每个
    tick 都不再执行），而症状只是「定时任务不再跑了」—— 与「没到时间」长得一模一样。

    放在本模块（而不是 `task_worker_service`）：那里贴着一行长度闸门的 WARN，
    而这层薄包装与租约实现是同一件事。
    """
    try:
        return reclaim_expired_task_leases()
    except (worker.SQLAlchemyError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
        worker.log_print(f"⚠️ 回收过期任务租约失败: {exc}", 'TASK', force=True)
        return None


# ---------------------------------------------------------------------------
#  租约续期
#
#  没有续期时，租约的时长就等价于「任务最长能跑多久」—— 一次合法的长跑会在中途被判死、
#  放回 pending、被重新执行：同步是白跑一遍，**AI 分析是同一份输入付两次费**
#  （静默的重复开销，比报错难发现得多）。所以「租约绝不能在一次合法运行期间过期」
#  不能靠调大阈值来做（那只是把洞挪到更大的时长上），必须真的续。
#
#  **为什么是独立线程，而不是在 worker 主循环里顺手续一次**：主循环在跑任务的那段时间
#  正阻塞在任务里（一次 AI 分析 6~8 分钟、大仓同步 340~420 秒），根本轮不到下一轮循环 ——
#  把续租塞进阻塞调用之间等于没续。放在自己的线程里，"执行者还活着"这个事实才有人维护。
#
#  **为什么不用 AI 引擎的执行进度回调来续**（`services/ai/run_progress.publish` 那条路，
#  它是最小的非阻塞挂钩点）：那个文件属于别的分片（本分片只报告不改），而且它只覆盖
#  `weekly_ai_analysis` 一种类型 —— 同步、缓存、excel_diff 仍然没有续租。一条判据要盖
#  所有类型，就只能挂在类型无关的地方（worker 的执行入口），也就是这里。
#
#  **另有一条备选是「按类型给不同的租约时长」**（AI 用 7200 秒配合它自己的陈旧界）：
#  它改动更小、也不用起线程，但**不满足「合法运行期间不过期」**—— 它只是把门槛从 1 小时
#  抬到 2 小时，跑过 2 小时的分析照样被回收重跑（同一个重复付费的洞，换个大一点的阈值），
#  而且首次拉一个大仓库的 `auto_sync` 也在这条线上。阈值型方案还有第二个毛病：它让
#  「能跑多久」变成一句没人维护的隐含约定，将来任务变慢时不会有人想起来改它。
#
#  **进程死 vs 线程卡住**：进程死 → 没人续租 → 到点回收（这是想要的）。进程还活着但
#  执行线程真的卡死（死锁）→ 会一直被续租，这时兜底的是各类型自己的陈旧闸门
#  （`reset_stale_weekly_analysis_tasks` 的 7200 秒、页面那条 `is_stale_sync_task` 的
#  1800 秒）—— 两层判据各管一段，任何一层都不假设另一层存在。
# ---------------------------------------------------------------------------

# 本进程正在执行的任务：task_id → 我们写进库里的那个租约时刻。
# 值必须是**实际写在行上的**那一个，续租才能用 CAS 认出来「这行还是我的」
# （见 `renew_inflight_task_leases`），所以它由抢占成功的那一段登记，不在这里算。
_inflight_lease_lock = threading.Lock()
_inflight_leases: dict = {}
_lease_renewer_thread = None
_lease_renewer_stop = threading.Event()


def register_inflight_task(task_id, lease_value):
    """把「本进程正在执行它、库里的租约是这个时刻」记进续租名单。"""
    if task_id is None or lease_value is None:
        return None
    with _inflight_lease_lock:
        _inflight_leases[int(task_id)] = lease_value
    return lease_value


def forget_inflight_task(task_id):
    """任务跑完（不论成败）就撤出续租名单。**必须在 finally 里**：漏一个就是一条永远
    在被续租的行，它的执行者早就走了，别人也不会来收。"""
    if task_id is None:
        return None
    try:
        key = int(task_id)
    except (TypeError, ValueError):
        return None
    with _inflight_lease_lock:
        _inflight_leases.pop(key, None)
    return None


def inflight_task_ids():
    """当前登记在续租名单里的任务 id（排查用：日志/测试）。"""
    with _inflight_lease_lock:
        return sorted(_inflight_leases)


def renew_inflight_task_leases(*, now=None):
    """给本进程正在跑的任务续租。返回 `{"renewed": n, "lost": m}`。

    **CAS 续租**：条件 UPDATE 同时要求 `id` 对、`status='processing'`、
    `lease_expires_at` 仍是我们写下的那个值（rowcount 才是判据）。
    只有我们还握着它时才延长；命中 0 行说明这行已经换了主人或者已经结束
    （别人回收后重投、被置 failed、被重置），此时**把它撤出名单**，绝不盲目续 ——
    盲目续等于替别人养着一条租约，那条真死掉的任务就永远收不回来了。

    **续租失败一律安全退化**：不重试、不改状态、不向上抛，让租约自然到期，
    到期后由 `reclaim_expired_task_leases` 按正常流程回收。这里唯一要守住的底线是
    「不要因为续不上就把任务判死」—— 续不上是**存储/上下文**的问题，不是任务的问题。
    """
    outcome = {"renewed": 0, "lost": 0}
    with _inflight_lease_lock:
        pending_items = list(_inflight_leases.items())
    if not pending_items:
        return outcome
    app = getattr(worker, "_app", None)
    if app is None:
        # 没有 app（纯函数式调用/测试）：没有库可写，直接退化成「让它自然过期」。
        return outcome
    try:
        with app.app_context():
            new_deadline = lease_deadline(now)
            for task_id, expected in pending_items:
                updated = (
                    worker._db.session.query(worker._BackgroundTask)
                    .filter(
                        worker._BackgroundTask.id == task_id,
                        worker._BackgroundTask.status == "processing",
                        worker._BackgroundTask.lease_expires_at == expected,
                    )
                    .update({"lease_expires_at": new_deadline}, synchronize_session=False)
                )
                worker._db.session.commit()
                with _inflight_lease_lock:
                    if _inflight_leases.get(task_id) != expected:
                        # 期间被注销/被换过，别人已经在管了，别覆盖别人的账。
                        continue
                    if updated == 1:
                        _inflight_leases[task_id] = new_deadline
                    else:
                        _inflight_leases.pop(task_id, None)
                if updated == 1:
                    outcome["renewed"] += 1
                else:
                    outcome["lost"] += 1
                    worker.log_print(
                        f"⏹️ 任务 {task_id} 已不再归本进程（换主人/已结束），停止为它续租",
                        'TASK',
                    )
    except (worker.SQLAlchemyError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
        # 一次续租失败不影响别的任务，也不影响本进程继续跑：租约会自然到期。
        worker.log_print(f"⚠️ 续租失败（交给租约自然到期回收）: {exc}", 'TASK', force=True)
    return outcome


def _lease_renewer_loop():
    """续租线程主体：每 `TASK_LEASE_RENEW_INTERVAL_SECONDS` 续一轮。

    循环体自己吞掉可预期的异常（与 worker 主循环同一组），否则这线程一死就是
    「所有长任务都会在租约到期时被判死」—— 而那症状与「任务真的变慢了」长得一样。
    """
    worker.log_print("任务租约续期线程启动", 'APP')
    while not _lease_renewer_stop.wait(TASK_LEASE_RENEW_INTERVAL_SECONDS):
        try:
            renew_inflight_task_leases()
        except worker.NON_CRITICAL_WORKER_LOOP_ERRORS as exc:
            worker.log_print(f"⚠️ 续租线程出错（忽略，租约会自然到期）: {exc}", 'TASK', force=True)
    worker.log_print("任务租约续期线程停止", 'APP')


def start_lease_renewer():
    """起续租线程（幂等）。由 `start_background_task_worker` 调用。"""
    global _lease_renewer_thread
    if _lease_renewer_thread is not None and _lease_renewer_thread.is_alive():
        return _lease_renewer_thread
    _lease_renewer_stop.clear()
    _lease_renewer_thread = threading.Thread(
        target=_lease_renewer_loop, name="task-lease-renewer", daemon=True
    )
    _lease_renewer_thread.start()
    return _lease_renewer_thread


def stop_lease_renewer():
    """停续租线程并清空名单（进程要停了，没人在跑的任务不该被继续续租）。"""
    global _lease_renewer_thread
    _lease_renewer_stop.set()
    thread = _lease_renewer_thread
    if thread is not None and thread.is_alive():
        try:
            thread.join(timeout=3)
        except RuntimeError as exc:
            worker.log_print(f"停止租约续期线程时出现错误: {exc}", 'APP', force=True)
    _lease_renewer_thread = None
    with _inflight_lease_lock:
        _inflight_leases.clear()


def _enqueue_pending_row(db_task, *, waiting_groups=frozenset(), now=None):
    """把一行 `pending` 任务按类型建成载荷放进内存队列；返回是否真的入了队。

    抽出来是因为它现在有**两个**调用方：启动恢复（`load_pending_tasks`）与租约到期后的
    重投（`_requeue_row_for_execution`）。两份实现必然会分叉，而分叉的形态是安静的：
    一边记得带 `config_id`、另一边忘了 → 处理器 KeyError → 被 worker 循环吞掉 →
    库里的行永久 pending（这一条链子踩过一次，别再来第二次）。

    「等待同步」的意图行不是可执行任务（登记意图本身不许产生任何模型调用，见
    `register_waiting_analysis_intent`）：worker 不认识它，一律跳过，返回 False。
    """
    if db_task.task_type == WAITING_INTENT_TASK_TYPE:
        return False
    if db_task.task_type == 'weekly_sync':
        # 载荷必须带 config_id（周版本任务把它存在 commit_id 列里，见
        # create_weekly_sync_task）。原先没有这条分支 → 落进下面的通用分支、
        # 载荷里没有 config_id → 处理器第一句 KeyError，被 worker 循环的
        # NON_CRITICAL_WORKER_LOOP_ERRORS 吞掉，写终态那句在 try 里执行不到
        # → 库里那行永久 pending（线上「永久排队中」的成因）。
        # 走 enqueue_weekly_sync_task 而不是下面那个 put：它同时登记「已入队」账本。
        worker.enqueue_weekly_sync_task(
            worker.background_task_queue, worker.TaskWrapper, db_task.id,
            worker.parse_config_id_from_commit_id(db_task.commit_id),
        )
        return True
    if db_task.task_type == 'weekly_excel_cache':
        task_data = {
            'id': db_task.id,
            'type': 'weekly_excel_cache',
            'data': {
                'config_id': db_task.repository_id,
                'file_path': db_task.file_path
            }
        }
    elif db_task.task_type == 'weekly_ai_analysis':
        config_id = worker.parse_config_id_from_commit_id(db_task.commit_id)
        task_data = {
            'type': 'weekly_ai_analysis',
            'config_id': config_id,
            'group_key': db_task.file_path,
            'commit_id': db_task.commit_id,
            'task_id': db_task.id
        }
        # **来源以库里的列为准**（`BackgroundTask.trigger_source`）。原先这里靠
        # 「这一组还留着等待登记」去猜，猜错的形态就是用户点出来的那次被记成「定时」。
        # 列上没有（老行）才退回「这一组还有等待登记 → manual」那条旧判据。
        row_source = _normalize_trigger_source(getattr(db_task, 'trigger_source', None))
        if row_source == TRIGGER_SOURCE_MANUAL or (
            row_source is None and str(db_task.file_path) in waiting_groups
        ):
            task_data['trigger_source'] = TRIGGER_SOURCE_MANUAL
        row_mode = _normalize_requested_mode(getattr(db_task, 'requested_mode', None))
        if row_mode:
            task_data['requested_mode'] = row_mode
    else:
        task_data = {
            'type': db_task.task_type,
            'repository_id': db_task.repository_id,
            'commit_id': db_task.commit_id,
            'file_path': db_task.file_path,
            'task_id': db_task.id
        }
    task_counter = int(time.time() * 1000000)
    priority = db_task.priority if db_task.priority is not None else _PRIORITY_DEFAULT
    tw = worker.TaskWrapper(priority, task_counter, task_data)
    # 库里那行可能已经等了几十分钟：把「入队时刻」往前拨，让它按**真实年龄**参与 aging
    # （按 `time.time()` 起算等于刚入队，aging 恒为 0 —— 那正好是本次要修的那个病）。
    # 读不到 `created_at`（测试里的桩）时不动它。
    backdate_wrapper(tw, _row_waited_seconds(db_task, now=now))
    target_queue = (
        _analysis_task_queue()
        if db_task.task_type == "weekly_ai_analysis"
        else worker.background_task_queue
    )
    target_queue.put(tw)
    return True


def _row_waited_seconds(db_task, *, now=None):
    """这一行已经等了多久（秒）。`created_at` 是 naive-UTC，与 `now` 同口径。"""
    created = _naive_utc(getattr(db_task, "created_at", None))
    if created is None:
        return 0.0
    return max((_now_utc_naive(now) - created).total_seconds(), 0.0)


def load_pending_tasks():
    """从数据库加载待处理的任务到内存队列。

    ## 启动恢复为什么改看租约（原先是一刀切）

    原先这里把所有 `processing` **无条件**收回 `pending`，理由是「本进程刚起来，
    上一轮的执行者一定死了」。单进程单 worker 时那是成立的；但库里那行 `processing`
    也可能是**另一个还活着的 worker** 正拿在手里的（第二波的部署方案就是双 worker），
    无条件收回 = 把它手里的任务抢走并重跑一遍（同一次同步写两遍缓存、同一份输入重复付费）。

    现在的判据是租约（`lease_is_expired`）：**租约还有效就不动它**，到期（或没有租约的老行）
    才收回。这与进程重启这条场景完全兼容 —— 上一个进程死掉之后它的租约不再续，到期即收回。

    ## 顺序仍然必须是「先收回、再查 pending」

    收回只改库、不再入队，而单机模式下内存队列是唯一执行路径（worker 只从队列取任务），
    于是崩溃/重启时被中断的任务在本轮进程里永远不会被执行；期间它还是 pending，
    会占着业务键堵住同键任务的重建（add_excel_diff_task 等按 pending/processing
    去重）。收回必须发生在下面那条 pending 查询之前，这些行才会在同一次调用里
    被捞到并真正入队。
    """
    try:
        now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        processing_tasks = worker._BackgroundTask.query.filter_by(status='processing').all()
        reclaimed = []
        for task in processing_tasks:
            orphaned_ai = _processing_ai_task_is_orphaned(task, now=now_utc_naive)
            if not orphaned_ai and not lease_is_expired(task, now=now_utc_naive):
                # 租约还有效 = 它的执行者可能还活着（双 worker / 上一个进程还没死透）。
                # 抢走它比放着它更危险，留给 `reclaim_expired_task_leases` 在到期后处理。
                worker.log_print(
                    f"⏳ 任务 {task.id}（{task.task_type}）仍在租约内，启动恢复不接管",
                    'TASK',
                )
                continue
            if orphaned_ai:
                worker.log_print(
                    f"♻️ AI 任务 {task.id} 已无活动 Run/Job，忽略旧租约并启动恢复",
                    'TASK',
                    force=True,
                )
            task.status = 'pending'
            task.started_at = None
            clear_task_lease(task)
            reclaimed.append(task)
        if reclaimed:
            worker._db.session.commit()
            worker.log_print(f"重置了 {len(reclaimed)} 个处理中的任务状态为待处理", 'TASK')
        pending_tasks = worker._BackgroundTask.query.filter_by(status='pending').order_by(
            worker._BackgroundTask.priority.asc(), worker._BackgroundTask.created_at.asc()
        ).all()
        # 这一轮里哪些分组还有「等同步跑完就自动开始」的登记。重启会把**意图行**留在库里
        # （它不是可执行任务，不入队），而被它转交出去的那条分析任务要重新入队 —— 装载时
        # 得让它继续记成「手动」（用户点出来的那一次），否则用量面板上会凭空变成「定时」。
        waiting_groups = {
            str(row.file_path)
            for row in pending_tasks
            if row.task_type == WAITING_INTENT_TASK_TYPE
        }
        enqueued = 0
        for db_task in pending_tasks:
            if _enqueue_pending_row(db_task, waiting_groups=waiting_groups, now=now_utc_naive):
                enqueued += 1
        worker.log_print(f"从数据库加载了 {len(pending_tasks)} 个待处理任务到队列", 'TASK')
        worker.check_and_create_auto_sync_tasks()
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"加载待处理任务数据库失败: {e}", 'TASK', force=True)
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker.log_print(f"加载待处理任务失败: {e}", 'TASK', force=True)


def regenerate_repository_cache(repository_id):
    """重新生成仓库的Excel文件缓存"""
    try:
        worker.log_print(f"开始重新生成仓库缓存: {repository_id}", 'CACHE')
        repository = worker._db.session.get(worker._Repository, repository_id)
        if not repository:
            worker.log_print(f"仓库不存在: {repository_id}", 'CACHE', force=True)
            # 返回 None（失败）而不是 0：调用方原先把 0 渲染成
            # 「✅ 已添加 0 个任务到队列」，仓库已删这种情况看起来像「正常，只是没东西」。
            return None
        worker.log_print(f"清理仓库 {repository_id} 的现有队列任务", 'CACHE')
        pending_tasks_deleted = worker._BackgroundTask.query.filter(
            worker._BackgroundTask.repository_id == repository_id,
            worker._BackgroundTask.status.in_(['pending', 'processing'])
        ).delete(synchronize_session=False)
        worker.log_print(f"删除了 {pending_tasks_deleted} 个现有队列任务", 'CACHE')
        worker.log_print(f"清理仓库 {repository_id} 的现有缓存数据", 'CACHE')
        cache_deleted = worker._DiffCache.query.filter_by(repository_id=repository_id).delete()
        worker.log_print(f"删除了 {cache_deleted} 个缓存记录", 'CACHE')
        worker._db.session.commit()
        recent_commits = worker._excel_cache_service.get_recent_excel_commits(repository, limit=1000)
        worker.log_print(f"找到 {len(recent_commits)} 个最近的可对比文件提交", 'CACHE')
        for commit in recent_commits:
            worker.add_excel_diff_task(repository_id, commit.commit_id, commit.path)
        worker.log_print(f"已添加 {len(recent_commits)} 个缓存重建任务", 'CACHE')
        return len(recent_commits)
    except (worker.SQLAlchemyError, RuntimeError, AttributeError, TypeError, ValueError) as e:
        # 必须 rollback：本函数在 commit 之前有两次 DELETE，失败时不清会话会留下
        # 脏事务（后续同一 session 上的写入会抛 PendingRollbackError），
        # 违反 .claude/rules/rules.mdc 第 8.5 条。
        try:
            worker._db.session.rollback()
        except worker.SQLAlchemyError as rollback_error:
            worker.log_print(f"重新生成仓库缓存失败后回滚也失败: {rollback_error}", 'CACHE', force=True)
        worker.log_print(f"重新生成仓库缓存失败: {e}", 'CACHE', force=True)
        traceback.print_exc()
        # 返回 None 而不是「掉出函数末尾」：调用方（worker 的 regenerate_cache 分支）
        # 原本据此打印「✅ ... 已添加 None 个任务」，把失败伪装成成功。
        return None


def schedule_cleanup_task():
    """调度清理任务"""
    task = {
        'type': 'cleanup_cache',
        'days': 30,
        'task_id': None
    }
    task_counter = int(time.time() * 1000000)
    tw = worker.TaskWrapper(_PRIORITY_CLEANUP_CACHE, task_counter, task)
    worker.background_task_queue.put(tw)
    worker.log_print("添加缓存清理任务到队列", 'TASK')


def _analysis_priority(trigger_source):
    """这次分析该排多高。**手工（含被闸门推迟后转交的那一次）高一档。**

    一次手工分析是用户点出来的动作、且要花钱；排在「保持仓库新鲜」的后台同步之后没有任何
    道理（实测：用户点击到模型开始隔了 13 分 53 秒，账单上还写着「定时」）。它不会让同步
    停摆 —— 手工点击是低频事件，同一个分组还按 `group_key` 去重。
    """
    return (
        _PRIORITY_WEEKLY_AI_MANUAL
        if _normalize_trigger_source(trigger_source) == TRIGGER_SOURCE_MANUAL
        else _PRIORITY_WEEKLY_AI_ANALYSIS
    )


def _active_analysis_tasks(group_key):
    """这个分组**所有**排队中/正在跑的分析任务（按 id 升序）。"""
    tasks = (
        worker._BackgroundTask.query.filter(
            worker._BackgroundTask.task_type == 'weekly_ai_analysis',
            worker._BackgroundTask.file_path == group_key,
            worker._BackgroundTask.status.in_(['pending', 'processing']),
        )
        .order_by(worker._BackgroundTask.id.asc())
        .all()
    )
    # 防御运行期的异常退出：不必等平台下次重启或一小时租约到期。新请求到来时，若旧执行体
    # 已经跨过保护期且 task/job/run 三层明确矛盾，就先恢复成 pending；后续附着流程会更新
    # job 关联并确保它重新入队。仍有活动 Run/Job 的任务不会进入这里。
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    reclaimed = []
    for task in tasks:
        if not _processing_ai_task_is_orphaned(task, now=now_utc_naive):
            continue
        task.status = "pending"
        task.started_at = None
        clear_task_lease(task)
        reclaimed.append(task)
    if reclaimed:
        worker._db.session.commit()
        worker.log_print(
            f"♻️ 用户请求唤醒了 {len(reclaimed)} 个无活动 Run/Job 的 AI 执行体",
            "AI",
            force=True,
        )
    return tasks


def _pick_attachable_analysis_task(tasks, requested_mode):
    """在同一个分组的活跃任务里挑一条**可以附着**的（挑不到返回 None）。

    判定表（`requested_mode` 是用户**要求**的模式，见 `BackgroundTask.requested_mode`）：

    | 这次的请求 | 已有任务 | 结果 |
    |---|---|---|
    | 没指定（调度器排的） | 任意 | 附着（调度器说的是「你看着办」，已有那条就是「你看着办」的结果） |
    | 指定了 M | 模式也是 M | 附着 |
    | 指定了 M | 没指定模式 | 附着**并升级模式**为 M（那条还没表达过偏好，用户的要求优先） |
    | 指定了 M | 模式是别的 M' | **不附着** —— 两个不同的请求，各自排一条 |

    最后一行的理由：「点全量重跑却拿到一份增量结论」比「多等一会儿」坏得多。
    """
    if not tasks:
        return None
    if requested_mode is None:
        return tasks[0]
    for task in tasks:
        if _normalize_requested_mode(getattr(task, 'requested_mode', None)) == requested_mode:
            return task
    for task in tasks:
        if _normalize_requested_mode(getattr(task, 'requested_mode', None)) is None:
            return task
    return None


def _attach_to_existing_analysis_task(
    existing_task,
    *,
    group_key,
    config_id,
    trigger_source,
    requested_mode,
    idempotency_key,
    job_id,
):
    """把这次请求**附着**到既有任务上：升级来源 / 模式 / 优先级，并补上显式关联。

    这是本次修复的核心一跳：原先去重命中时（agent 模式）什么都不改、（单机模式）只打一行
    日志就 return —— 于是手工点的那一次被记成 scheduled、意图与任务之间也没有关联。
    """
    changes = []
    row_source = _normalize_trigger_source(getattr(existing_task, 'trigger_source', None))
    source = _manual_wins(row_source, trigger_source) or TRIGGER_SOURCE_SCHEDULED
    if source != row_source:
        existing_task.trigger_source = source
        changes.append(f"来源→{source}")
    row_mode = _normalize_requested_mode(getattr(existing_task, 'requested_mode', None))
    if row_mode is None and requested_mode:
        existing_task.requested_mode = requested_mode
        changes.append(f"模式→{requested_mode}")
    row_job = getattr(existing_task, 'job_id', None)
    if job_id is not None and row_job != job_id:
        # 显式关联：这条任务服务于**哪一次用户动作**。
        #
        # **命中的是另一条 job 的任务时，这一行必须改指新的 job**（第二波 P0-01 的裁决）：
        # 复用的是「同一份输入」的排队任务，而用户在页面上拿的是**自己那个 job_id** ——
        # 不改指的话，页面按自己的 job_id 查不到任何东西（看到的是「点了没反应」），
        # 而 run 会被记到另一条 job 的账上。
        #
        # 代价（如实记下）：原来那条 job 从此指不到这条任务行。它的页面读到的是
        # 「job 还在排队/在跑但没有 task_id」—— `job_service.mark_running(job_id, task_id=…)`
        # 才是那条 job 自己的执行体关联，两条 job 谁最终跑完由 run 的那一侧结算。
        # 去重把两条 job 收成一条任务，本来就意味着**只有一次分析会跑**。
        existing_task.job_id = job_id
        changes.append(f"关联→job#{job_id}" + (f"（原 #{row_job}）" if row_job else ""))
    if idempotency_key and not getattr(existing_task, 'idempotency_key', None):
        existing_task.idempotency_key = idempotency_key

    target_priority = _analysis_priority(source)
    current_priority = getattr(existing_task, 'priority', None)
    if current_priority is None or current_priority > target_priority:
        existing_task.priority = target_priority
        changes.append(f"优先级→{target_priority}")
    worker._db.session.commit()

    if changes:
        worker.log_print(
            f"🔗 这次分析附着到已有任务 #{existing_task.id}（{', '.join(changes)}）: "
            f"group_key={group_key}",
            "AI",
            force=True,
        )
    else:
        worker.log_print(
            f"周版本AI分析任务已存在: group_key={group_key}, task_id={existing_task.id}",
            "AI",
        )

    # **库改了，内存队列里那一份也要改**：否则日志与行为对不上（库里优先级已经变了、
    # worker 取到的还是旧的那一份），而且「手工的那次排在同步前面」这条承诺落不了地。
    _ensure_analysis_task_enqueued(
        existing_task, group_key, config_id, source, target_priority
    )
    return existing_task.id


def _ensure_analysis_task_enqueued(db_task, group_key, config_id, trigger_source, priority):
    """让这条分析任务确实待在内存队列里该待的位置；返回是否**补了一次入队**。

    * 队列里已经有它（账本里有）→ 只把它的优先级挪到该在的位置（aging/升级用）；
    * 队列里没有它而库里是 pending（进程重启把内存队列丢了）→ 补一次；
    * `processing` 的那条 worker 正拿在手里，**绝不补**（补一份就是同一份输入跑两遍）。

    只看库里是不是 pending 是不够的 —— 那正是同步任务踩过的坑（「永久排队中」），
    所以判据与同步那边同一份：内存账本 `is_weekly_ai_task_enqueued`。
    """
    if worker._use_agent_dispatch():
        # 平台模式由 Agent 派发负责投递，内存队列不是执行路径。
        return False
    if getattr(db_task, 'status', None) != 'pending':
        return False
    if _is_weekly_ai_task_enqueued(getattr(db_task, 'id', None)):
        worker.retune_queued_task(
            _analysis_task_queue(),
            db_task.id,
            priority=priority,
            # 载荷里那份来源也要跟着改：随载荷走到执行侧（Agent 模式还会原样回传），
            # 库里写着「手动」、跑出来的账却是「定时」，同一条链子两处说法。
            patch={'trigger_source': trigger_source},
        )
        return False
    _enqueue_weekly_ai_analysis_task(
        _analysis_task_queue(),
        worker.TaskWrapper,
        db_task.id,
        config_id,
        group_key,
        trigger_source=trigger_source,
        priority=priority,
    )
    return True


def create_weekly_ai_analysis_task(
    config_id,
    group_key=None,
    trigger_source=None,
    requested_mode=None,
    idempotency_key=None,
    job_id=None,
):
    """为周版本配置创建AI分析任务（按「分组 + 请求模式」去重）。

    `trigger_source`：这次运行该记成谁发起的（`"manual"` / `"scheduled"`）。**只有
    「等同步跑完就自动开始」那条**需要传 `"manual"` —— 那是用户点出来的一次分析，被同步
    闸门推迟了而已（见 `wake_waiting_analysis_intents`）；调度器排的走默认值。

    这个值现在**落库**（`BackgroundTask.trigger_source`），不再只活在内存载荷里：
    去重命中一条更早创建的任务时，改的是**同一条行**；执行侧从行上读（载荷仅作兜底）。
    实测那一幕（意图 477 复用 scheduled 任务 468，run 20 被记成 scheduled、
    用户点击到模型开始隔了 13 分 53 秒）就是「只活在载荷里」的直接后果。

    `requested_mode` / `idempotency_key` / `job_id` 同理落库。`job_id` 是**意图与分析任务
    之间的显式关联**（不再靠 `group_key + created_at` 猜）。
    """
    try:
        config = worker._db.session.get(worker._WeeklyVersionConfig, config_id)
        if not config:
            worker.log_print(f"周版本配置不存在，无法创建AI分析任务: {config_id}", "AI", force=True)
            return None

        group_key = group_key or worker.build_weekly_group_key(config)
        source = _normalize_trigger_source(trigger_source) or TRIGGER_SOURCE_SCHEDULED
        mode = _normalize_requested_mode(requested_mode)
        payload_extra = {"config_id": config_id, "group_key": group_key, "trigger_source": source}
        if mode:
            payload_extra["requested_mode"] = mode

        existing_task = _pick_attachable_analysis_task(
            _active_analysis_tasks(group_key), mode
        )
        if existing_task is not None:
            if worker._use_agent_dispatch():
                # 平台模式下还要把新的来源/模式补进**已下发的那条 AgentTask** 的载荷里，
                # 否则远端回传的仍是旧来源（原先这里是直接 `return False`，连补都不补）。
                worker._ensure_agent_dispatch_for_background_task(
                    existing_task,
                    extra_payload=payload_extra,
                )
            return _attach_to_existing_analysis_task(
                existing_task,
                group_key=group_key,
                config_id=config_id,
                trigger_source=trigger_source,
                requested_mode=mode,
                idempotency_key=idempotency_key,
                job_id=job_id,
            )

        priority = _analysis_priority(source)
        new_task = worker._BackgroundTask(
            task_type='weekly_ai_analysis',
            repository_id=None,
            commit_id=str(config_id),
            file_path=group_key,
            priority=priority,
            status='pending',
            trigger_source=source,
            requested_mode=mode,
            idempotency_key=idempotency_key,
            job_id=job_id,
        )
        worker._db.session.add(new_task)
        worker._db.session.flush()
        worker._ensure_agent_dispatch_for_background_task(
            new_task,
            extra_payload=payload_extra,
        )
        worker._db.session.commit()
        _ensure_analysis_task_enqueued(new_task, group_key, config_id, source, priority)
        worker.log_print(f"创建周版本AI分析任务: config_id={config_id}, task_id={new_task.id}", "AI")
        return new_task.id
    except worker.SQLAlchemyError as e:
        worker._db.session.rollback()
        worker.log_print(f"创建周版本AI分析任务数据库失败: {e}", "AI", force=True)
        return None
    except (TypeError, ValueError, RuntimeError, AttributeError) as e:
        worker._db.session.rollback()
        worker.log_print(f"创建周版本AI分析任务失败: {e}", "AI", force=True)
        return None


# ---------------------------------------------------------------------------
#  「等同步跑完就自动开始」的等待意图
#
#  ## 为什么需要它（用户报的那句话）
#
#  闸门（`services/ai/weekly_sync_gate.py`）判得**对**：同步正逐文件写缓存时分析只能看到
#  半份变更清单，而且漏文件是**静默**的。但拦下之后原先**没有任何出路** —— 不排队、不
#  重试、不登记，用户只能自己再点一次，撞上哪一段看运气。而大仓一轮同步要 340~420 秒，
#  调度器（改节拍前）每 2 分钟还补一条，「有一条同步在写缓存」接近于稳态，于是用户看到
#  的是**反复**只出现同一句「等待 Diff 同步完成」。
#
#  审计文档要求的那一半（原文）：「同步中点击按钮时不应静默失败。创建 `waiting_snapshot`
#  任务并展示『等待 Diff 同步完成』，**同步结束后再冻结快照**」。
#
#  ## 为什么是「一行意图」而不是「直接排一条分析任务」
#
#  * 直接排 = 立刻开跑 → 撞上同步闸门被标 skipped，**意图随之丢失**（用户又得自己再点）；
#  * 它还会被「自动分析开关」管住，而这一次是**用户手动发起**的；
#  * 而意图行是一条**不执行**的记录（`load_pending_tasks` 明确跳过它），于是「登记意图
#    本身不产生任何模型调用」是结构上成立的，不靠调用方的自觉。
#
#  ## 三条不许破的口径
#
#  1. 登记不产生任何模型调用；
#  2. 转交出去的那一次照走**同一道闸门**（`sync_gate.weekly_sync_in_flight`）与**运行
#     认领**（`create_weekly_ai_analysis_task` → `run_weekly_analysis_background` →
#     `_create_run`），不绕过任何一条 —— 否则又会出现重复付费；
#  3. 用户在这期间**已经跑成了**，登记的那次必须自动作废。
# ---------------------------------------------------------------------------

# 意图在 BackgroundTask 里的类型。**不是**可执行任务：worker 不取它、不执行它。
WAITING_INTENT_TASK_TYPE = "weekly_ai_waiting"

# 意图最多等多久。判据与闸门那条上限（`SYNC_IN_FLIGHT_MAX_SECONDS`）同量级：同步超过
# 30 分钟还没把这一轮跑完，就不再让用户在页面上干等，改回「可以手动再点一次」。
WAITING_INTENT_TTL_SECONDS = 30 * 60

# 一条意图这一趟只可能落到这四种结局（**没有第五种**）。
#
# 原先有第五种 `skipped`：「同组已经有一条 pending 分析任务」→ 原地放弃（不转交、
# 不升级来源、不建立关联、意图继续 pending）。那不是一个结局，是**没有结局** ——
# 而它的症状与「正在等」逐字相同（页面上都是「已登记，等着」）。
# 这份清单是口径本身：加了第五个键，`TestAWaitedIntentAlwaysLandsSomewhere` 会红。
#
# **注意：`handed_off` 之后意图行仍然是 `pending`，这不等于「没有结局」。** 它继续等的是
# 它**自己转交出去的那条任务**（关联写在意图行的 `job_id` 上），因为意图行同时还是手工入口
# 那道「已经登记过」的判据（`effective_waiting_analysis_intent`，第二道付费闸门）——
# 提前了结它，用户在这个窗口里再点一下就又建一条 run，同一份输入跑两遍。
# 它退出 pending 只有两条路，都有界：那条任务进终态，或者有 run 覆盖了这份输入
# （见 `_resolve_waiting_intent` 与 `_intent_is_legitimately_waiting`）。
WAITING_INTENT_OUTCOMES = ("handed_off", "blocked", "retired", "expired")


def _naive_utc(value):
    """库里的时间是 naive-UTC，比较的另一头可能带时区 —— 混着减会抛 TypeError。

    不是 `datetime`（测试桩里的字符串、老数据里的怪值）时返回 `None` = **没有可用的时间**。
    不解析、也不猜：下面几处判据（任务年龄、租约是否过期）拿不到时间时的正确回答是
    「按没有时间戳处理」（租约按已过期、年龄按 0），而不是抛出去把整次装载打断 ——
    真机上一次 AttributeError 就会让 `load_pending_tasks` 一行都不入队。
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _now_utc_naive(now=None):
    current = _naive_utc(now)
    return current or datetime.now(timezone.utc).replace(tzinfo=None)


def register_waiting_analysis_intent(config_id, group_key, requested_mode=None,
                                     idempotency_key=None, job_id=None):
    """登记「这次分析在等同步跑完」。返回意图行的 id（登记不了返回 None）。

    **登记不许产生任何模型调用**：这里只落一行 `weekly_ai_waiting`，不入内存队列、
    不建 `weekly_ai_analysis` 任务、不建 `AiAnalysisRun`。真正开跑要等
    `wake_waiting_analysis_intents` 在同步收尾时把它转交出去。

    同一个分组**只留一条** pending 意图（连点两次不该攒出两条，否则唤醒时会各转交一次）。

    `trigger_source` 固定记 `manual` —— 意图只由手工入口登记（
    `ai_analysis_service.stream_weekly_analysis` 被闸门拦下那一条），这是**用户点出来的**
    一次请求。`requested_mode` / `idempotency_key` 随行落库，转交时原样带到分析任务上
    （不落库的话，用户在等待期间表达的「要全量」会在队列那一跳丢掉）。

    ## `job_id`（第二波：P0-01 job 协议）

    意图行上的 `job_id` 是**冻结语义**：「这条意图服务于哪个 `ai_analysis_job`」。
    转交出去的那条分析任务会带上它（见 `_wake_one_waiting_intent`），页面因此能靠自己的
    `job_id` 一路查到这次分析 —— 在这之前，意图与分析之间只有 `group_key + 创建时间`
    这种**猜出来的**关联（转交那一刻写下的反向往回关联才让它可查）。

    **去重命中一条已有的意图时**：传进来的 `job_id` 与那一行上的不同就**改那一行**
    （同一行改，不新建 —— 与 `_attach_to_existing_analysis_task` 改来源/模式同一个手法）。
    再登记一条会让同一个分组出现两条意图，而唤醒时会各转交一次（两次付费）。
    """
    if not group_key:
        return None
    try:
        existing = (
            worker._BackgroundTask.query.filter(
                worker._BackgroundTask.task_type == WAITING_INTENT_TASK_TYPE,
                worker._BackgroundTask.file_path == group_key,
                worker._BackgroundTask.status == "pending",
            )
            .order_by(worker._BackgroundTask.id.desc())
            .first()
        )
        if existing is not None:
            if job_id is not None and getattr(existing, 'job_id', None) != job_id:
                previous = getattr(existing, 'job_id', None)
                existing.job_id = job_id
                if requested_mode:
                    existing.requested_mode = (
                        _normalize_requested_mode(requested_mode)
                        or getattr(existing, 'requested_mode', None)
                    )
                worker._db.session.commit()
                worker.log_print(
                    f"🔗 等待意图 #{existing.id} 改指新的 job #{job_id}"
                    f"（原 #{previous}）: group_key={group_key}",
                    "AI",
                )
            return existing.id

        row = worker._BackgroundTask(
            task_type=WAITING_INTENT_TASK_TYPE,
            repository_id=None,
            commit_id=str(config_id),
            file_path=group_key,
            priority=_PRIORITY_WAITING_INTENT,
            status="pending",
            trigger_source=TRIGGER_SOURCE_MANUAL,
            requested_mode=_normalize_requested_mode(requested_mode),
            idempotency_key=idempotency_key,
            job_id=job_id,
        )
        worker._db.session.add(row)
        worker._db.session.commit()
        worker.log_print(
            f"📝 已登记「等同步跑完就自动分析」: config_id={config_id}, "
            f"intent_id={row.id}, group_key={group_key}"
            "（本次没有发起分析，也没有产生任何消耗）",
            "AI",
            force=True,
        )
        return row.id
    except worker.SQLAlchemyError as exc:
        worker._db.session.rollback()
        worker.log_print(f"❌ 登记等待同步的分析意图失败: {exc}", "AI", force=True)
        return None
    except (TypeError, ValueError, RuntimeError, AttributeError) as exc:
        worker.log_print(f"❌ 登记等待同步的分析意图失败: {exc}", "AI", force=True)
        return None


def pending_waiting_analysis_intents():
    """所有还没了结的等待意图（按登记先后）。"""
    try:
        return (
            worker._BackgroundTask.query.filter(
                worker._BackgroundTask.task_type == WAITING_INTENT_TASK_TYPE,
                worker._BackgroundTask.status == "pending",
            )
            .order_by(worker._BackgroundTask.id.asc())
            .all()
        )
    except worker.SQLAlchemyError as exc:
        worker.log_print(f"⚠️ 读取等待同步的分析意图失败: {exc}", "AI")
        return []


def _intent_age_seconds(intent, *, now=None):
    created = _naive_utc(getattr(intent, "created_at", None))
    if created is None:
        return 0.0
    return max((_now_utc_naive(now) - created).total_seconds(), 0.0)


def _weekly_runs_of_group(group_key):
    """这个分组最近的几次周版本运行（读不到就是空列表 —— 见下面的口径）。"""
    if not group_key:
        return []
    from models.ai_analysis import AiAnalysisRun

    try:
        return (
            AiAnalysisRun.query.filter(
                AiAnalysisRun.target_type == "weekly",
                AiAnalysisRun.target_key == group_key,
            )
            .order_by(AiAnalysisRun.id.desc())
            .limit(5)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 —— 读不到只是少一条判据，不该把唤醒流程打断
        worker.log_print(f"⚠️ 读取周版本分析运行记录失败（按「没有已有运行」处理）: {exc}", "AI")
        return []


def _run_covering_intent(group_key, intent):
    """已经有一次分析把这条意图「替」了吗（返回那条运行，没有返回 None）。

    两种情况都算「替了」：

    * **现在还在跑**（`pending` / `running`）：同一份输入的认领握在别人手里，再转交一次
      只会被认领拦下（或更糟：等它跑完认领放开之后又跑一遍 = 第二次付费）；
    * **在意图之后建的**（不论成败）：就是用户报的那句「我在此期间已经跑成了」。
    """
    intent_created = _naive_utc(getattr(intent, "created_at", None))
    for row in _weekly_runs_of_group(group_key):
        if str(getattr(row, "effective_status", "") or "") in ("pending", "running"):
            return row
        created = _naive_utc(getattr(row, "created_at", None))
        if intent_created is not None and created is not None and created >= intent_created:
            return row
    return None


def effective_waiting_analysis_intent(group_key, *, now=None):
    """这个分组现在有一条**生效**的等待意图吗（没有返回 None）。

    「生效」= 还没过期 **且** 还没有任何一次分析覆盖掉这份输入。三处用它，必须是同一
    把尺子：手工入口（挡住重复建 run）、唤醒（决定要不要转交）、页面轮询（决定还要不要等）。

    从**最新**的一条往回找：旧的那条过期了不该把后来登记的那条一起否掉。
    """
    wanted = str(group_key or "")
    for intent in reversed(pending_waiting_analysis_intents()):
        if str(getattr(intent, "file_path", None) or "") != wanted:
            continue
        if _intent_age_seconds(intent, now=now) > WAITING_INTENT_TTL_SECONDS:
            continue
        if _run_covering_intent(group_key, intent) is not None:
            continue
        return intent
    return None


def _retire_waiting_intent(intent, message, *, status="completed", job_reason=""):
    """把一条意图了结掉（completed=被覆盖/已转交，cancelled=过期作废）。

    `job_reason` 给「这次登记**没有转交出去**、用户这一下点击就此作废」的那几条出路用：
    它同时把**这条意图服务的 job** 收口（`job_service.settle_without_run`）。

    ## 为什么必须一起收（真机实测，2026-09-24）

    少了这一步，job 会**永远停在非终态**：它的终态原先只有两条路能推
    （`settle_from_run` 靠一条 run / `recover_stale_jobs` 靠陈旧窗口），两条都不覆盖
    「意图过期作废」。实测：意图 #4964 在 18:56 被判「等待超过 30 分钟」作废，
    而 job 31 到 19:00 仍是 `waiting_snapshot`、`active_key` 还挂着 —— 那正是
    `settle_without_run` docstring 写的那个**功能性阻塞**：同一份输入从此再也建不出
    job，用户点按钮只会附着到这条永远不动的 job 上（「点了没反应」）。

    **转交成功那条路不许传 `job_reason`**：那条 job 由跑到的那条分析任务收口。
    （另一条出路「已经被 run 覆盖」也不能替它判死 —— 那由 `_settle_job_of_intent` 里
    的 `settle_job_that_never_ran` 兜着：**已经有 run 的 job 不在这里收口**。真机实测
    2026-09-24：意图 #5034 转交出去之后，下一趟扫尾把它判成「已被自己的 run 62 覆盖」，
    顺手收口了正在跑的 job 32。）
    """
    try:
        intent.status = status
        intent.error_message = message
        intent.completed_at = datetime.now(timezone.utc)
        worker._db.session.commit()
    except worker.SQLAlchemyError as exc:
        worker._db.session.rollback()
        worker.log_print(f"❌ 收尾等待意图失败: intent={getattr(intent, 'id', None)}, {exc}", "AI", force=True)
        return
    if job_reason:
        _settle_job_of_intent(intent, job_reason, message)
    worker.log_print(
        f"⏹️ 等待同步的分析意图 #{getattr(intent, 'id', None)} 结束（{status}）: {message}",
        "AI",
        force=True,
    )


def _settle_job_of_intent(intent, reason, message):
    """把这条意图服务的 job 收口（意图作废时 job 不许留在非终态，见 `_retire_waiting_intent`）。

    **只收「从没跑起来过」的那一条**（`settle_job_that_never_ran`）：已经有 run 的 job
    终态归那条 run，在这里收会把一条**正在跑**的 job 判死 —— 而且这个分叉不会被自动
    纠正（`settle_from_run` 对终态 job 是 `continue`）。真机实测（2026-09-24）：意图
    #5034 已经转交出去、job 32 正带着 run 62 在跑，另一趟扫尾把它判成「已被 run 62
    覆盖」并顺手收口了 job —— run 继续跑，job 已经 `cancelled`。

    收口失败**只记日志**：它是一个补充动作，不该把意图的作废回滚掉 —— 回滚会让页面上
    重新出现「已登记，等着」的假象（那是比一条卡住的 job 更难查的形态）。
    """
    job_id = getattr(intent, "job_id", None)
    if not job_id:
        return
    try:
        # 局部导入：`job_service` 在函数内反向引用本模块（避免模块级循环）。
        from services.ai.job_service import settle_job_that_never_ran

        job = settle_job_that_never_ran(job_id, reason=reason, message=message)
        if job is not None:
            worker._db.session.commit()
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        try:
            worker._db.session.rollback()
        except worker.SQLAlchemyError:
            pass
        worker.log_print(
            f"⚠️ 等待意图 #{getattr(intent, 'id', None)} 作废后没能收口 job {job_id}: {exc}",
            "AI",
            force=True,
        )


def _analysis_job_ref_of_intent(intent):
    """这条意图该往分析任务行上写哪个「谁要的」引用。

    * 意图行上有 `job_id`（第二波 P0-01：`register_waiting_analysis_intent(job_id=…)`）
      → **就用它**：那一列的冻结语义是「服务于哪个 `ai_analysis_job`」，页面拿着自己的
      job_id 才能查到这次分析跑成了什么；
    * 没有 job（老数据 / 还没接 job 的入口 / 单测）→ 退回意图行自己的 id，
      让「这条任务是谁要的」至少还是一次查询能答上的事。
    """
    return getattr(intent, "job_id", None) or getattr(intent, "id", None)


def _intent_task_refs(intent):
    """这条意图在分析任务行上可能留下的引用。

    **有 job 就只认 job**（`job_id` 那一列的冻结语义），没有 job 的过渡路径才认意图行
    自己的 id —— 两个都塞进集合会让「意图 id 与 job id 数值撞车」这件事变成误判，
    而误判的代价是意图被提前作废（第二道付费闸门打开）。
    """
    job_ref = getattr(intent, "job_id", None)
    if job_ref:
        return {job_ref}
    intent_id = getattr(intent, "id", None)
    return {intent_id} if intent_id else set()


def _task_rows_of_intent(intent):
    """这个分组里引用这条意图的分析任务行（引用判据见 `_intent_task_refs`）。"""
    group_key = getattr(intent, "file_path", None)
    refs = _intent_task_refs(intent)
    if not group_key or not refs:
        return []
    try:
        rows = (
            worker._BackgroundTask.query.filter(
                worker._BackgroundTask.task_type == "weekly_ai_analysis",
                worker._BackgroundTask.file_path == group_key,
            )
            .order_by(worker._BackgroundTask.id.desc())
            .all()
        )
    except worker.SQLAlchemyError:
        return []
    return [row for row in rows if getattr(row, "job_id", None) in refs]


def _handed_off_task_of_intent(intent):
    """这条意图转交出去、**现在还活着**（排队中/在跑）的分析任务，否则 None。

    ## 怎么找（第二波改过一次判据）

    原先是在**意图行的 `job_id`** 上存「转交给了哪条任务」，再拿它 `get` 那一行。
    第二波的 `job_service._register_intent` 把同一列用来存它**服务的 job**
    （`row.job_id = job.id`，冻结语义），两个含义挤在一列 —— 拿 job id 当任务 id 去查，
    命中的会是**另一条行（或查不到）**，于是「它那条任务早没了」被误判，
    意图被提前作废、**第二道付费闸门当场打开**。所以改成**反向找**：

        这个分组里，`job_id` 指向这条意图（有 job 时）或指向这条意图行本身
        （没有 job 的过渡路径）的**还在排队/在跑**的分析任务。

    两条判据都在**同一个分组**内比较，而分组正是「同一时刻只分析一次」的那把尺子
    （`active_key` 是同一件事的服务端口径），所以不会认错到别的分组上去。
    """
    for row in _task_rows_of_intent(intent):
        if getattr(row, "status", None) in ("pending", "processing"):
            return row
    return None


def _intent_is_legitimately_waiting(intent, *, now=None):
    """这条意图现在还能光明正大地 pending 吗。返回「它还在等什么」，没有就返回 None。

    **这是「意图不许永久 pending」的守卫。** 只剩两条正当理由：

    1. **同步正在写这一批的缓存** —— 转交出去也是在闸门上再被挡一次。等多久有界：
       `WAITING_INTENT_TTL_SECONDS`；
    2. **已经转交出去了，那条分析任务还排在队列里** —— 等多久同样有界，但界不在意图身上
       而在那条任务身上（它要么被执行、要么被租约/超时清理成终态），所以这里不重复计一个
       TTL：重复计会让「排队 40 分钟但确实快轮到了」的那次被别人当成「登记已作废」，
       用户再点一次 → 同一份输入两条 run。

    其余**任何**情况都必须在这一趟里落终态（转交、作废或过期）—— 原先 `skipped` 那条早退
    把「已经转交过」也算成一种既不转交、又不作废、也不关联的悬空状态，于是意图可以永远
    pending。`wake_waiting_analysis_intents` 的收尾守卫用的就是本函数。
    """
    group_key = getattr(intent, "file_path", None)
    config_id = worker.parse_config_id_from_commit_id(getattr(intent, "commit_id", None))
    if not group_key or config_id is None:
        return None
    if _run_covering_intent(group_key, intent) is not None:
        # 用户在这期间已经跑成了（或正跑着）：该收尾了，不是「继续等」。
        return None
    handed = _handed_off_task_of_intent(intent)
    if handed is not None:
        return f"已经转交给分析任务 #{handed.id}（{handed.status}），它还排在队列里或在跑"
    if _intent_age_seconds(intent, now=now) > WAITING_INTENT_TTL_SECONDS:
        return None
    config = worker._db.session.get(worker._WeeklyVersionConfig, config_id)
    if config is None:
        return None
    # **同一道闸门**（与手工、后台那两道同一个判据、同一份实现）：同步还在写缓存就继续等。
    return sync_gate.weekly_sync_in_flight(sync_gate.group_config_ids(config)) or None


def wake_waiting_analysis_intents(*, now=None):
    """同步收尾（以及每个分析调度周期）时检查等待意图：该作废的作废、该转交的转交。

    返回各项计数，用于日志与测试断言。

    **收尾守卫（不许省）**：这一趟处理完之后仍然 pending 的意图，只允许有
    `_intent_is_legitimately_waiting` 认可的那两个理由。任何别的形态 = 有一处出路漏了
    （今天这个 bug 就是那条 `skipped` 早退），这里**当场收掉它**而不是只打一行日志 ——
    「留下日志让下一个周期再试」挡不住「用户永远等在那里」。
    """
    outcome = {"checked": 0, "swept": 0}
    outcome.update({key: 0 for key in WAITING_INTENT_OUTCOMES})
    for intent in pending_waiting_analysis_intents():
        outcome["checked"] += 1
        result = _wake_one_waiting_intent(intent, now=now)
        if result in outcome:
            outcome[result] += 1
    for intent in pending_waiting_analysis_intents():
        if _intent_is_legitimately_waiting(intent, now=now):
            continue
        outcome["swept"] += 1
        worker.log_print(
            f"🧹 等待意图 #{getattr(intent, 'id', None)} 的等待理由已经不成立"
            "（同步没在跑、也不在等它自己那条任务），guard 当场处置它"
            "（意图不许永久 pending）",
            "AI",
            force=True,
        )
        _resolve_waiting_intent(intent, now=now)
    return outcome


def _wake_one_waiting_intent(intent, *, now=None):
    """一条意图的处置。返回 outcome 的键名（永远不是 `skipped`）。"""
    if _intent_is_legitimately_waiting(intent, now=now):
        return "blocked"
    return _resolve_waiting_intent(intent, now=now)


def _resolve_waiting_intent(intent, *, now=None):
    """把一条意图**处置掉**（转交 / 作废 / 过期）。返回 outcome 的键名。

    「不许悬着」是**结构上**成立的：本函数没有早退分支，每条 return 之前都已经做了一件
    可查的事 —— 写下终态（作废/过期），或者写下**双向关联**并把那一次转交出去。
    转交成功那条路上意图仍然是 pending，但它等的是**它自己那条任务**（有关联、有界），
    不是悬空：见 `_intent_is_legitimately_waiting`。

    唯一转交不出去的情况（建任务失败）也照样把意图作废并说明原因 —— 让它留在 pending
    只会让用户在页面上一直等一个永远不会来的分析。
    """
    group_key = getattr(intent, "file_path", None)
    config_id = worker.parse_config_id_from_commit_id(getattr(intent, "commit_id", None))
    # 收口 job 用的原因短码（语义见 `job_service.WITHOUT_RUN_REASON_STATES`）。
    # 局部导入：`job_service` 在函数内反向引用本模块，模块级导入会成环。
    from services.ai.job_service import (
        REASON_ABANDONED,
        REASON_ALREADY_RUNNING,
        REASON_NO_CONFIGS,
        REASON_SYNC_IN_FLIGHT,
    )

    if not group_key or config_id is None:
        _retire_waiting_intent(
            intent,
            "意图载荷不完整（缺少分组键或 config_id），无法转交",
            job_reason=REASON_ABANDONED,
        )
        return "retired"

    covering = _run_covering_intent(group_key, intent)
    if covering is not None:
        # **用户在这期间已经跑成了**（或正跑着）：这条登记就地作废 —— 转交出去就是
        # 第二次付费运行。
        _retire_waiting_intent(
            intent,
            f"已经有一次分析（run={covering.id}）覆盖了这份输入，本次登记自动作废"
            "（没有再发起任何分析）",
            job_reason=REASON_ALREADY_RUNNING,
        )
        return "retired"

    if _intent_age_seconds(intent, now=now) > WAITING_INTENT_TTL_SECONDS:
        _retire_waiting_intent(
            intent,
            f"等待超过 {WAITING_INTENT_TTL_SECONDS // 60} 分钟仍没等到同步结束，"
            "本次登记已作废 —— 可以在同步跑完之后手动再点一次「重新分析」",
            status="cancelled",
            job_reason=REASON_SYNC_IN_FLIGHT,
        )
        return "expired"

    previous = _task_rows_of_intent(intent)
    if previous:
        # 已经转交过一次了（那个分组里有引用这条意图的任务行），而它**已经不是**排队中/
        # 在跑（否则上面就判成 blocked 了）。这次登记的使命到此为止。
        #
        # **不许在这里再转交一次**：那样失败会变成「一串任务」，而用户只点了一次。
        # 重试的语义留给用户（页面会说「可以再点一次」），那是一个明确的人的动作。
        latest = previous[0]
        _retire_waiting_intent(
            intent,
            f"转交出去的分析任务 #{latest.id} 已经不在排队中（当前 {latest.status}），"
            "本次登记结束 —— 可以再点一次「重新分析」重新发起",
        )
        return "retired"

    config = worker._db.session.get(worker._WeeklyVersionConfig, config_id)
    if config is None:
        _retire_waiting_intent(
            intent,
            f"周版本配置 {config_id} 已不存在，本次登记作废",
            job_reason=REASON_NO_CONFIGS,
        )
        return "retired"

    task_id = worker.create_weekly_ai_analysis_task(
        config_id,
        group_key=group_key,
        trigger_source="manual",
        requested_mode=getattr(intent, "requested_mode", None),
        idempotency_key=getattr(intent, "idempotency_key", None),
        # 显式关联：这条分析任务服务于**哪一次用户动作**。
        # 有 job（第二波）就用 job id —— 那一列的冻结语义是「服务于哪个 ai_analysis_job」，
        # 页面拿着自己的 job_id 才查得到这条任务；没有 job 的路径（老数据 / 测试 /
        # 尚未接 job 的入口）退回意图 id，让「谁要的」仍然可查。
        job_id=_analysis_job_ref_of_intent(intent),
    )
    if not task_id:
        _retire_waiting_intent(
            intent,
            "同步已经结束，但没能建出分析任务（转交失败），本次登记作废 —— "
            "可以手动再点一次「重新分析」",
            status="cancelled",
            job_reason=REASON_ABANDONED,
        )
        return "retired"

    _settle_handed_off_intent(intent, task_id)
    worker.log_print(
        f"▶️ 同步已结束，等待的分析自动开始: intent={getattr(intent, 'id', None)}, "
        f"config_id={config_id}, task_id={task_id}",
        "AI",
        force=True,
    )
    return "handed_off"


def _settle_handed_off_intent(intent, task_id):
    """转交成功：在分析任务行上写下「这次分析是谁要的」这个引用。

    关联走 `BackgroundTask.job_id` 一列，它的冻结语义是「服务于哪个 `ai_analysis_job`」：

    * 意图行上已经有 job（第二波 P0-01）→ 任务行写**那个 job id** —— 页面拿着自己的
      job_id 才查得到这次分析跑成了什么（写意图 id 的话，页面按 job_id 查会什么都没有）；
    * 没有 job 的过渡路径（老数据 / 还没接 job 的入口 / 单测）→ 写**意图行的 id**，
      让「这条任务是谁要的」至少还是一次查询能答上的事。

    **意图行的 `job_id` 不在这里写。** 那一列归 `job_service`（它服务的是哪个 job），
    而「这次登记转交给了哪条任务」是**反着查**出来的：任务行上的引用指回来
    （见 `_intent_task_refs` / `_task_rows_of_intent`）。两个含义挤在同一列上，
    读错一次就会把一条还在排队的任务判成「已经没了」—— 意图随即被作废，
    而它正是手工入口的第二道付费闸门。

    ## 为什么**不**在这里把意图了结

    意图要留在 pending 上继续等**它那条任务**。这不是「悬空」，而是手工入口唯一那道
    「已经登记过」的判据（`effective_waiting_analysis_intent`）：

        同步刚跑完、等排的那条分析任务还没被 worker 取走 → 用户又点了一下 →
        同步闸门**已经放行** → 直接建一条新 run；而队列里那条稍后执行时，手工这次
        可能已经跑完、认领也放开了 → 同一份输入跑两遍 = 两次付费。

    它落终态的时刻有两个，都有界：那条任务进了终态（跑完/失败/被清理），或者有 run
    覆盖了这份输入（`_resolve_waiting_intent` 的两条出路）。
    """
    task_ref = _analysis_job_ref_of_intent(intent)
    try:
        task_row = worker._db.session.get(worker._BackgroundTask, task_id)
        if task_row is not None and task_ref is not None:
            task_row.job_id = task_ref
        worker._db.session.commit()
    except worker.SQLAlchemyError as exc:
        worker._db.session.rollback()
        worker.log_print(
            f"⚠️ 写入「这次分析是谁要的」关联失败（不影响这次转交）: "
            f"intent={getattr(intent, 'id', None)}, task={task_id}, ref={task_ref}, {exc}",
            "AI",
            force=True,
        )


def _handed_off_analysis_task(group_key):
    """这个分组有没有一条**由登记转交出去**、还排在队列里的分析任务。

    判据是 `job_id` 非空 —— 转交时写下的显式关联（见 `_settle_handed_off_intent`）。
    不能用「这个分组有没有 pending 分析任务」代替：那会把调度器自己排的那条也算成
    「用户在等的东西」，于是用户什么都没点，页面也会说「这次分析正在排队」。
    """
    if not group_key:
        return None
    try:
        return (
            worker._BackgroundTask.query.filter(
                worker._BackgroundTask.task_type == "weekly_ai_analysis",
                worker._BackgroundTask.file_path == group_key,
                worker._BackgroundTask.status.in_(["pending", "processing"]),
                worker._BackgroundTask.job_id.isnot(None),
            )
            .order_by(worker._BackgroundTask.id.asc())
            .first()
        )
    except worker.SQLAlchemyError:
        return None


def describe_waiting_analysis(config_id, group_key):
    """页面轮询用：这次登记现在到哪一步了。**只读**，不建 run、不排队、不发请求。

    四种回答对应页面的四种动作：

    * `run_id` 有值 → **附着**到那次运行上（接着看它的进度与结论）；
    * 只有 `waiting` 且登记还在 → 继续等（同步还没跑完）；
    * 只有 `waiting` 且那次分析已**转交出去** → 继续等（它在队列里，还没轮到），
      这也是「排队中」而不是「没动静」——见下面的 `_handed_off_analysis_task`；
    * `waiting` 为假 → 别再等了（登记作废了、或者已经被别的分析覆盖），
      页面把按钮放回可点，并去读一次落库的结论。
    """
    for row in _weekly_runs_of_group(group_key):
        if str(getattr(row, "effective_status", "") or "") in ("pending", "running"):
            return {
                "waiting": True,
                "run_id": row.id,
                "message": (
                    f"同步已结束，这次分析已经自动开始（运行 #{row.id}）——"
                    "页面会接着显示它的进度与结论，不需要再点「重新分析」。"
                ),
            }
    intent = effective_waiting_analysis_intent(group_key)
    if intent is not None:
        return {
            "waiting": True,
            "run_id": None,
            "message": (
                f"这次分析已经登记（登记号 #{intent.id}）：版本同步一跑完就会自动开始，"
                "不需要再点「重新分析」。当前等待过程不会产生消耗。"
            ),
        }
    handed = _handed_off_analysis_task(group_key)
    if handed is not None:
        # 登记已经了结、但那一次分析还排在队列里（进度条上什么都没动的那一段）。
        # 这时说「已经结束：可以再点一次」是**错的** —— 用户再点一次就是同一份输入
        # 两条 run。判据用 `job_id` 非空（转交时写下的显式关联），所以调度器自己排的
        # 那条不会被误算成「用户在等的东西」。
        return {
            "waiting": True,
            "run_id": None,
            "message": (
                f"这次分析已经转交到后台任务 #{handed.id}（来源记为「手动」），"
                "正在排队或执行 —— 它跑起来之后页面会接着显示进度与结论，"
                "不需要再点「重新分析」。"
            ),
        }
    return {
        "waiting": False,
        "run_id": None,
        "message": (
            "这次登记已经结束：没有在等同步，也没有正在跑的分析（可能是同步一直没结束，"
            "或者已经被另一次分析覆盖）。可以再点一次「重新分析」。"
        ),
    }

