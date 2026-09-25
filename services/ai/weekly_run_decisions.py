#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本运行期的四个判定：谁要全量、这次看哪一段、跑完推哪些指针、复用哪条结论。

## 为什么单独一个文件

`services/ai_analysis_service.py` 贴着长度闸门（WARN 1800、ERROR 2000），2026-09-25 修
`force_full` 那一笔把它顶破了红线（2011 行）。这四个函数是**执行期的判定**：它们之间
只共享「这次运行」这一个上下文，与「怎么组装 payload」无关。搬出来之后那个文件回到
1900 行附近，而这里每一个都带着自己的判据与来由（下面几条 docstring 是它们的正文，
不是补充说明）。

## 名字仍是下划线开头，且**必须**在服务文件里回导

测试按 `ai_service._update_weekly_state` / `ai_service._requests_full_analysis` 取用，
还有一处 `monkeypatch.setattr(ai_service, "_update_weekly_state", …)`。**调用点仍在服务
文件里**，所以名字必须留在那个模块上 —— 删掉那组回导，打补丁就变成打不中任何调用的
空操作（补丁本身不会报错，这才是危险的地方）。
"""

from __future__ import annotations

from typing import Optional

from models import db
from models.ai_analysis import MODE_FULL, AiAnalysisRun, AiWeeklyAnalysisState
from services.ai.baseline_blocks import advance_weekly_state
from services.ai.latest_result import get_latest_weekly_result
from services.ai.run_cache_source import _is_run_fresh
from utils.logger import log_print


def _requests_full_analysis(requested_mode) -> bool:
    """用户**明确**要求全量吗。

    只认 `full`（`models.ai_analysis.MODE_FULL` 那一个字面值）：`None` / `incremental`
    / 认不出来的值一律返回 False —— 这一支的默认行为是「平台自己裁决」，
    把脏值当全量的后果是**静默多花钱**（全量不看基线、不增量）。
    """
    return str(requested_mode or "").strip().lower() == MODE_FULL


def _weekly_focus_for_task(task_id, focus):
    """这次分析的范围：显式给的优先，否则读它那条 job。

    `focus` 只落在 `AiAnalysisJob.focus` 上，而任务行上有 `job_id` —— 所以执行侧
    只要拿到 `task_id` 就能把用户选的范围读回来，**不需要改
    `create_weekly_ai_analysis_task` / `register_waiting_analysis_intent` 的签名**。
    取用与判据都在 `job_service.focus_for_task`（含「任务行上那一列可能是意图 id」
    那一坑的处置）。

    读不到就返回 `None` = **不筛**：范围是**缩窄**输入的东西，读不到它只会让这次分析
    看全，不会让它看漏（看漏才是那个「静默把一半输入丢掉」的缺陷）。
    """
    if focus:
        return focus
    if task_id is None:
        return None
    try:
        from services.ai.job_service import focus_for_task

        return focus_for_task(task_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到范围不该让这次分析起不来
        log_print(
            f"⚠️ 周版本分析：读不到这次分析的范围（按「不筛」处理）: "
            f"task_id={task_id}, {type(exc).__name__}: {exc}",
            "AI",
            force=True,
        )
        return None


def _update_weekly_state(
    payload: dict,
    run: AiAnalysisRun,
    state: Optional[AiWeeklyAnalysisState],
    *,
    engine_status: Optional[str],
) -> None:
    """推进这个周版本分组的指针（**三路**，判据是引擎状态）。

    ## 为什么判据只能是 `engine_status`

    它是「模型这次**读全了没有**」的判据，而 `run.status` 回答的是「这次**交付**是什么
    形态」（succeeded / degraded / failed，见 `_persist_outcome`）—— 两把尺子。降级
    （例如「上下文索取额度用尽，基于已有证据出结论」）恰恰是最不该推进时间水位线的
    那一类：线上有一次 767 个文件里有 748 个 `.lua` 的 diff 根本没读到，却被标成
    「已分析」，增量从此只看得到水位线之后的新文件。

    ## 三路（AI-P0-02 把「一个值当三件事用」拆开了）

    * **succeeded** → 时间水位线 + 运行号 + 结论基线 + （达标时）完整覆盖指针 + 指纹；
    * **degraded** → **只推结论基线**（且必须是结构化结论）+ 指纹；时间水位线与完整覆盖
      指针都不推 —— 降级结论可以当下一轮的基线，但**不能伪装为完整覆盖**；
    * **失败 / 认不出来的状态** → 一个都不推，**连指纹也不写**（写了指纹，调度器下一轮
      就会以「输入逐字相同」跳过它，而这个版本其实一次都没跑成）。

    具体实现在 `services/ai/baseline_blocks.advance_weekly_state`（本函数只是它的薄壳）。
    """
    advance_weekly_state(payload, run, state, engine_status=engine_status)


def _reusable_conclusion(
    config_id: int, state: Optional[AiWeeklyAnalysisState]
) -> Optional[AiAnalysisRun]:
    """没有新变化时该复用的那条结论。

    **先看状态行的结论基线指针**（`last_concluded_run_id`），它才是「上一轮那份可复用的
    结论」；指针指不到或那条不可用了，才退回读侧口径（`get_latest_weekly_result`）。

    复用的前提是 `_is_run_fresh`：交付形态有结论 + 有内容 + 没过保留期 + 溯源一致。
    **复用不建 run**，所以它同时也是「没有新变化不花钱」这条验收的实现位置。
    """
    pointer = getattr(state, "last_concluded_run_id", None) if state else None
    candidate = db.session.get(AiAnalysisRun, pointer) if pointer else None
    if candidate is not None and _is_run_fresh(candidate):
        return candidate

    cached = get_latest_weekly_result(config_id)
    run_id = (cached or {}).get("run_id") if isinstance(cached, dict) else None
    fallback = db.session.get(AiAnalysisRun, run_id) if run_id else None
    if fallback is not None and _is_run_fresh(fallback):
        return fallback
    return None

