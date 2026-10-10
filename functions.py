from contextlib import closing
import json
from pathlib import Path
import re

from db import DEFAULT_DATABASE, connect


STATUSES = ("open", "in_progress", "closed")


class TicketNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank.")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters.")
    return value


def _identifier(ticket_id: int) -> int:
    if type(ticket_id) is not int or ticket_id < 1:
        raise ValueError("Ticket ID must be a positive integer.")
    return ticket_id


def create_ticket(
    title: str,
    message: str,
    database: Path = DEFAULT_DATABASE,
    *,
    idempotency_key: str | None = None,
) -> dict:
    title = _text(title, "Title", 120)
    message = _text(message, "Message", 10_000)
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
                    if ticket["title"] != title or ticket["message"] != message:
                        raise IdempotencyConflictError("Idempotency key was already used for different ticket content.")
                    return ticket
            cursor = connection.execute(
                "INSERT INTO tickets (title, message) VALUES (?, ?)",
                (title, message),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            connection.execute(
                "INSERT INTO ticket_events (ticket_id, to_status) VALUES (?, 'open')",
                (row["id"],),
            )
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
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")
    if type(offset) is not int or offset < 0:
        raise ValueError("Offset must be a non-negative integer.")
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
) -> dict:
    ticket_id = _identifier(ticket_id)
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    if not isinstance(note, str) or len(note.strip()) > 500:
        raise ValueError("Note must be text of at most 500 characters.")
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
            connection.execute(
                """INSERT INTO ticket_events (ticket_id, from_status, to_status, note)
                   VALUES (?, ?, ?, ?)""",
                (ticket_id, row["status"], status, note.strip()),
            )
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


def add_comment(ticket_id: int, body: str, database: Path = DEFAULT_DATABASE) -> dict:
    ticket_id = _identifier(ticket_id)
    body = _text(body, "Comment", 5_000)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
                raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
            cursor = connection.execute(
                "INSERT INTO ticket_comments (ticket_id, body) VALUES (?, ?)", (ticket_id, body)
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
