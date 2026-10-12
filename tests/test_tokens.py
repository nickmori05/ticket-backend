from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from db import connect
from functions import (
    TokenNotFoundError, add_comment, authenticate, create_ticket, create_token, get_ticket,
    list_comments, list_tokens, revoke_token,
)


class TokenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def test_created_tokens_authenticate_as_their_name(self):
        created = create_token("alice", self.database)
        self.assertEqual(created["name"], "alice")
        self.assertTrue(created["token"].startswith("tkt_"))
        self.assertIsNone(created["revoked_at"])
        self.assertEqual(authenticate(created["token"], self.database), "alice")

    def test_tokens_are_stored_only_as_hashes(self):
        token = create_token("alice", self.database)["token"]
        with closing(connect(self.database)) as connection:
            stored = connection.execute("SELECT token_hash FROM api_tokens").fetchone()[0]
        self.assertNotIn(token, stored)
        self.assertEqual(len(stored), 64)
        self.assertNotIn("token", list_tokens(self.database)[0])
        self.assertNotIn("token_hash", list_tokens(self.database)[0])

    def test_each_token_is_unique(self):
        first = create_token("alice", self.database)["token"]
        second = create_token("bob", self.database)["token"]
        self.assertNotEqual(first, second)
        self.assertEqual(authenticate(second, self.database), "bob")

    def test_unknown_blank_and_oversized_tokens_do_not_authenticate(self):
        token = create_token("alice", self.database)["token"]
        for candidate in (None, "", token + "x", token.upper(), "x" * 201, 5):
            with self.subTest(candidate=str(candidate)[:20]):
                self.assertIsNone(authenticate(candidate, self.database))

    def test_revoked_tokens_stop_authenticating(self):
        token = create_token("alice", self.database)["token"]
        revoked = revoke_token("alice", self.database)
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertIsNone(authenticate(token, self.database))
        self.assertEqual(revoke_token("alice", self.database), revoked)
        self.assertEqual(list_tokens(self.database), [revoked])

    def test_names_are_validated_and_never_reused(self):
        for name in ("", " ", "two words", "x" * 65, "name;", None):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    create_token(name, self.database)
        create_token("alice", self.database)
        revoke_token("alice", self.database)
        with self.assertRaisesRegex(ValueError, "already exists"):
            create_token("alice", self.database)
        self.assertEqual(len(list_tokens(self.database)), 1)

    def test_revoking_a_missing_token_raises_not_found(self):
        with self.assertRaises(TokenNotFoundError):
            revoke_token("nobody", self.database)

    def test_comments_record_an_optional_author(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database)
        attributed = add_comment(ticket["id"], "From the API", self.database, author="alice")
        local = add_comment(ticket["id"], "From the terminal", self.database)
        self.assertEqual(attributed["author"], "alice")
        self.assertIsNone(local["author"])
        with self.assertRaises(ValueError):
            add_comment(ticket["id"], "Body", self.database, author=" ")

    def test_existing_comment_tables_gain_an_author_column(self):
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
                CREATE TABLE ticket_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    body TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tickets (title, message) VALUES ('Old', 'Before tokens');
                INSERT INTO ticket_comments (ticket_id, body) VALUES (1, 'Old comment');
            """)
        add_comment(1, "New comment", legacy, author="alice")
        add_comment(1, "Another", legacy)
        self.assertEqual(get_ticket(1, legacy)["title"], "Old")
        self.assertEqual([(row["body"], row["author"]) for row in list_comments(1, legacy)],
                         [("Old comment", None), ("New comment", "alice"), ("Another", None)])
        self.assertEqual(authenticate(create_token("alice", legacy)["token"], legacy), "alice")


if __name__ == "__main__":
    unittest.main()
