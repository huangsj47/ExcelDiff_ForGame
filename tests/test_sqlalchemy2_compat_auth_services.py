from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_auth_services_no_query_get_legacy_calls():
    content = _read("auth/services.py")
    assert ".query.get(" not in content


def test_auth_services_use_session_get_for_primary_lookup():
    content = _read("auth/services.py")
    assert "db.session.get(AuthUser, user_id)" in content
    assert "db.session.get(AuthFunction, function_id)" in content
    assert "db.session.get(AuthProjectJoinRequest, request_id)" in content
    assert "db.session.get(AuthProjectCreateRequest, request_id)" in content

