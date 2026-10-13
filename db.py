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
        _add_missing_columns(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    # CREATE TABLE IF NOT EXISTS leaves older tables as they were, so add new columns here.
    for table in ("ticket_comments", "ticket_events"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "author" not in columns:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN author TEXT")
            except sqlite3.OperationalError as error:
                # Another connection may have added the column after the check above.
                if "duplicate column" not in str(error):
                    raise


def initialize(database: Path = DEFAULT_DATABASE) -> Path:
    with closing(connect(database)):
        pass
    return Path(database)


if __name__ == "__main__":
    print(f"Database ready: {initialize()}")
