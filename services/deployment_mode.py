"""Shared deployment-mode helpers.

Single source of truth for reading and normalizing `DEPLOYMENT_MODE`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


_VALID_DEPLOYMENT_MODES = {"single", "platform", "agent"}
_AGENT_LOCAL_GIT_CLONE_BLOCK_MESSAGE = "platform+agent 模式下禁止平台本地 clone 仓库，请在 Agent 节点完成同步后重试"


@dataclass(frozen=True)
class CommitDiffModeStrategy:
    async_agent_diff: bool
    allow_platform_local_git_clone: bool
    local_clone_block_message: str


def get_deployment_mode() -> str:
    mode = str(os.environ.get("DEPLOYMENT_MODE") or "single").strip().lower() or "single"
    if mode in _VALID_DEPLOYMENT_MODES:
        return mode
    return "single"


def is_agent_dispatch_mode() -> bool:
    return get_deployment_mode() in {"platform", "agent"}


def is_single_mode() -> bool:
    return get_deployment_mode() == "single"


def get_commit_diff_mode_strategy() -> CommitDiffModeStrategy:
    agent_dispatch = is_agent_dispatch_mode()
    return CommitDiffModeStrategy(
        async_agent_diff=agent_dispatch,
        allow_platform_local_git_clone=not agent_dispatch,
        local_clone_block_message=_AGENT_LOCAL_GIT_CLONE_BLOCK_MESSAGE,
    )
