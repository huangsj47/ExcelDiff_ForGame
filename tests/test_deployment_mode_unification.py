import services.task_worker_service as task_worker_service


def test_shared_deployment_mode_normalization(monkeypatch):
    from services.deployment_mode import get_deployment_mode, is_agent_dispatch_mode

    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    assert get_deployment_mode() == "single"
    assert is_agent_dispatch_mode() is False

    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    assert get_deployment_mode() == "platform"
    assert is_agent_dispatch_mode() is True

    monkeypatch.setenv("DEPLOYMENT_MODE", "agent")
    assert get_deployment_mode() == "agent"
    assert is_agent_dispatch_mode() is True

    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    assert get_deployment_mode() == "single"
    assert is_agent_dispatch_mode() is False

    monkeypatch.setenv("DEPLOYMENT_MODE", "invalid-mode")
    assert get_deployment_mode() == "single"
    assert is_agent_dispatch_mode() is False


def test_task_worker_mode_uses_shared_normalization(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    assert task_worker_service._deployment_mode() == "platform"
    assert task_worker_service._use_agent_dispatch() is True

    monkeypatch.setenv("DEPLOYMENT_MODE", "invalid-mode")
    assert task_worker_service._deployment_mode() == "single"
    assert task_worker_service._use_agent_dispatch() is False


def test_commit_diff_mode_strategy(monkeypatch):
    from services.deployment_mode import get_commit_diff_mode_strategy

    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    strategy = get_commit_diff_mode_strategy()
    assert strategy.async_agent_diff is False
    assert strategy.allow_platform_local_git_clone is True
    assert "platform+agent" in strategy.local_clone_block_message

    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    strategy = get_commit_diff_mode_strategy()
    assert strategy.async_agent_diff is True
    assert strategy.allow_platform_local_git_clone is False

    monkeypatch.setenv("DEPLOYMENT_MODE", "agent")
    strategy = get_commit_diff_mode_strategy()
    assert strategy.async_agent_diff is True
    assert strategy.allow_platform_local_git_clone is False
