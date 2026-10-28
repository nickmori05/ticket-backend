"""The tickets.py command line: every storage operation, with text or JSON output and stable exit codes."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from ticket_backend.audit import list_audit, prune_audit
from ticket_backend.database import DEFAULT_DATABASE
from ticket_backend.mailer import DeliveryError, SmtpSender, settings_from_environment
from ticket_backend.notifications import (
    NOTIFICATION_STATUSES, NotificationNotFoundError, count_due_notifications, deliver_notifications,
    list_notifications, prune_notifications, retry_notifications,
)
from ticket_backend.submissions import prune_submission_keys
from ticket_backend.tickets import (
    STATUSES, IdempotencyConflictError, TicketNotFoundError, add_comment, create_ticket, get_ticket, list_comments,
    list_tickets, set_status, statistics, ticket_history,
)
from ticket_backend.tokens import SCOPES, TokenNotFoundError, create_token, list_tokens, revoke_token


def parser() -> argparse.ArgumentParser:
    """Build the argument parser for every command."""
    root = argparse.ArgumentParser(description="Create, find, and track support tickets from the terminal.")
    root.add_argument("--database", type=Path, default=DEFAULT_DATABASE,
                      help="SQLite file to use (default: tickets.db in the project folder)")
    root.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Submit a new ticket")
    create.add_argument("--title", help="Prompted for when omitted")
    create.add_argument("--message", help="Prompted for when omitted")
    create.add_argument("--idempotency-key",
                        help="Reuse this key when retrying the same submission (separate from API tokens' keys)")
    create.add_argument("--email", help="Requester address to notify of status changes")
    show = commands.add_parser("show", help="Retrieve one ticket")
    show.add_argument("id", type=int)
    listing = commands.add_parser("list", help="Find recent tickets")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--search", help="Match literal text in title or message")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    status = commands.add_parser("status", help="Change a ticket's status")
    status.add_argument("id", type=int)
    status.add_argument("status", choices=STATUSES)
    status.add_argument("--note", default="", help="Reason for this change (up to 500 characters)")
    history = commands.add_parser("history", help="Show a ticket's recorded changes")
    history.add_argument("id", type=int)
    comment = commands.add_parser("comment", help="Add a comment to a ticket")
    comment.add_argument("id", type=int)
    comment.add_argument("body", help="Comment text (up to 5000 characters)")
    comments = commands.add_parser("comments", help="Show a ticket's comments")
    comments.add_argument("id", type=int)
    commands.add_parser("stats", help="Count tickets by status")
    outbox = commands.add_parser("outbox", help="Show queued notification emails, oldest first")
    outbox.add_argument("--status", choices=NOTIFICATION_STATUSES)
    outbox.add_argument("--ticket", type=int, metavar="ID", help="Only notifications for this ticket")
    outbox.add_argument("--limit", type=int, default=20)
    outbox.add_argument("--offset", type=int, default=0)
    send = commands.add_parser("send-notifications", help="Send due notifications over SMTP (TICKETS_SMTP_* settings)")
    send.add_argument("--limit", type=int, default=20, help="Most notifications to send in this run (1-100)")
    send.add_argument("--max-attempts", type=int, default=5, help="Attempts before a notification is marked failed")
    send.add_argument("--retry-delay", type=int, default=60, help="Seconds before the first retry; doubles each time")
    retry = commands.add_parser("retry-notifications", help="Requeue failed notifications with a fresh attempt budget")
    retry.add_argument("ids", type=int, nargs="*", metavar="ID", help="Notifications to requeue; default all failed")
    prune = commands.add_parser("prune-notifications", help="Delete notifications sent more than a number of days ago")
    prune.add_argument("--older-than", type=int, required=True, metavar="DAYS")
    token = commands.add_parser("token", help="Manage API tokens")
    actions = token.add_subparsers(dest="action", required=True)
    token_create = actions.add_parser("create", help="Create a token; it is shown only once")
    token_create.add_argument("name", help="1-64 letters, digits, dots, underscores, or hyphens")
    token_create.add_argument("--scope", action="append", choices=SCOPES, dest="scopes",
                              help="Grant one scope; repeat for more (default: every scope)")
    token_create.add_argument("--expires-in", type=int, metavar="DAYS",
                              help="Stop the token working after this many days (default: never)")
    token_create.add_argument("--rate-limit", type=int, metavar="N",
                              help="Requests per minute, or 0 for no limit (default: the API's TICKETS_RATE_LIMIT)")
    actions.add_parser("list", help="Show tokens with their scopes and whether they are active")
    token_revoke = actions.add_parser("revoke", help="Stop a token from authenticating")
    token_revoke.add_argument("name")
    audit = commands.add_parser("audit", help="Review or prune the log of API requests")
    audit_actions = audit.add_subparsers(dest="action", required=True)
    audit_list = audit_actions.add_parser("list", help="Show recorded API requests, newest first")
    audit_list.add_argument("--token", metavar="NAME", help="Only requests made by this token")
    audit_list.add_argument("--ticket", type=int, metavar="ID", help="Only requests about this ticket")
    audit_list.add_argument("--limit", type=int, default=20)
    audit_list.add_argument("--offset", type=int, default=0)
    audit_prune = audit_actions.add_parser("prune", help="Delete recorded requests older than a number of days")
    audit_prune.add_argument("--older-than", type=int, required=True, metavar="DAYS")
    keys = commands.add_parser("keys", help="Manage saved idempotency keys")
    key_actions = keys.add_subparsers(dest="action", required=True)
    keys_prune = key_actions.add_parser("prune", help="Delete idempotency keys saved more than a number of days ago")
    keys_prune.add_argument("--older-than", type=int, required=True, metavar="DAYS")
    return root


def display(command: str, result, action: str | None = None) -> None:
    """Print a command's result as human-readable text."""
    if command in ("create", "show", "status"):
        print(f"#{result['id']} [{result['status']}] {result['title']}")
        print(result["message"])
        if result.get("email"):
            print(f"Contact: {result['email']}")
        print(f"Created: {result['created_at']} UTC")
    elif command == "list":
        if not result:
            print("No tickets found.")
        for row in result:
            print(f"#{row['id']} [{row['status']}] {row['title']}")
    elif command == "history":
        if not result:
            print("No recorded changes for this ticket.")
        for row in result:
            previous = row["from_status"] or "created"
            print(f"{row['created_at']} UTC, {row['author'] or 'local'}: {previous} -> {row['to_status']}")
            if row["note"]:
                print(f"  {row['note']}")
    elif command == "comment":
        print(f"Comment #{result['id']} added to ticket #{result['ticket_id']}")
    elif command == "comments":
        if not result:
            print("No comments on this ticket.")
        for row in result:
            print(f"{row['created_at']} UTC, {row['author'] or 'local'}:")
            print(f"  {row['body']}")
    elif command == "outbox":
        if not result:
            print("No notifications found.")
        for row in result:
            attempts = f"{row['attempts']} attempt" + ("" if row["attempts"] == 1 else "s")
            print(f"#{row['id']} [{row['status']}, {attempts}] {row['recipient']}: {row['subject']}")
            if row["last_error"]:
                print(f"  Last error: {row['last_error']}")
    elif command == "send-notifications":
        print(f"Sent {result['sent']}, retrying {result['retrying']}, failed {result['failed']}.")
    elif command == "retry-notifications":
        if not result:
            print("No failed notifications to retry.")
        for row in result:
            print(f"#{row['id']} requeued for {row['recipient']}: {row['subject']}")
    elif command == "prune-notifications":
        print(f"Deleted {result['deleted']} sent notification" + ("." if result["deleted"] == 1 else "s."))
    elif command == "stats":
        print(f"Total: {result['total']}")
        for status, count in result["by_status"].items():
            print(f"{status}: {count}")
    elif command == "audit" and action == "list":
        if not result:
            print("No recorded API requests.")
        for row in result:
            ticket = f" (ticket #{row['ticket_id']})" if row["ticket_id"] is not None else ""
            print(f"{row['created_at']} UTC, {row['token']}: {row['method']} {row['path']} -> {row['status']}{ticket}")
    elif command == "audit":
        print(f"Deleted {result['deleted']} recorded API request" + ("." if result["deleted"] == 1 else "s."))
    elif command == "keys":
        print(f"Deleted {result['deleted']} idempotency key" + ("." if result["deleted"] == 1 else "s."))
    elif action == "create":
        print(f"Token for {result['name']}: {result['token']}")
        print(f"Scopes: {' '.join(result['scopes'])}")
        if result["rate_limit"] is not None:
            print(f"Rate limit: {'none' if result['rate_limit'] == 0 else rate_limit_text(result['rate_limit'])}")
        if result["expires_at"]:
            print(f"Expires: {result['expires_at']} UTC")
        print("Store it now; it cannot be shown again.")
    elif action == "list":
        if not result:
            print("No API tokens.")
        for row in result:
            details = [f"created {row['created_at']} UTC"]
            if row["revoked_at"]:
                details.append(f"revoked {row['revoked_at']} UTC")
            elif row["expires_at"]:
                details.append(f"{'expired' if row['state'] == 'expired' else 'expires'} {row['expires_at']} UTC")
            if row["rate_limit"] is not None:
                details.append(rate_limit_text(row["rate_limit"]))
            print(f"{row['name']} [{row['state']}] {' '.join(row['scopes'])} ({', '.join(details)})")
    elif action == "revoke":
        print(f"Token {result['name']} revoked.")


