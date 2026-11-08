
from pathlib import Path



def _save_verification_result(file_run_dir: Path, attempt: str, result) -> None:
    """Persist Docker verification diagnostics for later investigation."""
    artifact_path = file_run_dir / f"{attempt}_verification.txt"

    content = [
        f"Status: {result.status}",
        f"Error type: {getattr(result, 'error_type', None)}",
        f"Error message: {getattr(result, 'error_message', None)}",
        "",
        "========== STDOUT ==========",
        getattr(result, "raw_stdout", "") or "",
        "",
        "========== STDERR ==========",
        getattr(result, "raw_stderr", "") or "",
    ]

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("\n".join(content), encoding="utf-8")


def save_code(path: Path, source_code: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source_code, encoding="utf-8")