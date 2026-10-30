from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ticket_backend import cli
from ticket_backend.database import connect
from ticket_backend.tickets import (
    CommentNotFoundError, CommentPermissionError, TicketNotFoundError, add_comment, create_ticket, delete_comment,
    edit_comment, get_ticket, list_comments, ticket_history,
)


def stored_comment(database, comment_id):
    with closing(connect(database)) as connection:
        return dict(connection.execute("SELECT * FROM ticket_comments WHERE id = ?", (comment_id,)).fetchone())


class CommentEditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = create_ticket("Login", "Cannot sign in", self.database)["id"]

    def test_new_comments_are_unedited(self):
        comment = add_comment(self.ticket, "Asked for logs", self.database, author="alice")
        self.assertEqual((comment["edited_at"], comment["edited_by"]), (None, None))
        self.assertNotIn("deleted_at", comment)

    def test_edits_replace_the_text_and_record_who_and_when(self):
        comment = add_comment(self.ticket, "Asked for logz", self.database, author="alice")
        edited = edit_comment(self.ticket, comment["id"], " Asked for logs ", self.database, author="alice")
        self.assertEqual(edited["body"], "Asked for logs")
        self.assertEqual((edited["author"], edited["edited_by"]), ("alice", "alice"))
        self.assertEqual(edited["created_at"], comment["created_at"])
        self.assertIsNotNone(edited["edited_at"])
        self.assertEqual(list_comments(self.ticket, self.database), [edited])
        local = edit_comment(self.ticket, comment["id"], "Asked for full logs", self.database)
        self.assertEqual((local["author"], local["edited_by"]), ("alice", None))

    def test_saving_the_same_text_changes_nothing(self):
        comment = add_comment(self.ticket, "Asked for logs", self.database, author="alice")
        self.assertEqual(edit_comment(self.ticket, comment["id"], "Asked for logs ", self.database), comment)
        self.assertIsNone(stored_comment(self.database, comment["id"])["edited_at"])

    def test_authors_can_change_only_their_own_comments(self):
        theirs = add_comment(self.ticket, "From Bob", self.database, author="bob")
        local = add_comment(self.ticket, "From the terminal", self.database)
        for comment in (theirs, local):
            with self.subTest(author=comment["author"]):
                with self.assertRaisesRegex(CommentPermissionError, "alice can only change its own comments"):
                    edit_comment(self.ticket, comment["id"], "Rewritten", self.database, author="alice")
                with self.assertRaises(CommentPermissionError):
                    delete_comment(self.ticket, comment["id"], self.database, author="alice")
        self.assertEqual(list_comments(self.ticket, self.database), [theirs, local])
        # The terminal can already open the database, so it may change any comment.
        self.assertEqual(edit_comment(self.ticket, theirs["id"], "Moderated", self.database)["body"], "Moderated")

    def test_deleting_removes_the_text_and_hides_the_comment(self):
        kept = add_comment(self.ticket, "Keep this", self.database, author="alice")
        removed = add_comment(self.ticket, "password: hunter2", self.database, author="alice")
        deleted = delete_comment(self.ticket, removed["id"], self.database, author="alice")
        self.assertEqual((deleted["id"], deleted["ticket_id"], deleted["deleted_by"]),
                         (removed["id"], self.ticket, "alice"))
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertEqual(list_comments(self.ticket, self.database), [kept])
        stored = stored_comment(self.database, removed["id"])
        self.assertEqual((stored["body"], stored["author"]), ("", "alice"))
        later = add_comment(self.ticket, "Later", self.database)
        self.assertGreater(later["id"], removed["id"])

    def test_replaced_and_deleted_text_is_not_left_in_the_database_file(self):
        edited = add_comment(self.ticket, "first-secret-value", self.database)
        removed = add_comment(self.ticket, "second-secret-value", self.database)
        edit_comment(self.ticket, edited["id"], "Redacted", self.database)
        delete_comment(self.ticket, removed["id"], self.database)
        contents = self.database.read_bytes()
        self.assertNotIn(b"first-secret-value", contents)
        self.assertNotIn(b"second-secret-value", contents)
        self.assertIn(b"Redacted", contents)

    def test_deleted_comments_can_be_neither_edited_nor_deleted_again(self):
        comment = add_comment(self.ticket, "Asked for logs", self.database)
        delete_comment(self.ticket, comment["id"], self.database)
        with self.assertRaisesRegex(CommentNotFoundError, f"Comment #{comment['id']} was not found"):
            edit_comment(self.ticket, comment["id"], "Back again", self.database)
        with self.assertRaises(CommentNotFoundError):
            delete_comment(self.ticket, comment["id"], self.database)
        self.assertEqual(stored_comment(self.database, comment["id"])["body"], "")

    def test_comments_are_found_only_on_their_own_ticket(self):
        other = create_ticket("Billing", "Wrong invoice", self.database)["id"]
        comment = add_comment(self.ticket, "Login note", self.database)
        with self.assertRaises(CommentNotFoundError):
            edit_comment(other, comment["id"], "Moved", self.database)
        with self.assertRaises(CommentNotFoundError):
            delete_comment(other, comment["id"], self.database)
        with self.assertRaises(CommentNotFoundError):
            edit_comment(self.ticket, 99, "Missing", self.database)
        with self.assertRaises(TicketNotFoundError):
            edit_comment(99, comment["id"], "Missing ticket", self.database)
        self.assertEqual(list_comments(self.ticket, self.database), [comment])

    def test_invalid_input_changes_nothing(self):
        comment = add_comment(self.ticket, "Asked for logs", self.database, author="alice")
        for arguments in ((self.ticket, comment["id"], " "), (self.ticket, comment["id"], "x" * 5_001),
                          (self.ticket, 0, "Body"), (self.ticket, "1", "Body"), (0, comment["id"], "Body"),
                          (self.ticket, comment["id"], None)):
            with self.subTest(arguments=str(arguments)[:40]), self.assertRaises(ValueError):
                edit_comment(*arguments, self.database)
        with self.assertRaises(ValueError):
            edit_comment(self.ticket, comment["id"], "Body", self.database, author=" ")
        with self.assertRaises(ValueError):
            delete_comment(self.ticket, True, self.database)
        self.assertEqual(list_comments(self.ticket, self.database), [comment])

    def test_edits_and_deletions_leave_the_ticket_and_its_history_alone(self):
        comment = add_comment(self.ticket, "Asked for logs", self.database)
        before = (get_ticket(self.ticket, self.database), ticket_history(self.ticket, self.database))
        edit_comment(self.ticket, comment["id"], "Asked for full logs", self.database)
        delete_comment(self.ticket, comment["id"], self.database)
        self.assertEqual((get_ticket(self.ticket, self.database), ticket_history(self.ticket, self.database)), before)

    def test_comment_tables_from_older_databases_gain_the_new_columns(self):
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
                CREATE TABLE ticket_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                    body TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO tickets (title, message) VALUES ('Old', 'Before edits');
                INSERT INTO ticket_comments (ticket_id, body) VALUES (1, 'Old comment'), (1, 'Old typo');
            """)
        self.assertEqual([row["edited_at"] for row in list_comments(1, legacy)], [None, None])
        with self.assertRaises(CommentPermissionError):
            edit_comment(1, 1, "Claimed", legacy, author="alice")
        edit_comment(1, 2, "Old, fixed", legacy)
        delete_comment(1, 1, legacy)
        self.assertEqual([(row["id"], row["body"]) for row in list_comments(1, legacy)], [(2, "Old, fixed")])


class CommentEditCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "tickets.db"
        self.ticket = str(create_ticket("Login", "Cannot sign in", self.database)["id"])
        self.comment = str(add_comment(int(self.ticket), "Asked for logz", self.database, author="alice")["id"])

    def run_command(self, *arguments, json_mode=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["--database", str(self.database), *(["--json"] if json_mode else []), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_edit_and_list_in_text(self):
        self.assertEqual(self.run_command("edit-comment", self.ticket, self.comment, "Asked for logs"),
                         (0, f"Comment #{self.comment} on ticket #{self.ticket} saved.\n", ""))
        listed = self.run_command("comments", self.ticket)[1]
        self.assertRegex(listed, rf"^#{self.comment} .* UTC, alice \(edited .* UTC by local\):\n  Asked for logs\n$")

    def test_edit_and_delete_in_json(self):
        code, output, _errors = self.run_command("edit-comment", self.ticket, self.comment, "Fixed", json_mode=True)
        edited = json.loads(output)
        self.assertEqual((code, edited["body"], edited["edited_by"]), (0, "Fixed", None))
        code, output, _errors = self.run_command("delete-comment", self.ticket, self.comment, json_mode=True)
        deleted = json.loads(output)
        self.assertEqual((code, deleted["id"], deleted["deleted_by"]), (0, int(self.comment), None))
        self.assertEqual(json.loads(self.run_command("comments", self.ticket, json_mode=True)[1]), [])

    def test_delete_in_text(self):
        self.assertEqual(self.run_command("delete-comment", self.ticket, self.comment),
                         (0, f"Comment #{self.comment} deleted from ticket #{self.ticket}.\n", ""))
        self.assertEqual(self.run_command("comments", self.ticket)[1], "No comments on this ticket.\n")

    def test_errors_use_existing_exit_codes(self):
        for arguments, code, expected in ((("edit-comment", self.ticket, "99", "Body"), 1, "Comment #99 was not"),
                                          (("delete-comment", "99", self.comment), 1, "Ticket #99 was not"),
                                          (("edit-comment", self.ticket, self.comment, " "), 2, "must not be blank"),
                                          (("delete-comment", self.ticket, "0"), 2, "Comment ID must")):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertEqual(result[:2], (code, ""))
                self.assertIn(expected, result[2])
        self.assertEqual(list_comments(int(self.ticket), self.database)[0]["body"], "Asked for logz")


if __name__ == "__main__":
    unittest.main()
