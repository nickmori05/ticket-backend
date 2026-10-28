from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.database import connect
from ticket_backend.notifications import deliver_notifications, list_notifications, prune_notifications
from ticket_backend.tickets import create_ticket


def fail(notification):
    raise ConnectionError("Mailbox unavailable")


class NotificationRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        for recipient in ("pat@example.com", "sam@example.com", "kim@example.com", "lee@example.com"):
            create_ticket("Login", "Broken", self.database, email=recipient)
        # Send the first two, fail the third permanently, and leave the fourth pending.
        deliver_notifications(lambda notification: None, self.database, limit=2)
        deliver_notifications(fail, self.database, limit=1, max_attempts=1)

    def age(self, notification_id, days):
        with closing(connect(self.database)) as connection, connection:
            connection.execute(
                """UPDATE ticket_notifications SET sent_at = datetime(sent_at, ?),
                   created_at = datetime(created_at, ?) WHERE id = ?""",
                (f"-{days} days", f"-{days} days", notification_id),
            )

    def remaining(self):
        return [(row["id"], row["status"]) for row in list_notifications(self.database)]

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *(["--json"] if json_mode else []), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_pruning_deletes_only_notifications_sent_before_the_cutoff(self):
        self.age(1, 40)
        self.age(2, 10)
        self.assertEqual(prune_notifications(30, self.database), {"deleted": 1})
        self.assertEqual(self.remaining(), [(2, "sent"), (3, "failed"), (4, "pending")])

    def test_pending_and_failed_notifications_are_kept_whatever_their_age(self):
        for notification_id in (1, 2, 3, 4):
            self.age(notification_id, 400)
        self.assertEqual(prune_notifications(1, self.database), {"deleted": 2})
        self.assertEqual(self.remaining(), [(3, "failed"), (4, "pending")])

    def test_ages_outside_the_allowed_range_are_rejected(self):
        for days in (0, -1, 3651, 1.5, "30", True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                prune_notifications(days, self.database)
        self.assertEqual(len(self.remaining()), 4)

    def test_command_reports_the_count_as_text_and_json(self):
        self.age(1, 40)
        self.age(2, 40)
        self.assertEqual(self.run_command("prune-notifications", "--older-than", "60"),
                         (0, "Deleted 0 sent notifications.\n", ""))
        code, output, _ = self.run_command("prune-notifications", "--older-than", "30", json_mode=True)
        self.assertEqual((code, json.loads(output)), (0, {"deleted": 2}))

    def test_command_rejects_an_invalid_age_with_exit_code_2(self):
        code, output, errors = self.run_command("prune-notifications", "--older-than", "0")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("1 to 3650", errors)


if __name__ == "__main__":
    unittest.main()
