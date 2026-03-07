from types import SimpleNamespace

from app import app


def _build_context(**kwargs):
    from services.commit_diff_template_context import build_commit_diff_template_context

    defaults = {
        "commit": SimpleNamespace(id=123),
        "repository": SimpleNamespace(id=9),
        "project": SimpleNamespace(id=7),
        "file_commits": [],
        "previous_commit": None,
        "is_excel": False,
        "diff_data": None,
        "is_deleted": False,
        "mode_strategy": SimpleNamespace(async_agent_diff=False),
    }
    defaults.update(kwargs)
    with app.test_request_context("/"):
        return build_commit_diff_template_context(**defaults)


def test_builder_enables_async_agent_diff_when_agent_mode_and_no_diff():
    ctx = _build_context(mode_strategy=SimpleNamespace(async_agent_diff=True), diff_data=None, is_deleted=False)
    assert ctx["async_agent_diff"] is True
    assert "123" in str(ctx["agent_diff_endpoint"] or "")


def test_builder_disables_async_agent_diff_when_deleted():
    ctx = _build_context(
        mode_strategy=SimpleNamespace(async_agent_diff=True),
        is_deleted=True,
        diff_data={"type": "deleted"},
    )
    assert ctx["async_agent_diff"] is False


def test_builder_disables_async_agent_diff_when_sync_diff_ready():
    ctx = _build_context(
        mode_strategy=SimpleNamespace(async_agent_diff=True),
        diff_data={"type": "code", "hunks": []},
        is_deleted=False,
    )
    assert ctx["async_agent_diff"] is False
