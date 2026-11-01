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
    archive = evidence_directory(store)
    result = compact_history(messages, target_chars=max(0, target-len(pinned)-len(archive)),
                             keep_recent_turns=1, memos=() if archive else memos,
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


def evidence_directory(store):
    """Keep every verified address visible; a directory that cannot fit must fail closed."""
    verify = getattr(store, "verify", None)
    rows, seen = [], set()
    if callable(verify):
        for item in store.values():
            eid = str(item.meta.get("evidence_id") or "")
            if eid and eid not in seen and verify(eid):
                seen.add(eid)
                rows.append(f"- @evidence_id={eid} {item.label}")
    return "\n## 可恢复证据目录\n" + "\n".join(rows) if rows else ""


def restore_pending_pointers(items, store):
    """A pointer into removed history must become the original evidence again."""
    from dataclasses import replace

    by_id, verify = getattr(store, "by_id", None), getattr(store, "verify", None)
    result = []
    for item in items:
        if item.meta.get("repeat_pointer"):
            eid = str(item.meta.get("evidence_id") or "")
            original = by_id(eid) if callable(by_id) and callable(verify) and verify(eid) else None
            item = original if original is not None else replace(item,
                text=f"[归档证据恢复失败] evidence_id={eid}；正文不可用，不能据此认定事实。",
                meta={"evidence_id": eid, "tool_failed": True})
        result.append(item)
    return tuple(result)


def archived_brief(brief, store, note="旧对话已归档，以下地址可用 evidence 工具恢复原文。"):
    from dataclasses import replace

    return replace(brief, pending_items=restore_pending_pointers(brief.pending_items, store),
                   recap=note + evidence_directory(store) + "\n" + brief.recap)


def protected_history(messages, brief, protect_head, *, include_task=True):
    """A seeded first-round retry has not appended its task to history yet."""
    base = list(messages[:protect_head])
    if include_task and protect_head >= 3 and len(base) < 3 and brief.task_message.strip():
        base.append({"role": "user", "content": brief.task_message})
    return base


def _oldest_exchange_end(base, start):
    # A protected task may leave its response at start. Remove that response
    # alone; otherwise remove all user messages and their assistant response.
    seen_assistant = base[start].get("role") == "assistant"
    end = start + 1
    while end < len(base):
        role = base[end].get("role")
        if role == "user" and seen_assistant:
            break
        seen_assistant = seen_assistant or role == "assistant"
        end += 1
    return end


def fit_observed_window(brief, messages, prepare, *, protect_head=1, store=None):
    """An observed rejection overrides the normal recent-turn preservation floor.

    Archive the fewest whole exchanges needed, retaining system instructions and
    current evidence. The immutable evidence store remains available for rereads.
    Never repeatedly send a request larger than a window already observed to fail.
    """
    base = list(messages)
    items, text = prepare(brief, base)
    removed = 0
    note = ""
    while len(base) > protect_head and estimate_chars(base) + len(text) > brief.limits.prompt_char_budget:
        del base[protect_head:_oldest_exchange_end(base, protect_head)]
        removed += 1
        note = ("上游已拒绝更大的上下文，旧对话按实测水位归档；此前证据可按 evidence_id 回取。"
                "未保留正文不代表该问题已核实或可以忽略；需要完整清单时索取 change-manifest。")
        items, text = prepare(archived_brief(brief, store, note), base)
    return base, items, text, note, removed
