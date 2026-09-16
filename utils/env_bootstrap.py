#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment file bootstrap helpers for startup scripts.

Goals:
1. Generate `.env` with real line breaks (never literal `\\n` sequences).
2. Repair previously malformed one-line `.env` files containing escaped newlines.
3. Refuse to start when the template's placeholder secrets were copied verbatim.

Why goal 3 exists
-----------------
`.env.simple` is a *template*: it ships with human-readable placeholders such as
``please-change-me``.  Copying it to `.env` without editing produces a platform
whose `AGENT_SHARED_SECRET` is a publicly known constant (anyone can call
`/api/agents/register` and receive tasks) and whose `FLASK_SECRET_KEY` is
guessable (sessions / CSRF tokens can be forged).  A deployment that *looks*
configured but is not is worse than one that refuses to boot, so the startup
hook fails loudly instead of running insecurely.

Escape hatches (deliberately narrow):
- ``TESTING=1`` disables the check so the pytest suite and local experiments
  are never blocked (see ``tests/conftest.py``).
- Leaving a key *empty* is treated as "feature not configured" (warning only),
  preserving the long-standing behaviour where the platform falls back to an
  ephemeral random `FLASK_SECRET_KEY` and disables agent task dispatch.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import secrets
import sys
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

# 每个密钥要求的最小长度。取值依据：
# - FLASK_SECRET_KEY: `secrets.token_urlsafe(32)` 约 43 字符；32 是安全下限。
# - AGENT_SHARED_SECRET: 与平台/Agent 对称共享，属长期凭据；16 是安全下限。
_SECRET_MIN_LENGTHS: Dict[str, int] = {
    "FLASK_SECRET_KEY": 32,
    "AGENT_SHARED_SECRET": 16,
}

# 逐字节出现在仓库里的已知常量/占位值。命中任意一个即视为「照抄了模板」。
# `diff-platform-local-key` 来自 utils/security_utils.py 的兜底常量，
# `your-secret-key-here` 来自 config.py 的示例值 —— 两者同样是公开可猜的。
_PLACEHOLDER_SECRET_VALUES = frozenset(
    {
        "please-change-me",
        "please_change_me",
        "pleasechangeme",
        "change-me",
        "change_me",
        "changeme",
        "replace-me",
        "replace_me",
        "replaceme",
        "your-secret-key-here",
        "your-secret-here",
        "your-secret",
        "your_secret",
        "your-secret-key",
        "placeholder",
        "default",
        "secret",
        "test",
        "testing",
        "todo",
        "fixme",
        "xxx",
        "none",
        "null",
        "diff-platform-local-key",
    }
)

# 占位值常见措辞。命中即判定为占位（大小写不敏感）。
_PLACEHOLDER_SECRET_KEYWORDS = (
    "change-me",
    "change_me",
    "changeme",
    "replace",
    "placeholder",
    "your-secret",
    "your_secret",
)

# 含中日韩字符的「密钥」一定是人写的说明文字（例如「请替换为一个随机字符串」），
# 不可能是随机串。这条规则能覆盖未来新增的各种中文占位写法。
_CJK_PATTERN = re.compile(r"[一-鿿㐀-䶿]")

# 生成随机密钥的命令，用于给运维清晰指引。
_GENERATE_COMMAND = 'python -c "import secrets;print(secrets.token_urlsafe(48))"'


class InsecureEnvSecretError(RuntimeError):
    """启动前置校验失败：`.env` 里仍是占位/过短的密钥。"""


def build_default_env_lines() -> Tuple[list[str], Dict[str, str]]:
    """Build default `.env` lines and generated credentials.

    `AGENT_SHARED_SECRET` 必须是随机值：它曾被硬编码为 `please-change-me`
    （与 `.env.simple` 逐字节相同），照抄部署即可被任何人冒充 Agent。
    """
    flask_secret = secrets.token_urlsafe(48)
    admin_password = secrets.token_urlsafe(16)
    admin_token = secrets.token_urlsafe(32)
    agent_shared_secret = secrets.token_urlsafe(32)
    lines = [
        "# Auto-generated .env for Diff Platform",
        "HOST=0.0.0.0",
        "PORT=8002",
        "DEPLOYMENT_MODE=single",
        f"AGENT_SHARED_SECRET={agent_shared_secret}",
        "AUTH_BACKEND=local",
        f"FLASK_SECRET_KEY={flask_secret}",
        "ADMIN_USERNAME=admin",
        f"ADMIN_PASSWORD={admin_password}",
        f"ADMIN_API_TOKEN={admin_token}",
        "ENABLE_ADMIN_SECURITY=true",
        "AUTH_DEBUG_MODE=false",
        "DB_BACKEND=sqlite",
        "DEBUG_LOG=false",
        "BRANCH_REFRESH_COOLDOWN_SECONDS=120",
    ]
    creds = {
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": admin_password,
        "ADMIN_API_TOKEN": admin_token,
        "AGENT_SHARED_SECRET": agent_shared_secret,
    }
    return lines, creds


