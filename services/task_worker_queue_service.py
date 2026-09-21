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

import time
import traceback
from datetime import datetime, timezone

import services.task_worker_service as worker
# 同步闸门：唤醒意图时**必须**走同一道判据（手工与后台那两道用的就是它）。
# 这里按模块取用（`sync_gate.X`）而不是 `from … import X`：测试要能按属性打补丁。
from services.ai import weekly_sync_gate as sync_gate


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
            priority=2 if force_retry else 5,
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
            tw = worker.TaskWrapper(5, task_counter, task_data)
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


def load_pending_tasks():
    """从数据库加载待处理的任务到内存队列"""
    try:
        # **先收回上次残留的 processing，再查 pending。** 顺序曾经是反的：收回只改库、
        # 不再入队，而单机模式下内存队列是唯一执行路径（worker 只从队列取任务），
        # 于是崩溃/重启时被中断的任务在本轮进程里永远不会被执行；期间它还是 pending，
        # 会占着业务键堵住同键任务的重建（add_excel_diff_task 等按 pending/processing
        # 去重）。收回必须发生在下面那条 pending 查询之前，这些行才会在同一次调用里
        # 被捞到并真正入队。
        processing_tasks = worker._BackgroundTask.query.filter_by(status='processing').all()
        for task in processing_tasks:
            task.status = 'pending'
            task.started_at = None
        if processing_tasks:
            worker._db.session.commit()
            worker.log_print(f"重置了 {len(processing_tasks)} 个处理中的任务状态为待处理", 'TASK')
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
        for db_task in pending_tasks:
            if db_task.task_type == WAITING_INTENT_TASK_TYPE:
                # **「等待同步」的意图不是可执行任务**（登记意图本身不许产生任何模型
                # 调用，见 register_waiting_analysis_intent）：worker 不认识它，装载时
                # 一律跳过。唤醒由同步收尾的钩子与每个分析调度周期负责
                # （`wake_waiting_analysis_intents`）—— 进程重启丢掉的那一瞬间由后者兜住。
                continue
            if db_task.task_type == 'weekly_sync':
                # 载荷必须带 config_id（周版本任务把它存在 commit_id 列里，见
                # create_weekly_sync_task）。原先没有这条分支 → 落进下面的通用分支、
                # 载荷里没有 config_id → 处理器第一句 KeyError，被 worker 循环的
                # NON_CRITICAL_WORKER_LOOP_ERRORS 吞掉，写终态那句在 try 里执行不到
                # → 库里那行永久 pending（线上「永久排队中」的成因）。
                # 走 enqueue_weekly_sync_task 而不是下面那个 put：它同时登记「已入队」账本。
                worker.enqueue_weekly_sync_task(worker.background_task_queue, worker.TaskWrapper, db_task.id,
                                         worker.parse_config_id_from_commit_id(db_task.commit_id))
                continue
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
                if str(db_task.file_path) in waiting_groups:
                    # 这一组还留着等待登记 → 这条分析是被它转交出去的（用户点的那一次）。
                    task_data['trigger_source'] = 'manual'
            else:
                task_data = {
                    'type': db_task.task_type,
                    'repository_id': db_task.repository_id,
                    'commit_id': db_task.commit_id,
                    'file_path': db_task.file_path,
                    'task_id': db_task.id
                }
            task_counter = int(time.time() * 1000000)
            priority = db_task.priority if db_task.priority is not None else 10
            tw = worker.TaskWrapper(priority, task_counter, task_data)
            worker.background_task_queue.put(tw)
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
        worker.log_print(f"找到 {len(recent_commits)} 个最近的Excel文件提交", 'CACHE')
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
    tw = worker.TaskWrapper(20, task_counter, task)
    worker.background_task_queue.put(tw)
    worker.log_print("添加缓存清理任务到队列", 'TASK')


