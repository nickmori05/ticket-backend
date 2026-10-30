"""Idempotency keys for ticket submissions. Each caller has its own keys, so one token cannot replay another's.

A caller is identified by its author: the token name over HTTP, or no author from the terminal. Keys saved before
keys were scoped are read from the older ticket_submissions table and never rewritten. Keys are kept until
prune_submission_keys deletes them by age; after that, the same key creates a new ticket.
"""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.validation import require_age_days


def require_key(key: str | None) -> str | None:
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key)):
        raise ValueError("Idempotency key must be 1-128 ASCII letters, digits, dots, underscores, or hyphens.")
    return key


def _owner(author: str | None) -> str:
    # Authors are never blank, so the empty string cannot collide with a token name.
    return author if author is not None else ""


def saved_submission(connection: sqlite3.Connection, key: str, author: str | None) -> dict | None:
    """Return the response saved for this caller's key, or None when the caller has not used it."""
    row = connection.execute(
        "SELECT response_json FROM ticket_submission_keys WHERE owner = ? AND idempotency_key = ?",
        (_owner(author), key),
    ).fetchone()
    if row is None:
        # An older key belongs to whoever created its ticket. Tickets created without a recorded author, from the
        # terminal or before authors existed, have no known owner, so their keys stay shared as they always were.
        row = connection.execute(
            """SELECT response_json FROM ticket_submissions AS submission WHERE idempotency_key = ?
               AND COALESCE((SELECT author FROM ticket_events WHERE ticket_id = submission.ticket_id
                             ORDER BY id LIMIT 1), '') IN ('', ?)""",
            (key, _owner(author)),
        ).fetchone()
    return json.loads(row["response_json"]) if row is not None else None


def save_submission(connection: sqlite3.Connection, key: str, author: str | None, ticket: dict) -> None:
    """Record the response for this caller's key in the transaction that created the ticket."""
    connection.execute(
        "INSERT INTO ticket_submission_keys (owner, idempotency_key, ticket_id, response_json) VALUES (?, ?, ?, ?)",
        (_owner(author), key, ticket["id"], json.dumps(ticket, ensure_ascii=False)),
    )


def prune_submission_keys(older_than_days: int, database: Path = DEFAULT_DATABASE) -> dict:
    """Delete idempotency keys saved more than a number of days ago and return how many were deleted.

    Older keys have no saved time, so they are aged by their ticket, which was created in the same transaction.
    Tickets are kept; only the ability to replay their submission ends.
    """
    age = require_age_days(older_than_days)
    with closing(connect(database)) as connection, connection:
        deleted = connection.execute(
            "DELETE FROM ticket_submission_keys WHERE created_at < datetime('now', ?)", (age,)
        ).rowcount
        deleted += connection.execute(
            """DELETE FROM ticket_submissions WHERE ticket_id IN
               (SELECT id FROM tickets WHERE created_at < datetime('now', ?))""",
            (age,),
        ).rowcount
    return {"deleted": deleted}