def render_env_text(lines: list[str]) -> str:
    """Render `.env` text using real newlines."""
    return "\n".join(lines) + "\n"


def is_escaped_newline_malformed_env(text: str) -> bool:
    """Detect malformed `.env` content written as a single line with `\\n` literals."""
    if "\\n" not in text:
        return False
    # Correct env files contain many real line breaks; malformed historical output usually has <= 1.
    return text.count("\n") <= 1


def repair_escaped_newline_env(env_path: pathlib.Path) -> bool:
    """Repair malformed `.env` content in-place. Returns True when file changed."""
    if not env_path.exists():
        return False
    text = env_path.read_text(encoding="utf-8")
    if not is_escaped_newline_malformed_env(text):
        return False
    fixed = text.replace("\\n", "\n")
    if not fixed.endswith("\n"):
        fixed += "\n"
    env_path.write_text(fixed, encoding="utf-8")
    return True


def ensure_env_file(env_path: pathlib.Path) -> Tuple[str, Dict[str, str]]:
    """Ensure `.env` exists and is valid.

    Returns:
    - action: one of `generated`, `repaired`, `ok`
    - creds: generated credentials only when action is `generated`, otherwise empty dict
    """
    if not env_path.exists():
        lines, creds = build_default_env_lines()
        env_path.write_text(render_env_text(lines), encoding="utf-8")
        return "generated", creds

    repaired = repair_escaped_newline_env(env_path)
    if repaired:
        return "repaired", {}
    return "ok", {}


def read_env_file_values(env_path: pathlib.Path) -> Dict[str, str]:
    """Parse a `.env` file into a plain dict.

    健壮性优先：忽略注释/空行/无 `=` 的行，容忍 `export KEY=VALUE` 与成对引号。
    解析失败（文件不存在/不可读）返回空 dict，由调用方的「未配置」分支处理。
    """
    values: Dict[str, str] = {}
    try:
        text = pathlib.Path(env_path).read_text(encoding="utf-8-sig")
    except OSError:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def is_testing_mode(environ: Optional[Mapping[str, str]] = None) -> bool:
    """`TESTING=1` 时跳过密钥强校验（测试/本地调试不能被卡住）。"""
    source = os.environ if environ is None else environ
    return str(source.get("TESTING") or "").strip().lower() in {"1", "true", "yes", "on"}


def is_placeholder_secret(value: str) -> bool:
    """判断一个值是否明显是模板占位值（而非随机密钥）。"""
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered in _PLACEHOLDER_SECRET_VALUES:
        return True
    if any(keyword in lowered for keyword in _PLACEHOLDER_SECRET_KEYWORDS):
        return True
    if _CJK_PATTERN.search(text):
        return True
    return False


def find_insecure_env_secrets(values: Mapping[str, str]) -> List[Tuple[str, str, str]]:
    """返回 `[(key, reason, 中文说明), ...]`，空列表表示密钥可用。

    - reason=`placeholder`: 照抄了模板占位值（公开可猜，必须拒绝启动）
    - reason=`too_short`: 长度不足，熵太低

    空值**不**在此处报错：那属于「未配置」，由各模块既有的降级逻辑处理
    （`FLASK_SECRET_KEY` 回退到运行期随机密钥，Agent 接口返回 503）。
    """
    issues: List[Tuple[str, str, str]] = []
    for key, min_length in _SECRET_MIN_LENGTHS.items():
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue
        if is_placeholder_secret(raw):
            issues.append(
                (
                    key,
                    "placeholder",
                    f"{key} 仍是模板里的占位值（当前长度 {len(raw)}），任何拿到本仓库的人都能猜到它",
                )
            )
            continue
        if len(raw) < min_length:
            issues.append(
                (
                    key,
                    "too_short",
                    f"{key} 长度仅 {len(raw)}，至少需要 {min_length} 个字符",
                )
            )
    return issues


