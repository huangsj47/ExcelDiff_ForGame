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
from typing import Any, Mapping, Optional

from models import db
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from models.ai_analysis.anomaly import (
    DEFAULT_DISPOSITION,
    DISPOSITION_LABELS,
    DISPOSITIONS,
    AiAnalysisIgnoredFingerprint,
)
from services.ai.baseline import DISPOSITION_IGNORED
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


# ---------------------------------------------------------------------------
# 人工忽略的**持久台账**（P0 真漏口）
# ---------------------------------------------------------------------------
# 为什么处置之外还要多写一张表：`disposition` 挂在**行**上，而被忽略的条目交付时会被
# `result_payload` 从 `anomalies` 里剔掉 —— 于是「下一条运行」的行里根本没有它。基线
# 读侧又只读「最近一次有结构化结论的运行」那一批行，中间夹一次「有结论但 0 行」的运行，
# 人工的判断就从读侧彻底消失（下一轮同一条误报以 `pending` 重新报出来）。
#
# 所以「忽略」要落在一个**不跟着行走**的地方：按 `(target_key, fingerprint)` 记。
# 读侧在 `baseline_source`（`baseline_findings` / `suppressed`），写侧只有这里。
#
# 判据的**唯一来源是分组键**（`target_key` = 周版本分组键，`build_weekly_group_key`）。
# 它为空时（单提交分析）不写台账：那张运行没有「组」可隔离，跨组抑制会误伤别的版本
# 的同名结论 —— 宁可在这一档退回旧逻辑。
IGNORED_TITLE_MAX_CHARS = 500


def _ignored_ledger_identity(anomaly: AiAnalysisAnomaly) -> Optional[tuple[str, int]]:
    """这条结论在台账里的身份：`(target_key, project_id)`；没有分组键就返回 None。

    分组键只能从**运行**上取（`AiAnalysisRun.target_key`），不在这条异常行上 —— 所以
    这里多一次查询。它是处置路径（一次一条 / 一次最多 `MAX_BATCH_ITEMS` 条），不是
    热路径；拿错分组的代价（抑制到别的版本、或者该抑制的没抑制）比这一下查询贵得多。

    运行行已经不在库里时返回 None：没有分组键就无法定位台账身份，与「分组键为空」
    同一档处理（退回旧逻辑，不猜）。
    """
    run_id = anomaly.run_id
    if not run_id:
        return None
    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return None
    target_key = str(run.target_key or "").strip()
    if not target_key:
        return None
    return target_key, int(run.project_id or anomaly.project_id or 0)


def record_ignored_fingerprint(
    anomaly: AiAnalysisAnomaly,
    *,
    ignored_at: Optional[datetime] = None,
) -> Optional[AiAnalysisIgnoredFingerprint]:
    """把「这条不用再报」记进台账（同一个身份重复处置就刷新，不叠加行）。

    刷新 `ignored_at` / `last_run_id` / 标题是有意的：用户后来又在另一个运行的行上点了
    一次「忽略」，台账应当说「最近一次忽略发生在这一次运行上」—— 删运行时正是按
    `last_run_id` 级联清台账（见模型列的说明），留着旧 run 号会让清理漏掉它。
    """
    identity = _ignored_ledger_identity(anomaly)
    fingerprint = str(anomaly.fingerprint or "").strip()
    if identity is None or not fingerprint:
        # 没有身份就没有台账行可写。**不报错**：这是「这一档不适用」，不是失败
        # （处置本身仍然照旧生效，下一次读侧走旧逻辑）。
        return None
    target_key, project_id = identity
    row = AiAnalysisIgnoredFingerprint.query.filter_by(
        target_key=target_key, fingerprint=fingerprint
    ).first()
    if row is None:
        row = AiAnalysisIgnoredFingerprint(target_key=target_key, fingerprint=fingerprint)
        db.session.add(row)
    row.project_id = project_id
    row.title = str(anomaly.title or "")[:IGNORED_TITLE_MAX_CHARS] or None
    row.file_path = str(anomaly.file_path or "")[:500] or None
    row.ignored_at = ignored_at or anomaly.disposition_at or datetime.now(timezone.utc)
    row.last_run_id = anomaly.run_id
    return row


