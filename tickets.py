import argparse
import json
from pathlib import Path
import sqlite3
import sys

from db import DEFAULT_DATABASE
from functions import (
    STATUSES, TicketNotFoundError, create_ticket, get_ticket, list_tickets,
    set_status, statistics, ticket_history,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Create, find, and track support tickets from the terminal.")
    root.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    root.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Submit a new ticket")
    create.add_argument("--title", help="Prompted for when omitted")
    create.add_argument("--message", help="Prompted for when omitted")
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
    commands.add_parser("stats", help="Count tickets by status")
    return root


def display(command: str, result) -> None:
    if command in ("create", "show", "status"):
        print(f"#{result['id']} [{result['status']}] {result['title']}")
        print(result["message"])
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
            print(f"{row['created_at']} UTC: {previous} -> {row['to_status']}")
            if row["note"]:
                print(f"  {row['note']}")
    elif command == "stats":
        print(f"Total: {result['total']}")
        for status, count in result["by_status"].items():
            print(f"{status}: {count}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "create":
            if args.json and (args.title is None or args.message is None):
                raise ValueError("JSON mode requires --title and --message; it never prompts.")
            title = args.title if args.title is not None else input("Topic: ")
            message = args.message if args.message is not None else input("Message: ")
            result = create_ticket(title, message, args.database)
        elif args.command == "show":
            result = get_ticket(args.id, args.database)
        elif args.command == "list":
            result = list_tickets(args.database, args.limit, offset=args.offset,
                                  status=args.status, search=args.search)
        elif args.command == "status":
            result = set_status(args.id, args.status, args.database, note=args.note)
        elif args.command == "history":
            result = ticket_history(args.id, args.database)
        else:
            result = statistics(args.database)
    except TicketNotFoundError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 130
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        display(args.command, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
