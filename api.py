import logging
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Path as PathParameter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from db import DEFAULT_DATABASE
from functions import (
    IdempotencyConflictError, TicketNotFoundError, add_comment, create_ticket, get_ticket,
    list_comments, list_tickets, set_status, statistics, ticket_history,
)


Status = Literal["open", "in_progress", "closed"]
TicketId = Annotated[int, PathParameter(gt=0)]
logger = logging.getLogger(__name__)


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120, strict=True)
    message: str = Field(min_length=1, max_length=10_000, strict=True)


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Status
    note: str = Field(default="", max_length=500, strict=True)


class TicketOutput(BaseModel):
    id: int
    title: str
    message: str
    status: Status
    created_at: str


class EventOutput(BaseModel):
    id: int
    ticket_id: int
    from_status: Status | None
    to_status: Status
    note: str
    created_at: str


class CommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body: str = Field(min_length=1, max_length=5_000, strict=True)


class CommentOutput(BaseModel):
    id: int
    ticket_id: int
    body: str
    created_at: str


class StatsOutput(BaseModel):
    total: int
    by_status: dict[str, int]


def create_app(database: Path | None = None) -> FastAPI:
    database = Path(database if database is not None else os.environ.get("TICKETS_DATABASE", DEFAULT_DATABASE))
    app = FastAPI(title="Ticket Backend", version="0.4.0")

    @app.exception_handler(TicketNotFoundError)
    async def missing_ticket(_request: Request, error: TicketNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/tickets", status_code=201, response_model=TicketOutput,
              responses={409: {"description": "Submission key already used for different ticket content"}})
    def submit(
        payload: TicketInput,
        response: Response,
        idempotency_key: Annotated[str | None, Header(
            description="Reuse for retries of one submission. 1-128 ASCII letters, digits, dots, underscores, or hyphens."
        )] = None,
    ):
        ticket = create_ticket(payload.title, payload.message, database, idempotency_key=idempotency_key)
        response.headers["Location"] = f"/tickets/{ticket['id']}"
        return ticket

    @app.get("/tickets", response_model=list[TicketOutput])
    def listing(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Status | None = None,
        search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ):
        return list_tickets(database, limit, offset=offset, status=status, search=search)

    @app.get("/tickets/{ticket_id}", response_model=TicketOutput)
    def show(ticket_id: TicketId):
        return get_ticket(ticket_id, database)

    @app.patch("/tickets/{ticket_id}/status", response_model=TicketOutput)
    def change_status(ticket_id: TicketId, payload: StatusInput):
        return set_status(ticket_id, payload.status, database, note=payload.note)

    @app.get("/tickets/{ticket_id}/history", response_model=list[EventOutput])
    def history(ticket_id: TicketId):
        return ticket_history(ticket_id, database)

    @app.post("/tickets/{ticket_id}/comments", status_code=201, response_model=CommentOutput)
    def comment(ticket_id: TicketId, payload: CommentInput):
        return add_comment(ticket_id, payload.body, database)

    @app.get("/tickets/{ticket_id}/comments", response_model=list[CommentOutput])
    def comments(ticket_id: TicketId):
        return list_comments(ticket_id, database)

    @app.get("/stats", response_model=StatsOutput)
    def stats():
        return statistics(database)

    return app


app = create_app()
