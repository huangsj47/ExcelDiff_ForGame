#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`AiAnalysisJob` 的**唯一写入口**（复测文档 AI-P0-01）。

## 这个模块解决什么

改动之前，「发起一次分析」没有身份：`EventSource` 的 GET 既建任务又承担进度流。
于是**一次刷新、一次断线重连、一次「关掉抽屉再点一下」都可能是第二次真金白银的调用**，
而「同步中点击」那一档连可恢复的凭据都没有 —— 页面只能报「没有运行号所以无法确认」。

现在的分工是一条直线：

    POST /ai-analysis/weekly/<id>/jobs   → 本模块建「身份」（不跑分析、不发模型请求）
    GET  /ai-analysis/jobs/<id>          → 读身份
    GET  /ai-analysis/jobs/<id>/events   → **只订阅**身份的事件流

执行仍然由 worker 负责（`task_worker_*` 里的 `weekly_ai_analysis` 任务），本模块只把
「这次点击」落成一行、排进队列或登记成等待意图。**HTTP 层因此不再需要跑分析**。

## 两条幂等键（细节见 `models/ai_analysis/job.py` 的模块 docstring）

* `idempotency_key`：**客户端**给的动作标识。同一个键重复 POST 只算一次 ——
  「关掉抽屉再打开又点一下」不该变成第二次付费调用。页面在内存里生成，刷新即新动作。
* `active_key`：**服务端**算的输入指纹（目标身份 + `focus`）。同一个 `active_key`
  同时只允许一条活动 job，由**唯一索引**裁决。

**为什么 `active_key` 里没有 `requested_mode`**：同一个目标同时只许有一次分析在跑。
「先点增量、再点全量」是**同一个动作被加强了**（升级 `requested_mode` 就够），
按模式分成两条 job 的后果是两份输入各排一次队、各跑一遍 —— 那是两次付费。

**为什么裁决靠唯一索引而不是「先查再插」**：查与插之间永远有窗口，而用户连点两次
（或手工与定时同时触发）恰好落在那条窗口里，就是两次调用。所以插入撞
`IntegrityError` 时**回滚后回头查那一条**（`_resolve_insert_conflict`），返回它。

## 状态与排程

建行时按同步闸门（`weekly_sync_gate.weekly_sync_in_flight`）裁决：

* 同步正把缓存写在半路 → `waiting_snapshot`，并登记等待意图（同步收尾由 worker 转交）；
* 否则 → `queued`，并直接排一条 `weekly_ai_analysis` 任务。

两种都返回**稳定的 `job_id`** —— 这就是「不再有『没有运行号所以无法确认』那一档」。

## 副作用与事务口径

* 用 `db.session`，**写、flush、由调用方提交**（与既有代码同一手法）；只有需要 id 的
  地方才 `flush()`，`IntegrityError` 分支是唯一的例外（必须自己 `rollback()`）。
* **本模块会创建 `ai_weekly_analysis_state` 行**（经由既有的取用函数
  `services.ai.weekly_state.get_or_create_weekly_state`）。那是 status/session 里既有的
  建行逻辑，幂等且带唯一约束兜底；不用它就得自己 query，而「先查再插」在这里同样有窗口。
* **本模块不发起任何模型调用。** 排队与登记都不建 `AiAnalysisRun`（见
  `task_worker_queue_service.register_waiting_analysis_intent` 的头三条口径）。

### 两处跨文件缺口（第二波收尾时已落地，见各自的实现）

* `create_weekly_ai_analysis_task` 收 `requested_mode` 并落库，执行侧
  （`services/ai_analysis_service.run_weekly_analysis_background`）**原先只读
  `trigger_source`** —— 「用户点了全量」这一跳在 worker 那条路上没落地。现在那个入口
  有 `requested_mode` / `focus` 两个形参了（`requested_mode="full"` → 强制全量）；
* `focus` 的来源是**本模块的 `focus_for_task`**：job 行上有 `focus`、任务行上有
  `job_id`，所以执行侧只要拿到 `task_id` 就能读回来 —— **不需要**改
  `create_weekly_ai_analysis_task` / `register_waiting_analysis_intent` 的签名。

### 没有 run 的结局也必须结清（`settle_without_run`）

`settle_from_run` 要一条 run 行。而「开关关着 / 预算不足 / 同步闸门拦下 / 没有变化直接
复用」这几个结局在 worker 里直接结束、**不带 run 回来** —— 没有出口的话那条 job 永远停在
非终态，`active_key` 也从没被清空，而它是**唯一索引**：同一个 target + focus 从此再也
建不出任何 job。出口是 `settle_without_run`（按 `reason` 选终态），外加一条启动恢复扫描
`recover_stale_jobs` 兜住「连那条路都没走到」的形态。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.exc import IntegrityError

from models import BackgroundTask, db
from models.ai_analysis import (
    ACTIVE_JOB_STATES,
    ANALYSIS_MODES,
    JOB_STALE_SECONDS,
    MODE_FULL,
    MODE_INCREMENTAL,
    PRE_RUN_JOB_STATES,
    SOURCE_MANUAL,
    STATE_CANCELLED,
    STATE_DEGRADED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_REUSED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_WAITING_SNAPSHOT,
    TERMINAL_JOB_STATES,
    TRIGGER_SOURCES,
    AiAnalysisJob,
)
from services.ai import project_gate

# 目标身份的字面值。与 `AiAnalysisRun.target_type` / `BackgroundTask.task_type`
# 同一套词，**不另立一套**：三张表说的是同一次分析。
TARGET_TYPE_WEEKLY = "weekly"

#: 不带范围的默认值（`focus=all`）。既有代码里同一个词写了三遍（`_filter_delta_files_by_focus`
#: / `FOCUS_ALL` / 模板的 select 默认项），这里只作为**本模块的入参归一化**使用。
FOCUS_ALL = "all"

# 列宽（`AiAnalysisJob.idempotency_key` / `focus` / `active_key` 都是 String(120)）。
# **超长一律拒绝，不截断**：截断会让两个不同的键映射到同一行，而那正是「一次点击两次
# 调用」的形态（用户看到的是「我点了两次，只跑了一次」或反过来）。
MAX_IDEMPOTENCY_KEY_LENGTH = 120
MAX_FOCUS_LENGTH = 120
MAX_ACTIVE_KEY_LENGTH = 120

#: 平台把增量升成全量的原因短码之一：**没有可复用的结论基线**（首次运行）。
#: 取值与 `services/ai/scope_sampling._decide_scope` 的第二返回值同一套词。
UPGRADE_REASON_FIRST_RUN = "first_run"

# `AiAnalysisRun.status` 的取值 → job 终态。**不猜值**：`succeeded` / `degraded` /
# `failed` 就是 `services/ai/engine.py` 的三个 `STATUS_*`，由 `_persist_outcome`
# 原生写进 `run.status`（模型那一列没有单独的 `engine_status`）。
_RUN_STATUS_TO_JOB_STATE = {
    STATE_SUCCEEDED: STATE_SUCCEEDED,
    STATE_DEGRADED: STATE_DEGRADED,
    STATE_FAILED: STATE_FAILED,
}

#: `run.status` 里**没有结论**的那两个值（还在跑 / 刚建好）。它们不构成终态，
#: `settle_from_run` 见到它们一律不动 job。
_RUN_STATUS_OPEN = ("pending", "running")


# ---------------------------------------------------------------------------
#  「没有 run 的结局」的理由短码
#
#  **这几个字面值与 `services/ai_analysis_service.run_weekly_analysis_background`
#  返回的 `result["reason"]` 是同一套词**（它就是那句 `reason` 的唯一产地，本模块
#  只按它映射终态）。不猜键名：那份字典的键就是 `status` / `reason` / `message` /
#  `run_id` / `error_message`，没有 run 的结局一定带 `status="skipped"` + `reason`。
# ---------------------------------------------------------------------------

