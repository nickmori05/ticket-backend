from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend import cli
from ticket_backend.api import create_app
from ticket_backend.audit import list_audit, record_request, summarize_audit
from ticket_backend.database import connect
from ticket_backend.tokens import create_token, revoke_token


def at(database, identifier, moment):
    with closing(connect(database)) as connection, connection:
        connection.execute("UPDATE api_audit_log SET created_at = ? WHERE id = ?", (moment, identifier))


def record(database, token, status, moment):
    record_request(database, token, "GET", "/stats", "/stats", status)
    at(database, list_audit(database, 1)[0]["id"], moment)


def counts(rows):
    return {row["token"]: (row["requests"], row["succeeded"], row["client_errors"], row["forbidden"],
                           row["server_errors"]) for row in rows}


class AuditSummaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        for name in ("alice", "bob", "idle"):
            create_token(name, self.database)
        record(self.database, "alice", 200, "2026-10-01 09:00:00")
        record(self.database, "alice", 403, "2026-10-02 09:00:00")
        record(self.database, "alice", 404, "2026-10-03 09:00:00")
        record(self.database, "bob", 201, "2026-10-02 12:00:00")
        record(self.database, "bob", 503, "2026-10-04 12:00:00")
        record(self.database, "bob", 304, "2026-10-04 13:00:00")

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_each_token_is_counted_by_outcome_least_recently_used_first(self):
        rows = summarize_audit(self.database)
        self.assertEqual([row["token"] for row in rows], ["idle", "alice", "bob"])
        self.assertEqual(counts(rows), {"idle": (0, 0, 0, 0, 0), "alice": (3, 1, 2, 1, 0), "bob": (3, 2, 0, 0, 1)})
        self.assertEqual({row["token"]: (row["first_request_at"], row["last_request_at"]) for row in rows}, {
            "idle": (None, None),
            "alice": ("2026-10-01 09:00:00", "2026-10-03 09:00:00"),
            "bob": ("2026-10-02 12:00:00", "2026-10-04 13:00:00"),
        })

    def test_the_window_limits_what_is_counted(self):
        rows = summarize_audit(self.database, since="2026-10-02T10:00", until="2026-10-04T12:30:00Z")
        self.assertEqual([row["token"] for row in rows], ["idle", "alice", "bob"])
        self.assertEqual(counts(rows), {"idle": (0, 0, 0, 0, 0), "alice": (1, 0, 1, 0, 0), "bob": (2, 1, 0, 0, 1)})
        rows = summarize_audit(self.database, since="2026-10-04")
        self.assertEqual([(row["token"], row["requests"]) for row in rows], [("alice", 0), ("idle", 0), ("bob", 2)])

    def test_revoked_and_expired_tokens_appear_only_with_requests_in_the_window(self):
        revoke_token("alice", self.database)
        create_token("lapsed", self.database, expires_in_days=1)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE api_tokens SET expires_at = datetime('now', '-1 day') WHERE name = 'lapsed'")
        self.assertEqual([row["token"] for row in summarize_audit(self.database)], ["idle", "alice", "bob"])
        self.assertEqual([row["token"] for row in summarize_audit(self.database, since="2026-10-04")], ["idle", "bob"])

    def test_requests_by_tokens_no_longer_in_the_database_are_still_counted(self):
        record(self.database, "deleted-by-hand", 200, "2026-10-05 00:00:00")
        self.assertEqual(summarize_audit(self.database)[-1]["token"], "deleted-by-hand")

    def test_an_empty_database_has_an_empty_summary(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertEqual(summarize_audit(Path(directory.name) / "tickets.db"), [])

    def test_invalid_windows_are_rejected(self):
        for window, message in (({"since": "soon"}, "Since must"), ({"until": "2026-02-30"}, "Until is not"),
                                ({"since": "2026-10-04", "until": "2026-10-04"}, "Since must be earlier")):
            with self.subTest(window=window), self.assertRaisesRegex(ValueError, message):
                summarize_audit(self.database, **window)

    def test_cli_prints_text_and_json(self):
        code, output, _ = self.cli("audit", "summary")
        self.assertEqual((code, output.splitlines()), (0, [
            "idle: no requests",
            "alice: 3 requests, last 2026-10-03 09:00:00 UTC "
            "(1 succeeded, 2 client errors (1 forbidden), 0 server errors)",
            "bob: 3 requests, last 2026-10-04 13:00:00 UTC (2 succeeded, 0 client errors, 1 server error)",
        ]))
        code, output, _ = self.cli("audit", "summary", "--since", "2026-10-03", "--until", "2026-10-04")
        self.assertEqual(output.splitlines()[-1],
                         "alice: 1 request, last 2026-10-03 09:00:00 UTC "
                         "(0 succeeded, 1 client error, 0 server errors)")
        code, output, _ = self.cli("--json", "audit", "summary", "--since", "2026-10-04")
        self.assertEqual((code, counts(json.loads(output))),
                         (0, {"alice": (0, 0, 0, 0, 0), "idle": (0, 0, 0, 0, 0), "bob": (2, 1, 0, 0, 1)}))
        self.assertEqual(self.cli("audit", "summary", "--until", "yesterday")[0], 2)

    def test_cli_reports_an_empty_summary(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["--database", str(Path(directory.name) / "tickets.db"), "audit", "summary"])
        self.assertEqual((code, output.getvalue()), (0, "No API tokens or recorded requests.\n"))


class AuditSummaryApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": f"Bearer {create_token('admin', self.database)['token']}"}
        reader = create_token("reader", self.database, scopes=["tickets:read"])
        self.reader = {"Authorization": f"Bearer {reader['token']}"}
        create_token("idle", self.database)

    def test_summary_counts_real_requests_and_records_its_own_reads(self):
        self.client.get("/stats", headers=self.reader)
        self.client.get("/tickets/7", headers=self.reader)
        self.client.post("/tickets", json={"title": "x", "message": "y"}, headers=self.reader)
        response = self.client.get("/audit/summary", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        # The summary's own request is recorded after its response, so admin has none yet.
        self.assertEqual([row["token"] for row in rows], ["admin", "idle", "reader"])
        self.assertEqual(counts(rows)["reader"], (3, 1, 2, 1, 0))
        self.assertEqual(list_audit(self.database, 1)[0]["route"], "/audit/summary")
        self.assertEqual(counts(self.client.get("/audit/summary", headers=self.admin).json())["admin"],
                         (1, 1, 0, 0, 0))

    def test_summary_takes_a_window(self):
        self.client.get("/stats", headers=self.reader)
        at(self.database, 1, "2026-10-01 09:00:00")
        for query, expected in (("since=2026-10-01", 1), ("since=2026-10-01T09:00:01Z", 0),
                                ("until=2026-10-01T10:30:00%2B01:00", 1), ("until=2026-10-01T11:00:00%2B02:00", 0)):
            with self.subTest(query=query):
                rows = self.client.get(f"/audit/summary?{query}", headers=self.admin).json()
                self.assertEqual(counts(rows)["reader"][0], expected)
        for query in ("since=soon", "until=2026-02-30", "since=2026-10-02&until=2026-10-01", "since=" + "2" * 33):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/audit/summary?{query}", headers=self.admin).status_code, 422)

    def test_summary_needs_the_audit_scope(self):
        response = self.client.get("/audit/summary", headers=self.reader)
        self.assertEqual((response.status_code, response.headers["WWW-Authenticate"]),
                         (403, 'Bearer error="insufficient_scope", scope="audit:read"'))
        self.assertEqual(self.client.get("/audit/summary").status_code, 401)
        revoke_token("reader", self.database)
        self.assertEqual(self.client.get("/audit/summary", headers=self.reader).status_code, 401)

    def test_openapi_documents_the_summary(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/audit/summary"]["get"]
        self.assertEqual({parameter["name"] for parameter in operation["parameters"]}, {"since", "until"})
        self.assertTrue({"200", "401", "403", "429"} <= set(operation["responses"]))


if __name__ == "__main__":
    unittest.main()
