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
from ticket_backend.audit import list_audit, record_request
from ticket_backend.database import connect
from ticket_backend.tokens import create_token
from ticket_backend.validation import optional_timestamp


def at(database, identifier, moment):
    with closing(connect(database)) as connection, connection:
        connection.execute("UPDATE api_audit_log SET created_at = ? WHERE id = ?", (moment, identifier))


def ids(rows):
    return [row["id"] for row in rows]


class TimestampTests(unittest.TestCase):
    def test_dates_and_times_are_returned_in_utc_as_sqlite_stores_them(self):
        for value, expected in (
            ("2026-10-31", "2026-10-31 00:00:00"),
            ("2026-10-31T14:05", "2026-10-31 14:05:00"),
            ("2026-10-31 14:05:09", "2026-10-31 14:05:09"),
            ("2026-10-31T14:05:09Z", "2026-10-31 14:05:09"),
            ("2026-10-31T01:00:00+02:00", "2026-10-30 23:00:00"),
            ("2026-10-31T23:30-01:00", "2026-11-01 00:30:00"),
            ("  2026-10-31  ", "2026-10-31 00:00:00"),
            ("0001-01-01", "0001-01-01 00:00:00"),
        ):
            with self.subTest(value=value):
                self.assertEqual(optional_timestamp(value, "Since"), expected)
        self.assertIsNone(optional_timestamp(None, "Since"))

    def test_other_forms_and_impossible_dates_are_rejected(self):
        for value in ("", "yesterday", "20261031", "2026-10-31T14", "2026-10-31T14:00:00.5", "2026-10-31T14:00:00 Z",
                      "2026-10-31T14:00+0200", "31/10/2026", "2026-1-31", 20261031):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Until must be a date or a time"):
                optional_timestamp(value, "Until")
        for value in ("2026-02-30", "2026-13-01", "2026-10-31T25:00", "0001-01-01T00:00+01:00"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Until is not a valid date or time"):
                optional_timestamp(value, "Until")


class AuditFilterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        for token, path, status, moment in (
            ("alice", "/tickets", 201, "2026-10-01 09:00:00"),
            ("alice", "/stats", 403, "2026-10-02 09:00:00"),
            ("bob", "/tickets/9", 404, "2026-10-03 09:00:00"),
            ("bob", "/stats", 200, "2026-10-03 18:30:00"),
            ("alice", "/stats", 503, "2026-10-04 00:00:00"),
        ):
            record_request(self.database, token, "GET", path, path, status)
            at(self.database, len(list_audit(self.database, 100)), moment)

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_status_matches_one_code_or_a_class(self):
        for status, expected in ((403, [2]), ("403", [2]), ("4xx", [3, 2]), ("2xx", [4, 1]), ("5xx", [5]),
                                 ("500", []), ("1xx", [])):
            with self.subTest(status=status):
                self.assertEqual(ids(list_audit(self.database, status=status)), expected)

    def test_window_includes_since_and_excludes_until(self):
        for window, expected in (
            ({"since": "2026-10-03"}, [5, 4, 3]),
            ({"until": "2026-10-03"}, [2, 1]),
            ({"since": "2026-10-02T09:00:00", "until": "2026-10-03T18:30:00"}, [3, 2]),
            ({"since": "2026-10-03T18:30:00Z"}, [5, 4]),
            # 20:00 at UTC-1 is 21:00 UTC on the 3rd, so only the request at midnight on the 4th is after it.
            ({"since": "2026-10-03T20:00:00-01:00"}, [5]),
            ({"since": "2026-10-05"}, []),
        ):
            with self.subTest(window=window):
                self.assertEqual(ids(list_audit(self.database, **window)), expected)

    def test_filters_combine_with_each_other_and_with_paging(self):
        self.assertEqual(ids(list_audit(self.database, token="alice", status="4xx")), [2])
        self.assertEqual(ids(list_audit(self.database, token="bob", since="2026-10-03T12:00")), [4])
        self.assertEqual(ids(list_audit(self.database, 1, offset=1, since="2026-10-02", until="2026-10-04")), [3])

    def test_invalid_filters_are_rejected(self):
        for arguments, message in (
            ({"status": 99}, "Status must"), ({"status": 600}, "Status must"), ({"status": "6xx"}, "Status must"),
            ({"status": "4XX"}, "Status must"), ({"status": "40x"}, "Status must"), ({"status": True}, "Status must"),
            ({"status": 403.0}, "Status must"), ({"since": "soon"}, "Since must"), ({"until": "2026-02-30"}, "Until"),
            ({"since": "2026-10-03", "until": "2026-10-03"}, "Since must be earlier than until."),
            ({"since": "2026-10-04", "until": "2026-10-03"}, "Since must be earlier than until."),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, message):
                list_audit(self.database, **arguments)

    def test_existing_databases_gain_the_time_index(self):
        def indexes():
            with closing(sqlite3.connect(self.database)) as connection:
                return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP INDEX api_audit_log_created_at")
        self.assertNotIn("api_audit_log_created_at", indexes())
        self.assertEqual(ids(list_audit(self.database, since="2026-10-04")), [5])
        self.assertIn("api_audit_log_created_at", indexes())
        with closing(connect(self.database)) as connection:
            plan = " ".join(row["detail"] for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM api_audit_log WHERE created_at >= ? AND created_at < ?", ("a", "b")))
        self.assertIn("api_audit_log_created_at", plan)

    def test_cli_filters_by_status_and_time(self):
        code, output, _ = self.cli("audit", "list", "--status", "4xx", "--since", "2026-10-02")
        self.assertEqual((code, output), (0, "2026-10-03 09:00:00 UTC, bob: GET /tickets/9 -> 404\n"
                                             "2026-10-02 09:00:00 UTC, alice: GET /stats -> 403\n"))
        code, output, _ = self.cli("--json", "audit", "list", "--token", "alice", "--until", "2026-10-02T09:00:00Z")
        self.assertEqual((code, ids(json.loads(output))), (0, [1]))
        self.assertEqual(self.cli("audit", "list", "--status", "2xx", "--since", "2026-10-04")[:2],
                         (0, "No recorded API requests.\n"))

    def test_cli_rejects_invalid_filters_with_exit_code_2(self):
        for arguments, message in (
            (("--status", "teapot"),
             "Error: Status must be an HTTP status code such as 403, or a class such as 4xx.\n"),
            (("--since", "yesterday"), "Error: Since must be a date or a time in UTC, like 2026-10-31 or "
                                       "2026-10-31T14:00:00Z.\n"),
            (("--since", "2026-10-04", "--until", "2026-10-01"), "Error: Since must be earlier than until.\n"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.cli("audit", "list", *arguments), (2, "", message))


class AuditFilterApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": f"Bearer {create_token('admin', self.database)['token']}"}
        reader = {"Authorization": f"Bearer {create_token('reader', self.database, scopes=['tickets:read'])['token']}"}
        self.client.get("/tickets/1", headers=reader)
        self.client.post("/tickets", json={"title": "x", "message": "y"}, headers=reader)
        self.client.get("/stats", headers=reader)
        at(self.database, 1, "2026-10-01 09:00:00")
        at(self.database, 2, "2026-10-02 09:00:00")
        at(self.database, 3, "2026-10-03 09:00:00")

    def paths(self, query):
        response = self.client.get(f"/audit?{query}", headers=self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        return [(row["path"], row["status"]) for row in response.json() if row["token"] == "reader"]

    def test_audit_route_filters_by_status_and_time(self):
        for query, expected in (
            ("status=4xx", [("/tickets", 403), ("/tickets/1", 404)]),
            ("status=403", [("/tickets", 403)]),
            ("status=2xx&token=reader", [("/stats", 200)]),
            ("since=2026-10-02", [("/stats", 200), ("/tickets", 403)]),
            ("until=2026-10-02T09:00:00Z", [("/tickets/1", 404)]),
            ("since=2026-10-02T10:00:00%2B01:00&until=2026-10-03", [("/tickets", 403)]),
            ("status=4xx&since=2026-10-02&ticket_id=1", []),
        ):
            with self.subTest(query=query):
                self.assertEqual(self.paths(query), expected)

    def test_invalid_filters_return_422_and_are_still_audited(self):
        for query in ("status=4XX", "status=600", "status=40", "status=", "since=yesterday", "until=2026-02-30",
                      "since=2026-10-03&until=2026-10-01", "since=" + "2" * 33,
                      # An unencoded + arrives as a space, which is not a valid offset.
                      "since=2026-10-02T10:00:00+01:00"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/audit?{query}", headers=self.admin).status_code, 422)
        self.assertEqual({row["status"] for row in list_audit(self.database, 100, token="admin")}, {422})

    def test_openapi_documents_the_new_parameters(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/audit"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        self.assertEqual(set(parameters), {"limit", "offset", "token", "ticket_id", "status", "since", "until"})
        self.assertIn("4xx", parameters["status"]["description"])


if __name__ == "__main__":
    unittest.main()
