# Ticket Backend

[![Tests](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml)

A support-ticket tracker built on Python and SQLite, with a command line and an
HTTP API that share one database. Tickets move between `open`, `in_progress`, and
`closed` with a recorded history, collect comments, and can notify the person who
filed them by email.

- **CLI** using only the standard library: create, search, and update tickets, with
  text or JSON output and stable exit codes.
- **HTTP API** built with FastAPI, protected by bearer tokens that are stored hashed,
  limited to scopes and a number of requests per minute, optionally expiring, and
  managed from the terminal.
- **Safe retries**: an optional idempotency key, private to the token that sent it,
  makes a repeated submission return the original ticket instead of creating a duplicate,
  until old keys are pruned.
- **Audit log** of every request made with a token: which token, what route, which
  ticket, and the result, readable over HTTP by tokens with `audit:read`.
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
| `prune-notifications --older-than DAYS` | Delete notifications sent more than 1 to 3650 days ago |
| `suppressions list [--limit N] [--offset N]` | List addresses notifications are not sent to, newest first |
| `suppressions add ADDRESS` / `suppressions remove ADDRESS` | Stop / resume notifications to an address |
| `token create NAME [--scope S ...] [--expires-in DAYS] [--rate-limit N]` | Create an API token, shown once |
| `token list` / `token revoke NAME` | List tokens with their scopes and state / revoke one |
| `audit list [--token NAME] [--ticket ID] [--limit N] [--offset N]` | List recorded API requests, newest first |
| `audit prune --older-than DAYS` | Delete recorded API requests older than 1 to 3650 days |
| `keys prune --older-than DAYS` | Delete idempotency keys saved more than 1 to 3650 days ago |

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
| `1` | Ticket, token, notification, or suppression not found |
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

Every endpoint except `/health`, the [unsubscribe pages](#unsubscribe-links), and the
docs requires `Authorization: Bearer TOKEN`.

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
| `GET /suppressions` | `notifications:read` | Suppressed addresses, newest first, with optional `limit` and `offset` |
| `POST /suppressions` | `notifications:write` | Suppress the JSON `address` by hand; returns 201 and the suppression |
| `DELETE /suppressions/{id}` | `notifications:write` | Remove a suppression and return it |
| `GET /unsubscribe/{token}` | none; the link is signed | HTML page asking the requester to confirm |
| `POST /unsubscribe/{token}` | none; the link is signed | Suppress the requester's address (RFC 8058 one-click) |
| `GET /audit` | `audit:read` | Recorded API requests, newest first, with optional `token`, `ticket_id`, `limit`, and `offset` |

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
`WWW-Authenticate: Bearer error="insufficient_scope", scope="..."`. A token over its
[rate limit](#rate-limits) returns 429 with `Retry-After`. A missing ticket
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
| `notifications:write` | Requeueing failed notifications and managing suppressed addresses |
| `audit:read` | Reading the audit log of every token's requests |

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
notification or audit endpoints.

### Rate limits

Each token may make up to its limit of requests in a burst, and then that many per
minute: its allowance refills continuously, so a token limited to 60 regains one
request each second. A request over the limit returns 429 with `Retry-After` giving
the whole seconds until the next one would be accepted, and the route does not run.
The limit is checked right after the token, before its scope, so requests to routes
the token has no scope for count too; requests refused with 429 do not.

Tokens use the API's `TICKETS_RATE_LIMIT` (default 120 requests per minute; `0` turns
the default off) unless they were created with their own:

```sh
python3 tickets.py token create web-form --scope tickets:write --rate-limit 10
python3 tickets.py token create nightly-import --rate-limit 0   # no limit
TICKETS_RATE_LIMIT=300 python -m uvicorn ticket_backend.api:app --host 127.0.0.1 --port 8000
```

A token's limit is fixed when it is created, like its scopes. Tokens from earlier
versions use the default. Requests without a valid token and `/health` are not
limited. An invalid `TICKETS_RATE_LIMIT` stops the API from starting.

Counts are kept in the API process's memory, so they start again when it restarts,
and each process counts separately: run one API process (as Docker Compose does), or
expect each of `uvicorn --workers N` to allow the full limit. The first refused
request after an accepted one is logged as a warning naming the token.

## Audit log

Every request made with a valid token is recorded once its response is decided: the
token's name, the method, the path without its query string, the matched route (such
as `/tickets/{ticket_id}`), the ticket it named or created, the status code, the
client address, and the time in UTC. Refused and failed requests are recorded too, so
a 403 shows a token trying a route it lacks the scope for, and a 404 shows it probing
for tickets. Requests without a valid token or over the token's rate limit, `/health`,
and the docs are not recorded.

```sh
python3 tickets.py audit list --token web-form
python3 tickets.py audit list --ticket 1
curl -H "Authorization: Bearer $TICKETS_TOKEN" 'http://127.0.0.1:8000/audit?token=web-form&limit=50'
```

Query strings are left out because `search` can hold any text a client sent, and
nothing in a path is an email. Reading the log is recorded like any other request.
Rows are never changed; `audit prune --older-than DAYS` deletes those older than the
given number of days, so run it on a schedule to keep the log from growing without
limit:

```text
0 3 * * * cd /path/to/ticket-backend && python3 tickets.py audit prune --older-than 90
```

`audit:read` shows what every token has done, so grant it only to administrators.

## Configuration

| Variable | Used by | Meaning |
| --- | --- | --- |
| `TICKETS_DATABASE` | API | SQLite file; defaults to `tickets.db` in the project folder. The CLI uses `--database`. |
| `TICKETS_RATE_LIMIT` | API | Requests per minute for each token without its own limit, default 120; `0` for none |
| `TICKETS_PORT` | Docker Compose | Host port for the API, default 8000 |
| `TICKETS_SMTP_HOST` | `send-notifications` | Mail server host name or IP address (required) |
| `TICKETS_SMTP_FROM` | `send-notifications` | Sender, such as `Support <support@example.com>` (required) |
| `TICKETS_SMTP_SECURITY` | `send-notifications` | `starttls` (default), `ssl` for implicit TLS, or `none` |
| `TICKETS_SMTP_PORT` | `send-notifications` | Defaults to 587, 465, or 25 to match the security mode |
| `TICKETS_SMTP_USERNAME`, `TICKETS_SMTP_PASSWORD` | `send-notifications` | Optional login; set both or neither |
| `TICKETS_SMTP_TIMEOUT` | `send-notifications` | Seconds to wait for the server, default 10 |
| `TICKETS_UNSUBSCRIBE_SECRET` | API, `send-notifications` | At least 32 characters that sign [unsubscribe links](#unsubscribe-links); the same value in both |
| `TICKETS_UNSUBSCRIBE_URL` | `send-notifications` | The API's public address, such as `https://support.example.com`; links are added when this and the secret are set |

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
`failed`. A recipient the server refuses with a permanent (5xx) reply, such as
`550 No such user`, is marked `failed` after that one attempt, since sending the same
message again cannot succeed. Temporary (4xx) refusals, and `552`, which servers also
use for full mailboxes, are retried as usual. A refused sender or message body affects
every message, so it is retried like an outage. Once the cause is fixed,
`retry-notifications` moves every failed notification, or only the IDs given, back to
`pending` with a fresh attempt budget.
Naming a missing notification exits with code 1, and naming one that is not `failed`
exits with code 2, without changing anything.

```sh
python3 tickets.py outbox --status failed
python3 tickets.py retry-notifications 4 7
```

A permanently refused address is also **suppressed**: later notifications to it, and
any already queued, are marked `failed` with `Not sent: ... is suppressed` instead of
being sent, without using an attempt. Addresses match without regard to case. Use
`suppressions add` to stop notifications to an address by hand, for example when a
requester asks, and `suppressions remove` once an address works again:

```sh
python3 tickets.py suppressions list
python3 tickets.py suppressions remove pat@example.com
python3 tickets.py retry-notifications
```

Removing a suppression does not resend anything by itself. `retry-notifications`
skips notifications to suppressed addresses, and naming one exits with code 2, so
remove the suppression first. An address keeps its first suppression, so suppressing a
refused address by hand does not change its recorded reason or server reply.

Over HTTP, `GET`, `POST`, and `DELETE /suppressions` do the same with the notification
scopes. Suppressions are removed by ID rather than by address, so no address appears
in a URL or the audit log. As in the outbox, a token without `emails:read` sees
`address` as `null` and the address as `[hidden]` in the server's reply.

```sh
curl -H "Authorization: Bearer $TICKETS_TOKEN" http://127.0.0.1:8000/suppressions
curl -X DELETE -H "Authorization: Bearer $TICKETS_TOKEN" http://127.0.0.1:8000/suppressions/3
```

Sent notifications are kept until `prune-notifications --older-than DAYS` deletes
those sent more than the given number of days ago. Pending and failed notifications
are never pruned, so nothing still waiting to be sent or retried is lost. A schedule
keeps the outbox, which holds copies of ticket messages and requester emails, from
growing without limit:

```text
0 3 * * * cd /path/to/ticket-backend && python3 tickets.py prune-notifications --older-than 30
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

### Unsubscribe links

A requester can stop notifications themselves from a link, without asking the support
team and without an API token. `/unsubscribe/{token}` names a ticket and carries an
HMAC-SHA256 of the ticket ID and its requester's address, keyed with
`TICKETS_UNSUBSCRIBE_SECRET`:

- `GET` shows a page with an **Unsubscribe** button and changes nothing, so link
  scanners and mail clients that open links in advance cannot unsubscribe anyone.
- `POST` suppresses the address with reason `unsubscribed`, the same suppression a
  permanent refusal creates, and works with any body, as RFC 8058 one-click requests
  from mail clients send `List-Unsubscribe=One-Click`. Repeating it changes nothing.
- A malformed, forged, or outdated link, or any link when no secret is set, gets the
  same 404 page, so a link reveals nothing about which tickets exist.

Nothing is stored per link. The API recomputes the signature from the ticket, so a
link works as long as the ticket exists and the secret is unchanged, and the address
never appears in the URL or on the page. Pages are sent with `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`, and a restrictive Content Security Policy, because the
URL is the credential. Requests to these pages are not written to the audit log, which
records only token requests; the suppression records the ticket the link came from.
Generate a secret with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
Changing it invalidates every link already sent.

To put the link in emails, give `send-notifications` the same secret and the address
where requesters reach the API. Each message then carries `List-Unsubscribe` and
`List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers, which mail clients show
as an unsubscribe button, and the link again at the end of the body:

```sh
TICKETS_SMTP_HOST=smtp.example.com TICKETS_SMTP_FROM=support@example.com \
TICKETS_UNSUBSCRIBE_URL=https://support.example.com TICKETS_UNSUBSCRIBE_SECRET=... \
python3 tickets.py send-notifications
```

The URL must use `https`, as RFC 8058 requires, unless it points at localhost, and may
include a path prefix if the API is served under one. Setting only one of the two
variables, a URL that is not absolute, or a short secret exits with code 2 before
connecting. Links are built when a message is sent, not stored in the outbox, so a
changed URL or secret applies to every message sent afterwards, including retries.

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

For [unsubscribe links](#unsubscribe-links), start the API with
`TICKETS_UNSUBSCRIBE_SECRET` set, which Compose passes to the `api` service, and give
`send-notifications` the same secret and the API's public address:

```sh
export TICKETS_UNSUBSCRIBE_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up -d api
docker compose run --rm -e TICKETS_SMTP_HOST=smtp.example.com \
  -e TICKETS_SMTP_FROM=support@example.com -e TICKETS_UNSUBSCRIBE_SECRET \
  -e TICKETS_UNSUBSCRIBE_URL=https://support.example.com tickets send-notifications
```

## Design

```text
tickets.py                  CLI launcher (python3 tickets.py ...)
ticket_backend/
  cli.py                    argument parsing, text and JSON output, exit codes
  api.py                    FastAPI app, request and response models, token check
  api_audit.py              middleware that records each authenticated request
  api_unsubscribe.py        unsubscribe pages that need no token
  audit.py                  audit log storage, listing, and pruning
  tickets.py                tickets, status history, comments, statistics
  submissions.py            idempotency keys, scoped to each token, and their pruning
  tokens.py                 token creation, revocation, and authentication
  ratelimit.py              per-token request limits kept in memory
  notifications.py          outbox queries, delivery, and retries
  suppressions.py           addresses notifications are not sent to
  unsubscribe.py            signing and checking unsubscribe links
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
header, 1–128 ASCII letters, digits, dots, underscores, or hyphens) is saved in
`ticket_submission_keys` together with the caller and the original response, in the
same transaction as the ticket. Reusing a key with the same title, message, and email
returns that response, with 201 and the same `Location` over HTTP, even if the ticket
has since changed. Different content returns 409 or exit code 3, and an invalid
request does not reserve its key.

Keys belong to the caller: each token has its own, identified by its name, and the
terminal has one set shared by every CLI user. Another token sending the same key gets
a new ticket, so it can neither replay someone else's submission nor learn from a 409
what they submitted, such as an email it cannot see. Keys saved by earlier versions
stay in `ticket_submissions` and are not rewritten. Each belongs to the token recorded
as its ticket's creator; keys whose ticket has no recorded creator, because it came
from the terminal or predates attribution, stay shared by every caller as before, so
no retry in flight during an upgrade creates a duplicate.

Keys are kept until `keys prune --older-than DAYS` deletes those saved more than the
given number of days ago, from every caller, so run it on a schedule to keep
`ticket_submission_keys` from growing with every keyed submission:

```text
0 3 * * * cd /path/to/ticket-backend && python3 tickets.py keys prune --older-than 30
```

Tickets are never deleted, only the ability to replay their submission: a pruned key
used again, with any content, creates a new ticket instead of returning the original
or a 409. Choose an age well beyond how long any client keeps retrying one submission.
Keys saved by earlier versions have no saved time, so they are pruned by the age of
their ticket, which was created in the same transaction.

**Authentication.** Tokens are 256-bit random values. Only their SHA-256 hashes are
stored, so a lost token cannot be recovered, only revoked and replaced under a new
name. Each token stores its scopes and an optional expiry time next to its hash, and
both are checked on every request, so revocation and expiry apply to the next request.
Each route depends on one scope, which is checked after the token and before the
request body or the ticket, so a client without access learns nothing about either.
Emails are removed from responses in the API layer, so the CLI, notifications, and
idempotent replays still see the stored address. Tokens are created and revoked only
from the terminal, because anyone who can run the CLI can already open the database.

**Audit log.** The token check stores the token's name on the request, and a
middleware writes one row after the response is decided, in its own transaction, so
it records the real status code without checking the token again. A route that
creates a ticket also stores the new ID. Because the row is written after the
request's own transaction commits, a storage failure while recording is logged and
the response is still returned: the change has already happened, and failing the
request would invite a retry.

**Rate limits.** Each token has a bucket in memory holding up to its limit, refilled
at that many requests per minute, behind one lock so concurrent requests cannot
overspend it. Buckets are keyed by token name, so there is at most one per token. The
check runs after authentication, because an unauthenticated request has no token to
charge, and before the audit log marks the request, so refusals are not written: a
client over its limit costs a token lookup but adds nothing to the database.

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
retries, their per-token keys, and pruning them, token authentication, scopes, expiry, and rate limits,
the audit log, the CLI
and HTTP interfaces, and notification delivery. SMTP tests talk to a small SMTP server
on 127.0.0.1 inside the test process (`tests/smtp_stub.py`) and never contact a real mail server. The Docker smoke test
builds the image and checks that the API and CLI containers share data, that a
read-only token cannot write, see emails, or read the outbox or audit log, that the
CLI sees requests the API recorded in the audit log, that a token over its rate limit
gets 429 and is not audited, that the API sees
notifications the CLI sent, that tickets, tokens, and idempotency
keys survive container recreation, that the CLI's keys are separate from the API
token's, that the CLI prunes an old key so the API's next retry creates a new ticket,
that the image runs as a
non-root user, and that `send-notifications` delivers through the image, skipping an
address suppressed over HTTP until the CLI removes the suppression, and that the
link in a sent message shows a page without unsubscribing and then unsubscribes the
requester with one POST and no token, while a forged link gets 404. It uses a
random loopback port and removes only its own Compose project and volume. GitHub
Actions runs ruff, the unit tests on Python 3.10, 3.11, and 3.14, and the Docker smoke
test.

## Limitations

- SQLite serializes writers, so this suits a small team or local use, not a busy
  shared service.
- Scopes are coarse (read, write, and emails across all tickets), and neither scopes
  nor rate limits can be changed after a token is created. Tokens travel in plain text
  over HTTP, so keep the API on loopback or behind HTTPS.
- Rate limits are per process and reset on restart, so they do not hold across several
  API processes or hosts. Requests without a valid token are not limited; put a proxy
  that limits by address in front of the API if it is exposed.
- Authors are token names, not people, and terminal changes have no author (shown as
  `local`). Entries from before attribution existed have none either.
- Idempotency keys are deleted only by `keys prune`, which needs an external
  scheduler, and a key pruned while a client is still retrying lets that retry create a
  duplicate. Keys belong to a token's name, so a replacement token cannot replay keys
  sent by the one it replaces. Keys saved by earlier versions
  for tickets with no recorded creator remain shared by every caller.
- The audit log is written after each request, not in the same transaction, so a
  crash or storage failure between the two leaves a completed request unrecorded.
  Requests without a valid token are not recorded, the client address is the proxy's
  when one is in front of the API, and old rows are deleted only by `audit prune`.
- Comments cannot be edited or deleted, and a ticket's email cannot be changed.
- Requester emails are stored in plain text in the database, and hidden only from API
  tokens without `emails:read`. An address an SMTP server quotes in a different form
  than the recipient, such as an encoded one, is not hidden from `last_error`.
- Notifications have no HTML version, and `send-notifications` and
  `prune-notifications` need an external scheduler. Bounces that arrive later by email,
  after the server accepted the message, are not read, so only refusals during the SMTP
  session suppress an address. Mail providers honour one-click unsubscribe only for
  messages with a DKIM signature that covers the `List-Unsubscribe` headers; signing is
  left to the SMTP relay. Unsubscribing stops every notification to the address, not
  only those about one ticket, and the pages are not rate limited.
