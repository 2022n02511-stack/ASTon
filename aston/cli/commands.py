import shutil
import subprocess
from aston.config import save_default_config, load_config, redact_secrets, CONFIG_FILE, LOGS_DIR
from aston.cli.console import rich_success, error, warning, info
import os
import yaml
import sys
from pathlib import Path
from aston.llm import call_llm
from aston.paths import get_aston_paths, find_repo_root
import difflib

def handle_status(repo_path: Path | str | None = None):
    """List all staged files pending review or PR creation."""
    repo_root = Path(repo_path).expanduser().resolve() if repo_path else find_repo_root()
    paths = get_aston_paths(repo_root)
    staging_dir = paths["staging"]

    if not staging_dir.is_dir() or not any(staging_dir.iterdir()):
        info("No changes staged. Run 'aston check --run' to begin a migration.")
        return

    print("\n==================================================")
    print("--- ASTon Staged Migration Workspace Status ---")
    print("==================================================")
    
    staged_files = [f for f in staging_dir.rglob("*.py") if f.is_file()]
    for file_path in staged_files:
        rel = file_path.relative_to(staging_dir)
        rich_success(f"  staged:   {rel}")
    
    print("--------------------------------------------------")
    info(f"Total staged files: {len(staged_files)}")
    info("Run 'aston diff' to inspect changes, or 'aston pr' to push.")


def handle_diff(repo_path: Path | str | None = None):
    """Compare working tree files against their staged migrated counterparts."""
    repo_root = Path(repo_path).expanduser().resolve() if repo_path else find_repo_root()
    paths = get_aston_paths(repo_root)
    staging_dir = paths["staging"]

    if not staging_dir.is_dir() or not any(staging_dir.iterdir()):
        info("No staged files found to diff.")
        return

    staged_files = [f for f in staging_dir.rglob("*.py") if f.is_file()]
    
    for staged_path in staged_files:
        rel = staged_path.relative_to(staging_dir)
        original_path = repo_root / rel

        if not original_path.is_file():
            warning(f"[NEW] {rel} (Staged file does not exist in working tree yet)")
            continue

        original_code = original_path.read_text(encoding="utf-8").splitlines()
        staged_code = staged_path.read_text(encoding="utf-8").splitlines()

        diff = list(difflib.unified_diff(
            original_code,
            staged_code,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm=""
        ))

        if diff:
            print(f"\n--- Diff for {rel} ---")
            for line in diff:
                if line.startswith("+") and not line.startswith("+++"):
                    print(f"\033[32m{line}\033[0m")  # Green for additions
                elif line.startswith("-") and not line.startswith("---"):
                    print(f"\033[31m{line}\033[0m")  # Red for deletions
                else:
                    print(line)


def handle_clean(repo_path: Path | str | None = None):
    """Wipe out staging and temporary runtime artifacts."""
    repo_root = Path(repo_path).expanduser().resolve() if repo_path else find_repo_root()
    paths = get_aston_paths(repo_root)
    
    aston_dir = paths["aston_dir"]
    if aston_dir.is_dir():
        shutil.rmtree(paths["staging"], ignore_errors=True)
        shutil.rmtree(paths["runs"], ignore_errors=True)
        paths["staging"].mkdir(parents=True, exist_ok=True)
        paths["runs"].mkdir(parents=True, exist_ok=True)
        rich_success("ASTon staging area and temporary run artifacts cleaned successfully.")
    else:
        info("No ASTon workspace found to clean.")


def handle_init(repo_path: Path | str | None = None):
    """Execute `aston init` (Git-like workspace and configuration setup)."""
    info("Initializing ASTon configuration environment...")
    
    # 1. Save default configuration (keeping your existing setup)
    save_default_config()
    
    # 2. Bootstrap the hidden .aston/ workspace and subdirectories
    resolved_root = Path(repo_path).expanduser().resolve() if repo_path else Path.cwd().resolve()
    paths = get_aston_paths(resolved_root)
    
    paths["staging"].mkdir(parents=True, exist_ok=True)
    paths["runs"].mkdir(parents=True, exist_ok=True)
    paths["reports"].mkdir(parents=True, exist_ok=True)
    
    # 3. Automatically protect the workspace via .gitignore
    gitignore_path = resolved_root / ".gitignore"
    ignore_entry = ".aston/"
    
    if gitignore_path.is_file():
        content = gitignore_path.read_text(encoding="utf-8", errors="replace")
        if ignore_entry not in content:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write(f"\n# ASTon AI Migration Workspace\n{ignore_entry}\n")
            info("Added .aston/ to existing .gitignore")
    else:
        gitignore_path.write_text(f"# ASTon AI Migration Workspace\n{ignore_entry}\n", encoding="utf-8")
        info("Created .gitignore and added .aston/")
    
    rich_success(f"Workspace initialized at: {paths['aston_dir'].relative_to(resolved_root)}")
    rich_success(f"Logs directory ready at: {LOGS_DIR}")
    info("Setup complete! You are ready to run migrations.")


