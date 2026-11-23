"""Tickets, their status history, and comments."""

from contextlib import closing
from pathlib import Path

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.notifications import queue_notification
from ticket_backend.submissions import require_key, save_submission, saved_submission
from ticket_backend.validation import optional_author, optional_email, require_id, require_page, require_text


STATUSES = ("open", "in_progress", "closed")


class TicketNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class StaleTicketError(Exception):
    """A status change named a revision that is no longer the ticket's latest history entry."""

    def __init__(self, revision: int):
        self.revision = revision
        super().__init__(f"The ticket has changed since that revision; its current revision is {revision}.")


def _require_revisions(revisions: tuple[int, ...] | None) -> tuple[int, ...] | None:
    """Reject anything other than None or one or more revision ids small enough for SQLite."""
    if revisions is None:
        return None
    if type(revisions) is not tuple or not revisions:
        raise ValueError("Revisions must be one or more non-negative integers.")
    for revision in revisions:
        # bool is an int subclass, and a boolean is not a revision a client could have read.
        if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
            raise ValueError("Revision must be a non-negative integer.")
    return revisions


def create_ticket(
    title: str,
    message: str,
    database: Path = DEFAULT_DATABASE,
    *,
    idempotency_key: str | None = None,
    author: str | None = None,
    email: str | None = None,
) -> dict:
    """Create a ticket with its initial history entry, or replay the saved response for a reused key.

    Keys belong to the author, so each token, and the terminal (no author), has its own.
    Raises IdempotencyConflictError when this author already used the key for different content.
    """
    title = require_text(title, "Title", 120)
    message = require_text(message, "Message", 10_000)
    author = optional_author(author)
    email = optional_email(email)
    idempotency_key = require_key(idempotency_key)
    with closing(connect(database)) as connection:
        with connection:
            if idempotency_key is not None:
                connection.execute("BEGIN IMMEDIATE")
                ticket = saved_submission(connection, idempotency_key, author)
                if ticket is not None:
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
            queue_notification(connection, row, event.lastrowid, "received",
                               f"We received your ticket #{row['id']}. Its status is open.\n\n{message}")
            ticket = dict(row)
            if idempotency_key is not None:
                save_submission(connection, idempotency_key, author, ticket)
        return ticket


def ticket_state(ticket_id: int, database: Path = DEFAULT_DATABASE) -> tuple[dict, int]:
    """Return a ticket and its revision, read in one transaction.

    The revision is the id of the latest history entry, or 0 when a ticket has none. Comments do not move it,
    and a status change does, because that change inserts a history entry.
    """
    ticket_id = require_id(ticket_id)
    with closing(connect(database)) as connection:
        with connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
            ticket = dict(row)
            revision = connection.execute(
                "SELECT MAX(id) AS revision FROM ticket_events WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()["revision"]
    return ticket, int(revision or 0)


def get_ticket(ticket_id: int, database: Path = DEFAULT_DATABASE) -> dict:
    """Return one ticket, raising TicketNotFoundError when it does not exist."""
    ticket, _revision = ticket_state(ticket_id, database)
    return ticket


def ticket_revision(ticket_id: int, database: Path = DEFAULT_DATABASE) -> int:
    """Return the id of a ticket's latest history entry, or 0 when it has none."""
    _ticket, revision = ticket_state(ticket_id, database)
    return revision


def list_tickets(
    database: Path = DEFAULT_DATABASE,
    limit: int = 20,
    *,
    offset: int = 0,
    status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Return tickets newest first, optionally filtered by status and literal text in the title or message."""
    require_page(limit, offset)
    if status is not None and status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    clauses = []
    parameters = []
    if status is not None:
        clauses.append("status = ?")
        parameters.append(status)
    if search is not None:
        search = require_text(search, "Search", 200)
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
    revisions: tuple[int, ...] | None = None,
) -> dict:
    """Change a ticket's status, recording a history entry and queuing a notification in one transaction.

    Setting the current status again returns the ticket unchanged and records nothing. When revisions is given,
    the change applies only if the ticket's latest history entry is one of them; otherwise StaleTicketError
    is raised and nothing is written. None keeps the previous behavior and overwrites unconditionally.
    """
    ticket_id = require_id(ticket_id)
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    if not isinstance(note, str) or len(note.strip()) > 500:
        raise ValueError("Note must be text of at most 500 characters.")
    author = optional_author(author)
    revisions = _require_revisions(revisions)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
            current = connection.execute(
                "SELECT MAX(id) AS revision FROM ticket_events WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()["revision"]
            # Check before the unchanged-status shortcut, so a client that read an older entry is rejected
            # even when the status now happens to be the one it asked for.
            if revisions is not None and int(current or 0) not in revisions:
                raise StaleTicketError(int(current or 0))
            if row["status"] == status:
                return dict(row)
            connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
            event = connection.execute(
                """INSERT INTO ticket_events (ticket_id, from_status, to_status, note, author)
                   VALUES (?, ?, ?, ?, ?)""",
                (ticket_id, row["status"], status, note.strip(), author),
            )
            body = f"Ticket #{ticket_id} changed from {row['status']} to {status}."
            queue_notification(connection, row, event.lastrowid, f"is now {status}",
                               body + (f"\n\n{note.strip()}" if note.strip() else ""))
            return dict(connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone())


def ticket_history(ticket_id: int, database: Path = DEFAULT_DATABASE) -> list[dict]:
    """Return a ticket's status changes, oldest first."""
    ticket_id = require_id(ticket_id)
    with closing(connect(database)) as connection:
        if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
            raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            )
        ]


def statistics(database: Path = DEFAULT_DATABASE) -> dict:
    """Count tickets by status, including statuses with no tickets."""
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
    """Add a comment to a ticket in any status, without changing its status or history."""
    ticket_id = require_id(ticket_id)
    body = require_text(body, "Comment", 5_000)
    author = optional_author(author)
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
    """Return a ticket's comments, oldest first."""
    ticket_id = require_id(ticket_id)
    with closing(connect(database)) as connection:
        if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
            raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            )
        ]
