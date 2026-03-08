from __future__ import annotations

import services.git_diff_helpers as git_diff_helpers


class _BadDataFrame:
    def astype(self, _dtype):
        raise TypeError("astype failed")


class _BadContent:
    def splitlines(self):
        raise ValueError("splitlines failed")


def test_git_diff_helpers_exception_tuples_are_declared():
    assert hasattr(git_diff_helpers, "GIT_DIFF_HELPER_DF_COMPARE_ERRORS")
    assert hasattr(git_diff_helpers, "GIT_DIFF_HELPER_BASIC_DIFF_ERRORS")
    assert hasattr(git_diff_helpers, "GIT_DIFF_HELPER_INITIAL_DIFF_ERRORS")


def test_compare_dataframes_returns_empty_on_known_errors():
    result = git_diff_helpers.compare_dataframes(_BadDataFrame(), _BadDataFrame(), "Sheet1")
    assert result == []


def test_generate_basic_diff_returns_none_on_known_errors():
    result = git_diff_helpers.generate_basic_diff(_BadContent(), "new", "demo.txt")
    assert result is None


def test_generate_initial_commit_diff_returns_none_on_known_errors():
    result = git_diff_helpers.generate_initial_commit_diff(_BadContent(), "demo.txt")
    assert result is None
