import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    project = f"ticket-smoke-{uuid.uuid4().hex[:12]}"
    compose = ["docker", "compose", "--project-name", project]
    environment = {**os.environ, "TICKETS_PORT": "0"}

    def run(*arguments):
        return subprocess.run(
            [*compose, *arguments], cwd=ROOT, check=True,
            text=True, capture_output=True, env=environment, timeout=180,
        ).stdout

    def command(*arguments):
        return json.loads(run("run", "--rm", "-T", "tickets", "--json", *arguments))

    def request(base, path, method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
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
        run("up", "-d", "--no-build", "--wait", "--wait-timeout", "30", "api")
        address = api_address()
        status, response = request(address, f"/tickets/{identifier}")
        assert status == 200 and response["status"] == "closed"
        status, submitted = request(address, "/tickets", "POST",
                                    {"title": "HTTP ticket", "message": "Shared with CLI"})
        assert status == 201
        http_id = str(submitted["id"])
        assert command("show", http_id) == submitted
        request(address, f"/tickets/{http_id}/status", "PATCH", {"status": "closed", "note": "Done"})
        assert len(command("history", http_id)) == 2
        run("up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "30", "api")
        status, restored = request(api_address(), f"/tickets/{http_id}")
        assert status == 200 and restored["status"] == "closed"
        print("Docker CLI/API sharing, container recreation, and non-root execution passed.")
        return 0
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        return 1
    finally:
        subprocess.run([*compose, "down", "--volumes"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, env=environment, timeout=60)


if __name__ == "__main__":
    raise SystemExit(main())
