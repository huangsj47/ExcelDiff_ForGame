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


def sanitize_text(text: Optional[str]) -> str:
    if text is None:
        return ""
    safe = str(text)
    safe = re.sub(r"(?<=://)([^:@/\s]+):([^@/\s]+)@", r"\1:***@", safe)
    safe = re.sub(r"(oauth2:)([^@/\s]+)@", r"\1***@", safe)
    return safe


def validate_repository_name(name: Optional[str]) -> bool:
    if not name:
        return False
    return bool(REPO_NAME_PATTERN.match(name))

