#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析记录的**保留期与复用**：缓存判定、回放、过期清理、僵尸记录收尾。

## 为什么单独一个文件

`services/ai_analysis_service.py` 贴着长度闸门（`scripts/check_file_length.py --strict`
在 2000 行报错），而「这条历史结论还能不能直接复用、该不该清理」是那个文件里**自成一体**
的一条时间线：它只碰 `AiAnalysisRun` 这张表与几个时间常量，不认识引擎、不认识提示词。

## 两件顺手一起搬的事

* `_sse_event`（4 行）与 `_json_dumps`（2 行）：回放缓存结果时要用它们，而它们留在原文件
  会让本模块反向依赖 `ai_analysis_service`（成环）。原文件的流式入口与落库路径照旧按
  旧名字取用（回导），**只有一份定义**。
* `_utcnow` 从 `project_config_source` 取 —— 那个模块是最底层的一个，不反向依赖这里。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from models import db
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun, AiAnalysisTrace
from services.ai.conclusion_view import _created_at_display, _parse_response_payload
from services.ai.project_config_source import _utcnow
from services.ai.provenance import current_provenance, provenance_matches
from utils.logger import log_print


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

ANALYSIS_CACHE_DAYS = int(os.environ.get("AI_ANALYSIS_CACHE_DAYS", "90"))

def _analysis_cache_cutoff() -> datetime:
    return _utcnow() - timedelta(days=ANALYSIS_CACHE_DAYS)

def _is_run_fresh(run: Optional[AiAnalysisRun], *, expected: Optional[dict] = None) -> bool:
    """这份历史结论现在还能不能直接复用。

    除了时间窗，还要求产生它的那套 prompt/skill/rules/model 与现在一致（见
    `provenance.current_provenance`）。不传 `expected` 时按 run 自己的项目现算。

    **失败的 run 一律不可复用。** 这一条以前漏了，后果比「显示错了」更严重：
    失败的 run 也写了 `response_text`（内容是错误文本）与 `finished_at`，溯源也在
    建 run 时就写全了，于是 `_is_run_fresh` 判它可用 → 失败记录被当成「已有结果」，
    还会被 `stream_*` 当缓存**直接回放**：用户再点一次分析，拿到的是上次的失败，
    而不是重新跑。读侧必须自己判 status，不能指望写入侧不写。
    （`_previous_run()` 一直只取 `status == "succeeded"`，说明这是本来的设计意图。）
    """
    if not run:
        return False
    if run.status != "succeeded":
        return False
    if not (run.response_text or run.response_payload):
        return False
    ts = run.finished_at or run.created_at
    if not ts:
        return False
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < _analysis_cache_cutoff():
        return False
    if expected is None and run.project_id:
        expected = current_provenance(run.project_id)
    return provenance_matches(run, expected)

def _stream_cached_run(run: AiAnalysisRun) -> Iterable[str]:
    yield _sse_event(
        "cached",
        {
            "run_id": run.id,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "created_at_display": _created_at_display(run),
            "scope": run.scope,
        },
    )
    if run.response_text:
        for line in run.response_text.splitlines():
            yield _sse_event("chunk", {"text": line})
    payload = _parse_response_payload(run.response_payload)
    if payload:
        yield _sse_event("result", payload)

