from qkit_auth.routes import _normalize_user_sort_params


def test_normalize_user_sort_params_defaults():
    sort_by, sort_dir = _normalize_user_sort_params(None, None)
    assert sort_by == "username"
    assert sort_dir == "asc"


def test_normalize_user_sort_params_invalid_fallback():
    sort_by, sort_dir = _normalize_user_sort_params("unknown", "invalid")
    assert sort_by == "username"
    assert sort_dir == "asc"


def test_normalize_user_sort_params_accepts_supported_values():
    sort_by, sort_dir = _normalize_user_sort_params("email", "desc")
    assert sort_by == "email"
    assert sort_dir == "desc"
