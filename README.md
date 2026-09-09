# Ticket Backend

A Python and SQLite ticket tracker. Submit a ticket in one terminal session and
retrieve it by ID in another. Requires Python 3.10 or later; no third-party
packages are needed.

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

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary databases and cover persistence, existing data, assigned
IDs, SQL-shaped input, validation, missing tickets, concurrent submissions,
transaction rollback, search, pagination, and terminal/JSON workflows.
