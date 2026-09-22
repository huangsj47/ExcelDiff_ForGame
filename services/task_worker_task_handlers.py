"""通用任务 handler：搬出 `task_worker_service.py` 的执行末端。

## 为什么单独一个文件

`services/task_worker_service.py` 原先 1999 行，**顶在 `scripts/check_file_length.py
--strict` 的 2000 行 ERROR 门槛上** —— 再加一行 CI 就红。这里放的是 worker 主循环
「拿到任务之后真正干活」的那几个末端函数：配表差异、同步失败后的仓库善后、周版本
AI 分析。它们的共同点是**只被 worker 主循环调用**、彼此之间不互相调用，搬走之后
`task_worker_service.py` 里剩下的就只有「调度 + 队列 + 主循环」这条主线了。

## 与 `task_worker_service.py` 的耦合方式（本文件唯一需要理解的东西）

函数体里凡是引用 worker 模块级名字的地方都写成 `worker.X`，而不是在本文件顶部
`from services.task_worker_service import X`。这不是风格偏好，是**语义要求**：

- 那些槽位（`_app` / `_db` / `_excel_cache_service`）由 `configure_task_worker()`
  **事后重绑**，走 `from ... import` 会在 import 那一刻把它们绑成 `None`；
- 几十处测试靠 `monkeypatch.setattr(worker, "_db", fake)` 换掉它们，只有**调用时**
  去取模块属性才看得见补丁。

环形 import（本模块在 worker 加载到一半时被 import）是安全的：本文件只在函数被
调用时读 `worker.X`，那时 worker 早已加载完。`os` / `time` / `traceback` 这类
没人重绑的标准库名字不绕 worker，直接 import。
"""

from __future__ import annotations

import json
import os

import services.task_worker_service as worker
from services.task_worker_weekly_handlers import resolve_trigger_source


def trigger_source_for_task(task_id, payload=None):
    """这次分析该记成谁发起的：**数据库行是权威**，载荷只作兜底。

    为什么必须读库：去重命中一条**更早创建的**任务时，队列复用的是那一行 —— 载荷里带的
    `manual` 只在「这次是新排的」时才存在。实测那一幕（意图 477 复用 scheduled 任务 468）
    就是这么被记成 `scheduled` 的：用户点击到模型开始隔了 13 分 53 秒，账单上却写着
    「定时」。行上的来源由 `create_weekly_ai_analysis_task` 的附着逻辑改写
    （`_attach_to_existing_analysis_task`），所以读库读到的就是权威值。

    读不到行（`task_id` 为 None 的内联调用、库读失败）时才用载荷。载荷也没有就是
    `scheduled`（调度器排的）。
    """
    payload_source = payload.get("trigger_source") if isinstance(payload, dict) else None
    row_source = None
    if task_id is not None:
        try:
            row = worker._db.session.get(worker._BackgroundTask, task_id)
        except (worker.SQLAlchemyError, AttributeError, TypeError, RuntimeError):
            # 读不到行不是失败：这次分析照跑，只是来源退回载荷。
            # （AttributeError/TypeError 是给 `_db` 还没注入 / 测试桩的形态留的，
            # 它们跟「读不到行」是同一件事，不该把分析打断。）
            row = None
        row_source = getattr(row, "trigger_source", None)
    return resolve_trigger_source(row_source, payload_source)


def requested_mode_for_task(task_id, payload=None):
    """这次分析用户要的是「增量」还是「全量」：**数据库行是权威**，载荷只作兜底。

    与 `trigger_source_for_task` 同一口径、同一理由：去重命中一条更早创建的任务时，改的
    是那一行（`_attach_to_existing_analysis_task`），载荷里根本没有这次请求的模式 ——
    只看载荷的话，「用户点了全量」在队列那一跳就丢了。

    归一化借用 `models.ai_analysis` 的那一对常量（它们是唯一来源，本文件不另立一套）；
    认不出来的值当没有 —— 不把脏值往下传。
    """
    payload_mode = payload.get("requested_mode") if isinstance(payload, dict) else None
    row_mode = None
    if task_id is not None:
        try:
            row = worker._db.session.get(worker._BackgroundTask, task_id)
        except (worker.SQLAlchemyError, AttributeError, TypeError, RuntimeError):
            row = None
        row_mode = getattr(row, "requested_mode", None)
    try:
        from models.ai_analysis import ANALYSIS_MODES
    except ImportError:
        return None
    for candidate in (row_mode, payload_mode):
        text = str(candidate or "").strip().lower()
        if text in ANALYSIS_MODES:
            return text
    return None


