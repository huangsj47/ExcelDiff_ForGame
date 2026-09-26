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
   「有结论」是 `run_cache_source.CONCLUDED_STATUSES`（succeeded **与 degraded**）：
   降级那次的报告正文是真的，而且它在地化为原生 `degraded` 之前就一直在列表里。

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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import and_, func, or_

from models import db
from models.ai_analysis import (
    IN_FLIGHT_STATUSES,
    STALE_RUNNING_SECONDS,
    AiAnalysisRun,
    AiWeeklyAnalysisState,
)
from services.ai import report_document
from services.ai.run_cache_source import CONCLUDED_STATUSES, remove_analysis_runs
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
from utils.logger import log_print

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
    if str(run.effective_status or "") == "failed":
        reason = str(run.error_message or "").strip()
        if not reason and run.status == "running":
            # 僵尸 running：它不是「失败了但没留原因」，是**没跑完**。这两句话用户
            # 能做的事不一样（前者看原因、后者重跑），所以分开说。
            return "失败：这次运行没有跑完（进程中断），平台上没有留下原因"
        return f"失败：{reason}" if reason else "失败：平台上没有留下原因"

    label = str((payload or {}).get("degradation_label") or "").strip()
    body = _first_line_of_report(run.response_text)
    if not body:
        body = "这次没有留下可读的报告正文"
    return f"降级（{label}）：{body}" if label else body


def _removable_reason(run: AiAnalysisRun) -> str:
    """这条运行**本身**能不能删（不看权限）；`""` = 可以，其余是拒绝原因。

    判据与服务端的拒绝分支**必须同源**（`delete_target_run` 用的就是它），否则会出现
    「按钮在、点下去被拒」或者更糟的「按钮不在、其实删得掉」。

    * **只有周版本**：单提交那一条的产品口径就是不开删除，写在这个判据里而不是写在路由里；
    * **在途不许删**：那条进程还会回写结果，删掉之后它踩到已删的行，报出来的是「分析失败」
      而不是「你刚删过它」（同 `purge_usage_statistics` 拒绝在途那一档的理由）。

    **在途那一档取 `effective_status`**（僵尸 running 在界面上就是 failed），与全量重置
    刻意取不同的那一把尺子 —— 理由见 `models/ai_analysis/analysis_run.py`
    `IN_FLIGHT_STATUSES` 的注释：这里问的是「用户看到的是不是一条失败的记录」，按库里
    原值拒的话，用户会对着一条写着「分析失败」的记录点删除、被回绝说「它还在跑」。
    """
    if str(run.target_type or "") != "weekly":
        return "not_weekly"
    if str(run.effective_status or "") in IN_FLIGHT_STATUSES:
        return "in_flight"
    return ""


def _deletable(run: AiAnalysisRun, *, can_delete: bool) -> bool:
    """界面上这一行要不要摆删除按钮。`can_delete` 是权限（路由算好传进来的）。"""
    return bool(can_delete) and not _removable_reason(run)


def _row(run: AiAnalysisRun, *, can_delete: bool = False, baseline_run_id: Optional[int] = None) -> Dict[str, Any]:
    # `_parse_response_payload` 只挡「JSON 坏了」，不挡「是合法 JSON 但不是对象」
    # （`[1, 2]` / `"x"` 都能解析成功）。那种行上 `.get()` 会 AttributeError，
    # **一条坏行让整个列表 500** —— 而同一个提交里路由的 `_payload_of` 与
    # `report_document.anomaly_rows` 都判了类型，这里漏了。
    payload = _parse_response_payload(run.response_payload)
    if not isinstance(payload, dict):
        payload = {}
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
        # 能不能删（判据见 `_deletable`，与 `delete_target_run` 同源）。界面按它决定
        # 要不要摆那个按钮 —— 不做「摆了再拒绝」。
        "deletable": _deletable(run, can_delete=can_delete),
        # 「下一次增量会继承这一条的结论」。删它要提醒一句基线会跟着退，所以这个标记
        # 得跟着列表一起给。它按**分组状态行上的指针**判，不是按「时间最近的那条」
        # （两者在既有的历史数据里本来就可能分歧，见 `refresh_concluded_pointer`）。
        "is_baseline": baseline_run_id is not None
        and int(run.id) == int(baseline_run_id),
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


def _zombie_clause(now: datetime):
    """僵尸 running（进程被杀留下的）在 SQL 里的判据 —— 与 `AiAnalysisRun.is_stale_running` 同一把尺子。

    **为什么要在 SQL 里重写一遍**：`effective_status` 是 Python 属性，`filter()` 用不上。
    而三处读路径必须一致：`/progress` 与 `/runs/<id>/report` 按 `effective_status` 把它
    报成失败，`/latest` 也按它（`_read_latest_result` 第 4 步）。列表这边若还按原值
    `status == "running"` 判，同一个目标就会出现：弹层说「正在分析、还没有结论可翻」
    （永远），抽屉说「上次分析失败」，`/progress` 说失败 —— 同一条记录，三个入口三句话。

    不改库里的值（那个进程可能只是慢）：只是不给用户看一个永远停在「分析中」的界面。
    """
    reference = func.coalesce(AiAnalysisRun.started_at, AiAnalysisRun.created_at)
    cutoff = now.replace(tzinfo=None) - timedelta(seconds=STALE_RUNNING_SECONDS)
    return and_(AiAnalysisRun.status == "running", reference < cutoff)


