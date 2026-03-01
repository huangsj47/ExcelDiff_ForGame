import os
import re
from typing import Optional

SAFE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_segment(segment: Optional[str], fallback: str = "unknown") -> str:
    raw = str(segment or "").strip()
    if SAFE_SEGMENT_PATTERN.match(raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or fallback


def validate_segment(segment: Optional[str]) -> bool:
    if not segment:
        return False
    return bool(SAFE_SEGMENT_PATTERN.match(str(segment).strip()))


def build_repository_local_path(project_code: str, repository_name: str, repository_id: int, base_dir: str = "repos", strict: bool = False) -> str:
    if strict:
        if not validate_segment(project_code):
            raise ValueError("Invalid project code")
        if not validate_segment(repository_name):
            raise ValueError("Invalid repository name")

    safe_project = _sanitize_segment(project_code, "project")
    safe_repo = _sanitize_segment(repository_name, "repository")
    safe_id = int(repository_id)

    base_abs = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base_abs, f"{safe_project}_{safe_repo}_{safe_id}"))

    if not (candidate == base_abs or candidate.startswith(base_abs + os.sep)):
        raise ValueError("Repository path escapes base directory")

    return candidate

