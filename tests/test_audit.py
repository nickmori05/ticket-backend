from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.audit import list_audit, prune_audit, record_request
from ticket_backend.database import connect


def age(database, identifier, days):
    with closing(connect(database)) as connection, connection:
        connection.execute("UPDATE api_audit_log SET created_at = datetime('now', ?) WHERE id = ?",
                           (f"-{days} days", identifier))


class AuditStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        record_request(self.database, "alice", "POST", "/tickets", "/tickets", 201, ticket_id=1, client="10.0.0.5")
        record_request(self.database, "bob", "GET", "/tickets/1", "/tickets/{ticket_id}", 200, ticket_id=1)
        record_request(self.database, "alice", "GET", "/stats", "/stats", 403)

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_requests_are_listed_newest_first_with_every_field(self):
        rows = list_audit(self.database)
        self.assertEqual([row["id"] for row in rows], [3, 2, 1])
        first = rows[-1]
        self.assertEqual({key: first[key] for key in first if key != "created_at"},
                         {"id": 1, "token": "alice", "method": "POST", "path": "/tickets", "route": "/tickets",
                          "ticket_id": 1, "status": 201, "client": "10.0.0.5"})
        self.assertRegex(first["created_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_listing_filters_by_token_and_ticket_and_pages(self):
        self.assertEqual([row["id"] for row in list_audit(self.database, token="alice")], [3, 1])
        self.assertEqual([row["id"] for row in list_audit(self.database, ticket_id=1)], [2, 1])
        self.assertEqual([row["id"] for row in list_audit(self.database, token="alice", ticket_id=1)], [1])
        self.assertEqual([row["id"] for row in list_audit(self.database, 1, offset=1)], [2])
        self.assertEqual(list_audit(self.database, token="carol"), [])

    def test_invalid_filters_are_rejected(self):
        for arguments in ({"token": ""}, {"token": "a b"}, {"token": "x" * 65}, {"token": 5}, {"ticket_id": 0},
                          {"ticket_id": "1"}, {"limit": 0}, {"limit": 101}, {"offset": -1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                list_audit(self.database, **arguments)

    def test_long_paths_and_clients_are_truncated(self):
        record_request(self.database, "alice", "GET", "/" + "x" * 600, None, 404, client="c" * 200)
        row = list_audit(self.database, 1)[0]
        self.assertEqual((len(row["path"]), len(row["client"]), row["route"]), (500, 100, None))

    def test_pruning_deletes_only_older_requests(self):
        age(self.database, 1, 40)
        age(self.database, 2, 10)
        self.assertEqual(prune_audit(30, self.database), {"deleted": 1})
        self.assertEqual([row["id"] for row in list_audit(self.database)], [3, 2])
        self.assertEqual(prune_audit(30, self.database), {"deleted": 0})
        self.assertEqual(prune_audit(5, self.database), {"deleted": 1})

    def test_invalid_ages_delete_nothing(self):
        age(self.database, 1, 40)
        for days in (0, -1, 3651, "30", 1.5, True, None):
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, "Age must"):
                prune_audit(days, self.database)
        self.assertEqual(len(list_audit(self.database)), 3)

    def test_existing_databases_gain_the_audit_log(self):
        def objects():
            with closing(sqlite3.connect(self.database)) as connection:
                return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TABLE api_audit_log")
            connection.execute("INSERT INTO tickets (title, message) VALUES ('Kept', 'From before the audit log')")
        self.assertFalse({"api_audit_log", "api_audit_log_token"} & objects())
        self.assertEqual(list_audit(self.database), [])
        self.assertTrue({"api_audit_log", "api_audit_log_token", "api_audit_log_ticket_id"} <= objects())
        record_request(self.database, "alice", "GET", "/stats", "/stats", 200)
        self.assertEqual(len(list_audit(self.database)), 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT title FROM tickets").fetchall(), [("Kept",)])

    def test_cli_lists_requests_as_text_and_json(self):
        code, output, _ = self.cli("audit", "list", "--token", "alice")
        self.assertEqual(code, 0)
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\S+ \S+ UTC, alice: GET /stats -> 403$")
        self.assertTrue(lines[1].endswith("alice: POST /tickets -> 201 (ticket #1)"))
        code, output, _ = self.cli("--json", "audit", "list", "--ticket", "1", "--limit", "1")
        self.assertEqual((code, [row["id"] for row in json.loads(output)]), (0, [2]))
        self.assertEqual(self.cli("audit", "list", "--token", "nobody")[:2], (0, "No recorded API requests.\n"))

    def test_cli_prunes_and_rejects_invalid_input(self):
        age(self.database, 1, 40)
        self.assertEqual(self.cli("audit", "prune", "--older-than", "30")[:2], (0, "Deleted 1 recorded API request.\n"))
        code, output, _ = self.cli("--json", "audit", "prune", "--older-than", "30")
        self.assertEqual((code, json.loads(output)), (0, {"deleted": 0}))
        code, _, errors = self.cli("audit", "prune", "--older-than", "0")
        self.assertEqual((code, errors), (2, "Error: Age must be a whole number of days from 1 to 3650.\n"))
        self.assertEqual(self.cli("audit", "list", "--ticket", "0")[0], 2)
        self.assertEqual(self.cli("audit", "list", "--token", "bad name")[0], 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--database", str(self.database), "audit", "prune"])
        self.assertEqual(len(list_audit(self.database)), 2)


if __name__ == "__main__":
    unittest.main()
