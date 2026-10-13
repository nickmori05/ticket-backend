from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from api import create_app
from db import connect
from functions import create_ticket, create_token, get_ticket, revoke_token, set_status, ticket_history
import tickets


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


class StatusAttributionApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.alice = {"Authorization": f"Bearer {create_token('alice', self.database)['token']}"}
        self.bob = {"Authorization": f"Bearer {create_token('bob', self.database)['token']}"}
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)

    def submit(self, headers, key=None):
        if key is not None:
            headers = {**headers, "Idempotency-Key": key}
        return self.client.post("/tickets", json={"title": "Login", "message": "Cannot sign in"}, headers=headers)

    def test_creation_and_status_changes_are_attributed_to_the_token_name(self):
        identifier = self.submit(self.alice).json()["id"]
        url = f"/tickets/{identifier}/status"
        self.assertEqual(self.client.patch(url, json={"status": "in_progress"}, headers=self.bob).status_code, 200)
        self.client.patch(url, json={"status": "closed", "note": "Fixed"}, headers=self.alice)
        set_status(identifier, "open", self.database, note="Reopened locally")
        events = self.client.get(f"/tickets/{identifier}/history", headers=self.bob).json()
        self.assertEqual([(event["to_status"], event["author"]) for event in events],
                         [("open", "alice"), ("in_progress", "bob"), ("closed", "alice"), ("open", None)])
        self.assertEqual(events, ticket_history(identifier, self.database))

    def test_author_cannot_be_supplied_in_the_status_body(self):
        identifier = self.submit(self.alice).json()["id"]
        response = self.client.patch(f"/tickets/{identifier}/status", headers=self.alice,
                                     json={"status": "closed", "author": "bob"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(ticket_history(identifier, self.database)), 1)

    def test_revoked_tokens_cannot_change_status(self):
        identifier = self.submit(self.alice).json()["id"]
        revoke_token("bob", self.database)
        response = self.client.patch(f"/tickets/{identifier}/status", json={"status": "closed"}, headers=self.bob)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(get_ticket(identifier, self.database)["status"], "open")
        self.assertEqual([event["author"] for event in ticket_history(identifier, self.database)], ["alice"])

    def test_replay_by_another_token_keeps_the_original_author(self):
        first = self.submit(self.alice, "login-1")
        replayed = self.submit(self.bob, "login-1")
        self.assertEqual(replayed.status_code, 201)
        self.assertEqual(replayed.json(), first.json())
        self.assertEqual([event["author"] for event in ticket_history(first.json()["id"], self.database)], ["alice"])

    def test_openapi_documents_the_event_author(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("author", schema["components"]["schemas"]["EventOutput"]["properties"])
        self.assertNotIn("author", schema["components"]["schemas"]["StatusInput"]["properties"])


class StatusAttributionCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        options = ["--json"] if json_mode else []
        with redirect_stdout(output), redirect_stderr(errors):
            code = tickets.main(["--database", str(self.database), *options, *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_history_shows_the_author_or_local(self):
        identifier = create_ticket("Login", "Cannot sign in", self.database, author="alice")["id"]
        self.assertEqual(self.run_command("status", str(identifier), "closed", "--note", "Fixed")[0], 0)
        code, output, errors = self.run_command("history", str(identifier))
        self.assertEqual((code, errors), (0, ""))
        lines = output.splitlines()
        self.assertTrue(lines[0].endswith(" UTC, alice: created -> open"))
        self.assertTrue(lines[1].endswith(" UTC, local: open -> closed"))
        self.assertEqual(lines[2], "  Fixed")

    def test_json_history_includes_a_null_author_for_terminal_changes(self):
        self.run_command("create", "--title", "Login", "--message", "Broken", json_mode=True)
        self.run_command("status", "1", "closed", json_mode=True)
        events = json.loads(self.run_command("history", "1", json_mode=True)[1])
        self.assertEqual([event["author"] for event in events], [None, None])


if __name__ == "__main__":
    unittest.main()
