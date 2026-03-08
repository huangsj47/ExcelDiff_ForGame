from types import SimpleNamespace

import services.app_request_logging_service as request_logging


def test_agent_access_filter_suppresses_2xx_agent_requests():
    record = SimpleNamespace(
        getMessage=lambda: '127.0.0.1 - - [x] "GET /api/agents/heartbeat HTTP/1.1" 200 123'
    )
    result = request_logging._WerkzeugAgentAccessFilter().filter(record)
    assert result is False


def test_agent_access_filter_keeps_5xx_agent_requests():
    record = SimpleNamespace(
        getMessage=lambda: '127.0.0.1 - - [x] "POST /api/agents/tasks/claim HTTP/1.1" 500 123'
    )
    result = request_logging._WerkzeugAgentAccessFilter().filter(record)
    assert result is True


def test_agent_access_filter_returns_true_when_message_fetch_raises():
    record = SimpleNamespace(getMessage=lambda: (_ for _ in ()).throw(RuntimeError("message broken")))
    result = request_logging._WerkzeugAgentAccessFilter().filter(record)
    assert result is True


def test_register_werkzeug_filter_is_idempotent(monkeypatch):
    fake_logger = SimpleNamespace(filters=[], addFilter=lambda item: fake_logger.filters.append(item))
    monkeypatch.setattr(
        request_logging,
        "logging",
        SimpleNamespace(getLogger=lambda _name=None: fake_logger),
    )

    request_logging._register_werkzeug_filter(suppress_agent_access_log=True)
    request_logging._register_werkzeug_filter(suppress_agent_access_log=True)

    assert len(fake_logger.filters) == 1
    assert isinstance(fake_logger.filters[0], request_logging._WerkzeugAgentAccessFilter)
