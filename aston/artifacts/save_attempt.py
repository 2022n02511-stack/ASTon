from pathlib import Path

def _save_verification_result(file_run_dir: Path, attempt: str, result) -> None:
    """Persist Docker verification diagnostics for later investigation."""
    artifact_path = file_run_dir / f"{attempt}_verification.txt"

    content = [
        f"Status: {getattr(result, 'status', 'UNKNOWN')}",
        f"Error type: {getattr(result, 'error_type', None)}",
        f"Error message: {getattr(result, 'error_message', None)}",
        f"Network Mode: {getattr(result, 'network_mode', None)}",
        f"Duration Seconds: {getattr(result, 'duration_seconds', None)}",
        f"Attempts History: {getattr(result, 'attempts', [])}",
        "",
        "========== STDOUT ==========",
        getattr(result, "raw_stdout", "") or "",
        "",
        "========== STDERR ==========",
        getattr(result, "raw_stderr", "") or "",
    ]

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("\n".join(str(c) for c in content), encoding="utf-8")


def save_code(path: Path, source_code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_code, encoding="utf-8")

def save_full_repo_verification_result(run_dir: Path, result: dict) -> Path:
    """Persist full-repository verification telemetry to disk."""
    artifact_path = run_dir / "full_repo_verification.txt"

    content = [
        f"Status: {result.get('status')}",
        f"Success: {result.get('success')}",
        f"Exit Code: {result.get('exit_code')}",
        f"Network Mode Used: {result.get('network_mode')}",
        f"Total Attempts: {len(result.get('attempts', []))}",
        "",
        "========== FULL-REPO STDOUT ==========",
        result.get("stdout", "") or "",
        "",
        "========== FULL-REPO STDERR ==========",
        result.get("stderr", "") or "",
    ]

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("\n".join(str(c) for c in content), encoding="utf-8")
    return artifact_path 