REASON_NO_CHANGE = "no_change"
REASON_AUTO_WEEKLY_DISABLED = "auto_weekly_disabled"
REASON_OVER_BUDGET = "over_budget"
REASON_SYNC_IN_FLIGHT = "sync_in_flight"
REASON_ALREADY_RUNNING = "already_running"
REASON_MISSING_API_KEY = "missing_api_key"
REASON_FOCUS_EMPTY = "focus_empty"
REASON_PAYLOAD_EMPTY = "payload_empty"
REASON_NO_CONFIGS = "no_configs"
#: 恢复扫描专用：产出（run）已经落到终态，只是收口那一跳没走到（判据 ①）。
#: **它不走 `WITHOUT_RUN_REASON_STATES`** —— 那条路上状态由 `settle_from_run` 按
#: `run.status` 自己映射（`degraded` 不许被折叠），这个短码只出现在 `by_reason` 里。
REASON_RUN_ALREADY_TERMINAL = "run_already_terminal"
#: 恢复扫描专用：任务行已经结束，却没留下任何运行记录（判据 ②）。
REASON_TASK_ENDED_WITHOUT_RUN = "task_ended_without_run"
#: 恢复扫描专用：没有任何关联任务/run，且早已过了 `JOB_STALE_SECONDS`（判据 ③）。
REASON_ABANDONED = "abandoned"
#: 执行侧**抛异常**结束（收尾那一段炸了：载荷构造 / 推进水位线 / 落库）。
#: run 可能已经建出来、甚至已经落成 `succeeded`，但这次交付**没有完成** ——
#: 任务行同样被标成 failed（`task_worker_task_handlers` 既有的那一支），两者口径一致。
REASON_INTERRUPTED = "interrupted"

#: 理由短码 → job 终态。**语义要诚实**（见模型 docstring 的 `STATE_REUSED` /
#: `STATE_CANCELLED`）：
#:
#: * `no_change` → `reused`：**有结论、零消耗**。塞进 `failed` 会让用量面板把
#:   「没花钱的那一次」算成一次失败运行，两件事在面板上是分开的两栏；
#: * 开关关着 / 预算拦下 / 被闸门拦下 / 已有活动运行 → `cancelled`：这次动作
#:   **没有被执行**，而且它不是错误（用户该做的是改配置或稍后再点，不是去查故障）；
#: * 其余（缺接口密钥、载荷建不出来、范围筛空了、认不出来的短码）→ `failed`。
#:
#: **不在表里的一律 `failed`**：认不出来的理由默认「失败」，不许静默看起来像成功。
WITHOUT_RUN_REASON_STATES = {
    REASON_NO_CHANGE: STATE_REUSED,
    REASON_AUTO_WEEKLY_DISABLED: STATE_CANCELLED,
    REASON_OVER_BUDGET: STATE_CANCELLED,
    REASON_SYNC_IN_FLIGHT: STATE_CANCELLED,
    REASON_ALREADY_RUNNING: STATE_CANCELLED,
    REASON_MISSING_API_KEY: STATE_FAILED,
    REASON_FOCUS_EMPTY: STATE_FAILED,
    REASON_PAYLOAD_EMPTY: STATE_FAILED,
    REASON_NO_CONFIGS: STATE_FAILED,
    REASON_TASK_ENDED_WITHOUT_RUN: STATE_FAILED,
    REASON_ABANDONED: STATE_FAILED,
    REASON_INTERRUPTED: STATE_FAILED,
}

#: 每个理由给一句人话（写进 `error_message`；**它是本行唯一可写的说明字段**，
#: 终态本身由 `state` 表达，两件事不要混起来读）。
WITHOUT_RUN_REASON_MESSAGES = {
    REASON_NO_CHANGE: "输入与上一轮逐字相同，直接复用了已有结论 —— 本次零模型消耗。",
    REASON_AUTO_WEEKLY_DISABLED: (
        "该项目的「周版本自动分析」开关已关闭，这次分析没有被执行（零消耗）。"
        "用户点出来的分析不受这个开关管 —— 需要的话在抽屉里手动发起一次。"
    ),
    REASON_OVER_BUDGET: "项目 AI 用量已达预算上限，这次分析被拦下（零消耗）。",
    REASON_SYNC_IN_FLIGHT: "Diff 同步正在写缓存，这次分析被推迟（零消耗）。",
    REASON_ALREADY_RUNNING: (
        "同一份输入已经有一条分析在进行中，这次没有重复发起（零消耗）。"
    ),
    REASON_MISSING_API_KEY: "项目没有配置 AI 接口密钥，这次分析跑不起来。",
    REASON_FOCUS_EMPTY: "选定的分析范围里没有改动过的文件，换一个范围再试。",
    REASON_PAYLOAD_EMPTY: "这次分析的输入没能建出来（周版本配置缺失或窗口内没有内容）。",
    REASON_NO_CONFIGS: "这个周版本分组里一条配置都没有。",
    REASON_TASK_ENDED_WITHOUT_RUN: (
        "这次任务的执行体已经结束，却没有留下任何运行记录 —— 无法确认结论，按失败收口。"
    ),
    REASON_ABANDONED: (
        "这次分析既没有执行体、也没有产出，且早已超过最长等待时间"
        "（进程被杀或排程失败），按失败收口 —— 可以重新发起。"
    ),
    REASON_INTERRUPTED: "这次分析在收尾阶段中断，按失败收口 —— 可以重新发起。",
}


class JobRequestError(ValueError):
    """请求本身不合法（模式认不出来、键超长、幂等键被别的目标占用）。

    单独一个类型是为了让路由层**只**接这一种异常回 400：接 `ValueError` 会把
    「我们自己代码里的一个 bug」也变成一句「你的参数不对」。
    """


class ProjectBusyError(JobRequestError):
    """同一项目下已经有分析在跑。

    **继承 `JobRequestError` 是为了「同一条出口」**（路由按本类型统一回结构化错误），
    但它是**另一种语义**：请求本身没毛病，等一会儿再来就行。所以路由那侧把它接在
    父类**之前**、回 409 而不是 400 —— 前端据此把「等它跑完」与「你的参数错了」
    分成两种提示。`reason` 就是这个区分用的机器可读值。
    """

    reason = "project_analysis_running"


# ---------------------------------------------------------------------------
#  入参归一化
# ---------------------------------------------------------------------------


def normalize_mode(value) -> str:
    """`incremental` / `full`。认不出来就**报错**，不静默当增量。

    静默回落的后果是用户点了「全量」却拿到一份增量结论 —— 而他不会知道，
    因为界面上写着「全量」。这与「把清单当全量」是同一类静默错误。
    """
    mode = str(value or "").strip().lower()
    if mode not in ANALYSIS_MODES:
        raise JobRequestError(
            f"analysis_mode 只能是 {' / '.join(ANALYSIS_MODES)}，收到 {value!r}。"
        )
    return mode


def normalize_focus(value) -> str:
    """分析范围。空值 → `all`；非字符串 / 超长 → 报错（见列宽那一段）。"""
    if value is None:
        return FOCUS_ALL
    if not isinstance(value, str):
        raise JobRequestError("focus 必须是字符串。")
    focus = value.strip()
    if not focus:
        return FOCUS_ALL
    if len(focus) > MAX_FOCUS_LENGTH:
        raise JobRequestError(f"focus 过长（上限 {MAX_FOCUS_LENGTH} 字符）。")
    return focus


def normalize_source(value) -> str:
    """`manual` / `scheduled`。认不出来回落 `manual`。

    与 `analysis_mode` 不同，这个值的**后果只是账单上的一栏**（不会改变这次分析跑了
    什么），而调用方（页面）不传它时想要的就是「我点的」—— 所以回落而不是报错。
    """
    source = str(value or "").strip().lower()
    return source if source in TRIGGER_SOURCES else SOURCE_MANUAL


