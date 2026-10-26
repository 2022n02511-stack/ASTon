
import subprocess
from pathlib import Path
import shlex


def verify_full_repo_in_docker(
    repo_root: str,
    req_file: str | Path,
    *,
    python_image: str = "python:3.11-slim",
    timeout: int = 300,
    test_command: str = "python -m pytest -q",
) -> dict:
    """Verify the migrated repository inside an isolated Docker container."""

    repo_path = Path(repo_root).resolve()
    requirements_path = Path(req_file).resolve()

    if not repo_path.is_dir():
        return {
            "status": "INVALID_REPO",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Repository directory not found: {repo_path}",
        }

    if not requirements_path.is_file():
        return {
            "status": "INVALID_REQUIREMENTS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Requirements file not found: {requirements_path}",
        }

    # The requirements file must be inside the mounted repository.
    try:
        relative_req = requirements_path.relative_to(repo_path)
    except ValueError:
        return {
            "status": "INVALID_REQUIREMENTS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "Requirements file must be inside the repository.",
        }

    container_req = f"/app/{relative_req.as_posix()}"

    script = f"""
export XDG_CACHE_HOME=/tmp/cache
set -eu

python -m pip install --no-cache-dir -r {shlex.quote(container_req)} ||
    {{ echo ASTON_SETUP_FAILED; exit 20; }}

python -m compileall -q /app ||
    {{ echo ASTON_SYNTAX_FAILED; exit 21; }}

{test_command} ||
    {{ echo ASTON_TESTS_FAILED; exit 22; }}

echo ASTON_VERIFICATION_PASSED
"""

    docker_cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--mount", f"type=bind,source={repo_path},target=/app,readonly",
        "--workdir", "/app",
        python_image,
        "sh", "-c", script,
    ]

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        output = result.stdout + "\n" + result.stderr

        if result.returncode == 0 and "ASTON_VERIFICATION_PASSED" in output:
            status = "PASS"
        elif "ASTON_SETUP_FAILED" in output:
            status = "SETUP_ERROR"
        elif "ASTON_SYNTAX_FAILED" in output:
            status = "SYNTAX_ERROR"
        elif "ASTON_TESTS_FAILED" in output:
            status = "TEST_FAILED"
        else:
            status = "DOCKER_ERROR"

        return {
            "status": status,
            "success": status == "PASS",
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "success": False,
            "exit_code": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "Repository verification timed out.",
        }

    except OSError as exc:
        return {
            "status": "DOCKER_ERROR",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
        }