def create_weekly_ai_analysis_task(config_id, group_key=None, trigger_source=None):
    """为周版本配置创建AI分析任务（按组去重）。

    `trigger_source`：这次运行该记成谁发起的（`"manual"` / `"scheduled"`），随载荷传给
    执行侧，最终落进 `AiAnalysisRun.trigger_source`。**只有「等同步跑完就自动开始」那条**
    需要传 `"manual"` —— 那是用户点出来的一次分析，被同步闸门推迟了而已（见
    `wake_waiting_analysis_intents`）；调度器排的走默认值。
    """
    try:
        config = worker._db.session.get(worker._WeeklyVersionConfig, config_id)
        if not config:
            worker.log_print(f"周版本配置不存在，无法创建AI分析任务: {config_id}", "AI", force=True)
            return None

        group_key = group_key or worker.build_weekly_group_key(config)
        payload_extra = {"config_id": config_id, "group_key": group_key}
        if trigger_source:
            payload_extra["trigger_source"] = trigger_source
        existing_task = worker._BackgroundTask.query.filter(
            worker._BackgroundTask.task_type == 'weekly_ai_analysis',
            worker._BackgroundTask.file_path == group_key,
            worker._BackgroundTask.status.in_(['pending', 'processing']),
        ).first()
        if existing_task:
            if worker._use_agent_dispatch():
                worker._ensure_agent_dispatch_for_background_task(
                    existing_task,
                    extra_payload=payload_extra,
                )
                worker._db.session.commit()
            worker.log_print(f"周版本AI分析任务已存在: group_key={group_key}", "AI")
            return existing_task.id

        new_task = worker._BackgroundTask(
            task_type='weekly_ai_analysis',
            repository_id=None,
            commit_id=str(config_id),
            file_path=group_key,
            priority=6,
            status='pending',
        )
        worker._db.session.add(new_task)
        worker._db.session.flush()
        worker._ensure_agent_dispatch_for_background_task(
            new_task,
            extra_payload=payload_extra,
        )
        worker._db.session.commit()
        if not worker._use_agent_dispatch():
            task_data = {
                'type': 'weekly_ai_analysis',
                'config_id': config_id,
                'group_key': group_key,
                'commit_id': str(config_id),
                'task_id': new_task.id,
            }
            if trigger_source:
                task_data['trigger_source'] = trigger_source
            task_counter = int(time.time() * 1000000)
            tw = worker.TaskWrapper(6, task_counter, task_data)
            worker.background_task_queue.put(tw)
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


