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