import os
import re
from typing import Optional

from utils.runtime_paths import default_repos_base_dir, resolve_runtime_path

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


def build_repository_local_path(project_code: str, repository_name: str, repository_id: int, base_dir: Optional[str] = None, strict: bool = False) -> str:
    if strict:
        if not validate_segment(project_code):
            raise ValueError("Invalid project code")
        if not validate_segment(repository_name):
            raise ValueError("Invalid repository name")

    safe_project = _sanitize_segment(project_code, "project")
    safe_repo = _sanitize_segment(repository_name, "repository")
    safe_id = int(repository_id)

    # base_dir 的相对路径按**仓库根目录**解析，不是当前工作目录。
    # 这里曾经是 `os.path.abspath(base_dir)`：于是 git/svn 工作副本的落点取决于
    # 进程的 CWD。换个目录启动（Windows 服务、systemd 的 WorkingDirectory、
    # `cd /` 后跟绝对路径）就会在别处新建一个空的 repos/，所有仓库都被判定为
    # 「未克隆」而重新 clone 一遍，旧目录成为孤儿。详见 utils/runtime_paths.py。
    #
    # 不传 base_dir（平台 GitService/SvnService 等就是如此）时用
    # `default_repos_base_dir()`：默认仍是 `repos`（平台现有部署不变），但
    # `AGENT_REPOS_BASE_DIR` 可以把它指向别处 —— Agent 侧写工作副本用的是同一个
    # 变量、同一条锚定规则，于是「同步写入的目录」与「Diff 读取的目录」必然相同。
    base_abs = resolve_runtime_path(base_dir, default_relative=default_repos_base_dir())
    candidate = os.path.abspath(os.path.join(base_abs, f"{safe_project}_{safe_repo}_{safe_id}"))

    if not (candidate == base_abs or candidate.startswith(base_abs + os.sep)):
        raise ValueError("Repository path escapes base directory")

    return candidate

