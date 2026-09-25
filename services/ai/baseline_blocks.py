#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本增量基线的**编排块**：拿基准、冻目标、推进指针（AI-P0-02）。

## 为什么单独一个文件

`services/ai_analysis_service.py` 正好贴着长度闸门（WARN 1800、ERROR 2000），而
AI-P0-02 新增的每一行逻辑都属于「快照与基线」，与「怎么跑一次分析」无关：这一层的
输入是 payload / run / state 三个**已经存在的对象**，输出是「这次拿哪一份做差」与
「跑完之后把哪几个指针推到哪」。留在编排层只会把它推过硬上限。

判据与数据在 `services/ai/snapshot_store.py`（模型层之上、纯读写快照），本层只做
「按什么顺序问那几个问题」。

## 三件事被拆成了三个指针（这是本次重构的落点）

| 指针 | 回答的问题 | 谁推进 |
|---|---|---|
| `last_analyzed_at` | 最近一次**完整**分析是什么时候 | 只有 succeeded |
| `last_concluded_run_id` | 最近一次**有可复用结论**的运行 | succeeded 或降级（且结论结构化） |
| `last_complete_snapshot_id` | 最近一份**达到完整覆盖门槛**的快照 | 只有达标的那次 |

改动前这三件事由 `last_analyzed_at` **一个值**承担，而它对降级运行刻意不推进 ——
于是「首次全量还是增量」跟着一起废掉，手工路径每次都看到 `None`，每次都把约 1000 个
文件全放进白名单（实测 Run 20 相对 Run 15 只有 45 个路径身份变化）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from models import db
from models.ai_analysis import AiAnalysisRun, AiWeeklyAnalysisState
from services.ai import snapshot_store
from services.ai.baseline import FORCE_FULL_REASON
from services.ai.coverage_ledger import ledger_from_run
from services.ai.engine import STATUS_DEGRADED, STATUS_SUCCEEDED
from services.ai.project_config_source import _utcnow, get_project_analysis_config
from services.ai.run_cache_source import _json_dumps
from services.ai.scope_sampling import weekly_snapshot_digest
from services.ai.weekly_state import get_or_create_weekly_state
from utils.logger import log_print


