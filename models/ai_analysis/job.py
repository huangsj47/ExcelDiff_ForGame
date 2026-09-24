#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析的**任务身份**（job）：一次用户动作从头到尾的那条线。

## 为什么要有它（复测文档 AI-P0-01 / AI-P0-04）

改动之前，「发起一次分析」这件事没有身份。它被拆成三样互不相认的东西：

* `ai_analysis_run` —— 真正跑模型的那条记录，**跑起来才有**；
* `background_tasks` 里 `task_type='weekly_ai_analysis'` 的一行 —— 排队中的执行体，
  `trigger_source` 只在内存载荷里；
* `background_tasks` 里 `task_type='weekly_ai_waiting'` 的一行 —— 「等同步」的意图，
  与分析任务之间**只有 `group_key` + 创建时间**这种猜出来的关联。

于是实测里出现了这几件事，全都不是偶发：

1. 用户点了按钮，服务端登记了等待意图，`EventSource` 收到 `waiting` 后关闭 ——
   浏览器补一个连接层 `error`，页面把「等待同步」覆盖成「连接中断：没有收到运行号」。
   数据库里那次点击好端端地 pending 着，**页面与真实状态相反**。
2. 手工意图登记时库里已有一条更早创建的 scheduled 任务，队列**直接复用了它**，
   不升级来源、不建立关联 —— 那次分析在账单上被记成「定时」。
3. 页面刷新、关抽屉、断线重连都没有可恢复的身份：`EventSource` 的 GET 既创建任务
   又承担进度流，重连一次就是**重复创建、重复付费**。

`AiAnalysisJob` 就是补上的那个身份：**在产生任何模型消耗之前就存在、持久、可查询**。
POST 创建它、GET 查它、SSE 只订阅它的事件流。同步没跑完也照样返回一个稳定的
`job_id`，状态是 `waiting_snapshot` —— 不再有「没有运行号所以无法确认」这一档。

## 两条独立的幂等键（不要合成一条）

* `idempotency_key` —— **客户端**给的动作标识。同一个键重复 POST 只算一次
  （「关掉抽屉再打开又点一下」不该变成第二次付费调用）。可空：调用方不给就不去重。
* `active_key` —— **服务端**算的输入指纹（`target_snapshot + effective_mode +
  provenance + focus`）。同一个 `active_key` 同时只允许一条活动 job，由唯一索引裁决。
  这与 `AiAnalysisRun.active_key` 是同一套手法、同一个理由：**先查再插永远有窗口**，
  用户连点两次（或手工与定时同时触发）恰好落在窗口里，就是两次真金白银的调用。

两者不可互相替代：前者答「同一个用户动作」，后者答「同一份输入」。

## 状态机

```text
                 ┌─ 同步在跑 ─→ waiting_snapshot ─┐
POST ─→ pending ──┼─ 已有可复用结论 ─→ reused（终态，不花钱）
                 └─ 放行 ──────→ queued ─→ running ─┬─→ succeeded
                                                    ├─→ degraded
                                                    └─→ failed
任意非终态 ─→ cancelled（用户取消 / 意图作废）
```

* `reused` 是**终态且零消耗**：没有新变化时增量模式直接复用已有结论，不建 run。
  它与 `succeeded` 必须分开 —— 靠 `succeeded` 表示它会让用量面板多一次「运行」。
* 任何非终态都必须能走到某个终态。**「等待意图永久 pending」是不允许的**：
  由 `lease_expires_at` + 启动恢复兜底（见 `services/ai/job_service.py`）。

## 与 `BackgroundTask` / `AiAnalysisRun` 的分工

* job 是**意图与身份**：用户要什么、平台答应怎么跑、现在到哪一步了；
* `BackgroundTask` 是**执行体**：worker 队列里的一行，承载租约与重试；
* `AiAnalysisRun` 是**产出**：模型调用的账与结论。

一条 job 至多有一条活动 run、至多有一条活动 task。三者靠
`job.task_id` / `job.run_id` 显式相连，不再靠时间戳猜。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import db
from ..big_text import BigText

