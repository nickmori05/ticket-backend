from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from api import create_app
from functions import add_comment, create_ticket, get_ticket, list_comments
import tickets


class CommentApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.identifier = create_ticket("Login", "Cannot sign in", self.database)["id"]
        self.url = f"/tickets/{self.identifier}/comments"

    def test_comments_are_created_and_listed_through_shared_storage(self):
        response = self.client.post(self.url, json={"body": " Asked for logs "})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["body"], "Asked for logs")
        add_comment(self.identifier, "From terminal", self.database)
        listed = self.client.get(self.url).json()
        self.assertEqual([row["body"] for row in listed], ["Asked for logs", "From terminal"])
        self.assertEqual(listed, list_comments(self.identifier, self.database))

    def test_invalid_comment_bodies_are_rejected_without_writing(self):
        for payload in ({}, {"body": " "}, {"body": 5}, {"body": "x" * 5_001},
                        {"body": "Body", "status": "closed"}):
            with self.subTest(payload=str(payload)[:80]):
                self.assertEqual(self.client.post(self.url, json=payload).status_code, 422)
        self.assertEqual(list_comments(self.identifier, self.database), [])
        self.assertEqual(get_ticket(self.identifier, self.database)["status"], "open")

    def test_missing_tickets_return_404_and_invalid_ids_return_422(self):
        self.assertEqual(self.client.get("/tickets/99/comments").status_code, 404)
        self.assertEqual(self.client.post("/tickets/99/comments", json={"body": "Body"}).status_code, 404)
        self.assertEqual(self.client.get("/tickets/0/comments").status_code, 422)

    def test_storage_errors_return_503(self):
        broken = TestClient(create_app(self.database.with_name("directory")))
        self.addCleanup(broken.close)
        self.database.with_name("directory").mkdir()
        with self.assertLogs("api", level="ERROR"):
            response = broken.post("/tickets/1/comments", json={"body": "Body"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Ticket storage is unavailable."})


class CommentCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.identifier = str(create_ticket("Login", "Cannot sign in", self.database)["id"])

    def run_command(self, *arguments, json_mode=True):
        output, errors = io.StringIO(), io.StringIO()
        options = ["--json"] if json_mode else []
        with redirect_stdout(output), redirect_stderr(errors):
            code = tickets.main(["--database", str(self.database), *options, *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_json_comment_workflow(self):
        code, output, errors = self.run_command("comment", self.identifier, "Asked for logs")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["body"], "Asked for logs")
        rows = json.loads(self.run_command("comments", self.identifier)[1])
        self.assertEqual([row["body"] for row in rows], ["Asked for logs"])

    def test_text_output_for_comments(self):
        self.assertIn("No comments", self.run_command("comments", self.identifier, json_mode=False)[1])
        code, output, _ = self.run_command("comment", self.identifier, "Asked for logs", json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn(f"added to ticket #{self.identifier}", output)
        self.assertIn("  Asked for logs", self.run_command("comments", self.identifier, json_mode=False)[1])

    def test_errors_use_existing_exit_codes(self):
        code, output, errors = self.run_command("comment", "99", "Body")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("not found", errors)
        code, output, errors = self.run_command("comment", self.identifier, " ")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("must not be blank", errors)


if __name__ == "__main__":
    unittest.main()
