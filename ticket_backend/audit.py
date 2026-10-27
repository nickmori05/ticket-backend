"""The audit log: one row for each API request made with a valid token, saying which token did what and when.

Rows are written by the API after each response is decided and are never changed, only pruned by age.
"""

from contextlib import closing
from pathlib import Path

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.validation import require_age_days, require_id, require_page, require_token_name


def record_request(
    database: Path,
    token: str,
    method: str,
    path: str,
    route: str | None,
    status: int,
    *,
    ticket_id: int | None = None,
    client: str | None = None,
) -> None:
    """Save one request. Paths are stored without their query string, which can hold search text."""
    with closing(connect(database)) as connection, connection:
        connection.execute(
            """INSERT INTO api_audit_log (token, method, path, route, ticket_id, status, client)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (token, method, path[:500], route, ticket_id, status, client[:100] if client else None),
        )


def list_audit(
    database: Path = DEFAULT_DATABASE,
    limit: int = 20,
    *,
    offset: int = 0,
    token: str | None = None,
    ticket_id: int | None = None,
) -> list[dict]:
    """Return recorded requests newest first, optionally only one token's or only those about one ticket."""
    require_page(limit, offset)
    clauses, parameters = [], []
    if token is not None:
        clauses.append("token = ?")
        parameters.append(require_token_name(token))
    if ticket_id is not None:
        clauses.append("ticket_id = ?")
        parameters.append(require_id(ticket_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM api_audit_log" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
        ]


def prune_audit(older_than_days: int, database: Path = DEFAULT_DATABASE) -> dict:
    """Delete recorded requests older than a number of days and return how many were deleted."""
    age = require_age_days(older_than_days)
    with closing(connect(database)) as connection, connection:
        deleted = connection.execute("DELETE FROM api_audit_log WHERE created_at < datetime('now', ?)", (age,)).rowcount
    return {"deleted": deleted}
