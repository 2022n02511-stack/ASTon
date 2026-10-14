import subprocess
import os
import json
from enum import Enum
from dataclasses import dataclass
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
    raw_stderr: Optional[str] = None
    raw_stdout: Optional[str] = None

def verify_single_file_in_docker(target_file: str = "before.py", req_file: str = "temp_requirements.txt") -> VerificationOutput:
    """
    Stage 4: Sandboxed Docker Verification with Mocked Network Calls & Rich Error Context.
    Uses direct COPY into the image to eliminate host volume permission conflicts.
    """
    print("\n==================================================")
    print("--- [Stage 4] Sandboxed Docker Verification ---")
    print("==================================================")

    # Read requirements to determine which dependency-specific
    # verification behavior is needed.
    with open(req_file, "r") as f:
        requirements = f.read().lower()

    has_openai = any(
        line.strip().startswith("openai")
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    # 1. Generate the test wrapper script on host

    if has_openai:
        mock_wrapper_content = f"""import sys
import traceback
from unittest.mock import patch, MagicMock

# 1. Construct realistic synthetic returns for v1 SDK response objects
mock_choice = MagicMock()
mock_choice.message.content = "Mocked response verification successful."

mock_completion = MagicMock()
mock_completion.choices = [mock_choice]

mock_embedding_data = MagicMock()
mock_embedding_data.embedding = [0.1, 0.2, 0.3]

mock_embedding_response = MagicMock()
mock_embedding_response.data = [mock_embedding_data]

# 2. Patch OpenAI client methods directly at the resource layer
with patch("openai.resources.chat.completions.Completions.create", return_value=mock_completion), \
     patch("openai.resources.embeddings.Embeddings.create", return_value=mock_embedding_response):
    try:
        import {target_file.replace('.py', '')}
        print("STAGE4_MOCK_EXECUTION_SUCCESS")
    except AttributeError as e:
        print("STAGE4_ERROR_TYPE: AttributeError", file=sys.stderr)
        print(f"STAGE4_ERROR_MSG: {{e}}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(101)
    except Exception as e:
        print(f"STAGE4_ERROR_TYPE: {{type(e).__name__}}", file=sys.stderr)
        print(f"STAGE4_ERROR_MSG: {{e}}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(102)
"""
    else:
        mock_wrapper_content = f"""import sys
import traceback

try:
    import {target_file.replace('.py', '')}
    print("STAGE4_MOCK_EXECUTION_SUCCESS")
except AttributeError as e:
    print("STAGE4_ERROR_TYPE: AttributeError", file=sys.stderr)
    print(f"STAGE4_ERROR_MSG: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(101)
except Exception as e:
    print(f"STAGE4_ERROR_TYPE: {{type(e).__name__}}", file=sys.stderr)
    print(f"STAGE4_ERROR_MSG: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(102)
"""

    with open("run_mocked_test.py", "w") as f:
        f.write(mock_wrapper_content)

    # 2. Solution A: COPY files into Dockerfile & explicitly grant read permissions
    # In docker_sandbox.py
    dockerfile_content = f"""FROM python:3.11-slim

WORKDIR /app

ENV OPENAI_API_KEY=sk-test-dummy-key-for-verification-only

COPY {req_file} /app/requirements.txt

RUN pip install --no-cache-dir --default-timeout=120 --retries 5 -r requirements.txt

COPY {target_file} /app/{target_file}
COPY run_mocked_test.py /app/run_mocked_test.py

RUN chmod 644 /app/{target_file} /app/run_mocked_test.py
"""

    with open("Dockerfile.sandbox", "w") as f:
        f.write(dockerfile_content)

    try:
        # --- Step 1: Docker Build ---
        build_res = subprocess.run(
            ["docker", "build", "--progress=plain",
                "-t", "patch-verifier",
                "-f", "Dockerfile.sandbox", "."],
            text=True
        )
        if build_res.returncode != 0:
            print(f"❌ Environment Build Failed:\n{build_res.stderr}")
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=target_file,
                error_type="DockerBuildError",
                error_message="Docker build image failed",
                raw_stderr=build_res.stderr
            )

        # --- Step 2: AST Compilation Check ---
        print("⚡ Test 1: Checking Python AST compilation...")
        compile_res = subprocess.run(
            ["docker", "run", "--rm", "patch-verifier", "python", "-m", "py_compile", target_file],
            capture_output=True, text=True
        )
        if compile_res.returncode != 0:
            print(f"❌ AST Compilation Error:\n{compile_res.stderr}")
            return VerificationOutput(
                status=VerificationResult.MIGRATION_ERROR,
                target_file=target_file,
                error_type="SyntaxError",
                error_message="Python py_compile failed",
                raw_stderr=compile_res.stderr
            )
        print("✅ AST Compilation Passed!")

        # --- Step 3: Mocked Runtime & API Method Execution ---
        print("⚡ Test 2 & 3: Executing Mocked Runtime & Import Check...")
        exec_res = subprocess.run(
            ["docker", "run", "--rm", "patch-verifier", "python", "run_mocked_test.py"],
            capture_output=True, text=True, timeout=30
        )

        if exec_res.returncode == 0 and "STAGE4_MOCK_EXECUTION_SUCCESS" in exec_res.stdout:
            print("✅ Mocked Execution Passed! Migration verified cleanly.")
            return VerificationOutput(
                status=VerificationResult.PASS,
                target_file=target_file,
                raw_stdout=exec_res.stdout
            )

        stderr_output = exec_res.stderr

        # Catch Host / Container Filesystem Permissions Failure
        if "Permission denied" in stderr_output or "can't open file" in stderr_output:
            print(f"⚠️ Environment Permission Error:\n{stderr_output}")
            return VerificationOutput(
                status=VerificationResult.ENVIRONMENT_ERROR,
                target_file=target_file,
                error_type="PermissionError",
                error_message="Container filesystem permission denied",
                raw_stderr=stderr_output
            )

        # Extract Error Type and Message for Stage 5 Repair Loop
        error_type = "RuntimeError"
        error_msg = "Execution failed inside sandbox"
        for line in stderr_output.splitlines():
            if line.startswith("STAGE4_ERROR_TYPE:"):
                error_type = line.replace("STAGE4_ERROR_TYPE:", "").strip()
            elif line.startswith("STAGE4_ERROR_MSG:"):
                error_msg = line.replace("STAGE4_ERROR_MSG:", "").strip()

        if exec_res.returncode == 101 or "AttributeError" in stderr_output or "ImportError" in stderr_output:
            print(f"❌ Migration Structural Error detected:\n{stderr_output}")
            return VerificationOutput(
                status=VerificationResult.MIGRATION_ERROR,
                target_file=target_file,
                error_type=error_type,
                error_message=error_msg,
                raw_stderr=stderr_output,
                raw_stdout=exec_res.stdout
            )
        else:
            print(f"❌ Unhandled Execution Error:\n{stderr_output}")
            return VerificationOutput(
                status=VerificationResult.MIGRATION_ERROR,
                target_file=target_file,
                error_type=error_type,
                error_message=error_msg,
                raw_stderr=stderr_output,
                raw_stdout=exec_res.stdout
            )

    except subprocess.TimeoutExpired:
        return VerificationOutput(
            status=VerificationResult.TIMEOUT,
            target_file=target_file,
            error_message="Container execution timed out after 30s"
        )
    finally:
        # Cleanup temporary files from host working directory
        for tmp_file in ["Dockerfile.sandbox", "run_mocked_test.py"]:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)