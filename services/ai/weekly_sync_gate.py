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
* **有上限。** 一个卡死的同步任务不能把分析永久挡住：超过 `SYNC_IN_FLIGHT_MAX_SECONDS`
  就带着一条醒目日志放行。卡死本身有别的机制兜（`schedule_weekly_sync_tasks` 会把
  超时的 pending 任务置 failed；重启时 `load_pending_tasks` 会把 processing 改回 pending），
  这道上限是最后一道保底。
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

# 同步任务在这个时长之内算「还在跑」，超过就认为是卡死了（放行并记一条醒目日志）。
#
# 取值依据：周版本同步要逐文件算合并 diff（大仓库上千个文件），分钟级是常态；
# 而 AI 分析的触发周期是 1 分钟 —— 等一会儿没有代价，等一小时才是问题。
SYNC_IN_FLIGHT_MAX_SECONDS = 30 * 60


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
    """这个批次里**算在跑**的周版本同步任务，返回最该被说出口的那一条。

    返回 `(task, 已跑秒数)`；没有就 `(None, 0)`。

    ## 「算在跑」= `pending` 与 `processing` **一律算**（2026-09-22）

    这条判据被收窄过一次又被改回来，两次都有实测依据 —— 改动前**必须先读**
    `tests/test_ai_weekly_sync_gate_scope.py` 的模块 docstring，那里留着完整来龙去脉。
    一句话版本：2026-09-21 那次「执行侧忙就放行」的前提是**单线程 worker 被分析占满**，
    而 `62b4746` 把 `weekly_ai_analysis` 拆进独立队列 + 独立线程
    （`services/task_worker_service.py:984-987`）之后，分析**不再占住通用线程**，
    一条 `pending` 的同步**一定**会在分析期间被通用线程取走并开始写缓存。

    ## 上限的判据是「还有没有一条在上限内」，不是「最老的那条超没超」

    `SYNC_IN_FLIGHT_MAX_SECONDS` 的用途是「一条卡死的同步不能把分析永久挡住」，所以它
    **逐条**作用：一批里同时有一条卡死 40 分钟的旧同步与一条刚开跑 2 分钟的新同步时，
    后者仍可能在写缓存，必须继续拦。只有**全都在上限之外**时才返回最老的那条 ——
    那是给 `weekly_sync_stuck_note` 指名用的。
    """
    ids = [str(int(item)) for item in config_ids if item is not None]
    if not ids:
        return None, 0.0

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
        return None, 0.0
    if not rows:
        return None, 0.0

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
    within = [item for item in aged if item[0] <= SYNC_IN_FLIGHT_MAX_SECONDS]
    # 在上限内的取**最老**的那条：它最接近上限，报出来信息量最大。
    # 全超上限时取最老的那条 = 真正卡死的那条（`stuck_note` 要指名它）。
    age, worst = max(within or aged, key=lambda item: item[0])
    return worst, age


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
    task, age = _in_flight_sync_task(config_ids, now=now)
    if task is None:
        return ""
    if age > SYNC_IN_FLIGHT_MAX_SECONDS:
        return ""
    minutes = int(age // 60)
    return (
        f"周版本同步还在跑（task_id={task.id}，已 {minutes} 分钟）："
        "现在分析只能看到已经写进缓存的那部分文件，等它写完再分析"
    )


def weekly_sync_stuck_note(config_ids: Iterable[int], *, now: Optional[datetime] = None) -> str:
    """卡死的同步任务（超过上限仍在跑）：给一句醒目日志用的话，没有就空串。"""
    task, age = _in_flight_sync_task(config_ids, now=now)
    if task is None or age <= SYNC_IN_FLIGHT_MAX_SECONDS:
        return ""
    minutes = int(age // 60)
    return (
        f"⚠️ 周版本同步 task_id={task.id} 已跑 {minutes} 分钟仍未结束，"
        f"超过 {SYNC_IN_FLIGHT_MAX_SECONDS // 60} 分钟上限，不再拦着 AI 分析"
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