def normalize_idempotency_key(value) -> Optional[str]:
    """客户端幂等键。空 → `None`（不去重）；超长 → 报错（见列宽那一段）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise JobRequestError("idempotency_key 必须是字符串。")
    key = value.strip()
    if not key:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise JobRequestError(
            f"idempotency_key 过长（上限 {MAX_IDEMPOTENCY_KEY_LENGTH} 字符）。"
        )
    return key


def active_key_for(*, target_type: str, target_key: str, focus: str) -> str:
    """活动 job 的输入指纹（**服务端算**，不含 `requested_mode`）。

    与 `AiAnalysisRun.active_key` 同一套手法、同一个理由：**唯一索引裁决**。
    散列取 32 位十六进制（远小于列宽 120），装的是「同一份输入」这件事。

    P0-02 的快照指针落地后，`target_key` 会被换成目标快照身份（内容身份，不是
    `updated_at`）—— 到那时同一个 `group_key` 下内容不同的两份输入才会各得其所。
    """
    raw = f"{target_type}|{target_key or ''}|{focus}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:MAX_ACTIVE_KEY_LENGTH]


# ---------------------------------------------------------------------------
#  返回值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobResult:
    """`create_or_attach_job` 的结果：**这次是新建还是附着**，以及那条 job。"""

    job: AiAnalysisJob
    #: True = 这次新建了一条；False = 附着到了已有的那条上（含幂等键命中的情形）。
    created: bool
    #: 这次附着把 `requested_mode` 从增量**升级**成了全量（用户后点的那个意图更强）。
    upgraded: bool = False

    @property
    def job_id(self) -> Optional[int]:
        return getattr(self.job, "id", None)

    @property
    def attached(self) -> bool:
        return not self.created

    def to_dict(self) -> dict:
        """HTTP 层的响应体（`success` 由路由加）。字段名就是复测文档列的那几个。"""
        job = self.job
        return {
            "job_id": self.job_id,
            "state": job.state,
            "run_id": job.run_id,
            "requested_mode": job.requested_mode,
            "effective_mode": job.effective_mode,
            "upgrade_reason": job.upgrade_reason or "",
            "reused_run_id": job.reused_run_id,
            "attached": self.attached,
            "job": job.to_dict(),
        }


# ---------------------------------------------------------------------------
#  读
# ---------------------------------------------------------------------------


def get_job(job_id) -> Optional[AiAnalysisJob]:
    """按 id 取一条 job。取不到（不存在 / id 是脏值）返回 `None`，**不抛异常**。

    `/jobs/<id>` 是一条会被页面轮询的只读端点，脏 id 的正确回答是 404，不是 500。
    """
    try:
        key = int(job_id)
    except (TypeError, ValueError):
        return None
    if key <= 0:
        return None
    return db.session.get(AiAnalysisJob, key)


def _find_by_idempotency_key(key: Optional[str], *, target_key: Optional[str] = None):
    if not key:
        return None
    query = AiAnalysisJob.query.filter(AiAnalysisJob.idempotency_key == key)
    if target_key is not None:
        # 键命中的那条**必须属于同一个目标**才算「同一次动作」：一个拼错目标的客户端
        # 不该把别人那次分析的 job 领走（返回一条别的周版本的结论比 400 危险得多）。
        query = query.filter(AiAnalysisJob.target_key == target_key)
    return query.first()


def _find_by_active_key(active_key: str):
    """这个输入指纹现在那条活动 job（终态的 job 已经把 `active_key` 清成 NULL）。"""
    return (
        AiAnalysisJob.query.filter(AiAnalysisJob.active_key == active_key)
        .order_by(AiAnalysisJob.id.desc())
        .first()
    )


def _job_of_run(run) -> Optional[AiAnalysisJob]:
    """这条 run 服务于哪条 job。

    **先按 `job.run_id` 找**（`mark_running` 写下的显式关联，唯一可靠的判据），
    找不到才退回「这个目标还活动着的那条」—— 退回那一支是有代价的（同一目标下
    理论上可能有两条不同 focus 的活动 job），所以它只在前者不可用时才走，
    并且**只在它还没指向别的 run 时**采纳。
    """
    run_id = getattr(run, "id", None)
    if run_id is None:
        return None
    row = (
        AiAnalysisJob.query.filter(AiAnalysisJob.run_id == run_id)
        .order_by(AiAnalysisJob.id.desc())
        .first()
    )
    if row is not None:
        return row
    target_key = getattr(run, "target_key", None)
    if not target_key:
        return None
    candidates = (
        AiAnalysisJob.query.filter(
            AiAnalysisJob.target_type == getattr(run, "target_type", None),
            AiAnalysisJob.target_key == target_key,
            AiAnalysisJob.state.in_(ACTIVE_JOB_STATES),
        )
        .order_by(AiAnalysisJob.id.desc())
        .all()
    )
    for candidate in candidates:
        if getattr(candidate, "run_id", None) in (None, run_id):
            return candidate
    return None


# ---------------------------------------------------------------------------
#  建 / 附着
# ---------------------------------------------------------------------------


def create_or_attach_job(
    *,
    config,
    requested_mode,
    focus=FOCUS_ALL,
    trigger_source=SOURCE_MANUAL,
    idempotency_key=None,
    now=None,
) -> JobResult:
    """把「用户点了分析」这件事落成一行 job（或附着到已有的那条上）。

    裁决顺序（**顺序本身是契约**）：

    1. 给了 `idempotency_key` → 先按键查。命中且目标一致 → 直接返回它（`created=False`）；
    2. 否则算 `active_key` 并查活动 job。命中 → **附着**；新的 `requested_mode` 更强
       （`full` > `incremental`）时把它升级成 `full` 并落库；
    3. 都没有 → 建一条新的。

    第 1、2 步都可能落在「另一个并发请求刚插进去」的窗口里，所以第 3 步的插入撞唯一
    索引时**回滚后回头查**（`_resolve_insert_conflict`），而不是把异常抛给用户。
    """
    if config is None:
        raise JobRequestError("周版本配置不存在。")

    from services.ai.project_config_source import build_weekly_group_key

    project_id = getattr(config, "project_id", None)
    target_key = build_weekly_group_key(config)
    mode = normalize_mode(requested_mode)
    scope = normalize_focus(focus)
    source = normalize_source(trigger_source)
    key = normalize_idempotency_key(idempotency_key)
    active_key = active_key_for(
        target_type=TARGET_TYPE_WEEKLY, target_key=target_key, focus=scope
    )

    # ---- 第 1 步：客户端幂等键 ----
    if key:
        hit = _find_by_idempotency_key(key)
        if hit is not None:
            if str(getattr(hit, "target_key", "") or "") != str(target_key):
                raise JobRequestError(
                    "这个 idempotency_key 已经用在另一个目标上了：同一次动作只能指向"
                    "一个目标（请换一个键，或者去 POST /jobs 建一次新的动作）。"
                )
            return _attach(hit, mode)

    # ---- 第 2 步：输入指纹 ----
    existing = _find_by_active_key(active_key)
    if existing is not None and existing.state in ACTIVE_JOB_STATES:
        return _attach(existing, mode)

    # ---- 第 2.5 步：项目级互斥 ----
    # 走到这里说明第 2 步没找到可附着的 job —— 这确实是一次**新的**分析。第 1、2 步都是
    # target 级的：同目标重复点会附着（好行为），但同一项目下两个不同目标能同时跑，
    # 而一次分析约 3.4 元。
    #
    # **要排除「这个请求所针对的那一个目标」自己**（见 project_gate 的 docstring）：
    # 同一个目标在处理中，结论是「附着 / skipped-already_running」，由既有那两层闸门
    # 裁决；在这里拦住会把那套正确行为改坏。这道闸门管的是**别的目标**。
    busy = project_gate.describe_active_analysis(
        project_id,
        exclude_active_key=active_key,
        exclude_target=(TARGET_TYPE_WEEKLY, getattr(config, "id", None)),
    )
    if busy is not None:
        raise ProjectBusyError(project_gate.project_busy_message(busy))

    # ---- 第 3 步：新建 ----
    state_row = _weekly_state(config, target_key)
    effective_mode, upgrade_reason = _decide_effective_mode(mode, state_row)
    created_at = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    job = AiAnalysisJob(
        project_id=project_id,
        target_type=TARGET_TYPE_WEEKLY,
        target_id=getattr(config, "id", None),
        target_key=target_key,
        requested_mode=mode,
        effective_mode=effective_mode,
        upgrade_reason=upgrade_reason or None,
        state=STATE_QUEUED,
        trigger_source=source,
        idempotency_key=key,
        active_key=active_key,
        base_run_id=getattr(state_row, "last_concluded_run_id", None),
        base_snapshot_id=getattr(state_row, "last_complete_snapshot_id", None),
        focus=scope,
        created_at=created_at,
    )
    db.session.add(job)
    try:
        db.session.flush()
    except IntegrityError:
        # **唯一索引裁决的那条路**：另一个并发请求（或另一个标签页）刚刚插进去了。
        db.session.rollback()
        raced = _resolve_insert_conflict(key=key, active_key=active_key)
        if raced is None:
            # 不是「并发同键」，而是别的完整性错误 —— 照旧抛出去，别把它咽掉。
            raise
        return _attach(raced, mode)

    _dispatch(job, config, target_key, mode=mode, source=source, key=key, now=now)
    db.session.flush()
    return JobResult(job=job, created=True)


def _resolve_insert_conflict(*, key: Optional[str], active_key: str):
    """插入撞唯一索引之后，回头把那一条**找回来**。

    两条索引各自对应一种并发：`uq_ai_job_idempotency_key`（同一个用户动作）与
    `uq_ai_job_active_key`（同一份输入）。先按键找（更具体），再按指纹找。
    """
    hit = _find_by_idempotency_key(key) if key else None
    if hit is not None:
        return hit
    return _find_by_active_key(active_key)


def _attach(job, mode: str) -> JobResult:
    """把这次请求附着到已有 job 上（必要时**升级模式**）。

    升级只往强的方向走：`full` 覆盖 `incremental`，反向永远不降 —— 用户说过「我要
    全量」，后面一次「增量」的点击不该把它降回去（同一份输入，全量已经包含增量）。
    """
    upgraded = False
    current = str(getattr(job, "requested_mode", "") or "").strip().lower()
    if mode == MODE_FULL and current != MODE_FULL and not _is_terminal(job):
        job.requested_mode = MODE_FULL
        # `effective_mode` 是「平台实际怎么跑」：用户要求全量时它只能也是全量
        # （`full` 是上限，没有比它更强的）。还没裁决（NULL）的那一档**保持 NULL** ——
        # 那是 `waiting_snapshot` / `pending` 的既定语义（见模型那一列的注释）。
        if job.effective_mode:
            job.effective_mode = MODE_FULL
        upgraded = True
        db.session.flush()
    return JobResult(job=job, created=False, upgraded=upgraded)


def _is_terminal(job) -> bool:
    """这条 job 跑完了没有。**读属性优先**（模型上是 `is_terminal`），取不动就自己判。"""
    flag = getattr(job, "is_terminal", None)
    if isinstance(flag, bool):
        return flag
    from models.ai_analysis import TERMINAL_JOB_STATES

    return str(getattr(job, "state", "") or "") in TERMINAL_JOB_STATES


def _weekly_state(config, target_key):
    """这个分组的「水位线状态行」（没有就建一行）。**用既有的取用函数，不自己 query。**

    建行是幂等的、由 `group_key` 上的唯一约束兜底；它的 `commit()` 发生在
    **本模块还没有任何待写内容**的时候（在 job 行 `add` 之前调用），所以不会把
    半条 job 提前落库。
    """
    from services.ai.weekly_state import get_or_create_weekly_state

    return get_or_create_weekly_state(
        project_id=getattr(config, "project_id", None),
        group_key=target_key,
        base_name=str(getattr(config, "name", "") or ""),
        start_time=getattr(config, "start_time", None),
        end_time=getattr(config, "end_time", None),
    )


def _decide_effective_mode(requested_mode: str, state_row):
    """`requested_mode` → `(effective_mode, upgrade_reason)`。

    1. 要的就是全量 → 全量，没有「升级原因」（那本来就是用户要的）；
    2. 要的是增量、但**没有可复用的结论基线**（`last_concluded_run_id` 为 NULL）→
       平台升成全量，`upgrade_reason="first_run"`。**这一条必须落库**：否则
       「我点的是增量，为什么跑了全量」只能靠日志回答；
    3. 其余 → 增量，没有升级原因。
    """
    if requested_mode == MODE_FULL:
        return MODE_FULL, ""
    concluded = getattr(state_row, "last_concluded_run_id", None)
    if concluded is None:
        return MODE_FULL, UPGRADE_REASON_FIRST_RUN
    return MODE_INCREMENTAL, ""


def _dispatch(job, config, target_key, *, mode, source, key, now=None) -> None:
    """把这条 job 排进「等同步」或「排队」的那条路，并写下执行体/意图的关联。

    **两道闸门在这里裁决**（`weekly_sync_gate`）：同步正把缓存写在半路时，排队等于
    让分析看到半份变更清单（漏文件是**静默**的，提示词里的「共 N 个文件」会跟着变小）。
    拦下时登记一条**等待意图**（不执行、不产生任何消耗），同步收尾由 worker 转交。
    """
    from services.ai.weekly_sync_gate import (
        group_config_ids,
        weekly_sync_in_flight,
        weekly_sync_needed_config_ids,
    )

    config_id = getattr(config, "id", None)
    # **判据是整批**（同一个项目 / 同一个窗口里的全部仓库），不是这一个 config：
    # 变更清单来自这一批全部的缓存行，另一个仓库还在写的时候照样会漏文件。
    # 这与 `run_weekly_analysis_background`（后台/任务那条路唯一的执行入口）用的是
    # 同一个 `group_config_ids(config)` —— 闸门只有一份实现，判定也跟着只有一处。
    config_ids = group_config_ids(config)
    # 手动点击发生在 auto_sync 刚拉到新提交之后时，不能只说「缓存旧了」然后永久等待；
    # 立即补齐整批 weekly_sync，完成回调会把下面登记的等待意图转交给分析 worker。
    needed = weekly_sync_needed_config_ids(config_ids)
    if needed:
        from services.task_worker_service import create_weekly_sync_task

        for needed_id in needed:
            create_weekly_sync_task(needed_id)
    sync_reason = weekly_sync_in_flight(config_ids, now=now)
    if sync_reason:
        job.state = STATE_WAITING_SNAPSHOT
        _register_intent(job, config_id, target_key, mode=mode, key=key)
        return

    job.state = STATE_QUEUED
    task_id = _create_analysis_task(
        job, config_id, target_key, mode=mode, source=source, key=key
    )
    if task_id:
        job.task_id = task_id


def _register_intent(job, config_id, target_key, *, mode, key) -> None:
    """登记「这次分析在等同步跑完」，并把这条 job 的关联写回意图行。

    **不许产生任何模型调用**：登记只落一行 `weekly_ai_waiting`（见
    `task_worker_queue_service.register_waiting_analysis_intent` 的 docstring）。
    """
    from services.task_worker_queue_service import register_waiting_analysis_intent

    intent_id = register_waiting_analysis_intent(
        config_id, target_key, requested_mode=mode, idempotency_key=key
    )
    if not intent_id:
        # 登记失败（库写不进去）：**如实留一条**，页面轮询会发现这次点击没有出路。
        # 不抛异常 —— job 行本身是有效的身份，抛出去会让用户连 job_id 都拿不到。
        _log(f"⚠️ 等待同步的分析意图登记失败: job={job.id}, config_id={config_id}")
        return
    row = db.session.get(BackgroundTask, intent_id)
    if row is None:
        return
    if getattr(row, "job_id", None) in (None, job.id):
        # **这一笔写在我们名下**：意图行指回它服务的 job（`job_id` 的冻结语义是
        # 「服务于哪个 ai_analysis_job」）。已经有值且不是我们时不覆盖 ——
        # 那条意图服务的是更早的一次点击，抢过来会让两条 job 的关系对不上。
        row.job_id = job.id
        db.session.flush()
    else:
        _log(
            f"⚠️ 这条等待意图 #{intent_id} 已经关联到别的 job（{row.job_id}），"
            f"job={job.id} 不覆盖它；同步收尾转交出去的那次分析服务的是前一条 job"
        )


def _create_analysis_task(job, config_id, target_key, *, mode, source, key) -> Optional[int]:
    """排一条 `weekly_ai_analysis` 任务（等价的请求会被它自己按分组去重/附着）。"""
    from services.task_worker_queue_service import create_weekly_ai_analysis_task

    task_id = create_weekly_ai_analysis_task(
        config_id,
        target_key,
        trigger_source=source,
        requested_mode=mode,
        idempotency_key=key,
        job_id=job.id,
    )
    if task_id is None and get_job(job.id) is None:
        # 队列那一层失败时会 `db.session.rollback()`，**那条回滚会把我们这条只 flush
        # 过、还没提交的 job 行一起丢掉** —— 而身份必须比执行体先存在（模型 docstring
        # 的第一条）。补回来，让用户至少拿到一个可恢复的 job_id。
        db.session.add(job)
        db.session.flush()
        _log(f"⚠️ 分析任务创建失败，job #{job.id} 已保留（等待同步/恢复扫描接手）")
    return task_id


# ---------------------------------------------------------------------------
#  执行侧写回（worker 调用）
# ---------------------------------------------------------------------------


def record_plan(
    job_id,
    *,
    payload: Mapping[str, Any] | None = None,
    project_id: Optional[int] = None,
    config_id: Optional[int] = None,
    target_type: str = "weekly",
):
    """把**这一轮的计划**写回这条 job（P2-2）：输入文件数 / token 区间 / 目标快照 id。

    ## 为什么落在这里、这个时刻

    三个 `planned_*` 与 `target_snapshot_id` 此前**没有任何生产写入**（只有 `to_dict`
    在读它们），于是 `/jobs/<id>` 与确认框上那几格永远是空的 —— 而「这次要跑多少文件、
    大概多少钱」正是执行前唯一还能拦一下的信息。

    写的时刻是「快照已冻结、模式升格已确定」：这三样里有两样（目标快照、真实输入文件数）
    只有到那时才是**事实**而不是预估值；模式升格则会让计划整体变一次（增量 → 全量），
    早写的那一份必然作废。

    ## 估算走**预检端点用的同一个函数**

    `analysis_estimate` 是那个端点的取数+算术入口，这里原样调它（同参数、同口径）——
    两处各算一遍，用户就会在确认框看到一个数、在 job 详情里看到另一个数，而两个数都是
    「预计消耗」。取不到区间时（没有同类样本、配置读不动、函数抛错）**不写 0**：把原因
    留进 `planned_estimate_note`，界面照它显示。
    """
    job = get_job(job_id)
    if job is None:
        return None
    data = dict(payload) if isinstance(payload, Mapping) else {}
    files = _payload_file_count(data)
    if files is not None:
        job.planned_files = files
    snapshot_id = _payload_snapshot_id(data)
    if snapshot_id is not None:
        job.target_snapshot_id = snapshot_id

    if files is None or project_id is None:
        job.planned_estimate_note = (
            "这次没有可用的输入账（payload 里没有文件清单），区间没有估算 ——"
            "面板上那两格空白表示**未估**，不是 0"
        )
    else:
        try:
            from services.ai_usage_service import analysis_estimate

            estimate = analysis_estimate(
                project_id,
                planned_files=files,
                mode=str(data.get("scope") or ""),
                target_type=target_type,
                config_id=config_id,
            )
            tokens = (estimate or {}).get("tokens") or {}
            low, high = tokens.get("low"), tokens.get("high")
            if low is None and high is None:
                job.planned_estimate_note = "；".join(
                    str(note) for note in ((estimate or {}).get("notes") or [])[:2]
                ) or "没有同类历史运行可作样本，区间未估算（不是 0）"
            else:
                job.planned_tokens_low = low
                job.planned_tokens_high = high
                job.planned_estimate_note = ""
        except Exception as exc:  # noqa: BLE001 —— 估算失败不该挡住这次分析
            job.planned_estimate_note = (
                f"区间估算失败（{type(exc).__name__}: {exc}）—— 空白表示**未估**，不是 0"
            )
            _log(f"⚠️ AI 分析：job {job_id} 的计划估算写不进去：{exc}")
    db.session.flush()
    return job


def _payload_file_count(payload: Mapping[str, Any]) -> Optional[int]:
    """这次运行的输入文件数（`delta_files` 的条数；取不到时退回 summary 里的计数）。

    **取不到就是 `None`**：写成 0 会让面板显示「这次 0 个文件」，而真相是「这份 payload
    里没有清单」（单提交模式就是那样）。
    """
    rows = payload.get("delta_files")
    if isinstance(rows, (list, tuple)):
        return len(rows)
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        for key in ("delta_files", "batch_files"):
            value = summary.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _payload_snapshot_id(payload: Mapping[str, Any]) -> Optional[int]:
    """这次分析冻结的目标快照 id（`payload["snapshot"]["snapshot_id"]`）。"""
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    value = snapshot.get("snapshot_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def mark_running(
    job_id, *, run_id=None, task_id=None, lease_seconds=None,
    effective_mode=None, upgrade_reason=None,
):
    """这条 job 开始跑了：记运行号 / 任务号 / 租约，状态置 `running`。

    返回那条 job（找不到返回 `None`）。**已经终态的 job 不复活** —— 迟到的
    `mark_running`（例如重启后重放一条老任务）不该把一条跑完的 job 推回「分析中」。
    """
    job = get_job(job_id)
    if job is None:
        return None
    if _is_terminal(job):
        return job
    job.state = STATE_RUNNING
    if job.started_at is None:
        job.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if run_id is not None:
        job.run_id = run_id
    if task_id is not None:
        job.task_id = task_id
    if effective_mode:
        job.effective_mode = effective_mode
    if upgrade_reason:
        job.upgrade_reason = upgrade_reason
    if lease_seconds:
        job.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            seconds=int(lease_seconds)
        )
    db.session.flush()
    return job


def settle_from_run(run):
    """这条 run 跑完了 → 按它的终态收口 job（返回那条 job，找不到返回 `None`）。

    三件事必须做对：

    1. **状态按 `run.status` 映射**（`succeeded` / `degraded` / `failed`，
       `services/ai/engine.py` 的三个 `STATUS_*`，由 `_persist_outcome` 原生写入）。
       `degraded` 是**终态且有可复用结论**，不许折叠成 `succeeded`，也不许当成失败；
    2. 写 `finished_at`；
    3. **把 `active_key` 清成 NULL** —— 唯一索引的口径是「跑完就清空」。不清的后果是
       同一份输入的下一次分析**永远创建不出 job**（只能附着到一条已经跑完的 job 上），
       也就是「点了没反应」。
    """
    if run is None:
        return None
    raw_status = str(getattr(run, "status", "") or "").strip().lower()
    if raw_status in _RUN_STATUS_OPEN:
        # 还没跑完（`pending` / `running`）：没有终态可言，什么都不动。
        return None
    new_state = _RUN_STATUS_TO_JOB_STATE.get(raw_status)
    if new_state is None:
        # 认不出来的状态（老数据 / 新加的枚举）：**不动 job**，也不猜成 failed ——
        # 猜错的代价是把一条还在跑的 job 判死（用户再也等不到结论）。
        return None

    jobs = (
        AiAnalysisJob.query.filter(AiAnalysisJob.run_id == getattr(run, "id", None))
        .order_by(AiAnalysisJob.id.desc())
        .all()
    )
    if not jobs:
        fallback = _job_of_run(run)
        jobs = [fallback] if fallback is not None else []
    if not jobs:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        request_payload = json.loads(getattr(run, "request_payload", None) or "{}")
    except (TypeError, ValueError):
        request_payload = {}
    policy = request_payload.get("policy") or {}
    for job in jobs:
        if _is_terminal(job):
            continue
        job.state = new_state
        job.run_id = getattr(run, "id", job.run_id)
        job.effective_mode = getattr(run, "scope", None) or job.effective_mode
        if job.requested_mode == MODE_INCREMENTAL and job.effective_mode == MODE_FULL:
            job.upgrade_reason = str(policy.get("reason") or job.upgrade_reason or "runtime_scope_upgrade")
        job.finished_at = now
        job.active_key = None
        job.lease_expires_at = None
        error_message = str(getattr(run, "error_message", "") or "").strip()
        if new_state == STATE_FAILED and error_message:
            job.error_message = error_message
    db.session.flush()
    return jobs[0]


def settle_without_run(job_id, *, reason, state=None, message=None):
    """这次分析**没有建出 run** 就结束了 → 照样把它收口（返回那条 job，找不到返回 `None`）。

    ## 为什么必须有这个出口（这是一个**功能性阻塞**，不是状态难看）

    `settle_from_run` 靠一条 `AiAnalysisRun` 行来映射终态。但有好几个结局在 worker 里
    直接结束、**不带 run 回来**：分析开关关着 / 预算不足 / 同步闸门拦下 / 没有变化直接
    复用。原先这些结局没有任何人结清那条 job，于是它**永远停在非终态**，而它的
    `active_key` 也从没被清空过 —— 而 `uq_ai_job_active_key` 是**唯一索引**：

        同一个 target + focus 从此再也建不出任何 job。

    用户点按钮会一直附着到那条永远不动的 job 上（`attached=True`、状态不变、什么都不
    发生），也就是「点了没反应」那一类静默故障。

    ## 终态怎么选（`reason` → 状态，语义要诚实）

    见 `WITHOUT_RUN_REASON_STATES`。要点：**「没有变化、直接复用了结论」是 `reused`**
    （有结论、零消耗），**「开关关着 / 预算不足 / 被闸门拦下」是 `cancelled`**
    （这次动作没被执行），**真正的错误才是 `failed`**。一律塞 `failed` 会让用量面板
    把「没花钱的那一次」算成一次失败运行 —— 那两件事在模型 docstring 里是分开的。

    `state` 显式给出时以它为准（调用方知道得更多时用它）；`message` 覆盖默认那句话。

    ## 幂等

    已经是终态就**什么都不做、返回它**：不复活、不改终态、不覆盖 `finished_at`
    与 `error_message`。迟到的收口（重放的老任务、恢复扫描的重复一趟）不该改动一条
    已经跑完的 job。

    ## 事务

    只 `flush()`，**由调用方提交**（与本模块其余写侧同一口径）。
    """
    job = get_job(job_id)
    if job is None:
        return None
    if _is_terminal(job):
        return job

    code = _reason_code(reason)
    resolved = state or WITHOUT_RUN_REASON_STATES.get(code) or STATE_FAILED
    if resolved not in TERMINAL_JOB_STATES:
        # 调用方给了一个**非终态**（写错了）：不许把 job 留在一个看起来收过口的
        # 中间状态上，退回默认的那一档，并如实记成失败。
        resolved = STATE_FAILED
        code = REASON_TASK_ENDED_WITHOUT_RUN

    job.state = resolved
    job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # **唯一索引的口径是「跑完就清空」**：不清的后果不是状态难看，而是同一份输入的
    # 下一次分析永远创建不出 job。这一行就是这个缺口的核心。
    job.active_key = None
    job.lease_expires_at = None
    job.error_message = (
        str(message).strip()
        if message
        else WITHOUT_RUN_REASON_MESSAGES.get(code) or f"这次分析没有结论（{code}）。"
    )
    db.session.flush()
    return job


def settle_job_that_never_ran(job_id, *, reason, message=None):
    """只结清**从没跑起来过**的 job（已经有 run 的返回 `None`，一个字都不改）。

    与 `settle_without_run` 只差一条判据，但那条判据是**安全判据**：`settle_without_run`
    只挡「已经终态」，**不挡「已经带着 run 在跑」**。

    ## 为什么必须单独有这个出口（真机实测，2026-09-24）

    扫尾路径（`task_worker_queue_service._settle_job_of_intent`）会在一条等待意图被了结时
    收口它服务的 job。真机上的意图 #5034 **已经转交出去**、job 32 正带着 run 62 在跑，
    下一趟扫尾却把这条意图判成「已经被 run=62 覆盖」（判据是「这个分组有没有 run 在跑」，
    而那条 run **正是这条意图自己转交出去的那一次**），顺手把 job 32 收成 `cancelled` ——
    run 还在跑、job 已经死了。而且这个分叉**不会被自动纠正**：`settle_from_run` 对终态
    job 是 `continue`，那条 run 跑完也再没人把 job 摆正。

    「这条 job 的终态归谁写」只有一个答案：**它有 run 就归那条 run**，别人不许替它判。
    所以判据用 `PRE_RUN_JOB_STATES`（还没交出执行体的那几档）**并且**没有 `run_id` ——
    两条都要成立才收。状态与 `run_id` 在 `mark_running` 里同一次 flush 写入，只有一条
    为真只可能是脏数据，而脏数据上的正确处置是**不动它**（与 `_settle_one_stale_job`
    判据 3 同一口径）。

    `job_id` 找不到时返回 `None`；已经是终态时按 `settle_without_run` 的幂等口径原样返回。
    """
    job = get_job(job_id)
    if job is None:
        return None
    if _is_terminal(job):
        return job
    if getattr(job, "run_id", None) is not None:
        # 已经有 run：它的终态归那条 run 的映射（`settle_from_run`）。
        _log(
            f"⏭️ 不收口 job {job_id}：它已经带着 run {job.run_id} 在跑/跑过，"
            f"终态归那条 run（{reason}）"
        )
        return None
    if str(getattr(job, "state", "") or "") not in PRE_RUN_JOB_STATES:
        # 不是「还没交出执行体」的那几档，却没有 run_id：脏数据，不猜，不动它。
        _log(f"⏭️ 不收口 job {job_id}：状态 {job.state!r} 不在「还没跑」的那几档里（{reason}）")
        return None
    return settle_without_run(job_id, reason=reason, message=message)


def _reason_code(reason) -> str:
    """把 `result["reason"]` 归一成短码（认不出来时原样返回，映射表会兜到 `failed`）。"""
    return str(reason or "").strip().lower() or REASON_TASK_ENDED_WITHOUT_RUN


def focus_for_task(task_id) -> Optional[str]:
    """这条分析任务服务的 job 选的**分析范围**（读不到 / 范围是「全部」→ `None`）。

    ## 为什么读 job 行而不是任务行

    `focus` 只落在 `AiAnalysisJob.focus` 上（任务行上没有这一列），而任务行上有
    `job_id`。所以执行侧（`run_weekly_analysis_background`）只要拿到 `task_id` 就能
    把用户选的范围读回来 —— **不需要改 `create_weekly_ai_analysis_task` /
    `register_waiting_analysis_intent` 的签名**。

    ## 认一次「那个 id 真的是一条 job」

    与 `task_worker_task_handlers.job_id_for_task` 同一把尺子、同一个理由：任务行上
    `job_id` 那一列的冻结语义是「服务于哪个 `ai_analysis_job`」，但在 job 体系落地
    之前的路径上写进去的是**意图行的 id**，两者数值上完全可能撞车。用 `get_job`
    认不出来就当没有 job（退回「不筛」），**不许**拿一个意图 id 去读别人的范围。

    读不到时返回 `None` = 「不筛」（`_filter_delta_files_by_focus` 的既有默认），
    这是一个安全的回落：范围是**缩窄**输入的东西，读不到范围只会让这次分析看全，
    不会让它看漏。
    """
    if task_id is None:
        return None
    try:
        row = db.session.get(BackgroundTask, int(task_id))
    except (TypeError, ValueError):
        return None
    if row is None:
        return None
    ref = getattr(row, "job_id", None)
    if not ref:
        return None
    job = get_job(ref)
    if job is None:
        return None
    focus = str(getattr(job, "focus", "") or "").strip()
    if not focus or focus == FOCUS_ALL:
        return None
    return focus


# ---------------------------------------------------------------------------
#  启动恢复扫描：兜住「连 `settle_without_run` 那条路都没走到」的 job
#
#  那三种形态都是「没有任何在途代码会去收口它」：
#
#  * 进程被杀（worker 再也不回来，任务行却已经被人/别的路径改成终态）；
#  * job 建好了，但排程那一步炸了（`_create_analysis_task` 失败、等待意图登记失败）；
#  * 任务行被人手工改成终态而 job 没跟着走。
#
#  判据必须**保守**：一条合法长跑了两小时的分析不许被扫成终态。
# ---------------------------------------------------------------------------


def recover_stale_jobs(*, now=None, limit=200) -> dict:
    """把卡在非终态、且**确定**不会再有下文的 job 结清。返回一份可进日志的账。

    形态：`{"checked": N, "settled": N, "by_reason": {短码: 条数}}` ——
    「清了几条、按哪个理由」两件事都答得上（只报一个总数，日志里就分不清
    「一堆同步闸门」与「一堆孤儿」）。

    ## 三条判据（任一成立才动手）

    1. **它关联的 run 已是终态**（`succeeded` / `degraded` / `failed`）——
       产出已经落定，只是 `settle_from_run` 那一跳没走到。走它自己的映射
       （`settle_from_run`），状态最准；
    2. **它关联的任务行已是终态**，而 job 不是 —— 执行体结束了，job 不会再有下文。
       任务行上那句 `skipped:<reason>` 会被读回来，于是终态仍然按**语义**选
       （`reused` / `cancelled` / `failed`），不是一个笼统的 failed；
    3. **它没有任何关联任务/run**、`created_at` 超过 `JOB_STALE_SECONDS`、且它那个
       target 上**没有活的等待意图**（也没有还排着队/正在跑、引用这条 job 的分析任务）
       → 建完就崩 / 排程那一步炸了这一类。

    ## 为什么每条判据都留了「不动它」的余地

    任何一种「不确定」都**不要**动它（宁可留给下一轮）：

    * 判据 1 只在 run **已经是终态**时才成立。一条跑了两小时还在跑的合法分析，
      `is_stale` 早就是 True 了（租约也过期了），但它的 run 还在 `running` ——
      这里不看 `is_stale`，只看 run 的终态；
    * 判据 2 只在任务行**已经是终态**时才成立。worker 正拿着的那条是 `processing`；
    * 判据 3 要求**两条**否定证据：没有活的等待意图，且没有活的分析任务引用它。
      「同步还在写缓存、这次点击好端端地登记着」与「建完就崩」在 job 行上长得一样，
      区别就在那条意图上 —— 误杀的后果是：同步一收尾那次分析照跑，而 job 已经是
      终态了（页面显示「已取消」而钱照花）。

    `JOB_STALE_SECONDS` **只是「还有没有证据」的兜底，不是「同步等多久」的口径**
    （2026-09-26 订正）：判据 3 那两条否定证据才是决定性的，其中「活的等待意图」按
    **意图是否 pending** 判（`_live_waiting_intent` 不看 TTL）—— 同步跑多久，意图就
    pending 多久。所以一条正在等的 job 不会被这个 30 分钟判死，而意图被
    `wake_waiting_analysis_intents` 收掉之后（过期/被覆盖/转交完成）它才真的没有未来 ——
    也就是本函数接手的时候。

    ## 事务

    **本函数自己提交。** 与 `run_cache_source.fail_orphaned_analysis_runs` 同一手法：
    启动扫描是一次独立的、幂等的清理，事务边界在它自己身上（调用点是一行 fire-and-
    forget 的启动代码，把提交留给它等于让这次清理吊在下一个不相干的事务上）。
    """
    outcome = {"checked": 0, "settled": 0, "by_reason": {}}
    try:
        candidates = (
            AiAnalysisJob.query.filter(AiAnalysisJob.state.in_(ACTIVE_JOB_STATES))
            .order_by(AiAnalysisJob.id.asc())
            .limit(max(int(limit), 1))
            .all()
        )
        for job in candidates:
            outcome["checked"] += 1
            try:
                reason, settled = _settle_one_stale_job(job, now=now)
            except Exception as exc:  # noqa: BLE001 —— 单条判据读不到不该让整趟扫描停住
                _log(f"⚠️ 恢复扫描：判不了 job #{job.id}（按「不动它」处理）: {exc}")
                continue
            if settled is None:
                continue
            outcome["settled"] += 1
            outcome["by_reason"][reason] = outcome["by_reason"].get(reason, 0) + 1
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 —— 启动路径上不许把整个 worker 拖死
        db.session.rollback()
        _log(f"⚠️ 恢复扫描失败（不影响启动）: {exc}")
        return outcome
    if outcome["settled"]:
        _log(
            f"🧹 恢复扫描：结清了 {outcome['settled']} 条卡在非终态的 job"
            f"（按理由 {outcome['by_reason']}，检查了 {outcome['checked']} 条）"
        )
    return outcome


def _settle_one_stale_job(job, *, now=None):
    """一条 job 的处置。返回 `(理由短码, 结清后的那条 job)`；不该动时 `(None, None)`。

    **这是最常见的返回**：一条正常的分析（任务 `processing`、run `running`）在这里
    原样返回 `(None, None)`。
    """
    # --- 判据 1：产出已经落定 ---
    run = _run_of_job(job)
    if run is not None:
        raw = str(getattr(run, "status", "") or "").strip().lower()
        if raw in _RUN_STATUS_OPEN:
            # run 还在跑 / 刚建好：**不动它** —— 这正是一条合法长跑的分析。
            return None, None
        if raw not in _RUN_STATUS_TO_JOB_STATE:
            # 认不出来的状态（老数据 / 新加的枚举）：不猜成 failed（猜错的代价是把一条
            # 还在跑的 job 判死）。与 `settle_from_run` 同一口径。
            return None, None
        # 交给它自己的映射：状态最准（`degraded` 不许被折叠成别的）。
        return REASON_RUN_ALREADY_TERMINAL, settle_from_run(run)

    # --- 判据 2：执行体已经结束 ---
    task = _task_of_job(job)
    if task is not None:
        if not _task_is_terminal(str(getattr(task, "status", "") or "").strip().lower()):
            # 排队中 / 正在跑：它还活着。
            return None, None
        reason = _skip_reason_of_task(task) or REASON_TASK_ENDED_WITHOUT_RUN
        return reason, settle_without_run(job.id, reason=reason)

    # --- 判据 3：既没有执行体、也没有产出 ---
    if job.run_id is not None or job.task_id is not None:
        # 有引用、两行却都读不到（行被清了 / 库脏）：**判不了**，不动它。
        return None, None
    if not _is_older_than_stale_window(job, now=now):
        return None, None
    if _live_waiting_intent(job) is not None:
        return None, None
    if _live_task_of_job(job) is not None:
        return None, None
    return REASON_ABANDONED, settle_without_run(job.id, reason=REASON_ABANDONED)


def _task_of_job(job):
    """这条 job 的执行体（`job.task_id` 那一行）。"""
    task_id = getattr(job, "task_id", None)
    if not task_id:
        return None
    try:
        return db.session.get(BackgroundTask, int(task_id))
    except (TypeError, ValueError):
        return None


def _task_is_terminal(status: str) -> bool:
    """这个任务状态是不是终态。

    **用既有的那份常量**（`services/weekly_version_sync_status.TERMINAL_TASK_STATUSES`），
    不在这里另立一套：任务状态是队列那一层的词，两处判分叉的后果是「一边认为结束了、
    一边认为还在跑」，而那正好是本缺口要修的那种静默。
    """
    from services.weekly_version_sync_status import TERMINAL_TASK_STATUSES

    return str(status or "") in TERMINAL_TASK_STATUSES


def _skip_reason_of_task(task) -> Optional[str]:
    """任务行上那句 `skipped:<reason>`（`task_worker_task_handlers` 写下的）。

    有它就把终态按**语义**选出来（`reused` / `cancelled`），而不是笼统地记成 failed。
    读不到就返回 `None`，由调用方退回默认那一档。
    """
    raw = str(getattr(task, "error_message", "") or "").strip()
    prefix = "skipped:"
    if not raw.lower().startswith(prefix):
        return None
    code = raw[len(prefix):].strip().lower()
    return code or None


def _is_older_than_stale_window(job, *, now=None) -> bool:
    """`created_at` 超过 `JOB_STALE_SECONDS` 了吗。

    **判据是 `created_at`**（模型那一列的注释：`is_stale` 用 `started_at or created_at`，
    而这里是「连执行体都没建出来」的那一档，`started_at` 必然是 NULL）。时间读不到时
    返回 `False` —— 判不了就不动它。
    """
    created = _as_naive_utc(getattr(job, "created_at", None))
    if created is None:
        return False
    reference = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    return created + timedelta(seconds=JOB_STALE_SECONDS) < reference


def _live_waiting_intent(job):
    """这个 target 上还有一条**还没了结**的等待意图吗（判据 ③ 的否定证据之一）。

    判据用 `pending_waiting_analysis_intents()` 而不是
    `effective_waiting_analysis_intent()`：后者会把**过了 TTL** 的那条也算成「不生效」
    （它服务的是手工入口那道「已经登记过」的闸门），而过了 TTL 的意图**这一趟**就由
    `wake_waiting_analysis_intents` 收成 `cancelled` —— 在它被收掉之前，本函数必须
    按「还活着」对待，否则就会在同步还在跑的时候把那条 job 判死
    （页面显示「已取消」，而同步一收尾那次分析照跑、钱照花）。
    """
    target_key = str(getattr(job, "target_key", "") or "")
    if not target_key:
        return None
    try:
        from services.task_worker_queue_service import pending_waiting_analysis_intents

        for intent in pending_waiting_analysis_intents() or []:
            if str(getattr(intent, "file_path", "") or "") == target_key:
                return intent
    except Exception as exc:  # noqa: BLE001 —— 读不到就**别动**（宁可留给下一轮）
        _log(f"⚠️ 恢复扫描：读不到 job #{job.id} 那个 target 的等待意图（按「还有」处理）: {exc}")
        return True
    return None


def _live_task_of_job(job):
    """还排着队 / 正在跑、且**引用这条 job** 的分析任务（判据 ③ 的否定证据之二）。

    为什么不能只看 `job.task_id`：等待意图转交出去的那条任务是在 job_service 之外
    建的（`_resolve_waiting_intent`），`job.task_id` 上**没有**它。只看 `job.task_id`
    会把「已经转交出去、那条任务还排着队」的 job 当成孤儿清掉。
    """
    try:
        return (
            BackgroundTask.query.filter(
                BackgroundTask.task_type == "weekly_ai_analysis",
                BackgroundTask.job_id == job.id,
                BackgroundTask.status.in_(("pending", "processing")),
            )
            .order_by(BackgroundTask.id.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001 —— 读不到就**别动**
        _log(f"⚠️ 恢复扫描：读不到 job #{job.id} 名下还活着的分析任务（按「还有」处理）: {exc}")
        return True


# ---------------------------------------------------------------------------
#  订阅侧（`GET /ai-analysis/jobs/<id>/events` 的载荷）
#
#  这一层只**读**：读 job 行、读进度快照、读等待意图的状态。三处都不建 run、
#  不排队、不发模型请求 —— 「重连不会创建第二条 run」这条验收就是靠它成立的。
# ---------------------------------------------------------------------------


def notice_for_waiting(job) -> dict:
    """这条 job 在等同步时，页面该说的那句话。

    文案**只有一份**（`task_worker_queue_service.describe_waiting_analysis` 里）：
    它同时看「登记还在不在」「有没有已经转交出去的任务」「有没有运行在跑」，
    在这里另写一段必然与它漂移。
    """
    config_id = getattr(job, "target_id", None)
    target_key = getattr(job, "target_key", None)
    payload = {"waiting": True, "run_id": None, "message": ""}
    try:
        from services.task_worker_queue_service import describe_waiting_analysis

        described = describe_waiting_analysis(config_id, target_key) or {}
    except Exception as exc:  # noqa: BLE001 —— 一句话读不到不该把订阅流打断
        _log(f"⚠️ 读取等待同步状态失败（按「还在等」处理）: job={job.id} {exc}")
        return payload
    return {
        "waiting": True,
        "run_id": described.get("run_id"),
        "message": described.get("message") or "",
    }


def result_payload(job) -> Optional[dict]:
    """终态 job 的 `result` 事件载荷 —— 与抽屉既有那条路读到的形状**同形**。

    形状取「落库的 `response_payload` + `run_id` + `status`」：那正是执行侧
    （`ai_analysis_service._execute_analysis` → `_persist_outcome`）落库的那一份
    （`result` 就是 `_persist_outcome` 写进 `response_payload` 的那个 dict）。
    界面因此不需要第二套渲染器：`risk_level` / `usage` / `degradation_label` /
    `coverage_notice` 这些键在同一个位置上。

    没有 run（job 建好了但没跑起来就被取消）返回 `None` —— 页面据此走「没有结论」
    那一支，而不是拿到一个空壳当成一份结论。
    """
    run = _run_of_job(job)
    if run is None and getattr(job, "reused_run_id", None):
        from models.ai_analysis import AiAnalysisRun

        run = db.session.get(AiAnalysisRun, job.reused_run_id)
    if run is None:
        return None
    parsed = _parsed_payload(getattr(run, "response_payload", None))
    payload = dict(parsed) if isinstance(parsed, dict) else {}
    payload["run_id"] = run.id
    payload["status"] = str(getattr(run, "status", "") or "")
    payload["response_text"] = getattr(run, "response_text", "") or ""
    error_message = str(getattr(run, "error_message", "") or "")
    if error_message:
        payload.setdefault("error_message", error_message)
    return payload


def progress_payload(job) -> Optional[dict]:
    """正在跑的 job 的进度（`services/ai/run_progress.py` 的进程内快照）。

    **读不到就是 `None`**（没在跑 / 跑在别的进程 / 已过期），界面照既有口径显示
    「进度不可用」—— 不显示 0，也不假装「第 0 轮」（见那个模块的三条纪律）。
    """
    run = _run_of_job(job)
    if run is None:
        return None
    try:
        from services.ai import run_progress

        snap = run_progress.snapshot(run.id)
    except Exception:  # noqa: BLE001 —— 进度只是给界面看的一眼，读不到不该打断流
        snap = None
    if snap is not None:
        return snap.to_dict()
    return _parsed_payload(getattr(job, "progress_json", None))


def progress_payload_for_run(run_id: int) -> Optional[dict]:
    """按 run 读取进度；Web 与 worker 分进程时退回 job 的持久化快照。"""
    if not run_id:
        return None
    job = (
        AiAnalysisJob.query.filter(AiAnalysisJob.run_id == run_id)
        .order_by(AiAnalysisJob.progress_updated_at.desc(), AiAnalysisJob.id.desc())
        .first()
    )
    return progress_payload(job) if job is not None else None


def persist_progress(run_id, payload: dict) -> None:
    """把显示用进度写到所有附着于该 run 的 job；失败不得影响模型分析。"""
    if not run_id or not isinstance(payload, dict):
        return
    rows = AiAnalysisJob.query.filter(AiAnalysisJob.run_id == run_id).all()
    if not rows:
        return
    encoded = json.dumps(payload, ensure_ascii=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        row.progress_json = encoded
        row.progress_updated_at = now
    db.session.commit()


def _run_of_job(job):
    """这条 job 的产出 run（有运行号才有 run）。"""
    run_id = getattr(job, "run_id", None)
    if not run_id:
        return None
    from models.ai_analysis import AiAnalysisRun

    return db.session.get(AiAnalysisRun, run_id)


def _parsed_payload(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------


def _as_naive_utc(value) -> Optional[datetime]:
    """带时区的值折成 naive UTC（库里存的就是 naive，比较前必须统一）。"""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _log(message: str) -> None:
    from utils.logger import log_print

    log_print(message, "AI", force=True)
