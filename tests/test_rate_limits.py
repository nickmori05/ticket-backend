from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from ticket_backend import cli
from ticket_backend.ratelimit import DEFAULT_RATE_LIMIT, RateLimiter, rate_limit_from_environment
from ticket_backend.tokens import create_token, list_tokens, token_access


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = RateLimiter(self.clock)

    def allowed(self, key, limit, count):
        return [self.limiter.check(key, limit).allowed for _ in range(count)]

    def test_a_full_bucket_allows_a_burst_up_to_the_limit(self):
        self.assertEqual(self.allowed("alice", 3, 4), [True, True, True, False])
        refused = self.limiter.check("alice", 3)
        self.assertEqual((refused.allowed, refused.retry_after), (False, 20))

    def test_buckets_refill_at_the_limit_per_minute_and_never_overflow(self):
        self.allowed("alice", 60, 60)
        self.assertEqual(self.limiter.check("alice", 60).retry_after, 1)
        self.clock.now += 0.5
        self.assertFalse(self.limiter.check("alice", 60).allowed)
        self.clock.now += 0.5
        self.assertEqual(self.allowed("alice", 60, 2), [True, False])
        self.clock.now += 3600
        self.assertEqual(self.allowed("alice", 60, 61).count(True), 60)

    def test_retry_after_counts_whole_seconds_until_the_next_request(self):
        self.allowed("slow", 1, 1)
        self.assertEqual(self.limiter.check("slow", 1).retry_after, 60)
        self.clock.now += 59.2
        self.assertEqual(self.limiter.check("slow", 1).retry_after, 1)
        self.clock.now += 0.8
        self.assertTrue(self.limiter.check("slow", 1).allowed)

    def test_each_key_has_its_own_bucket(self):
        self.allowed("alice", 2, 2)
        self.assertFalse(self.limiter.check("alice", 2).allowed)
        self.assertEqual(self.allowed("bob", 2, 2), [True, True])

    def test_a_limit_of_zero_never_refuses(self):
        self.assertTrue(all(self.allowed("batch", 0, 1000)))

    def test_only_the_first_refusal_in_a_run_is_new(self):
        self.allowed("alice", 1, 1)
        self.assertEqual([self.limiter.check("alice", 1).newly_limited for _ in range(3)], [True, False, False])
        self.clock.now += 60
        self.assertTrue(self.limiter.check("alice", 1).allowed)
        self.assertTrue(self.limiter.check("alice", 1).newly_limited)

    def test_a_changed_limit_starts_a_fresh_bucket(self):
        self.allowed("alice", 2, 3)
        self.assertEqual(self.allowed("alice", 5, 6), [True] * 5 + [False])

    def test_concurrent_requests_never_exceed_the_limit(self):
        results, start = [], threading.Barrier(8)

        def spend():
            start.wait()
            outcomes = self.allowed("shared", 100, 50)
            results.extend(outcomes)

        threads = [threading.Thread(target=spend) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((results.count(True), len(results)), (100, 400))

    def test_the_default_comes_from_the_environment(self):
        for value, expected in (("", DEFAULT_RATE_LIMIT), (" 30 ", 30), ("0", 0), ("100000", 100_000)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TICKETS_RATE_LIMIT": value}):
                self.assertEqual(rate_limit_from_environment(), expected)
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(rate_limit_from_environment(), 120)
        for value in ("abc", "-1", "100001", "1.5", "1e3"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TICKETS_RATE_LIMIT": value}), \
                    self.assertRaisesRegex(ValueError, "TICKETS_RATE_LIMIT must be a whole number"):
                rate_limit_from_environment()


class TokenRateLimitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_tokens_store_their_own_limit_or_use_the_default(self):
        limited = create_token("form", self.database, rate_limit=10)
        unlimited = create_token("batch", self.database, rate_limit=0)
        default = create_token("agent", self.database)
        self.assertEqual([limited["rate_limit"], unlimited["rate_limit"], default["rate_limit"]], [10, 0, None])
        self.assertEqual([token_access(created["token"], self.database)["rate_limit"]
                          for created in (limited, unlimited, default)], [10, 0, None])
        self.assertEqual([row["rate_limit"] for row in list_tokens(self.database)], [10, 0, None])

    def test_invalid_limits_create_nothing(self):
        for limit in (-1, 100_001, "30", 1.5, True):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "Rate limit must"):
                create_token("alice", self.database, rate_limit=limit)
        self.assertEqual(list_tokens(self.database), [])

    def test_existing_tokens_gain_the_default_limit(self):
        token = "tkt_before-rate-limits"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(f"""
                CREATE TABLE api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    revoked_at TIMESTAMP
                );
                INSERT INTO api_tokens (name, token_hash, scopes)
                VALUES ('old', '{hashlib.sha256(token.encode()).hexdigest()}', 'tickets:read');
            """)
        self.assertEqual(token_access(token, self.database),
                         {"name": "old", "scopes": ["tickets:read"], "rate_limit": None})
        self.assertEqual(create_token("new", self.database, rate_limit=5)["rate_limit"], 5)

    def test_cli_sets_and_shows_limits(self):
        code, output, _ = self.cli("token", "create", "form", "--rate-limit", "10")
        self.assertEqual(code, 0)
        self.assertIn("Rate limit: 10 requests per minute\n", output)
        self.assertIn("Rate limit: none\n", self.cli("token", "create", "batch", "--rate-limit", "0")[1])
        self.assertNotIn("Rate limit", self.cli("token", "create", "agent")[1])
        lines = self.cli("token", "list")[1].splitlines()
        self.assertTrue(lines[0].endswith(" UTC, 10 requests per minute)"))
        self.assertTrue(lines[1].endswith(" UTC, no rate limit)"))
        self.assertTrue(lines[2].endswith(" UTC)"))
        code, output, _ = self.cli("--json", "token", "list")
        self.assertEqual([row["rate_limit"] for row in json.loads(output)], [10, 0, None])

    def test_cli_rejects_invalid_limits(self):
        code, _, errors = self.cli("token", "create", "form", "--rate-limit", "-5")
        self.assertEqual((code, errors), (2, "Error: Rate limit must be a whole number of requests per minute "
                                             "from 0 to 100000.\n"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--database", str(self.database), "token", "create", "form", "--rate-limit", "ten"])
        self.assertEqual(list_tokens(self.database), [])


if __name__ == "__main__":
    unittest.main()
