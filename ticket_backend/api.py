"""The HTTP API. Run it with `uvicorn ticket_backend.api:app`; every route except /health needs a bearer token.

Each route also needs one scope on that token, and requester emails, including notification recipients, are shown
only to tokens with emails:read. Every request made with a valid token is recorded in the audit log.
"""

import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from fastapi import (
    APIRouter, Depends, FastAPI, Header, HTTPException, Path as PathParameter, Query, Request, Response,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from ticket_backend import __version__
from ticket_backend.api_audit import record_requests
from ticket_backend.audit import list_audit
from ticket_backend.database import DEFAULT_DATABASE
from ticket_backend.notifications import (
    NotificationNotFoundError, NotificationStateError, get_notification, list_notifications, retry_notifications,
)
from ticket_backend.tickets import (
    IdempotencyConflictError, TicketNotFoundError, add_comment, create_ticket, get_ticket, list_comments,
    list_tickets, set_status, statistics, ticket_history,
)
from ticket_backend.tokens import token_access


Status = Literal["open", "in_progress", "closed"]
NotificationStatus = Literal["pending", "sent", "failed"]
TicketId = Annotated[int, PathParameter(gt=0)]
NotificationId = Annotated[int, PathParameter(gt=0)]
bearer = HTTPBearer(auto_error=False, description="A token from `tickets.py token create NAME`.")
logger = logging.getLogger(__name__)


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120, strict=True)
    message: str = Field(min_length=1, max_length=10_000, strict=True)
    email: str | None = Field(default=None, max_length=254, strict=True,
                              description="Optional requester address; queues status notifications.")


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Status
    note: str = Field(default="", max_length=500, strict=True)


class TicketOutput(BaseModel):
    id: int
    title: str
    message: str
    status: Status
    email: Annotated[str | None, Field(description="The requester address, or null when there is none or the "
                                                   "token does not have the emails:read scope.")]
    created_at: str


class EventOutput(BaseModel):
    id: int
    ticket_id: int
    from_status: Status | None
    to_status: Status
    note: str
    author: str | None
    created_at: str


class CommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body: str = Field(min_length=1, max_length=5_000, strict=True)


class CommentOutput(BaseModel):
    id: int
    ticket_id: int
    body: str
    author: str | None
    created_at: str


class StatsOutput(BaseModel):
    total: int
    by_status: dict[str, int]


class NotificationOutput(BaseModel):
    id: int
    ticket_id: int
    event_id: int
    recipient: Annotated[str | None, Field(description="The address, or null when the token does not have the "
                                                       "emails:read scope.")]
    subject: str
    body: str
    status: NotificationStatus
    attempts: int
    last_error: str | None
    next_attempt_at: str
    sent_at: str | None
    created_at: str


class AuditOutput(BaseModel):
    id: int
    token: Annotated[str, Field(description="Name of the token that made the request.")]
    method: str
    path: Annotated[str, Field(description="Requested path, without the query string.")]
    route: Annotated[str | None, Field(description="The matched route, such as /tickets/{ticket_id}.")]
    ticket_id: Annotated[int | None, Field(description="The ticket named in the path, or created by the request.")]
    status: Annotated[int, Field(description="HTTP status code of the response.")]
    client: Annotated[str | None, Field(description="Address of the client, or of the proxy in front of it.")]
    created_at: str


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[int, Field(gt=0, strict=True)]] | None = Field(
        default=None, min_length=1, max_length=100,
        description="Failed notifications to requeue; omit to requeue every failed notification.",
    )


