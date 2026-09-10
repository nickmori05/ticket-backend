# Ticket Backend

[![Tests](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/ticket-backend/actions/workflows/tests.yml)

A Python and SQLite ticket tracker with terminal commands and an HTTP API.
Submit a ticket, retrieve it by ID, and track its status changes. Requires
Python 3.10 or later. The terminal commands use the standard library; the
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

Start the API with a persistent database:

```sh
docker compose up --build -d api
curl http://127.0.0.1:8000/health
```

The API is available at `http://127.0.0.1:8000`, with interactive docs at `/docs`.
Set `TICKETS_PORT` to change the host port if 8000 is already occupied. Port
publishing is restricted to loopback. The API and terminal service share the
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

Open [the interactive API docs](http://127.0.0.1:8000/docs), or use another
terminal:

```sh
curl -i http://127.0.0.1:8000/tickets \
  -H 'Content-Type: application/json' \
  -d '{"title":"Login issue","message":"Cannot sign in"}'
curl http://127.0.0.1:8000/tickets/1
curl -X PATCH http://127.0.0.1:8000/tickets/1/status \
  -H 'Content-Type: application/json' \
  -d '{"status":"closed","note":"Fixed"}'
curl http://127.0.0.1:8000/tickets/1/history
```

Use the ID returned by the POST request. Successful creation returns HTTP 201
and a `Location` header. Both interfaces use the same storage functions and
default database. Set `TICKETS_DATABASE` when starting Uvicorn to choose another
file; an absolute path avoids dependence on the working directory.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Process health; does not check database availability |
| `POST /tickets` | Create a ticket from JSON `title` and `message` |
| `GET /tickets` | List with optional `status`, `search`, `limit`, and `offset` |
| `GET /tickets/{id}` | Retrieve a ticket |
| `PATCH /tickets/{id}/status` | Change status with an optional note |
| `GET /tickets/{id}/history` | Read recorded changes |
| `GET /stats` | Count tickets by status |

Missing tickets return 404, invalid request fields return 422, and storage
failures return 503 without exposing local database paths. The API has no
authentication and is intended for local use. Keep it bound to loopback until
access control is added.

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests use temporary databases and cover persistence, existing data, assigned
IDs, SQL-shaped input, validation, missing tickets, concurrent submissions,
transaction rollback, search, pagination, and terminal/JSON workflows.
API tests exercise HTTP responses, body validation, shared persistence, status
history, filters, and storage errors without contacting external services.

Run the Docker integration check separately:

```sh
python3 scripts/smoke_docker.py
```

It builds the image, checks that the API and CLI share data, recreates the API
container and retrieves a saved ticket, and checks non-root execution. It uses
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

This is a local support-ticket tracker. It does not send email or authenticate
users. SQLite serializes writers, so this
design targets local workflows rather than a busy shared service. Initialization
adds missing tables and indexes; it does not modify existing column definitions.
Existing tickets are preserved, and history is not reconstructed for older work.

Next steps: add access control before sharing the API, then model email delivery
with retries and recorded delivery attempts.
