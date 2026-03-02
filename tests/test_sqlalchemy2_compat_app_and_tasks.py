from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_app_py_has_no_legacy_query_get_calls():
    content = _read("app.py")
    assert ".query.get(" not in content


def test_weekly_sync_tasks_has_no_legacy_query_get_calls():
    content = _read("tasks/weekly_sync_tasks.py")
    assert ".query.get(" not in content

