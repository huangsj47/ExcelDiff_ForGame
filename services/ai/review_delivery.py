"""独立复核的机器裁决直接走协议，不藏在业务 Markdown 中。"""
import json


def parse_review_delivery(value):
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("verdicts 必须是裁决对象数组")
    from services.ai.verdict import parse_verdicts
    parsed = parse_verdicts(json.dumps({"verdicts": value}, ensure_ascii=False))
    if len(parsed) != len(value) or len({item.finding_id for item in parsed}) != len(parsed):
        raise ValueError("verdicts 有无效或重复的 finding_id/verdict，请逐条修正")
    return tuple(value)


def review_verdicts_of(outcome):
    from services.ai.verdict import parse_verdicts
    raw = getattr(getattr(outcome, "payload", None), "verdicts", ())
    return parse_verdicts(json.dumps({"verdicts": raw}, ensure_ascii=False)) if raw else parse_verdicts(
        outcome.report_markdown)