def _naive_utc(value):
    """库里的时间是 naive-UTC，比较的另一头可能带时区 —— 混着减会抛 TypeError。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _now_utc_naive(now=None):
    current = _naive_utc(now)
    return current or datetime.now(timezone.utc).replace(tzinfo=None)


def register_waiting_analysis_intent(config_id, group_key):
    """登记「这次分析在等同步跑完」。返回意图行的 id（登记不了返回 None）。

    **登记不许产生任何模型调用**：这里只落一行 `weekly_ai_waiting`，不入内存队列、
    不建 `weekly_ai_analysis` 任务、不建 `AiAnalysisRun`。真正开跑要等
    `wake_waiting_analysis_intents` 在同步收尾时把它转交出去。

    同一个分组**只留一条** pending 意图（连点两次不该攒出两条，否则唤醒时会各转交一次）。
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
            return existing.id

        row = worker._BackgroundTask(
            task_type=WAITING_INTENT_TASK_TYPE,
            repository_id=None,
            commit_id=str(config_id),
            file_path=group_key,
            priority=6,
            status="pending",
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


def _retire_waiting_intent(intent, message, *, status="completed"):
    """把一条意图了结掉（completed=被覆盖/已转交，cancelled=过期作废）。"""
    try:
        intent.status = status
        intent.error_message = message
        intent.completed_at = datetime.now(timezone.utc)
        worker._db.session.commit()
    except worker.SQLAlchemyError as exc:
        worker._db.session.rollback()
        worker.log_print(f"❌ 收尾等待意图失败: intent={getattr(intent, 'id', None)}, {exc}", "AI", force=True)
        return
    worker.log_print(
        f"⏹️ 等待同步的分析意图 #{getattr(intent, 'id', None)} 结束（{status}）: {message}",
        "AI",
        force=True,
    )


def _pending_analysis_task_exists(group_key):
    try:
        return (
            worker._BackgroundTask.query.filter(
                worker._BackgroundTask.task_type == "weekly_ai_analysis",
                worker._BackgroundTask.file_path == group_key,
                worker._BackgroundTask.status.in_(["pending", "processing"]),
            ).first()
            is not None
        )
    except worker.SQLAlchemyError:
        return False


def wake_waiting_analysis_intents(*, now=None):
    """同步收尾（以及每个分析调度周期）时检查等待意图：该作废的作废、该转交的转交。

    返回各项计数，用于日志与测试断言。
    """
    outcome = {"checked": 0, "handed_off": 0, "blocked": 0, "retired": 0, "expired": 0, "skipped": 0}
    for intent in pending_waiting_analysis_intents():
        outcome["checked"] += 1
        result = _wake_one_waiting_intent(intent, now=now)
        if result in outcome:
            outcome[result] += 1
    return outcome


def _wake_one_waiting_intent(intent, *, now=None):
    """一条意图的处置。返回 outcome 的键名。"""
    group_key = getattr(intent, "file_path", None)
    config_id = worker.parse_config_id_from_commit_id(getattr(intent, "commit_id", None))
    if not group_key or config_id is None:
        _retire_waiting_intent(intent, "意图载荷不完整（缺少分组键或 config_id），无法转交")
        return "retired"

    covering = _run_covering_intent(group_key, intent)
    if covering is not None:
        # **用户在这期间已经跑成了**（或正跑着）：这条登记就地作废 —— 转交出去就是
        # 第二次付费运行。
        _retire_waiting_intent(
            intent,
            f"已经有一次分析（run={covering.id}）覆盖了这份输入，本次登记自动作废"
            "（没有再发起任何分析）",
        )
        return "retired"

    if _intent_age_seconds(intent, now=now) > WAITING_INTENT_TTL_SECONDS:
        _retire_waiting_intent(
            intent,
            f"等待超过 {WAITING_INTENT_TTL_SECONDS // 60} 分钟仍没等到同步结束，"
            "本次登记已作废 —— 可以在同步跑完之后手动再点一次「重新分析」",
            status="cancelled",
        )
        return "expired"

    config = worker._db.session.get(worker._WeeklyVersionConfig, config_id)
    if config is None:
        _retire_waiting_intent(intent, f"周版本配置 {config_id} 已不存在，本次登记作废")
        return "retired"

    # **同一道闸门**（与手工、后台那两道同一个判据、同一份实现）：同步还在写缓存就继续等。
    reason = sync_gate.weekly_sync_in_flight(sync_gate.group_config_ids(config))
    if reason:
        return "blocked"

    # 已经转交过（那一条分析任务在排队或在跑）→ 不重复转交。
    if _pending_analysis_task_exists(group_key):
        return "skipped"

    task_id = worker.create_weekly_ai_analysis_task(
        config_id, group_key=group_key, trigger_source="manual"
    )
    if not task_id:
        return "skipped"
    worker.log_print(
        f"▶️ 同步已结束，等待的分析自动开始: intent={getattr(intent, 'id', None)}, "
        f"config_id={config_id}, task_id={task_id}",
        "AI",
        force=True,
    )
    return "handed_off"


def describe_waiting_analysis(config_id, group_key):
    """页面轮询用：这次登记现在到哪一步了。**只读**，不建 run、不排队、不发请求。

    三种回答对应页面的三种动作：

    * `run_id` 有值 → **附着**到那次运行上（接着看它的进度与结论）；
    * 只有 `waiting` → 继续等（同步还没跑完）；
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
                f"这次分析已经登记（登记号 #{intent.id}）：同步一跑完就会自动开始，"
                "不需要再点「重新分析」。本次没有发起分析，也没有产生任何消耗。"
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

