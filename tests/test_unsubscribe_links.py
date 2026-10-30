from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from ticket_backend import cli
from ticket_backend.mailer import (
    SmtpSender, SmtpSettings, UnsubscribeLinks, build_message, unsubscribe_links_from_environment,
)
from ticket_backend.notifications import deliver_notifications, list_notifications
from ticket_backend.tickets import create_ticket
from ticket_backend.unsubscribe import unsubscribe, unsubscribe_token
from smtp_stub import SmtpStub


SECRET = "s" * 40


class UnsubscribeLinkTests(unittest.TestCase):
    def test_links_point_at_the_api_and_keep_a_path_prefix(self):
        links = UnsubscribeLinks("https://support.example.com/api/", SECRET)
        self.assertEqual(links.base_url, "https://support.example.com/api")
        token = unsubscribe_token(SECRET, 1, "pat@example.com")
        self.assertEqual(links.url_for(1, "pat@example.com"), f"https://support.example.com/api/unsubscribe/{token}")
        self.assertNotIn(SECRET, repr(links))
        for base_url in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
            with self.subTest(base_url=base_url):
                UnsubscribeLinks(base_url, SECRET)

    def test_invalid_links_are_rejected(self):
        for base_url, secret, message in (
            ("http://support.example.com", SECRET, "https"),
            ("support.example.com", SECRET, "absolute URL"),
            ("ftp://support.example.com", SECRET, "absolute URL"),
            ("https://", SECRET, "absolute URL"),
            ("https://support.example.com/?a=1", SECRET, "absolute URL"),
            ("https://support.example.com/#top", SECRET, "absolute URL"),
            ("https://support.example.com/a b", SECRET, "absolute URL"),
            ("https://support.example.com", "short", "at least 32"),
        ):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(ValueError, message):
                UnsubscribeLinks(base_url, secret)

    def test_environment_settings_need_both_variables(self):
        self.assertIsNone(unsubscribe_links_from_environment({}))
        links = unsubscribe_links_from_environment({"TICKETS_UNSUBSCRIBE_URL": " https://support.example.com ",
                                                    "TICKETS_UNSUBSCRIBE_SECRET": SECRET})
        self.assertEqual(links.base_url, "https://support.example.com")
        for environment in ({"TICKETS_UNSUBSCRIBE_URL": "https://support.example.com"},
                            {"TICKETS_UNSUBSCRIBE_SECRET": SECRET}):
            with self.subTest(environment=list(environment)), self.assertRaisesRegex(ValueError, "together"):
                unsubscribe_links_from_environment(environment)

    def test_messages_offer_one_click_unsubscribe_only_with_a_link(self):
        settings = SmtpSettings("mail.example.com", "support@example.com")
        notification = {"id": 7, "ticket_id": 3, "recipient": "pat@example.com", "subject": "Ticket #3 received",
                        "body": "We got it.\n", "created_at": "2026-10-19 09:30:00"}
        plain = build_message(notification, settings)
        self.assertIsNone(plain["List-Unsubscribe"])
        self.assertEqual(plain.get_content(), "We got it.\n")
        url = f"https://support.example.com/a/long/path/prefix/unsubscribe/{unsubscribe_token(SECRET, 3, 'pat@x.io')}"
        message = build_message(notification, settings, url)
        self.assertEqual((message["List-Unsubscribe"], message["List-Unsubscribe-Post"]),
                         (f"<{url}>", "List-Unsubscribe=One-Click"))
        self.assertEqual(message.get_content(),
                         f"We got it.\n\n-- \nTo stop receiving emails about your tickets, open:\n{url}\n")
        self.assertEqual(message["Message-ID"], plain["Message-ID"])
        # Mail clients read the header from the wire, so the URL must not be folded or encoded there.
        wire = bytes(message)
        self.assertIn(f"\r\nList-Unsubscribe: <{url}>\r\n".encode(), wire)
        self.assertTrue(wire.isascii())
        self.assertTrue(all(len(line) <= 78 for line in wire.split(b"\r\n\r\n", 1)[1].splitlines()),
                        "The body must still use short lines")


class UnsubscribeDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        create_ticket("Login", "Broken", self.database, email="pat@example.com")

    def run_send(self, stub_port, **environment):
        variables = {"TICKETS_SMTP_HOST": "127.0.0.1", "TICKETS_SMTP_FROM": "support@example.com",
                     "TICKETS_SMTP_PORT": str(stub_port), "TICKETS_SMTP_SECURITY": "none", **environment}
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, variables), redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), "--json", "send-notifications"])
        return code, output.getvalue(), errors.getvalue()

    def test_sent_messages_carry_a_link_that_unsubscribes_their_recipient(self):
        links = UnsubscribeLinks("https://support.example.com", SECRET)
        with SmtpStub() as stub, SmtpSender(SmtpSettings("127.0.0.1", "support@example.com", port=stub.port,
                                                         security="none", timeout=5), links) as sender:
            deliver_notifications(sender, self.database)
        [(_recipients, message)] = stub.parsed()
        url = message["List-Unsubscribe"].strip("<>")
        self.assertIn(f"\r\nList-Unsubscribe: <{url}>\r\n".encode(), stub.messages[0][2])
        self.assertEqual(url, links.url_for(1, "pat@example.com"))
        self.assertIn(url, message.get_content())
        token = urlsplit(url).path.rsplit("/", 1)[1]
        self.assertEqual(unsubscribe(token, SECRET, self.database)["address"], "pat@example.com")

    def test_send_command_reads_the_unsubscribe_settings(self):
        with SmtpStub() as stub:
            code, output, errors = self.run_send(stub.port, TICKETS_UNSUBSCRIBE_URL="https://support.example.com",
                                                 TICKETS_UNSUBSCRIBE_SECRET=SECRET)
        self.assertEqual((code, json.loads(output)["sent"], errors), (0, 1, ""))
        self.assertTrue(stub.parsed()[0][1]["List-Unsubscribe"].startswith("<https://support.example.com/unsub"))

    def test_invalid_unsubscribe_settings_fail_before_connecting(self):
        with SmtpStub() as stub:
            for environment, message in (({"TICKETS_UNSUBSCRIBE_URL": "https://support.example.com"}, "together"),
                                         ({"TICKETS_UNSUBSCRIBE_URL": "http://support.example.com",
                                           "TICKETS_UNSUBSCRIBE_SECRET": SECRET}, "https"),
                                         ({"TICKETS_UNSUBSCRIBE_URL": "https://support.example.com",
                                           "TICKETS_UNSUBSCRIBE_SECRET": "short"}, "at least 32")):
                with self.subTest(message=message):
                    code, output, errors = self.run_send(stub.port, **environment)
                    self.assertEqual((code, output), (2, ""))
                    self.assertIn(message, errors)
        self.assertEqual(stub.connections, 0)
        self.assertEqual(list_notifications(self.database)[0]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
