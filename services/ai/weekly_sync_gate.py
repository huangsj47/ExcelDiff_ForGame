#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「周版本同步还在跑」的闸门：同步没写完之前不要开始 AI 分析。

## 为什么必须有这道闸门

周版本同步是**按文件逐个**写缓存行的（`weekly_version_logic.process_weekly_version_sync`
的循环 → `generate_weekly_merged_diff`：一个文件一行、各自提交），而 AI 分析的变更清单
按 `WeeklyVersionDiffCache.updated_at > last_analyzed_at` 取（`_summarize_weekly_files`）。

于是**同步跑到一半时的快照只有已写好的那批文件**。用户看到的表现是这句话：

    本轮仅取得少量配表 diff 与角色属性表/怪物仇恨表内容，
    战斗逻辑、吸灵器链、拓扑组队、支付等代码改动均未取到 diff 正文

——不是「取不到」，是**那批文件当时还没被写进缓存**，所以根本没进清单。更糟的是它是
**静默**的：提示词里的「共 N 个文件」也跟着变小，模型与读报告的人都以为那就是全量。

为什么容易撞上：两个调度器周期不同（同步每 2 分钟、AI 每 1 分钟），启动时 AI 任务可以
先被创建并执行；而重启时 `load_pending_tasks` 还会把上次残留的 `processing` 任务改回
pending 再跑一次（见 `ai_analysis_service.run_weekly_analysis_background` 的说明）。

队列优先级本身是对的（`weekly_sync`=3 先于 `weekly_ai_analysis`=6），但那只在**两者都
已经排队**时才起作用。

## 边界

* **只看 `weekly_sync`。** `auto_sync`（每 2 分钟、所有仓库）不改变变更清单的构成
  （清单来自周版本缓存行），而它长期在跑 —— 拿它当闸门会让分析几乎永远不触发。
* **有上限，但上界的判据是「执行者还在不在」**（2026-09-26 改）。一个卡死的同步任务不能
  把分析永久挡住 —— 但「卡死」不等于「跑得久」。执行者还在（平台侧租约还在续）时，
  这条同步确实在往缓存里写，跑多久都该继续等；执行者的租约过期了，才是它死了。
  所以现在是两档：**执行者已失联**按 `SYNC_IN_FLIGHT_MAX_SECONDS`（30 分钟）放行；
  **执行者还在**则按 `SYNC_IN_FLIGHT_ALIVE_MAX_SECONDS`（6 小时）兜底。
  放行时一律带一条醒目日志（`weekly_sync_stuck_note`）。改之前的样子是一刀切 30 分钟墙钟：
  真机上 2054 个提交 / 583 个文件的仓库跑了十几分钟，再大一个数量级就会被**当成卡死放行**，
  而那时缓存正写到一半 —— 静默缺文件，正是本模块存在的理由。
  卡死本身还有别的机制兜（`schedule_weekly_sync_tasks` 会把超时的 pending 置 failed；
  `reclaim_expired_task_leases` 会把租约过期的任务收回重投），这道上限是最后一道保底。
* **`pending` 与 `processing` 一律算在跑**（2026-09-22；此前 2026-09-21 曾收窄过一次，
  见 `_in_flight_sync_task` 的 docstring —— 那次收窄的前提已被队列拆分推翻）。
  结论是不对称的：误拦的代价是分析晚几分钟（有上限兜底、同步**真的会跑完**），
  误放的代价是一份**看起来完整、实则缺文件**的报告 —— 后者是静默的。
* **「缓存有没有落后」不止看时间**（2026-09-23 补）：`weekly_sync_needed_config_ids`
  另有一条判据 —— 缓存行引用的提交还在不在**当前 tip 的可达集合**里
  （`services/ai/snapshot_consistency.py`）。强推 / 回退 / 重建裸库之后，tip 相等、
  也没有「同步之后新入库的 Commit」，缓存却指向了已被删除的提交；只比 tip 判不出来。
  落地动作在 `services/weekly_window_reconcile.py`（同步时按可达集合原子替换窗口缓存），
  所以这条闸门判出来的「需要同步」是真能被修好的。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from services.task_liveness import executor_alive

