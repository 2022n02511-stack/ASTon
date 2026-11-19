import subprocess
from pathlib import Path
import shutil
import tempfile
import threading
import time
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


def _is_network_failure(output: str) -> bool:
    """Return True only when output contains recognisable network failures."""

    output = output.lower()

    network_markers = (
        "temporary failure in name resolution",
        "could not resolve host",
        "name or service not known",
        "name resolution failed",
        "network is unreachable",
        "no route to host",
        "failed to establish a new connection",
        "connection timed out",
        "connect timeout",
        "read timeout",
        "max retries exceeded with url",
        "connectionerror",
        "name resolution error",
        "network is down",
    )

    return any(marker in output for marker in network_markers)


def _run_docker_with_progress(
    docker_cmd: list[str],
    timeout: int,
    description: str,
) -> tuple[int, str, str]:
    """Run a docker command silently with a Rich progress bar while capturing stdout and stderr."""
    process = subprocess.Popen(
        docker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _read_stream(pipe, sink: list[str]) -> None:
        if pipe is None:
            return
        try:
            for chunk in iter(lambda: pipe.read(4096), ""):
                sink.append(chunk)
        finally:
            pipe.close()

    t_out = threading.Thread(target=_read_stream, args=(process.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_read_stream, args=(process.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    start_time = time.monotonic()
    total_timeout = max(1, timeout)

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn(f"[bold cyan]{description}[/bold cyan]"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task_id = progress.add_task("verify", total=total_timeout)

        while process.poll() is None:
            elapsed = time.monotonic() - start_time
            if elapsed >= total_timeout:
                process.kill()
                t_out.join()
                t_err.join()
                process.wait()
                raise subprocess.TimeoutExpired(
                    cmd=docker_cmd,
                    timeout=timeout,
                    output="".join(stdout_chunks),
                    stderr="".join(stderr_chunks) or "Repository verification timed out.",
                )

            progress.update(task_id, completed=min(total_timeout, elapsed))
            time.sleep(0.1)

    t_out.join()
    t_err.join()
    returncode = process.returncode
    return returncode, "".join(stdout_chunks), "".join(stderr_chunks)


def _print_failure_summary(status: str, returncode: int | None, stdout: str, stderr: str) -> None:
    """Surface a concise failure summary on the terminal without dumping full logs."""
    print(f"[ERROR] Full-repo verification failed (status={status}, exit_code={returncode}).")
    combined = (stderr or stdout or "").strip()
    if combined:
        tail = "\n".join(combined.splitlines()[-25:])
        print(tail)


def verify_full_repo_in_docker(
    repo_root: str,
    migrated_files_path: str | Path,
    req_file: str | Path,
    *,
    python_image: str = "python:3.11-slim",
    timeout: int = 300,
    test_command: str = "python -m pytest -q",
) -> dict:
    """Verify the migrated repository inside an isolated Docker container with offline-first network fallback."""

    repo_path = Path(repo_root).resolve()
    migration_path = Path(migrated_files_path).resolve()
    requirements_path = Path(req_file).resolve()

    if not repo_path.is_dir():
        return {
            "status": "INVALID_REPO",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Repository directory not found: {repo_path}",
        }

    if not migration_path.is_dir():
        return {
            "status": "INVALID_MIGRATIONS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Migrated files directory not found: {migration_path}",
        }

    if not requirements_path.is_file():
        return {
            "status": "INVALID_REQUIREMENTS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Requirements file not found: {requirements_path}",
        }

    test_cmd = test_command.strip()

    try:
        with tempfile.TemporaryDirectory(prefix="aston_verify_") as temp_dir:

            verification_repo = Path(temp_dir) / "repo"

            # 1. Copy the original repository.
            shutil.copytree(
                repo_path,
                verification_repo,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".pytest_cache",
                ),
            )

            # 2. Overlay migrated files, preserving their relative paths.
            for migrated_file in migration_path.rglob("*"):

                if not migrated_file.is_file():
                    continue

                relative_path = migrated_file.relative_to(migration_path)

                # Requirements are handled separately below.
                if relative_path.as_posix() == "requirements.txt":
                    continue

                destination = verification_repo / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)

                shutil.copy2(migrated_file, destination)

            # 3. Replace requirements with the target requirements.
            shutil.copy2(
                requirements_path,
                verification_repo / "requirements.txt",
            )

            # 4. Run the assembled repository inside Docker.
            script = f"""
export PIP_CACHE_DIR=/root/.cache/pip
set -eu

echo "--- [Verification] Installing requirements ---"
python -m pip install --disable-pip-version-check -r /app/requirements.txt ||
    {{ echo "ASTON_SETUP_FAILED"; exit 20; }}
"""

            if test_cmd:
                if "pytest" in test_cmd.lower():
                    script += """
echo "--- [Verification] Checking test runner ---"
if ! python -c "import pytest" 2>/dev/null; then
    python -m pip install --disable-pip-version-check pytest ||
        { echo "ASTON_PYTEST_SETUP_FAILED"; exit 23; }
fi
"""

            script += """
echo "--- [Verification] Running AST syntax checks ---"
python -c '
import ast
import sys
from pathlib import Path

root = Path("/app")

excluded = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
}

errors = []

for path in root.rglob("*.py"):
    if any(part in excluded for part in path.parts):
        continue

    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        errors.append((path, exc))

for path, exc in errors:
    print("Syntax error in {}: {}".format(path, exc), file=sys.stderr)

sys.exit(1 if errors else 0)
' || { echo "ASTON_SYNTAX_FAILED"; exit 21; }
"""

            if test_cmd:
                if "pytest" in test_cmd.lower():
                    script += f"""
echo "--- [Verification] Running test suite ---"
set +e
export PYTEST_ADDOPTS="${{PYTEST_ADDOPTS:-}} -p no:cacheprovider"
{test_cmd}
test_status=$?
set -e

if [ "$test_status" -eq 5 ]; then
    echo "ASTON_TESTS_SKIPPED_NO_TESTS: pytest collected no tests."
elif [ "$test_status" -ne 0 ]; then
    echo "ASTON_TESTS_FAILED: exit code $test_status"
    exit 22
else
    echo "ASTON_TESTS_PASSED"
fi
"""
                else:
                    script += f"""
echo "--- [Verification] Running test suite ---"
{test_cmd} ||
    {{ echo "ASTON_TESTS_FAILED"; exit 22; }}
"""

            script += """
echo "ASTON_VERIFICATION_PASSED"
"""

            network_modes = ["none", "bridge"]
            attempts = []
            returncode = 1
            stdout = ""
            stderr = ""
            network_mode = None

            for mode in network_modes:

                docker_cmd = [
                    "docker", "run", "--rm",
                    "--network", mode,
                    "-v", f"{verification_repo}:/app:ro,Z",
                    "-v", "aston-pip-cache:/root/.cache/pip",
                    "--workdir", "/app",
                    python_image,
                    "sh", "-c", script,
                ]

                returncode, stdout, stderr = _run_docker_with_progress(
                    docker_cmd=docker_cmd,
                    timeout=timeout,
                    description=f"Verifying repository in Docker ({mode})",
                )

                output = stdout + "\n" + stderr

                attempts.append({
                    "network_mode": mode,
                    "exit_code": returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                })

                network_mode = mode

                # Successful verification: stop immediately.
                if (
                    returncode == 0
                    and "ASTON_VERIFICATION_PASSED" in output
                ):
                    break

                # Only retry when offline execution clearly failed
                # because of network connectivity.
                if mode == "none" and _is_network_failure(output):
                    continue

                # Do not retry migration, syntax, test, or other failures.
                break

            output = stdout + "\n" + stderr

            if (
                returncode == 0
                and "ASTON_VERIFICATION_PASSED" in output
            ):
                status = "PASS"

            elif "ASTON_SETUP_FAILED" in output:
                status = "SETUP_ERROR"

            elif "ASTON_PYTEST_SETUP_FAILED" in output:
                status = "SETUP_ERROR"

            elif "ASTON_SYNTAX_FAILED" in output:
                status = "SYNTAX_ERROR"

            elif "ASTON_TESTS_FAILED" in output:
                status = "TEST_FAILED"

            elif _is_network_failure(output):
                status = "NETWORK_ERROR"

            else:
                status = "DOCKER_ERROR"

            if status != "PASS":
                _print_failure_summary(status, returncode, stdout, stderr)

            return {
                "status": status,
                "success": status == "PASS",
                "exit_code": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "network_mode": network_mode,
                "attempts": attempts,
            }

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or "Repository verification timed out."

        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")

        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

        _print_failure_summary("TIMEOUT", None, stdout, stderr)

        return {
            "status": "TIMEOUT",
            "success": False,
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
        }

    except OSError as exc:
        _print_failure_summary("DOCKER_ERROR", None, "", str(exc))
        return {
            "status": "DOCKER_ERROR",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
        }