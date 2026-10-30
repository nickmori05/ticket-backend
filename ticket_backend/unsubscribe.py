"""Signed unsubscribe links for notification emails (RFC 8058 one-click unsubscribe).

A link names a ticket and carries an HMAC of the ticket ID and its requester's address, keyed with
TICKETS_UNSUBSCRIBE_SECRET. Nothing is stored per link: the API recomputes the signature from the ticket, so a link
works for as long as the ticket exists and the secret is unchanged, and no address appears in the URL.
"""

import base64
from collections.abc import Mapping
from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import re

from ticket_backend.database import DEFAULT_DATABASE, connect
from ticket_backend.suppressions import suppress_address


MIN_SECRET_LENGTH = 32
# A ticket ID, a dot, and an unpadded base64url SHA-256 HMAC.
TOKEN_PATTERN = re.compile(r"([1-9][0-9]{0,18})\.([A-Za-z0-9_-]{43})")


def secret_from_environment(environment: Mapping[str, str] = os.environ) -> str | None:
    """Read TICKETS_UNSUBSCRIBE_SECRET, returning None when it is unset and raising ValueError when it is too short."""
    secret = environment.get("TICKETS_UNSUBSCRIBE_SECRET", "")
    if not secret.strip():
        return None
    return require_secret(secret)


def require_secret(secret: str) -> str:
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"TICKETS_UNSUBSCRIBE_SECRET must be at least {MIN_SECRET_LENGTH} characters.")
    return secret


def _signature(secret: str, ticket_id: int, address: str) -> str:
    message = f"unsubscribe:{ticket_id}:{address.lower()}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def unsubscribe_token(secret: str, ticket_id: int, address: str) -> str:
    """Return the path segment that unsubscribes a ticket's requester."""
    return f"{ticket_id}.{_signature(secret, ticket_id, address)}"


def find_unsubscribe_ticket(token: str, secret: str, database: Path = DEFAULT_DATABASE) -> dict | None:
    """Return the ticket a valid token names, or None for a malformed, forged, or outdated token."""
    match = TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) else None
    if match is None:
        return None
    ticket_id = int(match[1])
    if ticket_id >= 2**63:
        return None
    with closing(connect(database)) as connection:
        row = connection.execute("SELECT id, email FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None or row["email"] is None:
        return None
    if not hmac.compare_digest(match[2], _signature(secret, ticket_id, row["email"])):
        return None
    return dict(row)


def unsubscribe(token: str, secret: str, database: Path = DEFAULT_DATABASE) -> dict | None:
    """Suppress the address a valid token names and return the suppression, or None when the token is not valid.

    Unsubscribing twice, or an address that is already suppressed, returns the existing suppression unchanged.
    """
    ticket = find_unsubscribe_ticket(token, secret, database)
    if ticket is None:
        return None
    return suppress_address(ticket["email"], database, reason="unsubscribed",
                            detail=f"Unsubscribed from a link in a notification about ticket #{ticket['id']}.")

