from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.tickets import add_comment, create_ticket, list_comments
from ticket_backend.tokens import create_token, rotate_token


class CommentEditApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.client = TestClient(create_app(self.database))
        self.addCleanup(self.client.close)
        self.alice = self.bearer(create_token("alice", self.database)["token"])
        self.bob = self.bearer(create_token("bob", self.database)["token"])
        self.ticket = create_ticket("Login", "Cannot sign in", self.database)["id"]
        self.comments = f"/tickets/{self.ticket}/comments"
        self.comment = self.client.post(self.comments, json={"body": "Asked for logz"}, headers=self.alice).json()
        self.url = f"{self.comments}/{self.comment['id']}"

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    def test_authors_edit_their_comments(self):
        response = self.client.patch(self.url, json={"body": " Asked for logs "}, headers=self.alice)
        self.assertEqual(response.status_code, 200)
        edited = response.json()
        self.assertEqual((edited["body"], edited["author"], edited["edited_by"]), ("Asked for logs", "alice", "alice"))
        self.assertIsNotNone(edited["edited_at"])
        self.assertIsNone(self.comment["edited_at"])
        self.assertEqual(self.client.get(self.comments, headers=self.bob).json(), [edited])
        self.assertEqual(list_comments(self.ticket, self.database), [edited])

    def test_authors_delete_their_comments(self):
        kept = self.client.post(self.comments, json={"body": "Keep"}, headers=self.alice).json()
        response = self.client.delete(self.url, headers=self.alice)
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertEqual(self.client.get(self.comments, headers=self.alice).json(), [kept])
        self.assertEqual(self.client.delete(self.url, headers=self.alice).status_code, 404)
        missing = self.client.patch(self.url, json={"body": "Back"}, headers=self.alice)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"detail": f"Comment #{self.comment['id']} was not found on ticket "
                                                    f"#{self.ticket}."})

    def test_tokens_cannot_change_other_authors_comments(self):
        local = add_comment(self.ticket, "From the terminal", self.database)
        for url in (self.url, f"{self.comments}/{local['id']}"):
            with self.subTest(url=url):
                edited = self.client.patch(url, json={"body": "Rewritten"}, headers=self.bob)
                self.assertEqual(edited.status_code, 403)
                self.assertIn("bob can only change its own comments", edited.json()["detail"])
                self.assertEqual(self.client.delete(url, headers=self.bob).status_code, 403)
        self.assertEqual([row["body"] for row in list_comments(self.ticket, self.database)],
                         ["Asked for logz", "From the terminal"])

    def test_a_rotated_token_keeps_its_comments(self):
        rotated = self.bearer(rotate_token("alice", self.database)["token"])
        self.assertEqual(self.client.patch(self.url, json={"body": "Fixed"}, headers=rotated).status_code, 200)
        self.assertEqual(self.client.patch(self.url, json={"body": "Again"}, headers=self.alice).status_code, 401)

    def test_writes_need_tickets_write_before_the_comment_is_looked_up(self):
        reader = self.bearer(create_token("reader", self.database, scopes=["tickets:read"])["token"])
        for url in (self.url, f"{self.comments}/99", "/tickets/99/comments/1"):
            with self.subTest(url=url):
                refused = self.client.patch(url, json={"body": "Nope"}, headers=reader)
                self.assertEqual(refused.status_code, 403)
                self.assertEqual(refused.headers["WWW-Authenticate"],
                                 'Bearer error="insufficient_scope", scope="tickets:write"')
                self.assertEqual(self.client.delete(url, headers=reader).status_code, 403)
        self.assertEqual(self.client.patch(self.url, json={"body": "Fixed"}).status_code, 401)
        self.assertEqual(list_comments(self.ticket, self.database)[0]["body"], "Asked for logz")

    def test_missing_and_invalid_targets_and_bodies(self):
        other = create_ticket("Billing", "Wrong invoice", self.database)["id"]
        for url, code in ((f"/tickets/{other}/comments/{self.comment['id']}", 404), (f"{self.comments}/99", 404),
                          (f"/tickets/99/comments/{self.comment['id']}", 404), (f"{self.comments}/0", 422),
                          ("/tickets/0/comments/1", 422), (f"{self.comments}/x", 422)):
            with self.subTest(url=url):
                self.assertEqual(self.client.patch(url, json={"body": "Body"}, headers=self.alice).status_code, code)
                self.assertEqual(self.client.delete(url, headers=self.alice).status_code, code)
        for payload in ({}, {"body": " "}, {"body": 5}, {"body": "x" * 5_001}, {"body": "Body", "author": "bob"}):
            with self.subTest(payload=str(payload)[:60]):
                self.assertEqual(self.client.patch(self.url, json=payload, headers=self.alice).status_code, 422)
        self.assertEqual(list_comments(self.ticket, self.database), [self.comment])

    def test_changes_and_refusals_are_audited_with_the_ticket(self):
        self.client.patch(self.url, json={"body": "Rewritten"}, headers=self.bob)
        self.client.patch(self.url, json={"body": "Fixed"}, headers=self.alice)
        self.client.delete(self.url, headers=self.alice)
        rows = [(row["token"], row["method"], row["route"], row["ticket_id"], row["status"])
                for row in list_audit(self.database)[:3]]
        route = "/tickets/{ticket_id}/comments/{comment_id}"
        self.assertEqual(rows, [("alice", "DELETE", route, self.ticket, 204),
                                ("alice", "PATCH", route, self.ticket, 200),
                                ("bob", "PATCH", route, self.ticket, 403)])

    def test_openapi_documents_the_routes(self):
        path = self.client.get("/openapi.json").json()["paths"]["/tickets/{ticket_id}/comments/{comment_id}"]
        self.assertEqual(set(path), {"patch", "delete"})
        for method in ("patch", "delete"):
            self.assertLessEqual({"401", "403", "404", "429"}, set(path[method]["responses"]))
        self.assertIn("edited_by", self.client.get("/openapi.json").json()["components"]["schemas"]["CommentOutput"]
                      ["properties"])


if __name__ == "__main__":
    unittest.main()