def rate_limit_text(limit: int) -> str:
    return "no rate limit" if limit == 0 else f"{limit} requests per minute"


def send_notifications(args: argparse.Namespace) -> dict:
    """Send one batch of due notifications over SMTP and return the delivery counts."""
    # Settings and arguments are checked, and the server reached, before any notification is claimed.
    settings = settings_from_environment()
    if not 1 <= args.limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")
    if args.max_attempts < 1 or args.retry_delay < 0:
        raise ValueError("Maximum attempts must be positive and the retry delay must not be negative.")
    if count_due_notifications(args.database) == 0:
        return {"sent": 0, "retrying": 0, "failed": 0}
    with SmtpSender(settings) as sender:
        return deliver_notifications(sender, args.database, limit=args.limit,
                                     max_attempts=args.max_attempts, retry_delay=args.retry_delay)


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code.

    0 means success, 1 a missing ticket, token, or notification, 2 invalid input or a storage error, 3 a reused
    submission key with different content, 4 an unavailable SMTP server, and 130 cancellation.
    """
    args = parser().parse_args(argv)
    try:
        if args.command == "create":
            if args.json and (args.title is None or args.message is None):
                raise ValueError("JSON mode requires --title and --message; it never prompts.")
            title = args.title if args.title is not None else input("Topic: ")
            message = args.message if args.message is not None else input("Message: ")
            result = create_ticket(title, message, args.database, idempotency_key=args.idempotency_key,
                                   email=args.email)
        elif args.command == "show":
            result = get_ticket(args.id, args.database)
        elif args.command == "list":
            result = list_tickets(args.database, args.limit, offset=args.offset,
                                  status=args.status, search=args.search)
        elif args.command == "status":
            result = set_status(args.id, args.status, args.database, note=args.note)
        elif args.command == "history":
            result = ticket_history(args.id, args.database)
        elif args.command == "comment":
            result = add_comment(args.id, args.body, args.database)
        elif args.command == "comments":
            result = list_comments(args.id, args.database)
        elif args.command == "stats":
            result = statistics(args.database)
        elif args.command == "outbox":
            result = list_notifications(args.database, args.limit, offset=args.offset, status=args.status,
                                        ticket_id=args.ticket)
        elif args.command == "send-notifications":
            result = send_notifications(args)
        elif args.command == "retry-notifications":
            result = retry_notifications(args.database, args.ids or None)
        elif args.command == "prune-notifications":
            result = prune_notifications(args.older_than, args.database)
        elif args.command == "audit" and args.action == "list":
            result = list_audit(args.database, args.limit, offset=args.offset, token=args.token, ticket_id=args.ticket)
        elif args.command == "audit":
            result = prune_audit(args.older_than, args.database)
        elif args.command == "keys":
            result = prune_submission_keys(args.older_than, args.database)
        elif args.action == "create":
            result = create_token(args.name, args.database, scopes=args.scopes, expires_in_days=args.expires_in,
                                  rate_limit=args.rate_limit)
        elif args.action == "list":
            result = list_tokens(args.database)
        else:
            result = revoke_token(args.name, args.database)
    except (TicketNotFoundError, TokenNotFoundError, NotificationNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except IdempotencyConflictError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 3
    except DeliveryError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 4
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        display(args.command, result, getattr(args, "action", None))
    return 0
