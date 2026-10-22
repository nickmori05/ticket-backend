import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from ticket_backend.api import create_app
from ticket_backend.audit import list_audit
from ticket_backend.ratelimit import RateLimiter
from ticket_backend.tickets import create_ticket, statistics
from ticket_backend.tokens import create_token


class RateLimitApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        create_ticket("Login", "Broken", self.database)
        # Keep expected rate limit warnings out of the test output; assertLogs still sees them.
        handler, logger = logging.NullHandler(), logging.getLogger("ticket_backend.api")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

    def client(self, **options):
        client = TestClient(create_app(self.database, **options))
        self.addCleanup(client.close)
        return client

    def bearer(self, name, **options):
        return {"Authorization": f"Bearer {create_token(name, self.database, **options)['token']}"}

    def codes(self, client, headers, count, path="/tickets/1"):
        return [client.get(path, headers=headers).status_code for _ in range(count)]

    def test_tokens_over_the_default_limit_get_429_with_retry_after(self):
        client, alice = self.client(rate_limit=3), self.bearer("alice")
        self.assertEqual(self.codes(client, alice, 3), [200, 200, 200])
        refused = client.get("/tickets/1", headers=alice)
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.headers["Retry-After"], "20")
        self.assertEqual(refused.json(), {"detail": "This token is limited to 3 requests per minute; retry in 20 "
                                                    "seconds."})
        self.assertEqual(self.codes(client, self.bearer("bob"), 3), [200, 200, 200])

    def test_a_tokens_own_limit_replaces_the_default(self):
        client = self.client(rate_limit=2)
        self.assertEqual(self.codes(client, self.bearer("form", rate_limit=1), 2), [200, 429])
        self.assertEqual(self.codes(client, self.bearer("batch", rate_limit=0), 20), [200] * 20)
        self.assertEqual(self.codes(client, self.bearer("bulk", rate_limit=5), 6), [200] * 5 + [429])
        self.assertEqual(self.codes(self.client(rate_limit=0), self.bearer("agent"), 20), [200] * 20)

    def test_limits_apply_across_routes_before_the_scope_and_the_request_run(self):
        client, writer = self.client(rate_limit=2), self.bearer("writer", scopes=["tickets:write"])
        self.assertEqual(client.get("/tickets/1", headers=writer).status_code, 403)
        self.assertEqual(client.post("/tickets", json={"title": "A", "message": "B"}, headers=writer).status_code, 201)
        refused = client.post("/tickets", json={"title": "C", "message": "D"}, headers=writer)
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(client.get("/stats", headers=writer).status_code, 429)
        self.assertEqual(statistics(self.database)["total"], 2)

    def test_buckets_refill_over_time(self):
        clock = mock.Mock(return_value=100.0)
        with mock.patch("ticket_backend.api.RateLimiter", lambda: RateLimiter(clock)):
            client = self.client(rate_limit=2)
        alice = self.bearer("alice")
        self.assertEqual(self.codes(client, alice, 3), [200, 200, 429])
        clock.return_value = 130.0
        self.assertEqual(self.codes(client, alice, 2), [200, 429])

    def test_requests_without_a_valid_token_and_health_checks_are_not_limited(self):
        client = self.client(rate_limit=1)
        self.assertEqual(self.codes(client, {"Authorization": "Bearer tkt_unknown"}, 5), [401] * 5)
        self.assertEqual(self.codes(client, {}, 5, "/health"), [200] * 5)
        self.assertEqual(self.codes(client, self.bearer("alice"), 2), [200, 429])

    def test_refusals_are_not_audited_and_are_logged_once_per_run(self):
        client, alice = self.client(rate_limit=1), self.bearer("alice")
        client.get("/tickets/1", headers=alice)
        with self.assertLogs("ticket_backend.api", "WARNING") as logs:
            self.assertEqual(self.codes(client, alice, 3), [429] * 3)
        self.assertEqual(logs.output,
                         ["WARNING:ticket_backend.api:Token alice exceeded its rate limit (1 per minute)."])
        self.assertEqual([row["status"] for row in list_audit(self.database)], [200])

    def test_the_default_limit_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"TICKETS_RATE_LIMIT": "1"}):
            client = self.client()
        self.assertEqual(self.codes(client, self.bearer("alice"), 2), [200, 429])
        with mock.patch.dict(os.environ, {"TICKETS_RATE_LIMIT": "fast"}), \
                self.assertRaisesRegex(ValueError, "TICKETS_RATE_LIMIT must be"):
            create_app(self.database)
        for limit in (-1, 100_001, "5", True):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "Rate limit must"):
                create_app(self.database, rate_limit=limit)

    def test_each_app_keeps_its_own_buckets(self):
        alice = self.bearer("alice")
        self.assertEqual(self.codes(self.client(rate_limit=1), alice, 2), [200, 429])
        self.assertEqual(self.codes(self.client(rate_limit=1), alice, 1), [200])

    def test_openapi_documents_429(self):
        paths = self.client().get("/openapi.json").json()["paths"]
        self.assertIn("429", paths["/tickets"]["post"]["responses"])
        self.assertIn("429", paths["/audit"]["get"]["responses"])
        self.assertNotIn("429", paths["/health"]["get"]["responses"])


if __name__ == "__main__":
    unittest.main()
