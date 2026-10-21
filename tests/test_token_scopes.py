from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.database import connect
from ticket_backend.tokens import (
    SCOPES, authenticate, create_token, list_tokens, revoke_token, token_access,
)


def expire(database, name):
    with closing(connect(database)) as connection, connection:
        connection.execute("UPDATE api_tokens SET expires_at = datetime('now', '-1 second') WHERE name = ?", (name,))


class TokenScopeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def test_tokens_default_to_every_scope_and_no_expiry(self):
        created = create_token("alice", self.database)
        self.assertEqual(created["scopes"], list(SCOPES))
        self.assertIsNone(created["expires_at"])
        self.assertEqual(created["state"], "active")
        self.assertEqual(token_access(created["token"], self.database), {"name": "alice", "scopes": list(SCOPES)})

    def test_scopes_are_deduplicated_in_a_fixed_order(self):
        created = create_token("triage", self.database, scopes=["emails:read", "tickets:read", "emails:read"])
        self.assertEqual(created["scopes"], ["tickets:read", "emails:read"])
        self.assertEqual(token_access(created["token"], self.database)["scopes"], ["tickets:read", "emails:read"])
        self.assertEqual(list_tokens(self.database)[0]["scopes"], ["tickets:read", "emails:read"])

    def test_invalid_scopes_create_nothing(self):
        for scopes in ([], ["admin"], ["tickets:read", "tickets:delete"], "tickets:read", [["tickets:read"]], 5):
            with self.subTest(scopes=scopes), self.assertRaisesRegex(ValueError, "Scopes must"):
                create_token("alice", self.database, scopes=scopes)
        self.assertEqual(list_tokens(self.database), [])

    def test_invalid_expiries_create_nothing(self):
        for days in (0, -1, 3651, "30", 1.5, True):
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, "Expiry must"):
                create_token("alice", self.database, expires_in_days=days)
        self.assertEqual(list_tokens(self.database), [])

    def test_expiring_tokens_stop_authenticating_once_expired(self):
        created = create_token("contractor", self.database, expires_in_days=30)
        expires = datetime.strptime(created["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        expected = datetime.now(timezone.utc) + timedelta(days=30)
        self.assertLess(abs(expires - expected), timedelta(minutes=1))
        self.assertEqual(authenticate(created["token"], self.database), "contractor")
        expire(self.database, "contractor")
        self.assertIsNone(token_access(created["token"], self.database))
        self.assertIsNone(authenticate(created["token"], self.database))
        self.assertEqual(list_tokens(self.database)[0]["state"], "expired")
        self.assertEqual(revoke_token("contractor", self.database)["state"], "revoked")

    def test_tokens_from_before_scopes_keep_full_access(self):
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
        self.assertEqual(token_access(token, legacy), {"name": "old", "scopes": list(SCOPES)})
        listed = list_tokens(legacy)[0]
        self.assertEqual((listed["scopes"], listed["expires_at"], listed["state"]), (list(SCOPES), None, "active"))
        reader = create_token("reader", legacy, scopes=["tickets:read"], expires_in_days=1)
        self.assertEqual(token_access(reader["token"], legacy)["scopes"], ["tickets:read"])


class TokenScopeCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *(["--json"] if json_mode else []), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_create_with_scopes_and_expiry(self):
        code, output, _errors = self.run_command("token", "create", "triage", "--scope", "tickets:read",
                                                 "--scope", "emails:read", "--expires-in", "7", json_mode=True)
        created = json.loads(output)
        self.assertEqual((code, created["scopes"]), (0, ["tickets:read", "emails:read"]))
        self.assertIsNotNone(created["expires_at"])
        self.assertEqual(token_access(created["token"], self.database)["name"], "triage")
        code, output, _errors = self.run_command("token", "create", "reader", "--scope", "tickets:read")
        self.assertIn("Scopes: tickets:read\n", output)
        self.assertNotIn("Expires:", output)

    def test_list_shows_scopes_and_state(self):
        self.run_command("token", "create", "alice")
        self.run_command("token", "create", "temp", "--scope", "tickets:read", "--expires-in", "1")
        self.run_command("token", "create", "gone", "--scope", "tickets:write")
        expire(self.database, "temp")
        revoke_token("gone", self.database)
        lines = self.run_command("token", "list")[1].splitlines()
        self.assertTrue(lines[0].startswith("alice [active] tickets:read tickets:write emails:read (created "))
        self.assertRegex(lines[1], r"^temp \[expired\] tickets:read \(created .* UTC, expired .* UTC\)$")
        self.assertRegex(lines[2], r"^gone \[revoked\] tickets:write \(created .* UTC, revoked .* UTC\)$")

    def test_invalid_expiry_and_scope_are_rejected(self):
        code, output, errors = self.run_command("token", "create", "alice", "--expires-in", "0")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Expiry must", errors)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["--database", str(self.database), "token", "create", "alice", "--scope", "admin"])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(list_tokens(self.database), [])


if __name__ == "__main__":
    unittest.main()
