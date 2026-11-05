"""Per-user credit ledger.

Policy (failed and uncertain runs):
A credit is committed only when this process records an engine acceptance.
If the engine is not configured, refuses the run, times out, or the response
is lost, the reservation is released and the credit returns to the account.
That can under-charge the operator if a model call actually started. It does
not charge the account twice, and it does not leave a held credit behind.

The same account idempotency key always maps to one reservation. Retrying it
does not open a second hold. A new attempt needs a new key.

Stale holds with no recorded acceptance are released after the reservation
TTL (default 15 minutes). They are not converted into charges.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

from app.auth import iso, parse_iso, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


def ensure_account(conn: sqlite3.Connection, user_id: str, allowance: int) -> None:
    conn.execute(
        """
        INSERT INTO ledger_accounts (user_id, free_allowance, free_committed, paid_credits, paid_committed)
        VALUES (?, ?, 0, 0, 0)
        ON CONFLICT(user_id) DO NOTHING
        """,
        (user_id, allowance),
    )


def _held(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT source, COUNT(*) AS c
        FROM reservations
        WHERE user_id = ? AND state = 'reserved'
        GROUP BY source
        """,
        (user_id,),
    ).fetchall()
    return {row["source"]: row["c"] for row in rows}


def snapshot(conn: sqlite3.Connection, user_id: str) -> dict:
    account = conn.execute(
        "SELECT * FROM ledger_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    held = _held(conn, user_id)
    free_held = held.get("free", 0)
    paid_held = held.get("paid", 0)
    free_remaining = account["free_allowance"] - account["free_committed"] - free_held
    paid_remaining = account["paid_credits"] - account["paid_committed"] - paid_held
    entries = conn.execute(
        """
        SELECT kind, source, amount, note, created_at
        FROM ledger_entries
        WHERE user_id = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 8
        """,
        (user_id,),
    ).fetchall()
    return {
        "free_allowance": account["free_allowance"],
        "free_committed": account["free_committed"],
        "free_held": free_held,
        "free_remaining": free_remaining,
        "paid_credits": account["paid_credits"],
        "paid_committed": account["paid_committed"],
        "paid_held": paid_held,
        "paid_remaining": paid_remaining,
        "remaining": free_remaining + paid_remaining,
        "entries": [dict(row) for row in entries],
    }


def sweep(conn: sqlite3.Connection, now_iso: str, ttl_seconds: int) -> int:
    """Release holds that never recorded an acceptance. Caller owns the transaction."""
    cutoff = iso(parse_iso(now_iso) - timedelta(seconds=ttl_seconds))
    rows = conn.execute(
        """
        SELECT id FROM reservations
        WHERE state = 'reserved' AND created_at < ?
        """,
        (cutoff,),
    ).fetchall()
    released = 0
    for row in rows:
        if _release_locked(conn, row["id"], now_iso, "Released: no acceptance was recorded before the hold expired."):
            released += 1
    return released


def reserve(
    conn: sqlite3.Connection,
    user_id: str,
    idempotency_key: str,
    question: str,
    now_iso: str,
    ttl_seconds: int,
) -> dict:
    """Hold one credit or return the reservation already stored for this key."""

    def work():
        sweep(conn, now_iso, ttl_seconds)
        existing = conn.execute(
            """
            SELECT * FROM reservations
            WHERE user_id = ? AND idempotency_key = ?
            """,
            (user_id, idempotency_key),
        ).fetchone()
        if existing:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (existing["run_id"],)).fetchone()
            return {
                "ok": True,
                "idempotent": True,
                "created": False,
                "reservation": dict(existing),
                "run": dict(run),
            }
        view = snapshot(conn, user_id)
        if view["free_remaining"] > 0:
            source = "free"
        elif view["paid_remaining"] > 0:
            source = "paid"
        else:
            return {
                "ok": False,
                "error": "payment_required",
                "snapshot": view,
            }
        reservation_id = new_id()
        run_id = new_id()
        try:
            conn.execute(
                """
                INSERT INTO reservations (
                  id, user_id, idempotency_key, state, source, run_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?)
                """,
                (reservation_id, user_id, idempotency_key, source, run_id, now_iso, now_iso),
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                """
                SELECT * FROM reservations
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            ).fetchone()
            if existing is None:
                raise
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (existing["run_id"],)).fetchone()
            return {
                "ok": True,
                "idempotent": True,
                "created": False,
                "reservation": dict(existing),
                "run": dict(run) if run else None,
            }
        conn.execute(
            """
            INSERT INTO runs (
              id, user_id, reservation_id, status, question, created_at, updated_at
            ) VALUES (?, ?, ?, 'reserved', ?, ?, ?)
            """,
            (run_id, user_id, reservation_id, question, now_iso, now_iso),
        )
        conn.execute(
            """
            INSERT INTO ledger_entries (
              id, user_id, reservation_id, kind, source, amount, note, created_at
            ) VALUES (?, ?, ?, 'reserve', ?, 1, 'Held one test credit', ?)
            """,
            (new_id(), user_id, reservation_id, source, now_iso),
        )
        return {
            "ok": True,
            "idempotent": False,
            "created": True,
            "reservation_id": reservation_id,
            "run_id": run_id,
            "source": source,
        }

    from app.db import tx

    return tx(conn, work)


def _release_locked(conn: sqlite3.Connection, reservation_id: str, now_iso: str, note: str) -> bool:
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    if row is None or row["state"] != "reserved":
        return False
    updated = conn.execute(
        """
        UPDATE reservations
        SET state = 'released', updated_at = ?
        WHERE id = ? AND state = 'reserved'
        """,
        (now_iso, reservation_id),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        """
        UPDATE runs
        SET status = CASE WHEN status = 'reserved' THEN 'released' ELSE status END,
            updated_at = ?
        WHERE reservation_id = ?
        """,
        (now_iso, reservation_id),
    )
    conn.execute(
        """
        INSERT INTO ledger_entries (
          id, user_id, reservation_id, kind, source, amount, note, created_at
        ) VALUES (?, ?, ?, 'release', ?, 1, ?, ?)
        """,
        (new_id(), row["user_id"], reservation_id, row["source"], note, now_iso),
    )
    return True


def finalize(
    conn: sqlite3.Connection,
    reservation_id: str,
    now_iso: str,
    *,
    accepted: bool,
    run_status: str,
    engine_code: str | None,
    engine_ref: str | None,
    result_json: str | None,
    detail: str | None,
) -> dict:
    """Commit or release. A second call cannot charge again."""

    def work():
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        if row is None:
            return {"changed": False, "state": None}
        if row["state"] != "reserved":
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
            return {"changed": False, "state": row["state"], "run": dict(run)}
        if accepted:
            if row["source"] == "free":
                conn.execute(
                    """
                    UPDATE ledger_accounts
                    SET free_committed = free_committed + 1
                    WHERE user_id = ?
                    """,
                    (row["user_id"],),
                )
            else:
                conn.execute(
                    """
                    UPDATE ledger_accounts
                    SET paid_committed = paid_committed + 1
                    WHERE user_id = ?
                    """,
                    (row["user_id"],),
                )
            new_state = "committed"
            kind = "commit"
            note = "Counted one test. The engine accepted the run."
        else:
            new_state = "released"
            kind = "release"
            note = detail or "Released: the engine did not accept the run."
        updated = conn.execute(
            """
            UPDATE reservations
            SET state = ?, updated_at = ?
            WHERE id = ? AND state = 'reserved'
            """,
            (new_state, now_iso, reservation_id),
        )
        if updated.rowcount != 1:
            fresh = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
            return {"changed": False, "state": fresh["state"], "run": dict(run)}
        conn.execute(
            """
            UPDATE runs
            SET status = ?, engine_code = ?, engine_ref = ?, result_json = ?, detail = ?, updated_at = ?
            WHERE id = ?
            """,
            (run_status, engine_code, engine_ref, result_json, detail, now_iso, row["run_id"]),
        )
        conn.execute(
            """
            INSERT INTO ledger_entries (
              id, user_id, reservation_id, kind, source, amount, note, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (new_id(), row["user_id"], reservation_id, kind, row["source"], note, now_iso),
        )
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
        return {"changed": True, "state": new_state, "run": dict(run)}

    from app.db import tx

    return tx(conn, work)


def cancel(conn: sqlite3.Connection, user_id: str, run_id: str, now_iso: str) -> dict | None:
    def work():
        run = conn.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?",
            (run_id, user_id),
        ).fetchone()
        if run is None:
            return None
        released = _release_locked(
            conn,
            run["reservation_id"],
            now_iso,
            "Released: the run was cancelled before the engine accepted it.",
        )
        if released:
            conn.execute(
                """
                UPDATE runs
                SET status = 'cancelled', detail = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                ("Cancelled before the engine accepted the run.", now_iso, run_id, user_id),
            )
        fresh = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        reservation = conn.execute(
            "SELECT * FROM reservations WHERE id = ?",
            (run["reservation_id"],),
        ).fetchone()
        return {"released": released, "run": dict(fresh), "reservation": dict(reservation)}

    from app.db import tx

    return tx(conn, work)


def grant_paid(conn: sqlite3.Connection, user_id: str, credits: int, event_id: str, payload_hash: str, now_iso: str) -> dict:
    def work():
        existing = conn.execute(
            "SELECT * FROM payment_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing:
            return {"ok": True, "duplicate": True, "credits": existing["credits"], "user_id": existing["user_id"]}
        conn.execute(
            """
            INSERT INTO payment_events (event_id, user_id, credits, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, user_id, credits, payload_hash, now_iso),
        )
        conn.execute(
            "UPDATE ledger_accounts SET paid_credits = paid_credits + ? WHERE user_id = ?",
            (credits, user_id),
        )
        conn.execute(
            """
            INSERT INTO ledger_entries (
              id, user_id, reservation_id, kind, source, amount, note, created_at
            ) VALUES (?, ?, NULL, 'grant', 'paid', ?, 'Paid credits recorded from a verified webhook', ?)
            """,
            (new_id(), user_id, credits, now_iso),
        )
        return {"ok": True, "duplicate": False, "credits": credits, "user_id": user_id}

    from app.db import tx

    return tx(conn, work)


def read_run(conn: sqlite3.Connection, user_id: str, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE id = ? AND user_id = ?",
        (run_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def read_reservation(conn: sqlite3.Connection, reservation_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, status, question, engine_code, created_at, updated_at
        FROM runs
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]
