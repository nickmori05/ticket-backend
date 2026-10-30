from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.mailer import DeliveryError
from ticket_backend.notifications import (
    NotificationStateError, deliver_notifications, list_notifications, retry_notifications,
)
from ticket_backend.suppressions import (
    SuppressionNotFoundError, list_suppressions, remove_suppression, suppress_address,
)
from ticket_backend.tickets import create_ticket, set_status


def refuse_gone(notification):
    if notification["recipient"].lower() == "gone@example.com":
        raise DeliveryError(f"Recipient {notification['recipient']} refused: 550 5.1.1 No such user", permanent=True)


def fail(notification):
    raise ConnectionError("Mail server unreachable")


class SuppressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.sent = []

    def send(self, notification):
        self.sent.append(notification["recipient"])

    def outbox(self):
        return [(row["recipient"], row["status"], row["attempts"]) for row in list_notifications(self.database)]

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *(["--json"] if json_mode else []), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_a_permanent_refusal_suppresses_the_address_for_later_notifications(self):
        create_ticket("Login", "Broken", self.database, email="gone@example.com")
        self.assertEqual(deliver_notifications(refuse_gone, self.database), {"sent": 0, "retrying": 0, "failed": 1})
        [suppression] = list_suppressions(self.database)
        self.assertEqual({key: suppression[key] for key in ("id", "address", "reason", "detail", "notification_id")},
                         {"id": 1, "address": "gone@example.com", "reason": "refused",
                          "detail": "Recipient gone@example.com refused: 550 5.1.1 No such user",
                          "notification_id": 1})
        # Addresses match without regard to case, and other recipients are unaffected.
        create_ticket("Again", "Still broken", self.database, email="Gone@Example.com")
        create_ticket("Other", "Fine", self.database, email="pat@example.com")
        self.assertEqual(deliver_notifications(self.send, self.database), {"sent": 1, "retrying": 0, "failed": 1})
        self.assertEqual(self.sent, ["pat@example.com"])
        self.assertEqual(self.outbox(), [("gone@example.com", "failed", 1), ("Gone@Example.com", "failed", 0),
                                         ("pat@example.com", "sent", 1)])
        skipped = list_notifications(self.database)[1]
        self.assertRegex(skipped["last_error"], r"^Not sent: Gone@Example.com is suppressed \(refused, .+ UTC\)\.$")
        self.assertEqual(len(list_suppressions(self.database)), 1)

    def test_notifications_already_queued_for_a_refused_address_are_not_sent_in_the_same_run(self):
        ticket = create_ticket("Login", "Broken", self.database, email="gone@example.com")
        set_status(ticket["id"], "closed", self.database, note="Fixed")
        calls = []

        def send(notification):
            calls.append(notification["id"])
            refuse_gone(notification)
        self.assertEqual(deliver_notifications(send, self.database), {"sent": 0, "retrying": 0, "failed": 2})
        self.assertEqual(calls, [1])
        self.assertEqual(self.outbox(), [("gone@example.com", "failed", 1), ("gone@example.com", "failed", 0)])

    def test_temporary_and_exhausted_failures_do_not_suppress(self):
        create_ticket("Login", "Broken", self.database, email="busy@example.com")

        def defer(notification):
            raise DeliveryError("Recipient busy@example.com refused: 450 Mailbox busy")
        deliver_notifications(defer, self.database, max_attempts=1)
        self.assertEqual(self.outbox(), [("busy@example.com", "failed", 1)])
        self.assertEqual(list_suppressions(self.database), [])

    def test_retrying_skips_suppressed_addresses_until_the_suppression_is_removed(self):
        create_ticket("Login", "Broken", self.database, email="gone@example.com")
        create_ticket("Other", "Broken", self.database, email="pat@example.com")
        deliver_notifications(refuse_gone, self.database, limit=1)
        deliver_notifications(fail, self.database, max_attempts=1)
        self.assertEqual([row["id"] for row in retry_notifications(self.database)], [2])
        with self.assertRaisesRegex(NotificationStateError, "#1 is to a suppressed address"):
            retry_notifications(self.database, [1])
        self.assertEqual(self.outbox()[0], ("gone@example.com", "failed", 1))
        removed = remove_suppression(self.database, address="GONE@example.com")
        self.assertEqual((removed["address"], removed["reason"]), ("gone@example.com", "refused"))
        self.assertEqual([row["id"] for row in retry_notifications(self.database, [1])], [1])
        self.assertEqual(deliver_notifications(self.send, self.database), {"sent": 2, "retrying": 0, "failed": 0})

    def test_addresses_can_be_suppressed_by_hand_without_overwriting_a_refusal(self):
        create_ticket("Login", "Broken", self.database, email="gone@example.com")
        deliver_notifications(refuse_gone, self.database)
        self.assertEqual(suppress_address(" GONE@example.com ", self.database)["reason"], "refused")
        manual = suppress_address("pat@example.com", self.database)
        self.assertEqual((manual["id"], manual["address"], manual["reason"], manual["detail"],
                          manual["notification_id"]), (2, "pat@example.com", "manual", None, None))
        self.assertEqual(suppress_address("Pat@example.com", self.database), manual)
        self.assertEqual([row["address"] for row in list_suppressions(self.database)],
                         ["pat@example.com", "gone@example.com"])
        self.assertEqual([row["id"] for row in list_suppressions(self.database, 1, offset=1)], [1])
        create_ticket("Hello", "Body", self.database, email="pat@example.com")
        deliver_notifications(self.send, self.database)
        self.assertEqual(self.sent, [])

    def test_invalid_or_missing_suppressions_change_nothing(self):
        suppress_address("pat@example.com", self.database)
        for address in ("", "pat", "pat@example.com, sam@example.com", None, 5):
            with self.subTest(address=address), self.assertRaises(ValueError):
                suppress_address(address, self.database)
        with self.assertRaisesRegex(SuppressionNotFoundError, "sam@example.com"):
            remove_suppression(self.database, address="sam@example.com")
        with self.assertRaisesRegex(SuppressionNotFoundError, "#9"):
            remove_suppression(self.database, suppression_id=9)
        for arguments in ({}, {"address": "pat@example.com", "suppression_id": 1}, {"suppression_id": 0},
                          {"suppression_id": "1"}, {"address": "pat"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                remove_suppression(self.database, **arguments)
        for limit, offset in ((0, 0), (101, 0), (1, -1)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(ValueError):
                list_suppressions(self.database, limit, offset=offset)
        self.assertEqual(remove_suppression(self.database, suppression_id=1)["address"], "pat@example.com")
        self.assertEqual(list_suppressions(self.database), [])

    def test_cli_lists_adds_and_removes_suppressions(self):
        create_ticket("Login", "Broken", self.database, email="gone@example.com")
        deliver_notifications(refuse_gone, self.database)
        code, output, _errors = self.run_command("suppressions", "add", "pat@example.com")
        self.assertEqual(code, 0)
        self.assertRegex(output, r"^Notifications to pat@example.com are suppressed \(manual, .+ UTC\)\.\n$")
        code, output, _errors = self.run_command("suppressions", "list")
        self.assertEqual(code, 0)
        self.assertRegex(output, r"^#2 pat@example.com \(manual, .+ UTC\)\n#1 gone@example.com \(refused, .+ UTC\)\n"
                                 r"  Recipient gone@example.com refused: 550 5.1.1 No such user\n$")
        code, output, _errors = self.run_command("suppressions", "list", "--limit", "1", json_mode=True)
        self.assertEqual((code, [row["address"] for row in json.loads(output)]), (0, ["pat@example.com"]))
        code, output, _errors = self.run_command("suppressions", "remove", "Gone@example.com")
        self.assertEqual((code, output), (0, "Removed the suppression for gone@example.com; retry its failed "
                                             "notifications to send them.\n"))
        code, output, _errors = self.run_command("suppressions", "remove", "pat@example.com", json_mode=True)
        self.assertEqual((code, json.loads(output)["reason"]), (0, "manual"))
        self.assertEqual(self.run_command("suppressions", "list")[1], "No suppressed addresses.\n")

    def test_cli_reports_missing_and_invalid_suppressions_and_blocked_retries(self):
        create_ticket("Login", "Broken", self.database, email="gone@example.com")
        deliver_notifications(refuse_gone, self.database)
        code, output, errors = self.run_command("suppressions", "remove", "sam@example.com")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("No suppression was found for sam@example.com.", errors)
        code, _output, errors = self.run_command("suppressions", "add", "not an address")
        self.assertEqual(code, 2)
        self.assertIn("Email must be", errors)
        code, _output, errors = self.run_command("retry-notifications", "1")
        self.assertEqual(code, 2)
        self.assertIn("suppressed address", errors)
        self.assertEqual(self.run_command("retry-notifications")[1], "No failed notifications to retry.\n")


if __name__ == "__main__":
    unittest.main()