def accepts_keyword(func, name):
    """这个可调用对象接受这个关键字参数吗。

    用在**执行侧形参还没落地**的那一跳（`requested_mode`）：不想把「传不了」写成永久
    的硬编码，也不想在对方加形参的那一天再改一次这一跳。认不出来（内建函数 /
    某些包装对象）就当不支持 —— 不支持只是少一个参数，支持却传错会直接把任务打死。
    """
    import inspect

    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
#  job 写回（第二波 P0-01 的两个钩子，都是本文件 → `services.ai.job_service`）
#
#  `AiAnalysisJob` 的**唯一写入口**是 `services.ai.job_service`（见它的模块 docstring），
#  这里只负责在「真的开始跑」与「run 落到终态」两个时刻各调一次。按模块名现取而不是
#  `from ... import`：与 `worker.X` 同一个理由（那是别的分片正在改的文件，属性取用
#  才看得见补丁），并且避免把 ai 那一层拖进本模块的 import 链。
# ---------------------------------------------------------------------------


def job_id_for_task(task_id):
    """这条分析任务服务于哪条 job（没有 job / 认不出来 → None）。

    **必须验一下那个 id 真的是一条 job。** 任务行上的 `job_id` 的冻结语义是
    「服务于哪个 `ai_analysis_job`」，但在 job 体系落地之前的路径上，写进去的是
    **意图行的 id**（见 `task_worker_queue_service._analysis_job_ref_of_intent`）。
    意图 id 与 job id 数值上完全可能撞车 —— 直接拿它去 `mark_running`，会把一条
    跟这次分析毫无关系的 job 标成「分析中」。所以这里用 `get_job` 认一次：
    认不出来就当没有 job（退回过渡期的行为：不写 job）。
    """
    if task_id is None:
        return None
    try:
        row = worker._db.session.get(worker._BackgroundTask, task_id)
    except (worker.SQLAlchemyError, AttributeError, TypeError, RuntimeError):
        return None
    ref = getattr(row, "job_id", None)
    if not ref:
        return None
    try:
        from services.ai import job_service
    except ImportError:
        return None
    try:
        return ref if job_service.get_job(ref) is not None else None
    except (worker.SQLAlchemyError, RuntimeError, AttributeError, TypeError, ValueError):
        return None


def mark_job_running_for_task(task_id, *, run_id=None):
    """把这条任务服务的 job 推到 `running`（找不到 job 就什么也不做）。

    调用点有两个，缺一不可：

    * **任务进 processing 那一刻**（`_handle_weekly_ai_analysis_task` 开头）——
      这是「真的开始跑」的时刻，`started_at` 记在这里才对（等到函数返回再记，
      记下的是**结束**时刻，job 的耗时从此永远是 0）;
    * **拿到 run 号之后**再调一次，把 `run_id` 补上（`mark_running` 的 docstring 里
      「记运行号」那一件事）。

    `job_service` 不可用（import 失败 / 库异常）时**不打断分析** —— 分析本身比 job 的
    状态重要，而 job 那边有它自己的恢复扫描兜底。
    """
    job_id = job_id_for_task(task_id)
    if job_id is None:
        return None
    try:
        from services.ai import job_service

        job = job_service.mark_running(job_id, run_id=run_id, task_id=task_id)
        worker._db.session.commit()
        return job
    except (ImportError, worker.SQLAlchemyError, RuntimeError, AttributeError,
            TypeError, ValueError) as exc:
        worker._db.session.rollback()
        worker.log_print(
            f"⚠️ 标记 job 开始失败（不影响这次分析）: job={job_id}, task={task_id}, {exc}",
            "AI",
            force=True,
        )
        return None


