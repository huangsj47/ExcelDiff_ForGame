from __future__ import annotations

import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_ruff_changed.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_ruff_changed", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_changed_lines_from_patch_parses_hunks():
    module = _load_module()
    patch_text = "\n".join(
        [
            "diff --git a/services/a.py b/services/a.py",
            "@@ -10,0 +11,3 @@",
            "+x = 1",
            "+y = 2",
            "+z = 3",
            "@@ -30,1 +35,2 @@",
            "-old",
            "+new1",
            "+new2",
            "diff --git a/services/b.py b/services/b.py",
            "@@ -1,0 +1,1 @@",
            "+hello = 'world'",
        ]
    )

    changed = module._extract_changed_lines_from_patch(patch_text)
    assert changed["services/a.py"] == {11, 12, 13, 35, 36}
    assert changed["services/b.py"] == {1}


def test_filter_ruff_issues_only_keeps_changed_lines():
    module = _load_module()
    changed_lines = {
        "services/a.py": {10, 20},
        "services/b.py": {3},
    }
    issues = [
        {"filename": "services/a.py", "location": {"row": 10}, "code": "F401"},
        {"filename": "services/a.py", "location": {"row": 99}, "code": "F821"},
        {"filename": "services/b.py", "location": {"row": 3}, "code": "E402"},
    ]

    kept, ignored = module._filter_ruff_issues_by_changed_lines(issues, changed_lines)
    assert [item["code"] for item in kept] == ["F401", "E402"]
    assert [item["code"] for item in ignored] == ["F821"]
