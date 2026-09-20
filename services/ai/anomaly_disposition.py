#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工处置一条结论：`AiAnalysisAnomaly.disposition` 的**写路径**。

## 为什么需要这一层（它此前整棵树都不存在）

`disposition` 这一列从建表起就有完整的读侧 —— `baseline_source.baseline_findings`
把它读进 `BaselineFinding`，`baseline.classify` 据此把「已忽略且相关文件没有再变」的
结论判成 `suppressed`，`result_payload` 再把它们从下一轮的结论清单里剔掉。整条链路
是通的，**唯一缺的是「谁来把它设成 ignored」**。于是那一列永远是建表时的默认值
`pending`，上面整条链路一次都没生效过：用户在界面上找不到任何地方说「这条我确认过 /
这条不用管」，模型下一轮照样把同一条原样再报一遍。

这不是「少一个接口」，是**一个已经写好的能力没有任何入口**。所以本模块与路由一起
补齐，并且配套一条「读得回来」的接口（`/ai-analysis/runs/<id>/anomalies`）——
只写不读的话，用户处置完刷新页面会看到所有条目又变回「待确认」。

## 为什么单独一个模块，而不是塞进 `ai_analysis_service`

`ai_analysis_service.py` 已经顶到文件长度上限（1800 行 WARN / 2000 行 ERROR，
见 `scripts/check_file_length.py`）。而且这两件事的读者不同：那里是「跑一次分析」，
这里是「人改一条记录」。

## 与 `rules.anomaly_fingerprint` 的分工

处置状态挂在**行**上，而新一轮的继承靠**指纹**（`anomaly_fingerprint`）：
模型每次重跑措辞都会略有不同，靠标题原文匹配不上。指纹在写库时就落进每一行
（`ai_analysis_service._persist_outcome`），本模块不重新计算它 —— 算两遍就会有两套
口径（比如标点归一化的规则改了，历史行的指纹与新算的对不上，处置静默失效）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from models import db
from models.ai_analysis import AiAnalysisAnomaly
from models.ai_analysis.anomaly import DEFAULT_DISPOSITION, DISPOSITION_LABELS, DISPOSITIONS
from utils.logger import log_print

# 备注的长度上限。**必须有**：这是自由文本，界面上是一个 textarea，
# 而它是会随 `baseline_digest` 进提示词的（见 `services/ai/baseline.py`）——
# 一段几万字的备注会把提示词预算吃掉一大块，而它本来只是给「为什么忽略」留一句话。
DISPOSITION_NOTE_MAX_CHARS = 500
NOTE_TRUNCATION_MARK = "…（已截断）"

# 一次请求里允许处置的最大条数（批量接口用）。定成 200 是为了让「把这一页全标成已忽略」
# 这种操作一次发得完，同时挡住「一次几万条」把事务拖长。
MAX_BATCH_ITEMS = 200


class DispositionError(ValueError):
    """处置请求不合法。

    **不降级、不猜**（与 `rules.RulesConfigError` 同一条纪律）：把不认识的
    `disposition` 悄悄退回 `pending`，会让「用户以为他标了已忽略、实际什么都没发生」
    变成一个没有任何痕迹的错误 —— 而下一轮那条结论会**再次**冒出来，用户只会觉得
    「这平台的忽略功能坏了」，查不到真正的原因。
    """


def normalize_disposition(raw: Any) -> str:
    """把请求里的处置值收成合法码值。不认识就抛 `DispositionError`。"""
    value = str(raw or "").strip().lower()
    if value not in DISPOSITIONS:
        raise DispositionError(
            f"处置状态只能是 {'、'.join(DISPOSITIONS)} 之一"
            f"（对应 {'、'.join(DISPOSITION_LABELS[item] for item in DISPOSITIONS)}），"
            f"实际收到 {raw!r}"
        )
    return value


def normalize_note(raw: Any) -> str:
    """备注：去掉首尾空白，超过上限**截断并标记**（不静默丢字符）。

    超长不报错而截断，是因为它多半来自粘贴了一整段会议记录 —— 为此让整次处置失败，
    用户会以为「忽略」这个动作本身坏了。截断与否由调用方另外回报（`note_truncated`），
    **标记里不带原长度**：带了的话这个函数就不是幂等的（第二次调用会把第一次写下的
    长度当成原长度），而它会被批量处置那条路径对着同一段文本调用两次。
    """
    text = str(raw or "").strip()
    if len(text) <= DISPOSITION_NOTE_MAX_CHARS:
        return text
    return text[:DISPOSITION_NOTE_MAX_CHARS] + NOTE_TRUNCATION_MARK


