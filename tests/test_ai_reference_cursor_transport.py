import re
from types import SimpleNamespace

import pytest

PAIRS = [(f"flat/{number:03}.lua", "a" * 40) for number in range(300)]


def next_cursor(text):
    return re.search(r'next_cursor="([^"]+)"', text).group(1)


def test_local_fallback_can_reach_a_tail_file_with_the_same_prefix(monkeypatch):
    import services.vcs_content_service as vcs
    from services.ai import provider_search as module
    monkeypatch.setattr(module, "is_agent_dispatch_mode", lambda: False)
    monkeypatch.setattr(vcs, "get_file_content_from_git",
                        lambda repository, commit, path: "hiddenNeedle" if path.endswith("299.lua") else "normal")
    class Provider(module.ReferenceSearchMixin):
        _search_cache = {}
        _reference_index = None
        _reference_index_key = None
        def _repository_of(self, pairs):
            return SimpleNamespace(id=7)

        def _attribution(self, pairs):
            return {}

        def _narrow_scope_note(self):
            return ""
    provider = Provider()
    first = provider._search_local(PAIRS, "hiddenNeedle", prefix="flat/")
    cursor = next_cursor(first)
    second = provider._search_local(PAIRS, "hiddenNeedle", prefix="flat/", cursor=cursor)
    assert "flat/299.lua:1:" in second
    assert first != second


def test_agent_executor_keeps_the_cursor_and_declared_batch_identity(monkeypatch):
    import services.vcs_content_service as vcs
    from services import agent_reference_search as module
    monkeypatch.setattr(module, "db", SimpleNamespace(session=SimpleNamespace(get=lambda *args: SimpleNamespace(id=7))))
    monkeypatch.setattr(vcs, "get_file_content_from_git",
                        lambda repository, commit, path: "hiddenNeedle" if path.endswith("299.lua") else "normal")
    payload = dict(repository_id=7, query="hiddenNeedle", entries=PAIRS, prefix="flat/", total_files=300)
    first = module.search_references_for_agent(payload)
    assert isinstance(first["next_cursor"], str) and first["next_cursor"]
    second = module.search_references_for_agent(dict(payload, cursor=first["next_cursor"]))
    assert "flat/299.lua:1:" in second["text"]
    assert second["scan_start"] == 240 and second["files_total"] == 300


def test_agent_task_dedup_separates_cursor_and_actual_output_budget(monkeypatch):
    from services import agent_file_content_dispatch as module
    captured = []
    monkeypatch.setattr(module, "_request_from_agent", lambda **kwargs: captured.append(kwargs) or {})
    repository = SimpleNamespace(id=7, project_id=2)
    module.request_references(repository, query="needle", entries=PAIRS, cursor="page1", max_output_chars=1800)
    module.request_references(repository, query="needle", entries=PAIRS, cursor="page2", max_output_chars=1800)
    module.request_references(repository, query="needle", entries=PAIRS, cursor="page1", max_output_chars=2000)
    assert len({entry["lines"] for entry in captured}) == 3
    assert captured[0]["extra_payload"]["cursor"] == "page1"
    assert captured[0]["extra_payload"]["max_output_chars"] == 1800


def test_real_local_batch_blob_is_size_checked_before_the_legacy_reader(tmp_path, monkeypatch):
    from services.ai import batch_reference as module
    from services.ai import frozen_repo
    from services.ai.reference_index import MAX_INDEX_FILE_BYTES, IndexReadLimit
    from tests.test_ai_frozen_repo_read_scope import _commit, _git
    _git(tmp_path, "init", "-q")
    (tmp_path / "huge.lua").write_bytes(b"x" * (MAX_INDEX_FILE_BYTES + 1))
    tip = _commit(tmp_path, "oversized blob")
    monkeypatch.setattr(frozen_repo, "_git_service_for", lambda repository: SimpleNamespace(local_path=str(tmp_path)))
    def legacy(*args):
        pytest.fail("An oversized Git blob must not reach the legacy unbounded read")
    reader = module.bounded_git_reader(SimpleNamespace(id=7, type="git"), legacy)
    with pytest.raises(IndexReadLimit):
        reader("huge.lua", tip)


def test_agent_cursor_relay_works_across_independent_signing_processes_and_routes_one_repo(monkeypatch):
    import services.vcs_content_service as vcs
    from services import agent_file_content_dispatch, agent_reference_search
    from services.ai import provider_search, reference_cursor
    monkeypatch.setattr(agent_reference_search, "db", SimpleNamespace(
        session=SimpleNamespace(get=lambda model, key: SimpleNamespace(id=key))))
    monkeypatch.setattr(vcs, "get_file_content_from_git",
                        lambda repository, commit, path: "hiddenNeedle" if path.endswith("299.lua") else "normal")
    calls = []
    def remote(repository, **payload):
        calls.append(repository.id)
        platform_key = reference_cursor._KEY
        reference_cursor._KEY = b"independent agent process key"
        try:
            outcome = agent_reference_search.search_references_for_agent(dict(payload, repository_id=repository.id))
        finally:
            reference_cursor._KEY = platform_key
        return dict(outcome, status="ready")
    monkeypatch.setattr(agent_file_content_dispatch, "request_references", remote)
    class Provider(provider_search.ReferenceSearchMixin):
        def _attribution(self, pairs):
            return {(path, commit): int(path[1]) for path, commit in pairs}

        def _repository_row(self, key):
            return SimpleNamespace(id=key)

        def _describe_repository(self, key):
            return str(key)

        def _narrow_scope_note(self):
            return ""
    pairs = [(f"r{repo}/{number:03}.lua", "a" * 40) for repo in [1, 2] for number in range(300)]
    provider = Provider()
    first = provider._search_from_agent(pairs, "hiddenNeedle", prefix="", total_files=600, max_output_chars=4000)
    cursor = next_cursor(first)
    calls.clear()
    second = provider._search_from_agent(pairs, "hiddenNeedle", prefix="", total_files=600,
                                          cursor=cursor, max_output_chars=4000)
    assert calls == [1]
    assert "r1/299.lua:1:" in second


