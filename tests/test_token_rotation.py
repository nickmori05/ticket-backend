from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend import cli
from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.database import connect
from ticket_backend.tickets import create_ticket, ticket_history
from ticket_backend.tokens import (
    LEGACY_SCOPES, TokenNotFoundError, authenticate, create_token, list_tokens, revoke_token, rotate_token,
    token_access,
)


def expire(database, name):
    with closing(connect(database)) as connection, connection:
        connection.execute("UPDATE api_tokens SET expires_at = datetime('now', '-1 second') WHERE name = ?", (name,))


def stored_hash(database, name):
    with closing(connect(database)) as connection:
        return connection.execute("SELECT token_hash FROM api_tokens WHERE name = ?", (name,)).fetchone()[0]


def end_grace(database, name):
    with closing(connect(database)) as connection, connection:
        connection.execute("UPDATE api_tokens SET previous_expires_at = datetime('now', '-1 second') WHERE name = ?",
                           (name,))


def utc(timestamp):
    return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


class TokenRotationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def test_rotation_replaces_the_secret_and_keeps_everything_else(self):
        created = create_token("form", self.database, scopes=["tickets:write"], expires_in_days=30, rate_limit=10)
        rotated = rotate_token("form", self.database)
        self.assertTrue(rotated["token"].startswith("tkt_"))
        self.assertNotEqual(rotated["token"], created["token"])
        self.assertIsNone(authenticate(created["token"], self.database))
        self.assertEqual(token_access(rotated["token"], self.database),
                         {"name": "form", "scopes": ["tickets:write"], "rate_limit": 10})
        for field in ("id", "name", "scopes", "rate_limit", "created_at", "expires_at", "revoked_at", "state"):
            with self.subTest(field=field):
                self.assertEqual(rotated[field], created[field])
        self.assertIsNone(created["rotated_at"])
        self.assertLess(abs(utc(rotated["rotated_at"]) - datetime.now(timezone.utc)), timedelta(minutes=1))
        self.assertEqual(list_tokens(self.database), [{k: v for k, v in rotated.items() if k != "token"}])

    def test_only_the_hash_of_the_new_secret_is_stored(self):
        rotated = rotate_token(create_token("alice", self.database)["name"], self.database)
        self.assertEqual(stored_hash(self.database, "alice"), hashlib.sha256(rotated["token"].encode()).hexdigest())

    def test_rotating_again_retires_each_earlier_secret(self):
        secrets = [create_token("alice", self.database)["token"]]
        secrets += [rotate_token("alice", self.database)["token"] for _ in range(3)]
        self.assertEqual(len(set(secrets)), 4)
        self.assertEqual([authenticate(secret, self.database) for secret in secrets], [None, None, None, "alice"])

    def test_other_tokens_are_untouched(self):
        bob = create_token("bob", self.database)["token"]
        create_token("alice", self.database)
        rotate_token("alice", self.database)
        self.assertEqual(authenticate(bob, self.database), "bob")
        self.assertIsNone(list_tokens(self.database)[0]["rotated_at"])

    def test_a_new_expiry_can_be_set_and_revives_an_expired_token(self):
        create_token("contractor", self.database, expires_in_days=1)
        kept = rotate_token("contractor", self.database)
        self.assertLess(abs(utc(kept["expires_at"]) - datetime.now(timezone.utc) - timedelta(days=1)),
                        timedelta(minutes=1))
        expire(self.database, "contractor")
        with self.assertRaisesRegex(ValueError, "has expired; rotate it with a new expiry"):
            rotate_token("contractor", self.database)
        self.assertIsNone(authenticate(kept["token"], self.database))
        renewed = rotate_token("contractor", self.database, expires_in_days=90)
        self.assertEqual(renewed["state"], "active")
        self.assertLess(abs(utc(renewed["expires_at"]) - datetime.now(timezone.utc) - timedelta(days=90)),
                        timedelta(minutes=1))
        self.assertEqual(authenticate(renewed["token"], self.database), "contractor")
        never = rotate_token(create_token("service", self.database)["name"], self.database, expires_in_days=7)
        self.assertIsNotNone(never["expires_at"])

    def test_revoked_missing_and_invalid_rotations_change_nothing(self):
        token = create_token("alice", self.database)["token"]
        revoke_token("alice", self.database)
        before = stored_hash(self.database, "alice")
        with self.assertRaisesRegex(ValueError, "revoked and cannot be rotated"):
            rotate_token("alice", self.database)
        with self.assertRaisesRegex(ValueError, "revoked"):
            rotate_token("alice", self.database, expires_in_days=30)
        self.assertEqual(stored_hash(self.database, "alice"), before)
        self.assertIsNone(authenticate(token, self.database))
        with self.assertRaises(TokenNotFoundError):
            rotate_token("nobody", self.database)
        create_token("bob", self.database)
        before = stored_hash(self.database, "bob")
        for name, days in (("bad name", None), ("bob", 0), ("bob", 3651), ("bob", "7"), ("bob", True)):
            with self.subTest(name=name, days=days), self.assertRaises(ValueError):
                rotate_token(name, self.database, expires_in_days=days)
        self.assertEqual(stored_hash(self.database, "bob"), before)
        self.assertIsNone(list_tokens(self.database)[1]["rotated_at"])

    def test_tokens_from_older_databases_can_be_rotated(self):
        legacy = self.database.with_name("legacy.db")
        token = "tkt_legacy-token"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(f"""
                CREATE TABLE api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    revoked_at TIMESTAMP
                );
                INSERT INTO api_tokens (name, token_hash)
                VALUES ('old', '{hashlib.sha256(token.encode()).hexdigest()}');
            """)
        self.assertIsNone(list_tokens(legacy)[0]["rotated_at"])
        rotated = rotate_token("old", legacy)
        self.assertIsNone(token_access(token, legacy))
        self.assertEqual(token_access(rotated["token"], legacy),
                         {"name": "old", "scopes": list(LEGACY_SCOPES), "rate_limit": None})


class RotationGracePeriodTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def test_both_secrets_work_until_the_grace_period_ends(self):
        old = create_token("form", self.database, scopes=["tickets:write"])["token"]
        rotated = rotate_token("form", self.database, grace_minutes=60)
        self.assertLess(abs(utc(rotated["previous_valid_until"]) - datetime.now(timezone.utc) - timedelta(hours=1)),
                        timedelta(minutes=1))
        for secret in (old, rotated["token"]):
            self.assertEqual(token_access(secret, self.database),
                             {"name": "form", "scopes": ["tickets:write"], "rate_limit": None})
        end_grace(self.database, "form")
        self.assertIsNone(authenticate(old, self.database))
        self.assertEqual(authenticate(rotated["token"], self.database), "form")
        self.assertIsNone(list_tokens(self.database)[0]["previous_valid_until"])

    def test_without_a_grace_period_nothing_is_kept(self):
        create_token("alice", self.database)
        self.assertIsNone(rotate_token("alice", self.database)["previous_valid_until"])
        with closing(connect(self.database)) as connection:
            kept = connection.execute("SELECT previous_token_hash, previous_expires_at FROM api_tokens").fetchone()
        self.assertEqual(tuple(kept), (None, None))

    def test_rotating_again_retires_the_older_secret_at_once(self):
        first = create_token("alice", self.database)["token"]
        second = rotate_token("alice", self.database, grace_minutes=60)["token"]
        third = rotate_token("alice", self.database, grace_minutes=60)["token"]
        self.assertEqual([authenticate(secret, self.database) for secret in (first, second, third)],
                         [None, "alice", "alice"])
        fourth = rotate_token("alice", self.database)["token"]
        self.assertEqual([authenticate(secret, self.database) for secret in (second, third, fourth)],
                         [None, None, "alice"])

    def test_revoking_or_expiring_the_token_refuses_both_secrets(self):
        old = create_token("alice", self.database, expires_in_days=1)["token"]
        new = rotate_token("alice", self.database, grace_minutes=60)["token"]
        expire(self.database, "alice")
        self.assertEqual([authenticate(old, self.database), authenticate(new, self.database)], [None, None])
        self.assertEqual(list_tokens(self.database)[0]["state"], "expired")
        old = create_token("bob", self.database)["token"]
        new = rotate_token("bob", self.database, grace_minutes=60)["token"]
        revoke_token("bob", self.database)
        self.assertEqual([authenticate(old, self.database), authenticate(new, self.database)], [None, None])

    def test_renewing_an_expired_token_does_not_bring_back_its_old_secret(self):
        old = create_token("contractor", self.database, expires_in_days=1)["token"]
        expire(self.database, "contractor")
        renewed = rotate_token("contractor", self.database, expires_in_days=30, grace_minutes=60)
        self.assertIsNone(renewed["previous_valid_until"])
        self.assertIsNone(authenticate(old, self.database))
        self.assertEqual(authenticate(renewed["token"], self.database), "contractor")

    def test_invalid_grace_periods_change_nothing(self):
        old = create_token("alice", self.database)["token"]
        for minutes in (-1, 10_081, "60", 1.5, None, True):
            with self.subTest(minutes=minutes), self.assertRaisesRegex(ValueError, "Grace period must"):
                rotate_token("alice", self.database, grace_minutes=minutes)
        self.assertEqual(authenticate(old, self.database), "alice")
        self.assertEqual(rotate_token("alice", self.database, grace_minutes=10_080)["state"], "active")

    def test_older_databases_gain_the_grace_columns_and_index(self):
        legacy = self.database.with_name("legacy.db")
        token = "tkt_legacy-token"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(f"""
                CREATE TABLE api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    revoked_at TIMESTAMP
                );
                INSERT INTO api_tokens (name, token_hash)
                VALUES ('old', '{hashlib.sha256(token.encode()).hexdigest()}');
            """)
        new = rotate_token("old", legacy, grace_minutes=5)["token"]
        self.assertEqual([authenticate(token, legacy), authenticate(new, legacy)], ["old", "old"])
        with closing(sqlite3.connect(legacy)) as connection:
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(api_tokens)")}
        self.assertIn("api_tokens_previous_token_hash", indexes)


class TokenRotationApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def client(self, **options):
        client = TestClient(create_app(self.database, **options))
        self.addCleanup(client.close)
        return client

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    def test_the_old_secret_is_refused_and_the_new_one_keeps_the_tokens_history(self):
        client = self.client()
        old = create_token("form", self.database)["token"]
        payload = {"title": "Login", "message": "Cannot sign in"}
        first = client.post("/tickets", json=payload, headers={**self.bearer(old), "Idempotency-Key": "k1"})
        self.assertEqual(first.status_code, 201)
        new = rotate_token("form", self.database)["token"]
        self.assertEqual(client.get("/stats", headers=self.bearer(old)).status_code, 401)
        replayed = client.post("/tickets", json=payload, headers={**self.bearer(new), "Idempotency-Key": "k1"})
        self.assertEqual((replayed.status_code, replayed.json()), (201, first.json()))
        conflict = client.post("/tickets", json={**payload, "message": "Other"},
                               headers={**self.bearer(new), "Idempotency-Key": "k1"})
        self.assertEqual(conflict.status_code, 409)
        ticket_id = first.json()["id"]
        client.patch(f"/tickets/{ticket_id}/status", json={"status": "closed"}, headers=self.bearer(new))
        self.assertEqual([event["author"] for event in ticket_history(ticket_id, self.database)], ["form", "form"])
        self.assertEqual([row["status"] for row in list_audit(self.database, token="form")], [200, 409, 201, 201])

    def test_rotation_does_not_reset_the_rate_limit(self):
        client = self.client(rate_limit=2)
        old = create_token("alice", self.database)["token"]
        self.assertEqual([client.get("/stats", headers=self.bearer(old)).status_code for _ in range(2)], [200, 200])
        new = rotate_token("alice", self.database)["token"]
        with self.assertLogs("ticket_backend.api", level="WARNING"):
            self.assertEqual(client.get("/stats", headers=self.bearer(new)).status_code, 429)

    def test_during_the_grace_period_both_secrets_act_as_one_token(self):
        client = self.client(rate_limit=3)
        old = create_token("alice", self.database)["token"]
        new = rotate_token("alice", self.database, grace_minutes=10)["token"]
        codes = [client.get("/stats", headers=self.bearer(secret)).status_code for secret in (old, new, old)]
        self.assertEqual(codes, [200, 200, 200])
        with self.assertLogs("ticket_backend.api", level="WARNING"):
            self.assertEqual(client.get("/stats", headers=self.bearer(new)).status_code, 429)
        self.assertEqual([row["token"] for row in list_audit(self.database)], ["alice"] * 3)
        end_grace(self.database, "alice")
        self.assertEqual(client.get("/stats", headers=self.bearer(old)).status_code, 401)

    def test_rotation_never_widens_access(self):
        client = self.client()
        create_token("reader", self.database, scopes=["tickets:read"])
        create_ticket("Login", "Cannot sign in", self.database, email="pat@example.com")
        new = rotate_token("reader", self.database)["token"]
        self.assertIsNone(client.get("/tickets/1", headers=self.bearer(new)).json()["email"])
        self.assertEqual(client.post("/tickets", json={"title": "A", "message": "B"},
                                     headers=self.bearer(new)).status_code, 403)


class TokenRotationCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *(["--json"] if json_mode else []), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_rotate_prints_the_new_token_once(self):
        old = create_token("alice", self.database, scopes=["tickets:read"], rate_limit=5)["token"]
        code, output, errors = self.run_command("token", "rotate", "alice")
        self.assertEqual((code, errors), (0, ""))
        self.assertRegex(output, r"^New token for alice: tkt_\S+\nScopes: tickets:read\n"
                                 r"Rate limit: 5 requests per minute\nThe previous token no longer works\.\n"
                                 r"Store it now; it cannot be shown again\.\n$")
        new = output.split()[4]
        self.assertEqual((authenticate(old, self.database), authenticate(new, self.database)), (None, "alice"))
        listed = self.run_command("token", "list")[1]
        self.assertRegex(listed, r"^alice \[active\] tickets:read \(created .* UTC, rotated .* UTC, 5 requests")
        self.assertNotIn("tkt_", listed)

    def test_rotate_in_json_with_a_new_expiry(self):
        create_token("alice", self.database, expires_in_days=1)
        expire(self.database, "alice")
        code, output, _errors = self.run_command("token", "rotate", "alice", "--expires-in", "30", json_mode=True)
        rotated = json.loads(output)
        self.assertEqual((code, rotated["state"]), (0, "active"))
        self.assertEqual(authenticate(rotated["token"], self.database), "alice")
        self.assertIsNotNone(rotated["rotated_at"])

    def test_rotate_with_a_grace_period(self):
        old = create_token("alice", self.database)["token"]
        code, output, _errors = self.run_command("token", "rotate", "alice", "--grace-period", "30")
        self.assertEqual(code, 0)
        self.assertRegex(output, r"\nThe previous token works until \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC\.\n")
        self.assertEqual(authenticate(old, self.database), "alice")
        self.assertRegex(self.run_command("token", "list")[1], r"rotated .* UTC, previous token until .* UTC\)$")
        rotated = json.loads(self.run_command("token", "rotate", "alice", json_mode=True)[1])
        self.assertIsNone(rotated["previous_valid_until"])
        self.assertNotIn("previous token", self.run_command("token", "list")[1])
        code, output, errors = self.run_command("token", "rotate", "alice", "--grace-period", "10081")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Grace period must", errors)

    def test_errors_use_existing_exit_codes(self):
        create_token("alice", self.database)
        revoke_token("alice", self.database)
        create_token("bob", self.database)
        for arguments, code, expected in ((("token", "rotate", "nobody"), 1, "not found"),
                                          (("token", "rotate", "alice"), 2, "revoked"),
                                          (("token", "rotate", "bob", "--expires-in", "0"), 2, "Expiry must")):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertEqual((result[0], result[1]), (code, ""))
                self.assertIn(expected, result[2])
        self.assertIsNone(list_tokens(self.database)[1]["rotated_at"])


if __name__ == "__main__":
    unittest.main()
