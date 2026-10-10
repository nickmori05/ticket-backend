from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from db import connect
from functions import (
    TicketNotFoundError, add_comment, create_ticket, get_ticket, list_comments,
    set_status, ticket_history,
)


class CommentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = create_ticket("Login", "Cannot sign in", self.database)

    def test_comments_are_trimmed_and_listed_oldest_first(self):
        first = add_comment(self.ticket["id"], " Asked for a screenshot ", self.database)
        second = add_comment(self.ticket["id"], "Reproduced on mobile", self.database)
        self.assertEqual(first["body"], "Asked for a screenshot")
        self.assertEqual(first["ticket_id"], self.ticket["id"])
        self.assertTrue(first["created_at"])
        self.assertEqual(list_comments(self.ticket["id"], self.database), [first, second])

    def test_comments_belong_to_one_ticket(self):
        other = create_ticket("Billing", "Wrong invoice", self.database)
        add_comment(self.ticket["id"], "Login note", self.database)
        self.assertEqual(list_comments(other["id"], self.database), [])

    def test_comments_do_not_change_status_or_history(self):
        set_status(self.ticket["id"], "closed", self.database, note="Fixed")
        add_comment(self.ticket["id"], "Customer confirmed the fix", self.database)
        self.assertEqual(get_ticket(self.ticket["id"], self.database)["status"], "closed")
        self.assertEqual(len(ticket_history(self.ticket["id"], self.database)), 2)

    def test_sql_text_is_stored_as_data(self):
        body = "'); DROP TABLE ticket_comments; --"
        add_comment(self.ticket["id"], body, self.database)
        self.assertEqual(list_comments(self.ticket["id"], self.database)[0]["body"], body)

    def test_invalid_comments_are_rejected_without_writing(self):
        for body in (" ", "x" * 5_001, None):
            with self.subTest(body=str(body)[:20]):
                with self.assertRaises(ValueError):
                    add_comment(self.ticket["id"], body, self.database)
        with self.assertRaises(ValueError):
            add_comment(0, "Body", self.database)
        self.assertEqual(list_comments(self.ticket["id"], self.database), [])

    def test_missing_tickets_raise_not_found(self):
        for operation in (lambda: add_comment(99, "Body", self.database),
                          lambda: list_comments(99, self.database)):
            with self.assertRaises(TicketNotFoundError):
                operation()

    def test_existing_databases_gain_comments_without_losing_tickets(self):
        legacy = self.database.with_name("legacy.db")
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript("""
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tickets (title, message) VALUES ('Old', 'Before comments');
            """)
        add_comment(1, "First comment", legacy)
        self.assertEqual(get_ticket(1, legacy)["title"], "Old")
        self.assertEqual([row["body"] for row in list_comments(1, legacy)], ["First comment"])

    def test_failed_insert_leaves_no_comment(self):
        with closing(connect(self.database)) as connection:
            connection.executescript("""
                CREATE TRIGGER reject_comment AFTER INSERT ON ticket_comments
                BEGIN SELECT RAISE(ABORT, 'Simulated comment failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            add_comment(self.ticket["id"], "Body", self.database)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_comments").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
