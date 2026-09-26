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
from models.ai_analysis import (
    AiAnalysisAnomaly,
    AiAnalysisRoundEvent,
    AiAnalysisRun,
    AiAnalysisTrace,
)
from services.ai.conclusion_view import _created_at_display, _parse_response_payload
from services.ai.project_config_source import _utcnow
from services.ai.provenance import current_provenance, provenance_matches
from utils.logger import log_print


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

ANALYSIS_CACHE_DAYS = int(os.environ.get("AI_ANALYSIS_CACHE_DAYS", "90"))

# 「这次**交付**是有结论的」那两种形态 —— 读侧共用的唯一一份口径。
#
# 为什么单独一个常量：`succeeded` 与 `degraded` 必须在**每一条**读路径上同进同出
# （`/latest`、缓存回放、基线、历次结论、导出、用量面板），而这六处的判据分别写在五个
# 文件里。每个地方各写一遍 `("succeeded", "degraded")`，就是「同一句话在多处各自演化」
# —— 加第四种状态时必然漏掉一处，而漏掉的症状各不相同（转圈 / 不再回放 / 假 stale /
# 基线清空 / 历史消失 / 导出倒退），没有一条会自己报错。
#
# 它**不回答**「模型读全了没有」：那件事看引擎状态（`outcome.status` /
# `result["status"]`），`run.status` 只回答交付形态。降级归降级，能不能看是另一回事。
#
# 放在这里（而不是 `models/`）：本模块是「这份结论还能不能复用」的判据所在，也就是
# 「有结论」这件事被问得最严格的地方 —— 这个常量正是从那句判据里长出来的。
CONCLUDED_STATUSES = ("succeeded", "degraded")

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

    ## `degraded` 也算「现成的结论」（这里原来只认 succeeded）

    `run.status` 回答的是「这次**交付**是什么形态」：`degraded` = **有结论但浅**
    （有报告正文、有结构化结论形态），**与 succeeded 同等对待**；`failed` 才是「没有
    结论」。写入侧已经原生区分了这三态，而这里原先是 `run.status != "succeeded"` ——
    于是「这份结论还能不能直接复用」对降级运行一律为假：`/latest` 第 1 步失效、
    `stream_*` 不再回放，**用户再点一次分析就重新花一次钱**。

    「模型读全了没有」在这里判不出来，也不该在这里判 —— 那要看引擎状态
    （`outcome.status` / `result["status"]`）。降级归降级，复用归复用。
    """
    if not run:
        return False
    # 有结论的两种交付形态。`running` / `pending` / `failed` 一律不是「现成的结论」。
    if run.status not in CONCLUDED_STATUSES:
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

def remove_analysis_runs(run_ids) -> Optional[dict]:
    """删除这些运行**及其全部子表行**，并把受影响分组的结论基线指针修正一遍。

    返回 `{"runs", "traces", "anomalies", "events"}` 四个计数；**失败返回 None**。
    失败不返回全 0 的字典：那与「本来就没有可删的行」在读的人眼里是同一个形状
    （同 `cleanup_expired_analysis_runs` 的说明）。

    ## 为什么是**按 id 删的那条路上唯一一处**实现

    「删一条分析记录」有两个入口：**保留期清理**（`cleanup_expired_analysis_runs`）与
    **用户删掉某一条历次结论**（`ai_report_history_service.delete_target_run`）。三张子表
    的先子后父顺序、`AiAnalysisRoundEvent` 那张没有外键的表（漏删的症状是 run id 复用后
    下一次运行读到幽灵行，见 `cleanup_expired_analysis_runs` 的 docstring）、以及**指针
    修正**，两处各写一遍的话，加第四张子表时必然只加一处 —— 而漏掉的那一处不会报错。

    **另一条路是「删光全部」（`usage_statistics.purge_usage_statistics`）**：它是全表
    DELETE、没有 id 列表，所以走不到这里。那条路自己也是这样删三张子表的，
    两条路都漏过一次第三张（2026-09-26 复核才发现 purge 那处）—— 这正是
    `tests/test_ai_run_deletion.py` 里那条**跟着 schema 走的护栏**存在的原因：任何一张
    带 `run_id` 的表，两条路都必须点到名。

    ## 指针修正在同一个事务里

    删完行、写指针、`commit` —— 中间任何一步抛异常都整笔回滚（调用方看到 `None`，
    run 还在）。所以 `refresh_concluded_pointers_for_removed_runs` **自己不许 commit**
    （它确实没有）。

    ## 为什么在删除**之前**把 run 行读出来

    指针要按分组键重算，而删完就查不到「这几条属于哪个分组」了。读出来的那一批也跟着
    交给修正函数（它只取 `target_type` / `target_key` / `id`）。
    """
    ids = []
    for value in run_ids or ():
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))
    if not ids:
        # **不是失败**：没有要删的行，本来就是 0。
        return {"runs": 0, "traces": 0, "anomalies": 0, "events": 0}
    try:
        # 先读出来（分组键要在删之前拿到）—— 这一批也是下面修正函数的输入。
        #
        # **只取三列，不取实体对象**：把 `AiAnalysisRun` 的实体读进 session 会让下面那次
        # flush 去同步 run ↔ trace/anomaly 的关系（两个模型上都有 `backref`），而父行已经
        # 被批量 DELETE 掉了 —— 于是子表的 `run_id` 被回填成 NULL：
        # `NOT NULL constraint failed: ai_analysis_anomaly.run_id`，**一次成功的删除以 500
        # 收场**。列查询不进 identity map，这条关系同步根本不发生。
        rows = (
            db.session.query(
                AiAnalysisRun.id, AiAnalysisRun.target_type, AiAnalysisRun.target_key
            )
            .filter(AiAnalysisRun.id.in_(ids))
            .all()
        )
        # 三张子表：前两张不删会被 FK 顶回来（整条 DELETE 回滚，一条都删不掉），
        # 第三张没有外键、不删不报错但会留下幽灵行。
        children = (
            AiAnalysisTrace.query.filter(AiAnalysisTrace.run_id.in_(ids)).delete(
                synchronize_session=False
            ),
            AiAnalysisAnomaly.query.filter(AiAnalysisAnomaly.run_id.in_(ids)).delete(
                synchronize_session=False
            ),
            AiAnalysisRoundEvent.query.filter(AiAnalysisRoundEvent.run_id.in_(ids)).delete(
                synchronize_session=False
            ),
        )
        deleted = AiAnalysisRun.query.filter(AiAnalysisRun.id.in_(ids)).delete(
            synchronize_session=False
        )
        _forget_rows_deleted_outside_the_session(ids)
        # **函数内 import**：`baseline_blocks` 在模块级 import 本模块（`_json_dumps`），
        # 模块级反向 import 会成环。
        from services.ai import baseline_blocks

        baseline_blocks.refresh_concluded_pointers_for_removed_runs(rows)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log_print(f"删除AI分析记录失败: {exc}", "AI", force=True)
        return None
    if any(children):
        log_print(
            f"🧹 随删掉的分析记录一并清理: {children[0] or 0} 条轮次明细，"
            f"{children[1] or 0} 条异常，{children[2] or 0} 条轮次事件",
            "AI",
        )
    return {
        "runs": int(deleted or 0),
        "traces": int(children[0] or 0),
        "anomalies": int(children[1] or 0),
        "events": int(children[2] or 0),
    }


def _forget_rows_deleted_outside_the_session(ids) -> None:
    """把已经被批量 DELETE 掉的行**从 session 里摘出去**（`expunge`）。

    `Query.delete(synchronize_session=False)` 只发 SQL，不告诉 ORM 哪几行没了。而这些
    对象可能仍在 session 里 —— 调用方 `db.session.get` 过、测试里刚造过、路由刚刚读过。
    下一次 flush 时，`AiAnalysisAnomaly.run` / `AiAnalysisTrace.run` 这两个 `backref`
    会让 ORM 用父对象去回填子表的 `run_id`，而父行已经不在库里，于是子表被写成
    `run_id=NULL` → `NOT NULL constraint failed` —— **一次成功的删除以 500 收场**。

    ## 只扫 `identity_map` 就够 —— 但理由不是「pending 不会出事」

    这里**只扫 persistent 对象**（`identity_map`），**不扫 `session.new`**。这是查证过
    的取舍，不是漏写：

    * 本函数进门前必然先跑过一次查询（读「要删哪几行」），那次查询的**自动 flush** 会把
      调用方挂着的 pending 行先 INSERT 进去 —— 那时父行还在，插得进去；紧接着的批量
      DELETE 再把它连同父行一起带走。所以 pending 那一档在这个调用序列里不会留下
      `run_id` 悬空的行，扫它等于永远扫不到东西。
    * 真去 `expunge(session.new)` 反而更糟：那等于**替调用方取消一次插入** —— 他 `add`
      的那行会静默消失，而删除本来只该删「已经存在的那几行」。
    * 要出事只有一种写法：关掉 autoflush（`with db.session.no_autoflush:`）之后先 `add`
      子行、再删父行。本仓库没有这种调用点；真出现了，`tests/test_ai_run_deletion.py`
      里那条「挂一行未提交的子表行也能删掉」会先红。

    摘出去之后它们只是「脱离 session」的对象：库里那一行确实没了，这与事实相符。
    """
    keep = set(int(value) for value in ids)
    for obj in list(db.session.identity_map.values()):
        if not isinstance(
            obj, (AiAnalysisRun, AiAnalysisTrace, AiAnalysisAnomaly, AiAnalysisRoundEvent)
        ):
            continue
        own_run_id = obj.id if isinstance(obj, AiAnalysisRun) else obj.run_id
        try:
            key = int(own_run_id)
        except (TypeError, ValueError):
            continue
        if key in keep:
            db.session.expunge(obj)


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

    **先子后父，三张子表都要删** —— 具体做法与理由都在 `remove_analysis_runs` 里
    （那是唯一一处实现），本函数只负责「哪些算过期」。

    ## 指针：保留期清理也要修（2026-09-26 补）

    原先这里删完就走，`ai_weekly_analysis_state.last_concluded_run_id` 留着**指向一行
    已经不存在的 run**。后果不是「界面上多了个坏 id」，而是：`_decide_effective_mode`
    只看「指针是不是 NULL」，于是下一次「增量」**不被升成全量**、也没有升级原因，
    而快照不会被清 —— 用户点增量、看到的是一份来路不明的报告。现在与用户删除走
    同一条路（`remove_analysis_runs` 里的指针修正）。
    """
    cutoff = _utcnow() - timedelta(days=retention_days)
    expired_run_ids = [
        row.id
        for row in AiAnalysisRun.query.with_entities(AiAnalysisRun.id)
        .filter(AiAnalysisRun.created_at.isnot(None))
        .filter(AiAnalysisRun.created_at < cutoff)
        .all()
    ]
    if not expired_run_ids:
        return 0
    deleted = remove_analysis_runs(expired_run_ids)
    if deleted is None:
        # 失败的那一句日志由 `remove_analysis_runs` 打（它带着真正的异常），这里只把
        # 「谁在清」补上 —— 两处都打会在日志里出现两条看起来不相干的失败。
        log_print(f"清理AI分析缓存失败（保留 {retention_days} 天）", "AI", force=True)
        return None
    return int(deleted["runs"])

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