# **执行者已经失联**（平台侧租约过期或从来没有过租约）时，一条同步最多再挡多久。
#
# 取值依据：周版本同步要逐文件算合并 diff（大仓库上千个文件），分钟级是常态；
# 而 AI 分析的触发周期是 1 分钟 —— 等一会儿没有代价，等一小时才是问题。
#
# **它不再是唯一的界**：执行者还活着时用下面那条（2026-09-26 改）。这一条现在的含义收窄成
# 「一条没人管的僵尸任务最多再挡住多久」—— 租约过期已经说明执行者死了，不该再让它拦着。
SYNC_IN_FLIGHT_MAX_SECONDS = 30 * 60

# **执行者还活着**（平台侧租约还在续）时，一条 `processing` 的同步最多再挡多久。
#
# 与上面那条的区别不在数值，在**判据**：租约还在续说明进程还在、这条同步确实在往缓存里写，
# 只是大仓的同步本来就要跑很久 —— 真机 2026-09-26 实测：2054 个提交、583 个文件、
# 约 1 文件/秒 ≈ 十几分钟；再大一个数量级的仓库就是这个数的十倍。拿「30 分钟墙钟」去砍它，
# 砍掉的不是卡死的任务，而是**正常但慢**的那一类，而放行的代价是 AI 在一份写了一半的缓存上
# 出结论（正是本模块存在的理由）。
#
# 所以这里给的是一条**兜底**，不是「同步应该跑多久」：6 小时 = 实测量的一个数量级以上，
# 它只治「进程活着但任务永远写不完」那一种。
SYNC_IN_FLIGHT_ALIVE_MAX_SECONDS = 6 * 3600


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """把库里的时间统一成 naive-UTC。

    SQLite 存的是 naive（ORM 默认写 `datetime.now(timezone.utc)`，丢 tzinfo），
    而比较的另一头可能带时区 —— 混着减会抛 `TypeError`，那会让闸门变成「每次都不拦」，
    也就是静默失效。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _in_flight_sync_task(config_ids: Iterable[int], *, now: Optional[datetime] = None):
    """这个批次里**算在跑**的周版本同步任务，返回 `(task, 已跑秒数, 是否仍在拦)`。

    没有就 `(None, 0.0, False)`。

    ## 「算在跑」= `pending` 与 `processing` **一律算**（2026-09-22）

    这条判据被收窄过一次又被改回来，两次都有实测依据 —— 改动前**必须先读**
    `tests/test_ai_weekly_sync_gate_scope.py` 的模块 docstring，那里留着完整来龙去脉。
    一句话版本：2026-09-21 那次「执行侧忙就放行」的前提是**单线程 worker 被分析占满**，
    而 `62b4746` 把 `weekly_ai_analysis` 拆进独立队列 + 独立线程
    （`services/task_worker_service.py:984-987`）之后，分析**不再占住通用线程**，
    一条 `pending` 的同步**一定**会在分析期间被通用线程取走并开始写缓存。

    ## 上限的判据是「还有没有一条仍该拦」，不是「最老的那条超没超」

    上界**逐条**作用：一批里同时有一条卡死 40 分钟的旧同步与一条刚开跑 2 分钟的新同步时，
    后者仍可能在写缓存，必须继续拦。只有**全都不该拦**时才返回最老的那条 ——
    那是给 `weekly_sync_stuck_note` 指名用的。

    一条任务该不该拦，看两件事（2026-09-26 起）：

    * **执行者还在**（`task_liveness.executor_alive`）→ 拦，跑多久都拦（界是
      `SYNC_IN_FLIGHT_ALIVE_MAX_SECONDS`）；
    * 执行者不在了 → 按年龄判，界是 `SYNC_IN_FLIGHT_MAX_SECONDS`（30 分钟）。

    第三个返回值就是「选中的这条还在拦吗」。它必须跟着**选中的那一条**走，不能各算各的：
    `weekly_sync_in_flight` 与 `weekly_sync_stuck_note` 是两个互斥的回答，读同一份选择
    才不会出现「一边说在拦、一边说已放行」。
    """
    ids = [str(int(item)) for item in config_ids if item is not None]
    if not ids:
        return None, 0.0, False

    from models import BackgroundTask

    try:
        rows = (
            BackgroundTask.query.filter(
                BackgroundTask.task_type == "weekly_sync",
                BackgroundTask.commit_id.in_(ids),
                BackgroundTask.status.in_(("pending", "processing")),
            )
            # **降序**（=上面 docstring 说的「最新的那一条」）。原来是 `asc` 取 `rows[0]`
            # —— 那拿到的是**最旧**的一条：一批里同时有一条卡死 40 分钟的同步与一条刚起跑
            # 10 秒的同步时，闸门按旧那条算 age 已经超过 `SYNC_IN_FLIGHT_MAX_SECONDS`，
            # 于是**直接放行**，而另一个同步正在往缓存里写 —— 变更清单缺文件，正是这个
            # 模块存在的理由。
            .order_by(BackgroundTask.created_at.desc())
            .all()
        )
    except Exception:  # noqa: BLE001 —— 查不动只是少一道闸，不该把分析卡死
        return None, 0.0, False
    if not rows:
        return None, 0.0, False

    current = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)

    def _age_of(row) -> float:
        # pending 还没有执行起点，只能从 created_at 计算「排队卡了多久」。processing
        # 必须从本次 started_at 计算：启动恢复会保留原 created_at，再重新认领并写一个新的
        # started_at。若仍用 created_at，一条排队/中断了 31 分钟、实际只重跑 5 分钟的同步
        # 会被误判为超过上限，AI 随即在它写缓存的中途冻结半份快照。
        status = str(getattr(row, "status", "") or "").strip().lower()
        started = _as_naive_utc(getattr(row, "started_at", None))
        created = _as_naive_utc(getattr(row, "created_at", None))
        anchor = started if status == "processing" and started is not None else created
        return max((current - anchor).total_seconds(), 0.0) if anchor else 0.0

    aged = [(_age_of(row), row) for row in rows]

    def _blocks(age, row) -> bool:
        if executor_alive(row, now=current):
            # 执行者还在：它确实在写缓存，只是这条同步本来就久 —— 大仓按小时算。
            return age <= SYNC_IN_FLIGHT_ALIVE_MAX_SECONDS
        return age <= SYNC_IN_FLIGHT_MAX_SECONDS

    blocking = [item for item in aged if _blocks(*item)]
    # 仍在拦的里面取**最老**的那条：它最接近上限，报出来信息量最大。
    # 全不该拦时取最老的那条 = 真正卡死的那条（`stuck_note` 要指名它）。
    if not blocking:
        age, worst = max(aged, key=lambda item: item[0])
        return worst, age, False
    age, worst = max(blocking, key=lambda item: item[0])
    return worst, age, _blocks(age, worst)


def weekly_sync_needed_config_ids(config_ids: Iterable[int]) -> list[int]:
    """返回缓存可能落后于仓库采集结果的配置。

    三种情况都必须挡住快照：

    1. 仓库 ``auto_sync`` 仍在写 Commit 行；
    2. 它刚写完，而最近一次 ``weekly_sync`` 完成之后又出现了窗口内 Commit。这是
       2026-09-23 真机复现的启动竞态：worker 先用旧 Commit 表完成周版本同步，随后
       auto_sync 拉到新提交，AI 此时看不到任何 active weekly_sync，却会冻结一份已经
       过期的缓存。
    3. **缓存行引用了当前 tip 上不存在的提交**（`snapshot_consistency`）—— 强推 /
       回退 / 重建裸库之后的形态。它**不能**由前两条覆盖：2026-09-23 实测里
       ``repository.last_synced_tip`` 已经等于远端 tip、「同步之后有没有新 Commit」
       也判为否，而配置 3 的缓存仍指向 e54c73df 等已被删除的提交。「只比 tip 是不够的」
       说的就是这一格。
    """
    ids = sorted({int(item) for item in config_ids if item is not None})
    if not ids:
        return []
    from models import BackgroundTask, Commit, WeeklyVersionConfig
    from utils.timezone_utils import beijing_window_to_utc_naive

    try:
        configs = WeeklyVersionConfig.query.filter(WeeklyVersionConfig.id.in_(ids)).all()
        repo_ids = [cfg.repository_id for cfg in configs]
        active_repo_ids = {
            row.repository_id
            for row in BackgroundTask.query.filter(
                BackgroundTask.task_type == "auto_sync",
                BackgroundTask.repository_id.in_(repo_ids),
                BackgroundTask.status.in_(("pending", "processing")),
            ).all()
            if row.repository_id is not None
        }
        needed = {cfg.id for cfg in configs if cfg.repository_id in active_repo_ids}
        for cfg in configs:
            latest_sync = (
                BackgroundTask.query.filter(
                    BackgroundTask.task_type == "weekly_sync",
                    BackgroundTask.commit_id == str(cfg.id),
                    BackgroundTask.status == "completed",
                    BackgroundTask.completed_at.isnot(None),
                )
                .order_by(BackgroundTask.completed_at.desc())
                .first()
            )
            if latest_sync is None:
                continue
            start_utc, end_utc = beijing_window_to_utc_naive(cfg.start_time, cfg.end_time)
            newer = Commit.query.filter(
                Commit.repository_id == cfg.repository_id,
                Commit.commit_time >= start_utc,
                Commit.commit_time <= end_utc,
                Commit.created_at > latest_sync.completed_at,
            ).first()
            if newer is not None:
                needed.add(cfg.id)
    except Exception as exc:  # noqa: BLE001 —— 读不动时仍由下面那条来源核对与 active 闸门兜底
        from utils.logger import log_print

        log_print(
            f"⚠️ 检查周版本缓存新鲜度失败（时间口径这一条本轮放弃）: {exc}", "AI", force=True
        )
        needed = set()
    # 第三条判据单独一层 try：它要问 git，而上面那条只读库 —— 两边的失败面不重合，
    # 一个失败不该把另一个的结论一起丢掉。
    try:
        from services.ai.snapshot_consistency import stale_config_ids

        needed.update(stale_config_ids(ids))
    except Exception as exc:  # noqa: BLE001 —— 核对不动只是少一道闸，不该把分析卡死
        from utils.logger import log_print

        log_print(f"⚠️ 来源一致性核对失败，本次不据此拦截: {exc}", "AI")
    return sorted(needed)


def weekly_sync_in_flight(config_ids: Iterable[int], *, now: Optional[datetime] = None) -> str:
    """同步**正把缓存写在半路**吗？是的话返回一句**给人看的原因**，否则返回空串。

    判据是「算在跑」（见 `_in_flight_sync_task`）：`pending` 与 `processing` **一律拦**。
    2026-09-21 曾经把「执行侧忙」的 `pending` 放行，那条前提已被 `62b4746` 的队列/线程
    拆分推翻（分析不再占住通用线程）—— 改回去之前先读 `_in_flight_sync_task` 的 docstring。

    **「多久算太久」有两档**（2026-09-26 起）：执行者还在（租约还在续）时按
    `SYNC_IN_FLIGHT_ALIVE_MAX_SECONDS`（6 小时）兜底 —— 大仓的同步本来就是小时级，
    拿 30 分钟墙钟去砍会把**正常但慢**的那条当成卡死，放行后 AI 读的是半份缓存；
    执行者失联时才按 `SYNC_IN_FLIGHT_MAX_SECONDS`（30 分钟）放行。

    调用方拿到原因后应当**跳过这一次分析并保留触发水位线**（不要推进
    `last_triggered_at`/`last_analyzed_at`），下一个周期自然会重试。
    """
    needed = weekly_sync_needed_config_ids(config_ids)
    if needed:
        # 措辞对三种成因**都成立**：「同步之后又入库了新提交」与「缓存里的提交已被强推
        # 掉」都表现为「缓存落后于仓库当前历史」。`weekly_sync_needed_config_ids` 的
        # docstring 逐条写了这三种成因，日志里另有 `snapshot_consistency` 那条 force 行。
        return (
            f"周版本缓存落后于仓库同步（待刷新 config_id={','.join(map(str, needed))}）："
            "等最新提交写入周版本缓存后再分析"
        )
    task, age, blocking = _in_flight_sync_task(config_ids, now=now)
    if task is None or not blocking:
        return ""
    minutes = int(age // 60)
    return (
        f"周版本同步还在跑（task_id={task.id}，已 {minutes} 分钟）："
        "现在分析只能看到已经写进缓存的那部分文件，等它写完再分析"
    )


def weekly_sync_released_incomplete(config_ids: Iterable[int], *, now: Optional[datetime] = None):
    """闸门**不再拦、而那条同步还没跑完**吗？返回 `(task_id, 不再等它的原因)`；没有就 `(None, "")`。

    与 `weekly_sync_in_flight` 是互斥的两面：那条说「还在拦」，这条说「放行了 —— 但放行的
    理由是『判定它卡死』，**不是**『它跑完了』」。调用方（醒目日志、页面轮询）都要把这两件事
    分开说：少了这一条，页面会在同步没跑完时断言「已经跑完」，而下一拍就是 AI 在一份写了
    一半的缓存上出结论 —— 恰在本模块存在理由的那个场景上说反话。

    成因只有 `weekly_sync_release_cause` 产出的那一句，别在调用方再拼一遍。
    """
    task, age, blocking = _in_flight_sync_task(config_ids, now=now)
    if task is None or blocking:
        return None, ""
    current = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    minutes = int(age // 60)
    if executor_alive(task, now=current):
        cause = f"已跑 {minutes} 分钟，超过 {SYNC_IN_FLIGHT_ALIVE_MAX_SECONDS // 3600} 小时兜底上限"
    else:
        cause = f"已跑 {minutes} 分钟，执行者已失联（租约过期）"
    return task.id, cause


def weekly_sync_stuck_note(config_ids: Iterable[int], *, now: Optional[datetime] = None) -> str:
    """不再拦着分析、却**还没结束**的同步任务：给一句醒目日志用的话，没有就空串。

    放行时的代价是同一件事 —— 这一轮看到的变更清单可能不完整，所以这句话必须说清它。
    成因那句来自 `weekly_sync_released_incomplete`（口径只有那一份）。
    """
    task_id, cause = weekly_sync_released_incomplete(config_ids, now=now)
    if not cause:
        return ""
    return (
        f"⚠️ 周版本同步 task_id={task_id} {cause}，不再拦着 AI 分析"
        "（这一轮看到的变更清单可能不完整）"
    )


def group_config_ids(config) -> list:
    """一个周版本批次（同一项目、同一窗口）里的全部配置 id。

    与 `ai_analysis_service.build_weekly_payload` 的分组口径一致：闸门要拦的是
    **整批**的同步，因为变更清单来自这一批的全部仓库（只看自己那一个仓库的同步，
    另一个仓库还在写的时候照样会漏文件）。
    """
    if config is None:
        return []
    try:
        from services.ai.project_config_source import weekly_batch_configs

        rows = weekly_batch_configs(config)
    except Exception:  # noqa: BLE001
        return [getattr(config, "id", None)] if getattr(config, "id", None) else []
    ids = [getattr(row, "id", None) for row in rows]
    return [item for item in ids if item]
