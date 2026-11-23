"""Status changes can name the revision they read, so a stale one does not overwrite a newer change."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ticket_backend import cli
from ticket_backend.database import connect
from ticket_backend.notifications import list_notifications
from ticket_backend.tickets import (
    StaleTicketError, TicketNotFoundError, add_comment, create_ticket, get_ticket, set_status, ticket_history,
    ticket_revision, ticket_state,
)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def create(self, email=None):
        return create_ticket("Login", "Cannot sign in", self.database, email=email)

    def test_revision_is_the_latest_history_id(self):
        ticket = self.create()
        events = ticket_history(ticket["id"], self.database)
        state, revision = ticket_state(ticket["id"], self.database)
        self.assertEqual(state, get_ticket(ticket["id"], self.database))
        self.assertEqual(revision, events[-1]["id"])
        self.assertEqual(ticket_revision(ticket["id"], self.database), events[-1]["id"])

    def test_stale_revision_does_not_record_a_change_or_notification(self):
        ticket = self.create(email="pat@example.com")
        revision = ticket_revision(ticket["id"], self.database)
        set_status(ticket["id"], "closed", self.database, note="Fixed", revisions=(revision,))
        current = ticket_revision(ticket["id"], self.database)
        with self.assertRaises(StaleTicketError) as raised:
            set_status(ticket["id"], "in_progress", self.database, note="Too late", revisions=(revision,))
        self.assertEqual(raised.exception.revision, current)
        # The status already matches, and the revision still does not, so the note is not recorded either.
        with self.assertRaises(StaleTicketError):
            set_status(ticket["id"], "closed", self.database, note="Also late", revisions=(revision,))
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], "closed")
        self.assertEqual([event["note"] for event in ticket_history(ticket["id"], self.database)], ["", "Fixed"])
        self.assertEqual(len(list_notifications(self.database, ticket_id=ticket["id"])), 2)

    def test_a_listed_revision_applies_and_the_same_status_records_nothing(self):
        ticket = self.create()
        revision = ticket_revision(ticket["id"], self.database)
        updated = set_status(ticket["id"], "closed", self.database, revisions=(revision - 1, revision))
        self.assertEqual(updated["status"], "closed")
        current = ticket_revision(ticket["id"], self.database)
        self.assertNotEqual(current, revision)
        set_status(ticket["id"], "closed", self.database, note="Again", revisions=(current,))
        self.assertEqual(ticket_revision(ticket["id"], self.database), current)
        self.assertEqual([event["to_status"] for event in ticket_history(ticket["id"], self.database)],
                         ["open", "closed"])

    def test_omitted_revision_still_overwrites(self):
        ticket = self.create()
        set_status(ticket["id"], "closed", self.database)
        set_status(ticket["id"], "open", self.database)
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], "open")
        self.assertEqual(len(ticket_history(ticket["id"], self.database)), 3)

    def test_comments_do_not_move_the_revision(self):
        ticket = self.create()
        revision = ticket_revision(ticket["id"], self.database)
        add_comment(ticket["id"], "Still open", self.database)
        self.assertEqual(ticket_revision(ticket["id"], self.database), revision)
        set_status(ticket["id"], "in_progress", self.database, revisions=(revision,))
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], "in_progress")

    def test_bad_revisions_are_rejected_and_a_missing_ticket_is_not_a_conflict(self):
        ticket = self.create()
        revision = ticket_revision(ticket["id"], self.database)
        for revisions in ((), (-1,), (True,), ("1",), (revision, True), (2**63,)):
            with self.subTest(revisions=revisions), self.assertRaises(ValueError):
                set_status(ticket["id"], "closed", self.database, revisions=revisions)
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], "open")
        with self.assertRaises(TicketNotFoundError):
            set_status(99, "closed", self.database, revisions=(revision,))
        with self.assertRaises(TicketNotFoundError):
            ticket_revision(99, self.database)

    def test_a_ticket_without_history_matches_revision_zero(self):
        with closing(connect(self.database)) as connection:
            with connection:
                connection.execute("INSERT INTO tickets (title, message) VALUES ('Old', 'Imported')")
        self.assertEqual(ticket_revision(1, self.database), 0)
        # The status is already open, so a matching revision records nothing until it actually changes.
        set_status(1, "closed", self.database, revisions=(0,))
        self.assertEqual(get_ticket(1, self.database)["status"], "closed")
        self.assertGreater(ticket_revision(1, self.database), 0)
        with self.assertRaises(StaleTicketError):
            set_status(1, "open", self.database, revisions=(0,))
        self.assertEqual(get_ticket(1, self.database)["status"], "closed")

    def test_concurrent_matching_revisions_have_one_winner(self):
        ticket = self.create()
        revision = ticket_revision(ticket["id"], self.database)
        barrier = threading.Barrier(2)

        def change(status):
            barrier.wait(timeout=5)
            try:
                set_status(ticket["id"], status, self.database, revisions=(revision,))
            except StaleTicketError:
                return None
            return status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ("closed", "in_progress")))
        winners = [status for status in results if status is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], winners[0])
        self.assertEqual([event["to_status"] for event in ticket_history(ticket["id"], self.database)],
                         ["open", winners[0]])


class RevisionCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments, json_mode=True):
        output, errors = io.StringIO(), io.StringIO()
        prefix = ["--database", str(self.database)]
        if json_mode:
            prefix.append("--json")
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main([*prefix, *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_text_output_shows_the_revision_and_if_match_guards_the_change(self):
        code, output, errors = self.run_command("create", "--title", "Login", "--message", "Broken", json_mode=False)
        self.assertEqual((code, errors), (0, ""))
        revision = ticket_revision(1, self.database)
        self.assertIn(f"Revision: {revision}", output)
        self.assertNotIn('"revision"', self.run_command("show", "1")[1])
        code, output, errors = self.run_command("status", "1", "closed", "--if-match", str(revision))
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["status"], "closed")
        code, output, errors = self.run_command("status", "1", "open", "--note", "late", "--if-match", str(revision))
        self.assertEqual((code, output), (5, ""))
        self.assertIn(f"current revision is {ticket_revision(1, self.database)}", errors)
        self.assertEqual(json.loads(self.run_command("show", "1")[1])["status"], "closed")
        self.assertEqual(len(json.loads(self.run_command("history", "1")[1])), 2)

    def test_invalid_if_match_is_rejected_without_changing_the_ticket(self):
        self.run_command("create", "--title", "Login", "--message", "Broken")
        code, output, errors = self.run_command("status", "1", "closed", "--if-match", "-1")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Revision must be a non-negative integer", errors)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--database", str(self.database), "status", "1", "closed", "--if-match", "nope"])
        self.assertEqual(json.loads(self.run_command("show", "1")[1])["status"], "open")


if __name__ == "__main__":
    unittest.main()
