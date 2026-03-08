"""Shared helpers for unified JSON API response contracts."""

from __future__ import annotations

from typing import Any

from services.api_response_models import ErrorResponsePayload, SuccessResponsePayload


def build_error_payload(
    *,
    message: str,
    error_type: str,
    status: str = "error",
    retry_after_seconds: int | None = None,
    success: bool = False,
    **extras: Any,
) -> dict[str, Any]:
    payload = ErrorResponsePayload(
        status=status,
        message=message,
        error_type=error_type,
        retry_after_seconds=retry_after_seconds,
        success=success,
    )
    return payload.to_dict(**extras)


def build_success_payload(
    *,
    message: str,
    status: str = "success",
    success: bool = True,
    retry_after_seconds: int | None = None,
    **extras: Any,
) -> dict[str, Any]:
    payload = SuccessResponsePayload(
        status=status,
        message=message,
        success=success,
        retry_after_seconds=retry_after_seconds,
    )
    return payload.to_dict(**extras)


def json_error(
    *,
    jsonify,
    message: str,
    error_type: str,
    http_status: int = 500,
    status: str = "error",
    retry_after_seconds: int | None = None,
    success: bool = False,
    **extras: Any,
):
    payload = build_error_payload(
        message=message,
        error_type=error_type,
        status=status,
        retry_after_seconds=retry_after_seconds,
        success=success,
        **extras,
    )
    return jsonify(payload), http_status


def json_success(
    *,
    jsonify,
    message: str,
    http_status: int = 200,
    status: str = "success",
    success: bool = True,
    retry_after_seconds: int | None = None,
    **extras: Any,
):
    payload = build_success_payload(
        message=message,
        status=status,
        success=success,
        retry_after_seconds=retry_after_seconds,
        **extras,
    )
    return jsonify(payload), http_status
