from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.database import connect
from ticket_backend.notifications import list_notifications
from ticket_backend.tickets import create_ticket, get_ticket, list_comments, list_tickets, ticket_history
from ticket_backend.tokens import create_token


class TokenScopeApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.identifier = create_ticket("Login", "Cannot sign in", self.database, email="pat@example.com")["id"]

    def bearer(self, name, scopes=None):
        return {"Authorization": f"Bearer {create_token(name, self.database, scopes=scopes)['token']}"}

    def reads(self):
        return (f"/tickets/{self.identifier}", "/tickets", f"/tickets/{self.identifier}/history",
                f"/tickets/{self.identifier}/comments", "/stats")

    def writes(self):
        return (("POST", "/tickets", {"title": "New", "message": "Body"}),
                ("PATCH", f"/tickets/{self.identifier}/status", {"status": "closed"}),
                ("POST", f"/tickets/{self.identifier}/comments", {"body": "Note"}))

    def assert_forbidden(self, response, scope):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": f"This token does not have the {scope} scope."})
        self.assertEqual(response.headers["WWW-Authenticate"], f'Bearer error="insufficient_scope", scope="{scope}"')

    def test_read_only_tokens_can_read_but_not_write(self):
        headers = self.bearer("reader", ["tickets:read"])
        for path in self.reads():
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 200)
        for method, path, payload in self.writes():
            with self.subTest(method=method, path=path):
                self.assert_forbidden(self.client.request(method, path, json=payload, headers=headers),
                                      "tickets:write")
        self.assertEqual(len(list_tickets(self.database)), 1)
        self.assertEqual(get_ticket(self.identifier, self.database)["status"], "open")
        self.assertEqual(list_comments(self.identifier, self.database), [])

    def test_write_only_tokens_can_submit_but_not_read(self):
        headers = self.bearer("intake", ["tickets:write"])
        response = self.client.post("/tickets", json={"title": "Form", "message": "Body"}, headers=headers)
        self.assertEqual(response.status_code, 201)
        for path in self.reads():
            with self.subTest(path=path):
                self.assert_forbidden(self.client.get(path, headers=headers), "tickets:read")

    def test_scope_is_checked_before_the_ticket_or_body(self):
        headers = self.bearer("reader", ["tickets:read"])
        self.assert_forbidden(self.client.patch("/tickets/99/status", json={"status": "closed"}, headers=headers),
                              "tickets:write")
        self.assert_forbidden(self.client.post("/tickets", json={"title": " "}, headers=headers), "tickets:write")
        intake = self.bearer("intake", ["tickets:write"])
        self.assert_forbidden(self.client.get("/tickets/99", headers=intake), "tickets:read")

    def test_emails_are_hidden_without_the_emails_scope(self):
        agent = self.bearer("agent", ["tickets:read", "tickets:write"])
        url = f"/tickets/{self.identifier}"
        self.assertIsNone(self.client.get(url, headers=agent).json()["email"])
        self.assertIsNone(self.client.get("/tickets", headers=agent).json()[0]["email"])
        changed = self.client.patch(url + "/status", json={"status": "closed"}, headers=agent)
        self.assertEqual((changed.json()["status"], changed.json()["email"]), ("closed", None))
        payload = {"title": "New", "message": "Body", "email": "sam@example.com"}
        created = self.client.post("/tickets", json=payload, headers={**agent, "Idempotency-Key": "k1"})
        replayed = self.client.post("/tickets", json=payload, headers={**agent, "Idempotency-Key": "k1"})
        self.assertIsNone(created.json()["email"])
        self.assertEqual(replayed.json(), created.json())
        # The address is still stored and used for notifications; it is only left out of responses.
        self.assertEqual(get_ticket(created.json()["id"], self.database)["email"], "sam@example.com")
        self.assertEqual([row["recipient"] for row in list_notifications(self.database)],
                         ["pat@example.com", "pat@example.com", "sam@example.com"])
        support = self.bearer("support", ["tickets:read", "tickets:write", "emails:read"])
        self.assertEqual(self.client.get(url, headers=support).json()["email"], "pat@example.com")
        self.assertEqual(self.client.get(f"/tickets/{created.json()['id']}", headers=support).json()["email"],
                         "sam@example.com")

    def test_emails_scope_alone_grants_no_ticket_access(self):
        headers = self.bearer("emails", ["emails:read"])
        self.assert_forbidden(self.client.get(f"/tickets/{self.identifier}", headers=headers), "tickets:read")

    def test_expired_tokens_are_rejected_like_unknown_ones(self):
        headers = self.bearer("temp")
        self.assertEqual(self.client.get("/stats", headers=headers).status_code, 200)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE api_tokens SET expires_at = datetime('now', '-1 second')")
        response = self.client.get("/stats", headers=headers)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "A valid API token is required."})

    def test_tokens_from_before_scopes_keep_full_access(self):
        headers = self.bearer("old")
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE api_tokens SET scopes = NULL")
        url = f"/tickets/{self.identifier}"
        self.assertEqual(self.client.get(url, headers=headers).json()["email"], "pat@example.com")
        self.assertEqual(self.client.patch(url + "/status", json={"status": "closed"}, headers=headers).status_code,
                         200)
        self.assertEqual(ticket_history(self.identifier, self.database)[-1]["author"], "old")

    def test_openapi_documents_the_forbidden_response(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("403", paths["/tickets"]["post"]["responses"])
        self.assertIn("403", paths["/stats"]["get"]["responses"])
        self.assertNotIn("403", paths["/health"]["get"]["responses"])


if __name__ == "__main__":
    unittest.main()
