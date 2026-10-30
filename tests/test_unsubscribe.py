from pathlib import Path
import tempfile
import unittest

from ticket_backend.notifications import deliver_notifications
from ticket_backend.suppressions import list_suppressions, suppress_address
from ticket_backend.tickets import create_ticket
from ticket_backend.unsubscribe import find_unsubscribe_ticket, secret_from_environment, unsubscribe, unsubscribe_token


SECRET = "s" * 40
OTHER_SECRET = "t" * 40


class UnsubscribeTokenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = create_ticket("Login", "Broken", self.database, email="Pat@Example.com")
        create_ticket("No email", "Body", self.database)
        self.token = unsubscribe_token(SECRET, self.ticket["id"], self.ticket["email"])

    def test_tokens_name_the_ticket_and_sign_its_address(self):
        self.assertRegex(self.token, r"^1\.[A-Za-z0-9_-]{43}$")
        self.assertNotIn("example", self.token.lower(), "The address must not appear in the link")
        self.assertEqual(unsubscribe_token(SECRET, 1, "pat@example.com"), self.token, "Case must not matter")
        for other in (unsubscribe_token(OTHER_SECRET, 1, "pat@example.com"),
                      unsubscribe_token(SECRET, 2, "pat@example.com"),
                      unsubscribe_token(SECRET, 1, "sam@example.com")):
            self.assertNotEqual(other, self.token)
        self.assertEqual(find_unsubscribe_ticket(self.token, SECRET, self.database),
                         {"id": 1, "email": "Pat@Example.com"})

    def test_forged_outdated_and_malformed_tokens_are_refused(self):
        signature = self.token.split(".")[1]
        tampered = signature[:-1] + ("A" if signature[-1] != "A" else "B")
        for token, secret in (
            (self.token, OTHER_SECRET),                          # the secret was changed
            (f"1.{tampered}", SECRET),
            (f"2.{signature}", SECRET),                           # moved to a ticket without an email
            (f"3.{signature}", SECRET),                           # a ticket that does not exist
            (unsubscribe_token(SECRET, 2, "pat@example.com"), SECRET),
            (f"01.{signature}", SECRET), (f"1.{signature}=", SECRET), (f"1.{signature[:-1]}", SECRET),
            (f"{2**63}.{signature}", SECRET), (f"{'9' * 20}.{signature}", SECRET),
            ("", SECRET), (signature, SECRET), (None, SECRET), (1, SECRET),
        ):
            with self.subTest(token=token):
                self.assertIsNone(find_unsubscribe_ticket(token, secret, self.database))
                self.assertIsNone(unsubscribe(token, secret, self.database))
        self.assertEqual(list_suppressions(self.database), [])

    def test_unsubscribing_suppresses_the_address_once(self):
        suppression = unsubscribe(self.token, SECRET, self.database)
        self.assertEqual((suppression["address"], suppression["reason"], suppression["detail"]),
                         ("Pat@Example.com", "unsubscribed",
                          "Unsubscribed from a link in a notification about ticket #1."))
        self.assertEqual(unsubscribe(self.token, SECRET, self.database), suppression)
        self.assertEqual(len(list_suppressions(self.database)), 1)
        self.assertEqual(deliver_notifications(lambda notification: None, self.database),
                         {"sent": 0, "retrying": 0, "failed": 1})

    def test_an_existing_suppression_keeps_its_reason(self):
        suppress_address("pat@example.com", self.database)
        self.assertEqual(unsubscribe(self.token, SECRET, self.database)["reason"], "manual")
        with self.assertRaisesRegex(ValueError, "reason"):
            suppress_address("sam@example.com", self.database, reason="refused")

    def test_secrets_must_be_long_enough(self):
        self.assertIsNone(secret_from_environment({}))
        self.assertIsNone(secret_from_environment({"TICKETS_UNSUBSCRIBE_SECRET": "  "}))
        self.assertEqual(secret_from_environment({"TICKETS_UNSUBSCRIBE_SECRET": SECRET}), SECRET)
        with self.assertRaisesRegex(ValueError, "at least 32"):
            secret_from_environment({"TICKETS_UNSUBSCRIBE_SECRET": "short"})


if __name__ == "__main__":
    unittest.main()
