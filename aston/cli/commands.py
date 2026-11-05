import shutil
import subprocess
from aston.config import save_default_config, load_config, redact_secrets, CONFIG_FILE, LOGS_DIR
from aston.cli.console import rich_success, error, warning, info
import os
import yaml

def handle_init():
    """Execute `aston init`"""
    info("Initializing ASTon configuration environment...")
    save_default_config()
    rich_success(f"Logs directory ready at: {LOGS_DIR}")
    info("Setup complete! You are ready to run migrations.")


def handle_config_show():
    """Execute `aston config show`"""
    config = load_config()
    
    # Inject environment variable statuses (without exposing secrets)
    env_status = {
        "MISTRAL_API_KEY": "Present" if os.getenv("MISTRAL_API_KEY") else "Missing",
        "GITHUB_TOKEN": "Present" if os.getenv("GITHUB_TOKEN") else "Missing",
    }
    
    display_data = {
        "config_file": str(CONFIG_FILE),
        "settings": redact_secrets(config),
        "environment_secrets": env_status,
    }
    
    print(yaml.dump(display_data, sort_keys=False))


def handle_doctor():
    """Execute `aston doctor` - run environment and dependency diagnostics."""
    info("Running ASTon Doctor diagnostics...\n")
    passed = True

    # 1. Check Config File
    if CONFIG_FILE.is_file():
        rich_success(f"[OK] Configuration file found: {CONFIG_FILE}")
    else:
        warning(f"[WARN] Configuration file missing at {CONFIG_FILE}. Run `aston init`.")
        passed = False

    # 2. Check MISTRAL API Key
    if os.getenv("MISTRAL_API_KEY"):
        rich_success("[OK] MISTRAL_API_KEY environment variable is set.")
    else:
        error("[FAIL] MISTRAL_API_KEY environment variable is missing.")
        passed = False

    # 3. Check GitHub Token
    if os.getenv("GITHUB_TOKEN"):
        rich_success("[OK] GITHUB_TOKEN environment variable is set.")
    else:
        warning("[WARN] GITHUB_TOKEN environment variable is missing (Required for automated PR creation).")

    # 4. Check Docker Daemon
    docker_check = subprocess.run(["docker", "info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if docker_check.returncode == 0:
        rich_success("[OK] Docker daemon is running and accessible.")
    else:
        error("[FAIL] Docker daemon is not running or inaccessible. (Required for sandboxed verification)")
        passed = False

    # 5. Check Git
    git_check = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if git_check.returncode == 0:
        rich_success(f"[OK] Git is installed ({git_check.stdout.decode().strip()}).")
    else:
        error("[FAIL] Git is not installed or not in PATH.")
        passed = False

    print("\n-----------------------------------------")
    if passed:
        rich_success("All system checks passed successfully!")
    else:
        warning("Some checks failed or raised warnings. Review items above before running migrations.")