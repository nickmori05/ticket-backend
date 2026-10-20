import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SEND_THROUGH_STUB = """
import contextlib, io, json, os
from ticket_backend import cli
with SmtpStub() as stub:
    os.environ.update(TICKETS_SMTP_HOST="127.0.0.1", TICKETS_SMTP_PORT=str(stub.port))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(["--database", "/data/tickets.db", "--json", "send-notifications"])
print(json.dumps({"code": code, "counts": json.loads(output.getvalue()),
                  "recipients": [recipients for _sender, recipients, _data in stub.messages]}))
"""


def main() -> int:
    project = f"ticket-smoke-{uuid.uuid4().hex[:12]}"
    compose = ["docker", "compose", "--project-name", project]
    environment = {**os.environ, "TICKETS_PORT": "0"}

    def run(*arguments, input=None, check=True):
        result = subprocess.run(
            [*compose, *arguments], cwd=ROOT, check=check, input=input,
            text=True, capture_output=True, env=environment, timeout=180,
        )
        return result.stdout if check else result

    def command(*arguments):
        return json.loads(run("run", "--rm", "-T", "tickets", "--json", *arguments))

    token = None

    def request(base, path, method="GET", payload=None, idempotency_key=None, authorized=True):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        if authorized:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        with urlopen(Request(base + path, data=body, headers=headers, method=method), timeout=5) as response:
            return response.status, json.load(response)

    def api_address():
        address = run("port", "api", "8000").strip()
        assert address.startswith("127.0.0.1:"), "API port must be published on loopback"
        return "http://" + address

    try:
        run("build", "--quiet", "tickets")
        uid = int(run("run", "--rm", "-T", "--entrypoint", "python", "tickets",
                      "-c", "import os; print(os.getuid())"))
        assert uid != 0, "Container must run as a non-root user"
        created = command("create", "--title", "Container persistence", "--message", "Keep this ticket")
        identifier = str(created["id"])
        loaded = command("show", identifier)
        assert loaded == created, "Ticket did not survive the first container exiting"
        command("status", identifier, "closed", "--note", "Verified across containers")
        assert command("show", identifier)["status"] == "closed"
        assert len(command("history", identifier)) == 2
        assert command("stats")["total"] == 1
        token = command("token", "create", "smoke")["token"]
        run("up", "-d", "--no-build", "--wait", "--wait-timeout", "30", "api")
        address = api_address()
        try:
            request(address, f"/tickets/{identifier}", authorized=False)
            raise AssertionError("API accepted a request without a token")
        except HTTPError as error:
            assert error.code == 401, f"Expected 401 without a token, got {error.code}"
        status, response = request(address, f"/tickets/{identifier}")
        assert status == 200 and response["status"] == "closed"
        payload = {"title": "HTTP ticket", "message": "Shared with CLI", "email": "requester@example.com"}
        key = uuid.uuid4().hex
        status, submitted = request(address, "/tickets", "POST", payload, key)
        assert status == 201
        status, replayed = request(address, "/tickets", "POST", payload, key)
        assert status == 201 and replayed == submitted
        http_id = str(submitted["id"])
        assert command("show", http_id) == submitted
        request(address, f"/tickets/{http_id}/status", "PATCH", {"status": "closed", "note": "Done"})
        events = command("history", http_id)
        assert [event["author"] for event in events] == ["smoke", "smoke"], "CLI did not see status authors"
        assert command("history", identifier)[-1]["author"] is None, "CLI status change gained an author"
        queued = command("outbox", "--status", "pending")
        assert [(row["ticket_id"], row["recipient"]) for row in queued] == [(int(http_id), payload["email"])] * 2, \
            "CLI did not see notifications queued over HTTP"
        run("up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "30", "api")
        status, comment = request(api_address(), f"/tickets/{http_id}/comments", "POST", {"body": "From HTTP"})
        assert status == 201 and comment["author"] == "smoke"
        assert command("comments", http_id)[0]["author"] == "smoke", "CLI did not see the comment author"
        status, restored = request(api_address(), f"/tickets/{http_id}")
        assert status == 200 and restored["status"] == "closed"
        status, replayed = request(api_address(), "/tickets", "POST", payload, key)
        assert status == 201 and replayed == submitted, "Retry did not survive container recreation"
        assert command("create", "--title", payload["title"], "--message", payload["message"],
                       "--email", payload["email"], "--idempotency-key", key) == submitted
        assert command("show", http_id)["status"] == "closed", "Retry changed the current ticket"
        assert command("stats")["total"] == 2, "Retry created a duplicate ticket"
        assert len(command("history", http_id)) == 2, "Retry added duplicate history"
        assert len(command("outbox")) == 2, "Retry queued a duplicate notification"
        smtp = ["-e", "TICKETS_SMTP_FROM=support@example.com", "-e", "TICKETS_SMTP_SECURITY=none"]
        refused = run("run", "--rm", "-T", "-e", "TICKETS_SMTP_HOST=127.0.0.1", "-e", "TICKETS_SMTP_PORT=1", *smtp,
                      "tickets", "send-notifications", check=False)
        assert refused.returncode == 4 and "Could not connect" in refused.stderr, "Unreachable SMTP was not reported"
        assert [row["attempts"] for row in command("outbox")] == [0, 0], "Unreachable SMTP claimed notifications"
        # Run the image's sender against the test SMTP stub inside the same container, so no mail leaves it.
        driver = (ROOT / "tests" / "smtp_stub.py").read_text() + SEND_THROUGH_STUB
        delivered = json.loads(run("run", "--rm", "-T", *smtp, "--entrypoint", "python", "tickets", "-",
                                   input=driver).strip().splitlines()[-1])
        assert delivered == {"code": 0, "counts": {"sent": 2, "retrying": 0, "failed": 0},
                             "recipients": [[payload["email"]]] * 2}, f"SMTP delivery failed: {delivered}"
        assert {row["status"] for row in command("outbox")} == {"sent"}, "CLI did not see sent notifications"
        assert command("retry-notifications") == [], "Nothing should need retrying"
        command("token", "revoke", "smoke")
        try:
            request(api_address(), "/stats")
            raise AssertionError("API accepted a revoked token")
        except HTTPError as error:
            assert error.code == 401, f"Expected 401 for a revoked token, got {error.code}"
        print("Docker CLI/API sharing, API tokens, status attribution, notification outbox and SMTP delivery, "
              "persistent retries, container recreation, and non-root execution passed.")
        return 0
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        return 1
    finally:
        subprocess.run([*compose, "down", "--volumes"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, env=environment, timeout=60)


if __name__ == "__main__":
    raise SystemExit(main())