def settle_job_from_result(result, task_id=None):
    """这次分析跑完（或**没跑成**）之后收口它那条 job。

    判据是 `result["run_id"]`：`run_weekly_analysis_background` 只有在**真的建出了 run**
    时才带它回来。带回来就交给 `job_service.settle_from_run` 按 run 的终态映射
    （`succeeded` / `degraded` / `failed`），并清掉 `active_key`。

    ## 没有 run 的结局（第二波收尾补上的那一跳）

    分析开关关着 / 预算不足 / 同步闸门拦下 / 没有变化直接复用 —— 这些结局在 worker 里
    直接结束、**不带 run 回来**，返回的形状是 `{"status": "skipped", "reason": ...}`。
    原先这里什么也不做，于是那条 job 永远停在非终态、`active_key` 也从没被清空 ——
    而 `uq_ai_job_active_key` 是唯一索引，后果是**同一个 target + focus 再也建不出任何
    job**（用户点按钮只会附着到那条永远不动的 job 上：点了没反应）。

    现在这一跳按 `result["reason"]` 交给 `job_service.settle_without_run`，终态按
    **语义**选（`no_change` → `reused`、开关/预算/闸门 → `cancelled`、真错误 → `failed`）。

    `task_id` 是**必须**的入参：没有 run 的结局要靠它找到那条 job（job 行挂在任务行上）。
    老调用方不传时行为与原来一致（什么也不做）—— 认不出 job 时不许乱改别人的 job 行，
    见 `job_id_for_task` 的说明（任务行上那一列在过渡路径上可能是**意图 id**）。
    """
    run_id = (result or {}).get("run_id") if isinstance(result, dict) else None
    reason = (result or {}).get("reason") if isinstance(result, dict) else None
    message = (result or {}).get("message") if isinstance(result, dict) else None
    reused_run_id = (result or {}).get("reused_run_id") if isinstance(result, dict) else None
    try:
        from services.ai import job_service
    except ImportError:
        return None
    try:
        job_id = job_id_for_task(task_id) if task_id is not None else None
        if reason == "no_change" and job_id is not None:
            job = job_service.settle_without_run(job_id, reason=reason, message=message)
            if job is not None and reused_run_id:
                job.reused_run_id = reused_run_id
            worker._db.session.commit()
            return job
        if reason == "already_running" and run_id and job_id is not None:
            job = job_service.mark_running(job_id, run_id=run_id, task_id=task_id)
            from models.ai_analysis import AiAnalysisRun
            active_run = worker._db.session.get(AiAnalysisRun, run_id)
            if job is not None and active_run is not None:
                job.effective_mode = active_run.scope
                try:
                    request_payload = json.loads(active_run.request_payload or "{}")
                except (TypeError, ValueError):
                    request_payload = {}
                policy = request_payload.get("policy") or {}
                if job.requested_mode == "incremental" and active_run.scope == "full":
                    job.upgrade_reason = str(policy.get("reason") or "runtime_scope_upgrade")
            worker._db.session.commit()
            return job
        if not run_id:
            # **没有 run 的结局**：按 `reason` 结清（终态由 job_service 选）。
            if task_id is None:
                return None
            if job_id is None:
                return None
            job = job_service.settle_without_run(job_id, reason=reason, message=message)
            worker._db.session.commit()
            return job

        from models.ai_analysis import AiAnalysisRun

        run_row = worker._db.session.get(AiAnalysisRun, run_id)
        if run_row is None:
            # run 号有、那条行读不到（被人清了 / 库脏）：**按失败收口**，
            # 不留一条「看起来还在跑」的 job。判据是「有引用但读不到」= 不确定，
            # 而不确定在这里的正确处置是收口（否则 active_key 永远占着索引位）。
            if task_id is None:
                return None
            if job_id is None:
                return None
            job = job_service.settle_without_run(
                job_id, reason=job_service.REASON_TASK_ENDED_WITHOUT_RUN
            )
            worker._db.session.commit()
            return job
        job = job_service.settle_from_run(run_row)
        worker._db.session.commit()
        return job
    except (ImportError, worker.SQLAlchemyError, RuntimeError, AttributeError,
            TypeError, ValueError) as exc:
        worker._db.session.rollback()
        worker.log_print(
            f"⚠️ 收口 job 失败（不影响这次分析的结果落库）: run={run_id}, "
            f"task={task_id}, reason={reason}, {exc}",
            "AI",
            force=True,
        )
        return None


