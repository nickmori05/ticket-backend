from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from db import connect
from functions import create_ticket, get_ticket, set_status, ticket_history


class StatusAttributionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def test_creation_and_status_changes_record_an_optional_author(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database, author="alice")
        set_status(ticket["id"], "in_progress", self.database, author="bob")
        set_status(ticket["id"], "closed", self.database)
        self.assertEqual([(row["to_status"], row["author"]) for row in ticket_history(ticket["id"], self.database)],
                         [("open", "alice"), ("in_progress", "bob"), ("closed", None)])
        self.assertNotIn("author", get_ticket(ticket["id"], self.database))

    def test_unchanged_status_records_no_author(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database)
        set_status(ticket["id"], "open", self.database, author="alice")
        self.assertEqual([row["author"] for row in ticket_history(ticket["id"], self.database)], [None])

    def test_invalid_authors_are_rejected_without_writing(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database)
        for author in ("", " ", "x" * 65, 5):
            with self.subTest(author=author):
                with self.assertRaises(ValueError):
                    set_status(ticket["id"], "closed", self.database, author=author)
                with self.assertRaises(ValueError):
                    create_ticket("Other", "Body", self.database, author=author)
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], "open")
        self.assertEqual(len(ticket_history(ticket["id"], self.database)), 1)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)

    def test_existing_history_tables_gain_an_author_column(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE ticket_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    from_status TEXT,
                    to_status TEXT NOT NULL CHECK(to_status IN ('open', 'in_progress', 'closed')),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tickets (title, message) VALUES ('Old', 'Before attribution');
                INSERT INTO ticket_events (ticket_id, to_status) VALUES (1, 'open');
            """)
        set_status(1, "closed", self.database, note="Fixed", author="alice")
        self.assertEqual(get_ticket(1, self.database)["title"], "Old")
        self.assertEqual([(row["to_status"], row["note"], row["author"]) for row in ticket_history(1, self.database)],
                         [("open", "", None), ("closed", "Fixed", "alice")])

    def test_repeated_connections_add_the_column_once(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE ticket_events (id INTEGER PRIMARY KEY, ticket_id INTEGER, "
                               "from_status TEXT, to_status TEXT, note TEXT NOT NULL DEFAULT '')")
        with closing(connect(self.database)), closing(connect(self.database)) as second:
            columns = [row["name"] for row in second.execute("PRAGMA table_info(ticket_events)")]
        self.assertEqual(columns.count("author"), 1)


if __name__ == "__main__":
    unittest.main()