# ---------------------------------------------------------------------------
#  状态
# ---------------------------------------------------------------------------

#: 建好了、还没决定往哪走（同步闸门/预算闸门即将裁决）。
STATE_PENDING = "pending"
#: 等 Diff 同步跑完再开始。**这是「已经收下这次点击」的状态**，页面据此显示
#: 「等待同步」并轮询，而不是报「没有运行号」。
STATE_WAITING_SNAPSHOT = "waiting_snapshot"
#: 已排进 worker 队列，还没被取走。
STATE_QUEUED = "queued"
#: 正在跑（有活动 run）。
STATE_RUNNING = "running"
#: 跑完且完整。
STATE_SUCCEEDED = "succeeded"
#: 跑完但有缺口（见 `AiAnalysisRun.degradation`）。**有可复用结论**，
#: 因此可以当下一轮的结论基线。
STATE_DEGRADED = "degraded"
STATE_FAILED = "failed"
#: 输入没变、直接复用了已有结论。**零模型消耗** —— 与 succeeded 分开记，
#: 否则用量面板会把「没花钱的那一次」算成一次运行。
STATE_REUSED = "reused"
#: 用户取消 / 等待意图被作废。
STATE_CANCELLED = "cancelled"

JOB_STATES = (
    STATE_PENDING,
    STATE_WAITING_SNAPSHOT,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_DEGRADED,
    STATE_FAILED,
    STATE_REUSED,
    STATE_CANCELLED,
)

#: 跑完了的状态。**`degraded` 在里面**：它有结论、能当基线，只是覆盖不全。
TERMINAL_JOB_STATES = (
    STATE_SUCCEEDED,
    STATE_DEGRADED,
    STATE_FAILED,
    STATE_REUSED,
    STATE_CANCELLED,
)

#: 还没跑完的状态。`waiting_snapshot` 算「活动」—— 那次点击还没被消费掉，
#: 同一个输入的第二次点击必须附着到它上面，而不是新建一条。
ACTIVE_JOB_STATES = (
    STATE_PENDING,
    STATE_WAITING_SNAPSHOT,
    STATE_QUEUED,
    STATE_RUNNING,
)

#: 已经交出可复用结论的状态（下一轮的**结论基线**从中挑，见
#: `models/ai_analysis/weekly_state.py::last_concluded_run_id`）。
CONCLUDED_JOB_STATES = (STATE_SUCCEEDED, STATE_DEGRADED)

# ---------------------------------------------------------------------------
#  执行模式
# ---------------------------------------------------------------------------

MODE_INCREMENTAL = "incremental"
MODE_FULL = "full"
ANALYSIS_MODES = (MODE_INCREMENTAL, MODE_FULL)

# ---------------------------------------------------------------------------
#  来源
# ---------------------------------------------------------------------------

SOURCE_MANUAL = "manual"
SOURCE_SCHEDULED = "scheduled"
TRIGGER_SOURCES = (SOURCE_MANUAL, SOURCE_SCHEDULED)

#: 一条 job 最长能在非终态上待多久（等待同步 + 排队 + 跑）。超过就由
#: `services/ai/job_service.py` 的恢复扫描判死 —— 这是「意图不许永久 pending」的兜底。
#: 30 分钟与 `weekly_sync_gate.SYNC_IN_FLIGHT_MAX_SECONDS` 同量级，
#: 也与等待意图原来的 TTL（`task_worker_queue_service.WAITING_INTENT_TTL_SECONDS`）一致：
#: 比同步闸门先放弃是不对的（那会把「同步慢」说成「意图失效」）。
JOB_STALE_SECONDS = 30 * 60


