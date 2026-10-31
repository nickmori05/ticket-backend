"""The audit log: one row for each API request made with a valid token, saying which token did what and when.

Rows are written by the API after each response is decided and are never changed, only pruned by age.
"""

from contextlib import closing
from pathlib import Path
import re

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.validation import (
    optional_timestamp, require_age_days, require_id, require_page, require_token_name,
)


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
    status: int | str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Return recorded requests newest first, optionally narrowed to one token, one ticket, one status code or class
    of codes (such as 403 or "4xx"), and a time window from since (inclusive) to until (exclusive)."""
    require_page(limit, offset)
    clauses, parameters = _window(since, until)
    if token is not None:
        clauses.append("token = ?")
        parameters.append(require_token_name(token))
    if ticket_id is not None:
        clauses.append("ticket_id = ?")
        parameters.append(require_id(ticket_id))
    if status is not None:
        clauses.append("status BETWEEN ? AND ?")
        parameters.extend(_status_range(status))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM api_audit_log" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
        ]


def summarize_audit(
    database: Path = DEFAULT_DATABASE, *, since: str | None = None, until: str | None = None,
) -> list[dict]:
    """Count each token's recorded requests by outcome, with its first and last request, least recently used first.

    Every token that is neither revoked nor expired is included, with zero requests if it made none in the window, so
    tokens nobody uses any more stand out. Revoked and expired tokens appear only if they made requests in the window.
    """
    clauses, parameters = _window(since, until)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                """WITH requests AS (
                       SELECT token, COUNT(*) AS requests,
                              SUM(status < 400) AS succeeded,
                              SUM(status BETWEEN 400 AND 499) AS client_errors,
                              SUM(status = 403) AS forbidden,
                              SUM(status >= 500) AS server_errors,
                              MIN(created_at) AS first_request_at,
                              MAX(created_at) AS last_request_at
                       FROM api_audit_log""" + where + """ GROUP BY token
                   ), names AS (
                       SELECT name AS token FROM api_tokens WHERE revoked_at IS NULL
                           AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                       UNION SELECT token FROM requests
                   )
                   SELECT names.token, COALESCE(requests, 0) AS requests, COALESCE(succeeded, 0) AS succeeded,
                          COALESCE(client_errors, 0) AS client_errors, COALESCE(forbidden, 0) AS forbidden,
                          COALESCE(server_errors, 0) AS server_errors, first_request_at, last_request_at
                   FROM names LEFT JOIN requests USING (token)
                   ORDER BY last_request_at IS NOT NULL, last_request_at, names.token""",
                parameters,
            )
        ]


def _window(since: str | None, until: str | None) -> tuple[list[str], list]:
    """Return SQL clauses and parameters that keep requests made from since up to, but not including, until."""
    since, until = optional_timestamp(since, "Since"), optional_timestamp(until, "Until")
    if since is not None and until is not None and since >= until:
        raise ValueError("Since must be earlier than until.")
    clauses, parameters = [], []
    if since is not None:
        clauses.append("created_at >= ?")
        parameters.append(since)
    if until is not None:
        clauses.append("created_at < ?")
        parameters.append(until)
    return clauses, parameters


def _status_range(status: int | str) -> tuple[int, int]:
    """Turn a status code such as 403, or a class such as "4xx", into the lowest and highest codes it matches."""
    if type(status) is int and 100 <= status <= 599:
        return status, status
    if isinstance(status, str) and re.fullmatch(r"[1-5](\d\d|xx)", status):
        if status.endswith("xx"):
            return int(status[0]) * 100, int(status[0]) * 100 + 99
        return int(status), int(status)
    raise ValueError("Status must be an HTTP status code such as 403, or a class such as 4xx.")


def prune_audit(older_than_days: int, database: Path = DEFAULT_DATABASE) -> dict:
    """Delete recorded requests older than a number of days and return how many were deleted."""
    age = require_age_days(older_than_days)
    with closing(connect(database)) as connection, connection:
        deleted = connection.execute("DELETE FROM api_audit_log WHERE created_at < datetime('now', ?)", (age,)).rowcount
    return {"deleted": deleted}
