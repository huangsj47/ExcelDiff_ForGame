from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_repository_code_has_no_legacy_query_get_calls():
    scan_roots = [
        PROJECT_ROOT / "agent",
        PROJECT_ROOT / "auth",
        PROJECT_ROOT / "bootstrap",
        PROJECT_ROOT / "models",
        PROJECT_ROOT / "qkit_auth",
        PROJECT_ROOT / "routes",
        PROJECT_ROOT / "scripts",
        PROJECT_ROOT / "services",
        PROJECT_ROOT / "utils",
    ]
    offenders = []

    app_file = PROJECT_ROOT / "app.py"
    if app_file.exists():
        scan_roots.append(app_file)

    for root in scan_roots:
        if root.is_file():
            candidates = [root]
        else:
            candidates = list(root.rglob("*.py"))

        for path in candidates:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if ".query.get(" in text:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []
