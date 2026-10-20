from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from functions import create_ticket, deliver_notifications, list_notifications, set_status
from mailer import DeliveryError, SmtpSender, SmtpSettings, build_message, settings_from_environment
from smtp_stub import SmtpStub
import tickets


def closed_port():
    # Bind and release a loopback port, so nothing is listening on it during the test.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class SmtpSettingsTests(unittest.TestCase):
    def test_environment_settings_use_defaults_for_the_security_mode(self):
        settings = settings_from_environment({"TICKETS_SMTP_HOST": " mail.example.com ",
                                              "TICKETS_SMTP_FROM": "Support <support@example.com>"})
        self.assertEqual((settings.host, settings.port, settings.security, settings.timeout),
                         ("mail.example.com", 587, "starttls", 10.0))
        self.assertEqual(settings.domain, "example.com")
        for security, port in (("ssl", 465), ("NONE", 25)):
            with self.subTest(security=security):
                settings = settings_from_environment({"TICKETS_SMTP_HOST": "mail.example.com",
                                                      "TICKETS_SMTP_FROM": "support@example.com",
                                                      "TICKETS_SMTP_SECURITY": security})
                self.assertEqual(settings.port, port)

    def test_environment_settings_read_every_variable(self):
        settings = settings_from_environment({
            "TICKETS_SMTP_HOST": "mail.example.com", "TICKETS_SMTP_FROM": "support@example.com",
            "TICKETS_SMTP_PORT": "2525", "TICKETS_SMTP_SECURITY": "ssl", "TICKETS_SMTP_USERNAME": "support",
            "TICKETS_SMTP_PASSWORD": " secret phrase ", "TICKETS_SMTP_TIMEOUT": "2.5",
        })
        self.assertEqual((settings.port, settings.security, settings.username, settings.password, settings.timeout),
                         (2525, "ssl", "support", " secret phrase ", 2.5))
        self.assertNotIn("secret", repr(settings))

    def test_missing_or_invalid_settings_are_rejected(self):
        base = {"TICKETS_SMTP_HOST": "mail.example.com", "TICKETS_SMTP_FROM": "support@example.com"}
        cases = (
            ({}, "TICKETS_SMTP_HOST"),
            ({"TICKETS_SMTP_HOST": "mail.example.com"}, "TICKETS_SMTP_FROM"),
            ({**base, "TICKETS_SMTP_PORT": "smtp"}, "TICKETS_SMTP_PORT"),
            ({**base, "TICKETS_SMTP_PORT": "70000"}, "port"),
            ({**base, "TICKETS_SMTP_TIMEOUT": "0"}, "timeout"),
            ({**base, "TICKETS_SMTP_SECURITY": "tls"}, "security"),
            ({**base, "TICKETS_SMTP_FROM": "support"}, "sender"),
            ({**base, "TICKETS_SMTP_FROM": "support@example.com\r\nBcc: x@example.com"}, "sender"),
            ({**base, "TICKETS_SMTP_HOST": "mail example.com"}, "host"),
            ({**base, "TICKETS_SMTP_USERNAME": "support"}, "together"),
            ({**base, "TICKETS_SMTP_SECURITY": "none", "TICKETS_SMTP_USERNAME": "support",
              "TICKETS_SMTP_PASSWORD": "secret"}, "localhost"),
        )
        for environment, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                settings_from_environment(environment)

    def test_credentials_without_encryption_are_allowed_only_on_loopback(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                SmtpSettings(host, "support@example.com", security="none", username="u", password="p")

    def test_messages_carry_the_queued_content_and_a_stable_message_id(self):
        settings = SmtpSettings("mail.example.com", "Support <support@example.com>")
        notification = {"id": 7, "recipient": "pat@example.com", "subject": "Ticket #3 received: Café",
                        "body": "Ça marche.\n\n.Leading dot", "created_at": "2026-10-19 09:30:00"}
        message = build_message(notification, settings)
        self.assertEqual((message["From"], message["To"], message["Subject"], message["Auto-Submitted"]),
                         ("Support <support@example.com>", "pat@example.com", "Ticket #3 received: Café",
                          "auto-generated"))
        self.assertEqual(message["Message-ID"], "<ticket-notification-7.20261019093000@example.com>")
        self.assertEqual(build_message(notification, settings)["Message-ID"], message["Message-ID"])
        self.assertEqual(message["Date"].datetime.utcoffset(), timedelta(0))
        self.assertEqual(message.get_content(), "Ça marche.\n\n.Leading dot\n")
        self.assertTrue(bytes(message).isascii(), "Messages must not need 8BITMIME")


class SmtpDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def queue(self, *emails):
        for number, email in enumerate(emails, 1):
            create_ticket(f"Ticket {number}", f"Body {number}", self.database, email=email)

    def settings(self, stub, **overrides):
        return SmtpSettings("127.0.0.1", "Support <support@example.com>", port=stub.port, security="none",
                            timeout=5, **overrides)

    def run_send(self, stub_port, *arguments, **environment):
        variables = {"TICKETS_SMTP_HOST": "127.0.0.1", "TICKETS_SMTP_FROM": "support@example.com",
                     "TICKETS_SMTP_PORT": str(stub_port), "TICKETS_SMTP_SECURITY": "none",
                     "TICKETS_SMTP_TIMEOUT": "5", **environment}
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {key: value for key, value in variables.items() if value is not None}), \
                redirect_stdout(output), redirect_stderr(errors):
            for key in [key for key, value in variables.items() if value is None]:
                os.environ.pop(key, None)
            code = tickets.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_sender_delivers_the_outbox_over_one_connection(self):
        self.queue("pat@example.com")
        set_status(1, "closed", self.database, note="Fixed the form")
        with SmtpStub() as stub, SmtpSender(self.settings(stub)) as sender:
            self.assertEqual(deliver_notifications(sender, self.database), {"sent": 2, "retrying": 0, "failed": 0})
        self.assertEqual(stub.connections, 1)
        messages = stub.parsed()
        self.assertEqual([recipients for recipients, _message in messages], [["pat@example.com"]] * 2)
        self.assertEqual([message["Subject"] for _recipients, message in messages],
                         ["Ticket #1 received: Ticket 1", "Ticket #1 is now closed: Ticket 1"])
        self.assertEqual(messages[1][1].get_content(), "Ticket #1 changed from open to closed.\n\nFixed the form\n")
        self.assertEqual({row["status"] for row in list_notifications(self.database)}, {"sent"})

    def test_a_refused_recipient_is_retried_later_without_blocking_others(self):
        self.queue("gone@example.com", "pat@example.com")
        with SmtpStub(refuse={"gone@example.com"}) as stub, SmtpSender(self.settings(stub)) as sender:
            self.assertEqual(deliver_notifications(sender, self.database), {"sent": 1, "retrying": 1, "failed": 0})
        self.assertEqual([recipients for recipients, _message in stub.parsed()], [["pat@example.com"]])
        refused = list_notifications(self.database)[0]
        self.assertEqual((refused["status"], refused["attempts"]), ("pending", 1))
        self.assertEqual(refused["last_error"], "Recipient gone@example.com refused: 550 5.1.1 No such user")

    def test_a_dropped_connection_is_recorded_and_the_next_message_reconnects(self):
        self.queue("drop@example.com", "pat@example.com")
        with SmtpStub(disconnect={"drop@example.com"}) as stub, SmtpSender(self.settings(stub)) as sender:
            self.assertEqual(deliver_notifications(sender, self.database), {"sent": 1, "retrying": 1, "failed": 0})
        self.assertEqual(stub.connections, 2)
        self.assertEqual([row["status"] for row in list_notifications(self.database)], ["pending", "sent"])
        self.assertTrue(list_notifications(self.database)[0]["last_error"])

    def test_login_uses_the_configured_credentials(self):
        self.queue("pat@example.com")
        with SmtpStub(credentials=("support", "secret")) as stub:
            with SmtpSender(self.settings(stub, username="support", password="secret")) as sender:
                deliver_notifications(sender, self.database)
            with self.assertRaisesRegex(DeliveryError, "rejected the session: 535"):
                SmtpSender(self.settings(stub, username="support", password="wrong")).open()
        self.assertEqual(len(stub.messages), 1)

    def test_send_command_drains_the_outbox_and_reports_counts(self):
        self.queue("pat@example.com", "sam@example.com")
        with SmtpStub() as stub:
            code, output, errors = self.run_send(stub.port, "send-notifications")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(output, "Sent 2, retrying 0, failed 0.\n")
            code, output, _errors = self.run_send(stub.port, "--json", "send-notifications")
        self.assertEqual((code, json.loads(output)), (0, {"sent": 0, "retrying": 0, "failed": 0}))
        self.assertEqual(stub.connections, 1, "A run with nothing due must not connect")
        self.assertEqual(stub.parsed()[0][1]["From"], "support@example.com")

    def test_send_command_respects_the_limit(self):
        self.queue("pat@example.com", "sam@example.com")
        with SmtpStub() as stub:
            code, output, _errors = self.run_send(stub.port, "--json", "send-notifications", "--limit", "1")
        self.assertEqual((code, json.loads(output)["sent"]), (0, 1))
        self.assertEqual([row["status"] for row in list_notifications(self.database)], ["sent", "pending"])

    def test_unreachable_or_rejecting_servers_fail_before_claiming_anything(self):
        self.queue("pat@example.com")
        code, output, errors = self.run_send(closed_port(), "send-notifications")
        self.assertEqual((code, output), (4, ""))
        self.assertIn("Could not connect to SMTP server 127.0.0.1", errors)
        with SmtpStub() as stub:
            # The stub does not offer STARTTLS, so a starttls session must stop before sending anything.
            code, _output, errors = self.run_send(stub.port, "send-notifications", TICKETS_SMTP_SECURITY="starttls")
            self.assertEqual(code, 4)
            self.assertIn("rejected the session", errors)
        with SmtpStub(credentials=("support", "secret")) as stub:
            code, _output, errors = self.run_send(stub.port, "send-notifications", TICKETS_SMTP_USERNAME="support",
                                                  TICKETS_SMTP_PASSWORD="wrong")
            self.assertEqual(code, 4)
            self.assertIn("535", errors)
            self.assertNotIn("wrong", errors)
        self.assertNotIn("Traceback", errors)
        row = list_notifications(self.database)[0]
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("pending", 0, None))

    def test_invalid_configuration_and_arguments_fail_without_connecting(self):
        self.queue("pat@example.com")
        with SmtpStub() as stub:
            for arguments, environment, message in (
                ((), {"TICKETS_SMTP_HOST": None}, "TICKETS_SMTP_HOST"),
                ((), {"TICKETS_SMTP_SECURITY": "tls"}, "security"),
                (("--limit", "0"), {}, "Limit"),
                (("--max-attempts", "0"), {}, "Maximum attempts"),
                (("--retry-delay", "-1"), {}, "retry delay"),
            ):
                with self.subTest(message=message):
                    code, output, errors = self.run_send(stub.port, "send-notifications", *arguments, **environment)
                    self.assertEqual((code, output), (2, ""))
                    self.assertIn(message, errors)
        self.assertEqual(stub.connections, 0)
        self.assertEqual(list_notifications(self.database)[0]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
