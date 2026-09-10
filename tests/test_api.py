import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import create_app
from functions import create_ticket, get_ticket, list_tickets, ticket_history


class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)

    def submit(self, title="Login", message="Cannot sign in"):
        return self.client.post("/tickets", json={"title": title, "message": message})

    def test_health_does_not_create_a_database(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertFalse(self.database.exists())

    def test_create_returns_location_and_persists_for_a_new_app(self):
        response = self.submit(" Login ", " Can't sign in ")
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]
        self.assertEqual(response.json()["title"], "Login")
        with TestClient(create_app(self.database)) as reopened:
            retrieved = reopened.get(location)
        self.assertEqual(retrieved.status_code, 200)
        self.assertEqual(retrieved.json(), response.json())
        self.assertEqual(get_ticket(response.json()["id"], self.database), response.json())

    def test_api_reads_tickets_created_by_the_terminal_storage_functions(self):
        ticket = create_ticket("From terminal", "Shared database", self.database)
        response = self.client.get(f"/tickets/{ticket['id']}")
        self.assertEqual(response.json(), ticket)

    def test_invalid_bodies_never_create_a_ticket(self):
        for payload in ({}, {"title": " ", "message": "Body"},
                        {"title": 123, "message": "Body"},
                        {"title": "Title", "message": None},
                        {"title": "x" * 121, "message": "Body"},
                        {"title": "Title", "message": "x" * 10_001},
                        {"title": "Title", "message": "Body", "status": "closed"}):
            with self.subTest(payload=str(payload)[:80]):
                self.assertEqual(self.client.post("/tickets", json=payload).status_code, 422)
        self.assertFalse(self.database.exists())

    def test_malformed_json_and_form_data_are_rejected(self):
        malformed = self.client.post("/tickets", content='{broken', headers={"Content-Type": "application/json"})
        form = self.client.post("/tickets", data={"title": "Title", "message": "Body"})
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(form.status_code, 422)
        self.assertFalse(self.database.exists())

    def test_status_changes_and_history_share_cli_semantics(self):
        identifier = self.submit().json()["id"]
        url = f"/tickets/{identifier}/status"
        response = self.client.patch(url, json={"status": "closed", "note": "Fixed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "closed")
        self.client.patch(url, json={"status": "closed", "note": "Repeat"})
        events = self.client.get(f"/tickets/{identifier}/history").json()
        self.assertEqual([event["to_status"] for event in events], ["open", "closed"])
        self.assertEqual(events[-1]["note"], "Fixed")
        self.assertEqual(events, ticket_history(identifier, self.database))

    def test_invalid_status_changes_leave_the_ticket_unchanged(self):
        identifier = self.submit().json()["id"]
        for payload in ({"status": "unknown"}, {"status": "closed", "note": "x" * 501},
                        {"status": "closed", "note": None}, {"status": "closed", "extra": True}):
            with self.subTest(payload=str(payload)[:80]):
                self.assertEqual(self.client.patch(f"/tickets/{identifier}/status", json=payload).status_code, 422)
        self.assertEqual(get_ticket(identifier, self.database)["status"], "open")
        self.assertEqual(len(ticket_history(identifier, self.database)), 1)

    def test_missing_tickets_return_404_and_invalid_ids_return_422(self):
        for path in ("/tickets/99", "/tickets/99/history"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.patch("/tickets/99/status", json={"status": "closed"}).status_code, 404)
        for identifier in ("0", "-1", "abc"):
            self.assertEqual(self.client.get(f"/tickets/{identifier}").status_code, 422)

    def test_list_filters_pagination_and_stats(self):
        first = self.submit("Login one").json()["id"]
        second = self.submit("Login two").json()["id"]
        self.submit("Billing", "Question")
        self.client.patch(f"/tickets/{second}/status", json={"status": "closed"})
        rows = self.client.get("/tickets", params={"search": "login", "status": "open"}).json()
        self.assertEqual([row["id"] for row in rows], [first])
        page = self.client.get("/tickets", params={"limit": 1, "offset": 1}).json()
        self.assertEqual([row["id"] for row in page], [second])
        self.assertEqual(self.client.get("/stats").json(), {
            "total": 3, "by_status": {"open": 2, "in_progress": 0, "closed": 1}})

    def test_invalid_list_filters_are_rejected(self):
        for parameters in ({"limit": 0}, {"limit": 101}, {"offset": -1},
                           {"status": "unknown"}, {"search": " "}):
            with self.subTest(parameters=parameters):
                self.assertEqual(self.client.get("/tickets", params=parameters).status_code, 422)

    def test_storage_errors_return_503_without_exposing_local_paths(self):
        self.database.mkdir()
        with self.assertLogs("api", level="ERROR"):
            response = self.client.get("/tickets")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Ticket storage is unavailable."})
        self.assertNotIn(str(self.database), response.text)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_environment_selects_database_without_changing_other_instances(self):
        other = self.database.with_name("other.db")
        with patch.dict(os.environ, {"TICKETS_DATABASE": str(other)}):
            with TestClient(create_app()) as client:
                self.assertEqual(client.post("/tickets", json={"title": "Other", "message": "Body"}).status_code, 201)
        self.assertEqual(self.client.get("/tickets").json(), [])
        self.assertEqual(list_tickets(other)[0]["title"], "Other")

    def test_openapi_describes_request_and_response_models(self):
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/tickets"]["post"]
        self.assertIn("201", operation["responses"])
        self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"]["$ref"],
                         "#/components/schemas/TicketInput")


if __name__ == "__main__":
    unittest.main()
