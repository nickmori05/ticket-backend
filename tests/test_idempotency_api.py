from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from api import create_app
from functions import create_ticket, list_tickets, ticket_history


class IdempotencyApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.payload = {"title": "Login", "message": "Cannot sign in"}
        self.headers = {"Idempotency-Key": "submission-1"}

    def test_retry_after_a_new_app_returns_the_same_response_and_location(self):
        first = self.client.post("/tickets", json=self.payload, headers=self.headers)
        self.assertEqual(first.status_code, 201)
        location = first.headers["Location"]
        self.client.patch(location + "/status", json={"status": "closed"})
        with TestClient(create_app(self.database)) as reopened:
            retry = reopened.post("/tickets", json=self.payload, headers=self.headers)
            self.assertEqual(reopened.get(location).json()["status"], "closed")
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(retry.headers["Location"], location)
        self.assertEqual(len(list_tickets(self.database)), 1)
        self.assertEqual(len(ticket_history(first.json()["id"], self.database)), 2)

    def test_conflicting_payload_returns_409_without_exposing_original_content(self):
        first = self.client.post("/tickets", json=self.payload, headers=self.headers)
        response = self.client.post("/tickets", json={**self.payload, "message": "Different"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn("different ticket content", response.json()["detail"])
        self.assertNotIn(self.payload["message"], response.text)
        self.assertEqual(list_tickets(self.database), [first.json()])

    def test_missing_key_keeps_existing_creation_behavior(self):
        first = self.client.post("/tickets", json=self.payload)
        second = self.client.post("/tickets", json=self.payload)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertNotEqual(first.json()["id"], second.json()["id"])

    def test_invalid_keys_return_422_without_creating_a_database(self):
        for key in ("", "has space", "x" * 129, "a,b"):
            with self.subTest(key=key):
                response = self.client.post("/tickets", json=self.payload, headers={"Idempotency-Key": key})
                self.assertEqual(response.status_code, 422)
        self.assertFalse(self.database.exists())

    def test_invalid_payload_does_not_consume_the_key(self):
        response = self.client.post("/tickets", json={"title": " ", "message": "Body"}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.database.exists())
        self.assertEqual(self.client.post("/tickets", json=self.payload, headers=self.headers).status_code, 201)

    def test_http_retry_finds_a_submission_created_through_shared_storage(self):
        first = create_ticket("Login", "Cannot sign in", self.database, idempotency_key="submission-1")
        response = self.client.post("/tickets", json=self.payload, headers={"idempotency-key": "submission-1"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), first)
        self.assertEqual(len(ticket_history(first["id"], self.database)), 1)

    def test_overlapping_http_retries_return_one_ticket(self):
        barrier = threading.Barrier(4)

        def submit(_):
            with TestClient(create_app(self.database)) as client:
                barrier.wait(timeout=5)
                return client.post("/tickets", json=self.payload, headers=self.headers)

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(submit, range(4)))
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertTrue(all(response.json() == responses[0].json() for response in responses))
        self.assertEqual(len(list_tickets(self.database)), 1)
        self.assertEqual(len(ticket_history(responses[0].json()["id"], self.database)), 1)

    def test_openapi_exposes_optional_header_and_conflict_response(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/tickets"]["post"]
        parameter = next(item for item in operation["parameters"] if item["name"] == "idempotency-key")
        self.assertEqual(parameter["in"], "header")
        self.assertFalse(parameter["required"])
        self.assertIn("409", operation["responses"])


if __name__ == "__main__":
    unittest.main()
