"""Retrieve a ticket by its ID."""

import sqlite3
import sys

from functions import TicketNotFoundError, get_ticket


def main() -> int:
    try:
        ticket = get_ticket(int(input("Ticket ID: ")))
    except TicketNotFoundError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nRetrieval cancelled.", file=sys.stderr)
        return 130
    print(f"#{ticket['id']} [{ticket['status']}] {ticket['title']}")
    print(ticket["message"])
    print(f"Created: {ticket['created_at']} UTC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
