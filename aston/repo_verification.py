import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from aston.cli.console import error

logger = logging.getLogger(__name__)


def _is_network_failure(output: str) -> bool:
    """Detect recognisable DNS/resolution or network-unreachable failures."""

    output = output.lower()

    network_markers = (
        "temporary failure in name resolution",
        "could not resolve host",
        "could not resolve",
        "name or service not known",
        "name resolution failed",
        "name resolution error",
        "no address associated with hostname",
        "nodename nor servname provided",
        "getaddrinfo failed",
        "network is unreachable",
        "no route to host",
        "network is down",
        "connection refused",
        "failed to establish a new connection",
        "socket.gaierror",
        "newconnectionerror",
        "dns lookup",
    )

    return any(marker in output for marker in network_markers)


def _run_docker_with_progress(
    docker_cmd: list[str],
    timeout: int,
    description: str,
) -> tuple[int, str, str]:
    """Run Docker while capturing output and enforcing a timeout."""

    process = subprocess.Popen(
        docker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )

    stdout_chunks = []
    stderr_chunks = []

    def _read_stream(pipe, sink):
        if pipe is None:
            return

        try:
            for chunk in iter(lambda: pipe.read(4096), ""):
                sink.append(chunk)
        finally:
            pipe.close()

    t_out = threading.Thread(
        target=_read_stream,
        args=(process.stdout, stdout_chunks),
        daemon=True,
    )

    t_err = threading.Thread(
        target=_read_stream,
        args=(process.stderr, stderr_chunks),
        daemon=True,
    )

    t_out.start()
    t_err.start()

    start_time = time.monotonic()
    total_timeout = max(1, timeout)
    timed_out = False

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn(f"[bold cyan]{description}[/bold cyan]"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:

        task_id = progress.add_task(
            "verify",
            total=total_timeout,
        )

        while process.poll() is None:
            elapsed = time.monotonic() - start_time

            if elapsed >= total_timeout:
                timed_out = True
                logger.warning(
                    "Full-repo Docker verification timed out after %ds",
                    total_timeout,
                )

                try:
                    if os.name == "posix":
                        import signal

                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()

                    process.wait(timeout=5)

                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        if os.name == "posix":
                            import signal

                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass

                    process.wait()

                break

            progress.update(
                task_id,
                completed=min(total_timeout, elapsed),
            )

            time.sleep(0.1)

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)

    if timed_out:
        raise subprocess.TimeoutExpired(
            cmd=docker_cmd,
            timeout=timeout,
            output=stdout,
            stderr=stderr or "Repository verification timed out.",
        )

    logger.debug("Full-repo Docker command exited with code=%s", process.returncode)
    return process.returncode, stdout, stderr


