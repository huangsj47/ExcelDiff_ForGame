"""质量优先的历史工作集：只在可恢复的检查点后提前压缩。"""
from __future__ import annotations

from typing import Any

from services.ai.budget import CompactionResult, compact_history, estimate_chars


def parse_checkpoint(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("checkpoint 必须是对象")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("checkpoint.summary 必须是非空字符串")
    result = {"summary": summary.strip()}
    for key in ("evidence_ids", "completed_paths", "open_questions"):
        entries = value.get(key, [])
        if not isinstance(entries, list) or any(not isinstance(x, str) for x in entries):
            raise ValueError(f"checkpoint.{key} 必须是字符串数组")
        result[key] = list(dict.fromkeys(x.strip() for x in entries if x.strip()))
    # 不截断检查点：超长请求纠正，而不是默默删掉疑点。
    if len(str(result)) > 20_000:
        raise ValueError("checkpoint 过长，请仅保留已确认事实、未决问题与证据地址")
    return result


def compact_working_set(messages, *, hard_chars: int, current_chars: int,
                        memos=(), protect_head: int = 2, checkpoint=None,
                        store=None) -> CompactionResult:
    """检查点的关键证据原文全部钉住；无可验证检查点时仅服从硬水位。"""
    hard_target = max(0, hard_chars - current_chars)
    total = estimate_chars(messages)
    verify = getattr(store, "verify", None)
    by_id = getattr(store, "by_id", None)
    refs = (checkpoint or {}).get("evidence_ids", [])
    safe = bool(checkpoint and refs and callable(verify) and callable(by_id)
                and all(verify(ref) for ref in refs))
    # 软目标按用户水位派生，不另造隐藏硬上限。只有正文占主要体积、且安全检查点存在才启用。
    target = min(hard_target, max(0, hard_chars // 2 - current_chars)) if safe else hard_target
    if total <= target:
        return CompactionResult(messages=tuple(messages))
    pinned = ""
    if safe:
        bodies = []
        blobs = set()
        for ref in refs:
            item = by_id(ref)
            blob = item.meta.get("blob_id") or item.text
            if blob not in blobs:
                blobs.add(blob)
                bodies.append(f"### @evidence_id={ref} {item.label}\n{item.text}")
        pinned = ("## 分析检查点（模型工作记录，仍需依据证据核实）\n"
                  + checkpoint["summary"] + "\n未决问题："
                  + "；".join(checkpoint.get("open_questions", []))
                  + "\n## 检查点关键证据原文\n" + "\n\n".join(bodies))
        # 钉住的正文已占满工作集就不做软压缩，保全证据。
        if len(pinned) + estimate_chars(messages[:protect_head]) >= target:
            if total <= hard_target:
                return CompactionResult(messages=tuple(messages))
            safe, pinned, target = False, "", hard_target
    directory = []
    if callable(verify):
        seen_ids = set()
        for item in store.values():
            eid = str(item.meta.get("evidence_id") or "")
            if eid and eid not in seen_ids and verify(eid):
                seen_ids.add(eid)
                directory.append(f"- @evidence_id={eid} {item.label}")
    archive = "\n## 可恢复证据目录\n" + "\n".join(directory) if directory else ""
    result = compact_history(messages, target_chars=max(0, target-len(pinned)-len(archive)),
                             keep_recent_turns=1, memos=() if directory else memos,
                             protect_head=protect_head)
    if result.compacted:
        from dataclasses import replace
        recap = pinned + "\n\n" + result.recap + archive
        # 净节省必须扣除新增检查点和目录，不能把重装的关键原文算成省下来的token。
        saved = max(0, result.dropped_chars-len(recap))
        result = replace(result, recap=recap, dropped_chars=saved,
                         notes=(f"为控制上下文体积，历史已整理为检查点与可恢复目录，净减少约 {saved:,} 字；"
                                "关键证据和最近一轮原文保留，归档不等于已核实。",))
    return result


def checkpoint_hint() -> str:
    return ("长任务按业务簇取证。完成一簇后，在 need_more_context 中附 checkpoint 对象："
            "summary（已确认事实和暂定发现，不省略反证）、evidence_ids（支撑发现的全部证据地址）、"
            "completed_paths、open_questions（未决问题）。它不改变覆盖统计。平台仅在证据地址可恢复时"
            "据此整理历史，关键证据原文保留；需要归档正文时用 evidence 工具按地址回取。")
