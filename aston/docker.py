from __future__ import annotations

import ast
import logging
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tokenize
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from aston.cli_ui.rich import console

logger = logging.getLogger(__name__)


class VerificationResult(str, Enum):
    PASS = "PASS"
    MIGRATION_ERROR = "MIGRATION_ERROR"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"
    TIMEOUT = "TIMEOUT"


@dataclass
class VerificationOutput:
    status: VerificationResult
    target_file: str
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    raw_stderr: str = ""
    raw_stdout: str = ""
    network_mode: Optional[str] = None
    attempts: list[dict] = field(default_factory=list)
    duration_seconds: Optional[float] = None


def _validate_python_syntax(
    target_file: Path,
) -> Optional[VerificationOutput]:
    """Validate Python syntax before starting Docker."""

    try:
        with tokenize.open(str(target_file)) as source_file:
            source = source_file.read()

        ast.parse(source, filename=str(target_file))

    except SyntaxError as exc:
        logger.warning(
            "SyntaxError in candidate %s at line %s col %s: %s",
            target_file,
            exc.lineno,
            exc.offset,
            exc.msg,
        )
        return VerificationOutput(
            status=VerificationResult.MIGRATION_ERROR,
            target_file=str(target_file),
            error_type="SyntaxError",
            error_message=(
                f"{exc.msg} at line {exc.lineno}, "
                f"column {exc.offset}"
            ),
            raw_stderr=str(exc),
        )

    except (OSError, UnicodeError) as exc:
        logger.error("Failed reading source %s for syntax check: %s", target_file, exc)
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_file),
            error_type="SourceReadError",
            error_message=str(exc),
        )

    return None


def _run_streaming_build(
    command: list[str],
    timeout: int,
) -> tuple[int, str, str, bool]:
    """Run Docker build with Rich progress and a hard timeout."""

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )

    events: queue.Queue = queue.Queue()
    outputs = {"stdout": [], "stderr": []}

    def read_stream(name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                events.put((name, line))
        finally:
            stream.close()
            events.put((name, None))

    threads = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr),
            daemon=True,
        ),
    ]

    for thread in threads:
        thread.start()

    start_time = time.monotonic()
    total_timeout = max(1, timeout)
    deadline = start_time + total_timeout

    finished = 0
    timed_out = False

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn(
            "[bold cyan]Building Docker verification image[/bold cyan]"
        ),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:

        task_id = progress.add_task(
            "build",
            total=total_timeout,
        )

        while finished < 2:
            remaining = deadline - time.monotonic()

            if (
                remaining <= 0
                and process.poll() is None
                and not timed_out
            ):
                timed_out = True
                logger.warning("Docker build timed out after %ds; terminating process", total_timeout)

                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()

                    process.wait(timeout=5)

                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass

                    process.wait()

            try:
                name, line = events.get(
                    timeout=0.1 if timed_out else max(0.01, remaining)
                )

            except queue.Empty:
                progress.update(
                    task_id,
                    completed=min(
                        total_timeout,
                        time.monotonic() - start_time,
                    ),
                )
                continue

            if line is None:
                finished += 1
            else:
                outputs[name].append(line)

                progress.update(
                    task_id,
                    completed=min(
                        total_timeout,
                        time.monotonic() - start_time,
                    ),
                )

    for thread in threads:
        thread.join()

    returncode = process.wait()
    logger.debug("Docker build finished with returncode=%d timed_out=%s", returncode, timed_out)

    return (
        returncode,
        "".join(outputs["stdout"]),
        "".join(outputs["stderr"]),
        timed_out,
    )


def _ignore_repo_files(
    directory: str,
    names: list[str],
) -> set[str]:
    """Exclude large or machine-specific directories from Docker context."""

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
        ".idea",
        ".vscode",
        "node_modules",
        ".aston",
    }

    return {
        name for name in names
        if name in excluded
    }


def _is_network_connectivity_failure(output: str) -> bool:
    """Detect likely DNS or network-connectivity failures."""

    lower = output.lower()

    markers = (
        "temporary failure in name resolution",
        "could not resolve host",
        "name or service not known",
        "name resolution failed",
        "name resolution error",
        "getaddrinfo failed",
        "nodename nor servname provided",
        "no address associated with hostname",
        "network is unreachable",
        "network is down",
        "no route to host",
        "connection refused",
        "failed to establish a new connection",
        "socket.gaierror",
        "newconnectionerror",
    )

    return any(marker in lower for marker in markers)


