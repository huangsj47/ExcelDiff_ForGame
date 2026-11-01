"""Structured evidence gaps: retrieve, deliver, then ask the model to reconsider.

Never infer tool requests from prose, equate fetched data with resolved semantics,
or silently close an open need omitted from a later answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from services.ai.context_request_protocol import coerce_requests


def parse_needs(value):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("evidence_needs 必须是数组")
    result, seen = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("evidence_needs 每项须为对象")
        identifier, question = str(raw.get("need_id") or "").strip(), str(raw.get("question") or "").strip()
        kind, status = raw.get("kind"), raw.get("status", "open")
        if not identifier or identifier in seen or not question:
            raise ValueError("证据需求必须有唯一need_id与明确question")
        if kind not in ("retrievable", "external_semantics") or status not in ("open", "resolved", "not_applicable"):
            raise ValueError("证据需求kind/status不合法")
        requests = coerce_requests(raw.get("requests"))
        if kind == "external_semantics" and requests:
            raise ValueError("外部语义确认不应携带读取请求；可读取的源码/表格归retrievable")
        if kind == "retrievable" and status == "open" and not requests:
            raise ValueError("可补取的open需求必须给出现有只读工具requests")
        reason = str(raw.get("resolution_reason") or "").strip()
        ids, refs = raw.get("evidence_ids", []), raw.get("finding_refs", [])
        if not isinstance(ids, list) or not isinstance(refs, list):
            raise ValueError("evidence_ids/finding_refs必须是数组")
        if status != "open" and not reason:
            raise ValueError("关闭证据需求必须说明resolution_reason")
        result.append(dict(need_id=identifier, question=question, kind=kind, status=status,
                           requests=requests, evidence_ids=tuple(map(str, ids)),
                           finding_refs=tuple(map(str, refs)), resolution_reason=reason))
        seen.add(identifier)
    return tuple(result)


@dataclass
class EvidenceFollowup:
    needs: dict = field(default_factory=dict)
    attempted: set = field(default_factory=set)
    delivered: set = field(default_factory=set)
    delivered_ids: set = field(default_factory=set)
    classified_once: bool = False
    classification_missing: bool = False

    def observe(self, receipts):
        for row in receipts:
            if not row.get("failed") and not row.get("empty") and not row.get("omitted_for_budget"):
                key = row.get("request_key")
                if key:
                    self.delivered.add(tuple(key))
                if row.get("evidence_id") and not row.get("shrink_level"):
                    self.delivered_ids.add(row["evidence_id"])

    def plan(self, payload, store, *, remaining, can_continue):
        from services.ai.context_tools import _cache_key

        gaps = (payload.report_sections or {}).get("gaps") or []
        if gaps and not payload.evidence_needs and not self.needs and not self.classified_once:
            self.classification_missing = True
            if can_continue:
                self.classified_once = True
                return (), ("报告有信息缺口，请在final.evidence_needs逐条分类：可从冻结项目补取的用retrievable并给requests；"
                            "确需策划/业务确认的用external_semantics。没有代码diff不等于不能读取冻结消费端。保留现有结论。")
        requests = []
        for need in payload.evidence_needs:
            self.classification_missing = False
            row = dict(need)
            if need["status"] == "resolved":
                ids = need["evidence_ids"]
                valid = bool(ids) and all(i in self.delivered_ids and callable(getattr(store, "verify", None)) and store.verify(i)
                                          and store.by_id(i) is not None
                                          and not store.by_id(i).meta.get("tool_failed")
                                          and not store.by_id(i).meta.get("tool_empty") for i in ids)
                if not valid:
                    row.update(status="open", platform_reason="关闭声明缺少本次可验证依据，未采信")
            self.needs[need["need_id"]] = row
            if row["status"] != "open" or row["kind"] != "retrievable":
                continue
            for request in row["requests"]:
                key = _cache_key(request)
                if key in self.attempted or key in self.delivered:
                    row["platform_reason"] = "该地址已尝试或交付；仍有缺口时须给出不同坐标/续页，不能把交付等同语义确认"
                    continue
                if not can_continue or len(requests) >= remaining:
                    row["platform_reason"] = "轮次、索取或运行token额度不足，未继续补取"
                    continue
                self.attempted.add(key)
                requests.append(request)
        return tuple(requests), ("请使用本轮补取证据重判，不得把取数成功当语义已解决。逐条保留evidence_needs；"
                                  "关闭时写status=resolved/not_applicable、resolution_reason，resolved必须给可验证evidence_ids。") if requests else ""

    def fetched(self, batch, rejected, *, requests=()):
        from services.ai.context_tools import _cache_key
        from services.ai.trace_evidence import failure_notice

        def attribute(key, reason):
            for need in self.needs.values():
                if (need["status"] == "open" and need["kind"] == "retrievable"
                        and key in {_cache_key(r) for r in need["requests"]}):
                    need["platform_reason"] = reason
        for item in rejected:
            if 0 <= item.index < len(requests):
                attribute(_cache_key(requests[item.index]), "请求未获执行：" + item.reason)
        for item in batch.items:
            if item.meta.get("tool_failed") or item.meta.get("tool_empty") or failure_notice(item.text):
                attribute(tuple(item.meta.get("cache_key") or ()), item.text)

    @property
    def unresolved(self):
        return self.classification_missing or any(n["status"] == "open" and n["kind"] == "retrievable"
                                                  for n in self.needs.values())

    def summary(self):
        result = []
        for row in self.needs.values():
            result.append({**{k: v for k, v in row.items() if k != "requests"},
                           "requests": [dict(type=r.type, repository_id=r.repository_id, commit=r.commit,
                                             path=r.path, lines=r.lines, query=r.query, cursor=r.cursor,
                                             name=r.name) for r in row["requests"]]})
        if self.classification_missing:
            result.append(dict(need_id="unclassified", status="open", kind="retrievable",
                               question="报告缺口未结构化分类，无法确定哪些仍可补取"))
        return tuple(result)
