from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.excel_html_cache_service import ExcelHtmlCacheService
import services.weekly_version_logic as weekly_logic


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _safe_import_app_or_skip():
    try:
        with patch("sys.stdout") as mock_stdout, patch("sys.stderr") as mock_stderr:
            mock_stdout.buffer = MagicMock()
            mock_stderr.buffer = MagicMock()
            import app  # noqa: WPS433
            return app
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"app import unavailable in this environment: {exc}")


class _DummyCol:
    def __eq__(self, other):
        return ("eq", other)

    def __ge__(self, other):
        return ("ge", other)

    def __le__(self, other):
        return ("le", other)

    def __lt__(self, other):
        return ("lt", other)

    def asc(self):
        return self

    def desc(self):
        return self


class _WeeklyCommitQuery:
    def __init__(self, commits_in_window, base_commit):
        self._commits_in_window = commits_in_window
        self._base_commit = base_commit
        self.filter_calls = []
        self.order_calls = []
        self.all_calls = 0
        self.first_calls = 0

    def filter(self, *args, **kwargs):
        self.filter_calls.append((args, kwargs))
        return self

    def order_by(self, *args, **kwargs):
        self.order_calls.append((args, kwargs))
        return self

    def all(self):
        self.all_calls += 1
        return list(self._commits_in_window)

    def first(self):
        self.first_calls += 1
        return self._base_commit


def test_weekly_excel_fallback_uses_full_window_commits(monkeypatch):
    repo = SimpleNamespace(id=1, type="git")
    t1 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 3, 10, 0, tzinfo=timezone.utc)

    c1 = SimpleNamespace(id=1, commit_id="c1", commit_time=t1, path="a.xlsx", repository=repo, repository_id=1)
    c2 = SimpleNamespace(id=2, commit_id="c2", commit_time=t2, path="a.xlsx", repository=repo, repository_id=1)
    c3 = SimpleNamespace(id=3, commit_id="c3", commit_time=t3, path="a.xlsx", repository=repo, repository_id=1)
    base = SimpleNamespace(id=0, commit_id="base0", commit_time=datetime(2025, 12, 31, tzinfo=timezone.utc))

    query = _WeeklyCommitQuery([c1, c2, c3], base)
    fake_commit_model = type(
        "FakeCommit",
        (),
        {
            "query": query,
            "repository_id": _DummyCol(),
            "path": _DummyCol(),
            "commit_time": _DummyCol(),
            "id": _DummyCol(),
            "commit_id": _DummyCol(),
        },
    )
    monkeypatch.setattr(weekly_logic, "Commit", fake_commit_model)
    monkeypatch.setattr(weekly_logic, "_load_weekly_excel_diff_from_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(weekly_logic, "render_excel_diff_html", lambda *_args, **_kwargs: "<html-ok>")

    captured = {}

    def fake_generate_merged_diff_data(repository, file_path, base_commit, latest_commit, commits):
        captured["repository"] = repository
        captured["file_path"] = file_path
        captured["base_commit"] = base_commit
        captured["latest_commit"] = latest_commit
        captured["commits"] = commits
        return {
            "type": "excel",
            "file_path": file_path,
            "sheets": {"Sheet1": {"rows": [{"status": "modified"}], "stats": {"modified": 1}}},
        }

    monkeypatch.setattr(weekly_logic, "_generate_merged_diff_data", fake_generate_merged_diff_data)
    monkeypatch.setattr(weekly_logic, "_extract_excel_diff_from_payload", lambda payload: payload if payload else None)

    config = SimpleNamespace(
        id=9,
        repository=repo,
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 7, tzinfo=timezone.utc),
    )
    diff_cache = SimpleNamespace(base_commit_id=None, latest_commit_id="c3")

    html = weekly_logic.generate_weekly_excel_merged_diff_html(config, diff_cache, "a.xlsx")

    assert html == "<html-ok>"
    assert "commits" in captured
    assert [c.commit_id for c in captured["commits"]] == ["c1", "c2", "c3"]
    assert captured["latest_commit"].commit_id == "c3"
    assert query.all_calls >= 1


def test_weekly_excel_html_uses_cached_html_first(monkeypatch):
    repo = SimpleNamespace(id=1, type="git")
    config = SimpleNamespace(
        id=10,
        repository=repo,
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 7, tzinfo=timezone.utc),
    )
    diff_cache = SimpleNamespace(base_commit_id="base1", latest_commit_id="latest1")

    fake_weekly_cache_service = SimpleNamespace(
        get_cached_html=lambda *_args, **_kwargs: {"html_content": "<cached-weekly-html>"}
    )
    monkeypatch.setattr(weekly_logic, "_weekly_excel_cache_service", fake_weekly_cache_service)
    monkeypatch.setattr(
        weekly_logic,
        "_load_weekly_excel_diff_from_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not recompute when html cache hit")),
    )

    html = weekly_logic.generate_weekly_excel_merged_diff_html(config, diff_cache, "a.xlsx")
    assert html == "<cached-weekly-html>"


def test_excel_html_cache_get_cached_html_preserves_datetime(monkeypatch):
    service = ExcelHtmlCacheService(SimpleNamespace(session=None), "1.8.0")
    created_at = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)

    cache_record = SimpleNamespace(
        html_content="<div>ok</div>",
        css_content="/* css */",
        js_content="// js",
        cache_metadata='{"k":"v"}',
        created_at=created_at,
    )

    fake_query = SimpleNamespace(
        filter_by=lambda **_kwargs: SimpleNamespace(first=lambda: cache_record)
    )
    fake_model = SimpleNamespace(query=fake_query)
    fake_app = SimpleNamespace(app_context=lambda: nullcontext())

    monkeypatch.setattr(service, "_get_model", lambda *_names: (fake_model, fake_app))

    result = service.get_cached_html(1, "abc", "a.xlsx")

    assert result is not None
    assert result["created_at"] == created_at


def test_get_excel_diff_data_accepts_string_created_at_from_cache(monkeypatch):
    app_module = _safe_import_app_or_skip()

    fake_repo = SimpleNamespace(id=1, name="repo", project=SimpleNamespace(name="p"))
    fake_commit = SimpleNamespace(id=7, commit_id="abc123", path="a.xlsx", repository=fake_repo)
    fake_commit_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda _id: fake_commit)
    )

    monkeypatch.setattr(app_module, "Commit", fake_commit_model)
    monkeypatch.setattr(app_module.excel_cache_service, "is_excel_file", lambda _path: True)
    monkeypatch.setattr(
        app_module.excel_html_cache_service,
        "get_cached_html",
        lambda *_args, **_kwargs: {
            "html_content": "<div>ok</div>",
            "css_content": "",
            "js_content": "",
            "metadata": {},
            "created_at": "2026-03-03 18:00:00",
        },
    )

    with app_module.app.test_request_context("/commits/7/excel-diff-data"):
        response = app_module.get_excel_diff_data(7)
    payload = response.get_json()

    assert payload["success"] is True
    assert payload["from_html_cache"] is True
    assert payload["created_at"] == "2026-03-03 18:00:00"


def test_repository_excel_previous_commit_query_has_time_and_id_tiebreak():
    content = _read("app.py")
    assert "Commit.commit_time == commit.commit_time" in content
    assert "Commit.id < commit.id" in content