def _classify_runtime_failure(
    output: str,
) -> tuple[VerificationResult, str]:
    """Classify a failed target execution."""

    lower = output.lower()

    if _is_network_connectivity_failure(output):
        return (
            VerificationResult.EXTERNAL_SERVICE_ERROR,
            "NetworkConnectivityError",
        )

    if any(
        marker in lower
        for marker in (
            "modulenotfounderror",
            "no module named",
            "importerror",
        )
    ):
        return (
            VerificationResult.MIGRATION_ERROR,
            "ImportError",
        )

    for marker, name in (
        ("syntaxerror", "SyntaxError"),
        ("attributeerror", "AttributeError"),
        ("typeerror", "TypeError"),
        ("assertionerror", "AssertionError"),
        ("keyerror", "KeyError"),
        ("valueerror", "ValueError"),
        ("nameerror", "NameError"),
    ):
        if marker in lower:
            return (
                VerificationResult.MIGRATION_ERROR,
                name,
            )

    return (
        VerificationResult.MIGRATION_ERROR,
        "RuntimeError",
    )


def _ensure_base_image(
    docker: str,
    python_image: str,
    timeout: int,
) -> tuple[bool, str]:
    """
    Ensure the base image is available locally before offline builds.

    This prevents an offline Docker build from failing merely because
    the configured base image has not yet been pulled.
    """

    try:
        inspect = subprocess.run(
            [
                docker,
                "image",
                "inspect",
                python_image,
            ],
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
            [
                docker,
                "pull",
                python_image,
            ],
            capture_output=True,
            text=True,
            timeout=max(1, timeout),
            check=False,
        )

        if pull.returncode != 0:
            err_msg = (
                pull.stderr.strip()
                or pull.stdout.strip()
                or "Could not pull the Docker base image."
            )
            logger.error("Failed pulling Docker base image %s: %s", python_image, err_msg)
            return False, err_msg

        return True, pull.stdout

    except subprocess.TimeoutExpired as exc:
        logger.error("Base image pull exceeded %ds for %s: %s", timeout, python_image, exc)
        return (
            False,
            f"Base image pull exceeded {timeout} seconds: {exc}",
        )

    except OSError as exc:
        logger.error("OS error checking/pulling base image %s: %s", python_image, exc)
        return False, str(exc)


def _run_container(
    docker: str,
    image_tag: str,
    network_mode: str,
    timeout: int,
) -> tuple[
    Optional[subprocess.CompletedProcess],
    Optional[subprocess.TimeoutExpired],
]:
    """Execute the verification container."""

    command = [
        docker,
        "run",
        "--rm",
        "--network",
        network_mode,
        image_tag,
    ]
    logger.debug("Running container %s with network=%s timeout=%ds", image_tag, network_mode, timeout)

    try:
        return (
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(1, timeout),
                check=False,
            ),
            None,
        )

    except subprocess.TimeoutExpired as exc:
        logger.warning("Container %s timed out after %ds in network=%s", image_tag, timeout, network_mode)
        return None, exc


