import shutil
import subprocess
from aston.config import save_default_config, load_config, redact_secrets, CONFIG_FILE, LOGS_DIR
from aston.cli.console import rich_success, error, warning, info
import os
import yaml
import sys
from pathlib import Path
from aston.llm import call_llm


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
        "OPENAI_API_KEY": "Present" if os.getenv("OPENAI_API_KEY") else "Missing",
        "GITHUB_TOKEN": "Present" if os.getenv("GITHUB_TOKEN") else "Missing",
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

def handle_doctor(test_llm: bool = False, repo_path: str = "."):
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
    target_repo = Path(repo_path).expanduser().resolve()
    is_git_repo = (target_repo / ".git").exists()
    checks.append(("Repository Accessibility", is_git_repo, f"Path '{target_repo}' is {'' if is_git_repo else 'not '}a valid git repository"))

    # 8. Optional LLM Connectivity Test
    if test_llm:
        ok_llm, msg_llm = check_llm_connectivity(provider, model)
        checks.append(("LLM Live Connectivity", ok_llm, msg_llm))

    # Print Report
    failed_count = 0
    for name, success, details in checks:
        if success:
            print(f"  [✔] {name}: {details}")
        else:
            print(f"  [✘] {name}: {details}")
            failed_count += 1

    print("\n" + "-"*40)
    if failed_count == 0:
        rich_success("All diagnostic checks passed successfully!")
    else:
        warning(f"Doctor completed with {failed_count} warning(s) or failure(s). Review items above.")