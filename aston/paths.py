from pathlib import Path
from aston.exceptions import ConfigurationError

def find_repo_root(start_path: str | Path = None) -> Path:
    """Walk up parent directories from CWD to find the repository root (Git/ASTon style)."""
    current = Path(start_path).expanduser().resolve() if start_path else Path.cwd().resolve()
    
    for parent in [current, *current.parents]:
        if (parent / ".aston").is_dir() or (parent / ".git").is_dir() or (parent / "requirements.txt").is_file():
            return parent
            
    raise ConfigurationError(
        message="Not an ASTon repository (or any parent directory).",
        details="Run 'aston init' inside your project root to initialize."
    )

def get_aston_paths(repo_root: str | Path = None) -> dict:
    """Return all centralized internal paths for the ASTon workspace."""
    root = find_repo_root(repo_root)
    
    aston_dir = root / ".aston"
    return {
        "root": root,
        "aston_dir": aston_dir,
        "staging": aston_dir / "staging",
        "runs": aston_dir / "runs",
        "reports": aston_dir / "reports",
        "repo_index": aston_dir / "repo_index.json",
        "migrations_cache": aston_dir / "migrations.json",
    }