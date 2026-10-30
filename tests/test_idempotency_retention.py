from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend import cli
from ticket_backend.api import create_app
from ticket_backend.database import connect
from ticket_backend.submissions import prune_submission_keys
from ticket_backend.tickets import IdempotencyConflictError, create_ticket, get_ticket, list_tickets
from ticket_backend.tokens import create_token


class KeyRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def create(self, key, author=None, title="Login", message="Cannot sign in"):
        return create_ticket(title, message, self.database, idempotency_key=key, author=author)

    def age_key(self, key, days, author=None):
        with closing(connect(self.database)) as connection, connection:
            connection.execute(
                """UPDATE ticket_submission_keys SET created_at = datetime('now', ?)
                   WHERE owner = ? AND idempotency_key = ?""",
                (f"-{days} days", author or "", key),
            )

    def age_ticket(self, ticket_id, days):
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE tickets SET created_at = datetime('now', ?) WHERE id = ?",
                               (f"-{days} days", ticket_id))

    def keys(self):
        with closing(connect(self.database)) as connection:
            saved = connection.execute("SELECT owner, idempotency_key FROM ticket_submission_keys ORDER BY rowid")
            legacy = connection.execute("SELECT '(legacy)', idempotency_key FROM ticket_submissions ORDER BY rowid")
            return [tuple(row) for row in [*saved, *legacy]]

    def save_legacy_key(self, key, ticket):
        with closing(connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO ticket_submissions VALUES (?, ?, ?)",
                               (key, ticket["id"], json.dumps(ticket)))

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_pruning_deletes_only_older_keys_and_keeps_their_tickets(self):
        old, recent = self.create("old"), self.create("recent")
        self.age_key("old", 40)
        self.age_key("recent", 10)
        self.assertEqual(prune_submission_keys(30, self.database), {"deleted": 1})
        self.assertEqual(self.keys(), [("", "recent")])
        self.assertEqual(get_ticket(old["id"], self.database), old)
        self.assertEqual(self.create("recent"), recent)
        self.assertEqual(prune_submission_keys(30, self.database), {"deleted": 0})

    def test_a_pruned_key_creates_a_new_ticket_instead_of_conflicting(self):
        first = self.create("k1")
        self.age_key("k1", 40)
        prune_submission_keys(30, self.database)
        second = self.create("k1", title="Billing")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.create("k1", title="Billing"), second)
        with self.assertRaises(IdempotencyConflictError):
            self.create("k1")
        self.assertEqual(len(list_tickets(self.database)), 2)

    def test_every_callers_keys_are_pruned_by_the_same_age(self):
        for author in ("alice", "bob", None):
            self.create("k1", author)
        self.age_key("k1", 40, "alice")
        self.age_key("k1", 40)
        self.assertEqual(prune_submission_keys(30, self.database), {"deleted": 2})
        self.assertEqual(self.keys(), [("bob", "k1")])

    def test_keys_from_earlier_versions_are_aged_by_their_ticket(self):
        old, recent = self.create(None), self.create(None)
        self.save_legacy_key("legacy-old", old)
        self.save_legacy_key("legacy-recent", recent)
        self.age_ticket(old["id"], 40)
        self.assertEqual(prune_submission_keys(30, self.database), {"deleted": 1})
        self.assertEqual(self.keys(), [("(legacy)", "legacy-recent")])
        self.assertNotEqual(self.create("legacy-old")["id"], old["id"])
        self.assertEqual(self.create("legacy-recent"), recent)

    def test_invalid_ages_delete_nothing(self):
        self.create("k1")
        self.age_key("k1", 40)
        for days in (0, -1, 3651, "30", 1.5, True, None):
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, "Age must"):
                prune_submission_keys(days, self.database)
        self.assertEqual(self.keys(), [("", "k1")])

    def test_existing_databases_gain_the_index_and_keep_their_keys(self):
        first = self.create("k1")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP INDEX ticket_submission_keys_created_at")
        self.assertEqual(self.create("k1"), first)
        with closing(sqlite3.connect(self.database)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("ticket_submission_keys_created_at", names)
        self.assertEqual(prune_submission_keys(1, self.database), {"deleted": 0})

    def test_cli_prunes_as_text_and_json_and_rejects_invalid_input(self):
        for key in ("k1", "k2", "k3"):
            self.create(key)
            self.age_key(key, 40)
        self.assertEqual(self.cli("keys", "prune", "--older-than", "30")[:2], (0, "Deleted 3 idempotency keys.\n"))
        self.create("k4")
        self.age_key("k4", 40)
        self.assertEqual(self.cli("keys", "prune", "--older-than", "30")[:2], (0, "Deleted 1 idempotency key.\n"))
        code, output, _ = self.cli("--json", "keys", "prune", "--older-than", "30")
        self.assertEqual((code, json.loads(output)), (0, {"deleted": 0}))
        self.create("k5")
        self.age_key("k5", 40)
        code, _, errors = self.cli("keys", "prune", "--older-than", "3651")
        self.assertEqual((code, errors), (2, "Error: Age must be a whole number of days from 1 to 3650.\n"))
        for arguments in (["keys", "prune"], ["keys"], ["keys", "prune", "--older-than", "soon"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["--database", str(self.database), *arguments])
        self.assertEqual(self.keys(), [("", "k5")])

    def test_an_api_retry_after_pruning_creates_a_new_ticket(self):
        authorization = {"Authorization": f"Bearer {create_token('tests', self.database)['token']}"}
        payload, headers = {"title": "Login", "message": "Cannot sign in"}, {"Idempotency-Key": "submission-1"}
        with TestClient(create_app(self.database), headers=authorization) as client:
            first = client.post("/tickets", json=payload, headers=headers)
            self.assertEqual(client.post("/tickets", json=payload, headers=headers).json(), first.json())
            self.age_key("submission-1", 40, "tests")
            self.assertEqual(prune_submission_keys(30, self.database), {"deleted": 1})
            retry = client.post("/tickets", json=payload, headers=headers)
        self.assertEqual(retry.status_code, 201)
        self.assertNotEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(self.keys(), [("tests", "submission-1")])


if __name__ == "__main__":
    unittest.main()
