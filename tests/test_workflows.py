from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from db import connect
from functions import (
    TicketNotFoundError, create_ticket, get_ticket, list_tickets,
    set_status, statistics, ticket_history,
)
import tickets


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def create(self, title="Login", message="Cannot sign in"):
        return create_ticket(title, message, self.database)

    def test_status_history_records_creation_resolution_and_reopening(self):
        identifier = self.create()["id"]
        set_status(identifier, "in_progress", self.database, note="Investigating")
        set_status(identifier, "closed", self.database, note="Fixed")
        set_status(identifier, "open", self.database, note="Still happening")
        events = ticket_history(identifier, self.database)
        self.assertEqual([event["to_status"] for event in events],
                         ["open", "in_progress", "closed", "open"])
        self.assertEqual([event["from_status"] for event in events],
                         [None, "open", "in_progress", "closed"])
        self.assertEqual(events[-1]["note"], "Still happening")
        self.assertEqual(get_ticket(identifier, self.database)["status"], "open")

    def test_repeated_status_does_not_add_an_event(self):
        identifier = self.create()["id"]
        set_status(identifier, "closed", self.database)
        set_status(identifier, "closed", self.database)
        self.assertEqual(len(ticket_history(identifier, self.database)), 2)

    def test_failed_event_insert_rolls_back_the_status_change(self):
        identifier = self.create()["id"]
        with closing(connect(self.database)) as connection:
            connection.executescript("""
                CREATE TRIGGER reject_event BEFORE INSERT ON ticket_events
                BEGIN SELECT RAISE(ABORT, 'Simulated event failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            set_status(identifier, "closed", self.database)
        self.assertEqual(get_ticket(identifier, self.database)["status"], "open")
        self.assertEqual(len(ticket_history(identifier, self.database)), 1)

    def test_search_filters_and_pagination(self):
        first = self.create("Login failure", "Mobile browser")
        second = self.create("Billing", "Login issue on desktop")
        self.create("Other", "Unrelated")
        set_status(second["id"], "closed", self.database)
        self.assertEqual([row["id"] for row in list_tickets(self.database, search="LOGIN")],
                         [second["id"], first["id"]])
        self.assertEqual([row["id"] for row in list_tickets(
            self.database, search="login", status="open")], [first["id"]])
        self.assertEqual(list_tickets(self.database, limit=1, offset=1)[0]["id"], second["id"])
        self.assertEqual(list_tickets(self.database, offset=100), [])

    def test_search_treats_wildcards_and_sql_as_literal_text(self):
        self.create("100% complete", "O'Brien's issue")
        self.create("Another", "Ordinary")
        self.assertEqual(len(list_tickets(self.database, search="%")), 1)
        self.assertEqual(len(list_tickets(self.database, search="O'Brien")), 1)
        self.assertEqual(list_tickets(self.database, search="' OR 1=1 --"), [])

    def test_stats_include_zero_counts(self):
        self.assertEqual(statistics(self.database), {
            "total": 0, "by_status": {"open": 0, "in_progress": 0, "closed": 0}})
        identifier = self.create()["id"]
        self.create("Other", "Another ticket")
        set_status(identifier, "closed", self.database)
        self.assertEqual(statistics(self.database), {
            "total": 2, "by_status": {"open": 1, "in_progress": 0, "closed": 1}})

    def test_invalid_changes_leave_ticket_unchanged(self):
        identifier = self.create()["id"]
        for arguments in ({"status": "unknown"}, {"status": "closed", "note": "x" * 501}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    set_status(identifier, database=self.database, **arguments)
        for operation in (lambda: set_status(99, "closed", self.database),
                          lambda: ticket_history(99, self.database)):
            with self.assertRaises(TicketNotFoundError):
                operation()
        self.assertEqual(get_ticket(identifier, self.database)["status"], "open")

    def test_invalid_filters_are_rejected(self):
        for filters in ({"offset": -1}, {"status": "unknown"}, {"search": " "}):
            with self.subTest(filters=filters):
                with self.assertRaises(ValueError):
                    list_tickets(self.database, **filters)

    def test_concurrent_submissions_keep_distinct_ids_and_complete_histories(self):
        with closing(connect(self.database)):
            pass
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda number: self.create(f"Ticket {number}", "Body"), range(12)))
        self.assertEqual(len({row["id"] for row in rows}), 12)
        self.assertEqual(statistics(self.database)["total"], 12)
        for row in rows:
            self.assertEqual(len(ticket_history(row["id"], self.database)), 1)


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def run_command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = tickets.main(["--database", str(self.database), "--json", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_complete_json_cli_workflow(self):
        code, output, errors = self.run_command("create", "--title", "Login", "--message", "Broken")
        self.assertEqual((code, errors), (0, ""))
        identifier = str(json.loads(output)["id"])
        self.assertEqual(json.loads(self.run_command("show", identifier)[1])["title"], "Login")
        self.assertEqual(self.run_command("status", identifier, "closed", "--note", "Fixed")[0], 0)
        self.assertEqual(len(json.loads(self.run_command("history", identifier)[1])), 2)
        self.assertEqual(json.loads(self.run_command("stats")[1])["by_status"]["closed"], 1)
        self.assertEqual(json.loads(self.run_command("list", "--status", "open")[1]), [])

    def test_interactive_submission(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["Login", "Broken"]), redirect_stdout(output):
            code = tickets.main(["--database", str(self.database), "create"])
        self.assertEqual(code, 0)
        self.assertIn("#1 [open] Login", output.getvalue())

    def test_errors_use_stderr_and_meaningful_exit_codes(self):
        code, output, errors = self.run_command("show", "99")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("not found", errors)
        code, output, errors = self.run_command("create", "--title", " ", "--message", "Body")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("must not be blank", errors)

    def test_json_mode_never_prompts_for_missing_fields(self):
        with patch("builtins.input") as prompt:
            code, output, errors = self.run_command("create")
        prompt.assert_not_called()
        self.assertEqual((code, output), (2, ""))
        self.assertIn("requires --title and --message", errors)

    def test_unavailable_database_is_reported_without_traceback(self):
        self.database.mkdir()
        code, output, errors = self.run_command("list")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Error:", errors)


if __name__ == "__main__":
    unittest.main()
