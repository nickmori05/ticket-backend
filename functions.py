"""Ticket validation and SQLite operations."""

from contextlib import closing
from pathlib import Path

from db import DEFAULT_DATABASE, connect


class TicketNotFoundError(LookupError):
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


def create_ticket(title: str, message: str, database: Path = DEFAULT_DATABASE) -> dict:
    title = _text(title, "Title", 120)
    message = _text(message, "Message", 10_000)
    with closing(connect(database)) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT INTO tickets (title, message) VALUES (?, ?)",
                (title, message),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)


def get_ticket(ticket_id: int, database: Path = DEFAULT_DATABASE) -> dict:
    ticket_id = _identifier(ticket_id)
    with closing(connect(database)) as connection:
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    if row is None:
        raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
    return dict(row)


def list_tickets(database: Path = DEFAULT_DATABASE, limit: int = 20) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]
