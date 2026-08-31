import os
import subprocess


TEST_ROOT = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.join(TEST_ROOT, "docker")
COMPOSE_FILE = os.path.join(TEST_DIR, "docker-compose.yml")


def compose_exec(
    container: str, *command: str, stdin: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a command in a service of the compose stack."""
    return subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", container, *command],
        cwd=TEST_DIR,
        input=stdin,
        capture_output=True,
        text=True,
        check=check,
    )


def unix_http(
    container: str,
    socket: str,
    method: str,
    path: str,
    body: str | None = None,
    check: bool = True,
) -> tuple[int, str]:
    """Call an HTTP endpoint that a unix socket makes available."""
    command = [
        "curl",
        "-s",
        "-o",
        "-",
        "-w",
        "\n%{http_code}",
        "--unix-socket",
        socket,
        "-X",
        method,
    ]
    if body is not None:
        command.extend(["--data-binary", "@-"])
    command.append(f"http://localhost{path}")

    result = compose_exec(container, *command, stdin=body, check=check)
    payload, _, status = result.stdout.rpartition("\n")
    return int(status), payload
