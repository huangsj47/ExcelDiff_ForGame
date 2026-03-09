"""Helpers for weekly_version_files_api to keep weekly_version_logic lean."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone


def normalize_naive_datetime(raw_value):
    if not isinstance(raw_value, datetime):
        return None
    if raw_value.tzinfo is None:
        return raw_value
    return raw_value.astimezone(timezone.utc).replace(tzinfo=None)


def _task_age_seconds(task, now_value):
    if task is None:
        return None
    status_value = str(getattr(task, "status", "") or "").lower()
    if status_value == "processing":
        base_time = normalize_naive_datetime(getattr(task, "started_at", None)) or normalize_naive_datetime(
            getattr(task, "created_at", None)
        )
    else:
        base_time = normalize_naive_datetime(getattr(task, "created_at", None))
    if base_time is None:
        return None
    return (now_value - base_time).total_seconds()


def is_stale_sync_task(task, now_value, *, pending_timeout_seconds=300, processing_timeout_seconds=1800):
    if task is None:
        return False
    status_value = str(getattr(task, "status", "") or "").lower()
    age_seconds = _task_age_seconds(task, now_value)
    if age_seconds is None:
        return False
    if status_value == "pending":
        return age_seconds > pending_timeout_seconds
    if status_value == "processing":
        return age_seconds > processing_timeout_seconds
    return False


def parse_json_list(raw_value):
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, tuple):
        return list(raw_value)
    if isinstance(raw_value, str):
        text_value = raw_value.strip()
        if not text_value:
            return []
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, tuple):
                return list(parsed)
        except Exception:
            return [item.strip() for item in re.split(r"[,，;；|\n\r]+", text_value) if item and item.strip()]
    return []


def parse_json_obj(raw_value):
    if raw_value is None:
        return {}
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        text_value = raw_value.strip()
        if not text_value:
            return {}
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def parse_confirm_usernames(raw_value):
    if not raw_value:
        return []
    usernames = [item.strip() for item in re.split(r"[,，;；|\n\r]+", str(raw_value)) if item and item.strip()]
    unique_usernames = []
    for username in usernames:
        if username not in unique_usernames:
            unique_usernames.append(username)
    return unique_usernames


def extract_author_lookup_keys(raw_author):
    text = str(raw_author or "").strip()
    if not text:
        return []
    keys = []
    lower_text = text.lower()
    if all(symbol not in lower_text for symbol in ("@", "<", ">", " ")):
        keys.append(lower_text)
    if "@" in lower_text and "<" not in lower_text and ">" not in lower_text:
        email_prefix = lower_text.split("@", 1)[0].strip()
        if email_prefix and email_prefix not in keys:
            keys.append(email_prefix)
    for email in re.findall(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text):
        email_prefix = email.lower().split("@", 1)[0].strip()
        if email_prefix and email_prefix not in keys:
            keys.append(email_prefix)
    return keys


def resolve_author_display(raw_author, *, username_to_display_name_lower, email_prefix_to_display_name):
    text = str(raw_author or "").strip()
    if not text:
        return ""
    for author_key in extract_author_lookup_keys(text):
        mapped_name = username_to_display_name_lower.get(author_key) or email_prefix_to_display_name.get(author_key)
        if mapped_name:
            return mapped_name
    return text
