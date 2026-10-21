# Ticket Backend

[![Tests](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml)

A support-ticket tracker built on Python and SQLite, with a command line and an
HTTP API that share one database. Tickets move between `open`, `in_progress`, and
`closed` with a recorded history, collect comments, and can notify the person who
filed them by email.

- **CLI** using only the standard library: create, search, and update tickets, with
  text or JSON output and stable exit codes.
- **HTTP API** built with FastAPI, protected by bearer tokens that are stored hashed,
  limited to scopes, optionally expiring, and managed from the terminal.
- **Safe retries**: an optional idempotency key makes a repeated submission return the
  original ticket instead of creating a duplicate.
- **Email notifications** through a transactional outbox, delivered over SMTP with
  retries and backoff, and inspectable and retryable over HTTP.
- **Docker Compose** setup that runs the API and CLI against one persistent volume.

Requires Python 3.10 or later.

## Quick start

The CLI needs no installation. The database (`tickets.db` in the project folder)
is created on first use.

```sh
git clone https://github.com/nickmori05/ticket-backend.git
cd ticket-backend
python3 tickets.py create --title "Login issue" --message "Cannot sign in"
python3 tickets.py status 1 in_progress --note "Investigating"
python3 tickets.py history 1
```

To run the API, install its dependencies and create a token:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python tickets.py token create alice
python -m uvicorn ticket_backend.api:app --host 127.0.0.1 --port 8000
```

In a second terminal, use the printed token, which is shown only once:

```sh
export TICKETS_TOKEN=tkt_...   # the token printed above
curl -H "Authorization: Bearer $TICKETS_TOKEN" http://127.0.0.1:8000/tickets/1
```

Interactive API docs are at <http://127.0.0.1:8000/docs>; use **Authorize** to
enter the token.

## Command line

Run `python3 tickets.py COMMAND --help` for every option.

| Command | Purpose |
| --- | --- |
| `create [--title T] [--message M] [--email E] [--idempotency-key K]` | Submit a ticket; prompts for a missing title or message |
| `show ID` | Show one ticket |
| `list [--status S] [--search TEXT] [--limit N] [--offset N]` | List tickets, newest first |
| `status ID STATUS [--note TEXT]` | Change status to `open`, `in_progress`, or `closed` |
| `history ID` | Show recorded status changes |
| `comment ID BODY` / `comments ID` | Add a comment / list comments, oldest first |
| `stats` | Count tickets by status |
| `outbox [--status S] [--ticket ID] [--limit N] [--offset N]` | List queued notifications, oldest first |
| `send-notifications [--limit N] [--max-attempts N] [--retry-delay S]` | Send due notifications over SMTP |
| `retry-notifications [ID ...]` | Requeue failed notifications |
| `token create NAME [--scope S ...] [--expires-in DAYS]` | Create an API token, shown once |
| `token list` / `token revoke NAME` | List tokens with their scopes and state / revoke one |

Global options go before the command: `--database PATH` selects another SQLite
file, and `--json` prints machine-readable output.

```sh
python3 tickets.py --database /tmp/ticket-demo.db --json create --title "Demo" --message "Example ticket"
python3 tickets.py --database /tmp/ticket-demo.db list --status open --search demo
```

Output goes to stdout and errors to stderr. JSON mode never prompts, so `create`
then needs both `--title` and `--message`.

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Ticket, token, or notification not found |
| `2` | Invalid input or configuration, or a storage error |
| `3` | Idempotency key already used for different content |
| `4` | SMTP server unreachable, or it rejected STARTTLS or the login |
| `130` | Cancelled |

Titles are up to 120 characters, messages up to 10,000, comments up to 5,000, and
status notes up to 500; surrounding whitespace is trimmed. Lists take `--limit`
from 1 to 100 (default 20) and `--offset`. Search matches literal, case-insensitive
text in the title or message, so `%` and `_` are not wildcards. Closed tickets can be
reopened, and setting the current status again records nothing.

## HTTP API

Every endpoint except `/health` and the docs requires `Authorization: Bearer TOKEN`.

| Endpoint | Scope | Purpose |
| --- | --- | --- |
| `GET /health` | none | Process health; does not check the database or need a token |
| `POST /tickets` | `tickets:write` | Create from JSON `title`, `message`, and optional `email`; returns 201 and `Location` |
| `GET /tickets` | `tickets:read` | List with optional `status`, `search`, `limit`, and `offset` |
| `GET /tickets/{id}` | `tickets:read` | Retrieve a ticket |
| `PATCH /tickets/{id}/status` | `tickets:write` | Change status from JSON `status` and optional `note` |
| `GET /tickets/{id}/history` | `tickets:read` | Recorded status changes, each with its `author` |
| `POST /tickets/{id}/comments` | `tickets:write` | Add a comment from JSON `body`; returns 201 |
| `GET /tickets/{id}/comments` | `tickets:read` | Comments, oldest first |
| `GET /stats` | `tickets:read` | Ticket counts by status |
| `GET /notifications` | `notifications:read` | Outbox, oldest first, with optional `status`, `ticket_id`, `limit`, and `offset` |
| `GET /notifications/{id}` | `notifications:read` | One notification with its attempts and last error |
| `POST /notifications/retry` | `notifications:write` | Requeue failed notifications from JSON `ids`, or all failed ones without a body |

```sh
curl -i http://127.0.0.1:8000/tickets \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: login-demo-1' \
  -d '{"title":"Login issue","message":"Cannot sign in","email":"pat@example.com"}'
curl -X PATCH http://127.0.0.1:8000/tickets/1/status \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"status":"closed","note":"Fixed"}'
```

A missing, unknown, revoked, or expired token returns 401 before any ticket is read,
and a token without the route's scope returns 403 with
`WWW-Authenticate: Bearer error="insufficient_scope", scope="..."`. A missing ticket
or notification returns 404, a reused idempotency key with different content or a
retry of a notification that is not `failed` 409, invalid fields 422, and a storage
failure 503 without exposing file paths. Unknown JSON fields are rejected. Ticket creation, status changes, and comments record the
name of the token that made them.

### Token scopes and expiry

| Scope | Grants |
| --- | --- |
| `tickets:read` | Reading tickets, history, comments, and statistics |
| `tickets:write` | Creating tickets, changing status, and adding comments |
| `emails:read` | Seeing requester emails; without it, `email` and `recipient` are always `null` in responses |
| `notifications:read` | Reading the outbox, including subjects and bodies, which repeat ticket titles, messages, and notes |
| `notifications:write` | Requeueing failed notifications |

A token created without `--scope` gets every scope. Grant only what a client needs,
and add `--expires-in DAYS` (1 to 3650) for a token that should stop working on its
own:

```sh
python3 tickets.py token create web-form --scope tickets:write
python3 tickets.py token create dashboard --scope tickets:read --expires-in 90
python3 tickets.py token list
```

Scopes and expiry are fixed when a token is created; to change them, create a new
token and revoke the old one. A token without `emails:read` can still submit a ticket
with an email, and notifications are still sent; the address is only left out of
what that token is shown, including the response to its own submission. `emails:read`
grants nothing by itself.

A token keeps exactly the scopes it was created with, so it never gains a scope added
in a later version. Tokens created before scopes existed keep `tickets:read`,
`tickets:write`, and `emails:read` and never expire; create a new token to use the
notification endpoints.

## Configuration

| Variable | Used by | Meaning |
| --- | --- | --- |
| `TICKETS_DATABASE` | API | SQLite file; defaults to `tickets.db` in the project folder. The CLI uses `--database`. |
| `TICKETS_PORT` | Docker Compose | Host port for the API, default 8000 |
| `TICKETS_SMTP_HOST` | `send-notifications` | Mail server host name or IP address (required) |
| `TICKETS_SMTP_FROM` | `send-notifications` | Sender, such as `Support <support@example.com>` (required) |
| `TICKETS_SMTP_SECURITY` | `send-notifications` | `starttls` (default), `ssl` for implicit TLS, or `none` |
| `TICKETS_SMTP_PORT` | `send-notifications` | Defaults to 587, 465, or 25 to match the security mode |
| `TICKETS_SMTP_USERNAME`, `TICKETS_SMTP_PASSWORD` | `send-notifications` | Optional login; set both or neither |
| `TICKETS_SMTP_TIMEOUT` | `send-notifications` | Seconds to wait for the server, default 10 |

## Email notifications

A ticket created with an email (`--email` or the JSON `email` field) queues one
notification for each history entry: a "received" message on creation, and a message
with the old and new status and the note on each status change. Emails must be plain
ASCII addresses without spaces, commas, quotes, or angle brackets, and cannot be
changed after creation. Comments, unchanged statuses, and idempotent replays queue
nothing.

`send-notifications` sends one batch of due notifications over a single SMTP
connection and prints how many were sent, scheduled for retry, or marked failed.
It sends one batch and exits, so run it on a schedule, for example from cron:

```text
* * * * * cd /path/to/ticket-backend && TICKETS_SMTP_HOST=smtp.example.com TICKETS_SMTP_FROM=support@example.com python3 tickets.py send-notifications
```

Settings are validated and the server is reached before any notification is
claimed, so a misconfiguration or an outage exits with code 2 or 4 and uses up no
retry attempts. A run with nothing due does not connect. TLS certificates are
verified, and credentials are refused with `security=none` unless the server is on
localhost. A recipient the server refuses, or a connection that drops mid-message, is
recorded on that notification, and the next message uses a fresh connection.

A failed attempt is retried after `--retry-delay` seconds (default 60), doubling each
time up to a day. After `--max-attempts` (default 5) the notification is marked
`failed`. Once the cause is fixed, `retry-notifications` moves every failed
notification, or only the IDs given, back to `pending` with a fresh attempt budget.
Naming a missing notification exits with code 1, and naming one that is not `failed`
exits with code 2, without changing anything.

```sh
python3 tickets.py outbox --status failed
python3 tickets.py retry-notifications 4 7
```

The same operations are available over HTTP to tokens with the notification scopes,
so failures can be checked and retried without shell access to the server.
`POST /notifications/retry` follows the same rules as the command: a missing ID
returns 404 and one that is not `failed` returns 409, and in both cases nothing
changes. For a token without `emails:read`, the recipient is `null` and the address
is replaced with `[hidden]` in `last_error`, where SMTP errors usually quote it.

```sh
curl -H "Authorization: Bearer $TICKETS_TOKEN" 'http://127.0.0.1:8000/notifications?status=failed'
curl -X POST http://127.0.0.1:8000/notifications/retry \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ids":[4,7]}'
```

Messages are plain text with `Auto-Submitted: auto-generated` and a `Message-ID` that
stays the same on every attempt at one notification.

## Docker

```sh
docker compose up --build -d api
docker compose run --rm tickets token create alice
curl http://127.0.0.1:8000/health
docker compose run --rm tickets create --title "Login issue" --message "Cannot sign in"
docker compose run --rm -e TICKETS_SMTP_HOST=smtp.example.com \
  -e TICKETS_SMTP_FROM=support@example.com tickets send-notifications
```

The `api` and `tickets` services share a named volume holding `/data/tickets.db`,
which is separate from the local `tickets.db`, so tokens and ticket IDs differ
between them. The API is published on loopback only, and the image runs as a
non-root user. `docker compose down` keeps the volume; `docker compose down --volumes`
deletes the Docker database.

## Design

```text
tickets.py                  CLI launcher (python3 tickets.py ...)
ticket_backend/
  cli.py                    argument parsing, text and JSON output, exit codes
  api.py                    FastAPI app, request and response models, token check
  tickets.py                tickets, status history, comments, statistics
  tokens.py                 token creation, revocation, and authentication
  notifications.py          outbox queries, delivery, and retries
  mailer.py                 SMTP settings and sender
  validation.py             input checks shared by the storage modules
  database.py, schema.sql   connections and schema upgrades
tests/                      unit tests and the SMTP test server
scripts/smoke_docker.py     Docker integration check
```

The CLI and the API are thin layers over the same storage functions, so both apply
the same validation and transactions to the same database.

**Transactions.** Every write that touches more than one row runs in one SQLite
transaction. Status changes take the write lock (`BEGIN IMMEDIATE`) before reading
the current status, so the update, its history entry, and its notification always
agree. SQLite assigns IDs on insert, and writers wait up to five seconds for the lock
before failing with a storage error.

**Idempotent submissions.** A key (`--idempotency-key` or the `Idempotency-Key`
header, 1–128 ASCII letters, digits, dots, underscores, or hyphens) is a primary key
in `ticket_submissions`, saved in the same transaction as the ticket together with
the original response. Reusing a key with the same title, message, and email returns
that response, with 201 and the same `Location` over HTTP, even if the ticket has
since changed. Different content returns 409 or exit code 3, and an invalid request
does not reserve its key. The CLI and API share keys when they share a database.

**Authentication.** Tokens are 256-bit random values. Only their SHA-256 hashes are
stored, so a lost token cannot be recovered, only revoked and replaced under a new
name. Each token stores its scopes and an optional expiry time next to its hash, and
both are checked on every request, so revocation and expiry apply to the next request.
Each route depends on one scope, which is checked after the token and before the
request body or the ticket, so a client without access learns nothing about either.
Emails are removed from responses in the API layer, so the CLI, notifications, and
idempotent replays still see the stored address. Tokens are created and revoked only
from the terminal, because anyone who can run the CLI can already open the database.

**Outbox.** Notifications are written in the same transaction as the history entry
they describe, so one exists exactly when the other does, and delivery happens later
and separately. Each attempt is counted and its retry scheduled before sending, so a
crash mid-send or a second sender waits for the retry delay instead of sending again
at once. Delivery is at least once: a process that stops after sending but before
recording success sends that message again, with the same `Message-ID`.

**Schema upgrades.** Each connection applies `schema.sql`, which creates any missing
tables and indexes, then adds columns introduced since a database was created.
Existing rows and column definitions are never rewritten, so databases from earlier
versions keep working.

## Testing

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check .
python scripts/smoke_docker.py   # needs Docker
```

The unit tests use temporary databases and cover validation, persistence, schema
upgrades from older databases, transaction rollback, concurrent writers, idempotent
retries, token authentication, scopes, and expiry, the CLI and HTTP interfaces, and
notification delivery. SMTP tests talk to a small SMTP server on 127.0.0.1 inside the test process
(`tests/smtp_stub.py`) and never contact a real mail server. The Docker smoke test
builds the image and checks that the API and CLI containers share data, that a
read-only token cannot write, see emails, or read the outbox, that the API sees
notifications the CLI sent, that tickets, tokens, and idempotency
keys survive container recreation, that the image runs as a
non-root user, and that `send-notifications` delivers through the image. It uses a
random loopback port and removes only its own Compose project and volume. GitHub
Actions runs ruff, the unit tests on Python 3.10, 3.11, and 3.14, and the Docker smoke
test.

## Limitations

- SQLite serializes writers, so this suits a small team or local use, not a busy
  shared service.
- Scopes are coarse (read, write, and emails across all tickets), cannot be changed
  after a token is created, and there are no rate limits. Tokens travel in plain text
  over HTTP, so keep the API on loopback or behind HTTPS.
- Authors are token names, not people, and terminal changes have no author (shown as
  `local`). Entries from before attribution existed have none either.
- Idempotency keys are global rather than scoped to a token, and never expire.
- Comments cannot be edited or deleted, and a ticket's email cannot be changed.
- Requester emails are stored in plain text in the database, and hidden only from API
  tokens without `emails:read`. An address an SMTP server quotes in a different form
  than the recipient, such as an encoded one, is not hidden from `last_error`.
- Notifications have no unsubscribe, bounce handling, HTML version, or retention
  policy, and `send-notifications` needs an external scheduler. A permanently refused
  recipient is retried like any other failure.
