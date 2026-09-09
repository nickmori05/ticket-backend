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


if __name__ == "__main__":
    unittest.main()
