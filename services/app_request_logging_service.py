"""Request/access logging bootstrap helpers extracted from app.py."""

from __future__ import annotations

import logging
import re

from flask import request

REQUEST_LOG_MESSAGE_ERRORS = (RuntimeError, ValueError, TypeError, AttributeError)
REQUEST_LOG_STATUS_PARSE_ERRORS = (ValueError, TypeError)


class _WerkzeugAgentAccessFilter(logging.Filter):
    """Suppress high-frequency /api/agents access logs for 2xx/3xx responses."""

    _SUPPRESS_PATH_PREFIXES = ("/api/agents",)
    _REQUEST_LINE_PATTERN = re.compile(
        r'"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)\s+HTTP/[0-9.]+"\s+(?:\x1b\[[0-9;]*m)?(\d{3})'
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except REQUEST_LOG_MESSAGE_ERRORS:
            return True

        if "/api/agents" not in message:
            return True

        matched = self._REQUEST_LINE_PATTERN.search(message or "")
        if not matched:
            return True

        path = matched.group(1)
        try:
            status_code = int(matched.group(2))
        except REQUEST_LOG_STATUS_PARSE_ERRORS:
            return True

        if any(path.startswith(prefix) for prefix in self._SUPPRESS_PATH_PREFIXES):
            return status_code >= 400
        return True


def _register_werkzeug_filter(*, suppress_agent_access_log: bool) -> None:
    if not suppress_agent_access_log:
        return
    werkzeug_logger = logging.getLogger("werkzeug")
    if any(isinstance(item, _WerkzeugAgentAccessFilter) for item in werkzeug_logger.filters):
        return
    werkzeug_logger.addFilter(_WerkzeugAgentAccessFilter())


def configure_request_logging(*, app, log_print, suppress_agent_access_log: bool) -> None:
    """Configure access log filter and admin request trace hook."""
    _register_werkzeug_filter(suppress_agent_access_log=suppress_agent_access_log)

    @app.before_request
    def _log_request_info():
        if request.path.startswith("/admin/"):
            log_print(f"[REQUEST] {request.method} {request.path}", "REQUEST")
