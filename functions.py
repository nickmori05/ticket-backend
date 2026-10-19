from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable

from db import DEFAULT_DATABASE, connect


STATUSES = ("open", "in_progress", "closed")
NOTIFICATION_STATUSES = ("pending", "sent", "failed")


class TicketNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class TokenNotFoundError(LookupError):
    pass


class NotificationNotFoundError(LookupError):
    pass


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank.")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters.")
    return value


def _author(author: str | None) -> str | None:
    return None if author is None else _text(author, "Author", 64)


def _email(email: str | None) -> str | None:
    if email is None:
        return None
    email = email.strip() if isinstance(email, str) else ""
    # ASCII only, with no spaces, commas, quotes, or angle brackets, so a sender can use it as a header unchanged.
    if len(email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", email):
        raise ValueError("Email must be an ASCII address like name@example.com, at most 254 characters.")
    return email


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")
    if type(offset) is not int or offset < 0:
        raise ValueError("Offset must be a non-negative integer.")


def _queue_notification(connection: sqlite3.Connection, ticket: sqlite3.Row, event_id: int, subject: str,
                        body: str) -> None:
    # Queued in the caller's transaction, so a notification exists exactly when its history entry does.
    if ticket["email"] is None:
        return
    title = " ".join(ticket["title"].split())
    connection.execute(
        "INSERT INTO ticket_notifications (ticket_id, event_id, recipient, subject, body) VALUES (?, ?, ?, ?, ?)",
        (ticket["id"], event_id, ticket["email"], f"Ticket #{ticket['id']} {subject}: {title}", body),
    )


def _identifier(identifier: int, name: str = "Ticket ID") -> int:
    if type(identifier) is not int or identifier < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return identifier


def create_ticket(
    title: str,
    message: str,
    database: Path = DEFAULT_DATABASE,
    *,
    idempotency_key: str | None = None,
    author: str | None = None,
    email: str | None = None,
) -> dict:
    title = _text(title, "Title", 120)
    message = _text(message, "Message", 10_000)
    author = _author(author)
    email = _email(email)
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", idempotency_key)
    ):
        raise ValueError("Idempotency key must be 1-128 ASCII letters, digits, dots, underscores, or hyphens.")
    with closing(connect(database)) as connection:
        with connection:
            if idempotency_key is not None:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT response_json FROM ticket_submissions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if previous is not None:
                    ticket = json.loads(previous["response_json"])
                    # Responses saved before tickets had an email have no email key.
                    if ticket["title"] != title or ticket["message"] != message or ticket.get("email") != email:
                        raise IdempotencyConflictError("Idempotency key was already used for different ticket content.")
                    return ticket
            cursor = connection.execute(
                "INSERT INTO tickets (title, message, email) VALUES (?, ?, ?)",
                (title, message, email),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            event = connection.execute(
                "INSERT INTO ticket_events (ticket_id, to_status, author) VALUES (?, 'open', ?)",
                (row["id"], author),
            )
            _queue_notification(connection, row, event.lastrowid, "received",
                                f"We received your ticket #{row['id']}. Its status is open.\n\n{message}")
            ticket = dict(row)
            if idempotency_key is not None:
                connection.execute(
                    "INSERT INTO ticket_submissions (idempotency_key, ticket_id, response_json) VALUES (?, ?, ?)",
                    (idempotency_key, ticket["id"], json.dumps(ticket, ensure_ascii=False)),
                )
        return ticket


def get_ticket(ticket_id: int, database: Path = DEFAULT_DATABASE) -> dict:
    ticket_id = _identifier(ticket_id)
    with closing(connect(database)) as connection:
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    if row is None:
        raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
    return dict(row)


def list_tickets(
    database: Path = DEFAULT_DATABASE,
    limit: int = 20,
    *,
    offset: int = 0,
    status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    _page(limit, offset)
    if status is not None and status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    clauses = []
    parameters = []
    if status is not None:
        clauses.append("status = ?")
        parameters.append(status)
    if search is not None:
        search = _text(search, "Search", 200)
        clauses.append("(instr(lower(title), lower(?)) > 0 OR instr(lower(message), lower(?)) > 0)")
        parameters.extend((search, search))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM tickets" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
        ]


def set_status(
    ticket_id: int,
    status: str,
    database: Path = DEFAULT_DATABASE,
    *,
    note: str = "",
    author: str | None = None,
) -> dict:
    ticket_id = _identifier(ticket_id)
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    if not isinstance(note, str) or len(note.strip()) > 500:
        raise ValueError("Note must be text of at most 500 characters.")
    author = _author(author)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
            if row["status"] == status:
                return dict(row)
            connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
            event = connection.execute(
                """INSERT INTO ticket_events (ticket_id, from_status, to_status, note, author)
                   VALUES (?, ?, ?, ?, ?)""",
                (ticket_id, row["status"], status, note.strip(), author),
            )
            body = f"Ticket #{ticket_id} changed from {row['status']} to {status}."
            _queue_notification(connection, row, event.lastrowid, f"is now {status}",
                                body + (f"\n\n{note.strip()}" if note.strip() else ""))
            return dict(connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone())


def ticket_history(ticket_id: int, database: Path = DEFAULT_DATABASE) -> list[dict]:
    ticket_id = _identifier(ticket_id)
    with closing(connect(database)) as connection:
        if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
            raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            )
        ]


