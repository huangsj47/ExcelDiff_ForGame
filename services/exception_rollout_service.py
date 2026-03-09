"""Gradual rollout helpers for narrowed exception handling."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Mapping


_MODE_ALL = "all"
_MODE_NONE = "none"
_MODE_REPOSITORY = "repository"
_MODE_FILE = "file"
_MODE_REPOSITORY_OR_FILE = "repository_or_file"
_VALID_MODES = {
    _MODE_ALL,
    _MODE_NONE,
    _MODE_REPOSITORY,
    _MODE_FILE,
    _MODE_REPOSITORY_OR_FILE,
}


@dataclass(frozen=True)
class ExceptionRolloutDecision:
    enabled: bool
    mode: str
    matched_by: str


def _normalize_mode(raw_mode: str | None) -> str:
    mode = str(raw_mode or "").strip().lower() or _MODE_ALL
    if mode in _VALID_MODES:
        return mode
    return _MODE_ALL


def _parse_repository_ids(raw_value: str | None) -> set[int]:
    parsed: set[int] = set()
    for token in str(raw_value or "").split(","):
        value = token.strip()
        if not value:
            continue
        try:
            parsed.add(int(value))
        except ValueError:
            continue
    return parsed


def _parse_file_patterns(raw_value: str | None) -> list[str]:
    return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]


def resolve_exception_narrowing_rollout(
    *,
    repository_id: int | None,
    file_path: str | None,
    environ: Mapping[str, str] | None = None,
) -> ExceptionRolloutDecision:
    env = environ or os.environ
    mode = _normalize_mode(env.get("EXCEPTION_NARROWING_ROLLOUT_MODE"))

    if mode == _MODE_ALL:
        return ExceptionRolloutDecision(enabled=True, mode=mode, matched_by="all")
    if mode == _MODE_NONE:
        return ExceptionRolloutDecision(enabled=False, mode=mode, matched_by="none")

    repository_ids = _parse_repository_ids(env.get("EXCEPTION_NARROWING_ROLLOUT_REPOSITORIES"))
    file_patterns = _parse_file_patterns(env.get("EXCEPTION_NARROWING_ROLLOUT_FILES"))

    repository_matched = repository_id in repository_ids if repository_id is not None else False
    file_matched = any(
        fnmatch.fnmatch(file_path, pattern) for pattern in file_patterns
    ) if file_path else False

    if mode == _MODE_REPOSITORY:
        return ExceptionRolloutDecision(
            enabled=repository_matched,
            mode=mode,
            matched_by="repository" if repository_matched else "",
        )
    if mode == _MODE_FILE:
        return ExceptionRolloutDecision(
            enabled=file_matched,
            mode=mode,
            matched_by="file" if file_matched else "",
        )

    enabled = repository_matched or file_matched
    if repository_matched:
        matched_by = "repository"
    elif file_matched:
        matched_by = "file"
    else:
        matched_by = ""
    return ExceptionRolloutDecision(enabled=enabled, mode=mode, matched_by=matched_by)