@pytest.mark.parametrize("limit", [1800, 2500, 4000, 7800])
def test_relay_envelope_growth_is_included_in_the_output_budget(monkeypatch, limit):
    import services.vcs_content_service as vcs
    from services import agent_file_content_dispatch, agent_reference_search
    from services.ai import provider_search
    monkeypatch.setattr(agent_reference_search, "db", SimpleNamespace(session=SimpleNamespace(get=lambda *args: SimpleNamespace(id=7))))
    monkeypatch.setattr(vcs, "get_file_content_from_git", lambda *args: ("hiddenNeedle " * 50 + "\n") * 100)
    monkeypatch.setattr(agent_file_content_dispatch, "request_references", lambda repository, **payload:
                        dict(agent_reference_search.search_references_for_agent(dict(payload, repository_id=7)), status="ready"))
    provider = provider_search.ReferenceSearchMixin()
    text = provider._search_agent_one(SimpleNamespace(id=7), [("relay.lua", "b" * 40)], "hiddenNeedle",
                                       prefix="", total_files=1, max_output_chars=limit)
    assert len(text) <= limit
    assert next_cursor(text)


def test_many_agent_repositories_are_bounded_and_the_queued_repositories_are_reachable(monkeypatch):
    import services.vcs_content_service as vcs
    from services import agent_file_content_dispatch, agent_reference_search
    from services.ai import provider_search
    monkeypatch.setattr(agent_reference_search, "db", SimpleNamespace(session=SimpleNamespace(
        get=lambda model, key: SimpleNamespace(id=key))))
    monkeypatch.setattr(vcs, "get_file_content_from_git", lambda *args: "hiddenNeedle " * 100)
    monkeypatch.setattr(agent_file_content_dispatch, "request_references", lambda repository, **payload:
                        dict(agent_reference_search.search_references_for_agent(dict(payload, repository_id=repository.id)), status="ready"))
    class Provider(provider_search.ReferenceSearchMixin):
        def _attribution(self, pairs):
            return {(path, commit): int(path.split("/")[0]) for path, commit in pairs}

        def _repository_row(self, key):
            return SimpleNamespace(id=key)

        def _describe_repository(self, key):
            return str(key)

        def _narrow_scope_note(self):
            return ""
    pairs = [(f"{number}/tail.lua", "c" * 40) for number in range(1, 21)]
    provider = Provider()
    cursor, seen = "", set()
    for _ in range(24):
        text = provider._search_from_agent(pairs, "hiddenNeedle", prefix="", total_files=20,
                                           cursor=cursor, max_output_chars=7800)
        assert len(text) <= 7800
        seen.update(re.findall(r"\d+/tail.lua:1:", text))
        match = re.search(r'\[reference_repository_page\].*next_cursor="([^"]*)"', text)
        cursor = match.group(1) if match else ""
        if not cursor:
            break
    assert seen == {f"{number}/tail.lua:1:" for number in range(1, 21)}


def test_configured_signing_key_allows_continuation_across_worker_processes(monkeypatch):
    import os
    import subprocess
    import sys
    monkeypatch.setenv("AI_REFERENCE_CURSOR_KEY", "reference-cursor-test-key")
    encoded = subprocess.run([sys.executable, "-c", "from services.ai.reference_cursor import encode_cursor; print(encode_cursor('scope', 7, 240, 0))"],
                             capture_output=True, text=True, check=True).stdout.strip()
    env = dict(os.environ, REFERENCE_TEST_CURSOR=encoded)
    decoded = subprocess.run([sys.executable, "-c", "import os; from services.ai.reference_cursor import decode_cursor; print(decode_cursor(os.environ['REFERENCE_TEST_CURSOR'], scope='scope')['start'])"],
                             capture_output=True, text=True, env=env)
    assert decoded.returncode == 0, decoded.stderr
    assert decoded.stdout.strip() == "240"


def test_empty_body_cache_entries_are_bounded_too():
    from services.ai.batch_reference import ReferenceBodies
    cache = ReferenceBodies(max_bytes=30)
    for number in range(1000):
        cache.read((number, "empty.lua", "a" * 40), lambda: "")
    assert len(cache.values) <= 600
    assert cache.bytes <= 30
