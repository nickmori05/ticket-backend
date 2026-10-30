"""A minimal SMTP server on 127.0.0.1 for tests. It records messages and never relays them anywhere."""

import base64
from email import message_from_bytes, policy
import socketserver
import threading


class SmtpStub:
    def __init__(self, *, refuse=(), defer=(), disconnect=(), credentials=None):
        # refuse rejects a recipient permanently (550); defer rejects it temporarily (450).
        self.refuse = set(refuse)
        self.defer = set(defer)
        self.disconnect = set(disconnect)
        self.credentials = credentials
        self.messages = []
        self.connections = 0
        stub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                stub._session(self.rfile, self.wfile)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        return self

    def __exit__(self, *_exception):
        self.server.shutdown()
        self.server.server_close()

    def parsed(self):
        # Messages arrive with CRLF line endings; parse them as a mail client would see them.
        return [(recipients, message_from_bytes(data.replace(b"\r\n", b"\n"), policy=policy.default))
                for _sender, recipients, data in self.messages]

    def _session(self, rfile, wfile):
        self.connections += 1

        def reply(*lines):
            wfile.write(b"".join(line.encode() + b"\r\n" for line in lines))
            wfile.flush()

        reply("220 stub ESMTP")
        sender, recipients = None, []
        while line := rfile.readline():
            command = line.decode().rstrip("\r\n")
            verb = command.split(" ", 1)[0].upper()
            if verb == "EHLO":
                reply("250-stub", *(["250-AUTH PLAIN"] if self.credentials else []), "250 8BITMIME")
            elif verb == "HELO" or verb == "NOOP":
                reply("250 OK")
            elif verb == "AUTH":
                parts = command.split()
                given = base64.b64decode(parts[2]).split(b"\0")[1:] if len(parts) == 3 else []
                if self.credentials and tuple(part.decode() for part in given) == self.credentials:
                    reply("235 2.7.0 Authentication successful")
                else:
                    reply("535 5.7.8 Authentication credentials invalid")
            elif verb == "MAIL":
                sender, recipients = command.split(":", 1)[1].strip().strip("<>"), []
                reply("250 OK")
            elif verb == "RCPT":
                recipient = command.split(":", 1)[1].strip().strip("<>")
                if recipient in self.refuse:
                    reply("550 5.1.1 No such user")
                elif recipient in self.defer:
                    reply("450 4.2.1 Mailbox busy, try again later")
                else:
                    recipients.append(recipient)
                    reply("250 OK")
            elif verb == "DATA":
                if self.disconnect.intersection(recipients):
                    return
                reply("354 End data with <CR><LF>.<CR><LF>")
                data = []
                while (line := rfile.readline()) not in (b".\r\n", b""):
                    data.append(line[1:] if line.startswith(b".") else line)
                self.messages.append((sender, recipients, b"".join(data)))
                reply("250 2.0.0 Queued")
            elif verb == "RSET":
                sender, recipients = None, []
                reply("250 OK")
            elif verb == "QUIT":
                reply("221 Bye")
                return
            else:
                reply("502 5.5.2 Command not recognized")
