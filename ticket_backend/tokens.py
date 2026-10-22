"""API tokens. Only SHA-256 hashes are stored; the token itself is shown once, when it is created.

Each token carries a fixed set of scopes, an optional expiry, and an optional rate limit, all chosen at creation.
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
_COLUMNS = """id, name, scopes, rate_limit, created_at, expires_at, revoked_at,
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
    if expires_in_days is not None and (
        type(expires_in_days) is not int or not 1 <= expires_in_days <= MAX_EXPIRY_DAYS
    ):
        raise ValueError(f"Expiry must be a whole number of days from 1 to {MAX_EXPIRY_DAYS}.")
    if rate_limit is not None:
        require_rate_limit(rate_limit)
    token = "tkt_" + secrets.token_urlsafe(32)
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


def token_access(token: str | None, database: Path = DEFAULT_DATABASE) -> dict | None:
    """Return the name, scopes, and rate limit of an active token, or None when it is missing, unknown, revoked, or
    expired. A rate limit of None means the server's default."""
    if not isinstance(token, str) or not 1 <= len(token) <= 200:
        return None
    with closing(connect(database)) as connection:
        row = connection.execute(
            """SELECT name, scopes, rate_limit FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
            (_token_hash(token),),
        ).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "scopes": _stored_scopes(row["scopes"]), "rate_limit": row["rate_limit"]}


def authenticate(token: str | None, database: Path = DEFAULT_DATABASE) -> str | None:
    """Return the name of an active token, or None when it is missing, unknown, revoked, or expired."""
    access = token_access(token, database)
    return access["name"] if access is not None else None
