import base64
import hashlib
import os
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_PREFIX = "enc::"
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _derive_fernet_key() -> bytes:
    raw_key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY")
    if raw_key:
        material = raw_key.encode("utf-8")
    else:
        fallback = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or "diff-platform-local-key"
        material = fallback.encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return base64.urlsafe_b64encode(digest)


def get_fernet() -> Fernet:
    return Fernet(_derive_fernet_key())


def encrypt_credential(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith(ENCRYPTION_PREFIX):
        return text
    token = get_fernet().encrypt(text.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTION_PREFIX}{token}"


def decrypt_credential(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not text.startswith(ENCRYPTION_PREFIX):
        return text
    payload = text[len(ENCRYPTION_PREFIX):]
    try:
        return get_fernet().decrypt(payload.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Backward compatibility: if decryption fails, return raw payload.
        return payload


def sanitize_url(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if "@" not in parsed.netloc:
            return url
        userinfo, host = parsed.netloc.rsplit("@", 1)
        if ":" in userinfo:
            username, _ = userinfo.split(":", 1)
            userinfo = f"{username}:***"
        else:
            userinfo = "***"
        safe_netloc = f"{userinfo}@{host}"
        return urlunsplit((parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return re.sub(r"(?<=://)([^:@/\s]+):([^@/\s]+)@", r"\1:***@", str(url))


# 会被日志/错误信息带出去的敏感参数名（小写比较）。
# 用途见 redact_command_args / sanitize_text。
SECRET_ARG_NAMES = frozenset({
    "password", "passwd", "pwd", "token", "api-token", "auth-token",
    "access-token", "secret", "shared-secret", "client-secret",
    "credential", "credentials", "private-key",
})


def redact_command_args(cmd) -> str:
    """把 argv 列表渲染成可安全写日志的字符串。

    **为什么不能靠索引切片**：仓库里原先的写法是
    `' '.join(cmd[:3] + ['[认证信息已隐藏]'] + cmd[7:])` —— 靠「凭据正好落在第 5~6 位」
    这个假设工作。一旦参数顺序变化（加了 `-r`、换了子命令、凭据改用等号形式），
    切片就会把 `--password` 后面的**值**漏出来；`services/svn_service.py` 的
    `cmd[6:]` 就是这么把明文口令写进 logs/runlog.log 的（已实测复现）。

    这里改为按**参数名**判定：任何在 SECRET_ARG_NAMES 里的 `--flag value`，
    以及 `--flag=value` 形式，值一律替换成 `***`。与参数位置无关。

    用法：`log_print(f"...: {redact_command_args(cmd)}", 'SVN')`
    """
    if cmd is None:
        return ""
    if isinstance(cmd, str):
        # 已经是字符串的，交给 sanitize_text 做文本级脱敏
        return sanitize_text(cmd)

    parts = []
    redact_next = False
    for item in cmd:
        token = "" if item is None else str(item)
        if redact_next:
            parts.append("***")
            redact_next = False
            continue
        if token.startswith("-"):
            name, sep, value = token.partition("=")
            bare = name.lstrip("-").lower()
            if bare in SECRET_ARG_NAMES:
                if sep:
                    parts.append(f"{name}=***")
                else:
                    parts.append(token)
                    redact_next = True
                continue
        # URL 内嵌凭据（`svn checkout https://user:pass@host/...`）也要打码
        if "://" in token:
            token = sanitize_text(token)
        parts.append(token)
    return " ".join(parts)


SECRET_ARG_ALT = "|".join(sorted(SECRET_ARG_NAMES, key=len, reverse=True))

# `scheme://<rest>`（rest 到空白为止）。凭据判定在函数里做，见 _mask_url_credentials。
_URL_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<rest>[^\s]+)")

# `--flag<sep><value>`。三种 sep 都要认：
#   `--password s3cr3t`（shell 形态）
#   `--password=s3cr3t`（等号形态）
#   `'--password', 's3cr3t'`（Python 列表 repr —— TimeoutExpired 的消息就是这个形态）
_SECRET_KV_RE = re.compile(
    r"(?i)(?P<flag>--?(?:" + SECRET_ARG_ALT + r"))"
    r"(?P<sep>\s*[=:]\s*|\s*['\"]\s*,\s*['\"]\s*|\s+)"
    r"(?P<q>['\"]?)(?P<val>[^'\"\s,]+)(?P=q)"
)


def _mask_url_credentials(match: "re.Match") -> str:
    """把 URL 里的口令打码。

    用 `rpartition('@')` 取**最后一个** @ 之前的整段 userinfo —— 口令本身含 `@`
    或 `/` 时才不会漏出尾部片段。原先的正则 `([^:@/\\s]+):([^@/\\s]+)@` 两个字符类
    都排除 `@` 和 `/`，于是：
        https://oauth2:p@ss@host/x      → 漏出 `ss@host`
        https://oauth2:ghp_AA/BB@host/x → 完全不匹配，token 原样输出
    """
    rest = match.group("rest")
    if "@" not in rest:
        return match.group(0)
    userinfo, _, hostpart = rest.rpartition("@")
    if ":" not in userinfo:
        return match.group(0)
    user, _, _pwd = userinfo.partition(":")
    return f"{match.group('scheme')}{user}:***@{hostpart}"


def sanitize_text(text: Optional[str]) -> str:
    """脱敏自由文本（日志行、异常字符串）。

    覆盖两类形态：
      * URL 内嵌凭据 —— `scheme://user:pass@host`
      * 参数形态 —— `--password value` / `--password=value` / `'--password', 'value'`

    第二类必须覆盖，因为 `subprocess.TimeoutExpired.__str__` 会把**整条命令行**
    原样拼进异常信息（实测：`Command '['svn','cleanup',...,'--password','S3CR3T',...]'
    timed out after 120 seconds`）—— 只脱敏 URL 是挡不住的。

    已知局限：不识别裸的 `user:pass`（没有 `--flag` 也没有 URL 包裹）形态。
    这是刻意的 —— 那种文本里的 `alice:s3cr3t` 与日志时间戳 `12:30:45` 无法区分，
    强行匹配会把日志里的时间全部打码。请改用 redact_command_args 处理 argv。
    """
    if text is None:
        return ""
    safe = str(text)
    safe = _URL_RE.sub(_mask_url_credentials, safe)
    safe = _SECRET_KV_RE.sub(r"\g<flag>\g<sep>\g<q>***\g<q>", safe)
    return safe


def validate_repository_name(name: Optional[str]) -> bool:
    if not name:
        return False
    return bool(REPO_NAME_PATTERN.match(name))

