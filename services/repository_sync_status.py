#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repository sync status helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy.exc import SQLAlchemyError


REPOSITORY_SYNC_STATUS_ERRORS = (AttributeError, TypeError, ValueError, RuntimeError, SQLAlchemyError)
REPOSITORY_SYNC_ROLLBACK_ERRORS = (AttributeError, RuntimeError, SQLAlchemyError)


def _normalize_error_message(error_message, max_length=2000):
    text = str(error_message or "").strip()
    if not text:
        text = "未知同步错误"
    return text[:max_length]


def record_sync_error(session, repository, error_message, *, log_func=None, log_type="SYNC", commit=True):
    """Write sync error to repository and optionally commit."""
    if session is None or repository is None:
        return False
    try:
        repository.last_sync_error = _normalize_error_message(error_message)
        repository.last_sync_error_time = datetime.now(timezone.utc)
        if commit:
            session.commit()
        if log_func:
            log_func(
                f"📝 已记录仓库 {getattr(repository, 'name', repository.id)} 的同步错误: {repository.last_sync_error}",
                log_type,
            )
        return True
    except REPOSITORY_SYNC_STATUS_ERRORS as exc:
        if log_func:
            log_func(f"❌ 记录同步错误失败: {exc}", log_type, force=True)
        if commit:
            try:
                session.rollback()
            except REPOSITORY_SYNC_ROLLBACK_ERRORS:
                pass
        return False


def clear_sync_error(session, repository, *, log_func=None, log_type="SYNC", commit=True):
    """Clear repository sync error state and optionally commit."""
    if session is None or repository is None:
        return False
    if not getattr(repository, "last_sync_error", None):
        return True
    try:
        repository.last_sync_error = None
        repository.last_sync_error_time = None
        if commit:
            session.commit()
        if log_func:
            log_func(
                f"✅ 已清除仓库 {getattr(repository, 'name', repository.id)} 的同步错误状态",
                log_type,
            )
        return True
    except REPOSITORY_SYNC_STATUS_ERRORS as exc:
        if log_func:
            log_func(f"⚠️ 清除同步错误状态失败: {exc}", log_type, force=True)
        if commit:
            try:
                session.rollback()
            except REPOSITORY_SYNC_ROLLBACK_ERRORS:
                pass
        return False
