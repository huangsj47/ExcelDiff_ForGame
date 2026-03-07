from pathlib import Path


def test_start_agent_bat_uses_unbuffered_tee_logging():
    content = Path("agent/start_agent.bat").read_text(encoding="utf-8")
    assert "Tee-Object" in content
    assert "-u $script" in content
    assert "PYTHONUNBUFFERED" in content
