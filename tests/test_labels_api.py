from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.labels import add_labels, ticket_labels
from ticket_backend.tickets import create_ticket, get_ticket, ticket_history
from ticket_backend.tokens import create_token


class LabelApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.headers = {"Authorization": f"Bearer {create_token('alice', self.database)['token']}"}
        self.client = TestClient(create_app(self.database), headers=self.headers)
        self.addCleanup(self.client.close)
        self.identifier = create_ticket("Login", "Cannot sign in", self.database)["id"]
        self.url = f"/tickets/{self.identifier}/labels"

    def test_labels_are_added_removed_and_listed_through_shared_storage(self):
        response = self.client.put(f"{self.url}/Billing")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ticket_id": self.identifier, "labels": ["billing"]})
        add_labels(self.identifier, ["urgent"], self.database)
        self.assertEqual(self.client.get(self.url).json()["labels"], ["billing", "urgent"])
        self.assertEqual(self.client.put(f"{self.url}/priority:high").json()["labels"],
                         ["billing", "priority:high", "urgent"])
        response = self.client.delete(f"{self.url}/urgent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["labels"], ["billing", "priority:high"])
        self.assertEqual(ticket_labels(self.identifier, self.database)["labels"], ["billing", "priority:high"])

    def test_repeated_requests_change_nothing(self):
        first = self.client.put(f"{self.url}/billing").json()
        self.assertEqual(self.client.put(f"{self.url}/billing").json(), first)
        self.client.delete(f"{self.url}/billing")
        response = self.client.delete(f"{self.url}/billing")
        self.assertEqual((response.status_code, response.json()["labels"]), (200, []))

    def test_labels_do_not_change_the_ticket_or_its_history(self):
        before = get_ticket(self.identifier, self.database)
        self.client.put(f"{self.url}/billing")
        self.assertEqual(get_ticket(self.identifier, self.database), before)
        self.assertEqual(len(ticket_history(self.identifier, self.database)), 1)

    def test_listing_filters_by_every_label(self):
        other = create_ticket("Refund", "Charged twice", self.database)["id"]
        add_labels(self.identifier, ["billing"], self.database)
        add_labels(other, ["billing", "urgent"], self.database)
        self.assertEqual([row["id"] for row in self.client.get("/tickets?label=billing").json()],
                         [other, self.identifier])
        self.assertEqual([row["id"] for row in self.client.get("/tickets?label=billing&label=URGENT").json()],
                         [other])
        self.assertEqual(self.client.get("/tickets?label=billing&status=closed").json(), [])
        self.assertEqual(self.client.get("/tickets?label=" + "&label=".join("abcdef")).status_code, 422)
        self.assertEqual(self.client.get("/tickets?label=bad%25").status_code, 422)

    def test_label_counts(self):
        self.assertEqual(self.client.get("/labels").json(), [])
        add_labels(self.identifier, ["billing"], self.database)
        self.assertEqual(self.client.get("/labels").json(), [{"label": "billing", "tickets": 1, "unresolved": 1}])

    def test_invalid_labels_and_ids_return_422_without_writing(self):
        for label in ("two%20words", "-billing", "x" * 33, "caf%C3%A9", "%20"):
            with self.subTest(label=label):
                self.assertEqual(self.client.put(f"{self.url}/{label}").status_code, 422)
        self.assertEqual(self.client.put("/tickets/0/labels/billing").status_code, 422)
        self.assertEqual(ticket_labels(self.identifier, self.database)["labels"], [])
        add_labels(self.identifier, [f"l{number}" for number in range(20)], self.database)
        response = self.client.put(f"{self.url}/extra")
        self.assertEqual(response.status_code, 422)
        self.assertIn("at most 20 labels", response.json()["detail"])

    def test_missing_tickets_return_404(self):
        self.assertEqual(self.client.get("/tickets/99/labels").status_code, 404)
        self.assertEqual(self.client.put("/tickets/99/labels/billing").status_code, 404)
        self.assertEqual(self.client.delete("/tickets/99/labels/billing").status_code, 404)

    def test_scopes_are_required_and_changes_are_audited(self):
        reader = create_token("reader", self.database, scopes=["tickets:read"])["token"]
        headers = {"Authorization": f"Bearer {reader}"}
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/labels", headers=headers).status_code, 200)
        self.assertEqual(self.client.put(f"{self.url}/billing", headers=headers).status_code, 403)
        self.assertEqual(self.client.delete(f"{self.url}/billing", headers=headers).status_code, 403)
        self.assertEqual(self.client.get(self.url, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.client.put(f"{self.url}/billing")
        rows = list_audit(self.database, token="alice")
        self.assertEqual([(row["method"], row["route"], row["ticket_id"], row["status"]) for row in rows],
                         [("PUT", "/tickets/{ticket_id}/labels/{label}", self.identifier, 200)])

    def test_openapi_documents_the_label_routes(self):
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(set(schema["paths"]["/tickets/{ticket_id}/labels/{label}"]), {"put", "delete"})
        self.assertIn("/labels", schema["paths"])
        parameters = {parameter["name"] for parameter in schema["paths"]["/tickets"]["get"]["parameters"]}
        self.assertIn("label", parameters)


if __name__ == "__main__":
    unittest.main()
