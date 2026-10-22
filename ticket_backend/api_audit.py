"""Records each API request made with a valid token in the audit log, once its response is decided.

The token check stores the token's name on the request, and a route that creates a ticket stores the new ID, so this
middleware learns who made the request without checking the token again.
"""

import logging
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from ticket_backend.audit import record_request


logger = logging.getLogger(__name__)


def record_requests(app: FastAPI, database: Path) -> None:
    """Add the audit middleware to an app that sets request.state.audit_token for authenticated requests."""

    @app.middleware("http")
    async def audit_requests(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            await audit(request, 500)
            raise
        await audit(request, response.status_code)
        return response

    async def audit(request: Request, status: int) -> None:
        # Only requests whose token was valid have a name to record; /health, the docs, and 401s have none.
        token = getattr(request.state, "audit_token", None)
        if token is None:
            return
        ticket_id = getattr(request.state, "audit_ticket_id", None)
        named = str(request.path_params.get("ticket_id", ""))
        if ticket_id is None and named.isascii() and named.isdecimal() and 0 < int(named) < 2**63:
            ticket_id = int(named)
        try:
            await run_in_threadpool(
                record_request, database, token, request.method, request.url.path,
                getattr(request.scope.get("route"), "path", None), status,
                ticket_id=ticket_id, client=request.client.host if request.client else None,
            )
        except (sqlite3.Error, OSError) as error:
            # The response is already decided, and a write may have committed, so report it rather than fail it.
            logger.error("Could not record %s %s by %s in the audit log: %s",
                         request.method, request.url.path, token, error)
