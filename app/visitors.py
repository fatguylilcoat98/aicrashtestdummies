"""First-party visit notes. Raw IP addresses are not stored."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

BOT_MARKERS = (
    "googlebot",
    "bingbot",
    "duckduckbot",
    "baiduspider",
    "yandexbot",
    "applebot",
    "gptbot",
    "chatgpt-user",
    "claudebot",
    "bytespider",
    "petalbot",
    "semrush",
    "ahrefs",
    "facebookexternalhit",
    "slackbot",
    "twitterbot",
    "linkedinbot",
    "curl/",
    "wget/",
    "python-requests",
    "headlesschrome",
    "phantomjs",
    "scrapy",
    "httpclient",
    "go-http-client",
)


def classify_user_agent(user_agent: str) -> str:
    text = (user_agent or "").lower()
    if any(marker in text for marker in BOT_MARKERS):
        return "bot"
    return "unknown"


def client_ip(headers: dict, peer: str) -> str:
    """Use Cloudflare's connecting IP only when the socket peer is loopback."""
    host = peer or ""
    if host.startswith("::ffff:"):
        host = host[7:]
    if host in {"127.0.0.1", "::1", "localhost"}:
        forwarded = (headers.get("cf-connecting-ip") or "").strip()
        if forwarded and _ip_like(forwarded):
            return forwarded
    return host or "local"


def _ip_like(value: str) -> bool:
    if not value or len(value) > 64 or any(ch.isspace() for ch in value):
        return False
    if value.count(".") == 3 and all(part.isdigit() and 0 <= int(part) <= 255 for part in value.split(".")):
        return True
    return ":" in value and all(ch in "0123456789abcdefABCDEF:" for ch in value)


def ip_hash(secret: str, day: str, ip: str) -> str:
    return hmac.new(f"{secret}|{day}".encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def clean_path(value: str) -> str:
    raw = (value or "/").split("?", 1)[0].split("#", 1)[0].strip()
    if not raw.startswith("/") or raw.startswith("//") or ".." in raw:
        return "/"
    return raw[:120]


def clean_referrer(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = (parsed.path or "/").split("?", 1)[0][:80]
    return (parsed.netloc.lower() + path)[:160]


def record(conn: sqlite3.Connection, *, day: str, ts: str, ip_hash_value: str, kind: str, path: str, referrer: str, source: str) -> None:
    conn.execute(
        """
        INSERT INTO visit_hits (day, ts, ip_hash, kind, path, referrer, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (day, ts, ip_hash_value, kind, path, referrer, source),
    )


def recent_hit(conn: sqlite3.Connection, *, day: str, ip_hash_value: str, path: str, ts: str) -> bool:
    row = conn.execute(
        """
        SELECT ts FROM visit_hits
        WHERE day = ? AND ip_hash = ? AND path = ?
        ORDER BY id DESC LIMIT 1
        """,
        (day, ip_hash_value, path),
    ).fetchone()
    if row is None:
        return False
    try:
        previous = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        current = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return current - previous <= timedelta(minutes=10)


def mark_human(conn: sqlite3.Connection, *, day: str, ip_hash_value: str, path: str, ts: str) -> None:
    row = conn.execute(
        """
        SELECT id, ts FROM visit_hits
        WHERE day = ? AND ip_hash = ? AND path = ? AND kind = 'unknown'
        ORDER BY id DESC LIMIT 1
        """,
        (day, ip_hash_value, path),
    ).fetchone()
    if row is None:
        record(conn, day=day, ts=ts, ip_hash_value=ip_hash_value, kind="human", path=path, referrer="", source="interaction")
        return
    try:
        previous = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        current = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        previous = current = datetime.now(timezone.utc)
    if current - previous > timedelta(minutes=10):
        record(conn, day=day, ts=ts, ip_hash_value=ip_hash_value, kind="human", path=path, referrer="", source="interaction")
        return
    conn.execute("UPDATE visit_hits SET kind = 'human', source = 'interaction' WHERE id = ?", (row["id"],))


def summary(conn: sqlite3.Connection) -> dict:
    days = conn.execute(
        """
        SELECT day,
               SUM(CASE WHEN kind = 'human' THEN 1 ELSE 0 END) AS human_views,
               SUM(CASE WHEN kind = 'bot' THEN 1 ELSE 0 END) AS bot_views,
               SUM(CASE WHEN kind = 'unknown' THEN 1 ELSE 0 END) AS unknown_views,
               COUNT(DISTINCT CASE WHEN kind = 'human' THEN ip_hash END) AS human_uniques,
               COUNT(DISTINCT CASE WHEN kind = 'bot' THEN ip_hash END) AS bot_uniques,
               COUNT(DISTINCT CASE WHEN kind = 'unknown' THEN ip_hash END) AS unknown_uniques
        FROM visit_hits
        GROUP BY day
        ORDER BY day DESC
        LIMIT 14
        """
    ).fetchall()
    pages = conn.execute(
        """
        SELECT path, COUNT(*) AS views
        FROM visit_hits
        GROUP BY path
        ORDER BY views DESC
        LIMIT 8
        """
    ).fetchall()
    referrers = conn.execute(
        """
        SELECT referrer, COUNT(*) AS views
        FROM visit_hits
        WHERE referrer != ''
        GROUP BY referrer
        ORDER BY views DESC
        LIMIT 8
        """
    ).fetchall()
    signups = conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n
        FROM users GROUP BY day ORDER BY day DESC LIMIT 14
        """
    ).fetchall()
    tests = conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n
        FROM runs GROUP BY day ORDER BY day DESC LIMIT 14
        """
    ).fetchall()
    return {
        "days": [dict(row) for row in days],
        "pages": [dict(row) for row in pages],
        "referrers": [dict(row) for row in referrers],
        "signups": [dict(row) for row in signups],
        "crash_tests": [dict(row) for row in tests],
    }
