"""Per-user credit ledger.

A credit is consumed when an acceptance id is recorded, or when an operator
resolves an unknown transmission as consumed. Acceptance alone is not a report.

Holds that still count against the five included tests:
reserved (not sent), dispatching (request handed to the socket), and unknown
(the reply was lost or incomplete, so the engine may still be working).

Automatic release is only for a hold that never left `reserved`, or for a
definite engine rejection after a complete response. A lost reply stays
`unknown` until an operator resolves it. Retrying that idempotency key does
not send a second request.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

from app.auth import iso, parse_iso, utcnow


HOLDING_STATES = ("reserved", "dispatching", "unknown")
FINAL_STATES = ("reserved", "dispatching")


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
        WHERE user_id = ? AND state IN ('reserved', 'dispatching', 'unknown')
        GROUP BY source
        """,
        (user_id,),
    ).fetchall()
    return {row["source"]: row["c"] for row in rows}


def snapshot(conn: sqlite3.Connection, user_id: str, *, lock_unverified: bool = False) -> dict:
    account = conn.execute(
        "SELECT * FROM ledger_accounts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    user = conn.execute("SELECT email_verified FROM users WHERE id = ?", (user_id,)).fetchone()
    verified = bool(user and user["email_verified"])
    locked = lock_unverified and not verified
    held = _held(conn, user_id)
    free_held = held.get("free", 0)
    paid_held = held.get("paid", 0)
    free_remaining = 0 if locked else account["free_allowance"] - account["free_committed"] - free_held
    paid_remaining = 0 if locked else account["paid_credits"] - account["paid_committed"] - paid_held
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
        "email_verified": verified,
        "credits_locked": locked,
        "entries": [dict(row) for row in entries],
    }


def sweep(conn: sqlite3.Connection, now_iso: str, ttl_seconds: int) -> int:
    """Release holds that never started transmission. Caller owns the transaction."""
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
        if _release_locked(
            conn,
            row["id"],
            now_iso,
            "Released: the test never left this server, and the unused hold expired.",
            allowed=("reserved",),
        ):
            released += 1
    return released


