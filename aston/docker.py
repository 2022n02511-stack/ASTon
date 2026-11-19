from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from aston.cli_ui.rich import console


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
    # Additive defaults preserve existing positional construction.
    network_mode: Optional[str] = None
    attempts: list[dict] = field(default_factory=list)
    duration_seconds: Optional[float] = None


def _run_streaming_build(command: list[str], timeout: int) -> tuple[int, str, str, bool]:
    '''Run docker build silently with a Rich progress bar and a hard timeout.'''
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
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
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
        TextColumn("[bold cyan]Building Docker verification image[/bold cyan]"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("build", total=total_timeout)

        while finished < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and process.poll() is None and not timed_out:
                timed_out = True
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
                name, line = events.get(timeout=0.1 if timed_out else max(0.01, remaining))
            except queue.Empty:
                progress.update(task_id, completed=min(total_timeout, time.monotonic() - start_time))
                continue

            if line is None:
                finished += 1
            else:
                outputs[name].append(line)
                progress.update(task_id, completed=min(total_timeout, time.monotonic() - start_time))

    for thread in threads:
        thread.join()
    returncode = process.wait()
    return returncode, "".join(outputs["stdout"]), "".join(outputs["stderr"]), timed_out


def _ignore_repo_files(directory: str, names: list[str]) -> set[str]:
    '''Exclude large or machine-specific directories from the Docker context.'''
    excluded = {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".idea",
        ".vscode", "node_modules",
    }
    return {name for name in names if name in excluded}


def _is_network_connectivity_failure(output: str) -> bool:
    '''Detect errors plausibly caused by disabled container networking.'''
    lower = output.lower()
    markers = (
        "temporary failure in name resolution",
        "name or service not known",
        "name resolution",
        "getaddrinfo failed",
        "nodename nor servname provided",
        "no address associated with hostname",
        "failed to establish a new connection",
        "connectionerror",
        "connecttimeout",
        "connection timed out",
        "network is unreachable",
        "no route to host",
        "network unreachable",
        "could not resolve",
        "couldn't resolve",
        "dns lookup",
        "socket.gaierror",
        "newconnectionerror",
        "max retries exceeded",
        "connection refused",
        "failed to connect",
        "unable to connect",
    )
    return any(marker in lower for marker in markers)


def _classify_runtime_failure(stderr: str) -> tuple[VerificationResult, str]:
    lower = stderr.lower()
    if _is_network_connectivity_failure(stderr):
        return VerificationResult.EXTERNAL_SERVICE_ERROR, "NetworkConnectivityError"

    if any(x in lower for x in ("modulenotfounderror", "no module named", "importerror")):
        return VerificationResult.MIGRATION_ERROR, "ImportError"

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
            return VerificationResult.MIGRATION_ERROR, name
    return VerificationResult.MIGRATION_ERROR, "RuntimeError"


def _run_container(
    docker: str, image_tag: str, network_mode: str, timeout: int
) -> tuple[Optional[subprocess.CompletedProcess], Optional[subprocess.TimeoutExpired]]:
    command = [docker, "run", "--rm", "--network", network_mode, image_tag]
    try:
        return subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(1, timeout), check=False,
        ), None
    except subprocess.TimeoutExpired as exc:
        return None, exc


