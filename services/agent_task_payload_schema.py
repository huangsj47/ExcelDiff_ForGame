"""Agent task payload schema compatibility helpers."""

from __future__ import annotations

from typing import Any, Mapping


CURRENT_AGENT_TASK_PAYLOAD_SCHEMA_VERSION = 1
_SCHEMA_VERSION_PARSE_ERRORS = (TypeError, ValueError, OverflowError)


def _normalize_schema_version(raw_value: Any) -> int:
    try:
        parsed = int(raw_value)
    except _SCHEMA_VERSION_PARSE_ERRORS:
        return CURRENT_AGENT_TASK_PAYLOAD_SCHEMA_VERSION
    if parsed < 1:
        return CURRENT_AGENT_TASK_PAYLOAD_SCHEMA_VERSION
    return parsed


def normalize_agent_task_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize task payload to include a backward-compatible schema version."""
    normalized = dict(payload or {})
    normalized["schema_version"] = _normalize_schema_version(
        normalized.get("schema_version", CURRENT_AGENT_TASK_PAYLOAD_SCHEMA_VERSION)
    )
    return normalized


def get_agent_task_payload_schema_version(payload: Mapping[str, Any] | None) -> int:
    return normalize_agent_task_payload(payload).get("schema_version", CURRENT_AGENT_TASK_PAYLOAD_SCHEMA_VERSION)