def statistics(database: Path = DEFAULT_DATABASE) -> dict:
    with closing(connect(database)) as connection:
        counts = dict.fromkeys(STATUSES, 0)
        for row in connection.execute("SELECT status, COUNT(*) AS count FROM tickets GROUP BY status"):
            counts[row["status"]] = row["count"]
    return {"total": sum(counts.values()), "by_status": counts}


def add_comment(
    ticket_id: int,
    body: str,
    database: Path = DEFAULT_DATABASE,
    *,
    author: str | None = None,
) -> dict:
    ticket_id = _identifier(ticket_id)
    body = _text(body, "Comment", 5_000)
    author = _author(author)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
                raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
            cursor = connection.execute(
                "INSERT INTO ticket_comments (ticket_id, body, author) VALUES (?, ?, ?)",
                (ticket_id, body, author),
            )
            return dict(connection.execute(
                "SELECT * FROM ticket_comments WHERE id = ?", (cursor.lastrowid,)
            ).fetchone())


def list_comments(ticket_id: int, database: Path = DEFAULT_DATABASE) -> list[dict]:
    ticket_id = _identifier(ticket_id)
    with closing(connect(database)) as connection:
        if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
            raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            )
        ]


def list_notifications(
    database: Path = DEFAULT_DATABASE,
    limit: int = 20,
    *,
    offset: int = 0,
    status: str | None = None,
) -> list[dict]:
    _page(limit, offset)
    if status is not None and status not in NOTIFICATION_STATUSES:
        raise ValueError(f"Notification status must be one of: {', '.join(NOTIFICATION_STATUSES)}.")
    where = " WHERE status = ?" if status is not None else ""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM ticket_notifications" + where + " ORDER BY id LIMIT ? OFFSET ?",
                (*([status] if status is not None else []), limit, offset),
            )
        ]


def count_due_notifications(database: Path = DEFAULT_DATABASE) -> int:
    """Count pending notifications whose next attempt is due, so a sender can skip connecting when it is zero."""
    with closing(connect(database)) as connection:
        return connection.execute(
            """SELECT COUNT(*) FROM ticket_notifications
               WHERE status = 'pending' AND next_attempt_at <= CURRENT_TIMESTAMP"""
        ).fetchone()[0]


def retry_notifications(database: Path = DEFAULT_DATABASE, notification_ids: list[int] | None = None) -> list[dict]:
    """Move failed notifications back to pending with a fresh attempt budget, due immediately.

    With no IDs, every failed notification is requeued. Named IDs must all exist and be failed, or nothing changes.
    The last error is kept until the next attempt records its own outcome.
    """
    if notification_ids is not None:
        if not isinstance(notification_ids, (list, tuple)) or not notification_ids:
            raise ValueError("Notification IDs must be a non-empty list.")
        notification_ids = sorted({_identifier(identifier, "Notification ID") for identifier in notification_ids})
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if notification_ids is None:
                notification_ids = [row["id"] for row in connection.execute(
                    "SELECT id FROM ticket_notifications WHERE status = 'failed' ORDER BY id"
                )]
            for identifier in notification_ids:
                row = connection.execute(
                    "SELECT status FROM ticket_notifications WHERE id = ?", (identifier,)
                ).fetchone()
                if row is None:
                    raise NotificationNotFoundError(f"Notification #{identifier} was not found.")
                if row["status"] != "failed":
                    raise ValueError(f"Notification #{identifier} is {row['status']}; only failed notifications "
                                     "can be retried.")
            connection.executemany(
                """UPDATE ticket_notifications SET status = 'pending', attempts = 0,
                   next_attempt_at = CURRENT_TIMESTAMP WHERE id = ?""",
                [(identifier,) for identifier in notification_ids],
            )
            return [
                dict(connection.execute("SELECT * FROM ticket_notifications WHERE id = ?", (identifier,)).fetchone())
                for identifier in notification_ids
            ]