def _remove_image(docker: str, image_tag: str) -> None:
    try:
        subprocess.run(
            [docker, "image", "rm", "-f", image_tag],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def verify_single_file_in_docker(
    target_file: str | Path = "before.py",
    req_file: str | Path = "temp_requirements.txt",
    repo_root: str | Path | None = None,
    timeout: int = 300,
    build_timeout: int = 600,
    python_image: str = "python:3.11-slim",
) -> VerificationOutput:
    '''
    Verify one migrated Python file inside Docker.

    Build: try --network=none first; retry with bridge only for a recognizable
    build-time connectivity failure.
    Runtime: try --network=none first; retry with bridge only for a recognizable
    runtime connectivity failure.

    A network-enabled PASS is not an offline PASS. This executes the target
    file; it does not replace full-repository tests or prove every code path.
    '''
    started = time.monotonic()
    target_input = Path(target_file).expanduser()
    requirements_input = Path(req_file).expanduser()

    if repo_root is not None:
        root = Path(repo_root).expanduser().resolve()
    elif not target_input.is_absolute():
        root = Path.cwd().resolve()
    else:
        # For nested absolute paths, pass repo_root explicitly.
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
            error_message=f"Target file {target_abs} is outside repository root {root}. Pass the correct repo_root.",
        )

    if not root.is_dir():
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="RepositoryNotFound",
            error_message=f"Repository root does not exist: {root}",
        )
    if not target_abs.is_file():
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="TargetFileNotFound",
            error_message=f"Target file does not exist: {target_abs}",
        )

    if not requirements_input.is_absolute():
        requirements_input = (Path.cwd() / requirements_input).resolve()
    else:
        requirements_input = requirements_input.resolve()
    if not requirements_input.is_file():
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="RequirementsNotFound",
            error_message=f"Requirements file does not exist: {requirements_input}",
        )

    docker = shutil.which("docker")
    if docker is None:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerNotFound",
            error_message="Docker CLI was not found on PATH.",
        )

    try:
        daemon_check = subprocess.run(
            [docker, "info"], capture_output=True, text=True,
            timeout=20, check=False,
        )
    except subprocess.TimeoutExpired:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerUnavailable",
            error_message="Timed out checking whether the Docker daemon is available.",
        )
    except OSError as exc:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerUnavailable",
            error_message=str(exc),
        )

    if daemon_check.returncode != 0:
        return VerificationOutput(
            status=VerificationResult.ENVIRONMENT_ERROR,
            target_file=str(target_abs),
            error_type="DockerUnavailable",
            error_message=daemon_check.stderr.strip() or daemon_check.stdout.strip() or "Docker daemon is unavailable.",
            raw_stderr=daemon_check.stderr,
            raw_stdout=daemon_check.stdout,
        )

    context = Path(tempfile.mkdtemp(prefix="aston_verify_"))
    image_tag = f"aston-verify-{uuid.uuid4().hex[:12]}"
    image_created = False
    attempts: list[dict] = []
    build_stdout = ""
    build_stderr = ""
    print(f"\\n[ASTON DEBUG] Preserved Docker context: {context}")

    try:
        repo_copy = context / "repo"
        repo_copy.mkdir()
        try:
            shutil.copytree(
                root, repo_copy, dirs_exist_ok=True, ignore=_ignore_repo_files
            )
        except OSError as exc:
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="RepositoryCopyError",
                error_message=str(exc),
            )

        copied_target = repo_copy / target_rel
        if not copied_target.is_file():
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="TargetCopyError",
                error_message=f"Target file was not copied into Docker context: {target_rel}",
            )

        shutil.copy2(requirements_input, context / "requirements.txt")

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
        (context / "run_target.py").write_text(runner, encoding="utf-8")

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
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        # Build offline first; only a clear connectivity failure triggers retry.
        # Build with default network access so pip install can reach PyPI
        build_command = [
            docker, "build", "--progress=plain",
            "--tag", image_tag, "--file", str(context / "Dockerfile"),
            str(context),
        ]
        try:
            rc, build_stdout, build_stderr, build_timeout_hit = _run_streaming_build(
                build_command, timeout=build_timeout
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="DockerBuildError",
                error_message=str(exc),
                attempts=attempts,
            )

        attempts.append({
            "stage": "build", "network_mode": "default",
            "returncode": rc, "timed_out": build_timeout_hit,
        })

        if build_timeout_hit:
            return VerificationOutput(
                status=VerificationResult.TIMEOUT,
                target_file=str(target_abs),
                error_type="DockerBuildTimeout",
                error_message=f"Docker image build exceeded {build_timeout} seconds.",
                raw_stdout=build_stdout, raw_stderr=build_stderr,
                network_mode="none", attempts=attempts,
                duration_seconds=time.monotonic() - started,
            )

        if rc != 0 and _is_network_connectivity_failure(build_stdout + "\\n" + build_stderr):
            print("\\n[ASTON] Offline build hit a network error; retrying with bridge networking.")
            build_command[2] = "--network=bridge"
            try:
                retry_rc, retry_stdout, retry_stderr, retry_timeout = _run_streaming_build(
                    build_command, timeout=build_timeout
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return VerificationOutput(
                    status=VerificationResult.ENVIRONMENT_ERROR,
                    target_file=str(target_abs),
                    error_type="DockerBuildError",
                    error_message=str(exc),
                    raw_stdout=build_stdout, raw_stderr=build_stderr,
                    attempts=attempts,
                )

            build_stdout += "\\n\\n--- BRIDGE BUILD RETRY ---\\n" + retry_stdout
            build_stderr += "\\n\\n--- BRIDGE BUILD RETRY ---\\n" + retry_stderr
            rc = retry_rc
            attempts.append({
                "stage": "build", "network_mode": "bridge",
                "returncode": retry_rc, "timed_out": retry_timeout,
            })
            if retry_timeout:
                return VerificationOutput(
                    status=VerificationResult.TIMEOUT,
                    target_file=str(target_abs),
                    error_type="DockerBuildTimeout",
                    error_message=f"Docker image build exceeded {build_timeout} seconds during bridge fallback.",
                    raw_stdout=build_stdout, raw_stderr=build_stderr,
                    network_mode="bridge", attempts=attempts,
                    duration_seconds=time.monotonic() - started,
                )

        print(f"\\n[ASTON DEBUG] Docker build return code: {rc}")
        print(f"[ASTON DEBUG] Preserved Docker context: {context}")

        if rc != 0:
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="DockerBuildError",
                error_message=build_stderr.strip() or build_stdout.strip() or "Docker image build failed.",
                raw_stderr=build_stderr, raw_stdout=build_stdout,
                attempts=attempts,
                duration_seconds=time.monotonic() - started,
            )

        image_created = True

        # Runtime offline attempt.
        run, run_timeout = _run_container(docker, image_tag, "none", timeout)
        if run_timeout is not None:
            return VerificationOutput(
                status=VerificationResult.TIMEOUT,
                target_file=str(target_abs),
                error_type="ExecutionTimeout",
                error_message=f"Target execution exceeded {timeout} seconds in offline mode.",
                raw_stdout=run_timeout.stdout or "",
                raw_stderr=run_timeout.stderr or "",
                network_mode="none",
                attempts=attempts + [{
                    "stage": "runtime", "network_mode": "none", "timed_out": True,
                }],
                duration_seconds=time.monotonic() - started,
            )

        attempts.append({
            "stage": "runtime", "network_mode": "none",
            "returncode": run.returncode, "timed_out": False,
        })
        selected_network = "none"

        # Retry only if output shows a likely connectivity failure. A generic
        # application timeout, ImportError, syntax error, or assertion won't retry.
        combined = (run.stdout or "") + "\\n" + (run.stderr or "")
        if run.returncode != 0 and _is_network_connectivity_failure(combined):
            print("\\n[ASTON] Offline runtime hit a network error; retrying with bridge networking.")
            bridge_run, bridge_timeout = _run_container(
                docker, image_tag, "bridge", timeout
            )
            if bridge_timeout is not None:
                return VerificationOutput(
                    status=VerificationResult.TIMEOUT,
                    target_file=str(target_abs),
                    error_type="ExecutionTimeout",
                    error_message=f"Target execution exceeded {timeout} seconds in bridge fallback.",
                    raw_stdout=(run.stdout or "") + "\\n--- BRIDGE RETRY ---\\n" + (bridge_timeout.stdout or ""),
                    raw_stderr=(run.stderr or "") + "\\n--- BRIDGE RETRY ---\\n" + (bridge_timeout.stderr or ""),
                    network_mode="bridge",
                    attempts=attempts + [{
                        "stage": "runtime", "network_mode": "bridge", "timed_out": True,
                    }],
                    duration_seconds=time.monotonic() - started,
                )
            attempts.append({
                "stage": "runtime", "network_mode": "bridge",
                "returncode": bridge_run.returncode, "timed_out": False,
            })
            run = bridge_run
            selected_network = "bridge"

        elapsed = time.monotonic() - started
        if run.returncode == 0:
            return VerificationOutput(
                status=VerificationResult.PASS,
                target_file=str(target_abs),
                raw_stdout=(
                    f"Target executed successfully in {elapsed:.1f}s "
                    f"(runtime network: {selected_network}).\\n"
                    + (run.stdout or "")
                ),
                raw_stderr=run.stderr or "",
                network_mode=selected_network,
                attempts=attempts,
                duration_seconds=elapsed,
            )

        status, error_type = _classify_runtime_failure(
            (run.stderr or "") + "\\n" + (run.stdout or "")
        )
        return VerificationOutput(
            status=status,
            target_file=str(target_abs),
            error_type=error_type,
            error_message=(run.stderr or "").strip() or (run.stdout or "").strip() or f"Target exited with code {run.returncode}.",
            raw_stderr=run.stderr or "",
            raw_stdout=run.stdout or "",
            network_mode=selected_network,
            attempts=attempts,
            duration_seconds=elapsed,
        )

    finally:
        if image_created:
            _remove_image(docker, image_tag)
        # Preserve context as the existing implementation does for debugging.
        print(f"\\n[ASTON DEBUG] Docker context retained at: {context}")
