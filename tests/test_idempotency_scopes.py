from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.database import connect
from ticket_backend.tickets import IdempotencyConflictError, create_ticket, list_tickets, set_status, ticket_history
from ticket_backend.tokens import create_token


class KeyScopeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def create(self, author, key="k1", title="Login", message="Cannot sign in", email=None):
        return create_ticket(title, message, self.database, idempotency_key=key, author=author, email=email)

    def rows(self, table):
        with closing(connect(self.database)) as connection:
            return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def save_legacy_key(self, key, ticket):
        # The previous version stored every key in ticket_submissions, shared by all callers.
        with closing(connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO ticket_submissions VALUES (?, ?, ?)",
                               (key, ticket["id"], json.dumps(ticket)))

    def test_each_author_and_the_terminal_have_their_own_keys(self):
        tickets = {author: self.create(author) for author in ("alice", "bob", None)}
        self.assertEqual(len({ticket["id"] for ticket in tickets.values()}), 3)
        for author, ticket in tickets.items():
            with self.subTest(author=author):
                self.assertEqual(self.create(author), ticket)
        self.assertEqual(len(list_tickets(self.database)), 3)
        self.assertEqual(sorted(row[0] for row in self.rows("ticket_submission_keys")), ["", "alice", "bob"])

    def test_conflicts_are_checked_only_against_the_callers_own_key(self):
        first = self.create("alice", email="pat@example.com")
        # Another caller learns nothing from the key: any content, including a guessed email, creates its own ticket.
        guessed = self.create("bob", email="pat@example.com")
        different = self.create("carol", message="Other content")
        self.assertEqual(len({first["id"], guessed["id"], different["id"]}), 3)
        with self.assertRaises(IdempotencyConflictError):
            self.create("alice", message="Other content")
        with self.assertRaises(IdempotencyConflictError):
            self.create("bob")
        self.assertEqual(len(list_tickets(self.database)), 3)

    def test_legacy_key_belongs_to_the_token_that_created_its_ticket(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database, author="alice")
        self.save_legacy_key("k1", ticket)
        # The owner comes from the ticket's creation, not from whoever changed it last.
        set_status(ticket["id"], "closed", self.database, author="bob")
        legacy = self.rows("ticket_submissions")
        self.assertEqual(self.create("alice"), ticket)
        with self.assertRaises(IdempotencyConflictError):
            self.create("alice", message="Other content")
        others = [self.create("bob"), self.create(None)]
        self.assertNotIn(ticket["id"], {other["id"] for other in others})
        self.assertEqual([self.create("bob"), self.create(None)], others)
        self.assertEqual(self.rows("ticket_submissions"), legacy)
        self.assertEqual(sorted(row[0] for row in self.rows("ticket_submission_keys")), ["", "bob"])

    def test_legacy_key_without_a_recorded_author_stays_shared(self):
        # Tickets from the terminal, or from before authors were recorded, have no known owner.
        ticket = create_ticket("Login", "Cannot sign in", self.database)
        self.save_legacy_key("k1", ticket)
        for author in ("alice", "bob", None):
            with self.subTest(author=author):
                self.assertEqual(self.create(author), ticket)
                with self.assertRaises(IdempotencyConflictError):
                    self.create(author, title="Billing")
        self.assertEqual(list_tickets(self.database), [ticket])
        self.assertEqual(self.rows("ticket_submission_keys"), [])

    def test_concurrent_retries_by_two_tokens_create_one_ticket_each(self):
        connect(self.database).close()
        barrier = threading.Barrier(6)

        def submit(author):
            barrier.wait(timeout=5)
            return self.create(author)

        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(submit, ("alice", "bob") * 3))
        self.assertEqual(len({row["id"] for row in rows[0::2]}), 1)
        self.assertEqual(len({row["id"] for row in rows[1::2]}), 1)
        self.assertEqual(len(list_tickets(self.database)), 2)
        self.assertEqual(len(self.rows("ticket_events")), 2)


class KeyScopeApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.payload = {"title": "Login", "message": "Cannot sign in", "email": "pat@example.com"}

    def submit(self, headers, payload=None, key="k1"):
        return self.client.post("/tickets", json=payload or self.payload, headers={**headers, "Idempotency-Key": key})

    def bearer(self, name, scopes=None):
        return {"Authorization": f"Bearer {create_token(name, self.database, scopes=scopes)['token']}"}

    def test_another_token_with_the_same_key_gets_its_own_ticket(self):
        alice, bob = self.bearer("alice"), self.bearer("bob")
        first, other = self.submit(alice), self.submit(bob)
        self.assertEqual((first.status_code, other.status_code), (201, 201))
        self.assertNotEqual(first.headers["Location"], other.headers["Location"])
        self.assertEqual(self.submit(alice).json(), first.json())
        self.assertEqual(self.submit(bob).json(), other.json())
        self.assertEqual([event["author"] for event in ticket_history(other.json()["id"], self.database)], ["bob"])
        self.assertEqual(len(list_tickets(self.database)), 2)

    def test_write_only_token_cannot_probe_another_tokens_key(self):
        support = self.bearer("support")
        original = self.submit(support, key="k1").json()
        self.submit(support, key="k2")
        intake = self.bearer("intake", ["tickets:write"])
        # Before keys were scoped, a right guess replayed the original ticket and a wrong one returned 409.
        right = self.submit(intake, key="k1")
        wrong = self.submit(intake, {**self.payload, "email": "sam@example.com"}, key="k2")
        self.assertEqual((right.status_code, wrong.status_code), (201, 201))
        self.assertNotEqual(right.json()["id"], original["id"])
        self.assertEqual((right.json()["email"], wrong.json()["email"]), (None, None))
        conflict = self.submit(intake, key="k2")
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("sam@example.com", conflict.text)

    def test_terminal_keys_are_separate_from_token_keys(self):
        local = create_ticket("Login", "Cannot sign in", self.database, idempotency_key="k1", email="pat@example.com")
        response = self.submit(self.bearer("alice"))
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()["id"], local["id"])
        self.assertEqual(create_ticket("Login", "Cannot sign in", self.database, idempotency_key="k1",
                                       email="pat@example.com"), local)

    def test_openapi_says_keys_belong_to_the_token(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/tickets"]["post"]
        parameter = next(item for item in operation["parameters"] if item["name"] == "idempotency-key")
        self.assertIn("each token has its own keys", parameter["description"])


if __name__ == "__main__":
    unittest.main()
