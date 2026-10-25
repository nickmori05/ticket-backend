"""API tokens. Only SHA-256 hashes are stored; the token itself is shown once, when it is created.

Each token carries a fixed set of scopes, an optional expiry, and an optional rate limit, all chosen at creation.
Rotating a token replaces its secret but keeps its name, so everything recorded under the name stays with it. The
previous secret can keep working for a short grace period, so clients can switch over without a failed request.
"""

from collections.abc import Iterable
from contextlib import closing
import hashlib
from pathlib import Path
import secrets
import sqlite3

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.ratelimit import require_rate_limit
from ticket_backend.validation import require_token_name


SCOPES = (
    "tickets:read", "tickets:write", "emails:read", "notifications:read", "notifications:write", "audit:read",
)
# Tokens created before scopes existed have no stored scopes and keep the full access they always had.
LEGACY_SCOPES = ("tickets:read", "tickets:write", "emails:read")
MAX_EXPIRY_DAYS = 3650
MAX_GRACE_MINUTES = 7 * 24 * 60
_COLUMNS = """id, name, scopes, rate_limit, created_at, expires_at, revoked_at, rotated_at,
    CASE WHEN previous_expires_at > CURRENT_TIMESTAMP THEN previous_expires_at END AS previous_valid_until,
    CASE WHEN revoked_at IS NOT NULL THEN 'revoked'
         WHEN expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP THEN 'expired'
         ELSE 'active' END AS state"""


class TokenNotFoundError(LookupError):
    pass


def _token_scopes(scopes: Iterable[str] | None) -> tuple[str, ...]:
    if scopes is None:
        return SCOPES
    if isinstance(scopes, str) or not isinstance(scopes, Iterable):
        raise ValueError("Scopes must be a list of scope names.")
    try:
        scopes = set(scopes)
    except TypeError:
        raise ValueError("Scopes must be a list of scope names.") from None
    unknown = sorted(str(scope) for scope in scopes if scope not in SCOPES)
    if unknown or not scopes:
        raise ValueError(f"Scopes must be one or more of: {', '.join(SCOPES)}.")
    return tuple(scope for scope in SCOPES if scope in scopes)


def _stored_scopes(value: str | None) -> list[str]:
    return list(LEGACY_SCOPES) if value is None else value.split()


def _token_hash(token: str) -> str:
    # Tokens are long random values, so a plain SHA-256 digest is enough to avoid storing them.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_row(row: sqlite3.Row) -> dict:
    return {**dict(row), "scopes": _stored_scopes(row["scopes"])}


def _require_expiry(expires_in_days: int | None) -> None:
    if expires_in_days is not None and (
        type(expires_in_days) is not int or not 1 <= expires_in_days <= MAX_EXPIRY_DAYS
    ):
        raise ValueError(f"Expiry must be a whole number of days from 1 to {MAX_EXPIRY_DAYS}.")


def _new_token() -> str:
    return "tkt_" + secrets.token_urlsafe(32)


def create_token(
    name: str,
    database: Path = DEFAULT_DATABASE,
    *,
    scopes: Iterable[str] | None = None,
    expires_in_days: int | None = None,
    rate_limit: int | None = None,
) -> dict:
    """Create a named token and return it with its plain value, which is not stored and cannot be shown again.

    Without scopes the token gets every scope. Without an expiry it lasts until it is revoked. Without a rate limit
    it uses the server's default; a limit of 0 means none.
    """
    name = require_token_name(name)
    scopes = _token_scopes(scopes)
    _require_expiry(expires_in_days)
    if rate_limit is not None:
        require_rate_limit(rate_limit)
    token = _new_token()
    with closing(connect(database)) as connection:
        with connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO api_tokens (name, token_hash, scopes, rate_limit, expires_at)
                       VALUES (?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END)""",
                    (name, _token_hash(token), " ".join(scopes), rate_limit, expires_in_days,
                     f"+{expires_in_days} days"),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"A token named {name} already exists.") from None
            row = connection.execute(f"SELECT {_COLUMNS} FROM api_tokens WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {**_token_row(row), "token": token}


def list_tokens(database: Path = DEFAULT_DATABASE) -> list[dict]:
    """Return every token's name, scopes, rate limit, state, and creation, expiry, and revocation times."""
    with closing(connect(database)) as connection:
        return [_token_row(row) for row in connection.execute(f"SELECT {_COLUMNS} FROM api_tokens ORDER BY id")]