class AiAnalysisJob(db.Model):
    """一次用户发起的分析动作（身份 + 状态 + 血缘）。"""

    __tablename__ = "ai_analysis_job"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)

    target_type = db.Column(db.String(20), nullable=False)  # weekly / commit
    target_id = db.Column(db.Integer, nullable=True)  # config_id 或 commit_id
    target_key = db.Column(db.String(200), nullable=True)  # weekly 的 group_key

    # --- 用户要什么 vs 平台实际怎么跑 ---
    # **两者都必须落库。** 只记一个的后果是「我点的是增量，为什么跑了全量」只能靠
    # 日志回答；而平台确实会因为变更比例、关键路径、provenance 变化把增量升级成全量。
    requested_mode = db.Column(db.String(20), nullable=False, default=MODE_INCREMENTAL)
    # NULL = 还没裁决（`waiting_snapshot` / `pending` 阶段）。
    effective_mode = db.Column(db.String(20), nullable=True)
    # 平台把增量升级成全量的原因短码（`scope_sampling._decide_scope` 的第二返回值，
    # 如 `first_run` / `delta_count_high` / `critical_path_detected`）。
    # 页面拿它解释「为什么这次是全量」，不必再去翻日志。
    upgrade_reason = db.Column(db.String(60), nullable=True)

    state = db.Column(db.String(24), nullable=False, default=STATE_PENDING)
    trigger_source = db.Column(db.String(20), nullable=False, default=SOURCE_MANUAL)

    # --- 幂等（两条，见模块 docstring）---
    idempotency_key = db.Column(db.String(120), nullable=True)
    active_key = db.Column(db.String(120), nullable=True)

    # --- 血缘（P0-02 的增量基线靠这三个指针做差）---
    # 当结论基线的那条 run（最近一次有可复用结论的，可能是 degraded）
    base_run_id = db.Column(db.Integer, nullable=True)
    # 做差用的基准快照 / 本次目标快照（`ai_diff_snapshot.id`）
    base_snapshot_id = db.Column(db.Integer, nullable=True)
    target_snapshot_id = db.Column(db.Integer, nullable=True)
    # 复用的既有结论（`reused` 终态专用）：指向被复用的那条 run
    reused_run_id = db.Column(db.Integer, nullable=True)

    # --- 产出与执行体 ---
    run_id = db.Column(db.Integer, nullable=True)
    task_id = db.Column(db.Integer, nullable=True)

    # --- 分析口径的溯源（与 `AiAnalysisRun` 上那几个同义，冗余在这里是为了
    #     **在 run 还没建出来时**就能判断增量是否与基线可比）---
    prompt_version = db.Column(db.String(80), nullable=True)
    skill_version = db.Column(db.String(80), nullable=True)
    rules_version = db.Column(db.String(80), nullable=True)
    analysis_revision = db.Column(db.String(80), nullable=True)
    model = db.Column(db.String(200), nullable=True)
    # 基线与本轮的 provenance 不一致（用户选择「继续增量」时置真）。
    # 报告顶部据此写明「这次增量与基线不可比」，不许悄悄按可比处理。
    baseline_provenance_mismatch = db.Column(db.Boolean, nullable=True)

    # 分析范围（focus）也必须进幂等键：同一快照下「只看配表」与「只看代码」
    # 是两份不同的输入。
    focus = db.Column(db.String(120), nullable=True)

    # 这次输入里有多少文件 / 预计消耗（创建时就写下来，`/jobs/<id>` 与确认框读它）。
    # `planned_files` 是增量集合的大小，`planned_tokens_low/high` 是区间估算
    # （P1-03：把预计成本放到执行前）。
    planned_files = db.Column(db.Integer, nullable=True)
    planned_tokens_low = db.Column(db.Integer, nullable=True)
    planned_tokens_high = db.Column(db.Integer, nullable=True)
    # 三列 `planned_*` **取不到值时写在这里的文字**（P2-2）。NULL 本身说不出「为什么没有」
    # ——界面上一个空格子既可以读成「没估」，也可以读成「估出来是 0」，而这两件事对
    # 「这次要不要跑」的判断正好相反。写不了估算时就把原因留在这里，一个字都不许省。
    planned_estimate_note = db.Column(BigText, nullable=True)

    # --- 租约 ---
    # worker 取走时写，跑完清空。过期即可被恢复扫描判死或放回队列 ——
    # 这就是「一小时前的任务不能无限占着位子」的判据。
    lease_owner = db.Column(db.String(80), nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)

    error_message = db.Column(BigText, nullable=True)
    # 创建时的输入摘要（给人看的：变更文件数、窗口总数）。BigText 与
    # `AiAnalysisRun.delta_summary` 同一用法。
    delta_summary = db.Column(BigText, nullable=True)
    # 每轮结束时持久化一份显示用进度。它不是最终用量账，只用于跨进程/重启后继续展示。
    progress_json = db.Column(BigText, nullable=True)
    progress_updated_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("Project", backref="ai_analysis_jobs")

    __table_args__ = (
        # 按目标找活动 job（「这个周版本现在有没有一次分析在跑」）与历史列表。
        db.Index("idx_ai_job_target_created", "target_type", "target_key", "created_at"),
        # 恢复扫描：WHERE state IN (非终态) AND lease_expires_at < now
        db.Index("idx_ai_job_state_lease", "state", "lease_expires_at"),
        # 输入指纹唯一（活动 job 唯一性）。与 `uq_ai_run_active_key` 同一手法：
        # **可空 + UNIQUE**，SQLite 与 MySQL 语义一致（多行 NULL 共存、一行有值）。
        # 跑完就清空，所以 NULL 有两种含义：已结束，或这是一条不需要唯一性的老行。
        db.Index("uq_ai_job_active_key", "active_key", unique=True),
        # 客户端幂等键唯一。同样是可空 + UNIQUE —— 调用方不给键时不去重。
        db.Index("uq_ai_job_idempotency_key", "idempotency_key", unique=True),
    )

    # ------------------------------------------------------------------
    #  派生属性
    # ------------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_JOB_STATES

    @property
    def has_concluded(self) -> bool:
        """有没有交出可复用的结论（能当下一轮的结论基线）。"""
        return self.state in CONCLUDED_JOB_STATES

    @property
    def is_stale(self) -> bool:
        """非终态上待太久了。

        与 `AiAnalysisRun.is_stale_running` 同一个判据思路：**判断放在读取侧**，
        幂等、不依赖任何后台组件是否还活着。租约过期是「执行者死了」，
        这里还额外兜住「连租约都没写上就卡住了」那一档（老行 / 建完就崩）。
        """
        if self.is_terminal:
            return False
        if self.lease_expires_at is not None:
            return _as_naive_utc(self.lease_expires_at) < _utcnow()
        reference = self.started_at or self.created_at
        if reference is None:
            return False
        return _as_naive_utc(reference) + timedelta(seconds=JOB_STALE_SECONDS) < _utcnow()

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "project_id": self.project_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_key": self.target_key,
            "state": self.state,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "upgrade_reason": self.upgrade_reason or "",
            "trigger_source": self.trigger_source,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "reused_run_id": self.reused_run_id,
            "base_run_id": self.base_run_id,
            "base_snapshot_id": self.base_snapshot_id,
            "target_snapshot_id": self.target_snapshot_id,
            "baseline_provenance_mismatch": bool(self.baseline_provenance_mismatch),
            "focus": self.focus or "",
            "planned_files": self.planned_files,
            "planned_tokens_low": self.planned_tokens_low,
            "planned_tokens_high": self.planned_tokens_high,
            "planned_estimate_note": self.planned_estimate_note or "",
            "error_message": self.error_message or "",
            "delta_summary": self.delta_summary or "",
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }

    def __repr__(self) -> str:
        return (
            f"<AiAnalysisJob {self.id} {self.target_type}:"
            f"{self.target_id or self.target_key} {self.state}>"
        )


# ---------------------------------------------------------------------------
#  时间小工具（与 `models/ai_analysis/analysis_run.py` 同一口径）
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """naive UTC。库里存的就是 naive（`DateTime` 无时区），比较前必须统一。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    """把带时区的值折成 naive UTC（SQLite 读回来的行可能是带时区的）。

    **不能直接与 `_utcnow()` 相减**：一个有 tzinfo 一个没有会抛 `TypeError`，
    而放在 `is_stale` 这种被轮询频繁调用的属性里，异常会被上层宽 `except` 吞成
    「不 stale」—— 那正好是「任务永远卡在 running」这个缺陷本身。
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