def list_target_runs(
    *,
    kind: str,
    target_id: Optional[int] = None,
    target_key: Optional[str] = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    can_delete: bool = False,
) -> Dict[str, Any]:
    """这个目标的历次结论（新的在前）。**没有任何一条结论时返回空列表，不报错。**

    `kind` 只认 `commit` / `weekly`；`weekly` 必须给 `target_key`（周版本分组键）——
    按 `target_id`（config id）查会与抽屉上那份结论对不上：读侧
    （`get_latest_weekly_result`）就是按分组键查的，同一个周版本跨配置也对得上。
    传错了一个 `ValueError` 比一份「看起来对、其实不是它的」列表好。

    `can_delete` 由路由算好（项目管理员），**不由这里猜**：权限是路由的事，这一层只
    负责按它算出每一行的 `deletable`。默认 `False` —— 忘了传的调用点得到的是「不能删」，
    而不是一个谁都能删的界面。
    """
    if kind not in ("commit", "weekly"):
        raise ValueError(f"不认识的类型：{kind}")
    size = max(1, min(int(limit or DEFAULT_HISTORY_LIMIT), MAX_HISTORY_LIMIT))
    cutoff = _analysis_cache_cutoff()
    zombie = _zombie_clause(datetime.now(timezone.utc))
    # 结论基线指针（哪一条是「下一轮增量会继承的那份结论」）。**一次查询**，不是每条 run
    # 查一次：列表最多 100 行。
    baseline_run_id = (
        _concluded_pointer(target_key) if kind == "weekly" and target_key else None
    )

    base = AiAnalysisRun.query.filter(
        *_conditions(kind=kind, target_id=target_id, target_key=target_key),
        AiAnalysisRun.created_at >= cutoff,
        # 中间态不进这个列表（见模块 docstring 第 3 条）——**僵尸 running 例外**：
        # 它按 `effective_status` 就是一次失败，用户要能在「为什么失败」那一档里翻到它。
        #
        # `degraded` 与 `succeeded` 一样是**有结论**（见 `run_cache_source.CONCLUDED_STATUSES`）：
        # 它有报告正文，用户要能在「历次结论」里翻到它 —— 漏掉它这条运行就从列表上整条
        # 消失，而它在地化之前是存成 succeeded 的，也就是**本来就在列表里**。
        or_(
            AiAnalysisRun.status.in_(CONCLUDED_STATUSES + ("failed",)),
            zombie,
        ),
    )
    total = base.count()
    runs = (
        base.order_by(AiAnalysisRun.created_at.desc(), AiAnalysisRun.id.desc())
        .limit(size)
        .all()
    )
    orphan = _latest_failure_outside_the_window(
        kind=kind, target_id=target_id, target_key=target_key, listed=runs
    )
    if orphan is not None:
        runs = [orphan] + runs
        total += 1
    # 中间态不进列表（见模块 docstring 第 3 条），但**要说一声它存在**：这个弹层最常被
    # 打开的时刻正是「正在跑、想看看上一次」——那时列表可能是空的，界面若只说
    # 「这个目标还没有跑过分析」，就是一句假话。
    #
    # **僵尸 running 不算「正在跑」**：它在界面上是失败（`effective_status`），
    # 让它留在这一档会让弹层永远说「正在分析」（见 `_zombie_clause`）。
    in_progress = (
        AiAnalysisRun.query.filter(
            *_conditions(kind=kind, target_id=target_id, target_key=target_key),
            AiAnalysisRun.created_at >= cutoff,
            or_(
                AiAnalysisRun.status == "pending",
                and_(AiAnalysisRun.status == "running", ~zombie),
            ),
        ).count()
        > 0
    )
    return {
        "success": True,
        "kind": kind,
        "runs": [
            _row(run, can_delete=can_delete, baseline_run_id=baseline_run_id)
            for run in runs
        ],
        "total": total,
        "truncated": total > len(runs),
        "limit": size,
        "in_progress": in_progress,
        # 窗口天数如实给出来：读的人要知道「更早的看不到」是保留策略，不是平台没跑过。
        "window_days": ANALYSIS_CACHE_DAYS,
        # 服务端算好的权限（界面只照着摆，不自己判）。**也回给调用方**：删完之后要
        # 原地重画这份列表，用它接着画。
        "can_delete": bool(can_delete and kind == "weekly"),
        # 这个分组当前的分组键 —— 删除请求要把它带回来（`expected_target_key`），
        # 免得「看着版本 A 的历史、删掉的却是版本 B 的某一条」。
        "target_key": target_key if kind == "weekly" else None,
    }


