"""HTTP status changes send the ticket revision as an ETag and can require it with If-Match."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.tickets import create_ticket, get_ticket, ticket_history, ticket_revision
from ticket_backend.tokens import create_token


class RevisionApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        headers = {"Authorization": f"Bearer {create_token('tests', self.database)['token']}"}
        self.client = TestClient(create_app(self.database), headers=headers)
        self.addCleanup(self.client.close)

    def submit(self):
        return self.client.post("/tickets", json={"title": "Login", "message": "Cannot sign in"})

    def test_creation_response_has_no_etag(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("etag", response.headers)

    def test_stale_if_match_returns_412_and_changes_nothing(self):
        identifier = self.submit().json()["id"]
        url = f"/tickets/{identifier}/status"
        original = self.client.get(f"/tickets/{identifier}").headers["ETag"]
        self.assertEqual(original, f'"{ticket_revision(identifier, self.database)}"')
        moved = self.client.patch(url, json={"status": "closed", "note": "Fixed"}, headers={"If-Match": original})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.headers["ETag"], f'"{ticket_revision(identifier, self.database)}"')
        self.assertNotEqual(moved.headers["ETag"], original)
        stale = self.client.patch(url, json={"status": "open", "note": "Too late"}, headers={"If-Match": original})
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(stale.headers["ETag"], moved.headers["ETag"])
        self.assertIn("current revision", stale.json()["detail"])
        self.assertEqual(get_ticket(identifier, self.database)["status"], "closed")
        self.assertEqual([event["note"] for event in ticket_history(identifier, self.database)], ["", "Fixed"])

    def test_wildcard_list_and_weak_tags_match_and_malformed_tags_do_not(self):
        identifier = self.submit().json()["id"]
        url = f"/tickets/{identifier}/status"
        wildcard = self.client.patch(url, json={"status": "in_progress"}, headers={"If-Match": "*"})
        self.assertEqual(wildcard.status_code, 200)
        current = wildcard.headers["ETag"]
        listed = self.client.patch(url, json={"status": "closed"}, headers={"If-Match": f'"0", {current}'})
        self.assertEqual(listed.status_code, 200)
        weak = self.client.get(f"/tickets/{identifier}").headers["ETag"]
        same = self.client.patch(url, json={"status": "closed", "note": "Again"}, headers={"If-Match": f"W/{weak}"})
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.headers["ETag"], weak)
        self.assertEqual([event["to_status"] for event in ticket_history(identifier, self.database)],
                         ["open", "in_progress", "closed"])
        for header in ("3", "W/3", '"nope"', "", "*, " + weak, f"{weak}, *", '"9999999999999999999"', '"00"'):
            with self.subTest(header=header):
                response = self.client.patch(url, json={"status": "open"}, headers={"If-Match": header})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(get_ticket(identifier, self.database)["status"], "closed")

    def test_omitted_if_match_overwrites_and_a_missing_ticket_is_404(self):
        identifier = self.submit().json()["id"]
        response = self.client.patch(f"/tickets/{identifier}/status", json={"status": "closed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["ETag"], f'"{ticket_revision(identifier, self.database)}"')
        missing = self.client.patch("/tickets/99/status", json={"status": "closed"}, headers={"If-Match": '"1"'})
        self.assertEqual(missing.status_code, 404)

    def test_comments_do_not_change_the_etag(self):
        identifier = self.submit().json()["id"]
        etag = self.client.get(f"/tickets/{identifier}").headers["ETag"]
        self.assertEqual(self.client.post(f"/tickets/{identifier}/comments", json={"body": "Note"}).status_code, 201)
        self.assertEqual(self.client.get(f"/tickets/{identifier}").headers["ETag"], etag)
        response = self.client.patch(f"/tickets/{identifier}/status", json={"status": "closed"},
                                     headers={"If-Match": etag})
        self.assertEqual(response.status_code, 200)

    def test_etag_matches_a_ticket_created_from_storage(self):
        ticket = create_ticket("From terminal", "Shared database", self.database)
        response = self.client.get(f"/tickets/{ticket['id']}")
        self.assertEqual(response.json(), ticket)
        self.assertEqual(response.headers["ETag"], f'"{ticket_revision(ticket["id"], self.database)}"')


if __name__ == "__main__":
    unittest.main()
