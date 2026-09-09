import json
from pathlib import Path
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    project = f"ticket-smoke-{uuid.uuid4().hex[:12]}"
    compose = ["docker", "compose", "--project-name", project]

    def run(*arguments):
        return subprocess.run(
            [*compose, *arguments], cwd=ROOT, check=True,
            text=True, capture_output=True,
        ).stdout

    def command(*arguments):
        return json.loads(run("run", "--rm", "-T", "tickets", "--json", *arguments))

    try:
        run("build", "--quiet")
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
        print("Docker persistence, status history, and non-root execution passed.")
        return 0
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        return 1
    finally:
        subprocess.run([*compose, "down", "--volumes"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
