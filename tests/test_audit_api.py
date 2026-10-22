from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.database import connect
from ticket_backend.tickets import create_ticket
from ticket_backend.tokens import create_token, revoke_token


def summary(rows):
    return [(row["token"], row["method"], row["path"], row["route"], row["ticket_id"], row["status"])
            for row in reversed(rows)]


class AuditApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.admin = self.bearer("admin")

    def bearer(self, name, scopes=None):
        return {"Authorization": f"Bearer {create_token(name, self.database, scopes=scopes)['token']}"}

    def test_each_request_records_its_token_route_ticket_and_status(self):
        created = self.client.post("/tickets", json={"title": "Login", "message": "Broken"}, headers=self.admin)
        self.assertEqual(created.status_code, 201)
        self.client.get("/tickets/1", headers=self.admin)
        self.client.patch("/tickets/1/status", json={"status": "closed"}, headers=self.admin)
        self.client.post("/tickets/1/comments", json={"body": "Done"}, headers=self.admin)
        self.client.get("/stats", headers=self.admin)
        self.assertEqual(summary(list_audit(self.database)), [
            ("admin", "POST", "/tickets", "/tickets", 1, 201),
            ("admin", "GET", "/tickets/1", "/tickets/{ticket_id}", 1, 200),
            ("admin", "PATCH", "/tickets/1/status", "/tickets/{ticket_id}/status", 1, 200),
            ("admin", "POST", "/tickets/1/comments", "/tickets/{ticket_id}/comments", 1, 201),
            ("admin", "GET", "/stats", "/stats", None, 200),
        ])
        self.assertEqual({row["client"] for row in list_audit(self.database)}, {"testclient"})

    def test_refused_and_failed_requests_are_recorded_with_their_token(self):
        create_ticket("Login", "Broken", self.database)
        reader = self.bearer("reader", ["tickets:read"])
        self.client.post("/tickets", json={"title": "x", "message": "y"}, headers=reader)
        self.client.get("/tickets/99", headers=reader)
        self.client.get("/tickets/abc", headers=reader)
        self.client.patch("/tickets/1/status", json={"status": "done"}, headers=self.admin)
        payload = {"title": "Login", "message": "Broken"}
        self.client.post("/tickets", json=payload, headers={**self.admin, "Idempotency-Key": "k1"})
        self.client.post("/tickets", json={**payload, "message": "Other"},
                         headers={**self.admin, "Idempotency-Key": "k1"})
        self.assertEqual(summary(list_audit(self.database)), [
            ("reader", "POST", "/tickets", "/tickets", None, 403),
            ("reader", "GET", "/tickets/99", "/tickets/{ticket_id}", 99, 404),
            ("reader", "GET", "/tickets/abc", "/tickets/{ticket_id}", None, 422),
            ("admin", "PATCH", "/tickets/1/status", "/tickets/{ticket_id}/status", 1, 422),
            ("admin", "POST", "/tickets", "/tickets", 2, 201),
            ("admin", "POST", "/tickets", "/tickets", None, 409),
        ])

    def test_requests_without_a_valid_token_are_not_recorded(self):
        revoked = self.bearer("old")
        revoke_token("old", self.database)
        for headers in ({}, {"Authorization": "Bearer tkt_unknown"}, revoked):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get("/tickets", headers=headers).status_code, 401)
        self.client.get("/health")
        self.client.get("/openapi.json")
        self.client.get("/missing", headers=self.admin)
        self.assertEqual(list_audit(self.database), [])

    def test_query_strings_are_not_recorded(self):
        self.client.get("/tickets?search=password123&limit=5", headers=self.admin)
        self.assertEqual(list_audit(self.database)[0]["path"], "/tickets")

    def test_audit_route_lists_filters_and_records_its_own_reads(self):
        create_ticket("Login", "Broken", self.database)
        agent = self.bearer("agent", ["tickets:read"])
        self.client.get("/tickets/1", headers=agent)
        self.client.get("/stats", headers=agent)
        self.client.get("/stats", headers=self.admin)
        response = self.client.get("/audit", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([(row["token"], row["path"]) for row in response.json()],
                         [("admin", "/stats"), ("agent", "/stats"), ("agent", "/tickets/1")])
        for query, expected in (("token=agent", ["/stats", "/tickets/1"]), ("ticket_id=1", ["/tickets/1"]),
                                ("token=agent&limit=1", ["/stats"]), ("token=agent&offset=1", ["/tickets/1"]),
                                ("token=agent&ticket_id=1", ["/tickets/1"]), ("token=nobody", [])):
            with self.subTest(query=query):
                rows = self.client.get(f"/audit?{query}", headers=self.admin).json()
                self.assertEqual([row["path"] for row in rows], expected)
        self.assertEqual([row["route"] for row in list_audit(self.database, 7, token="admin")], ["/audit"] * 7)

    def test_invalid_audit_queries_return_422(self):
        for query in ("token=bad%20name", "token=" + "x" * 65, "ticket_id=0", "ticket_id=x", "limit=0", "limit=101",
                      "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/audit?{query}", headers=self.admin).status_code, 422)

    def test_audit_route_needs_its_own_scope(self):
        everything_else = self.bearer("ops", ["tickets:read", "tickets:write", "emails:read", "notifications:read",
                                              "notifications:write"])
        response = self.client.get("/audit", headers=everything_else)
        self.assertEqual((response.status_code, response.json()["detail"]),
                         (403, "This token does not have the audit:read scope."))
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer error="insufficient_scope", scope="audit:read"')
        self.assertEqual(self.client.get("/audit").status_code, 401)
        auditor = self.bearer("auditor", ["audit:read"])
        self.assertEqual(self.client.get("/audit", headers=auditor).status_code, 200)
        self.assertEqual(self.client.get("/tickets", headers=auditor).status_code, 403)

    def test_tokens_from_before_the_audit_log_do_not_gain_it(self):
        legacy = self.bearer("legacy")
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE api_tokens SET scopes = NULL WHERE name = 'legacy'")
        self.assertEqual(self.client.get("/tickets", headers=legacy).status_code, 200)
        self.assertEqual(self.client.get("/audit", headers=legacy).status_code, 403)
        self.assertEqual([row["status"] for row in list_audit(self.database, token="legacy")], [403, 200])

    def test_a_failure_to_record_is_logged_without_changing_the_response(self):
        failure = sqlite3.OperationalError("database is locked")
        with mock.patch("ticket_backend.api_audit.record_request", side_effect=failure), \
                self.assertLogs("ticket_backend.api_audit", "ERROR") as logs:
            response = self.client.post("/tickets", json={"title": "Login", "message": "Broken"}, headers=self.admin)
        self.assertEqual(response.status_code, 201)
        self.assertIn("Could not record POST /tickets by admin in the audit log: database is locked", logs.output[0])
        self.assertEqual(list_audit(self.database), [])

    def test_unhandled_errors_are_recorded_as_500(self):
        client = TestClient(create_app(self.database), raise_server_exceptions=False)
        self.addCleanup(client.close)
        with mock.patch("ticket_backend.api.statistics", side_effect=RuntimeError("bug")):
            self.assertEqual(client.get("/stats", headers=self.admin).status_code, 500)
        self.assertEqual(summary(list_audit(self.database)), [("admin", "GET", "/stats", "/stats", None, 500)])

    def test_openapi_documents_the_audit_route(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/audit"]["get"]
        self.assertEqual({parameter["name"] for parameter in operation["parameters"]},
                         {"limit", "offset", "token", "ticket_id"})
        self.assertTrue({"200", "401", "403"} <= set(operation["responses"]))


if __name__ == "__main__":
    unittest.main()
