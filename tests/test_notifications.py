from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ticket_backend.database import connect
from ticket_backend.notifications import deliver_notifications, list_notifications
from ticket_backend.tickets import IdempotencyConflictError, create_ticket, get_ticket, set_status, ticket_history


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def counts(self):
        with closing(connect(self.database)) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("tickets", "ticket_events", "ticket_notifications")
            )

    def test_creation_and_status_changes_queue_one_notification_per_history_entry(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database, email=" pat@example.com ")
        self.assertEqual(ticket["email"], "pat@example.com")
        set_status(ticket["id"], "closed", self.database, note=" Fixed the form ")
        events = ticket_history(ticket["id"], self.database)
        notifications = list_notifications(self.database)
        self.assertEqual([row["event_id"] for row in notifications], [event["id"] for event in events])
        self.assertEqual({row["recipient"] for row in notifications}, {"pat@example.com"})
        self.assertEqual([row["subject"] for row in notifications],
                         ["Ticket #1 received: Login", "Ticket #1 is now closed: Login"])
        self.assertIn("Cannot sign in", notifications[0]["body"])
        self.assertEqual(notifications[1]["body"], "Ticket #1 changed from open to closed.\n\nFixed the form")
        self.assertEqual([(row["status"], row["attempts"], row["last_error"], row["sent_at"])
                          for row in notifications], [("pending", 0, None, None)] * 2)

    def test_tickets_without_email_queue_nothing(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database)
        set_status(ticket["id"], "closed", self.database)
        self.assertIsNone(ticket["email"])
        self.assertEqual(self.counts(), (1, 2, 0))

    def test_unchanged_status_queues_nothing(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database, email="pat@example.com")
        set_status(ticket["id"], "open", self.database)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_subject_keeps_the_title_on_one_line(self):
        create_ticket("Login\r\nBcc: someone@example.com", "Body", self.database, email="pat@example.com")
        self.assertEqual(list_notifications(self.database)[0]["subject"],
                         "Ticket #1 received: Login Bcc: someone@example.com")

    def test_invalid_emails_are_rejected_without_writing(self):
        for email in ("", "pat", "pat@example", "pat smith@example.com", "pat@example.com\nBcc: x@y.z",
                      "a,b@example.com", "<pat@example.com>", "pät@example.com", "p@" + "e" * 250 + ".com", 5):
            with self.subTest(email=str(email)[:40]):
                with self.assertRaises(ValueError):
                    create_ticket("Login", "Cannot sign in", self.database, email=email)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_replays_queue_nothing_and_the_email_is_part_of_the_submission(self):
        first = create_ticket("Login", "Broken", self.database, email="pat@example.com", idempotency_key="k1")
        self.assertEqual(create_ticket("Login", "Broken", self.database, email="pat@example.com",
                                       idempotency_key="k1"), first)
        for email in (None, "sam@example.com"):
            with self.assertRaises(IdempotencyConflictError):
                create_ticket("Login", "Broken", self.database, email=email, idempotency_key="k1")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_failed_queueing_rolls_back_the_ticket_and_status_change(self):
        ticket = create_ticket("Login", "Cannot sign in", self.database, email="pat@example.com")
        with closing(connect(self.database)) as connection:
            connection.executescript("""
                CREATE TRIGGER reject_notification BEFORE INSERT ON ticket_notifications
                BEGIN SELECT RAISE(ABORT, 'Simulated outbox failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            create_ticket("Other", "Body", self.database, email="pat@example.com")
        with self.assertRaises(sqlite3.IntegrityError):
            set_status(ticket["id"], "closed", self.database)
        self.assertEqual(get_ticket(ticket["id"], self.database)["status"], "open")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_existing_databases_gain_email_and_outbox_without_losing_tickets(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE ticket_submissions (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    response_json TEXT NOT NULL
                );
                INSERT INTO tickets (title, message) VALUES ('Old', 'Before email');
            """)
            connection.row_factory = sqlite3.Row
            saved = dict(connection.execute("SELECT * FROM tickets").fetchone())
            connection.execute("INSERT INTO ticket_submissions VALUES ('old-key', 1, ?)", (json.dumps(saved),))
            connection.commit()
        self.assertEqual(create_ticket("Old", "Before email", self.database, idempotency_key="old-key"), saved)
        self.assertEqual(get_ticket(1, self.database), {**saved, "email": None})
        set_status(1, "closed", self.database)
        self.assertEqual(list_notifications(self.database), [])
        create_ticket("New", "After email", self.database, email="pat@example.com")
        self.assertEqual(self.counts(), (2, 2, 1))

    def test_listing_filters_by_status_and_validates_arguments(self):
        for number in range(3):
            create_ticket(f"Ticket {number}", "Body", self.database, email="pat@example.com")
        deliver_notifications(lambda notification: None, self.database, limit=1)
        self.assertEqual([row["id"] for row in list_notifications(self.database, status="pending")], [2, 3])
        self.assertEqual([row["id"] for row in list_notifications(self.database, status="sent")], [1])
        self.assertEqual([row["id"] for row in list_notifications(self.database, 1, offset=1)], [2])
        for arguments in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "queued"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                list_notifications(self.database, **arguments)


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = create_ticket("Login", "Cannot sign in", self.database, email="pat@example.com")
        self.sent = []

    def send(self, notification):
        self.sent.append(notification)

    def fail(self, notification):
        raise ConnectionError("Mail server refused the connection")

    def test_successful_delivery_marks_the_notification_sent_once(self):
        set_status(self.ticket["id"], "closed", self.database)
        self.assertEqual(deliver_notifications(self.send, self.database),
                         {"sent": 2, "retrying": 0, "failed": 0})
        self.assertEqual([row["subject"] for row in self.sent],
                         ["Ticket #1 received: Login", "Ticket #1 is now closed: Login"])
        self.assertEqual(self.sent[0]["attempts"], 1)
        rows = list_notifications(self.database)
        self.assertEqual([(row["status"], row["attempts"]) for row in rows], [("sent", 1)] * 2)
        self.assertTrue(all(row["sent_at"] for row in rows))
        self.assertEqual(deliver_notifications(self.send, self.database), {"sent": 0, "retrying": 0, "failed": 0})
        self.assertEqual(len(self.sent), 2)

    def test_failed_delivery_records_the_error_and_waits_for_the_retry_delay(self):
        self.assertEqual(deliver_notifications(self.fail, self.database),
                         {"sent": 0, "retrying": 1, "failed": 0})
        row = list_notifications(self.database)[0]
        self.assertEqual((row["status"], row["attempts"], row["last_error"]),
                         ("pending", 1, "Mail server refused the connection"))
        self.assertGreater(row["next_attempt_at"], row["created_at"])
        self.assertEqual(deliver_notifications(self.send, self.database), {"sent": 0, "retrying": 0, "failed": 0})
        self.assertEqual(self.sent, [])

    def test_retry_succeeds_once_the_notification_is_due(self):
        deliver_notifications(self.fail, self.database, retry_delay=0)
        self.assertEqual(deliver_notifications(self.send, self.database, retry_delay=0),
                         {"sent": 1, "retrying": 0, "failed": 0})
        row = list_notifications(self.database)[0]
        self.assertEqual((row["status"], row["attempts"], row["last_error"]),
                         ("sent", 2, "Mail server refused the connection"))

    def test_notifications_fail_permanently_after_the_maximum_attempts(self):
        outcomes = [deliver_notifications(self.fail, self.database, max_attempts=3, retry_delay=0)
                    for _ in range(4)]
        self.assertEqual([outcome["failed"] for outcome in outcomes], [0, 0, 1, 0])
        row = list_notifications(self.database)[0]
        self.assertEqual((row["status"], row["attempts"]), ("failed", 3))

    def test_errors_without_a_message_record_the_exception_type(self):
        def fail(notification):
            raise TimeoutError()
        deliver_notifications(fail, self.database)
        self.assertEqual(list_notifications(self.database)[0]["last_error"], "TimeoutError")

    def test_delivery_respects_the_limit_in_queue_order(self):
        create_ticket("Second", "Body", self.database, email="sam@example.com")
        deliver_notifications(self.send, self.database, limit=1)
        self.assertEqual([row["recipient"] for row in self.sent], ["pat@example.com"])

    def test_a_concurrent_sender_does_not_send_a_claimed_notification_again(self):
        create_ticket("Second", "Body", self.database, email="sam@example.com")
        other = []

        def send(notification):
            self.sent.append(notification["id"])
            if len(self.sent) == 1:
                deliver_notifications(lambda row: other.append(row["id"]), self.database)
        self.assertEqual(deliver_notifications(send, self.database), {"sent": 1, "retrying": 0, "failed": 0})
        self.assertEqual((self.sent, other), ([1], [2]))
        self.assertEqual([row["attempts"] for row in list_notifications(self.database)], [1, 1])

    def test_invalid_arguments_are_rejected_before_sending(self):
        for send, arguments in ((None, {}), (self.send, {"limit": 0}), (self.send, {"max_attempts": 0}),
                                (self.send, {"retry_delay": -1}), (self.send, {"retry_delay": 1.5})):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                deliver_notifications(send, self.database, **arguments)
        self.assertEqual(list_notifications(self.database)[0]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
