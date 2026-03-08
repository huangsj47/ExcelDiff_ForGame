from __future__ import annotations

import builtins

import services.git_excel_parser_helpers as excel_helpers


class _FakeBlob:
    def __init__(self, payload: bytes):
        self.data_stream = type("_DS", (), {"read": lambda _self: payload})()


class _FakeTree:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __truediv__(self, _file_path):
        return _FakeBlob(self._payload)

    def __getitem__(self, _file_path):
        return object()


class _FakeCommit:
    def __init__(self, payload: bytes = b"xlsx-bytes"):
        self.tree = _FakeTree(payload)
        self.parents = []


def test_git_excel_parser_exception_tuples_are_declared():
    assert hasattr(excel_helpers, "GIT_EXCEL_PARSE_ERRORS")
    assert hasattr(excel_helpers, "GIT_EXCEL_REPO_INIT_ERRORS")
    assert hasattr(excel_helpers, "GIT_EXCEL_WORKBOOK_PARSE_ERRORS")
    assert hasattr(excel_helpers, "GIT_EXCEL_EXTRACT_ERRORS")
    assert hasattr(excel_helpers, "GIT_EXCEL_SIMPLE_EXTRACT_ERRORS")


def test_parse_excel_diff_returns_none_when_repo_init_fails(monkeypatch, tmp_path):
    service = type(
        "_S",
        (),
        {
            "local_path": str(tmp_path),
            "_extract_excel_data": lambda *_a, **_k: {},
            "_generate_excel_diff_data": lambda *_a, **_k: {},
        },
    )()
    monkeypatch.setattr(excel_helpers.git, "Repo", lambda _path: (_ for _ in ()).throw(OSError("repo init failed")))

    result = excel_helpers.parse_excel_diff(service, "abc", "a.xlsx")
    assert result is None


def test_parse_excel_diff_returns_error_payload_when_commit_parse_fails(monkeypatch, tmp_path):
    class _Repo:
        def __init__(self, _path):
            pass

        def commit(self, _commit_id):
            raise RuntimeError("commit parse failed")

    service = type(
        "_S",
        (),
        {
            "local_path": str(tmp_path),
            "_extract_excel_data": lambda *_a, **_k: {},
            "_generate_excel_diff_data": lambda *_a, **_k: {},
        },
    )()
    monkeypatch.setattr(excel_helpers.git, "Repo", _Repo)

    result = excel_helpers.parse_excel_diff(service, "abc", "a.xlsx")
    assert isinstance(result, dict)
    assert result.get("error") is True
    assert "无法解析Excel差异" in result.get("message", "")


def test_extract_excel_data_falls_back_to_simple_parser_when_load_workbook_fails(monkeypatch):
    service = type(
        "_S",
        (),
        {
            "_detect_data_bounds": lambda *_a, **_k: {"max_row": 1, "max_col": 1},
            "_get_column_letter": lambda *_a, **_k: "A",
            "_extract_excel_data_simple": (
                lambda _self, excel_data, file_path: {"Sheet1": [{"A": f"fallback:{file_path}:{len(excel_data)}"}]}
            ),
        },
    )()
    commit = _FakeCommit(b"excel-bytes")

    original_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openpyxl":
            fake_module = type(
                "_M",
                (),
                {"load_workbook": lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad workbook"))},
            )
            return fake_module
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    result = excel_helpers.extract_excel_data(service, commit, "demo.xlsx")
    assert result == {"Sheet1": [{"A": "fallback:demo.xlsx:11"}]}


def test_extract_excel_data_returns_none_on_outer_extraction_error():
    service = type("_S", (), {"_extract_excel_data_simple": lambda *_a, **_k: {"x": 1}})()
    bad_commit = type("_BadCommit", (), {"tree": None})()
    result = excel_helpers.extract_excel_data(service, bad_commit, "demo.xlsx")
    assert result is None


def test_extract_excel_data_simple_returns_none_on_known_error():
    class _BadLen:
        def __len__(self):
            raise TypeError("len failed")

    assert excel_helpers.extract_excel_data_simple(_BadLen(), "demo.xlsx") is None
