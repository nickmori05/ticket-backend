"""Ticket labels: short names, such as billing or priority:high, for grouping and finding tickets.

Labels are normalized to lowercase, and adding or removing them is idempotent, so a retried request changes nothing.
They do not change a ticket's status or history and queue no notifications.
"""

from contextlib import closing
from pathlib import Path
import re
import sqlite3

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.validation import require_id


MAX_LABELS_PER_TICKET = 20
MAX_LABEL_FILTERS = 5


def require_label(label: str) -> str:
    """Return a label trimmed and lowercased, or raise ValueError when it is not a valid label."""
    label = label.strip().lower() if isinstance(label, str) else ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,31}", label):
        raise ValueError("Label must be 1-32 ASCII letters, digits, dots, colons, underscores, or hyphens, "
                         "starting with a letter or digit.")
    return label


def require_labels(labels: list[str], maximum: int = MAX_LABELS_PER_TICKET) -> list[str]:
    """Validate a list of labels, returning them normalized and without duplicates, in their original order."""
    if isinstance(labels, str) or not isinstance(labels, (list, tuple)) or not labels:
        raise ValueError("At least one label is required.")
    normalized = list(dict.fromkeys(require_label(label) for label in labels))
    if len(normalized) > maximum:
        raise ValueError(f"At most {maximum} labels can be given at once.")
    return normalized


def label_filter(labels: list[str] | None) -> tuple[str, list[str]] | None:
    """Return an SQL condition on tickets.id matching tickets that have every label, with its parameters."""
    if labels is None:
        return None
    labels = require_labels(labels, MAX_LABEL_FILTERS)
    placeholders = ", ".join("?" * len(labels))
    return (f"id IN (SELECT ticket_id FROM ticket_labels WHERE label IN ({placeholders}) "
            f"GROUP BY ticket_id HAVING COUNT(*) = {len(labels)})", labels)


def _require_ticket(connection: sqlite3.Connection, ticket_id: int) -> None:
    # Imported here because tickets.py imports this module for its list filter.
    from ticket_backend.tickets import TicketNotFoundError

    if connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
        raise TicketNotFoundError(f"Ticket #{ticket_id} was not found.")


def _labels(connection: sqlite3.Connection, ticket_id: int) -> dict:
    rows = connection.execute("SELECT label FROM ticket_labels WHERE ticket_id = ? ORDER BY label", (ticket_id,))
    return {"ticket_id": ticket_id, "labels": [row["label"] for row in rows]}


def add_labels(ticket_id: int, labels: list[str], database: Path = DEFAULT_DATABASE) -> dict:
    """Add labels to a ticket in any status, ignoring ones it already has, and return all of its labels.

    Raises ValueError, changing nothing, when the ticket would end up with more than MAX_LABELS_PER_TICKET labels.
    """
    ticket_id = require_id(ticket_id)
    labels = require_labels(labels)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_ticket(connection, ticket_id)
            connection.executemany("INSERT OR IGNORE INTO ticket_labels (ticket_id, label) VALUES (?, ?)",
                                   [(ticket_id, label) for label in labels])
            result = _labels(connection, ticket_id)
            if len(result["labels"]) > MAX_LABELS_PER_TICKET:
                raise ValueError(f"A ticket can have at most {MAX_LABELS_PER_TICKET} labels.")
            return result


def remove_labels(ticket_id: int, labels: list[str], database: Path = DEFAULT_DATABASE) -> dict:
    """Remove labels from a ticket, ignoring ones it does not have, and return the labels it keeps."""
    ticket_id = require_id(ticket_id)
    labels = require_labels(labels)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_ticket(connection, ticket_id)
            connection.executemany("DELETE FROM ticket_labels WHERE ticket_id = ? AND label = ?",
                                   [(ticket_id, label) for label in labels])
            return _labels(connection, ticket_id)


def ticket_labels(ticket_id: int, database: Path = DEFAULT_DATABASE) -> dict:
    """Return a ticket's labels in alphabetical order."""
    ticket_id = require_id(ticket_id)
    with closing(connect(database)) as connection:
        _require_ticket(connection, ticket_id)
        return _labels(connection, ticket_id)


def label_counts(database: Path = DEFAULT_DATABASE) -> list[dict]:
    """Return every label in use, alphabetically, with how many tickets have it and how many of those are not closed."""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                """SELECT label, COUNT(*) AS tickets, SUM(tickets.status != 'closed') AS unresolved
                   FROM ticket_labels JOIN tickets ON tickets.id = ticket_labels.ticket_id
                   GROUP BY label ORDER BY label"""
            )
        ]
