"""Password hashing and session cookies. Session tokens are stored only as hashes."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

EMAIL_RE = re.compile(r"^[^@\s/]{1,64}@[^@\s/]+\.[^@\s/]+$")
MIN_PASSWORD = 12
MAX_PASSWORD = 200

_DUMMY: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, derived_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(derived, bytes.fromhex(derived_hex))
    except (ValueError, TypeError):
        return False


def dummy_verify(password: str) -> None:
    """Spend a hash when the account does not exist so login timing is closer."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-account-password")
    verify_password(password, _DUMMY)


def normalize_email(email: str) -> str | None:
    if not isinstance(email, str):
        return None
    cleaned = email.strip().lower()
    if len(cleaned) > 254 or not EMAIL_RE.match(cleaned):
        return None
    if any(ord(ch) < 32 for ch in cleaned):
        return None
    return cleaned


def password_problem(password: str) -> str | None:
    if not isinstance(password, str):
        return "Use a password of at least 12 characters."
    if len(password) < MIN_PASSWORD:
        return "Use a password of at least 12 characters."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    if any(ord(ch) < 32 for ch in password):
        return "The password contains a character this form cannot store."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def session_expiry(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)


def client_bucket(secret: str, client: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), client.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:24]
