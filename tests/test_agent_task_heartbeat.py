"""长任务执行时的心跳线程必须持续运行并可靠退出。"""
from threading import Event
from types import SimpleNamespace


def test_task_heartbeat_runs_during_blocking_work_and_stops(monkeypatch):
    from agent import task_heartbeat

    sent = Event()
    calls = []

    def post(url, payload, **kwargs):
        calls.append(payload)
        sent.set()
        return 200, {"success": True}

    monkeypatch.setattr(task_heartbeat, "post_json", post)
    settings = SimpleNamespace(platform_base_url="http://platform", agent_code="node",
                               heartbeat_interval_seconds=0.01)
    with task_heartbeat.TaskHeartbeat(settings, {"id": 42, "attempt": 3}, "token", {}) as pulse:
        assert sent.wait(2), "执行线程阻塞期间必须仍能发心跳"
        assert pulse.thread.is_alive()
    assert not pulse.thread.is_alive()
    assert calls[0]["task_id"] == 42
    assert calls[0]["attempt"] == 3


def test_heartbeat_survives_transient_network_exception(monkeypatch):
    from agent import task_heartbeat

    recovered = Event()
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("connection reset")
        recovered.set()
        return 200, {"success": True}

    monkeypatch.setattr(task_heartbeat, "post_json", post)
    settings = SimpleNamespace(platform_base_url="http://platform", agent_code="node",
                               heartbeat_interval_seconds=0.01)
    with task_heartbeat.TaskHeartbeat(settings, {"id": 42, "attempt": 3}, "token", {}):
        assert recovered.wait(2)