def _latest_failure_outside_the_window(
    *,
    kind: str,
    target_id: Optional[int],
    target_key: Optional[str],
    listed: Sequence[AiAnalysisRun],
) -> Optional[AiAnalysisRun]:
    """窗口外那条「/latest 仍会拿出来」的失败记录（没有就返回 None）。

    **为什么单独补这一条**：`_read_latest_result` 的最后一步是「一条结论都没有时，
    退回最近一次失败」—— 而它**不看窗口**（它要说的是「最近一次没跑成」，与结论能不能
    复用是两件事）。列表这边被窗口挡在外面，于是同一个目标出现两种说法：
    抽屉里写着「上次分析失败：<原因>」，弹层里写着「这个目标还没有跑过分析」。

    只补这一种情形，不把整张列表的窗口放开：窗口的作用是「更早的别翻了」，
    而 `/latest` 只承诺「最近一次失败看得见」。
    """
    newest = (
        AiAnalysisRun.query.filter(
            *_conditions(kind=kind, target_id=target_id, target_key=target_key)
        )
        .order_by(AiAnalysisRun.created_at.desc(), AiAnalysisRun.id.desc())
        .first()
    )
    if newest is None or newest.effective_status != "failed":
        return None
    if any(row.id == newest.id for row in listed):
        # 已经在列表里（窗口内）—— 那本来就是它。
        return None
    return newest


def _concluded_pointer(target_key: Optional[str]) -> Optional[int]:
    """这个分组当前的分组状态行上那个「结论基线」指针（没有就是 `None`）。

    不反查 `baseline_source.previous_run`：`is_baseline` 这一栏要标的是**平台自己认的
    那一条**（`job_service._decide_effective_mode` 就是读它决定增量还是全量的），
    拿「按时间挑出来的那条」来标会在两者分歧时说错话 —— 而分歧是既有的、合法的
    （用户点过全量、或那条结论没被标成结构化）。
    """
    if not target_key:
        return None
    state = AiWeeklyAnalysisState.query.filter_by(group_key=str(target_key)).first()
    value = getattr(state, "last_concluded_run_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def delete_target_run(
    run_id: int, *, expected_target_key: Optional[str] = None, actor: str = ""
) -> tuple[str, str, Dict[str, Any]]:
    """删掉一条历次结论。返回 `(原因, 一句话, 附加信息)`；`原因` 为 `""` 表示成功。

    ## 为什么返回三元组而不是抛异常

    调用方是路由，它要把**每一档拒绝**翻译成一个 HTTP 码（404 / 403 / 400 / 409）。
    抛异常的话，路由得去把异常类型再映射回状态码 —— 那等于把状态码的判断写在两层里。
    这一层**不 import flask**（本模块全程如此），所以它只能给原因字符串。

    ## 为什么带 `expected_target_key`

    弹层是「同一个抽屉换目标」（合并视图就是这样）：列表属于版本 A，用户切到版本 B
    之后列表还没刷回来时点删除，删掉的会是 A 的某一条 —— 而界面上看的是 B。带上
    「我看到的这份列表是哪个分组的」，服务端一比就知道该不该拒。

    ## 拒绝的顺序（先问「这条存在吗」，再问「你是谁」）

    存在性放最前面：一条已经不存在的结论，回 404 比回 403 准确（而且它不泄露权限）。
    """
    run = db.session.get(AiAnalysisRun, run_id) if run_id else None
    if run is None:
        return "not_found", "这条结论已经不存在了（可能刚被别的管理员删掉）。", {}
    key = str(run.target_key or "")
    wanted = str(expected_target_key or "")
    if wanted and str(run.target_type or "") == "weekly" and wanted != key:
        return (
            "target_mismatch",
            "这条结论不属于你正在查看的版本（列表可能没刷新，请重新打开历次结论）。",
            {},
        )
    reason = _removable_reason(run)
    if reason == "not_weekly":
        return "not_weekly", "只有周版本的历次结论支持删除。", {}
    if reason == "in_flight":
        return (
            "in_flight",
            "这次分析还在进行中，不能删除 —— 等它跑完（或失败）再删。",
            {"status": run.effective_status},
        )
    # 那句回执里要带时间（用户刚删掉的那一行长什么样）。**先取再删**：删完之后对象还在
    # 内存里，但读一个已经不在库里的行来拼界面文案，是下一处「删完才发现取不到」的来源。
    when = _created_at_display(run)
    result = remove_analysis_runs([run.id])
    if result is None:
        return "failed", "删除失败：数据库没有接受这次删除，请稍后重试。", {}
    # 审计那一句打在这里（服务层），不打在路由：路由这一层不做业务判断，而「谁删了
    # 哪一条」是这件事本身的账（同 `purge_usage_statistics` 的 `updated_by`）。
    log_print(
        f"🗑️ 删掉一条 AI 历次结论：run={run.id} 分组={key or '-'}"
        f"（操作人：{actor or '未知'}）",
        "AI",
        force=True,
    )
    return "", f"已删除 {when} 那一次结论。", {"run_id": run.id, **result}


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
