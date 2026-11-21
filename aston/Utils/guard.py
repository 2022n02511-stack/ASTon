from pathlib import Path
from aston.cli.console import info, warning

class WorkspaceGuard:
    """Context manager to manage workspace lifecycle without destroying debug artifacts."""
    def __init__(self, repo_root: Path | str):
        self.repo_root = Path(repo_root).resolve()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            warning("\n[WorkspaceGuard] Pipeline error encountered.")
            info("[WorkspaceGuard] Preserving candidate files, logs, and artifacts in aston/artifacts/ for debugging.")
            
            # Only clean up transient root-level system boilerplate if it leaks
            temp_req = self.repo_root / "temp_requirements.txt"
            if temp_req.exists():
                temp_req.unlink(missing_ok=True)
                info("Cleaned up temporary requirements file.")
        
        # Propagate exception normally so main.py catches it and exits with correct code
        return False


from pathlib import Path
from aston.cli.console import warning, info


def _has_completed_migration(repo_root: Path) -> bool:
    """Return True if .aston already holds a non-failed migration."""
    aston_dir = repo_root / ".aston"
    if not aston_dir.exists():
        return False

    staging_dir = aston_dir / "staging"
    has_staged_files = staging_dir.is_dir() and any(p.is_file() for p in staging_dir.rglob("*"))
    if not has_staged_files:
        return False

    # Check if full-repo verification or latest run explicitly failed
    runs_dir = aston_dir / "runs"
    if runs_dir.is_dir():
        run_dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()])
        if run_dirs:
            latest_run = run_dirs[-1]
            full_repo_log = latest_run / "full_repo_verification.txt"
            if full_repo_log.is_file():
                content = full_repo_log.read_text(encoding="utf-8", errors="replace")
                if "Status: PASS" not in content and "Success: True" not in content:
                    return False

    return True    
    