def format_insecure_secret_guidance(
    issues: Iterable[Tuple[str, str, str]],
    env_path: Optional[pathlib.Path] = None,
) -> str:
    """把校验失败翻译成运维能直接照做中文指引。"""
    rows = list(issues)
    target = str(env_path) if env_path is not None else ".env"
    lines = [
        "",
        "=" * 68,
        "❌ 启动被拒绝：检测到未替换的占位密钥",
        "=" * 68,
        f"配置文件: {target}",
        "",
        "问题清单:",
    ]
    for key, reason, detail in rows:
        lines.append(f"  - [{reason}] {detail}")
    lines.extend(
        [
            "",
            "为什么必须修:",
            "  1) AGENT_SHARED_SECRET 是公开可猜的常量时，任何人都能调用",
            "     /api/agents/register 注册一个假 Agent 并领取任务；",
            "  2) FLASK_SECRET_KEY 可猜时，登录 session 与 CSRF token 可被伪造。",
            "",
            "修复步骤:",
            f"  1. 生成随机密钥: {_GENERATE_COMMAND}",
            "  2. 把生成的字符串分别填入 .env 的 FLASK_SECRET_KEY / AGENT_SHARED_SECRET",
            "     （两个密钥必须是各自独立生成的两个不同值）",
            "  3. Agent 节点机上的 AGENT_SHARED_SECRET 必须与平台侧完全一致",
            "  4. 改完重新执行启动脚本",
            "",
            "临时放行（仅供本地调试，切勿用于生产）:",
            "  set TESTING=1        (Windows cmd)",
            "  export TESTING=1     (Linux/macOS)",
            "=" * 68,
            "",
        ]
    )
    return "\n".join(lines)


def effective_env_values(
    env_path: pathlib.Path,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """合并 `.env` 文件与真实环境变量。

    真实环境变量优先 —— 与 `app.py` 的 `load_dotenv(override=False)` 保持一致。
    否则「环境变量里配了强密钥、.env 里留着占位值」的部署会被误判为不合格。
    """
    merged = dict(read_env_file_values(env_path))
    source = os.environ if environ is None else environ
    for key in _SECRET_MIN_LENGTHS:
        raw = source.get(key)
        if raw is not None and str(raw).strip():
            merged[key] = str(raw).strip()
    return merged


def check_env_file_secrets(
    env_path: pathlib.Path,
    environ: Optional[Mapping[str, str]] = None,
) -> List[Tuple[str, str, str]]:
    """启动阶段使用的密钥校验入口，返回问题列表（空表示通过）。"""
    return find_insecure_env_secrets(effective_env_values(env_path, environ))


def assert_env_secrets_secure(
    values: Mapping[str, str],
    env_path: Optional[pathlib.Path] = None,
) -> None:
    """校验失败即抛 `InsecureEnvSecretError`（fail-closed）。"""
    issues = find_insecure_env_secrets(values)
    if issues:
        raise InsecureEnvSecretError(format_insecure_secret_guidance(issues, env_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure .env file exists and is valid.")
    parser.add_argument("--env-path", default=".env", help="Path to env file (default: .env)")
    args = parser.parse_args()

    env_path = pathlib.Path(args.env_path)
    action, creds = ensure_env_file(env_path)

    if action == "generated":
        print("[INFO] .env generated successfully.")
        print(f"  ADMIN_USERNAME={creds['ADMIN_USERNAME']}")
        print(f"  ADMIN_PASSWORD={creds['ADMIN_PASSWORD']}")
        print(f"  ADMIN_API_TOKEN={creds['ADMIN_API_TOKEN']}")
        print(f"  AGENT_SHARED_SECRET={creds['AGENT_SHARED_SECRET']}")
        print("  [NOTE] Agent 节点机上的 AGENT_SHARED_SECRET 必须与此值一致。")
    elif action == "repaired":
        print("[INFO] Detected malformed .env with escaped newline literals; repaired in-place.")
    else:
        print("[INFO] .env already exists and format is valid.")

    if is_testing_mode():
        return 0

    issues = check_env_file_secrets(env_path)
    if issues:
        print(format_insecure_secret_guidance(issues, env_path), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
