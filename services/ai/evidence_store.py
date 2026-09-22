"""一次运行内的共享证据仓：正文只存一份，并可按稳定 evidence_id 回查。"""
from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Any

from services.ai.budget import ContextItem


class EvidenceStore(MutableMapping[Any, ContextItem]):
    def __init__(self) -> None:
        self._by_request: dict[Any, ContextItem] = {}
        self._by_id: dict[str, ContextItem] = {}

    def __getitem__(self, key: Any) -> ContextItem:
        return self._by_request[key]

    def __setitem__(self, key: Any, value: ContextItem) -> None:
        self._by_request[key] = value
        evidence_id = str(value.meta.get("evidence_id") or value.meta.get("chunk_id") or "")
        if evidence_id:
            self._by_id[evidence_id] = value

    def __delitem__(self, key: Any) -> None:
        value = self._by_request.pop(key)
        evidence_id = str(value.meta.get("evidence_id") or value.meta.get("chunk_id") or "")
        if evidence_id and self._by_id.get(evidence_id) is value:
            self._by_id.pop(evidence_id, None)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._by_request)

    def __len__(self) -> int:
        return len(self._by_request)

    def by_id(self, evidence_id: str) -> ContextItem | None:
        return self._by_id.get(str(evidence_id or ""))

    def summary(self) -> dict[str, int]:
        return {
            "request_keys": len(self._by_request),
            "unique_evidence": len(self._by_id),
            "stored_chars": sum(len(item.text) for item in self._by_id.values()),
        }
