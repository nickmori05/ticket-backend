"""API tokens. Only SHA-256 hashes are stored; the token itself is shown once, when it is created."""

from contextlib import closing
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3

from ticket_backend.database import DEFAULT_DATABASE, connect


class TokenNotFoundError(LookupError):
    pass


def _token_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("Token name must be 1-64 ASCII letters, digits, dots, underscores, or hyphens.")
    return name


def _token_hash(token: str) -> str:
    # Tokens are long random values, so a plain SHA-256 digest is enough to avoid storing them.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(name: str, database: Path = DEFAULT_DATABASE) -> dict:
    """Create a named token and return it with its plain value, which is not stored and cannot be shown again."""
    name = _token_name(name)
    token = "tkt_" + secrets.token_urlsafe(32)
    with closing(connect(database)) as connection:
        with connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO api_tokens (name, token_hash) VALUES (?, ?)", (name, _token_hash(token))
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"A token named {name} already exists.") from None
            row = connection.execute(
                "SELECT id, name, created_at, revoked_at FROM api_tokens WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    return {**dict(row), "token": token}


def list_tokens(database: Path = DEFAULT_DATABASE) -> list[dict]:
    """Return every token's name, creation time, and revocation time, without hashes."""
    with closing(connect(database)) as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT id, name, created_at, revoked_at FROM api_tokens ORDER BY id"
            )
        ]


def revoke_token(name: str, database: Path = DEFAULT_DATABASE) -> dict:
    """Stop a token from authenticating. Revoking it again keeps the original revocation time."""
    name = _token_name(name)
    with closing(connect(database)) as connection:
        with connection:
            connection.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) WHERE name = ?",
                (name,),
            )
            row = connection.execute(
                "SELECT id, name, created_at, revoked_at FROM api_tokens WHERE name = ?", (name,)
            ).fetchone()
    if row is None:
        raise TokenNotFoundError(f"Token {name} was not found.")
    return dict(row)


def authenticate(token: str | None, database: Path = DEFAULT_DATABASE) -> str | None:
    """Return the name of an active token, or None when the token is missing, unknown, or revoked."""
    if not isinstance(token, str) or not 1 <= len(token) <= 200:
        return None
    with closing(connect(database)) as connection:
        row = connection.execute(
            "SELECT name FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL", (_token_hash(token),)
        ).fetchone()
    return row["name"] if row is not None else None
