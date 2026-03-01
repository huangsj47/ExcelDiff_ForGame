from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_update_commit_status_supports_action_compatibility():
    content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    assert "action_to_status = {" in content
    assert "'confirm': 'confirmed'" in content
    assert "'reject': 'rejected'" in content


def test_batch_update_compat_accepts_ids_and_commit_ids():
    content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    assert "data.get('commit_ids') or data.get('ids') or request.form.getlist('ids')" in content
    assert "if action in {'confirm', 'confirmed', 'approve'}" in content
    assert "elif action in {'reject', 'rejected'}" in content