def _parse_iso(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def resolve_baseline(
    group_key: str,
    state: Optional[AiWeeklyAnalysisState],
    *,
    force_full: bool = False,
    force_full_reason: str = FORCE_FULL_REASON,
) -> Tuple[object, dict]:
    """这次做差的基准，以及写进 payload 的那一份账 `(baseline, account)`。

    优先**冻结快照**；一份都没有时退回时间水位线（过渡态：升级上来的老分组，
    还没有任何快照）；两样都没有就是首跑。

    `force_full` 时**永远返回 None** —— 全量模式不看基线（复测文档 :165：
    「全量模式始终新建目标快照上的完整分析任务」）。

    ## `force_full_reason` 是给**下游**读的（2026-09-25）

    这一层只关心「做差基准给不给」，两档 `force_full` 在这儿做的事**一模一样**（都返回
    `None`）。但对下游不是：`reason` 是 `baseline_source.run_ignores_history` 的判据，
    而它的语义是「旧结论要不要进模型输入」。所以写入方必须说清是哪一档 —— 默认值保持
    `FORCE_FULL_REASON`（用户点了全量），平台自己那次重建传
    `FORCE_FULL_REBUILD_REASON`（见 `baseline` 模块里两个常量的说明）。
    """
    if force_full:
        return None, {
            "kind": "none",
            "reason": str(force_full_reason or FORCE_FULL_REASON),
            "complete": False,
        }
    try:
        baseline = snapshot_store.load_baseline(group_key)
    except Exception as exc:  # noqa: BLE001 —— 读不出基准不该让整个分析起不来
        log_print(
            f"⚠️ AI 分析：读取做差基准失败（{type(exc).__name__}: {exc}），"
            f"本轮按首跑处理（group={group_key}）",
            "AI",
            force=True,
        )
        baseline = None
    if baseline is not None:
        return baseline, baseline.account()

    watermark = getattr(state, "last_analyzed_at", None) if state is not None else None
    if watermark is not None:
        return watermark, {
            "kind": "watermark",
            "snapshot_id": None,
            "complete": False,
        }
    return None, {"kind": "none", "snapshot_id": None, "complete": False}


def seal_target_snapshot(payload: dict) -> Optional[dict]:
    """把这次分析的**目标快照**冻结下来，返回它的账（写进 `payload["snapshot"]`）。

    位置在 `_create_run` 里、**所有闸门之后**：闸门之前冻快照会把同步写到一半的清单
    冻成一份「当时的状态」，而那份清单从此会成为下一次做差的基准 —— 半份基准做出来
    的差集会把没写进来的文件全算成「新增」。

    出错只记日志不抛：快照是**账**，不该成为「一次分析起不来」的原因。
    """
    group = payload.get("group") or {}
    group_key = str(group.get("key") or "")
    config_ids = list(group.get("config_ids") or [])
    if not group_key or not config_ids:
        return None
    try:
        snapshot = snapshot_store.seal_snapshot(
            config_ids, group_key=group_key, project_id=group.get("project_id")
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        db.session.rollback()
        log_print(
            f"⚠️ AI 分析：冻结目标快照失败（{type(exc).__name__}: {exc}）—— "
            f"这次分析照跑，但它不会成为下一轮的做差基准",
            "AI",
            force=True,
        )
        return None
    if snapshot is None:
        return None
    return snapshot.to_dict()


def complete_coverage_ratio(payload: dict) -> float:
    """完整覆盖门槛的阈值：项目配置优先，缺省/坏值一律回落默认值。

    **坏值不许当成「达标」**：读不出来时回落默认（0.9），而不是回落 0 —— 后者会让
    任何一次运行都声称「已完整检查」。
    """
    project_id = (payload.get("group") or {}).get("project_id")
    config = {}
    if project_id:
        try:
            config = get_project_analysis_config(project_id) or {}
        except Exception:  # noqa: BLE001 —— 取不到配置不是判成达标的理由
            config = {}
    raw = config.get(snapshot_store.COMPLETE_COVERAGE_CONFIG_KEY)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return snapshot_store.DEFAULT_COMPLETE_COVERAGE_RATIO
    if 0 < value <= 1:
        return value
    return snapshot_store.DEFAULT_COMPLETE_COVERAGE_RATIO


def _complete_snapshot_id(payload: dict, run: AiAnalysisRun) -> Optional[int]:
    """这次的目标快照达没达到完整覆盖门槛；达到了就返回它的 id。"""
    snapshot = payload.get("snapshot") or {}
    snapshot_id = snapshot.get("snapshot_id")
    if not snapshot_id:
        return None
    baseline = payload.get("baseline") or {}
    try:
        ledger = ledger_from_run(run)
        qualified = snapshot_store.covers_completely(
            ledger=ledger,
            snapshot_files=int(snapshot.get("item_count") or 0) or None,
            base_complete=bool(baseline.get("complete")),
            threshold=complete_coverage_ratio(payload),
        )
    except Exception as exc:  # noqa: BLE001 —— 算不出来就不声称达标
        log_print(
            f"⚠️ AI 分析：完整覆盖门槛没能算出来（{type(exc).__name__}: {exc}）—— "
            f"本次不声称「已完整检查」（run={getattr(run, 'id', None)}）",
            "AI",
            force=True,
        )
        return None
    return int(snapshot_id) if qualified else None


def _state_row(payload: dict, state: Optional[AiWeeklyAnalysisState]) -> AiWeeklyAnalysisState:
    """拿到状态行；没有就建一行（并发首跑由 `get_or_create_weekly_state` 裁决）。"""
    if state is not None:
        return state
    group = payload.get("group") or {}
    return get_or_create_weekly_state(
        project_id=group.get("project_id"),
        group_key=str(group.get("key") or ""),
        base_name=str(group.get("base_name") or ""),
        start_time=_parse_iso(group.get("start_time")),
        end_time=_parse_iso(group.get("end_time")),
    )


def snapshot_digest_for(payload: dict) -> str:
    """这次分析对应的快照指纹；算不出来返回空串（调用方保留原值）。"""
    group = payload.get("group") or {}
    if not group:
        return ""
    try:
        return weekly_snapshot_digest(list(group.get("config_ids") or []))
    except Exception:  # pragma: no cover - 指纹算不出来不该影响交付
        return ""


def remember_snapshot_digest(payload: dict, state) -> None:
    """只记「这次分析的是哪一份快照」（内容指纹），**不动任何指针**。

    指纹只影响**下一次**要不要跳过同一份输入，不影响这次的交付；出错只记日志。
    """
    try:
        digest = snapshot_digest_for(payload)
        if not digest:
            return
        row = _state_row(payload, state)
        row.last_snapshot_digest = digest
        row.updated_at = _utcnow()
        db.session.commit()
    except Exception as exc:  # pragma: no cover - 只影响下次是否跳过
        db.session.rollback()
        log_print(f"⚠️ AI 分析：记快照指纹失败（不影响本次交付）: {exc}", "AI", force=True)


def advance_weekly_state(
    payload: dict,
    run: AiAnalysisRun,
    state: Optional[AiWeeklyAnalysisState],
    *,
    engine_status: Optional[str],
) -> None:
    """按**引擎状态**分三路推进这个分组的指针（AI-P0-02 的落点）。

    * `succeeded` —— 时间水位线 + 运行号 + 结论基线 + （达标时）完整覆盖指针 + 指纹。
      时间水位线是「最近一次**完整**分析的时刻」，只有这一路能推。
    * `degraded` —— **只推结论基线**（且必须是**结构化结论**：只有一份 markdown 的那次
      一条结构化结论都没留下，拿它当基线会让下一轮把报过的问题全部当新发现重报），
      外加内容指纹。**绝不推时间水位线，也绝不推完整覆盖指针**：那 94% 没取到证据的
      文件不许被标成「已看过」，也不许被说成「已完整检查」。
    * 其余（`failed` / 认不出来的取值）—— **一个都不推，连指纹也不写**。

    ## 「失败不写指纹」是个反直觉的取舍，理由写在这里（半年后没人会记得是故意的）

    内容指纹（`last_snapshot_digest`）对 `snapshot_already_analyzed` 的意义是
    **「这份输入已经分析过了，再跑一遍只会得到同样的结果」**。这句话在**失败**的那次上
    是**假的**：一次没跑成的分析没有产出任何结论，它证明不了「再看一遍没意义」——
    它只证明了「这次没看成」（配额用尽、连接断了、进程被杀、key 配错了）。

    而调度器只看那个指纹：写下去之后，下一轮 tick 会直接跳过这一组
    （`task_worker_service.schedule_weekly_ai_analysis_tasks` 的 `same_snapshot` 分支），
    并且**推进 `last_triggered_at` 让这条判定被间隔节流**。于是这个版本在**下一批提交
    到来之前再也不会被分析**，而它一次都没跑成 —— 库里只有一条 `failed`，
    面板上没有任何地方说得出「它被自己的失败记录挡住了」。

    改动前那条路径是「`engine_status != succeeded` 一律写指纹」，所以这个坑是**存在过**的，
    不是假想：只要有一次分析在配额/网络/密钥上失败，这个周版本就静默停摆到下一批提交为止。

    ## 反方向的担忧（重试风暴）不成立

    「不写指纹」不等于「每分钟重试一次」：调度器**先**判 `not_due`
    （`state.last_triggered_at` + 项目的 `weekly_interval_minutes`，默认 120 分钟），
    而排队与执行都会推进那个水位线；预算闸门与同步闸门也各自有节流。所以最坏情况是
    **每个分析间隔重试一次**，直到跑成为止 —— 那正是想要的语义。

    ## 那失败这次留下的「缺口」谁记着

    `AiAnalysisRun.status = failed` + `error_message` 就是这次的账（`_persist_outcome` 写全），
    界面上那条「最近一次分析失败」也读它。**不需要**在状态行的指针上再记一笔 ——
    指针回答的是「最近一次**成功/有结论**的是哪次」，不是「最近一次尝试是哪次」。

    `engine_status` 是**必填关键字参数**（`tests/test_ai_analysis_service.py` 用
    `inspect.signature` 钉死）：给它默认值就等于留了一条「忘了传就退回 `run.status`」的
    路，而 `run.status` 分不出 degraded —— 那正是这个缺陷本身。
    """
    if engine_status == STATUS_SUCCEEDED:
        _advance_on_success(payload, run, state)
        return
    if engine_status == STATUS_DEGRADED:
        # 指纹先记（它自己会建行）：降级不推水位线是有意的，但**内容指纹**必须留下 ——
        # 否则「降级跑完 → 水位线不动 → 下个周期又判有新变化 → 同一份输入再分析一遍」
        # 会一直转，每小时烧一次全量分析，而输入一字未变。
        remember_snapshot_digest(payload, state)
        if not getattr(run, "conclusion_structured", False):
            # 没有结构化结论（`DEGRADE_MARKDOWN`）：这次没留下可比对的结论，
            # 结论基线不动 —— 它仍然指向上一次真的产出了结论的那条运行。
            return
        row = _state_row(payload, state)
        row.last_concluded_run_id = run.id
        row.updated_at = _utcnow()
        db.session.commit()
        return
    log_print(
        f"AI 分析：本次未推进任何指针（engine_status={engine_status}）"
        f" run={getattr(run, 'id', None)}",
        "AI",
    )


def _advance_on_success(
    payload: dict, run: AiAnalysisRun, state: Optional[AiWeeklyAnalysisState]
) -> None:
    group = payload.get("group") or {}
    summary = payload.get("summary") or {}
    if not group:
        return
    row = _state_row(payload, state)

    digest = snapshot_digest_for(payload)
    if digest:
        row.last_snapshot_digest = digest
    row.last_analyzed_at = _utcnow()
    row.last_analysis_run_id = run.id
    row.last_scope = run.scope
    row.last_summary = _json_dumps(summary)
    row.last_triggered_at = run.started_at or _utcnow()
    # 跑完整了 → 它同时是**结论基线**（下一次增量继承它的结论）。
    row.last_concluded_run_id = run.id
    complete_id = _complete_snapshot_id(payload, run)
    if complete_id:
        row.last_complete_snapshot_id = complete_id
        try:
            snapshot_store.mark_complete(complete_id)
        except Exception as exc:  # noqa: BLE001 —— 标记失败不影响交付
            log_print(
                f"⚠️ AI 分析：标记快照为「已完整检查」失败（快照 {complete_id}）：{exc}",
                "AI",
                force=True,
            )
    row.updated_at = _utcnow()
    db.session.commit()
