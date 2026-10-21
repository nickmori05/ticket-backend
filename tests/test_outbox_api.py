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
from ticket_backend.notifications import (
    NotificationNotFoundError, deliver_notifications, get_notification, list_notifications,
)
from ticket_backend.tickets import create_ticket, set_status
from ticket_backend.tokens import create_token


def refuse(notification):
    # Servers usually quote the address back, sometimes in another case.
    raise ConnectionError(f"Recipient {notification['recipient'].upper()} refused: 550 No such user")


class OutboxFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        first = create_ticket("Login", "Broken", self.database, email="pat@example.com")
        create_ticket("Billing", "Question", self.database, email="sam@example.com")
        set_status(first["id"], "closed", self.database, note="Fixed")
        # Notification 1 fails for good, 2 is sent, and 3 stays pending.
        deliver_notifications(refuse, self.database, limit=1, max_attempts=1)
        deliver_notifications(lambda notification: None, self.database, limit=1)

    def statuses(self):
        return [row["status"] for row in list_notifications(self.database)]


class OutboxStorageTests(OutboxFixture):
    def test_listing_filters_by_ticket_and_status(self):
        self.assertEqual([row["id"] for row in list_notifications(self.database, ticket_id=1)], [1, 3])
        self.assertEqual([row["id"] for row in list_notifications(self.database, ticket_id=1, status="failed")], [1])
        self.assertEqual(list_notifications(self.database, ticket_id=99), [])
        for ticket_id in (0, -1, "1", True):
            with self.subTest(ticket_id=ticket_id), self.assertRaises(ValueError):
                list_notifications(self.database, ticket_id=ticket_id)

    def test_single_notifications_are_found_by_id(self):
        self.assertEqual(get_notification(2, self.database), list_notifications(self.database)[1])
        with self.assertRaisesRegex(NotificationNotFoundError, "#9"):
            get_notification(9, self.database)
        for identifier in (0, "1", None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                get_notification(identifier, self.database)

    def test_existing_outboxes_gain_the_ticket_index(self):
        def indexes():
            with closing(sqlite3.connect(self.database)) as connection:
                return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP INDEX ticket_notifications_ticket_id")
        self.assertNotIn("ticket_notifications_ticket_id", indexes())
        self.assertEqual(len(list_notifications(self.database, ticket_id=1)), 2)
        self.assertIn("ticket_notifications_ticket_id", indexes())

    def test_cli_filters_the_outbox_by_ticket(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = cli.main(["--database", str(self.database), "--json", "outbox", "--ticket", "2"])
        self.assertEqual((code, [row["id"] for row in json.loads(output.getvalue())]), (0, [2]))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--database", str(self.database), "outbox", "--ticket", "0"]), 2)


class OutboxApiTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.operator = self.bearer("operator")

    def bearer(self, name, scopes=None):
        return {"Authorization": f"Bearer {create_token(name, self.database, scopes=scopes)['token']}"}

    def test_listing_matches_the_outbox_and_supports_filters(self):
        response = self.client.get("/notifications", headers=self.operator)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), list_notifications(self.database))
        self.assertEqual(response.json()[0]["last_error"], "Recipient PAT@EXAMPLE.COM refused: 550 No such user")
        for query, expected in (("status=failed", [1]), ("status=pending", [3]), ("ticket_id=1", [1, 3]),
                                ("ticket_id=1&status=sent", []), ("limit=1&offset=1", [2])):
            with self.subTest(query=query):
                rows = self.client.get(f"/notifications?{query}", headers=self.operator).json()
                self.assertEqual([row["id"] for row in rows], expected)

    def test_invalid_queries_return_422(self):
        for query in ("status=queued", "ticket_id=0", "ticket_id=x", "limit=0", "limit=101", "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/notifications?{query}", headers=self.operator).status_code, 422)

    def test_single_notification_and_missing_ids(self):
        response = self.client.get("/notifications/2", headers=self.operator)
        self.assertEqual((response.status_code, response.json()), (200, get_notification(2, self.database)))
        missing = self.client.get("/notifications/9", headers=self.operator)
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Notification #9 was not found."}))
        for identifier in ("0", "abc"):
            self.assertEqual(self.client.get(f"/notifications/{identifier}", headers=self.operator).status_code, 422)

    def test_recipients_are_hidden_without_the_emails_scope(self):
        headers = self.bearer("ops", ["notifications:read", "notifications:write"])
        rows = self.client.get("/notifications", headers=headers).json()
        self.assertEqual([row["recipient"] for row in rows], [None, None, None])
        self.assertEqual(rows[0]["last_error"], "Recipient [hidden] refused: 550 No such user")
        self.assertIsNone(self.client.get("/notifications/1", headers=headers).json()["recipient"])
        requeued = self.client.post("/notifications/retry", json={"ids": [1]}, headers=headers).json()
        self.assertEqual((requeued[0]["recipient"], requeued[0]["last_error"]),
                         (None, "Recipient [hidden] refused: 550 No such user"))
        self.assertEqual(get_notification(1, self.database)["recipient"], "pat@example.com")

    def test_retry_requeues_named_or_all_failed_notifications(self):
        response = self.client.post("/notifications/retry", json={"ids": [1]}, headers=self.operator)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([(row["id"], row["status"], row["attempts"]) for row in response.json()],
                         [(1, "pending", 0)])
        self.assertEqual(self.client.post("/notifications/retry", headers=self.operator).json(), [])
        deliver_notifications(refuse, self.database, limit=1, max_attempts=1)
        self.assertEqual([row["id"] for row in self.client.post("/notifications/retry", json={},
                                                                headers=self.operator).json()], [1])

    def test_retry_errors_change_nothing(self):
        missing = self.client.post("/notifications/retry", json={"ids": [1, 9]}, headers=self.operator)
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Notification #9 was not found."}))
        pending = self.client.post("/notifications/retry", json={"ids": [1, 3]}, headers=self.operator)
        self.assertEqual(pending.status_code, 409)
        self.assertEqual(pending.json(), {"detail": "Notification #3 is pending; only failed notifications can be "
                                                    "retried."})
        for payload in ({"ids": []}, {"ids": [0]}, {"ids": ["1"]}, {"ids": [True]}, {"ids": 1},
                        {"ids": list(range(1, 102))}, {"all": True}):
            with self.subTest(payload=str(payload)[:40]):
                response = self.client.post("/notifications/retry", json=payload, headers=self.operator)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.statuses(), ["failed", "sent", "pending"])

    def test_notification_routes_need_their_own_scopes(self):
        routes = (("GET", "/notifications", "notifications:read"), ("GET", "/notifications/1", "notifications:read"),
                  ("POST", "/notifications/retry", "notifications:write"))
        agent = self.bearer("agent", ["tickets:read", "tickets:write", "emails:read"])
        for method, path, scope in routes:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)
                response = self.client.request(method, path, headers=agent)
                self.assertEqual((response.status_code, response.json()["detail"]),
                                 (403, f"This token does not have the {scope} scope."))
        viewer = self.bearer("viewer", ["notifications:read"])
        self.assertEqual(self.client.get("/notifications", headers=viewer).status_code, 200)
        self.assertEqual(self.client.post("/notifications/retry", headers=viewer).status_code, 403)
        self.assertEqual(self.client.get("/tickets", headers=viewer).status_code, 403)
        retrier = self.bearer("retrier", ["notifications:write"])
        self.assertEqual(self.client.get("/notifications", headers=retrier).status_code, 403)
        self.assertEqual(self.client.post("/notifications/retry", headers=retrier).status_code, 200)
        self.assertEqual(self.statuses(), ["pending", "sent", "pending"])

    def test_tokens_from_before_the_outbox_api_do_not_gain_it(self):
        headers = self.bearer("old")
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE api_tokens SET scopes = NULL WHERE name = 'old'")
        self.assertEqual(self.client.get("/tickets", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/notifications", headers=headers).status_code, 403)

    def test_openapi_documents_the_outbox_routes(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths["/notifications"]), {"get"})
        self.assertIn("404", paths["/notifications/{notification_id}"]["get"]["responses"])
        self.assertTrue({"403", "404", "409"} <= set(paths["/notifications/retry"]["post"]["responses"]))


if __name__ == "__main__":
    unittest.main()