def cleanup_expired_analysis_runs(retention_days: int = ANALYSIS_CACHE_DAYS):
    """清理过期的 AI 分析记录。

    返回清理条数（int，按 **run** 计）；**失败返回 None**。
    失败不返回 0：0 表示「本来就没东西可清」，两者在调用方的日志/界面上无法区分
    （同 services/excel_diff_cache_service.py::cleanup_old_cache 的说明）。

    ## 为什么必须显式删子表（这条保留策略此前等于没跑）

    `AiAnalysisTrace` / `AiAnalysisAnomaly` 的 `run_id` 外键**没有** `ON DELETE CASCADE`
    （全仓 models/ 里没有一处 ondelete），而 SQLite 的 `PRAGMA foreign_keys=ON` 在
    `utils/sqlite_config.py` 里是真的开着的（app.py:251 导入那个监听器）。于是删父行
    会直接抛 `FOREIGN KEY constraint failed`：整条 DELETE 回滚，**一条都删不掉** ——
    不是「留下孤儿行」，而是「保留策略整体失效」，run/trace 表只涨不减。

    实测（给一条过期 run 挂一行 trace）：`cleanup_expired_analysis_runs()` 返回 None
    并打印「清理AI分析缓存失败: (sqlite3.IntegrityError) FOREIGN KEY constraint failed」，
    过期 run / trace / anomaly 一行都没少。

    **先子后父**，同一个事务里做完。两张子表都要删：trace 是逐轮明细，anomaly 是异常
    条目（含人工处置状态）—— 它们都只挂在 run 上，run 一删就再也读不到
    （`AiAnalysisAnomaly.queue` 只按 run_id 查），留着就是谁都取不到的死行。
    """
    cutoff = _utcnow() - timedelta(days=retention_days)
    try:
        expired_run_ids = AiAnalysisRun.query.with_entities(AiAnalysisRun.id).filter(
            AiAnalysisRun.created_at.isnot(None)
        ).filter(AiAnalysisRun.created_at < cutoff)

        children = (
            AiAnalysisTrace.query.filter(
                AiAnalysisTrace.run_id.in_(expired_run_ids)
            ).delete(synchronize_session=False),
            AiAnalysisAnomaly.query.filter(
                AiAnalysisAnomaly.run_id.in_(expired_run_ids)
            ).delete(synchronize_session=False),
        )
        deleted = (
            AiAnalysisRun.query.filter(AiAnalysisRun.created_at.isnot(None))
            .filter(AiAnalysisRun.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        if any(children):
            log_print(
                f"🧹 随过期分析记录一并清理: {children[0] or 0} 条轮次明细，"
                f"{children[1] or 0} 条异常",
                "AI",
            )
        return int(deleted or 0)
    except Exception as exc:
        db.session.rollback()
        log_print(f"清理AI分析缓存失败: {exc}", "AI", force=True)
        return None

def fail_orphaned_analysis_runs() -> int:
    """把平台重启后遗留的 `running` 记录判为失败，返回处理条数。

    **重启是「这些 run 已经死了」的确定性证据**：持有它们的进程已经不在了，它们永远
    不会再被写完成。留在库里就是一条两头骗人的幽灵记录：

    * 读侧 `_is_run_fresh` 要求 `status == "succeeded"`，所以 `/latest` 看不见它 ——
      界面以为「这个版本从没分析过」，或者悄悄退回更早的那次成功记录；
    * 界面拿到「没有结果」就会去自动开跑一次，于是用户每次重启后点一下「AI分析」
      都会莫名跑起一次分析，而且页面上一直是「进行中」。

    为什么不等 `AiAnalysisRun.effective_status` 那 1 小时超时：那 1 小时里界面会一直
    误判，而重启已经把答案给出来了。`effective_status` 那条兜底留给另一种情况 ——
    进程活着、但某次分析真的卡死了。
    """
    try:
        orphans = AiAnalysisRun.query.filter_by(status="running").all()
        if not orphans:
            return 0
        now = _utcnow()
        for run in orphans:
            run.status = "failed"
            run.finished_at = now
            # 与 `_persist_outcome` 的失败语义一致：失败的 run 不留结论字段。
            # running 记录本来也没有结论，这里是防御性的。
            run.response_payload = None
            run.response_text = ""
            run.error_message = "平台重启，本次分析被中断（未跑完，可以重新分析）。"
        db.session.commit()
        log_print(f"重置被重启中断的 AI 分析记录: {len(orphans)} 条", "AI", force=True)
        return len(orphans)
    except Exception as exc:
        db.session.rollback()
        log_print(f"重置中断的 AI 分析记录失败: {exc}", "AI", force=True)
        return 0

def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {_json_dumps(payload)}\n\n"