def handle_config_show():
    """Execute `aston config show`"""
    config = load_config()
    
    # Inject environment variable statuses (without exposing secrets)
    env_status = {
        "MISTRAL_API_KEY": "Present" if os.getenv("MISTRAL_API_KEY") else "Missing",
        "OPENAI_API_KEY": "Present" if os.getenv("OPENAI_API_KEY") else "Missing",
        "GITHUB_TOKEN": "Present" if os.getenv("GITHUB_TOKEN") else "Missing",
        "GEMINI_API_KEY": "Present" if os.getenv("GEMINI_API_KEY") else "Missing",
        "ANTHROPIC_API_KEY": "Present" if os.getenv("ANTHROPIC_API_KEY") else "Missing",
    }
    
    display_data = {
        "config_file": str(CONFIG_FILE),
        "settings": redact_secrets(config),
        "environment_secrets": env_status,
    }
    
    print(yaml.dump(display_data, sort_keys=False))


def check_python_version() -> tuple[bool, str]:
    """Check if Python version meets requirements (>= 3.10)."""
    v = sys.version_info
    if v.major >= 3 and v.minor >= 10:
        return True, f"Python {v.major}.{v.minor}.{v.micro} (Supported)"
    return False, f"Python {v.major}.{v.minor}.{v.micro} (Requires Python 3.10+)"

def check_command_availability(cmd: str) -> tuple[bool, str]:
    """Check if a CLI tool exists in the system PATH."""
    path = shutil.which(cmd)
    if path:
        return True, f"Found at {path}"
    return False, f"Not found in system PATH"

def check_docker_daemon() -> tuple[bool, str]:
    """Check if Docker is installed and the daemon is actively running."""
    if not shutil.which("docker"):
        return False, "Docker CLI not found"
    
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return True, "Docker daemon is running"
        return False, "Docker CLI found, but daemon is not running or lacks permissions"
    except Exception as exc:
        return False, f"Docker daemon check failed: {exc}"

def check_config_validity() -> tuple[bool, str]:
    """Check if configuration file parses correctly."""
    try:
        config = load_config()
        return True, f"Configuration loaded successfully (Sections: {list(config.keys())})"
    except Exception as exc:
        return False, f"Configuration error: {exc}"

def check_provider_credentials(provider: str) -> tuple[bool, str]:
    """Check if API keys are set for the selected or all providers."""
    env_keys = {
        "openai": "OPENAI_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY", # or GOOGLE_API_KEY
    }
    
    key_name = env_keys.get(provider.lower())
    if not key_name:
        return False, f"Unknown provider: {provider}"
    
    if provider.lower() == "google":
        has_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    else:
        has_key = bool(os.getenv(key_name))
        
    if has_key:
        return True, f"Environment variable {key_name} is set"
    return False, f"Environment variable {key_name} is missing"

def check_llm_connectivity(provider: str, model: str) -> tuple[bool, str]:
    """Performs a live API connectivity test call to the provider."""
    try:
        response = call_llm(
            prompt="Respond with 'OK' if you can read this.",
            provider=provider,
            model=model,
            temperature=0.0,
            system_prompt="You are a diagnostic health-check assistant."
        )
        return True, f"Successfully connected to {provider} ({model})"
    except Exception as exc:
        return False, f"Connectivity check failed: {exc}"

def handle_doctor(test_llm: bool = False, repo_path: Path | str | None = None):
    """Runs all diagnostic checks for ASTon."""
    info("Running ASTon Doctor Diagnostics...\n")
    
    checks = []

    # 1. Python Version
    ok, msg = check_python_version()
    checks.append(("Python Version", ok, msg))

    # 2. ASTon Installation
    try:
        import aston
        checks.append(("ASTon Installation", True, f"Installed at {Path(aston.__file__).parent}"))
    except ImportError:
        checks.append(("ASTon Installation", False, "ASTon package not found in Python environment"))

    # 3. Git Availability
    ok, msg = check_command_availability("git")
    checks.append(("Git Availability", ok, msg))

    # 4. Docker Availability & Daemon
    ok, msg = check_docker_daemon()
    checks.append(("Docker Availability", ok, msg))

    # 5. Configuration Validity
    ok, msg = check_config_validity()
    checks.append(("Configuration Validity", ok, msg))

    # Load config for provider/model context
    config = load_config()
    llm_cfg = config.get("llm", {})
    provider = llm_cfg.get("provider", "mistral")
    model = llm_cfg.get("model", "codestral-latest")

    # 6. Provider Credentials (Check all supported SDKs)
    for p in ["openai", "mistral", "anthropic", "google"]:
        ok_cred, msg_cred = check_provider_credentials(p)
        checks.append((f"Credentials [{p.capitalize()}]", ok_cred, msg_cred))

    # 7. Repository Accessibility
    resolved_root = Path(repo_path).expanduser().resolve() if repo_path else find_repo_root()
    is_git_repo = (resolved_root / ".git").exists()
    checks.append(("Repository Accessibility", is_git_repo, f"Path '{resolved_root}' is {'' if is_git_repo else 'not '}a valid git repository"))

    # 8. Optional LLM Connectivity Test
    if test_llm:
        ok_llm, msg_llm = check_llm_connectivity(provider, model)
        checks.append(("LLM Live Connectivity", ok_llm, msg_llm))

    # Print Report
    failed_count = 0
    for name, success, details in checks:
        if success:
            rich_success(f"{name}: {details}")
        else:
            warning(f"{name}: {details}")
            failed_count += 1

    print("\n" + "-"*40)
    if failed_count == 0:
        rich_success("All diagnostic checks passed successfully!")
    else:
        warning(f"Doctor completed with {failed_count} warning(s) or failure(s). Review items above.")