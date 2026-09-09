from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

import db
from functions import TicketNotFoundError, create_ticket, get_ticket, list_tickets


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "nested" / "tickets.db"

    def test_ticket_survives_reopening_the_database(self):
        created = create_ticket(" Login issue ", " Cannot sign in ", self.database)
        loaded = get_ticket(created["id"], self.database)
        self.assertEqual(loaded, created)
        self.assertEqual(loaded["title"], "Login issue")
        self.assertEqual(loaded["message"], "Cannot sign in")
        self.assertEqual(loaded["status"], "open")
        self.assertTrue(loaded["created_at"])

    def test_sql_text_is_stored_as_data(self):
        message = "'); DROP TABLE tickets; --"
        created = create_ticket("O'Brien's login", message, self.database)
        self.assertEqual(get_ticket(created["id"], self.database)["message"], message)
        self.assertEqual(len(list_tickets(self.database)), 1)

    def test_sqlite_assigns_distinct_ids_and_history_is_newest_first(self):
        first = create_ticket("One", "First ticket", self.database)
        second = create_ticket("Two", "Second ticket", self.database)
        self.assertGreater(second["id"], first["id"])
        self.assertEqual([row["id"] for row in list_tickets(self.database)],
                         [second["id"], first["id"]])
        self.assertEqual(len(list_tickets(self.database, limit=1)), 1)

    def test_invalid_input_does_not_create_a_database(self):
        for title, message in ((" ", "body"), ("title", "\n"),
                               ("x" * 121, "body"), ("title", "x" * 10_001)):
            with self.subTest(title=title[:10], message=message[:10]):
                with self.assertRaises(ValueError):
                    create_ticket(title, message, self.database)
        self.assertFalse(self.database.exists())

    def test_unknown_and_invalid_ids_are_distinct_errors(self):
        with self.assertRaises(TicketNotFoundError):
            get_ticket(42, self.database)
        for identifier in (0, -1, "1", True):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    get_ticket(identifier, self.database)

    def test_initialization_preserves_existing_tickets(self):
        self.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tickets (title, message) VALUES ('Existing', 'Keep me');
            """)
        db.initialize(self.database)
        db.initialize(self.database)
        self.assertEqual(get_ticket(1, self.database)["message"], "Keep me")

    def test_invalid_list_limit_is_rejected(self):
        for limit in (0, 101, -1, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    list_tickets(self.database, limit=limit)


if __name__ == "__main__":
    unittest.main()
