# Ticket Backend

[![Tests](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml)

A Python and SQLite ticket tracker. Submit a ticket in one terminal session and
retrieve it by ID in another. Requires Python 3.10 or later; no third-party
packages are needed.

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
tickets survive repeated database initialization.

## Ticket workflow

```sh
python3 tickets.py create --title "Login issue" --message "Cannot sign in"
python3 tickets.py show 1
python3 tickets.py list --status open --search login
python3 tickets.py status 1 in_progress --note "Investigating"
python3 tickets.py status 1 closed --note "Fixed the login form"
python3 tickets.py history 1
python3 tickets.py stats
```

Use the ID returned by `create`. Omit `--title` or `--message` for terminal
prompts. Statuses are `open`, `in_progress`, and `closed`; closed tickets can be
reopened. Setting the current status again does not add a duplicate event.

Status updates and their history entries are committed in the same transaction.
Tickets that existed before history was added retain their data and start
recording history with their next status change.

Lists are newest first, with `--limit` (1–100, default 20) and `--offset`.
Search matches literal text in the title or message. `%` is treated as text.

Use global options before the command:

```sh
python3 tickets.py --database /tmp/ticket-demo.db --json create --title "Demo" --message "Example ticket"
python3 tickets.py --database /tmp/ticket-demo.db --json list
```

JSON output goes to stdout; errors go to stderr. JSON creation requires both
fields and never prompts. Exit codes are `0` for success, `1` for a missing
ticket, `2` for invalid input or a storage error, and `130` for cancellation.

## Run with Docker

```sh
docker compose build
docker compose run --rm tickets create
docker compose run --rm tickets show 1
docker compose run --rm tickets status 1 closed --note "Resolved"
docker compose run --rm tickets history 1
```

Each command starts a container and removes it when finished. A named volume
retains `/data/tickets.db` between runs. Docker's database is separate from the
local `tickets.db`; use the ID printed by your Docker submission. The image
runs as a non-root user and contains only the application code and schema.

This is a terminal application with no HTTP server or published ports.
Use `docker compose run` for commands; there is no long-running service to
start with `up`. `docker compose down` keeps the data volume. Adding `--volumes`
to that command deletes the Docker database.

See [Docker's volume documentation](https://docs.docker.com/engine/storage/volumes/)
for how container storage persists.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary databases and cover persistence, existing data, assigned
IDs, SQL-shaped input, validation, missing tickets, concurrent submissions,
transaction rollback, search, pagination, and terminal/JSON workflows.

Run the Docker integration check separately:

```sh
python3 scripts/smoke_docker.py
```

It builds the image, writes and retrieves a ticket across separate containers,
checks status history and non-root execution, then removes only its temporary
Compose project and volume. GitHub Actions runs this check on Linux alongside
the Python suite on versions 3.10, 3.11, and 3.14.

## How it works

```text
Terminal input or CLI arguments
           |
       tickets.py          command parsing and output
           |
      functions.py         validation, queries, transactions
           |
         db.py             connections and schema initialization
           |
       SQLite file         tickets and ticket_events tables
```

`submit.py` and `retrieve.py` retain the original prompt-based entry points.
SQLite assigns IDs during insertion, avoiding a separate read-and-increment
operation. Status changes acquire the write transaction before reading the
old status, so the update and history describe the same change.

## Scope

This is a local support-ticket tracker. It does not send email, authenticate
users, or serve requests over a network. SQLite serializes writers, so this
design targets local workflows rather than a busy shared service. Initialization
adds missing tables and indexes; it does not modify existing column definitions.
Existing tickets are preserved, and history is not reconstructed for older work.

Next steps: add a small HTTP API around the same storage functions, then model
email delivery with retries and recorded delivery attempts.
