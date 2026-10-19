from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
import ipaddress
import os
import re
import smtplib
import ssl
from typing import Mapping


SECURITY_MODES = ("starttls", "ssl", "none")
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
# 7bit makes non-ASCII bodies quoted-printable, so servers without 8BITMIME accept them.
MESSAGE_POLICY = policy.SMTP.clone(cte_type="7bit")


class DeliveryError(Exception):
    pass


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    sender: str
    port: int | None = None
    security: str = "starttls"
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    timeout: float = 10.0

    def __post_init__(self):
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", self.host):
            raise ValueError("SMTP host must be a host name or IP address.")
        address = parseaddr(self.sender)[1] if isinstance(self.sender, str) else ""
        if "\r" in str(self.sender) or "\n" in str(self.sender) or not re.fullmatch(r"[^@\s]+@[^@\s]+", address):
            raise ValueError("SMTP sender must be an address like support@example.com, "
                             "optionally with a display name.")
        if self.security not in SECURITY_MODES:
            raise ValueError(f"SMTP security must be one of: {', '.join(SECURITY_MODES)}.")
        if self.port is None:
            object.__setattr__(self, "port", DEFAULT_PORTS[self.security])
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("SMTP port must be between 1 and 65535.")
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP username and password must be set together.")
        # Refuse to send a password in plain text anywhere but a relay on this machine.
        if self.username is not None and self.security == "none" and not _loopback(self.host):
            raise ValueError("SMTP credentials need starttls or ssl unless the server is on localhost.")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 0 < self.timeout <= 300:
            raise ValueError("SMTP timeout must be between 0 and 300 seconds.")

    @property
    def domain(self) -> str:
        return parseaddr(self.sender)[1].rsplit("@", 1)[1]


def settings_from_environment(environment: Mapping[str, str] = os.environ) -> SmtpSettings:
    """Read SMTP settings from TICKETS_SMTP_* variables, raising ValueError when they are missing or invalid."""
    def value(name: str) -> str | None:
        text = environment.get(f"TICKETS_SMTP_{name}", "").strip()
        return text or None

    for name in ("HOST", "FROM"):
        if value(name) is None:
            raise ValueError(f"Set TICKETS_SMTP_{name} to send notifications.")
    numbers = {}
    for name, convert in (("PORT", int), ("TIMEOUT", float)):
        if value(name) is not None:
            try:
                numbers[name.lower()] = convert(value(name))
            except ValueError:
                raise ValueError(f"TICKETS_SMTP_{name} must be a number.") from None
    return SmtpSettings(
        host=value("HOST"),
        sender=value("FROM"),
        security=(value("SECURITY") or "starttls").lower(),
        username=value("USERNAME"),
        # Passwords are used exactly as given, since spaces can be part of them.
        password=environment.get("TICKETS_SMTP_PASSWORD") or None,
        **numbers,
    )


def build_message(notification: dict, settings: SmtpSettings) -> EmailMessage:
    message = EmailMessage(policy=MESSAGE_POLICY)
    message["From"] = settings.sender
    message["To"] = notification["recipient"]
    message["Subject"] = notification["subject"]
    message["Date"] = formatdate(usegmt=True)
    # Stable per notification, so a message sent twice after a crash can be recognized as a duplicate.
    stamp = re.sub(r"\D", "", notification["created_at"])
    message["Message-ID"] = f"<ticket-notification-{notification['id']}.{stamp}@{settings.domain}>"
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(notification["body"])
    return message


def _describe(error: Exception) -> str:
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "; ".join(f"Recipient {recipient} refused: {code} {reply.decode(errors='replace')}"
                         for recipient, (code, reply) in error.recipients.items())
    if isinstance(error, smtplib.SMTPResponseException):
        reply = error.smtp_error.decode(errors="replace") if isinstance(error.smtp_error, bytes) else error.smtp_error
        return f"{error.smtp_code} {reply}"
    return str(error) or type(error).__name__


class SmtpSender:
    """Send outbox notifications over one SMTP connection, reconnecting after a failed message.

    Use it as a context manager: entering connects and logs in, so an unreachable or misconfigured server is
    reported before any notification is claimed. Pass the sender to deliver_notifications().
    """

    def __init__(self, settings: SmtpSettings):
        self.settings = settings
        self._connection: smtplib.SMTP | None = None

    def __enter__(self) -> "SmtpSender":
        self.open()
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    def open(self) -> None:
        if self._connection is not None:
            return
        settings = self.settings
        try:
            if settings.security == "ssl":
                connection = smtplib.SMTP_SSL(settings.host, settings.port, timeout=settings.timeout,
                                              context=ssl.create_default_context())
            else:
                connection = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout)
        except (smtplib.SMTPException, OSError) as error:
            raise DeliveryError(f"Could not connect to SMTP server {settings.host}:{settings.port}: "
                                f"{_describe(error)}") from error
        try:
            if settings.security == "starttls":
                connection.starttls(context=ssl.create_default_context())
            if settings.username is not None:
                connection.login(settings.username, settings.password)
        except (smtplib.SMTPException, OSError) as error:
            connection.close()
            raise DeliveryError(f"SMTP server {settings.host}:{settings.port} rejected the session: "
                                f"{_describe(error)}") from error
        self._connection = connection

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.quit()
        except (smtplib.SMTPException, OSError):
            connection.close()

    def __call__(self, notification: dict) -> None:
        self.open()
        try:
            self._connection.send_message(build_message(notification, self.settings))
        except (smtplib.SMTPException, OSError) as error:
            # The session may be mid-transaction or gone, so start the next message on a fresh connection.
            self.close()
            raise DeliveryError(_describe(error)) from error
