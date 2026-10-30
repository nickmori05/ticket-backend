"""SQLite connections. Every connection applies schema.sql, so a new or older database is ready to use."""

from contextlib import closing
from pathlib import Path
import sqlite3


SCHEMA = Path(__file__).resolve().with_name("schema.sql")
# The default database sits at the project root, where it has always been, not inside the package.
DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "tickets.db"


def connect(database: Path = DEFAULT_DATABASE) -> sqlite3.Connection:
    """Open a database, creating it and any missing tables, indexes, or columns, with rows returned as mappings."""
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        _add_missing_columns(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    # CREATE TABLE IF NOT EXISTS leaves older tables as they were, so add new columns here.
    for table, column, kind in (
        ("ticket_comments", "author", "TEXT"), ("ticket_events", "author", "TEXT"), ("tickets", "email", "TEXT"),
        ("api_tokens", "scopes", "TEXT"), ("api_tokens", "expires_at", "TIMESTAMP"),
        ("api_tokens", "rate_limit", "INTEGER"), ("api_tokens", "rotated_at", "TIMESTAMP"),
        ("api_tokens", "previous_token_hash", "TEXT"), ("api_tokens", "previous_expires_at", "TIMESTAMP"),
        ("ticket_comments", "edited_at", "TIMESTAMP"), ("ticket_comments", "edited_by", "TEXT"),
        ("ticket_comments", "deleted_at", "TIMESTAMP"), ("ticket_comments", "deleted_by", "TEXT"),
    ):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError as error:
                # Another connection may have added the column after the check above.
                if "duplicate column" not in str(error):
                    raise
    # Indexes on added columns are created here, because schema.sql runs before older tables have them.
    connection.execute("""CREATE INDEX IF NOT EXISTS api_tokens_previous_token_hash ON api_tokens(previous_token_hash)
                          WHERE previous_token_hash IS NOT NULL""")


def initialize(database: Path = DEFAULT_DATABASE) -> Path:
    """Create or upgrade a database without reading or writing tickets."""
    with closing(connect(database)):
        pass
    return Path(database)