def _ensure_docker_image(
    docker: str,
    python_image: str,
    timeout: int,
) -> tuple[bool, str]:
    """Ensure the configured base image is available locally."""

    try:
        inspect = subprocess.run(
            [docker, "image", "inspect", python_image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        if inspect.returncode == 0:
            logger.debug("Docker base image %s found locally", python_image)
            return True, ""

        logger.info("Pulling Docker base image %s (timeout=%ds)", python_image, timeout)
        pull = subprocess.run(
            [docker, "pull", python_image],
            capture_output=True,
            text=True,
            timeout=max(1, timeout),
            check=False,
        )

        if pull.returncode != 0:
            err_msg = (
                pull.stderr.strip()
                or pull.stdout.strip()
                or "Docker could not pull the base image."
            )
            logger.error("Failed pulling Docker base image %s: %s", python_image, err_msg)
            return False, err_msg

        return True, ""

    except subprocess.TimeoutExpired:
        logger.error("Docker base-image pull timed out for %s", python_image)
        return False, "Docker base-image pull timed out."

    except OSError as exc:
        logger.error("OS error checking/pulling Docker image %s: %s", python_image, exc)
        return False, str(exc)


def _remove_container(
    docker: str,
    container_id_file: Path,
) -> None:
    """Remove a container left behind after a timeout."""

    try:
        if not container_id_file.is_file():
            return

        container_id = container_id_file.read_text(
            encoding="utf-8"
        ).strip()

        if not container_id:
            return

        subprocess.run(
            [
                docker,
                "rm",
                "-f",
                container_id,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        logger.debug("Removed container %s from cidfile %s", container_id, container_id_file)

    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Failed removing container from %s: %s", container_id_file, exc)


def _print_failure_summary(
    status: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> None:
    """Log and report a concise failure summary."""

    summary = (
        f"Full-repo verification failed (status={status}, exit_code={returncode})."
    )
    error(summary)
    logger.error(summary)

    combined = (stderr or stdout or "").strip()

    if combined:
        tail = "\n".join(combined.splitlines()[-25:])
        logger.debug("Full-repo verification failure tail:\n%s", tail)


def _classify_verification_output(
    output: str,
    returncode: int | None,
) -> str:
    """Map verification output to a structured status."""

    if "ASTON_SETUP_FAILED" in output:
        return "SETUP_ERROR"

    if "ASTON_PYTEST_SETUP_FAILED" in output:
        return "SETUP_ERROR"

    if "ASTON_SYNTAX_FAILED" in output:
        return "SYNTAX_ERROR"

    if "ASTON_TESTS_NO_TESTS" in output:
        return "NO_TESTS"

    if "ASTON_TESTS_FAILED" in output:
        return "TEST_FAILED"

    if "ASTON_VERIFICATION_PASSED" in output and returncode == 0:
        return "PASS"

    if _is_network_failure(output):
        return "NETWORK_ERROR"

    return "DOCKER_ERROR"


def verify_full_repo_in_docker(
    repo_root: str,
    migrated_files_path: str | Path,
    req_file: str | Path,
    *,
    python_image: str = "python:3.11-slim",
    timeout: int = 300,
    test_command: str = "python -m pytest -q",
    network_mode: str = "auto",
    preserve_context: bool = False,
) -> dict:
    """
    Verify the assembled migrated repository inside Docker.

    Workflow:
    1. Validate input paths.
    2. Ensure Docker and the base image are available.
    3. Copy the original repository into a temporary directory.
    4. Overlay migrated files.
    5. Install target requirements.
    6. Run AST syntax checks.
    7. Run the configured test command.
    8. Retry with bridge networking only when network_mode="auto" and
       offline execution hits a recognisable network failure.

    PASS means the configured verification steps passed.
    It does not prove behavioural equivalence.
    """

    started = time.monotonic()
    requested_network = (
        network_mode if network_mode in {"auto", "none", "bridge"} else "auto"
    )

    repo_path = Path(repo_root).expanduser().resolve()
    migration_path = Path(migrated_files_path).expanduser().resolve()
    requirements_path = Path(req_file).expanduser().resolve()

    if not repo_path.is_dir():
        return {
            "status": "INVALID_REPO",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Repository directory not found: {repo_path}",
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    if not migration_path.is_dir():
        return {
            "status": "INVALID_MIGRATIONS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": (
                f"Migrated files directory not found: {migration_path}"
            ),
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    if not requirements_path.is_file():
        return {
            "status": "INVALID_REQUIREMENTS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": (
                f"Requirements file not found: {requirements_path}"
            ),
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    test_cmd = test_command.strip()

    docker = shutil.which("docker")

    if docker is None:
        return {
            "status": "DOCKER_UNAVAILABLE",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "Docker CLI was not found on PATH.",
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    # Check the Docker daemon.
    try:
        daemon_check = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    except subprocess.TimeoutExpired:
        return {
            "status": "DOCKER_UNAVAILABLE",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "Docker daemon check timed out.",
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    except OSError as exc:
        return {
            "status": "DOCKER_UNAVAILABLE",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    if daemon_check.returncode != 0:
        return {
            "status": "DOCKER_UNAVAILABLE",
            "success": False,
            "exit_code": None,
            "stdout": daemon_check.stdout,
            "stderr": daemon_check.stderr or "Docker daemon unavailable.",
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    # Pull the base image before running offline-first containers.
    image_ok, image_error = _ensure_docker_image(
        docker,
        python_image,
        timeout,
    )

    if not image_ok:
        return {
            "status": "IMAGE_ERROR",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": image_error,
            "network_mode": None,
            "attempts": [],
            "duration_seconds": time.monotonic() - started,
        }

    attempts = []
    stdout = ""
    stderr = ""
    returncode = None
    active_network = None

    temp_dir = tempfile.mkdtemp(prefix="aston_verify_")
    verification_repo = Path(temp_dir) / "repo"

    logger.debug(
        "Starting full-repo verification (repo=%s, staging=%s, image=%s, network_mode=%s, test_cmd=%r)",
        repo_path,
        migration_path,
        python_image,
        requested_network,
        test_cmd,
    )

    try:
        # 1. Copy original repository
        shutil.copytree(
            repo_path,
            verification_repo,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".tox",
                ".nox",
                ".idea",
                ".vscode",
                "node_modules",
                ".aston",
            ),
        )

        # 2. Overlay migrated files
        for migrated_file in migration_path.rglob("*"):
            if not migrated_file.is_file():
                continue

            relative_path = migrated_file.relative_to(
                migration_path
            )

            if relative_path.as_posix() == "requirements.txt":
                continue

            destination = verification_repo / relative_path
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            shutil.copy2(
                migrated_file,
                destination,
            )

        # 3. Replace target requirements
        shutil.copy2(
            requirements_path,
            verification_repo / "requirements.txt",
        )

        # 4. Construct verification script
        script = r'''
set -eu

export PIP_CACHE_DIR=/root/.cache/pip
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

echo "--- [Verification] Installing requirements ---"
python -m pip install --disable-pip-version-check -r /app/requirements.txt ||
    { echo "ASTON_SETUP_FAILED"; exit 20; }
'''

        if test_cmd and "pytest" in test_cmd.lower():
            script += r'''
echo "--- [Verification] Checking test runner ---"
if ! python -c "import pytest" 2>/dev/null; then
    python -m pip install --disable-pip-version-check pytest ||
        { echo "ASTON_PYTEST_SETUP_FAILED"; exit 23; }
fi
'''

        script += r'''
echo "--- [Verification] Running AST syntax checks ---"

if ! python - <<'PY'
import ast
import sys
import tokenize
from pathlib import Path

root = Path("/app")

excluded = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    ".aston",
}

errors = []

for path in root.rglob("*.py"):
    if any(part in excluded for part in path.relative_to(root).parts):
        continue

    try:
        with tokenize.open(str(path)) as source_file:
            source = source_file.read()

        ast.parse(source, filename=str(path))

    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        errors.append((path, exc))

for path, exc in errors:
    print(
        "Syntax error in {}: {}".format(path, exc),
        file=sys.stderr,
    )

sys.exit(1 if errors else 0)
PY
then
    echo "ASTON_SYNTAX_FAILED"
    exit 21
fi
'''

        if test_cmd:
            if "pytest" in test_cmd.lower():
                script += f'''
echo "--- [Verification] Running test suite ---"

set +e
export PYTEST_ADDOPTS="${{PYTEST_ADDOPTS:-}} -p no:cacheprovider"
{test_cmd}
test_status=$?
set -e

if [ "$test_status" -eq 5 ]; then
    echo "ASTON_TESTS_NO_TESTS"
    exit 24
elif [ "$test_status" -ne 0 ]; then
    echo "ASTON_TESTS_FAILED: exit code $test_status"
    exit 22
else
    echo "ASTON_TESTS_PASSED"
fi
'''
            else:
                script += f'''
echo "--- [Verification] Running test suite ---"

{test_cmd} ||
    {{
        echo "ASTON_TESTS_FAILED"
        exit 22
    }}
'''

        script += r'''
echo "ASTON_VERIFICATION_PASSED"
'''

        # 5. Execute according to configured network_mode
        if requested_network == "none":
            modes_to_try = ("none",)
        elif requested_network == "bridge":
            modes_to_try = ("bridge",)
        else:
            modes_to_try = ("none", "bridge")

        for mode in modes_to_try:
            active_network = mode
            logger.info("Running full-repo verification container with network=%s", mode)

            container_id_file = (
                Path(temp_dir)
                / f"aston-container-{uuid.uuid4().hex}.cid"
            )

            docker_cmd = [
                docker,
                "run",
                "--rm",
                "--cidfile",
                str(container_id_file),
                "--network",
                mode,
                "-v",
                f"{verification_repo}:/app:rw,Z",
                "-v",
                "aston-pip-cache:/root/.cache/pip",
                "--workdir",
                "/app",
                python_image,
                "sh",
                "-c",
                script,
            ]

            try:
                returncode, stdout, stderr = _run_docker_with_progress(
                    docker_cmd=docker_cmd,
                    timeout=timeout,
                    description=(
                        f"Verifying repository in Docker ({mode})"
                    ),
                )

            except subprocess.TimeoutExpired as exc:
                timeout_stdout = exc.stdout or ""
                timeout_stderr = exc.stderr or ""

                if isinstance(timeout_stdout, bytes):
                    timeout_stdout = timeout_stdout.decode(
                        "utf-8",
                        errors="replace",
                    )

                if isinstance(timeout_stderr, bytes):
                    timeout_stderr = timeout_stderr.decode(
                        "utf-8",
                        errors="replace",
                    )

                _remove_container(
                    docker,
                    container_id_file,
                )

                attempts.append({
                    "network_mode": mode,
                    "exit_code": None,
                    "timed_out": True,
                    "stdout": timeout_stdout,
                    "stderr": timeout_stderr,
                })

                _print_failure_summary(
                    "TIMEOUT",
                    None,
                    timeout_stdout,
                    timeout_stderr,
                )

                return {
                    "status": "TIMEOUT",
                    "success": False,
                    "exit_code": None,
                    "stdout": timeout_stdout,
                    "stderr": timeout_stderr,
                    "network_mode": mode,
                    "attempts": attempts,
                    "duration_seconds": time.monotonic() - started,
                }

            finally:
                _remove_container(
                    docker,
                    container_id_file,
                )

            output = stdout + "\n" + stderr

            attempts.append({
                "network_mode": mode,
                "exit_code": returncode,
                "timed_out": False,
                "stdout": stdout,
                "stderr": stderr,
            })

            # Stop immediately on success.
            if (
                returncode == 0
                and "ASTON_VERIFICATION_PASSED" in output
            ):
                break

            # Only retry offline failures that look network-related when mode="auto".
            if (
                requested_network == "auto"
                and mode == "none"
                and _is_network_failure(output)
            ):
                logger.info("Offline full-repo verification hit network error; retrying in bridge mode")
                continue

            # Don't retry syntax, migration, or test failures.
            break

        # 6. Classify final result
        output = stdout + "\n" + stderr
        elapsed = time.monotonic() - started

        status = _classify_verification_output(
            output,
            returncode,
        )

        if status != "PASS":
            _print_failure_summary(
                status,
                returncode,
                stdout,
                stderr,
            )
        else:
            logger.info(
                "Full-repo verification PASS in %.1fs (network=%s)",
                elapsed,
                active_network,
            )

        return {
            "status": status,
            "success": status == "PASS",
            "exit_code": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "network_mode": active_network,
            "attempts": attempts,
            "duration_seconds": elapsed,
        }

    except OSError as exc:
        _print_failure_summary(
            "DOCKER_ERROR",
            None,
            "",
            str(exc),
        )

        return {
            "status": "DOCKER_ERROR",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "network_mode": active_network,
            "attempts": attempts,
            "duration_seconds": time.monotonic() - started,
        }

    except Exception as exc:
        _print_failure_summary(
            "VERIFICATION_ERROR",
            None,
            "",
            str(exc),
        )

        return {
            "status": "VERIFICATION_ERROR",
            "success": False,
            "exit_code": None,
            "stdout": stdout,
            "stderr": str(exc),
            "network_mode": active_network,
            "attempts": attempts,
            "duration_seconds": time.monotonic() - started,
        }

    finally:
        if preserve_context:
            logger.debug("Full-repo verification repository retained at %s", verification_repo)
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)