def settle_job_failed_for_task(task_id, exc):
    """这次分析**抛异常**结束时收口它的 job（找不到 job 就什么也不做）。

    第三跳，专治一种漏网：异常发生在 `run` **已经建出来之后**（收尾那一段 ——
    载荷构造 / 推进水位线 / 落库），于是上面那两跳一个都没走到。那条 job 会带着
    `active_key` 停在 `running`，而 `active_key` 是**唯一索引**：同一个 target 从此
    再也建不出任何 job（用户点按钮只会附着到它上面，状态永远不变）。

    启动时的恢复扫描能兜住它，但扫描要等**下一次重启** —— 一个功能性阻塞不该等重启。

    终态记 `failed`：run 可能已经落成 `succeeded`，但这次交付**没有完成**，而任务行
    在同一个分支里也被标成 `failed`（本文件既有的那一支），两者的口径一致。
    """
    job_id = job_id_for_task(task_id)
    if job_id is None:
        return None
    try:
        from services.ai import job_service

        job = job_service.settle_without_run(
            job_id,
            reason=job_service.REASON_INTERRUPTED,
            message=(
                f"这次分析在收尾阶段中断（{type(exc).__name__}: {exc}），"
                "按失败收口 —— 可以重新发起。"
            ),
        )
        worker._db.session.commit()
        return job
    except (ImportError, worker.SQLAlchemyError, RuntimeError, AttributeError,
            TypeError, ValueError) as inner:
        worker._db.session.rollback()
        worker.log_print(
            f"⚠️ 异常收尾时结算 job 失败（不影响这次失败本身被记下）: "
            f"job={job_id}, task={task_id}, {inner}",
            "AI",
            force=True,
        )
        return None


def _handle_excel_diff_task(task, priority):
    """处理Excel差异任务"""
    worker.log_print(f"📊 处理Excel差异: repo={task['repository_id']}, commit={task['commit_id'][:8]}, file={task['file_path']}", 'EXCEL')
    with worker._app.app_context():
        if 'task_id' in task:
            try:
                worker.update_task_status_with_retry(task['task_id'], 'processing')
            except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                worker.log_print(f"更新任务开始状态失败: {update_error}", 'TASK', force=True)
        try:
            # 必须看返回值决定任务终态。原实现忽略返回值、无条件标 completed，
            # 于是「仓库已删 / 提交查不到」这类什么都没做的情况也被记为成功，
            # 与真的生成成功在任务表里完全同形（管理员无法区分「已完成」和「根本没做」）。
            status = worker._excel_cache_service.process_excel_diff_background(
                task['repository_id'], task['commit_id'], task['file_path']
            )
            if 'task_id' in task:
                try:
                    if status == worker._BG_STATUS_COMPLETED:
                        worker.update_task_status_with_retry(task['task_id'], 'completed')
                    elif status == worker._BG_STATUS_SKIPPED_IN_PROGRESS:
                        # 同一 (repo, commit, file) 正被另一线程处理 —— 工作确实在推进，
                        # 本任务无需重试，标 completed 是合理的。
                        worker.log_print(f"任务与在途处理重复，跳过: {task['task_id']}", 'TASK')
                        worker.update_task_status_with_retry(task['task_id'], 'completed')
                    else:
                        # repository_missing / commit_missing / error → 显式失败，
                        # 让管理员在任务列表里看得见，而不是被 ✅ 盖住。
                        worker.update_task_status_with_retry(
                            task['task_id'], 'failed',
                            f'Excel差异未生成（{status}）: repo={task["repository_id"]}, '
                            f'commit={task["commit_id"]}, file={task["file_path"]}'
                        )
                except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    worker.log_print(f"更新任务完成状态失败: {update_error}", 'TASK', force=True)
        except worker.NON_CRITICAL_TASK_EXECUTION_ERRORS as e:
            worker.log_print(f"❌ Excel差异处理失败: {e}", 'EXCEL', force=True)
            try:
                worker._db.session.rollback()
            except worker.SQLAlchemyError as rollback_error:
                worker.log_print(f"会话回滚失败: {rollback_error}", 'DB', force=True)
            if 'task_id' in task:
                try:
                    worker.update_task_status_with_retry(task['task_id'], 'failed', str(e))
                except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    worker.log_print(f"更新任务状态失败: {update_error}", 'TASK', force=True)


