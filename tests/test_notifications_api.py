from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend import cli
from ticket_backend.api import create_app
from ticket_backend.notifications import deliver_notifications, list_notifications
from ticket_backend.tickets import create_ticket, list_tickets
from ticket_backend.tokens import create_token


class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        headers = {"Authorization": f"Bearer {create_token('tests', self.database)['token']}"}
        self.client = TestClient(create_app(self.database), headers=headers)
        self.addCleanup(self.client.close)

    def test_email_is_returned_and_status_changes_queue_notifications(self):
        response = self.client.post("/tickets", json={"title": "Login", "message": "Broken",
                                                      "email": " pat@example.com "})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["email"], "pat@example.com")
        identifier = response.json()["id"]
        self.client.patch(f"/tickets/{identifier}/status", json={"status": "closed", "note": "Fixed"})
        self.assertEqual(self.client.get(f"/tickets/{identifier}").json()["email"], "pat@example.com")
        self.assertEqual([row["subject"] for row in list_notifications(self.database)],
                         ["Ticket #1 received: Login", "Ticket #1 is now closed: Login"])

    def test_tickets_without_email_return_null_and_queue_nothing(self):
        response = self.client.post("/tickets", json={"title": "Login", "message": "Broken"})
        self.assertIsNone(response.json()["email"])
        self.assertIsNone(self.client.get("/tickets").json()[0]["email"])
        self.assertEqual(list_notifications(self.database), [])

    def test_invalid_emails_return_422_without_creating_a_ticket(self):
        for email in ("", "pat", "pat@example.com\r\nBcc: x@y.z", 5, ["pat@example.com"], "p@" + "e" * 260 + ".com"):
            with self.subTest(email=str(email)[:40]):
                response = self.client.post("/tickets", json={"title": "Login", "message": "Broken", "email": email})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(list_tickets(self.database), [])

    def test_replay_with_a_different_email_conflicts(self):
        payload = {"title": "Login", "message": "Broken", "email": "pat@example.com"}
        headers = {"Idempotency-Key": "login-1"}
        first = self.client.post("/tickets", json=payload, headers=headers)
        self.assertEqual(self.client.post("/tickets", json=payload, headers=headers).json(), first.json())
        response = self.client.post("/tickets", json={**payload, "email": "sam@example.com"}, headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(list_notifications(self.database)), 1)

    def test_openapi_documents_the_optional_email(self):
        schemas = self.client.get("/openapi.json").json()["components"]["schemas"]
        self.assertIn("email", schemas["TicketInput"]["properties"])
        self.assertNotIn("email", schemas["TicketInput"].get("required", []))
        self.assertIn("email", schemas["TicketOutput"]["properties"])


class NotificationCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        options = ["--json"] if json_mode else []
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *options, *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_create_with_email_shows_the_contact_and_queues_a_notification(self):
        code, output, errors = self.run_command("create", "--title", "Login", "--message", "Broken",
                                                "--email", "pat@example.com")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Contact: pat@example.com", output.splitlines())
        self.assertNotIn("Contact:", self.run_command("create", "--title", "Other", "--message", "Body")[1])
        code, output, _ = self.run_command("outbox")
        self.assertEqual(output, "#1 [pending, 0 attempts] pat@example.com: Ticket #1 received: Login\n")

    def test_outbox_shows_delivery_errors_and_filters_by_status(self):
        create_ticket("Login", "Broken", self.database, email="pat@example.com")
        create_ticket("Other", "Body", self.database, email="sam@example.com")

        def send(notification):
            if notification["recipient"] == "pat@example.com":
                raise ConnectionError("Mailbox unavailable")
        deliver_notifications(send, self.database)
        output = self.run_command("outbox", "--status", "pending")[1].splitlines()
        self.assertEqual(output, ["#1 [pending, 1 attempt] pat@example.com: Ticket #1 received: Login",
                                  "  Last error: Mailbox unavailable"])
        rows = json.loads(self.run_command("outbox", "--status", "sent", json_mode=True)[1])
        self.assertEqual([(row["recipient"], row["status"]) for row in rows], [("sam@example.com", "sent")])
        self.assertEqual(self.run_command("outbox", "--status", "failed")[1], "No notifications found.\n")

    def test_invalid_email_and_paging_exit_with_code_2(self):
        code, output, errors = self.run_command("create", "--title", "Login", "--message", "Broken",
                                                "--email", "not an address")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Email must be", errors)
        self.assertEqual(self.run_command("outbox", "--limit", "0")[0], 2)
        self.assertEqual(list_tickets(self.database), [])

    def test_json_create_includes_the_email(self):
        code, output, _ = self.run_command("create", "--title", "Login", "--message", "Broken",
                                           "--email", "pat@example.com", json_mode=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["email"], "pat@example.com")


if __name__ == "__main__":
    unittest.main()
