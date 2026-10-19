from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.notifications import (
    NotificationNotFoundError, count_due_notifications, deliver_notifications, list_notifications,
    retry_notifications,
)
from ticket_backend.tickets import create_ticket


def fail(notification):
    raise ConnectionError("Mailbox unavailable")


class NotificationRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        for recipient in ("pat@example.com", "sam@example.com", "kim@example.com"):
            create_ticket("Login", "Broken", self.database, email=recipient)
        # Fail the first two permanently and leave the third pending.
        deliver_notifications(fail, self.database, limit=2, max_attempts=1)

    def statuses(self):
        return [(row["status"], row["attempts"]) for row in list_notifications(self.database)]

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *(["--json"] if json_mode else []), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_retrying_requeues_every_failed_notification_with_a_fresh_budget(self):
        self.assertEqual(self.statuses(), [("failed", 1), ("failed", 1), ("pending", 0)])
        requeued = retry_notifications(self.database)
        self.assertEqual([row["id"] for row in requeued], [1, 2])
        self.assertEqual(self.statuses(), [("pending", 0), ("pending", 0), ("pending", 0)])
        self.assertEqual(requeued[0]["last_error"], "Mailbox unavailable", "The last error is kept for context")
        self.assertEqual(count_due_notifications(self.database), 3)
        self.assertEqual(deliver_notifications(lambda row: None, self.database),
                         {"sent": 3, "retrying": 0, "failed": 0})
        self.assertEqual(retry_notifications(self.database), [])

    def test_named_notifications_are_requeued_alone(self):
        self.assertEqual([row["id"] for row in retry_notifications(self.database, [2, 2])], [2])
        self.assertEqual(self.statuses(), [("failed", 1), ("pending", 0), ("pending", 0)])

    def test_missing_or_unfailed_notifications_change_nothing(self):
        with self.assertRaisesRegex(NotificationNotFoundError, "#9"):
            retry_notifications(self.database, [1, 9])
        with self.assertRaisesRegex(ValueError, "#3 is pending"):
            retry_notifications(self.database, [1, 3])
        for identifiers in ([], [0], ["1"], [True], 1):
            with self.subTest(identifiers=identifiers), self.assertRaises(ValueError):
                retry_notifications(self.database, identifiers)
        self.assertEqual(self.statuses(), [("failed", 1), ("failed", 1), ("pending", 0)])

    def test_due_count_skips_notifications_waiting_for_a_retry(self):
        deliver_notifications(fail, self.database, retry_delay=3600)
        self.assertEqual(count_due_notifications(self.database), 0)

    def test_cli_retries_all_or_named_notifications(self):
        code, output, _errors = self.run_command("retry-notifications", "2")
        self.assertEqual((code, output), (0, "#2 requeued for sam@example.com: Ticket #2 received: Login\n"))
        code, output, _errors = self.run_command("retry-notifications", json_mode=True)
        self.assertEqual((code, [row["id"] for row in json.loads(output)]), (0, [1]))
        self.assertEqual(self.run_command("retry-notifications")[1], "No failed notifications to retry.\n")

    def test_cli_reports_missing_and_unfailed_notifications(self):
        code, output, errors = self.run_command("retry-notifications", "9")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Notification #9 was not found.", errors)
        code, _output, errors = self.run_command("retry-notifications", "3")
        self.assertEqual(code, 2)
        self.assertIn("only failed notifications can be retried", errors)
        self.assertEqual(self.statuses(), [("failed", 1), ("failed", 1), ("pending", 0)])


if __name__ == "__main__":
    unittest.main()
