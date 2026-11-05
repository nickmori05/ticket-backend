from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.database import connect
from ticket_backend.labels import (
    MAX_LABELS_PER_TICKET, add_labels, label_counts, remove_labels, require_label, ticket_labels,
)
from ticket_backend.tickets import (
    TicketNotFoundError, create_ticket, get_ticket, list_tickets, set_status, ticket_history,
)


class LabelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = create_ticket("Login", "Cannot sign in", self.database)["id"]

    def test_labels_are_normalized_sorted_and_added_once(self):
        result = add_labels(self.ticket, [" Urgent ", "billing", "urgent", "priority:high"], self.database)
        self.assertEqual(result, {"ticket_id": self.ticket, "labels": ["billing", "priority:high", "urgent"]})
        self.assertEqual(add_labels(self.ticket, ["BILLING"], self.database), result)
        self.assertEqual(ticket_labels(self.ticket, self.database), result)

    def test_removing_labels_ignores_ones_the_ticket_does_not_have(self):
        add_labels(self.ticket, ["billing", "urgent"], self.database)
        self.assertEqual(remove_labels(self.ticket, ["Urgent", "missing"], self.database)["labels"], ["billing"])
        self.assertEqual(remove_labels(self.ticket, ["urgent"], self.database)["labels"], ["billing"])

    def test_labels_do_not_change_status_history_or_the_outbox(self):
        ticket = create_ticket("Billing", "Wrong invoice", self.database, email="pat@example.com")["id"]
        set_status(ticket, "closed", self.database)
        add_labels(ticket, ["billing"], self.database)
        remove_labels(ticket, ["billing"], self.database)
        self.assertEqual(get_ticket(ticket, self.database)["status"], "closed")
        self.assertEqual(len(ticket_history(ticket, self.database)), 2)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_notifications").fetchone()[0], 2)

    def test_invalid_labels_are_rejected_without_writing(self):
        for labels in ([], "billing", [""], ["-billing"], ["two words"], ["x" * 33], ["café"], [None],
                       ["label%"], [f"l{number}" for number in range(MAX_LABELS_PER_TICKET + 1)]):
            with self.subTest(labels=str(labels)[:40]):
                with self.assertRaises(ValueError):
                    add_labels(self.ticket, labels, self.database)
        self.assertEqual(ticket_labels(self.ticket, self.database)["labels"], [])
        self.assertEqual(require_label("a" * 32), "a" * 32)

    def test_a_ticket_has_at_most_the_maximum_number_of_labels(self):
        add_labels(self.ticket, [f"l{number}" for number in range(MAX_LABELS_PER_TICKET - 1)], self.database)
        with self.assertRaisesRegex(ValueError, "at most 20 labels"):
            add_labels(self.ticket, ["extra", "another"], self.database)
        self.assertEqual(len(ticket_labels(self.ticket, self.database)["labels"]), MAX_LABELS_PER_TICKET - 1)
        self.assertEqual(len(add_labels(self.ticket, ["extra", "l0"], self.database)["labels"]),
                         MAX_LABELS_PER_TICKET)

    def test_missing_tickets_raise_not_found(self):
        for operation in (lambda: add_labels(99, ["billing"], self.database),
                          lambda: remove_labels(99, ["billing"], self.database),
                          lambda: ticket_labels(99, self.database)):
            with self.assertRaises(TicketNotFoundError):
                operation()

    def test_list_filters_by_every_given_label(self):
        billing = create_ticket("Billing", "Wrong invoice", self.database)["id"]
        refund = create_ticket("Refund", "Charged twice", self.database)["id"]
        add_labels(self.ticket, ["urgent"], self.database)
        add_labels(billing, ["billing"], self.database)
        add_labels(refund, ["billing", "urgent"], self.database)

        def listed(**filters):
            return [ticket["id"] for ticket in list_tickets(self.database, **filters)]

        self.assertEqual(listed(labels=["billing"]), [refund, billing])
        self.assertEqual(listed(labels=["Billing", "urgent"]), [refund])
        self.assertEqual(listed(labels=["urgent", "urgent"]), [refund, self.ticket])
        self.assertEqual(listed(labels=["unused"]), [])
        set_status(billing, "closed", self.database)
        self.assertEqual(listed(labels=["billing"], status="closed"), [billing])
        self.assertEqual(listed(labels=["billing"], search="twice", limit=1), [refund])
        with self.assertRaises(ValueError):
            listed(labels=["a", "b", "c", "d", "e", "f"])

    def test_label_counts_include_tickets_not_closed(self):
        self.assertEqual(label_counts(self.database), [])
        other = create_ticket("Billing", "Wrong invoice", self.database)["id"]
        add_labels(self.ticket, ["billing", "urgent"], self.database)
        add_labels(other, ["billing"], self.database)
        set_status(other, "closed", self.database)
        self.assertEqual(label_counts(self.database), [
            {"label": "billing", "tickets": 2, "unresolved": 1},
            {"label": "urgent", "tickets": 1, "unresolved": 1},
        ])

    def test_existing_databases_gain_labels_without_losing_tickets(self):
        legacy = self.database.with_name("legacy.db")
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript("""
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tickets (title, message) VALUES ('Old', 'Before labels');
            """)
        self.assertEqual(add_labels(1, ["legacy"], legacy)["labels"], ["legacy"])
        self.assertEqual([ticket["title"] for ticket in list_tickets(legacy, labels=["legacy"])], ["Old"])

    def test_a_rejected_addition_leaves_earlier_labels_unchanged(self):
        add_labels(self.ticket, ["billing"], self.database)
        with closing(connect(self.database)) as connection:
            connection.executescript("""
                CREATE TRIGGER reject_label AFTER INSERT ON ticket_labels WHEN NEW.label = 'broken'
                BEGIN SELECT RAISE(ABORT, 'Simulated label failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            add_labels(self.ticket, ["urgent", "broken"], self.database)
        self.assertEqual(ticket_labels(self.ticket, self.database)["labels"], ["billing"])


class LabelCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.identifier = str(create_ticket("Login", "Cannot sign in", self.database)["id"])

    def run_command(self, *arguments, json_mode=True):
        output, errors = io.StringIO(), io.StringIO()
        options = ["--json"] if json_mode else []
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *options, *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_json_label_workflow(self):
        code, output, errors = self.run_command("label", self.identifier, "urgent", "billing")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output), {"ticket_id": 1, "labels": ["billing", "urgent"]})
        self.assertEqual(json.loads(self.run_command("unlabel", self.identifier, "urgent")[1])["labels"], ["billing"])
        self.assertEqual(json.loads(self.run_command("labels", self.identifier)[1])["labels"], ["billing"])
        self.assertEqual(json.loads(self.run_command("labels")[1]),
                         [{"label": "billing", "tickets": 1, "unresolved": 1}])
        listed = json.loads(self.run_command("list", "--label", "billing")[1])
        self.assertEqual([row["id"] for row in listed], [1])
        self.assertEqual(json.loads(self.run_command("list", "--label", "billing", "--label", "urgent")[1]), [])

    def test_text_output_for_labels(self):
        self.assertIn("No labels in use.", self.run_command("labels", json_mode=False)[1])
        self.assertEqual(self.run_command("labels", self.identifier, json_mode=False)[1],
                         "Ticket #1 labels: none\n")
        code, output, _ = self.run_command("label", self.identifier, "billing", "urgent", json_mode=False)
        self.assertEqual((code, output), (0, "Ticket #1 labels: billing, urgent\n"))
        self.assertEqual(self.run_command("labels", json_mode=False)[1],
                         "billing: 1 ticket, 1 not closed\nurgent: 1 ticket, 1 not closed\n")

    def test_errors_use_existing_exit_codes(self):
        code, output, errors = self.run_command("label", "99", "billing")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("not found", errors)
        code, output, errors = self.run_command("label", self.identifier, "two words")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Label must be", errors)
        self.assertEqual(self.run_command("list", "--label", "bad%")[0], 2)


if __name__ == "__main__":
    unittest.main()
