# Ticket Backend

[![Tests](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml)

A Python and SQLite ticket tracker with terminal commands and an HTTP API.
Submit a ticket, retrieve it by ID, track its status changes, and keep
working notes as comments. Tickets with a requester email queue a notification
for each status change, which `tickets.py send-notifications` delivers over SMTP.
The API requires a token created from the terminal.
Requires Python 3.10 or later. The terminal commands use the standard library; the
optional API uses FastAPI and Uvicorn.

```sh
git clone https://github.com/nickmori05/ticket-backend.git
cd ticket-backend
```

```sh
python3 db.py
python3 submit.py
python3 retrieve.py
```

SQLite assigns each ticket its ID. Titles and messages are validated before
writing, and SQL parameters keep user input separate from query instructions.
The database is created automatically when a command first needs it.

`tickets.db` lives beside the Python files and is excluded from Git. Existing
tickets survive repeated database initialization. Optional submission keys
prevent retries from creating duplicate tickets.

## Ticket workflow

```sh
python3 tickets.py create --title "Login issue" --message "Cannot sign in" --email pat@example.com
python3 tickets.py show 1
python3 tickets.py list --status open --search login
python3 tickets.py status 1 in_progress --note "Investigating"
python3 tickets.py status 1 closed --note "Fixed the login form"
python3 tickets.py history 1
python3 tickets.py comment 1 "Asked the customer for a screenshot"
python3 tickets.py comments 1
python3 tickets.py stats
python3 tickets.py outbox --status pending
python3 tickets.py send-notifications
python3 tickets.py retry-notifications
python3 tickets.py token create alice
python3 tickets.py token list
python3 tickets.py token revoke alice
```

Use the ID returned by `create`. Omit `--title` or `--message` for terminal
prompts. Statuses are `open`, `in_progress`, and `closed`; closed tickets can be
reopened. Setting the current status again does not add a duplicate event.

Status updates and their history entries are committed in the same transaction.
Tickets that existed before history was added retain their data and start
recording history with their next status change.

Each history entry records who made it. Tickets created and status changes made
over HTTP record the name of the API token used; terminal changes have no author
and display as `local` in `history`. Entries recorded before attribution existed
also have no author.

Comments are free-form notes of up to 5000 characters, listed oldest first.
They can be added in any status, including after a ticket is closed, and never
change its status or history. Comments added over HTTP record the name of the
API token that wrote them; terminal comments have no author and display as `local`.

`--email` is optional and is never prompted for; see [Notifications](#notifications).

Lists are newest first, with `--limit` (1–100, default 20) and `--offset`.
Search matches literal text in the title or message. `%` is treated as text.

Use global options before the command:

```sh
python3 tickets.py --database /tmp/ticket-demo.db --json create --title "Demo" --message "Example ticket"
python3 tickets.py --database /tmp/ticket-demo.db --json list
```

JSON output goes to stdout; errors go to stderr. JSON creation requires both
fields and never prompts. Exit codes are `0` for success, `1` for a missing
ticket, token, or notification, `2` for invalid input, configuration, or a storage
error, `3` for a conflicting submission key, `4` when the SMTP server cannot be
reached or rejects the session, and `130` for cancellation.

## Run with Docker

Start the API with a persistent database:

```sh
docker compose up --build -d api
curl http://127.0.0.1:8000/health
docker compose run --rm tickets token create alice
```

The API is available at `http://127.0.0.1:8000`, with interactive docs at `/docs`.
Ticket endpoints need a token from the Docker database, as created above; a token
from the local `tickets.db` will not work there. Set `TICKETS_PORT` to change the
host port if 8000 is already occupied. Port publishing is restricted to loopback. The API and terminal service share the
same named data volume.

Run terminal commands against that database:

```sh
docker compose build
docker compose run --rm tickets create
docker compose run --rm tickets show 1
docker compose run --rm tickets status 1 closed --note "Resolved"
docker compose run --rm tickets history 1
```

Each terminal command starts a container and removes it when finished. A named volume
retains `/data/tickets.db` between runs. Docker's database is separate from the
local `tickets.db`; use the ID printed by your Docker submission. The image
runs as a non-root user and contains only the application code and schema.

These Docker commands run the terminal interface. Use `docker compose run`
for one-off commands. `docker compose down` keeps the data volume. Adding `--volumes`
to that command deletes the Docker database.

See [Docker's volume documentation](https://docs.docker.com/engine/storage/volumes/)
for how container storage persists.

## HTTP API

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Every endpoint except `/health` and the docs requires a bearer token. Create one
in another terminal against the same database. It is printed once; store it
somewhere safe:

```sh
python3 tickets.py token create alice
export TICKETS_TOKEN=tkt_...   # the value printed above
```

Open [the interactive API docs](http://127.0.0.1:8000/docs) and use **Authorize**,
or call the API with `curl`:

```sh
curl -i http://127.0.0.1:8000/tickets \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Login issue","message":"Cannot sign in","email":"pat@example.com"}'
curl -H "Authorization: Bearer $TICKETS_TOKEN" http://127.0.0.1:8000/tickets/1
curl -X PATCH http://127.0.0.1:8000/tickets/1/status \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"status":"closed","note":"Fixed"}'
curl -H "Authorization: Bearer $TICKETS_TOKEN" http://127.0.0.1:8000/tickets/1/history
curl -i http://127.0.0.1:8000/tickets/1/comments \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"body":"Asked the customer for a screenshot"}'
curl -H "Authorization: Bearer $TICKETS_TOKEN" http://127.0.0.1:8000/tickets/1/comments
```

Use the ID returned by the POST request. Successful creation returns HTTP 201
and a `Location` header. Both interfaces use the same storage functions and
default database. Set `TICKETS_DATABASE` when starting Uvicorn to choose another
file; an absolute path avoids dependence on the working directory.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Process health; does not check database availability or need a token |
| `POST /tickets` | Create a ticket from JSON `title`, `message`, and optional `email`, attributed to the token |
| `GET /tickets` | List with optional `status`, `search`, `limit`, and `offset` |
| `GET /tickets/{id}` | Retrieve a ticket |
| `PATCH /tickets/{id}/status` | Change status with an optional note, attributed to the token |
| `GET /tickets/{id}/history` | Read recorded changes, each with its `author` |
| `POST /tickets/{id}/comments` | Add a comment from JSON `body`, attributed to the token; returns 201 |
| `GET /tickets/{id}/comments` | Read comments, oldest first |
| `GET /stats` | Count tickets by status |

Missing, unknown, or revoked tokens return 401 before any ticket is read. Missing
tickets return 404, conflicting submission keys return 409, invalid request fields
return 422, and storage failures return 503 without exposing local database paths.

## API tokens

Tokens are managed only from the terminal, because anyone who can run
`tickets.py` can already read and write the database directly. Each token has a
unique name of 1-64 ASCII letters, digits, dots, underscores, or hyphens. The
database stores a SHA-256 hash of each token, never the token itself, so a lost
token cannot be recovered: revoke it and create one with a new name. Revocation
takes effect on the next request. Names are never reused, so comment and status-change
authors stay unambiguous after a token is revoked.

Every token has the same full access to tickets. There are no roles, expiry, or
rate limits, and tokens travel in plain text over HTTP, so keep the API bound to
loopback or put it behind HTTPS before sharing it.

## Notifications

A ticket can be created with an optional requester email, given as `--email` or
the JSON `email` field. It must be an ASCII address such as `pat@example.com`, up
to 254 characters, without spaces, commas, quotes, or angle brackets. It is returned
with the ticket and cannot be changed after creation.

For a ticket with an email, every history entry queues one notification in the
`ticket_notifications` outbox, in the same transaction as the entry itself:
creation queues a "received" message, and each status change queues a message with
the old and new status and the note. Setting the current status again, idempotent
replays, and comments queue nothing. The subject and body are written when the
notification is queued, and the title is collapsed onto one line in the subject.

```sh
python3 tickets.py outbox
python3 tickets.py --json outbox --status failed --limit 50
```

`outbox` lists notifications oldest first, with an optional `--status` of
`pending`, `sent`, or `failed`, plus `--limit` and `--offset`. Each row shows its
recipient, subject, attempt count, and last delivery error.

### Sending notifications

`send-notifications` delivers due notifications over SMTP using only the standard
library. Configure it with environment variables:

| Variable | Meaning |
| --- | --- |
| `TICKETS_SMTP_HOST` | Mail server host name or IP address (required) |
| `TICKETS_SMTP_FROM` | Sender, such as `support@example.com` or `Support <support@example.com>` (required) |
| `TICKETS_SMTP_SECURITY` | `starttls` (default), `ssl` for implicit TLS, or `none` |
| `TICKETS_SMTP_PORT` | Defaults to 587 for `starttls`, 465 for `ssl`, and 25 for `none` |
| `TICKETS_SMTP_USERNAME`, `TICKETS_SMTP_PASSWORD` | Optional login; set both or neither |
| `TICKETS_SMTP_TIMEOUT` | Seconds to wait for the server, default 10 |

```sh
export TICKETS_SMTP_HOST=smtp.example.com
export TICKETS_SMTP_FROM="Support <support@example.com>"
export TICKETS_SMTP_USERNAME=support TICKETS_SMTP_PASSWORD=...
python3 tickets.py send-notifications
python3 tickets.py --json send-notifications --limit 50 --max-attempts 3 --retry-delay 300
```

Each run sends at most `--limit` due notifications (1–100, default 20) over one
connection and prints how many were sent, scheduled for retry, or marked failed.
TLS certificates are verified against Python's default trust store. Credentials are
refused with `none` unless the server is on localhost, so a password is never sent in
plain text across a network. Settings are checked and the server is reached before any
notification is claimed: missing or invalid settings exit with code 2, and an
unreachable server, failed STARTTLS, or rejected login exits with code 4, leaving the
outbox untouched. A run with nothing due exits without connecting.

The command sends one batch and exits, so schedule it with cron, a systemd timer, or a
similar runner. For example, every minute with crontab:

```text
* * * * * cd /path/to/ticket-backend && TICKETS_SMTP_HOST=... TICKETS_SMTP_FROM=... python3 tickets.py send-notifications
```

With Docker, pass the settings to the terminal service:

```sh
docker compose run --rm -e TICKETS_SMTP_HOST=smtp.example.com \
  -e TICKETS_SMTP_FROM=support@example.com tickets send-notifications
```

Messages are plain text with the queued subject and body, a `Message-ID` that stays
the same for every attempt at one notification, and `Auto-Submitted: auto-generated`
so mail systems do not send auto-replies back.

### Retries

A message the server refuses, or a connection that drops mid-message, is recorded on
that notification and the run continues with the next one on a fresh connection.
`send-notifications` uses `functions.deliver_notifications(send, database)`, which
can also drive other senders: it calls `send(notification)` for each due pending
notification, oldest first. A call that returns marks the notification `sent`; one
that raises records the error and schedules a retry after `--retry-delay` seconds
(default 60), doubling each attempt up to a day. After `--max-attempts` (default 5)
the notification is marked `failed` and is not retried. Each attempt is counted and
its retry scheduled before sending, so a crash mid-send or a second sender running
at the same time waits for the delay instead of sending at once. Delivery is still at
least once: if the process stops after sending but before recording success, that
message is sent again later with the same `Message-ID`.

Once the cause of a failure is fixed, requeue failed notifications:

```sh
python3 tickets.py outbox --status failed
python3 tickets.py retry-notifications          # every failed notification
python3 tickets.py retry-notifications 4 7      # only these
```

Requeued notifications become `pending`, due immediately, with their attempt count
reset so they get the full number of attempts again. The last error stays visible
until the next attempt. Naming a notification that does not exist exits with code 1,
and naming one that is not `failed` exits with code 2; in both cases nothing changes.

Notification emails are visible to every API token holder through the ticket and are
stored in plain text in the database. The outbox is managed from the terminal only;
there is no HTTP endpoint for it.

## Retry a submission

Choose one key for each new submission and keep it for retries. These examples
use `login-demo-1`; use a fresh random identifier, such as a UUID, for real submissions.

```sh
python3 tickets.py create --title "Login issue" --message "Cannot sign in" \
  --idempotency-key login-demo-1
```

Running that command again returns the same ticket. The HTTP equivalent is:

```sh
curl -i http://127.0.0.1:8000/tickets \
  -H "Authorization: Bearer $TICKETS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: login-demo-1' \
  -d '{"title":"Login issue","message":"Cannot sign in"}'
```

The CLI and API share keys when they use the same database. Docker uses its own
data volume, so local and Docker submissions do not share keys by default.

| Submission | Result |
| --- | --- |
| New key | Create a ticket and save the original response |
| Same key and same content | Return the original ticket JSON; HTTP keeps 201 and the same `Location` |
| Same key, different title or message | HTTP 409 or CLI exit code 3; existing data stays unchanged |
| No key | Create a new ticket every time |

Keys are case-sensitive and accept 1-128 ASCII letters, digits, dots, underscores,
or hyphens. Title and message comparisons use their validated, whitespace-trimmed
values. An invalid request does not reserve its key.

A replay returns the original creation response, even if the ticket has since
been closed. Use `show` or `GET /tickets/{id}` for its current state. Replays never
reopen tickets or add history entries. The client must retain the key and content
after an uncertain result; generating a new key on every retry defeats this protection.

Submission records survive process and container restarts and have no automatic
expiry. They are global within this local database rather than scoped to an API
token, so any token holder can replay another caller's submission with the same key
and content. Scope keys to tokens and define a retention policy before giving tokens
to separate teams. Keys are not credentials and do not add automatic retries.

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests use temporary databases and cover persistence, existing data, assigned
IDs, SQL-shaped input, validation, missing tickets, concurrent submissions,
transaction rollback, search, pagination, and terminal/JSON workflows.
Submission tests cover concurrent retries and conflicts, rollback of all three
records, replay after status changes, and compatibility with existing databases.
API tests exercise HTTP responses, body validation, shared persistence, status
history, filters, and storage errors without contacting external services.
Comment tests cover validation, ordering, isolation between tickets, rollback,
existing databases, and the CLI and HTTP interfaces. Token tests cover hashed
storage, revocation, name validation, the 401 response on every ticket route,
comment attribution, older comment tables gaining an author column, and the CLI
token commands. Status attribution tests cover authors on creation and status
changes from each interface, rejected and unchanged updates writing nothing, revoked
tokens, a replay by another token, older history tables gaining an author column,
and the `history` output. Notification tests cover email validation, one queued
message per history entry, unchanged statuses and replays queuing nothing, rollback
of the ticket or status change when queuing fails, older tables gaining the email
column, delivery success, recorded failures, retry delays, permanent failure after
the maximum attempts, a concurrent sender skipping a claimed message, and the
`outbox` command and HTTP `email` field. SMTP tests run the sender and the
`send-notifications` command against a small SMTP server on 127.0.0.1 inside the
test process (`tests/smtp_stub.py`), never a real mail server. They cover settings
validation, message headers and encoding, delivery over one connection, a refused
recipient, a dropped connection, login, unreachable servers, failed STARTTLS, and
rejected logins leaving the outbox untouched, and `retry-notifications` requeueing all
or named failed notifications without changing anything on an invalid request.

Run the Docker integration check separately:

```sh
python3 scripts/smoke_docker.py
```

It builds the image, checks that the API and CLI share data, recreates the API
container and retrieves a saved ticket, and checks non-root execution. It also
retries an HTTP submission after recreation and from a separate CLI container,
checking that ticket and history counts stay unchanged. It creates an API token
with the CLI container, checks that the API rejects requests without it, attributes
HTTP status changes and an HTTP comment to it, checks that the CLI outbox shows the
notifications queued by an HTTP ticket with an email and that retries queue no more.
It checks that `send-notifications` reports an unreachable SMTP server without claiming
anything, then delivers both notifications through the image to the test SMTP server
running inside the same container, and checks that revoking the token from the CLI takes effect. It uses
a random loopback port and removes only its temporary Compose project and
volume. GitHub Actions runs this check on Linux alongside
the Python suite on versions 3.10, 3.11, and 3.14.

## How it works

```text
Terminal input or CLI arguments      HTTP requests
           |                              |
       tickets.py                       api.py
           |                              |
           +------------------------------+
      functions.py         validation, queries, transactions
           |               (mailer.py sends the outbox over SMTP)
           |
         db.py             connections and schema initialization
           |
       SQLite file         tickets, ticket_events, ticket_submissions,
                     ticket_comments, api_tokens, ticket_notifications
```

`submit.py` and `retrieve.py` retain the original prompt-based entry points.
SQLite assigns IDs during insertion, avoiding a separate read-and-increment
operation. Status changes acquire the write transaction before reading the
old status, so the update and history describe the same change.

Keyed creation acquires a write transaction before checking the submission key.
The key is a database primary key, and the ticket, initial history entry, and
saved response commit together. Concurrent attempts serialize through SQLite;
if a writer cannot acquire the lock within the connection's five-second timeout,
the existing storage-error response applies. Failed transactions leave no
partially created submission.

## Scope

This is a local support-ticket tracker. It queues notification emails and sends them
in batches when `send-notifications` runs, and API tokens identify callers without
separating their permissions.
SQLite serializes writers, so this design targets local workflows rather than a
busy shared service. Initialization adds missing tables, indexes, the `author`
columns on comments and history, and the `email` column on tickets; it does not
modify existing column definitions. Existing tickets are preserved, and history is
not reconstructed for older work.

Comments written before tokens existed, and comments added from the terminal,
have no author. The same applies to status history. Authors are token names, not
people; anyone holding a token acts under its name. Comments cannot be edited
or deleted.

Tickets created before notifications existed have no email and never queue any,
and earlier history is not queued retroactively. There is no unsubscribe, outbox
retention, HTML mail, or bounce handling; the sender does not run on its own schedule;
and a refused recipient is retried like any other failure rather than failed at once.

Next steps: add an unsubscribe link and a retention policy for sent notifications.