def _reset_repository_to_head(git_service, repository):
    """超时或失败后重置仓库到 HEAD 状态"""
    local_path = getattr(git_service, "local_path", None)
    if local_path and not os.path.isdir(local_path):
        # 工作目录根本不存在（clone 从来没成功过、或被人删掉了）：下面三条 git 命令
        # 都只会以「[WinError 267] 目录名称无效」失败，每条刷两行日志 —— 而这里
        # 本来就没有任何东西可重置。线上报障就是这一串（`repos/<xxx>_4` 反复刷屏）。
        worker.log_print(f"ℹ️ [RESET] 跳过重置：工作目录不存在 {local_path}", 'SYNC')
        return
    try:
        worker.log_print(f"🔄 [RESET] 正在重置仓库 {repository.name} 到 HEAD 状态...", 'SYNC', force=True)
        cleanup_locks = getattr(git_service, "_cleanup_git_lock_files", None)
        if callable(cleanup_locks):
            removed_locks = cleanup_locks()
            if removed_locks:
                worker.log_print(f"🧹 [RESET] 已清理Git锁文件: {', '.join(removed_locks)}", 'SYNC')

        reset_result = git_service._run_git_command(['git', 'reset', '--hard', 'HEAD'], timeout=60)
        if reset_result and reset_result.returncode == 0:
            worker.log_print("✅ [RESET] git reset --hard HEAD 成功", 'SYNC')
        else:
            worker.log_print("⚠️ [RESET] git reset --hard HEAD 失败", 'SYNC', force=True)
        clean_result = git_service._run_git_command(['git', 'clean', '-fd'], timeout=60)
        if clean_result and clean_result.returncode == 0:
            worker.log_print("✅ [RESET] git clean -fd 成功", 'SYNC')
        else:
            worker.log_print("⚠️ [RESET] git clean -fd 失败", 'SYNC', force=True)

        gc_result = git_service._run_git_command(['git', 'gc', '--prune=now'], timeout=120)
        if gc_result and gc_result.returncode == 0:
            worker.log_print("✅ [RESET] git gc --prune=now 成功", 'SYNC')
        else:
            worker.log_print("⚠️ [RESET] git gc --prune=now 失败", 'SYNC', force=True)
    except worker.NON_CRITICAL_VCS_PREHEAL_ERRORS as reset_err:
        worker.log_print(f"❌ [RESET] 重置仓库异常: {reset_err}", 'SYNC', force=True)


def _record_sync_error(repository, error_message):
    """将同步错误信息记录到仓库模型"""
    worker.record_repository_sync_error(
        worker._db.session,
        repository,
        error_message,
        log_func=worker.log_print,
        log_type="SYNC",
        commit=True,
    )


def _clear_sync_error(repository):
    """同步成功后清除仓库的错误信息"""
    worker.clear_repository_sync_error(
        worker._db.session,
        repository,
        log_func=worker.log_print,
        log_type="SYNC",
        commit=True,
    )


def _abandon_timed_out_auto_sync_task(task, message):
    """并发许可等不到时，把任务**真正了结掉**，而不是留下一行 pending。

    原先这里只打一行日志就 `return`：任务已经从内存队列里弹掉（单机模式下内存队列是
    唯一执行路径），而库里的行仍是 `pending` —— 它既不会被执行、也不会被重新入队，
    却仍然占着 `create_auto_sync_task` 的「同仓库已有 pending」去重位。于是这个仓库
    此后每一次「更新仓库 / 重试同步」都会被去重成一个返回 `existing_task.id` 的空操作，
    调用方却以为任务已经派下去了 —— 一次并发等待超时 = 该仓库静默停止同步，直到进程
    重启（重启时 `load_pending_tasks` 才会把它捞回队列）。

    标成 failed 的作用就是**把去重位释放掉**：下一次触发会创建一个新任务。
    `update_task_status_with_retry` 会顺手累加 `retry_count`，但本仓库没有任何代码
    读它做自动重试（`BackgroundTask.retry_count` 只在界面上展示），所以不会因此
    变成「重试次数用尽」。
    """
    task_id = task.get('task_id')
    if task_id is None:
        # Agent 派发路径不传 task_id（见 _dispatch_agent_task 的 auto_sync 分支）：
        # 那条路径的任务状态由 agent 侧管理，这里不能替它改。
        worker.log_print(f"⚠️ 任务无 task_id（Agent 派发路径），跳过状态标记：{message}", 'SYNC', force=True)
        return
    try:
        # 必须自带 app context：本函数由 Thread 启动的 worker 调用，不继承主线程的
        # context，而 update_task_status_with_retry 用的是模块级 _db.session.get()。
        with worker._app.app_context():
            worker.update_task_status_with_retry(task_id, worker.TASK_STATUS_FAILED, message)
    except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
        worker.log_print(f"标记超时任务为失败时出错: {update_error}", 'TASK', force=True)