def _remove_image(
    docker: str,
    image_tag: str,
) -> None:
    """Remove the temporary verification image."""

    try:
        subprocess.run(
            [
                docker,
                "image",
                "rm",
                "-f",
                image_tag,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        logger.debug("Removed temporary Docker image %s", image_tag)

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed removing temporary Docker image %s: %s", image_tag, exc)


def verify_single_file_in_docker(
    target_file: str | Path = "before.py",
    req_file: str | Path = "temp_requirements.txt",
    repo_root: str | Path | None = None,
    timeout: int = 300,
    build_timeout: int = 600,
    python_image: str = "python:3.11-slim",
    network_mode: str = "auto",
    preserve_context: bool = False,
) -> VerificationOutput:
    """
    Verify one migrated Python file inside Docker.

    Static:
        Validate Python syntax before Docker execution.

    Build:
        Ensure base image exists locally.
        Try offline build first.
        Retry with bridge only on recognizable network failures.

    Runtime:
        network_mode="auto": try offline first; retry with bridge only on
            recognizable network failures.
        network_mode="none": execute offline only (no bridge retry).
        network_mode="bridge": execute with bridge networking directly.

    PASS means the target file executed successfully.
    It does not prove full-repository correctness.
    """

    started = time.monotonic()
    requested_network = (
        network_mode if network_mode in {"auto", "none", "bridge"} else "auto"
    )

    target_input = Path(target_file).expanduser()
    requirements_input = Path(req_file).expanduser()

    if repo_root is not None:
        root = Path(repo_root).expanduser().resolve()

    elif not target_input.is_absolute():
        root = Path.cwd().resolve()

    else:
        root = target_input.resolve().parent

    target_abs = (
        target_input.resolve()
        if target_input.is_absolute()
        else (root / target_input).resolve()
    )

    try:
        target_rel = target_abs.relative_to(root)

    except ValueError:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="PathError",
            error_message=(
                f"Target file {target_abs} is outside repository root "
                f"{root}. Pass the correct repo_root."
            ),
            duration_seconds=time.monotonic() - started,
        )

    if not root.is_dir():
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="RepositoryNotFound",
            error_message=f"Repository root does not exist: {root}",
            duration_seconds=time.monotonic() - started,
        )

    if not target_abs.is_file():
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="TargetFileNotFound",
            error_message=f"Target file does not exist: {target_abs}",
            duration_seconds=time.monotonic() - started,
        )

    syntax_result = _validate_python_syntax(target_abs)

    if syntax_result is not None:
        syntax_result.duration_seconds = time.monotonic() - started
        return syntax_result

    if not requirements_input.is_absolute():
        requirements_input = (
            Path.cwd() / requirements_input
        ).resolve()
    else:
        requirements_input = requirements_input.resolve()

    if not requirements_input.is_file():
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="RequirementsNotFound",
            error_message=(
                f"Requirements file does not exist: {requirements_input}"
            ),
            duration_seconds=time.monotonic() - started,
        )

    docker = shutil.which("docker")

    if docker is None:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerNotFound",
            error_message="Docker CLI was not found on PATH.",
            duration_seconds=time.monotonic() - started,
        )

    try:
        daemon_check = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    except subprocess.TimeoutExpired:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerUnavailable",
            error_message=(
                "Timed out checking whether the Docker daemon is available."
            ),
            duration_seconds=time.monotonic() - started,
        )

    except OSError as exc:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerUnavailable",
            error_message=str(exc),
            duration_seconds=time.monotonic() - started,
        )

    if daemon_check.returncode != 0:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerUnavailable",
            error_message=(
                daemon_check.stderr.strip()
                or daemon_check.stdout.strip()
                or "Docker daemon is unavailable."
            ),
            raw_stderr=daemon_check.stderr,
            raw_stdout=daemon_check.stdout,
            duration_seconds=time.monotonic() - started,
        )

    image_ok, image_error = _ensure_base_image(
        docker,
        python_image,
        build_timeout,
    )

    if not image_ok:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="BaseImageUnavailable",
            error_message=image_error,
            duration_seconds=time.monotonic() - started,
        )

    context = Path(
        tempfile.mkdtemp(prefix="aston_verify_")
    )

    image_tag = f"aston-verify-{uuid.uuid4().hex[:12]}"

    image_created = False
    attempts: list[dict] = []

    build_stdout = ""
    build_stderr = ""
    build_network = "none"

    logger.debug(
        "Starting single-file verification for %s (image=%s, network_mode=%s, context=%s)",
        target_abs,
        python_image,
        requested_network,
        context,
    )

    try:
        repo_copy = context / "repo"
        repo_copy.mkdir()

        try:
            shutil.copytree(
                root,
                repo_copy,
                dirs_exist_ok=True,
                ignore=_ignore_repo_files,
            )

        except OSError as exc:
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="RepositoryCopyError",
                error_message=str(exc),
                duration_seconds=time.monotonic() - started,
            )

        copied_target = repo_copy / target_rel

        # Candidate files live under .aston/runs/<run_id>/..., which _ignore_repo_files
        # excludes during the full-repo copy. Explicitly copy target_abs into repo_copy.
        if not copied_target.is_file() and target_abs.is_file():
            try:
                copied_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target_abs, copied_target)
            except OSError as exc:
                return VerificationOutput(
                    status=VerificationResult.ENVIRONMENT_ERROR,
                    target_file=str(target_abs),
                    error_type="TargetCopyError",
                    error_message=(
                        f"Failed copying target file {target_abs} into Docker context: {exc}"
                    ),
                    duration_seconds=time.monotonic() - started,
                )

        if not copied_target.is_file():
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="TargetCopyError",
                error_message=(
                    f"Target file was not copied into Docker context: "
                    f"{target_rel}"
                ),
                duration_seconds=time.monotonic() - started,
            )

        shutil.copy2(
            requirements_input,
            context / "requirements.txt",
        )

        runner = r'''
import runpy
import sys
import traceback
from pathlib import Path

target = Path(sys.argv[1]).resolve()
repo_root = Path("/app/repo").resolve()

sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(target.parent))

try:
    runpy.run_path(str(target), run_name="__main__")

except SystemExit as exc:
    if exc.code not in (None, 0):
        traceback.print_exc()
        raise

except BaseException:
    traceback.print_exc()
    raise
'''

        (context / "run_target.py").write_text(
            runner,
            encoding="utf-8",
        )

        dockerfile = f'''
# syntax=docker/dockerfile:1.7

FROM {python_image}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV OPENAI_API_KEY=sk-test-aston-verification

COPY requirements.txt /app/requirements.txt

RUN --mount=type=cache,id=aston-pip-cache,target=/root/.cache/pip \\
    python -m pip install \\
    --cache-dir=/root/.cache/pip \\
    --default-timeout=120 \\
    --retries 3 \\
    -r /app/requirements.txt

COPY repo/ /app/repo/
COPY run_target.py /app/run_target.py

CMD ["python", "/app/run_target.py", "/app/repo/{target_rel.as_posix()}"]
'''

        (context / "Dockerfile").write_text(
            dockerfile,
            encoding="utf-8",
        )

        # BUILD: OFFLINE FIRST
        build_command = [
            docker,
            "build",
            "--network=none",
            "--progress=plain",
            "--tag",
            image_tag,
            "--file",
            str(context / "Dockerfile"),
            str(context),
        ]

        try:
            (
                rc,
                build_stdout,
                build_stderr,
                build_timeout_hit,
            ) = _run_streaming_build(
                build_command,
                timeout=build_timeout,
            )

        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Docker build failed with OS/subprocess error for %s: %s", target_abs, exc)
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="DockerBuildError",
                error_message=str(exc),
                attempts=attempts,
                duration_seconds=time.monotonic() - started,
            )

        attempts.append({
            "stage": "build",
            "network_mode": "none",
            "returncode": rc,
            "timed_out": build_timeout_hit,
        })

        if build_timeout_hit:
            return VerificationOutput(
                status=VerificationResult.TIMEOUT,
                target_file=str(target_abs),
                error_type="DockerBuildTimeout",
                error_message=(
                    f"Docker image build exceeded {build_timeout} "
                    "seconds in offline mode."
                ),
                raw_stdout=build_stdout,
                raw_stderr=build_stderr,
                network_mode="none",
                attempts=attempts,
                duration_seconds=time.monotonic() - started,
            )

        # BUILD: BRIDGE FALLBACK
        if rc != 0 and _is_network_connectivity_failure(
            build_stdout + "\n" + build_stderr
        ):
            logger.info(
                "Offline Docker build hit a network error for %s; retrying with bridge networking",
                target_abs,
            )

            bridge_command = [
                docker,
                "build",
                "--network=default",
                "--progress=plain",
                "--tag",
                image_tag,
                "--file",
                str(context / "Dockerfile"),
                str(context),
            ]

            try:
                (
                    retry_rc,
                    retry_stdout,
                    retry_stderr,
                    retry_timeout,
                ) = _run_streaming_build(
                    bridge_command,
                    timeout=build_timeout,
                )

            except (OSError, subprocess.SubprocessError) as exc:
                logger.error("Bridge Docker build retry failed for %s: %s", target_abs, exc)
                return VerificationOutput(
                    status=VerificationResult.ENVIRONMENT_ERROR,
                    target_file=str(target_abs),
                    error_type="DockerBuildError",
                    error_message=str(exc),
                    raw_stdout=build_stdout,
                    raw_stderr=build_stderr,
                    attempts=attempts,
                    duration_seconds=time.monotonic() - started,
                )

            build_stdout += (
                "\n\n--- BRIDGE BUILD RETRY ---\n"
                + retry_stdout
            )

            build_stderr += (
                "\n\n--- BRIDGE BUILD RETRY ---\n"
                + retry_stderr
            )

            rc = retry_rc
            build_network = "bridge"

            attempts.append({
                "stage": "build",
                "network_mode": "bridge",
                "returncode": retry_rc,
                "timed_out": retry_timeout,
            })

            if retry_timeout:
                return VerificationOutput(
                    status=VerificationResult.TIMEOUT,
                    target_file=str(target_abs),
                    error_type="DockerBuildTimeout",
                    error_message=(
                        f"Docker image build exceeded {build_timeout} "
                        "seconds during bridge fallback."
                    ),
                    raw_stdout=build_stdout,
                    raw_stderr=build_stderr,
                    network_mode="bridge",
                    attempts=attempts,
                    duration_seconds=time.monotonic() - started,
                )

        # BUILD RESULT
        if rc != 0:
            logger.error("Docker build failed for %s (network=%s, rc=%d)", target_abs, build_network, rc)
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="DockerBuildError",
                error_message=(
                    build_stderr.strip()
                    or build_stdout.strip()
                    or "Docker image build failed."
                ),
                raw_stderr=build_stderr,
                raw_stdout=build_stdout,
                network_mode=build_network,
                attempts=attempts,
                duration_seconds=time.monotonic() - started,
            )

        image_created = True

        # RUNTIME EXECUTION
        initial_runtime_mode = (
            "bridge" if requested_network == "bridge" else "none"
        )

        run, run_timeout = _run_container(
            docker,
            image_tag,
            initial_runtime_mode,
            timeout,
        )

        if run_timeout is not None:
            return VerificationOutput(
                status=VerificationResult.TIMEOUT,
                target_file=str(target_abs),
                error_type="ExecutionTimeout",
                error_message=(
                    f"Target execution exceeded {timeout} "
                    f"seconds in {initial_runtime_mode} mode."
                ),
                raw_stdout=run_timeout.stdout or "",
                raw_stderr=run_timeout.stderr or "",
                network_mode=initial_runtime_mode,
                attempts=attempts + [{
                    "stage": "runtime",
                    "network_mode": initial_runtime_mode,
                    "timed_out": True,
                }],
                duration_seconds=time.monotonic() - started,
            )

        attempts.append({
            "stage": "runtime",
            "network_mode": initial_runtime_mode,
            "returncode": run.returncode,
            "timed_out": False,
        })

        selected_network = initial_runtime_mode

        combined = (
            (run.stdout or "")
            + "\n"
            + (run.stderr or "")
        )

        if (
            requested_network == "auto"
            and initial_runtime_mode == "none"
            and run.returncode != 0
            and _is_network_connectivity_failure(combined)
        ):
            logger.info(
                "Offline container runtime hit a network error for %s; retrying with bridge networking",
                target_abs,
            )

            bridge_run, bridge_timeout = _run_container(
                docker,
                image_tag,
                "bridge",
                timeout,
            )

            if bridge_timeout is not None:
                return VerificationOutput(
                    status=VerificationResult.TIMEOUT,
                    target_file=str(target_abs),
                    error_type="ExecutionTimeout",
                    error_message=(
                        f"Target execution exceeded {timeout} "
                        "seconds during bridge fallback."
                    ),
                    raw_stdout=(
                        (run.stdout or "")
                        + "\n--- BRIDGE RETRY ---\n"
                        + (bridge_timeout.stdout or "")
                    ),
                    raw_stderr=(
                        (run.stderr or "")
                        + "\n--- BRIDGE RETRY ---\n"
                        + (bridge_timeout.stderr or "")
                    ),
                    network_mode="bridge",
                    attempts=attempts + [{
                        "stage": "runtime",
                        "network_mode": "bridge",
                        "timed_out": True,
                    }],
                    duration_seconds=time.monotonic() - started,
                )

            attempts.append({
                "stage": "runtime",
                "network_mode": "bridge",
                "returncode": bridge_run.returncode,
                "timed_out": False,
            })

            run = bridge_run
            selected_network = "bridge"

        # FINAL RESULT
        elapsed = time.monotonic() - started

        if run.returncode == 0:
            logger.info(
                "Single-file verification PASS for %s in %.1fs (network=%s)",
                target_abs,
                elapsed,
                selected_network,
            )
            return VerificationOutput(
                status=VerificationResult.PASS,
                target_file=str(target_abs),
                raw_stdout=(
                    f"Target executed successfully in {elapsed:.1f}s "
                    f"(runtime network: {selected_network}).\n"
                    + (run.stdout or "")
                ),
                raw_stderr=run.stderr or "",
                network_mode=selected_network,
                attempts=attempts,
                duration_seconds=elapsed,
            )

        combined_output = (
            (run.stderr or "")
            + "\n"
            + (run.stdout or "")
        )

        status, error_type = _classify_runtime_failure(
            combined_output
        )
        logger.warning(
            "Single-file verification %s (%s) for %s in %.1fs (rc=%d)",
            status.value,
            error_type,
            target_abs,
            elapsed,
            run.returncode,
        )

        return VerificationOutput(
            status=status,
            target_file=str(target_abs),
            error_type=error_type,
            error_message=(
                (run.stderr or "").strip()
                or (run.stdout or "").strip()
                or f"Target exited with code {run.returncode}."
            ),
            raw_stderr=run.stderr or "",
            raw_stdout=run.stdout or "",
            network_mode=selected_network,
            attempts=attempts,
            duration_seconds=elapsed,
        )

    finally:
        if image_created:
            _remove_image(docker, image_tag)

        if preserve_context:
            logger.debug("Docker context retained at %s", context)
        else:
            shutil.rmtree(context, ignore_errors=True)