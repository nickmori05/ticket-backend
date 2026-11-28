from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from ticket_backend import cli
from ticket_backend.api import create_app
from ticket_backend.database import connect
from ticket_backend.tickets import add_comment, create_ticket, list_comments, set_status
from ticket_backend.tokens import create_token


class CommentIdempotencyApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.authorization = {"Authorization": f"Bearer {create_token('tests', self.database)['token']}"}
        self.client = TestClient(create_app(self.database), headers=self.authorization)
        self.addCleanup(self.client.close)
        self.ticket = create_ticket("Login", "Cannot sign in", self.database)
        self.url = f"/tickets/{self.ticket['id']}/comments"
        self.payload = {"body": "Asked for logs"}
        self.headers = {"Idempotency-Key": "comment-1"}

    def test_retry_returns_the_same_comment_after_the_ticket_changes(self):
        first = self.client.post(self.url, json=self.payload, headers=self.headers)
        self.assertEqual(first.status_code, 201)
        set_status(self.ticket["id"], "closed", self.database)
        with TestClient(create_app(self.database), headers=self.authorization) as reopened:
            retry = reopened.post(self.url, json={"body": " Asked for logs "}, headers=self.headers)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(list_comments(self.ticket["id"], self.database), [first.json()])

    def test_conflicting_body_returns_409_without_exposing_the_original(self):
        first = self.client.post(self.url, json=self.payload, headers=self.headers)
        response = self.client.post(self.url, json={"body": "Different note"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn("different comment", response.json()["detail"])
        self.assertNotIn(self.payload["body"], response.text)
        self.assertEqual(list_comments(self.ticket["id"], self.database), [first.json()])

    def test_the_same_key_on_another_ticket_is_a_new_comment(self):
        other = create_ticket("Billing", "Wrong invoice", self.database)
        first = self.client.post(self.url, json=self.payload, headers=self.headers)
        second = self.client.post(f"/tickets/{other['id']}/comments", json={"body": "Billing note"},
                                  headers=self.headers)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.client.post(self.url, json=self.payload, headers=self.headers).json(), first.json())

    def test_missing_key_keeps_adding_a_comment_on_every_call(self):
        first = self.client.post(self.url, json=self.payload)
        second = self.client.post(self.url, json=self.payload)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertNotEqual(first.json()["id"], second.json()["id"])

    def test_invalid_keys_return_422_without_adding_a_comment(self):
        for key in ("", "has space", "x" * 129, "a,b"):
            with self.subTest(key=key):
                response = self.client.post(self.url, json=self.payload, headers={"Idempotency-Key": key})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(list_comments(self.ticket["id"], self.database), [])

    def test_invalid_body_does_not_consume_the_key(self):
        response = self.client.post(self.url, json={"body": " "}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(list_comments(self.ticket["id"], self.database), [])
        self.assertEqual(self.client.post(self.url, json=self.payload, headers=self.headers).status_code, 201)

    def test_a_missing_ticket_does_not_reserve_the_key(self):
        missing = self.client.post("/tickets/99/comments", json=self.payload, headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        with closing(connect(self.database)) as connection:
            saved = connection.execute("SELECT COUNT(*) FROM ticket_comment_keys").fetchone()[0]
        self.assertEqual(saved, 0)
        self.assertEqual(self.client.post(self.url, json=self.payload, headers=self.headers).status_code, 201)

    def test_http_retry_finds_a_comment_stored_for_the_same_token(self):
        first = add_comment(self.ticket["id"], "Asked for logs", self.database,
                            author="tests", idempotency_key="comment-1")
        response = self.client.post(self.url, json=self.payload, headers={"idempotency-key": "comment-1"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), first)
        self.assertEqual(len(list_comments(self.ticket["id"], self.database)), 1)

    def test_another_token_does_not_replay_this_tokens_comment(self):
        first = self.client.post(self.url, json=self.payload, headers=self.headers)
        other = {"Authorization": f"Bearer {create_token('other', self.database)['token']}"}
        with TestClient(create_app(self.database), headers=other) as client:
            response = client.post(self.url, json={"body": "Different note"}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()["id"], first.json()["id"])
        self.assertEqual(response.json()["author"], "other")
        self.assertEqual(len(list_comments(self.ticket["id"], self.database)), 2)

    def test_overlapping_http_retries_return_one_comment(self):
        barrier = threading.Barrier(4)

        def submit(_):
            with TestClient(create_app(self.database), headers=self.authorization) as client:
                barrier.wait(timeout=5)
                return client.post(self.url, json=self.payload, headers=self.headers)

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(submit, range(4)))
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertTrue(all(response.json() == responses[0].json() for response in responses))
        self.assertEqual(len(list_comments(self.ticket["id"], self.database)), 1)

    def test_openapi_exposes_optional_header_and_conflict_response(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/tickets/{ticket_id}/comments"]["post"]
        parameter = next(item for item in operation["parameters"] if item["name"] == "idempotency-key")
        self.assertEqual(parameter["in"], "header")
        self.assertFalse(parameter["required"])
        self.assertIn("409", operation["responses"])


class CommentIdempotencyCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.identifier = str(create_ticket("Login", "Cannot sign in", self.database)["id"])

    def run_command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), "--json", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_json_retry_returns_the_original_comment(self):
        arguments = ("comment", self.identifier, "Asked for logs", "--idempotency-key", "cli-comment-1")
        code, output, errors = self.run_command(*arguments)
        self.assertEqual((code, errors), (0, ""))
        first = json.loads(output)
        code, output, errors = self.run_command(*arguments)
        self.assertEqual((code, errors, json.loads(output)), (0, "", first))
        listed = json.loads(self.run_command("comments", self.identifier)[1])
        self.assertEqual(listed, [first])

    def test_a_different_body_exits_3_without_adding_a_comment(self):
        arguments = ("comment", self.identifier, "--idempotency-key", "cli-comment-1")
        code, output, errors = self.run_command(*arguments, "First")
        self.assertEqual((code, errors), (0, ""))
        code, output, errors = self.run_command(*arguments, "Different")
        self.assertEqual((code, output), (3, ""))
        self.assertIn("different comment", errors)
        self.assertEqual(len(json.loads(self.run_command("comments", self.identifier)[1])), 1)

    def test_an_invalid_key_exits_2_without_adding_a_comment(self):
        code, output, errors = self.run_command("comment", self.identifier, "Body", "--idempotency-key", " ")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Idempotency key", errors)
        self.assertEqual(json.loads(self.run_command("comments", self.identifier)[1]), [])


if __name__ == "__main__":
    unittest.main()
