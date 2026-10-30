"""Input checks shared by the storage modules. Each raises ValueError with a message safe to show to callers."""

import re


def require_text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank.")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters.")
    return value


def optional_author(author: str | None) -> str | None:
    return None if author is None else require_text(author, "Author", 64)


def optional_email(email: str | None) -> str | None:
    if email is None:
        return None
    email = email.strip() if isinstance(email, str) else ""
    # ASCII only, with no spaces, commas, quotes, or angle brackets, so a sender can use it as a header unchanged.
    if len(email) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", email):
        raise ValueError("Email must be an ASCII address like name@example.com, at most 254 characters.")
    return email


def require_token_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("Token name must be 1-64 ASCII letters, digits, dots, underscores, or hyphens.")
    return name


def require_page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")
    if type(offset) is not int or offset < 0:
        raise ValueError("Offset must be a non-negative integer.")


def require_id(identifier: int, name: str = "Ticket ID") -> int:
    if type(identifier) is not int or identifier < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return identifier


MAX_RETENTION_DAYS = 3650


def require_age_days(days: int) -> str:
    """Check a retention age and return it as an SQLite datetime modifier, such as '-30 days'."""
    if type(days) is not int or not 1 <= days <= MAX_RETENTION_DAYS:
        raise ValueError(f"Age must be a whole number of days from 1 to {MAX_RETENTION_DAYS}.")
    return f"-{days} days"