def deliver_notifications(
    send: Callable[[dict], None],
    database: Path = DEFAULT_DATABASE,
    *,
    limit: int = 20,
    max_attempts: int = 5,
    retry_delay: int = 60,
) -> dict:
    """Pass each due notification to send(), oldest first, and record the outcome.

    send() reports failure by raising. A failed attempt is retried after retry_delay seconds, doubling each
    time up to a day, until max_attempts is reached and the notification is marked failed.
    """
    if not callable(send):
        raise ValueError("Sender must be callable.")
    _page(limit, 0)
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("Maximum attempts must be a positive integer.")
    if type(retry_delay) is not int or retry_delay < 0:
        raise ValueError("Retry delay must be a non-negative number of seconds.")
    counts = {"sent": 0, "retrying": 0, "failed": 0}
    with closing(connect(database)) as connection:
        due = [
            dict(row) for row in connection.execute(
                """SELECT * FROM ticket_notifications WHERE status = 'pending'
                   AND next_attempt_at <= CURRENT_TIMESTAMP ORDER BY id LIMIT ?""",
                (limit,),
            )
        ]
        for notification in due:
            # Count the attempt and schedule its retry before sending, so a crash during send() or a second
            # sender running at the same time waits for the delay instead of sending again straight away.
            delay = min(retry_delay * 2 ** notification["attempts"], 86_400)
            with connection:
                claimed = connection.execute(
                    """UPDATE ticket_notifications SET attempts = attempts + 1, next_attempt_at = datetime('now', ?)
                       WHERE id = ? AND status = 'pending' AND attempts = ?""",
                    (f"+{delay} seconds", notification["id"], notification["attempts"]),
                ).rowcount
            if not claimed:
                continue
            notification = dict(connection.execute(
                "SELECT * FROM ticket_notifications WHERE id = ?", (notification["id"],)
            ).fetchone())
            try:
                send(notification)
            except Exception as error:
                outcome = "failed" if notification["attempts"] >= max_attempts else "retrying"
                with connection:
                    connection.execute(
                        "UPDATE ticket_notifications SET status = ?, last_error = ? WHERE id = ?",
                        ("failed" if outcome == "failed" else "pending",
                         (str(error) or type(error).__name__)[:500], notification["id"]),
                    )
            else:
                outcome = "sent"
                with connection:
                    connection.execute(
                        "UPDATE ticket_notifications SET status = 'sent', sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (notification["id"],),
                    )
            counts[outcome] += 1
    return counts


def _token_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("Token name must be 1-64 ASCII letters, digits, dots, underscores, or hyphens.")
    return name


def _token_hash(token: str) -> str:
    # Tokens are long random values, so a plain SHA-256 digest is enough to avoid storing them.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(name: str, database: Path = DEFAULT_DATABASE) -> dict:
    name = _token_name(name)
    token = "tkt_" + secrets.token_urlsafe(32)
    with closing(connect(database)) as connection:
        with connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO api_tokens (name, token_hash) VALUES (?, ?)", (name, _token_hash(token))
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"A token named {name} already exists.") from None
            row = connection.execute(
                "SELECT id, name, created_at, revoked_at FROM api_tokens WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    return {**dict(row), "token": token}


def list_tokens(database: Path = DEFAULT_DATABASE) -> list[dict]:
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT id, name, created_at, revoked_at FROM api_tokens ORDER BY id"
            )
        ]


def revoke_token(name: str, database: Path = DEFAULT_DATABASE) -> dict:
    name = _token_name(name)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) WHERE name = ?",
                (name,),
            )
            row = connection.execute(
                "SELECT id, name, created_at, revoked_at FROM api_tokens WHERE name = ?", (name,)
            ).fetchone()
    if row is None:
        raise TokenNotFoundError(f"Token {name} was not found.")
    return dict(row)


def authenticate(token: str | None, database: Path = DEFAULT_DATABASE) -> str | None:
    """Return the name of an active token, or None when the token is missing, unknown, or revoked."""
    if not isinstance(token, str) or not 1 <= len(token) <= 200:
        return None
    with closing(connect(database)) as connection:
        row = connection.execute(
            "SELECT name FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL", (_token_hash(token),)
        ).fetchone()
    return row["name"] if row is not None else None
