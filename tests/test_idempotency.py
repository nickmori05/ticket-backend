from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from db import connect, initialize
from functions import (
    IdempotencyConflictError, create_ticket, get_ticket, list_tickets,
    set_status, ticket_history,
)


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def create(self, key="submission-1", title="Login", message="Cannot sign in"):
        return create_ticket(title, message, self.database, idempotency_key=key)

    def counts(self):
        with closing(connect(self.database)) as connection:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("tickets", "ticket_events", "ticket_submissions"))

    def test_retry_after_reopening_returns_original_ticket_and_history(self):
        first = self.create(title=" Login ", message=" Cannot sign in ")
        initialize(self.database)
        self.assertEqual(self.create(), first)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(len(ticket_history(first["id"], self.database)), 1)

    def test_replay_preserves_creation_response_after_ticket_changes(self):
        first = self.create()
        set_status(first["id"], "closed", self.database, note="Fixed")
        self.assertEqual(self.create(), first)
        self.assertEqual(get_ticket(first["id"], self.database)["status"], "closed")
        self.assertEqual(self.counts(), (1, 2, 1))

    def test_different_content_conflicts_without_modifying_the_original(self):
        first = self.create()
        for arguments in ({"title": "Billing"}, {"message": "New details"}):
            with self.subTest(arguments=arguments), self.assertRaises(IdempotencyConflictError):
                self.create(**arguments)
        self.assertEqual(list_tickets(self.database), [first])
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.create(), first)

    def test_new_keys_and_unkeyed_requests_create_separate_tickets(self):
        rows = [self.create(key) for key in ("submission-1", "submission-2", "Submission-1", None, None)]
        self.assertEqual(len({row["id"] for row in rows}), 5)
        self.assertEqual(self.counts(), (5, 5, 3))

    def test_invalid_keys_and_payloads_do_not_create_a_database(self):
        for key in ("", " ", "has space", "x" * 129, "café", "line\n", "' OR 1=1 --", 1, True):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.create(key)
        with self.assertRaises(ValueError):
            self.create(title=" ")
        self.assertFalse(self.database.exists())
        first = self.create("A._-" + "x" * 124)
        self.assertEqual(self.create("A._-" + "x" * 124), first)

    def test_concurrent_retries_create_one_ticket(self):
        initialize(self.database)
        barrier = threading.Barrier(6)

        def submit(_):
            barrier.wait(timeout=5)
            return self.create()

        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(submit, range(6)))
        self.assertTrue(all(row == rows[0] for row in rows))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_conflicting_requests_have_one_winner(self):
        initialize(self.database)
        barrier = threading.Barrier(2)

        def submit(title):
            barrier.wait(timeout=5)
            try:
                return self.create(title=title)
            except IdempotencyConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(submit, ("Login", "Billing")))
        winners = [row for row in rows if row is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(list_tickets(self.database), winners)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_failed_submission_record_rolls_back_ticket_and_history(self):
        with closing(connect(self.database)) as connection:
            connection.executescript("""
                CREATE TRIGGER reject_submission BEFORE INSERT ON ticket_submissions
                BEGIN SELECT RAISE(ABORT, 'Simulated submission failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()
        self.assertEqual(self.counts(), (0, 0, 0))
        with closing(connect(self.database)) as connection:
            connection.execute("DROP TRIGGER reject_submission")
        first = self.create()
        self.assertEqual(self.create(), first)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_existing_database_is_extended_without_rewriting_tickets(self):
        first = create_ticket("Existing", "Keep me", self.database)
        with closing(connect(self.database)) as connection:
            connection.execute("DROP TABLE ticket_submissions")
        second = self.create()
        self.assertEqual(self.create(), second)
        self.assertEqual(get_ticket(first["id"], self.database), first)
        self.assertEqual(self.counts(), (2, 2, 1))


if __name__ == "__main__":
    unittest.main()
