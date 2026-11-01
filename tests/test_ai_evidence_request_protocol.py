import json

from services.ai.context_tools import ContextTools
from services.ai.protocol import ContextRequest, parse_payload, sanitize_requests
from tests.test_ai_engine import COMMIT, TABLE, _scope


def test_json_requests_keep_repository_cursor_and_exact_excel_coordinate():
    parsed = parse_payload(json.dumps({"status": "need_more_context", "requests": [
        {"type": "file_content", "commit": COMMIT, "path": TABLE,
         "repository_id": "7", "lines": "#1!A262:H262"},
        {"type": "find_references", "query": "reference_name", "cursor": "opaque-page-2"},
    ]}))
    assert parsed.requests[0].repository_id == "7"
    assert parsed.requests[1].cursor == "opaque-page-2"
    # This simple scope intentionally has no repository attribution; test coordinates separately.
    allowed, _ = sanitize_requests([ContextRequest(type="file_content", commit=COMMIT, path=TABLE,
                                                   lines="#1!A262:H262")], _scope())
    assert allowed[0].lines == "#1!A262:H262"


def test_reference_cursor_changes_tool_cache_identity_and_reaches_provider():
    class Provider:
        def __init__(self):
            self.calls = []
        def find_references(self, query, path="", cursor="", max_output_chars=8000):
            self.calls.append(cursor)
            return "page " + (cursor or "first")
    provider = Provider()
    tools = ContextTools(provider=provider, max_tool_requests=3)
    tools.execute([ContextRequest(type="find_references", query="needle")])
    second = tools.execute([ContextRequest(type="find_references", query="needle", cursor="next")])
    assert provider.calls == ["", "next"]
    assert "next" in second.items[0].text


def test_malformed_rectangle_is_refused_rather_than_silently_reading_first_sheet():
    allowed, dropped = sanitize_requests([ContextRequest(type="file_content", commit=COMMIT,
        path=TABLE, lines="Sheet1!A0:B2")], _scope())
    assert not allowed and dropped
