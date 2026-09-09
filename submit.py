import sqlite3
import sys

from functions import create_ticket


def main() -> int:
    try:
        ticket = create_ticket(input("Topic: "), input("Message: "))
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nSubmission cancelled.", file=sys.stderr)
        return 130
    print(f"Ticket #{ticket['id']} submitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
