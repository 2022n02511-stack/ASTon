
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


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

    @property
    def stderr(self) -> str:
        return self.raw_stderr

    @property
    def stdout(self) -> str:
        return self.raw_stdout


def _ignore_repo_files(directory: str, names: list[str]) -> set[str]:
    """Exclude large or machine-specific directories from the Docker context."""
    excluded = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
    return {name for name in names if name in excluded}


def _classify_runtime_failure(stderr: str) -> tuple[VerificationResult, str]:
    lower = stderr.lower()

    external_markers = (
        "connectionerror",
        "connecttimeout",
        "readtimeout",
        "name resolution",
        "failed to establish a new connection",
        "api connection error",
        "service unavailable",
        "rate limit",
    )
    if any(marker in lower for marker in external_markers):
        return VerificationResult.EXTERNAL_SERVICE_ERROR, "ExternalServiceError"

    if "modulenotfounderror" in lower or "importerror" in lower:
        return VerificationResult.MIGRATION_ERROR, "ImportError"

    if "syntaxerror" in lower:
        return VerificationResult.MIGRATION_ERROR, "SyntaxError"

    if "attributeerror" in lower:
        return VerificationResult.MIGRATION_ERROR, "AttributeError"

    if "typeerror" in lower:
        return VerificationResult.MIGRATION_ERROR, "TypeError"

    if "assertionerror" in lower:
        return VerificationResult.MIGRATION_ERROR, "AssertionError"

    return VerificationResult.MIGRATION_ERROR, "RuntimeError"


def verify_single_file_in_docker(
    target_file: str | Path = "before.py",
    req_file: str | Path = "temp_requirements.txt",
    repo_root: str | Path | None = None,
    timeout: int = 300,
    build_timeout: int = 600,
    python_image: str = "python:3.11-slim",
) -> VerificationOutput:
    """
    Verify one migrated Python file inside Docker.

    The full repository is copied into an isolated temporary build context so
    imports between local Python files can resolve.

    Existing usage remains valid:
        verify_single_file_in_docker(path, temp_req_path)

    Recommended for multi-file migrations:
        verify_single_file_in_docker(
            path,
            temp_req_path,
            repo_root=root_path,
        )

    This checks execution of the target file. It does not replace full-repo
    tests or guarantee that all application behavior is correct.
    """
    started = time.monotonic()

    target_input = Path(target_file).expanduser()
    requirements_input = Path(req_file).expanduser()

    if repo_root is not None:
        root = Path(repo_root).expanduser().resolve()
    elif not target_input.is_absolute():
        root = Path.cwd().resolve()
    else:
        # For nested absolute paths, pass repo_root explicitly so sibling
        # modules and package imports remain available.
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
                f"Target file {target_abs} is outside repository root {root}. "
                "Pass the correct repo_root."
            ),
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
            error_message="Timed out checking whether the Docker daemon is available.",
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
        )

    # Unique temporary context prevents concurrent verifier runs from
    # overwriting each other's Dockerfiles or runner scripts.
    with tempfile.TemporaryDirectory(prefix="aston_verify_") as temp_dir:
        context = Path(temp_dir)
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
            )

        copied_target = repo_copy / target_rel
        if not copied_target.is_file():
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="TargetCopyError",
                error_message=f"Target file was not copied into Docker context: {target_rel}",
            )

        # Use the supplied target-version requirements, not the repository's
        # original requirements file.
        shutil.copy2(requirements_input, context / "requirements.txt")

        runner = r'''
import runpy
import sys
import traceback
from pathlib import Path

target = Path(sys.argv[1]).resolve()
repo_root = Path("/app/repo").resolve()

# Make repository-root imports and local package imports available.
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(target.parent))

try:
    runpy.run_path(str(target), run_name="__main__")
except SystemExit as exc:
    # Treat sys.exit(0) as success; non-zero exits as failure.
    code = exc.code
    if code not in (None, 0):
        traceback.print_exc()
        raise
except BaseException:
    traceback.print_exc()
    raise
'''
        (context / "run_target.py").write_text(runner, encoding="utf-8")

        dockerfile = f"""
FROM {python_image}
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV OPENAI_API_KEY=sk-test-aston-verification

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --default-timeout=120 --retries 3 -r /app/requirements.txt

COPY repo/ /app/repo/
COPY run_target.py /app/run_target.py

CMD ["python", "/app/run_target.py", "/app/repo/{target_rel.as_posix()}"]
"""
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        # Unique image tag prevents concurrent invocations from sharing a tag.
        import uuid
        image_tag = f"aston-verify-{uuid.uuid4().hex[:12]}"

        try:
            build = subprocess.run(
                [
                    docker,
                    "build",
                    "--tag",
                    image_tag,
                    "--file",
                    str(context / "Dockerfile"),
                    str(context),
                ],
                capture_output=True,
                text=True,
                timeout=build_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return VerificationOutput(
                status=VerificationResult.TIMEOUT,
                target_file=str(target_abs),
                error_type="DockerBuildTimeout",
                error_message=f"Docker image build exceeded {build_timeout} seconds.",
                raw_stdout=exc.stdout or "",
                raw_stderr=exc.stderr or "",
            )

        if build.returncode != 0:
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=str(target_abs),
                error_type="DockerBuildError",
                error_message=(
                    build.stderr.strip()
                    or build.stdout.strip()
                    or "Docker image build failed."
                ),
                raw_stderr=build.stderr,
                raw_stdout=build.stdout,
            )

        try:
            run = subprocess.run(
                [
                    docker,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    image_tag,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return VerificationOutput(
                status=VerificationResult.TIMEOUT,
                target_file=str(target_abs),
                error_type="ExecutionTimeout",
                error_message=f"Target execution exceeded {timeout} seconds.",
                raw_stdout=exc.stdout or "",
                raw_stderr=exc.stderr or "",
            )
        finally:
            # Clean up the unique image even when execution fails.
            subprocess.run(
                [docker, "image", "rm", "-f", image_tag],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        elapsed = time.monotonic() - started

        if run.returncode == 0:
            return VerificationOutput(
                status=VerificationResult.PASS,
                target_file=str(target_abs),
                raw_stdout=(
                    f"Target executed successfully in {elapsed:.1f}s.\n"
                    + run.stdout
                ),
                raw_stderr=run.stderr,
            )

        status, error_type = _classify_runtime_failure(run.stderr)
        return VerificationOutput(
            status=status,
            target_file=str(target_abs),
            error_type=error_type,
            error_message=(
                run.stderr.strip()
                or run.stdout.strip()
                or f"Target exited with code {run.returncode}."
            ),
            raw_stderr=run.stderr,
            raw_stdout=run.stdout,
        )
