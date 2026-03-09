import sys
from types import ModuleType, SimpleNamespace

from bootstrap.runtime_entry import run_runtime_entry


def test_run_runtime_entry_accepts_app_module_without_original_print(monkeypatch):
    calls = {"agent": 0, "cleanup": 0}

    fake_agent_module = ModuleType("agent.runner_runtime")

    def _fake_run_agent():
        calls["agent"] += 1

    fake_agent_module.run_agent = _fake_run_agent
    monkeypatch.setitem(sys.modules, "agent.runner_runtime", fake_agent_module)

    app_module = SimpleNamespace(
        log_print=lambda *args, **kwargs: None,
        cleanup_app=lambda: calls.__setitem__("cleanup", calls["cleanup"] + 1),
        initialize_app=lambda: None,
        clear_log_file=lambda: None,
        app=SimpleNamespace(run=lambda **kwargs: None),
        DEPLOYMENT_MODE="agent",
    )

    run_runtime_entry(app_module)

    assert calls["agent"] == 1
    assert calls["cleanup"] == 1
