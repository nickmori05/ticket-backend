"""Suppressed addresses: recipients no notification is sent to until the suppression is removed.

An address is suppressed when a mail server refuses it permanently, or by hand from the terminal. Addresses are
compared without regard to case, as almost every mail server treats them.
"""

from contextlib import closing
from pathlib import Path
import sqlite3

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.validation import optional_email, require_id, require_page


SUPPRESSION_REASONS = ("refused", "manual")


class SuppressionNotFoundError(LookupError):
    pass


def find_suppression(connection: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    """Return the suppression covering an address, or None, using the caller's connection."""
    return connection.execute(
        "SELECT * FROM notification_suppressions WHERE address = ? COLLATE NOCASE", (address,)
    ).fetchone()


def record_refusal(connection: sqlite3.Connection, notification: dict, detail: str) -> None:
    """Suppress a notification's recipient after a permanent refusal, inside the caller's open transaction.

    An address that is already suppressed keeps its first suppression, so the original reason is not overwritten.
    """
    connection.execute(
        """INSERT INTO notification_suppressions (address, reason, detail, notification_id)
           SELECT ?1, 'refused', ?2, ?3
           WHERE NOT EXISTS (SELECT 1 FROM notification_suppressions WHERE address = ?1)""",
        (notification["recipient"], detail[:500], notification["id"]),
    )


def list_suppressions(database: Path = DEFAULT_DATABASE, limit: int = 20, *, offset: int = 0) -> list[dict]:
    """Return suppressed addresses, newest first."""
    require_page(limit, offset)
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM notification_suppressions ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            )
        ]


def suppress_address(address: str, database: Path = DEFAULT_DATABASE) -> dict:
    """Suppress an address by hand and return its suppression; an existing suppression is returned unchanged."""
    address = _require_address(address)
    with closing(connect(database)) as connection, connection:
        connection.execute(
            """INSERT INTO notification_suppressions (address, reason)
               SELECT ?1, 'manual' WHERE NOT EXISTS (SELECT 1 FROM notification_suppressions WHERE address = ?1)""",
            (address,),
        )
        return dict(find_suppression(connection, address))


def remove_suppression(
    database: Path = DEFAULT_DATABASE, *, address: str | None = None, suppression_id: int | None = None,
) -> dict:
    """Delete one suppression, named by its address or its ID, and return it as it was.

    Notifications that already failed stay failed; requeue them with retry_notifications().
    """
    if (address is None) == (suppression_id is None):
        raise ValueError("Name a suppression by its address or by its ID.")
    if address is not None:
        address = _require_address(address)
    else:
        suppression_id = require_id(suppression_id, "Suppression ID")
    with closing(connect(database)) as connection, connection:
        if address is not None:
            row = find_suppression(connection, address)
        else:
            row = connection.execute(
                "SELECT * FROM notification_suppressions WHERE id = ?", (suppression_id,)
            ).fetchone()
        if row is None:
            raise SuppressionNotFoundError(f"No suppression was found for {address or f'#{suppression_id}'}.")
        connection.execute("DELETE FROM notification_suppressions WHERE id = ?", (row["id"],))
    return dict(row)


def _require_address(address: str) -> str:
    if address is None:
        raise ValueError("Email must not be blank.")
    return optional_email(address)
