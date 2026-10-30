import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.notifications import deliver_notifications
from ticket_backend.suppressions import list_suppressions
from ticket_backend.tickets import create_ticket
from ticket_backend.tokens import create_token
from ticket_backend.unsubscribe import unsubscribe_token


SECRET = "s" * 40


class UnsubscribeApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        create_ticket("Login", "Broken", self.database, email="pat@example.com")
        self.client = self.app(SECRET)
        self.path = f"/unsubscribe/{unsubscribe_token(SECRET, 1, 'pat@example.com')}"

    def app(self, secret):
        client = TestClient(create_app(self.database, rate_limit=0, unsubscribe_secret=secret))
        self.addCleanup(client.close)
        return client

    def assert_page(self, response, status, text):
        self.assertEqual(response.status_code, status)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual((response.headers["cache-control"], response.headers["referrer-policy"]),
                         ("no-store", "no-referrer"))
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertIn(text, response.text)

    def test_get_asks_for_confirmation_without_unsubscribing(self):
        response = self.client.get(self.path)
        self.assert_page(response, 200, '<form method="post">')
        self.assertNotIn("pat@example.com", response.text, "The page must not reveal the address")
        self.assertEqual(list_suppressions(self.database), [])

    def test_post_unsubscribes_once_without_a_token(self):
        self.assert_page(self.client.post(self.path), 200, "You will no longer receive emails")
        [suppression] = list_suppressions(self.database)
        self.assertEqual((suppression["address"], suppression["reason"]), ("pat@example.com", "unsubscribed"))
        # A mail client's one-click request, and a repeat, both succeed without changing the suppression.
        response = self.client.post(self.path, content="List-Unsubscribe=One-Click",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assert_page(response, 200, "Unsubscribed")
        self.assertEqual(list_suppressions(self.database), [suppression])
        self.assertEqual(deliver_notifications(lambda notification: None, self.database),
                         {"sent": 0, "retrying": 0, "failed": 1})

    def test_invalid_links_get_the_same_404_and_change_nothing(self):
        signature = self.path.rsplit(".", 1)[1]
        create_ticket("No email", "Body", self.database)
        for path in (f"/unsubscribe/1.{signature[::-1]}", f"/unsubscribe/2.{signature}",
                     f"/unsubscribe/9.{signature}", "/unsubscribe/1", "/unsubscribe/abc",
                     f"/unsubscribe/{unsubscribe_token('t' * 40, 1, 'pat@example.com')}",
                     f"/unsubscribe/1.{signature}x", f"/unsubscribe/{'9' * 25}.{signature}"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    self.assert_page(self.client.request(method, path), 404, "This unsubscribe link is not valid.")
        self.assertEqual(list_suppressions(self.database), [])

    def test_links_are_refused_when_no_secret_is_configured(self):
        with patch.dict(os.environ, {"TICKETS_UNSUBSCRIBE_SECRET": ""}):
            client = self.app(None)
        for method in ("GET", "POST"):
            self.assertEqual(client.request(method, self.path).status_code, 404)
        self.assertEqual(list_suppressions(self.database), [])

    def test_the_secret_defaults_to_the_environment_and_must_be_long_enough(self):
        with patch.dict(os.environ, {"TICKETS_UNSUBSCRIBE_SECRET": SECRET}):
            client = self.app(None)
        self.assertEqual(client.post(self.path).status_code, 200)
        with self.assertRaisesRegex(ValueError, "at least 32"):
            create_app(self.database, unsubscribe_secret="short")
        with patch.dict(os.environ, {"TICKETS_UNSUBSCRIBE_SECRET": "short"}), \
                self.assertRaisesRegex(ValueError, "at least 32"):
            create_app(self.database)

    def test_unsubscribing_is_not_audited_and_shows_in_the_suppression_list(self):
        headers = {"Authorization": f"Bearer {create_token('operator', self.database)['token']}"}
        self.client.post(self.path, headers=headers)
        self.assertEqual(list_audit(self.database), [], "A token sent to the page must not be recorded or needed")
        [row] = self.client.get("/suppressions", headers=headers).json()
        self.assertEqual((row["address"], row["reason"], row["detail"]),
                         ("pat@example.com", "unsubscribed",
                          "Unsubscribed from a link in a notification about ticket #1."))

    def test_routes_are_documented_as_html(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        operations = paths["/unsubscribe/{token}"]
        self.assertEqual(set(operations), {"get", "post"})
        self.assertIn("text/html", operations["post"]["responses"]["200"]["content"])
        self.assertNotIn("security", operations["post"])


if __name__ == "__main__":
    unittest.main()