def forget_ignored_fingerprint(anomaly: AiAnalysisAnomaly) -> int:
    """把这条指纹从台账里删掉（`pending` / `confirmed` 时调用）。

    撤销与确认都要删：台账回答的是「用户判定过不用再报吗」，一旦他改成「确认」或
    「再等等」，答案就变成否 —— 留着不删，下一轮同一条会被继续静默抑制，而界面上
    显示的是「已确认」。返回删掉的行数（给测试与排查用）。
    """
    identity = _ignored_ledger_identity(anomaly)
    fingerprint = str(anomaly.fingerprint or "").strip()
    if identity is None or not fingerprint:
        return 0
    target_key, _project_id = identity
    return (
        AiAnalysisIgnoredFingerprint.query.filter_by(
            target_key=target_key, fingerprint=fingerprint
        ).delete(synchronize_session=False)
    )


def sync_ignored_ledger(anomaly: AiAnalysisAnomaly, disposition: str) -> None:
    """处置落库时同步台账：`ignored` → 记上；`pending` / `confirmed` → 删掉。

    **不吞异常**：台账写失败就意味着「用户以为忽略生效了、下一轮照旧重报」—— 那正是
    本模块存在要消灭的那种静默失效。让它抛，调用方的整笔事务一起回滚（处置也没发生），
    用户看到一次明确的失败，而不是一次看起来成功、下一轮被推翻的操作。
    """
    if disposition == DISPOSITION_IGNORED:
        record_ignored_fingerprint(anomaly)
        return
    forget_ignored_fingerprint(anomaly)


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

    ## 台账是这张表的另一半（P0）

    `ignored` 除了写在行上，还要写进**分组级**的台账（`sync_ignored_ledger`）：
    行会被下一轮的交付剔掉，而「这条不用再报」这个判断不能跟着它一起消失。
    `pending` / `confirmed` 则要把台账行删掉 —— 那是「撤销这次忽略」的唯一入口。
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

    # **同一个事务**里写台账：处置与它在台账上的影子要么一起生效、要么一起回滚。
    # 分开 commit 的话，「行改成功、台账写失败」会留下一个下一轮照旧重报的假忽略。
    sync_ignored_ledger(anomaly, target)

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


def persist_suppressed_previous(run_id: int, anomalies) -> int:
    """把**这一轮合并结果**里的「曾被人工忽略」标记落到已落库的结论行上。

    ## 为什么在 `_persist_outcome` 之外另开一个落库辅助

    `services/ai_analysis_service.py::_persist_outcome` 逐条建 `AiAnalysisAnomaly` 行，
    但它**不在本工作包的文件所有权内**（1987 行，贴着仓库的 2000 行上限）。而这一列
    恰恰只能由它写：它读的是 `result["anomalies"]`，标记也在这份结果上
    （`incremental_baseline.reconcile_result` 的 `suppressed_previous=True`）。
    所以做法是：**写库逻辑集中在这一个函数里**，`_persist_outcome` 落库之后调用一次
    即可（一行调用，留给那个文件的所有者接；不接也不会破坏任何既有行为，只是这一列
    保持默认的 False）。

    ## 判据与幂等

    只认 `suppressed_previous is True` **这一种真值**：合并器只在真的命中时加键，
    其余条目连键都没有 —— 这里不做 `bool()` 折真，一个手写的 `"false"` 字符串不该
    被读成「曾被忽略」。

    **按这一轮的结果重写，而不是只置 True**：同一个 run 被重复落库（重试/补写）时，
    「上一轮置过 True、这一轮结果里没有它」必须能改回 False，否则这一列会只增不减，
    最终每条都带着这个标记，失去区分力。

    返回改动的行数（测试与排查用）。
    """
    wanted = {
        str(item.get("fingerprint") or "")
        for item in anomalies or []
        if isinstance(item, Mapping) and item.get("suppressed_previous") is True
    }
    wanted.discard("")
    rows = AiAnalysisAnomaly.query.filter_by(run_id=run_id).all()
    changed = 0
    for row in rows:
        should_be = str(row.fingerprint or "") in wanted
        if bool(row.suppressed_previous) != should_be:
            row.suppressed_previous = should_be
            changed += 1
    if changed:
        db.session.commit()
    return changed
