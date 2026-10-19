"""The notification outbox: queueing messages with history entries, delivering them, and retrying failures."""

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
import sqlite3

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.validation import require_id, require_page


NOTIFICATION_STATUSES = ("pending", "sent", "failed")


class NotificationNotFoundError(LookupError):
    pass


def queue_notification(connection: sqlite3.Connection, ticket: sqlite3.Row, event_id: int, subject: str,
                       body: str) -> None:
    """Queue a message for a ticket's requester, if it has one, inside the caller's open transaction.

    Sharing the transaction means a notification exists exactly when its history entry does.
    """
    if ticket["email"] is None:
        return
    title = " ".join(ticket["title"].split())
    connection.execute(
        "INSERT INTO ticket_notifications (ticket_id, event_id, recipient, subject, body) VALUES (?, ?, ?, ?, ?)",
        (ticket["id"], event_id, ticket["email"], f"Ticket #{ticket['id']} {subject}: {title}", body),
    )



def list_notifications(
    database: Path = DEFAULT_DATABASE,
    limit: int = 20,
    *,
    offset: int = 0,
    status: str | None = None,
) -> list[dict]:
    """Return outbox entries oldest first, optionally only those with one status."""
    require_page(limit, offset)
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
        notification_ids = sorted({require_id(identifier, "Notification ID") for identifier in notification_ids})
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
    require_page(limit, 0)
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
