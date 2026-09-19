#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这个目标的**历次结论**：把库里那些运行的读侧形态列出来，并能取出其中任意一条。

## 为什么要有这个模块

用户报的原话是「AI 重新分析时无法预览旧的结论」—— 点「重新分析」之后，屏幕上那句
「AI 分析进行中...」就把旧报告换掉了，旧结论在页面上再也拿不到。而**结论一条都没丢**：
每条运行都落在 `ai_analysis_run` 里（`response_text` 就是那份报告）。缺的只是读的入口。

## 三条口径

1. **取数条件与 `/latest` 完全一致**（`target_type` + `target_id` / `target_key`），
   用的是**同一把尺子**（`_analysis_cache_cutoff()`，与保留策略同一天数）。列表里出现、
   而 `/latest` 已经当作过期的条目，会让人以为平台在藏东西；反过来，列表里没有、
   `/latest` 却拿得出来，那更没法解释。
2. **给人看的字在这里拼好**，前端只负责摆。三份模板各拼一遍必然漂移，而这个列表每一行
   都要出现「已有结论 / 分析失败」「仅配表仓库」这类词 —— 它们与抽屉 meta 行、导出文档的
   表头是同一批字（都来自 `services/ai/report_document.py`）。
3. **只列有结论的和失败的**。`running` / `pending` 是「还没有结论」的中间态：把它们混进
   一个叫「历次结论」的列表里，用户会点开一条看不了的东西。但**要说一声它存在**
   （`in_progress`）—— 这个弹层最常被打开的时刻就是「正在跑、想看看上一次」，那时列表
   可能是空的，界面只说「还没跑过分析」就是一句假话。

## 与 `services/ai_analysis_service.py` 的关系

读侧那几个形态函数（`_conclusion_payload` / `_last_attempt_failed_result` /
`_created_at_display` / `_focus_from_run`）住在那里面，而那个文件贴着仓库的长度闸门
（1999 行，`check_file_length.py --strict` 在 2000 行报错）—— 加不进去。

**这里直接调它们**（带下划线也调），而不是抄一份：抄一份的结果是「列表里的时间格式与
抽屉里不一样」这类漂移，而这类漂移全都不报错。取的是**同一次读**该给出的同一个形状，
这正是那组函数的用途。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from models import db
from models.ai_analysis import AiAnalysisRun
from services.ai import report_document
from services.ai_analysis_service import (
    ANALYSIS_CACHE_DAYS,
    _analysis_cache_cutoff,
    _conclusion_payload,
    _created_at_display,
    _focus_from_run,
    _in_progress_result,
    _last_attempt_failed_result,
    _parse_response_payload,
)

# 一次最多列多少条。**这不是分页**：一个目标在这个窗口里跑几十次是很少见的，真到了
# 那个量级，用户要的是「最近几次」，而不是把 90 天翻到底（`truncated` 会如实说还有更多）。
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100

# 列表里那一行摘要最多多少字。它是「一眼认出是哪一次」的线索，不是报告本身。
SUMMARY_MAX_CHARS = 80


def _first_line_of_report(text: Any) -> str:
    """报告正文的第一句「说得通的话」。

    跳过标题、空行、表格、**代码块（含块里的内容）**与纯列表符号 —— 它们不能当摘要用
    （一份报告的第一行永远是 `# 变更理解`，那是结构不是内容）。找不到就返回空串，
    由调用方如实说「这次没有可读的摘要」。
    """
    in_code = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            # 代码块的围栏要成对吃掉：只跳过围栏本身的话，块里的内容就成了「摘要」。
            in_code = not in_code
            continue
        if in_code or not line:
            continue
        if line.startswith("#") or line.startswith("|"):
            continue
        if line.startswith("- ") or line.startswith("* ") or line.startswith("> "):
            line = line[2:].strip()
            if not line:
                continue
        line = re.sub(r"[*`_]", "", line).strip()
        if not line:
            continue
        if len(line) > SUMMARY_MAX_CHARS:
            line = line[:SUMMARY_MAX_CHARS] + "…"
        return line
    return ""


def _summary(run: AiAnalysisRun, payload: Optional[Dict[str, Any]]) -> str:
    """列表里那一行摘要：**这次到底说了什么**。

    失败时给原因（那是用户点进来要看的），成功时给报告的第一句，降级时把它写在最前面
    —— 降级是「这份结论的可信度」的一部分，混在一句话后面会被漏读。
    """
    if str(run.status or "") == "failed":
        reason = str(run.error_message or "").strip()
        return f"失败：{reason}" if reason else "失败：平台上没有留下原因"

    label = str((payload or {}).get("degradation_label") or "").strip()
    body = _first_line_of_report(run.response_text)
    if not body:
        body = "这次没有留下可读的报告正文"
    return f"降级（{label}）：{body}" if label else body


