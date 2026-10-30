from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.mailer import DeliveryError
from ticket_backend.notifications import deliver_notifications
from ticket_backend.suppressions import list_suppressions
from ticket_backend.tickets import create_ticket
from ticket_backend.tokens import create_token


def refuse(notification):
    # Servers usually quote the address back, sometimes in another case.
    raise DeliveryError(f"Recipient {notification['recipient'].upper()} refused: 550 No such user", permanent=True)


class SuppressionApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        create_ticket("Login", "Broken", self.database, email="gone@example.com")
        deliver_notifications(refuse, self.database)
        self.client = TestClient(create_app(self.database, rate_limit=0))
        self.addCleanup(self.client.close)
        self.operator = self.bearer("operator")

    def bearer(self, name, scopes=None):
        return {"Authorization": f"Bearer {create_token(name, self.database, scopes=scopes)['token']}"}

    def test_listing_shows_refusals_and_manual_suppressions_newest_first(self):
        created = self.client.post("/suppressions", json={"address": " pat@example.com "}, headers=self.operator)
        self.assertEqual(created.status_code, 201)
        self.assertEqual({key: created.json()[key] for key in ("id", "address", "reason", "detail")},
                         {"id": 2, "address": "pat@example.com", "reason": "manual", "detail": None})
        response = self.client.get("/suppressions", headers=self.operator)
        self.assertEqual((response.status_code, response.json()), (200, list_suppressions(self.database)))
        self.assertEqual([(row["address"], row["reason"], row["notification_id"]) for row in response.json()],
                         [("pat@example.com", "manual", None), ("gone@example.com", "refused", 1)])
        page = self.client.get("/suppressions?limit=1&offset=1", headers=self.operator).json()
        self.assertEqual([row["id"] for row in page], [1])
        again = self.client.post("/suppressions", json={"address": "GONE@example.com"}, headers=self.operator)
        self.assertEqual((again.status_code, again.json()["reason"]), (201, "refused"), "A refusal is kept")

    def test_deleting_by_id_lifts_the_suppression(self):
        response = self.client.delete("/suppressions/1", headers=self.operator)
        self.assertEqual((response.status_code, response.json()["address"]), (200, "gone@example.com"))
        self.assertEqual(list_suppressions(self.database), [])
        missing = self.client.delete("/suppressions/1", headers=self.operator)
        self.assertEqual((missing.status_code, missing.json()),
                         (404, {"detail": "No suppression was found for #1."}))
        retried = self.client.post("/notifications/retry", json={"ids": [1]}, headers=self.operator)
        self.assertEqual((retried.status_code, retried.json()[0]["status"]), (200, "pending"))

    def test_a_suppressed_address_cannot_be_retried_over_http(self):
        response = self.client.post("/notifications/retry", json={"ids": [1]}, headers=self.operator)
        self.assertEqual(response.status_code, 409)
        self.assertIn("suppressed address", response.json()["detail"])
        self.assertEqual(self.client.post("/notifications/retry", headers=self.operator).json(), [])

    def test_addresses_are_hidden_without_the_emails_scope(self):
        headers = self.bearer("ops", ["notifications:read", "notifications:write"])
        [row] = self.client.get("/suppressions", headers=headers).json()
        self.assertEqual((row["address"], row["detail"]), (None, "Recipient [hidden] refused: 550 No such user"))
        created = self.client.post("/suppressions", json={"address": "pat@example.com"}, headers=headers)
        self.assertEqual((created.status_code, created.json()["address"]), (201, None))
        self.assertIsNone(self.client.delete("/suppressions/2", headers=headers).json()["address"])
        self.assertEqual(list_suppressions(self.database)[0]["address"], "gone@example.com")

    def test_each_route_needs_its_notification_scope(self):
        reader = self.bearer("reader", ["notifications:read"])
        writer = self.bearer("writer", ["notifications:write"])
        tickets = self.bearer("tickets", ["tickets:read", "tickets:write", "emails:read"])
        for method, path, headers, body in (
            ("GET", "/suppressions", writer, None), ("GET", "/suppressions", tickets, None),
            ("POST", "/suppressions", reader, {"address": "pat@example.com"}),
            ("DELETE", "/suppressions/1", reader, None), ("DELETE", "/suppressions/9", tickets, None),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers=headers, json=body)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/suppressions").status_code, 401)
        self.assertEqual(len(list_suppressions(self.database)), 1)

    def test_invalid_input_returns_422(self):
        for body in ({"address": "not an address"}, {"address": ""}, {"address": 5}, {},
                     {"address": "pat@example.com", "reason": "manual"}):
            with self.subTest(body=body):
                response = self.client.post("/suppressions", json=body, headers=self.operator)
                self.assertEqual(response.status_code, 422)
        for query in ("limit=0", "limit=101", "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/suppressions?{query}", headers=self.operator).status_code, 422)
        for identifier in ("0", "abc"):
            self.assertEqual(self.client.delete(f"/suppressions/{identifier}", headers=self.operator).status_code, 422)
        self.assertEqual(len(list_suppressions(self.database)), 1)

    def test_requests_are_audited_without_addresses_in_paths(self):
        self.client.post("/suppressions", json={"address": "pat@example.com"}, headers=self.operator)
        self.client.delete("/suppressions/2", headers=self.operator)
        rows = [(row["method"], row["path"], row["route"], row["status"]) for row in list_audit(self.database)]
        self.assertEqual(rows, [("DELETE", "/suppressions/2", "/suppressions/{suppression_id}", 200),
                                ("POST", "/suppressions", "/suppressions", 201)])

    def test_routes_are_documented(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths["/suppressions"]), {"get", "post"})
        self.assertIn("404", paths["/suppressions/{suppression_id}"]["delete"]["responses"])


if __name__ == "__main__":
    unittest.main()
