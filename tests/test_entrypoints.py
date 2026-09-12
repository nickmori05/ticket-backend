import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        for name in ("db.py", "functions.py", "tickets.py", "submit.py", "retrieve.py", "schema.sql"):
            shutil.copy2(ROOT / name, self.project / name)

    def run_script(self, name, *arguments, input=""):
        return subprocess.run(
            [sys.executable, str(self.project / name), *arguments],
            input=input, text=True, capture_output=True, cwd=self.root, timeout=10,
        )

    def test_prompt_scripts_and_cli_share_database_from_another_directory(self):
        submitted = self.run_script("submit.py", input="Login\nCannot sign in\n")
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertIn("Ticket #1 submitted", submitted.stdout)
        retrieved = self.run_script("retrieve.py", input="1\n")
        self.assertEqual(retrieved.returncode, 0, retrieved.stderr)
        self.assertIn("Cannot sign in", retrieved.stdout)
        listing = self.run_script("tickets.py", "--json", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(json.loads(listing.stdout)[0]["title"], "Login")
        self.assertTrue((self.project / "tickets.db").is_file())
        self.assertFalse((self.root / "tickets.db").exists())

    def test_importing_modules_does_not_prompt_or_create_a_database(self):
        result = subprocess.run(
            [sys.executable, "-c", "import db, functions, tickets, submit, retrieve"],
            cwd=self.project, input="", text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.project / "tickets.db").exists())

    def test_keyed_cli_retry_survives_separate_processes_and_status_changes(self):
        arguments = ("--json", "create", "--title", "Login", "--message", "Broken",
                     "--idempotency-key", "cli-submission-1")
        first = self.run_script("tickets.py", *arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        ticket = json.loads(first.stdout)
        updated = self.run_script("tickets.py", "status", str(ticket["id"]), "closed")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        retry = self.run_script("tickets.py", *arguments)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout), ticket)
        rows = json.loads(self.run_script("tickets.py", "--json", "list").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")

    def test_cli_conflicting_submission_has_a_distinct_exit_code(self):
        arguments = ("--json", "create", "--title", "Login", "--idempotency-key", "cli-submission-1")
        self.assertEqual(self.run_script("tickets.py", *arguments, "--message", "First").returncode, 0)
        conflict = self.run_script("tickets.py", *arguments, "--message", "Different")
        self.assertEqual(conflict.returncode, 3)
        self.assertEqual(conflict.stdout, "")
        self.assertIn("different ticket content", conflict.stderr)
        self.assertNotIn("Traceback", conflict.stderr)
        self.assertEqual(len(json.loads(self.run_script("tickets.py", "--json", "list").stdout)), 1)

    def test_cli_invalid_key_does_not_create_a_database(self):
        result = self.run_script("tickets.py", "--json", "create", "--title", "Login",
                                 "--message", "Broken", "--idempotency-key", " ")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Idempotency key", result.stderr)
        self.assertFalse((self.project / "tickets.db").exists())


if __name__ == "__main__":
    unittest.main()
