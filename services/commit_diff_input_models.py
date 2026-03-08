"""Input models for commit-diff related service handlers."""

from __future__ import annotations

from dataclasses import dataclass


_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CommitDiffQueryInput:
    """Query-string input for diff endpoints."""

    force_retry: bool = False

    @classmethod
    def from_request(cls, request):
        raw_force_retry = str(request.args.get("force_retry") or "").strip().lower()
        return cls(force_retry=raw_force_retry in _TRUTHY)


@dataclass(frozen=True)
class MergeDiffRefreshInput:
    """JSON body input for merge-diff refresh endpoint."""

    commit_ids: list[int]

    @classmethod
    def from_request_json(cls, request):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("请求体必须为JSON对象")
        raw_commit_ids = payload.get("commit_ids")
        if not isinstance(raw_commit_ids, list):
            raise ValueError("commit_ids 必须为数组")

        normalized_ids: list[int] = []
        for item in raw_commit_ids:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                normalized_ids.append(value)

        if not normalized_ids:
            raise ValueError("未提供有效的提交ID")
        return cls(commit_ids=normalized_ids)
