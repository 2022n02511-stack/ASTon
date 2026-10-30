
import subprocess
from pathlib import Path
import shlex
import shutil
import tempfile


def verify_full_repo_in_docker(
    repo_root: str,
    migrated_files_path: str | Path,
    *,
    python_image: str = "python:3.11-slim",
    timeout: int = 300,
    test_command: str = "python -m pytest -q",
) -> dict:
    """Verify the migrated repository inside an isolated Docker container."""

    repo_path = Path(repo_root).resolve()
    migration_path = Path(migrated_files_path).resolve()
    requirements_path = migration_path / "requirements.txt"

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
            "stderr": f"Migration directory not found: {migration_path}",
        }

    if not requirements_path.is_file():
        return {
            "status": "INVALID_REQUIREMENTS",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"Requirements file not found: {requirements_path}",
        }

    try:
        with tempfile.TemporaryDirectory(prefix="aston_verify_") as temp_dir:
            verification_repo = Path(temp_dir) / "repo"

            # 1. Copy the original repository, excluding Git and environments.
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

            # 2. Overlay only successfully migrated files.
            for migrated_file in migration_path.rglob("*"):
                if not migrated_file.is_file():
                    continue

                relative_path = migrated_file.relative_to(migration_path)

                # Requirements are handled separately below.
                if relative_path == Path("requirements.txt"):
                    continue

                destination = verification_repo / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)

                shutil.copy2(migrated_file, destination)

            # 3. Replace requirements in the temporary repository.
            shutil.copy2(
                requirements_path,
                verification_repo / "requirements.txt",
            )

            container_req = "/app/requirements.txt"
            test_cmd = test_command.strip()

            script = f"""
export PIP_CACHE_DIR=/root/.cache/pip
set -eu

echo "--- [Verification] Installing requirements ---"
python -m pip install --disable-pip-version-check -r {shlex.quote(container_req)} ||
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

            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{verification_repo}:/app:ro,Z",
                "-v", "aston-pip-cache:/root/.cache/pip",
                "--workdir", "/app",
                python_image,
                "sh", "-c", script,
            ]

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
            elif "ASTON_PYTEST_SETUP_FAILED" in output:
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
        stdout = exc.stdout or ""
        stderr = exc.stderr or "Repository verification timed out."

        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")

        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

        return {
            "status": "TIMEOUT",
            "success": False,
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
        }

    except OSError as exc:
        return {
            "status": "DOCKER_ERROR",
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
        }
