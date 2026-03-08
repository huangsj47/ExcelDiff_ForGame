"""Typed API response payload models for service-layer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ErrorResponsePayload:
    """Unified error payload used by API handlers."""

    status: str
    message: str
    error_type: str
    retry_after_seconds: Optional[int] = None
    success: bool = False

    def to_dict(self, **extras: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "message": self.message,
            "error_type": self.error_type,
            "retry_after_seconds": self.retry_after_seconds,
            "success": self.success,
        }
        payload.update(extras)
        return payload


@dataclass(frozen=True)
class SuccessResponsePayload:
    """Unified success payload used by API handlers."""

    status: str
    message: str
    success: bool = True
    retry_after_seconds: Optional[int] = None

    def to_dict(self, **extras: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "message": self.message,
            "success": self.success,
            "retry_after_seconds": self.retry_after_seconds,
        }
        payload.update(extras)
        return payload