def _row(run: AiAnalysisRun) -> Dict[str, Any]:
    payload = _parse_response_payload(run.response_payload) or {}
    status = run.effective_status
    risk_level = payload.get("risk_level") if status != "failed" else None
    focus_label = ""
    if run.target_type == "weekly":
        focus_label = str(_focus_from_run(run).get("label") or "")
    return {
        "run_id": run.id,
        "created_at_display": _created_at_display(run),
        "status": status,
        "status_label": report_document.status_label(status),
        "risk_label": report_document.risk_label(risk_level) if risk_level else "",
        "scope_label": report_document.scope_label(run.scope),
        "trigger_label": report_document.trigger_label(run.trigger_source),
        "focus_label": focus_label,
        "summary": _summary(run, payload),
        # 异常条数：列表里能一眼看出「哪一次报得多」，不用点进去。
        "anomaly_count": len(payload.get("anomalies") or []),
        "suppressed_count": int(payload.get("suppressed_count") or 0),
        # 这次能不能导出一份文档。**判定与导出端点同源**（同一个纯函数）——
        # 列表里给一个「导出」按钮、点下去却 409，比不给按钮更糟。
        "exportable": report_document.is_exportable(
            status=status, report_text=run.response_text
        ),
    }


def _conditions(*, kind: str, target_id: Optional[int], target_key: Optional[str]):
    """取数条件。**与 `/latest` 用的是同一组列。**

    `weekly` 只认分组键：调用方传 `target_id`（config id）时**直接报错**，而不是退回
    「`target_key IS NULL`」那一桶 —— 后者会安静地列出「一批没有分组键的周版本运行」，
    看起来像这个周版本的历史，其实谁都不是。
    """
    if kind == "weekly":
        if not target_key:
            raise ValueError("weekly 的历次结论必须按分组键（target_key）取，不是 config id")
        return (AiAnalysisRun.target_type == "weekly",
                AiAnalysisRun.target_key == target_key)
    return (AiAnalysisRun.target_type == "commit",
            AiAnalysisRun.target_id == target_id)


def list_target_runs(
    *,
    kind: str,
    target_id: Optional[int] = None,
    target_key: Optional[str] = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> Dict[str, Any]:
    """这个目标的历次结论（新的在前）。**没有任何一条结论时返回空列表，不报错。**

    `kind` 只认 `commit` / `weekly`；`weekly` 必须给 `target_key`（周版本分组键）——
    按 `target_id`（config id）查会与抽屉上那份结论对不上：读侧
    （`get_latest_weekly_result`）就是按分组键查的，同一个周版本跨配置也对得上。
    传错了一个 `ValueError` 比一份「看起来对、其实不是它的」列表好。
    """
    if kind not in ("commit", "weekly"):
        raise ValueError(f"不认识的类型：{kind}")
    size = max(1, min(int(limit or DEFAULT_HISTORY_LIMIT), MAX_HISTORY_LIMIT))
    cutoff = _analysis_cache_cutoff()

    base = AiAnalysisRun.query.filter(
        *_conditions(kind=kind, target_id=target_id, target_key=target_key),
        AiAnalysisRun.created_at >= cutoff,
        # 中间态不进这个列表（见模块 docstring 第 3 条）。
        AiAnalysisRun.status.in_(("succeeded", "failed")),
    )
    total = base.count()
    runs = (
        base.order_by(AiAnalysisRun.created_at.desc(), AiAnalysisRun.id.desc())
        .limit(size)
        .all()
    )
    # 中间态不进列表（见模块 docstring 第 3 条），但**要说一声它存在**：这个弹层最常被
    # 打开的时刻正是「正在跑、想看看上一次」——那时列表可能是空的，界面若只说
    # 「这个目标还没有跑过分析」，就是一句假话。
    in_progress = (
        AiAnalysisRun.query.filter(
            *_conditions(kind=kind, target_id=target_id, target_key=target_key),
            AiAnalysisRun.created_at >= cutoff,
            AiAnalysisRun.status.in_(("running", "pending")),
        ).count()
        > 0
    )
    return {
        "success": True,
        "kind": kind,
        "runs": [_row(run) for run in runs],
        "total": total,
        "truncated": total > len(runs),
        "limit": size,
        "in_progress": in_progress,
        # 窗口天数如实给出来：读的人要知道「更早的看不到」是保留策略，不是平台没跑过。
        "window_days": ANALYSIS_CACHE_DAYS,
    }


def get_run_report(run_id: int) -> Optional[Dict[str, Any]]:
    """某一次运行的读侧形态 —— **与 `/latest` 同一个形状**（一个渲染器通吃）。

    有结论 → `_conclusion_payload`（含 `result.anomalies` 与那份报告）；
    失败 → `_last_attempt_failed_result`（给原因、不给结论）；
    还在跑 / 排队中 → `_in_progress_result`（**不许走有结论那条**：库里那条 running 记录
    上可能挂着上一次尝试留下的 `response_payload`，把它当成「这次的结论」显示出来，
    就是「分析中」与一份报告同时出现在屏幕上）；
    查不到 → `None`（调用方回 404）。
    """
    run = db.session.get(AiAnalysisRun, run_id) if run_id else None
    if run is None:
        return None
    status = run.effective_status
    if status == "failed":
        return _last_attempt_failed_result(run)
    if status in ("running", "pending"):
        return _in_progress_result(run)
    payload = _conclusion_payload(run)
    if run.target_type == "weekly":
        payload["focus"] = _focus_from_run(run)
    return payload