def _handle_weekly_ai_analysis_task(task):
    """处理周版本AI分析任务"""
    with worker._app.app_context():
        task_id = task.get("task_id")
        if task_id is not None:
            try:
                worker.update_task_status_with_retry(task_id, "processing")
            except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                worker.log_print(f"更新AI分析任务开始状态失败: {update_error}", "AI", force=True)
        # **job 写回的第一跳**：这条任务服务的 job 现在真的开始跑了（第二波 P0-01）。
        # 放在这里而不是等下面那个阻塞调用返回：返回时 run 已经跑完，`started_at` 记的
        # 会是**结束**时刻（job 的耗时永远是 0）。找不到 job（没有 job 的过渡路径）时
        # 它什么也不做。
        mark_job_running_for_task(task_id)
        try:
            config_id = task.get("config_id") or task.get("commit_id") or task.get("repository_id")
            try:
                config_id = int(config_id)
            except (TypeError, ValueError):
                config_id = None
            if not config_id:
                raise ValueError("weekly_ai_analysis 缺少有效 config_id")

            run_kwargs = {
                "task_id": task_id,
                # **从数据库行读**（载荷仅作兜底）：去重复用一条更早创建的任务时，改的是
                # 那一行的 trigger_source，载荷里根本没有这次请求的来源。原先只看载荷，
                # 于是用户点出来的那一次被记成「定时」。见 `trigger_source_for_task`。
                "trigger_source": trigger_source_for_task(task_id, task),
            }
            # `requested_mode`：行上有就带上 —— 但**执行侧目前还没有这个形参**
            # （`services/ai_analysis_service.run_weekly_analysis_background` 的签名只有
            # `config_id / task_id / trigger_source`，范围由 `build_weekly_payload` 自己
            # 裁决）。所以这一跳按「对方接受就传」接线：形参一落地就自动通了，不用再改
            # 这里；今天它至少不会**静默**丢掉「用户点了全量」这件事（留一行警告）。
            mode = requested_mode_for_task(task_id, task)
            if mode and accepts_keyword(worker.run_weekly_analysis_background, "requested_mode"):
                run_kwargs["requested_mode"] = mode
            elif mode == "full":
                worker.log_print(
                    f"⚠️ 这次分析请求的是**全量**，但执行侧还没有接受 `requested_mode` 的形参"
                    f"（task_id={task_id}）—— 范围仍由平台自己裁决，这一跳被降级处理",
                    "AI",
                    force=True,
                )

            result = worker.run_weekly_analysis_background(config_id, **run_kwargs)
            # **job 写回的第二跳**：run 已经落到终态，按它的终态收口那条 job
            # （状态映射 + `finished_at` + 清 `active_key`）。先补一次 `run_id`
            # （`mark_running` 的 docstring 里「记运行号」那一件事），再结算。
            run_id = result.get("run_id") if isinstance(result, dict) else None
            if run_id:
                mark_job_running_for_task(task_id, run_id=run_id)
            settle_job_from_result(result, task_id=task_id)
            status = result.get("status")
            if task_id is not None:
                if status == "succeeded":
                    worker.update_task_status_with_retry(task_id, "completed")
                elif status == "skipped":
                    reason = result.get("reason") or "skipped"
                    worker.update_task_status_with_retry(task_id, "completed", f"skipped:{reason}")
                else:
                    worker.update_task_status_with_retry(task_id, "failed", str(result.get("reason") or "analysis_failed"))
        except worker.NON_CRITICAL_TASK_EXECUTION_ERRORS as exc:
            worker.log_print(f"❌ 周版本AI分析失败: {exc}", "AI", force=True)
            try:
                worker._db.session.rollback()
            except worker.SQLAlchemyError:
                pass
            # **job 写回的第三跳（失败兜底）**：异常可能发生在 `run` 已经建出来**之后**
            # （收尾那一段），于是上面两跳一个都没走到 —— 那条 job 会带着 `active_key`
            # 停在非终态，而那是唯一索引：同一个 target 从此再也建不出 job。
            # 顺序放在任务状态之前：先让身份闭口，再记执行体。
            settle_job_failed_for_task(task_id, exc)
            if task_id is not None:
                try:
                    worker.update_task_status_with_retry(task_id, "failed", str(exc))
                except worker.NON_CRITICAL_TASK_STATUS_ERRORS as update_error:
                    worker.log_print(f"更新AI分析任务失败状态异常: {update_error}", "AI", force=True)
