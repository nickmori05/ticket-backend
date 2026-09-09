"""Open the ticket database and initialize missing tables."""

from contextlib import closing
from pathlib import Path
import sqlite3


FOLDER = Path(__file__).resolve().parent
DEFAULT_DATABASE = FOLDER / "tickets.db"


def connect(database: Path = DEFAULT_DATABASE) -> sqlite3.Connection:
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript((FOLDER / "schema.sql").read_text(encoding="utf-8"))
    except Exception:
        connection.close()
        raise
    return connection


def initialize(database: Path = DEFAULT_DATABASE) -> Path:
    with closing(connect(database)):
        pass
    return Path(database)


if __name__ == "__main__":
    print(f"Database ready: {initialize()}")

