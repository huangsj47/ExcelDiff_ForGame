from __future__ import annotations

import re
from pathlib import Path


def test_add_git_repository_url_fields_use_expected_column_widths():
    content = Path("templates/add_git_repository.html").read_text(encoding="utf-8")

    assert re.search(r'<div class="col-md-9">\s*<label for="url"', content)
    assert re.search(r'<div class="col-md-3">\s*<label for="server_url"', content)