def note_was_truncated(raw: Any) -> bool:
    return len(str(raw or "").strip()) > DISPOSITION_NOTE_MAX_CHARS


def set_disposition(
    anomaly: AiAnalysisAnomaly,
    *,
    disposition: Any,
    note: Any = "",
    username: str = "",
    now: Optional[datetime] = None,
) -> AiAnalysisAnomaly:
    """把一条结论的处置状态改成 `disposition` 并记下是谁、什么时候。

    四个字段是**一个整体**，描述的是「当前这一次处置」：

    * 改成 `confirmed` / `ignored` → 记下处置人、时间、备注；
    * 改回 `pending`（撤销处置）→ **三个伴随字段一起清空**。留着上一轮的备注会让
      「待确认」这条读起来像已经有了结论 —— 而那正是最容易让人跳过它的一种错觉。

    撤销也要留痕，所以这里额外写一行日志。**没有落库的审计表**（谁在什么时候撤销了
    处置）是有意为之：为一个「改错了点回来」的动作建一张表，收益远小于它的维护成本；
    真正需要追溯的是当前状态，而当前状态在行上。
    """
    target = normalize_disposition(disposition)
    previous = anomaly.disposition or DEFAULT_DISPOSITION
    stamp = now or datetime.now(timezone.utc)

    anomaly.disposition = target
    if target == DEFAULT_DISPOSITION:
        anomaly.disposition_by = None
        anomaly.disposition_at = None
        anomaly.disposition_note = None
    else:
        anomaly.disposition_by = (username or "")[:100]
        anomaly.disposition_at = stamp
        anomaly.disposition_note = normalize_note(note) or None

    if target == DEFAULT_DISPOSITION and previous != DEFAULT_DISPOSITION:
        log_print(
            f"AI 结论处置撤销：anomaly={anomaly.id}（{anomaly.title}）"
            f"由 {previous} 改回 {DEFAULT_DISPOSITION}，操作人={username or '-'}",
            "AI",
            force=True,
        )
    return anomaly


def anomalies_of_run(
    run_id: int,
    *,
    disposition: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[AiAnalysisAnomaly]:
    """某一次运行的结论行，按严重度优先排序（与报告里的顺序一致）。

    `disposition` 为 None 时不过滤。取值由调用方先过 `normalize_disposition`
    ——这里不再兜一次，两处各判一次必然长出两种口径。
    """
    query = AiAnalysisAnomaly.query.filter_by(run_id=run_id)
    if disposition is not None:
        query = query.filter_by(disposition=disposition)
    rows = query.all()
    # 严重度排序走 `rules.SEVERITY_RANK`，与报告里那份清单同一套口径：
    # 界面上下两处各排一次、次序不同，读的人会以为看的是两份清单。
    from services.ai.rules import CONFIDENCE_RANK, SEVERITY_RANK

    rows.sort(
        key=lambda row: (
            -SEVERITY_RANK.get(row.severity or "", 0),
            -CONFIDENCE_RANK.get(row.confidence or "", 0),
            row.id,
        )
    )
    return rows[:limit] if limit else rows


def set_many(
    rows: list[AiAnalysisAnomaly],
    *,
    disposition: Any,
    note: Any = "",
    username: str = "",
    now: Optional[datetime] = None,
) -> int:
    """批量处置。返回改动的条数（**同一状态的不计入**，见下）。

    计数只算真的变了的那些：界面上那句「已处置 N 条」如果按「提交了几条」来报，
    用户重复点一次会看到「已处置 5 条」而其实一条都没动 —— 与「保存成功」却什么都没
    存是同一类谎话。
    """
    target = normalize_disposition(disposition)
    text = normalize_note(note)
    changed = 0
    for row in rows:
        before = (row.disposition or DEFAULT_DISPOSITION, row.disposition_note or "")
        set_disposition(row, disposition=target, note=text, username=username, now=now)
        after = (row.disposition or DEFAULT_DISPOSITION, row.disposition_note or "")
        if before != after:
            changed += 1
    db.session.commit()
    return changed