def revoke_token(name: str, database: Path = DEFAULT_DATABASE) -> dict:
    """Stop a token from authenticating. Revoking it again keeps the original revocation time."""
    name = require_token_name(name)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) WHERE name = ?",
                (name,),
            )
            row = connection.execute(f"SELECT {_COLUMNS} FROM api_tokens WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise TokenNotFoundError(f"Token {name} was not found.")
    return _token_row(row)


def rotate_token(
    name: str,
    database: Path = DEFAULT_DATABASE,
    *,
    expires_in_days: int | None = None,
    grace_minutes: int = 0,
) -> dict:
    """Give a token a new secret and return it with its plain value, which is not stored and cannot be shown again.

    The old secret stops working at once, or after grace_minutes while the token stays active. Only the secret
    replaced by this rotation gets a grace period, so rotating again retires any older one at once. The name, scopes,
    and rate limit stay, so the token keeps its idempotency keys, audit history, and authorship. The expiry stays too
    unless expires_in_days sets a new one from now, which is the only way to rotate a token that has already expired.
    Revoked tokens cannot be rotated, and revoking a token refuses its previous secret too.
    """
    name = require_token_name(name)
    _require_expiry(expires_in_days)
    if type(grace_minutes) is not int or not 0 <= grace_minutes <= MAX_GRACE_MINUTES:
        raise ValueError(f"Grace period must be a whole number of minutes from 0 to {MAX_GRACE_MINUTES}.")
    token = _new_token()
    with closing(connect(database)) as connection:
        with connection:
            # Take the write lock before reading the state, so a concurrent revocation cannot be undone by this.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT {_COLUMNS} FROM api_tokens WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise TokenNotFoundError(f"Token {name} was not found.")
            if row["state"] == "revoked":
                raise ValueError(f"Token {name} is revoked and cannot be rotated; create a new token instead.")
            if row["state"] == "expired" and expires_in_days is None:
                raise ValueError(f"Token {name} has expired; rotate it with a new expiry to use it again.")
            # An expired token's secret was already refused, so renewing the token must not bring it back.
            grace = grace_minutes if row["state"] == "active" else 0
            connection.execute(
                """UPDATE api_tokens SET
                       previous_token_hash = CASE WHEN ? > 0 THEN token_hash END,
                       previous_expires_at = CASE WHEN ? > 0 THEN datetime('now', ?) END,
                       token_hash = ?, rotated_at = CURRENT_TIMESTAMP,
                       expires_at = CASE WHEN ? IS NULL THEN expires_at ELSE datetime('now', ?) END
                   WHERE id = ?""",
                (grace, grace, f"+{grace} minutes", _token_hash(token), expires_in_days,
                 f"+{expires_in_days} days", row["id"]),
            )
            row = connection.execute(f"SELECT {_COLUMNS} FROM api_tokens WHERE id = ?", (row["id"],)).fetchone()
    return {**_token_row(row), "token": token}


def token_access(token: str | None, database: Path = DEFAULT_DATABASE) -> dict | None:
    """Return the name, scopes, and rate limit of an active token, or None when it is missing, unknown, revoked, or
    expired. A rotated token's previous secret is accepted until its grace period ends. A rate limit of None means
    the server's default."""
    if not isinstance(token, str) or not 1 <= len(token) <= 200:
        return None
    digest = _token_hash(token)
    with closing(connect(database)) as connection:
        row = connection.execute(
            """SELECT name, scopes, rate_limit FROM api_tokens
               WHERE (token_hash = ? OR (previous_token_hash = ? AND previous_expires_at > CURRENT_TIMESTAMP))
               AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
            (digest, digest),
        ).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "scopes": _stored_scopes(row["scopes"]), "rate_limit": row["rate_limit"]}


def authenticate(token: str | None, database: Path = DEFAULT_DATABASE) -> str | None:
    """Return the name of an active token, or None when it is missing, unknown, revoked, or expired."""
    access = token_access(token, database)
    return access["name"] if access is not None else None
