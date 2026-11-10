"""First-party request counts.

A request is not a person. These rows are the admin ledger only.
Nothing here is a historic visitor import.
"""

from __future__ import annotations

import sqlite3


def record(conn: sqlite3.Connection, *, day: str, authed: bool, user_id: str | None, path_class: str) -> None:
    conn.execute(
        """
        INSERT INTO request_counts (day, authed, user_id, path_class)
        VALUES (?, ?, ?, ?)
        """,
        (day, 1 if authed else 0, user_id if authed else None, path_class),
    )


def metrics(conn: sqlite3.Connection) -> dict:
    anonymous = conn.execute(
        "SELECT COUNT(*) AS c FROM request_counts WHERE authed = 0"
    ).fetchone()["c"]
    authenticated = conn.execute(
        "SELECT COUNT(*) AS c FROM request_counts WHERE authed = 1"
    ).fetchone()["c"]
    accounts = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    unique_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS c FROM request_counts WHERE authed = 1 AND user_id IS NOT NULL"
    ).fetchone()["c"]
    attempts = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    accepted = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status IN ('accepted', 'completed', 'failed')"
    ).fetchone()["c"]
    completed = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'completed' AND result_json IS NOT NULL"
    ).fetchone()["c"]
    accounts_detail = conn.execute(
        """
        SELECT u.email, u.created_at, u.role,
               a.free_allowance, a.free_committed, a.paid_credits, a.paid_committed
        FROM users u
        JOIN ledger_accounts a ON a.user_id = u.id
        ORDER BY u.created_at ASC
        """
    ).fetchall()
    return {
        "anonymous_requests": anonymous,
        "authenticated_requests": authenticated,
        "accounts": accounts,
        "unique_signed_in_accounts": unique_users,
        "test_attempts": attempts,
        "engine_accepted_runs": accepted,
        "reports_received": completed,
        "historic_visitors": None,
        "accounts_detail": [dict(row) for row in accounts_detail],
    }


def allow(conn: sqlite3.Connection, bucket: str, now_epoch: int, window: int, limit: int) -> bool:
    from app.db import tx

    def work():
        row = conn.execute("SELECT window_start, count FROM rate_limits WHERE bucket = ?", (bucket,)).fetchone()
        if row is None or now_epoch - row["window_start"] >= window:
            conn.execute(
                """
                INSERT INTO rate_limits (bucket, window_start, count)
                VALUES (?, ?, 1)
                ON CONFLICT(bucket) DO UPDATE SET window_start = excluded.window_start, count = 1
                """,
                (bucket, now_epoch),
            )
            return True
        if row["count"] >= limit:
            return False
        conn.execute(
            "UPDATE rate_limits SET count = count + 1 WHERE bucket = ?",
            (bucket,),
        )
        return True

    return tx(conn, work)
