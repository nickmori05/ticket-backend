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

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary databases and cover persistence, existing data, assigned
IDs, SQL-shaped input, validation, and missing tickets.