def create_app(database: Path | None = None) -> FastAPI:
    """Build the API for one database, defaulting to TICKETS_DATABASE or the project's tickets.db."""
    database = Path(database if database is not None else os.environ.get("TICKETS_DATABASE", DEFAULT_DATABASE))
    app = FastAPI(title="Ticket Backend", version=__version__)

    @app.exception_handler(TicketNotFoundError)
    async def missing_ticket(_request: Request, error: TicketNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(NotificationNotFoundError)
    async def missing_notification(_request: Request, error: NotificationNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(NotificationStateError)
    async def unretryable_notification(_request: Request, error: NotificationStateError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(IdempotencyConflictError)
    async def conflicting_submission(_request: Request, error: IdempotencyConflictError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, error: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    async def unavailable_storage(_request: Request, error: Exception):
        logger.error("Ticket storage failed: %s", error)
        return JSONResponse(status_code=503, content={"detail": "Ticket storage is unavailable."})

    app.add_exception_handler(sqlite3.Error, unavailable_storage)
    app.add_exception_handler(OSError, unavailable_storage)

    record_requests(app, database)

    def caller(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> dict:
        access = token_access(credentials.credentials if credentials else None, database)
        if access is None:
            raise HTTPException(status_code=401, detail="A valid API token is required.",
                                headers={"WWW-Authenticate": "Bearer"})
        request.state.audit_token = access["name"]
        return access

    def requires(scope: str):
        def check(access: Annotated[dict, Depends(caller)]) -> dict:
            if scope not in access["scopes"]:
                # RFC 6750 names the missing scope so a client can tell this apart from a bad token.
                raise HTTPException(status_code=403, detail=f"This token does not have the {scope} scope.",
                                    headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'})
            return access
        return check

    reader, writer = requires("tickets:read"), requires("tickets:write")
    outbox_reader, outbox_writer = requires("notifications:read"), requires("notifications:write")
    auditor = requires("audit:read")

    def shown(ticket: dict, access: dict) -> dict:
        return ticket if "emails:read" in access["scopes"] else {**ticket, "email": None}

    def shown_notification(notification: dict, access: dict) -> dict:
        if "emails:read" in access["scopes"]:
            return notification
        # SMTP errors usually quote the refused address, so remove it from the error as well.
        error = notification["last_error"]
        if error is not None:
            error = re.sub(re.escape(notification["recipient"]), "[hidden]", error, flags=re.IGNORECASE)
        return {**notification, "recipient": None, "last_error": error}

    # Every route except /health requires a token; FastAPI reuses the result within a request.
    protected = APIRouter(dependencies=[Depends(caller)],
                          responses={401: {"description": "Missing, unknown, revoked, or expired API token"},
                                     403: {"description": "The token does not have the scope this route needs"}})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @protected.post("/tickets", status_code=201, response_model=TicketOutput,
                    responses={409: {"description": "This token already used the key for different ticket content"}})
    def submit(
        payload: TicketInput,
        request: Request,
        response: Response,
        access: Annotated[dict, Depends(writer)],
        idempotency_key: Annotated[str | None, Header(
            description="Reuse for retries of one submission; each token has its own keys. "
                        "1-128 ASCII letters, digits, dots, underscores, or hyphens."
        )] = None,
    ):
        ticket = create_ticket(payload.title, payload.message, database,
                               idempotency_key=idempotency_key, author=access["name"], email=payload.email)
        response.headers["Location"] = f"/tickets/{ticket['id']}"
        request.state.audit_ticket_id = ticket["id"]
        return shown(ticket, access)

    @protected.get("/tickets", response_model=list[TicketOutput])
    def listing(
        access: Annotated[dict, Depends(reader)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Status | None = None,
        search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ):
        return [shown(ticket, access)
                for ticket in list_tickets(database, limit, offset=offset, status=status, search=search)]

    @protected.get("/tickets/{ticket_id}", response_model=TicketOutput)
    def show(ticket_id: TicketId, access: Annotated[dict, Depends(reader)]):
        return shown(get_ticket(ticket_id, database), access)

    @protected.patch("/tickets/{ticket_id}/status", response_model=TicketOutput)
    def change_status(ticket_id: TicketId, payload: StatusInput, access: Annotated[dict, Depends(writer)]):
        return shown(set_status(ticket_id, payload.status, database, note=payload.note, author=access["name"]), access)

    @protected.get("/tickets/{ticket_id}/history", response_model=list[EventOutput], dependencies=[Depends(reader)])
    def history(ticket_id: TicketId):
        return ticket_history(ticket_id, database)

    @protected.post("/tickets/{ticket_id}/comments", status_code=201, response_model=CommentOutput)
    def comment(ticket_id: TicketId, payload: CommentInput, access: Annotated[dict, Depends(writer)]):
        return add_comment(ticket_id, payload.body, database, author=access["name"])

    @protected.get("/tickets/{ticket_id}/comments", response_model=list[CommentOutput], dependencies=[Depends(reader)])
    def comments(ticket_id: TicketId):
        return list_comments(ticket_id, database)

    @protected.get("/stats", response_model=StatsOutput, dependencies=[Depends(reader)])
    def stats():
        return statistics(database)

    @protected.get("/notifications", response_model=list[NotificationOutput])
    def notifications(
        access: Annotated[dict, Depends(outbox_reader)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: NotificationStatus | None = None,
        ticket_id: Annotated[int | None, Query(gt=0)] = None,
    ):
        rows = list_notifications(database, limit, offset=offset, status=status, ticket_id=ticket_id)
        return [shown_notification(row, access) for row in rows]

    @protected.get("/notifications/{notification_id}", response_model=NotificationOutput,
                   responses={404: {"description": "No notification has this ID"}})
    def notification(notification_id: NotificationId, access: Annotated[dict, Depends(outbox_reader)]):
        return shown_notification(get_notification(notification_id, database), access)

    @protected.post("/notifications/retry", response_model=list[NotificationOutput],
                    responses={404: {"description": "A named notification does not exist"},
                               409: {"description": "A named notification is not failed"}})
    def retry(access: Annotated[dict, Depends(outbox_writer)], payload: RetryInput | None = None):
        ids = payload.ids if payload is not None else None
        return [shown_notification(row, access) for row in retry_notifications(database, ids)]

    @protected.get("/audit", response_model=list[AuditOutput], dependencies=[Depends(auditor)])
    def audit_log(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        token: Annotated[str | None, Query(max_length=64, description="Only requests made by this token")] = None,
        ticket_id: Annotated[int | None, Query(gt=0, description="Only requests about this ticket")] = None,
    ):
        return list_audit(database, limit, offset=offset, token=token, ticket_id=ticket_id)

    app.include_router(protected)
    return app


app = create_app()