def reserve(
    conn: sqlite3.Connection,
    user_id: str,
    idempotency_key: str,
    question: str,
    now_iso: str,
    ttl_seconds: int,
    *,
    lock_unverified: bool = False,
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
        view = snapshot(conn, user_id, lock_unverified=lock_unverified)
        if view["credits_locked"]:
            return {"ok": False, "error": "verification_required", "snapshot": view}
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


def _release_locked(
    conn: sqlite3.Connection,
    reservation_id: str,
    now_iso: str,
    note: str,
    allowed: tuple[str, ...] = ("reserved",),
) -> bool:
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    if row is None or row["state"] not in allowed:
        return False
    placeholders = ",".join("?" for _ in allowed)
    updated = conn.execute(
        f"""
        UPDATE reservations
        SET state = 'released', updated_at = ?
        WHERE id = ? AND state IN ({placeholders})
        """,
        (now_iso, reservation_id, *allowed),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        """
        UPDATE runs
        SET status = CASE
              WHEN status IN ('reserved', 'dispatching', 'unknown') THEN 'released'
              ELSE status
            END,
            detail = CASE
              WHEN status IN ('reserved', 'dispatching', 'unknown') THEN ?
              ELSE detail
            END,
            updated_at = ?
        WHERE reservation_id = ?
        """,
        (note, now_iso, reservation_id),
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
        if row["state"] not in FINAL_STATES:
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
            WHERE id = ? AND state IN ('reserved', 'dispatching')
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


def mark_dispatching(conn: sqlite3.Connection, reservation_id: str, now_iso: str) -> bool:
    def work():
        updated = conn.execute(
            """
            UPDATE reservations
            SET state = 'dispatching', updated_at = ?
            WHERE id = ? AND state = 'reserved'
            """,
            (now_iso, reservation_id),
        )
        if updated.rowcount != 1:
            return False
        conn.execute(
            """
            UPDATE runs
            SET status = 'dispatching', updated_at = ?
            WHERE reservation_id = ? AND status = 'reserved'
            """,
            (now_iso, reservation_id),
        )
        return True

    from app.db import tx

    return tx(conn, work)


def mark_unknown(conn: sqlite3.Connection, reservation_id: str, now_iso: str, detail: str) -> dict:
    """Keep the hold. The request may have reached the engine."""

    def work():
        if not _unknown_locked(conn, reservation_id, now_iso, detail):
            row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
            if row is None:
                return {"changed": False, "state": None, "run": None}
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
            return {"changed": False, "state": row["state"], "run": dict(run) if run else None}
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO ledger_entries (
              id, user_id, reservation_id, kind, source, amount, note, created_at
            ) VALUES (?, ?, ?, 'unknown', ?, 0, ?, ?)
            """,
            (
                new_id(),
                row["user_id"],
                reservation_id,
                row["source"],
                "Held. The engine reply was lost or incomplete, so this credit stays reserved.",
                now_iso,
            ),
        )
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
        return {"changed": True, "state": "unknown", "run": dict(run)}

    from app.db import tx

    return tx(conn, work)


def quarantine_dispatching(conn: sqlite3.Connection, now_iso: str, min_age_seconds: int) -> int:
    """An in-flight send whose process is gone stays unknown. It is not released."""

    def work():
        if min_age_seconds <= 0:
            rows = conn.execute(
                "SELECT id FROM reservations WHERE state = 'dispatching'"
            ).fetchall()
        else:
            cutoff = iso(parse_iso(now_iso) - timedelta(seconds=min_age_seconds))
            rows = conn.execute(
                """
                SELECT id FROM reservations
                WHERE state = 'dispatching' AND updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
        changed = 0
        for row in rows:
            if _unknown_locked(
                conn,
                row["id"],
                now_iso,
                "Outcome unknown after a restart. The credit stays held until an operator resolves it.",
            ):
                changed += 1
        return changed

    from app.db import tx

    return tx(conn, work)


def _unknown_locked(conn: sqlite3.Connection, reservation_id: str, now_iso: str, detail: str) -> bool:
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    if row is None or row["state"] != "dispatching":
        return False
    updated = conn.execute(
        """
        UPDATE reservations SET state = 'unknown', updated_at = ?
        WHERE id = ? AND state = 'dispatching'
        """,
        (now_iso, reservation_id),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        """
        UPDATE runs
        SET status = 'unknown', detail = ?, engine_code = 'unknown', updated_at = ?
        WHERE id = ?
        """,
        (detail, now_iso, row["run_id"]),
    )
    return True


def resolve_unknown(conn: sqlite3.Connection, run_id: str, now_iso: str, action: str) -> dict | None:
    """Operator only. release or commit. Does not invent a report."""

    def work():
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        reservation = conn.execute(
            "SELECT * FROM reservations WHERE id = ?",
            (run["reservation_id"],),
        ).fetchone()
        if reservation is None or reservation["state"] not in {"unknown", "dispatching"}:
            return {"changed": False, "run": dict(run)}
        if action == "release":
            released = _release_locked(
                conn,
                reservation["id"],
                now_iso,
                "Released by an operator. The operator recorded that this send did not run.",
                allowed=("unknown", "dispatching"),
            )
            fresh = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return {"changed": released, "run": dict(fresh)}
        if action != "commit":
            return {"changed": False, "run": dict(run)}
        column = "free_committed" if reservation["source"] == "free" else "paid_committed"
        updated = conn.execute(
            """
            UPDATE reservations
            SET state = 'committed', updated_at = ?
            WHERE id = ? AND state IN ('unknown', 'dispatching')
            """,
            (now_iso, reservation["id"]),
        )
        if updated.rowcount != 1:
            fresh = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return {"changed": False, "run": dict(fresh)}
        conn.execute(
            f"UPDATE ledger_accounts SET {column} = {column} + 1 WHERE user_id = ?",
            (reservation["user_id"],),
        )
        conn.execute(
            """
            UPDATE runs
            SET status = 'accepted', detail = ?, updated_at = ?
            WHERE id = ?
            """,
            ("An operator counted this test. No report was added.", now_iso, run_id),
        )
        conn.execute(
            """
            INSERT INTO ledger_entries (
              id, user_id, reservation_id, kind, source, amount, note, created_at
            ) VALUES (?, ?, ?, 'commit', ?, 1, 'Counted one test by operator resolution. No report was written.', ?)
            """,
            (new_id(), reservation["user_id"], reservation["id"], reservation["source"], now_iso),
        )
        fresh = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return {"changed": True, "run": dict(fresh)}

    from app.db import tx

    return tx(conn, work)


def reopen_for_retry(conn: sqlite3.Connection, reservation_id: str, now_iso: str, detail: str) -> None:
    """429 concurrency: no engine row. Keep the same key and do not count a test."""

    def work():
        conn.execute(
            """
            UPDATE reservations
            SET state = 'reserved', updated_at = ?
            WHERE id = ? AND state = 'dispatching'
            """,
            (now_iso, reservation_id),
        )
        conn.execute(
            """
            UPDATE runs
            SET status = 'retry_later', detail = ?, updated_at = ?
            WHERE reservation_id = ?
            """,
            (detail, now_iso, reservation_id),
        )

    from app.db import tx

    tx(conn, work)


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
