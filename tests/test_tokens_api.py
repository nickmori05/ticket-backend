from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend import cli
from ticket_backend.api import create_app
from ticket_backend.tickets import create_ticket, list_comments, list_tickets
from ticket_backend.tokens import authenticate, create_token, revoke_token


class TokenApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.token = create_token("alice", self.database)["token"]
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.identifier = create_ticket("Login", "Cannot sign in", self.database)["id"]

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_every_ticket_route_requires_a_token(self):
        requests = (
            ("POST", "/tickets", {"title": "New", "message": "Body"}),
            ("GET", "/tickets", None),
            ("GET", f"/tickets/{self.identifier}", None),
            ("PATCH", f"/tickets/{self.identifier}/status", {"status": "closed"}),
            ("GET", f"/tickets/{self.identifier}/history", None),
            ("POST", f"/tickets/{self.identifier}/comments", {"body": "Note"}),
            ("GET", f"/tickets/{self.identifier}/comments", None),
            ("GET", "/stats", None),
        )
        for method, path, payload in requests:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(response.json(), {"detail": "A valid API token is required."})
        self.assertEqual(len(list_tickets(self.database)), 1)
        self.assertEqual(list_comments(self.identifier, self.database), [])

    def test_health_and_docs_stay_public(self):
        for path in ("/health", "/docs", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_wrong_scheme_unknown_and_revoked_tokens_are_rejected(self):
        for headers in ({"Authorization": self.token}, {"Authorization": f"Basic {self.token}"},
                        self.bearer(self.token + "x"), self.bearer("")):
            with self.subTest(headers=str(headers)[:30]):
                self.assertEqual(self.client.get("/stats", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/stats", headers=self.bearer(self.token)).status_code, 200)
        revoke_token("alice", self.database)
        self.assertEqual(self.client.get("/stats", headers=self.bearer(self.token)).status_code, 401)

    def test_missing_token_is_checked_before_ticket_lookup(self):
        self.assertEqual(self.client.get("/tickets/99").status_code, 401)
        self.assertEqual(self.client.get("/tickets/99", headers=self.bearer(self.token)).status_code, 404)

    def test_comments_are_attributed_to_the_token_name(self):
        bob = create_token("bob", self.database)["token"]
        url = f"/tickets/{self.identifier}/comments"
        self.client.post(url, json={"body": "From Alice"}, headers=self.bearer(self.token))
        self.client.post(url, json={"body": "From Bob"}, headers=self.bearer(bob))
        listed = self.client.get(url, headers=self.bearer(bob)).json()
        self.assertEqual([(row["body"], row["author"]) for row in listed],
                         [("From Alice", "alice"), ("From Bob", "bob")])

    def test_author_cannot_be_supplied_in_the_body(self):
        response = self.client.post(f"/tickets/{self.identifier}/comments", headers=self.bearer(self.token),
                                    json={"body": "Note", "author": "bob"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(list_comments(self.identifier, self.database), [])

    def test_openapi_declares_bearer_security(self):
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"], "bearer")
        self.assertIn("401", schema["paths"]["/tickets"]["post"]["responses"])
        self.assertNotIn("security", schema["paths"]["/health"]["get"])


class TokenCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments, json_mode=True):
        output, errors = io.StringIO(), io.StringIO()
        options = ["--json"] if json_mode else []
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *options, *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_json_token_lifecycle(self):
        code, output, errors = self.run_command("token", "create", "alice")
        self.assertEqual((code, errors), (0, ""))
        created = json.loads(output)
        self.assertEqual(authenticate(created["token"], self.database), "alice")
        listed = json.loads(self.run_command("token", "list")[1])
        self.assertEqual([row["name"] for row in listed], ["alice"])
        self.assertNotIn("token", listed[0])
        code, output, _ = self.run_command("token", "revoke", "alice")
        self.assertEqual(code, 0)
        self.assertIsNotNone(json.loads(output)["revoked_at"])
        self.assertIsNone(authenticate(created["token"], self.database))

    def test_text_output_shows_the_token_once(self):
        self.assertIn("No API tokens", self.run_command("token", "list", json_mode=False)[1])
        code, output, _ = self.run_command("token", "create", "alice", json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn("Token for alice: tkt_", output)
        self.assertIn("cannot be shown again", output)
        listed = self.run_command("token", "list", json_mode=False)[1]
        self.assertIn("alice", listed)
        self.assertIn("active", listed)
        self.assertNotIn("tkt_", listed)
        self.run_command("token", "revoke", "alice", json_mode=False)
        self.assertIn("revoked", self.run_command("token", "list", json_mode=False)[1])

    def test_errors_use_existing_exit_codes(self):
        self.run_command("token", "create", "alice")
        for arguments, expected in ((("token", "create", "alice"), "already exists"),
                                    (("token", "create", "bad name"), "Token name"),
                                    (("token", "revoke", "nobody"), "not found")):
            with self.subTest(arguments=arguments):
                code, output, errors = self.run_command(*arguments)
                self.assertEqual(output, "")
                self.assertEqual(code, 1 if expected == "not found" else 2)
                self.assertIn(expected, errors)


if __name__ == "__main__":
    unittest.main()
