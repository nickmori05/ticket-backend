from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from ticket_backend.database import connect, initialize
from ticket_backend.tickets import (
    IdempotencyConflictError, TicketNotFoundError, add_comment, create_ticket, get_ticket, list_comments, set_status,
    ticket_history, ticket_revision,
)


class CommentIdempotencyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = create_ticket("Login", "Cannot sign in", self.database)

    def comment(self, key="comment-1", body="Asked for logs", ticket_id=None, author=None):
        return add_comment(self.ticket["id"] if ticket_id is None else ticket_id, body, self.database,
                           author=author, idempotency_key=key)

    def counts(self):
        with closing(connect(self.database)) as connection:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("ticket_comments", "ticket_comment_keys", "ticket_events"))

    def test_retry_returns_the_original_comment_and_writes_nothing_else(self):
        first = self.comment(body=" Asked for logs ")
        initialize(self.database)
        self.assertEqual(self.comment(), first)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(list_comments(self.ticket["id"], self.database), [first])

    def test_replay_is_unchanged_when_the_ticket_status_changes(self):
        first = self.comment()
        revision = ticket_revision(self.ticket["id"], self.database)
        set_status(self.ticket["id"], "closed", self.database, note="Fixed")
        self.assertEqual(self.comment(), first)
        self.assertEqual(get_ticket(self.ticket["id"], self.database)["status"], "closed")
        self.assertEqual(len(ticket_history(self.ticket["id"], self.database)), 2)
        self.assertNotEqual(ticket_revision(self.ticket["id"], self.database), revision)
        self.assertEqual(self.counts(), (1, 1, 2))

    def test_different_body_conflicts_without_adding_a_comment(self):
        first = self.comment()
        with self.assertRaises(IdempotencyConflictError):
            self.comment(body="Different note")
        self.assertEqual(list_comments(self.ticket["id"], self.database), [first])
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.comment(), first)

    def test_the_same_key_on_another_ticket_is_a_new_comment(self):
        other = create_ticket("Billing", "Wrong invoice", self.database)
        first = self.comment()
        second = self.comment(ticket_id=other["id"], body="Billing note")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.comment(), first)
        self.assertEqual(self.comment(ticket_id=other["id"], body="Billing note"), second)
        self.assertEqual([row["body"] for row in list_comments(other["id"], self.database)], ["Billing note"])

    def test_each_author_and_the_terminal_have_their_own_keys(self):
        comments = {author: self.comment(author=author, body=f"From {author}") for author in ("alice", "bob", None)}
        self.assertEqual(len({comment["id"] for comment in comments.values()}), 3)
        for author, comment in comments.items():
            with self.subTest(author=author):
                self.assertEqual(self.comment(author=author, body=f"From {author}"), comment)
        with self.assertRaises(IdempotencyConflictError):
            self.comment(author="alice", body="Other")
        self.assertEqual(self.counts(), (3, 3, 1))

    def test_unkeyed_comments_are_always_added(self):
        rows = [self.comment(key) for key in ("comment-1", "comment-2", "Comment-1", None, None)]
        self.assertEqual(len({row["id"] for row in rows}), 5)
        self.assertEqual(self.counts(), (5, 3, 1))

    def test_invalid_keys_and_bodies_do_not_write(self):
        for key in ("", " ", "has space", "x" * 129, "café", "line\n", "' OR 1=1 --", 1, True):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.comment(key)
        with self.assertRaises(ValueError):
            self.comment(body=" ")
        self.assertEqual(self.counts(), (0, 0, 1))
        first = self.comment("A._-" + "x" * 124)
        self.assertEqual(self.comment("A._-" + "x" * 124), first)

    def test_a_missing_ticket_does_not_reserve_the_key(self):
        with self.assertRaises(TicketNotFoundError):
            self.comment(ticket_id=99)
        self.assertEqual(self.counts(), (0, 0, 1))
        first = self.comment()
        self.assertEqual(self.comment(), first)

    def test_concurrent_retries_create_one_comment(self):
        initialize(self.database)
        barrier = threading.Barrier(6)

        def submit(_):
            barrier.wait(timeout=5)
            return self.comment()

        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(submit, range(6)))
        self.assertTrue(all(row == rows[0] for row in rows))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_conflicting_requests_have_one_winner(self):
        initialize(self.database)
        barrier = threading.Barrier(2)

        def submit(body):
            barrier.wait(timeout=5)
            try:
                return self.comment(body=body)
            except IdempotencyConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(submit, ("Asked for logs", "Different note")))
        winners = [row for row in rows if row is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(list_comments(self.ticket["id"], self.database), winners)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_failed_key_record_rolls_back_the_comment(self):
        with closing(connect(self.database)) as connection:
            connection.executescript("""
                CREATE TRIGGER reject_comment_key BEFORE INSERT ON ticket_comment_keys
                BEGIN SELECT RAISE(ABORT, 'Simulated comment key failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.comment()
        self.assertEqual(self.counts(), (0, 0, 1))
        with closing(connect(self.database)) as connection:
            connection.execute("DROP TRIGGER reject_comment_key")
        first = self.comment()
        self.assertEqual(self.comment(), first)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_existing_database_gains_comment_keys_without_rewriting_comments(self):
        kept = add_comment(self.ticket["id"], "Keep me", self.database)
        with closing(connect(self.database)) as connection:
            connection.execute("DROP TABLE ticket_comment_keys")
        first = self.comment()
        self.assertEqual(self.comment(), first)
        self.assertEqual(list_comments(self.ticket["id"], self.database), [kept, first])
        self.assertEqual(self.counts(), (2, 1, 1))


if __name__ == "__main__":
    unittest.main()
