import os
from pathlib import Path
import yaml
from aston.cli.console import error, info, rich_success, warning
from aston.exceptions import ConfigurationError

CONFIG_DIR = Path.home() / ".config" / "aston"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
LOGS_DIR = CONFIG_DIR / "logs"

DEFAULT_CONFIG = {
    "llm": {
        "provider": "mistral",           # Default LLM provider
        "model": "codestral-latest",
        "temperature": 0.0,
        "max_retries": 3,
    },
    "migration": {
        "per_file_repair_retries": 3,      # LLM repair loops per individual file
        "failed_files_sweep_passes": 2,    # Outer pipeline runs to retry remaining failed files
        "docker_file_timeout_seconds": 300, # Timeout for single-file sandbox checks
        "docker_repo_timeout_seconds": 300,# Timeout for repo-wide test suites
    },
    "verification": {
        "enabled": True,
        "python_image": "python:3.11-slim",
        "network_mode": "auto",             # "auto" | "none" | "bridge"
        "requirements_file": "requirements.txt",
        "test_command": "python -m pytest -q",
        "file_timeout": 300,
        "repo_timeout": 300,
        "build_timeout": 600,
    },
    "github": {
        "default_base_branch": "main",
        "draft_prs": False,
    },
}

# Runtime overrides storage (populated by CLI flags before execution)
_RUNTIME_OVERRIDES = {}


def set_runtime_config(overrides: dict):
    """Set temporary runtime configuration overrides from CLI flags."""
    global _RUNTIME_OVERRIDES
    _RUNTIME_OVERRIDES.update(overrides)


def get_config_dir() -> Path:
    """Ensure and return the user config directory (~/.config/aston/)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def _merge_yaml_file(config: dict, path: Path) -> None:
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
            if isinstance(user_config, dict):
                for section, values in user_config.items():
                    if (
                        section in config
                        and isinstance(values, dict)
                        and isinstance(config[section], dict)
                    ):
                        config[section].update(values)
                    else:
                        config[section] = values
    except Exception as exc:
        warning(f"Failed to parse {path}: {exc}. Continuing with current configuration.")


def load_config(repo_root: str | Path | None = None) -> dict:
    """
    Load configuration from ~/.config/aston/config.yaml and optional repo
    .aston/config.yaml, falling back to defaults, applying environment variable
    overrides, and overlaying runtime CLI overrides.
    """
    config = {
        section: values.copy() if isinstance(values, dict) else values
        for section, values in DEFAULT_CONFIG.items()
    }

    _merge_yaml_file(config, CONFIG_FILE)

    repo_dir = Path(repo_root).expanduser().resolve() if repo_root is not None else Path.cwd().resolve()
    repo_config = repo_dir / ".aston" / "config.yaml"
    if repo_config != CONFIG_FILE:
        _merge_yaml_file(config, repo_config)

    # Preserve legacy migration timeout keys if verification timeouts were not explicitly set.
    migration_cfg = config.get("migration", {})
    verification_cfg = config.get("verification", {})
    if isinstance(migration_cfg, dict) and isinstance(verification_cfg, dict):
        if "file_timeout" not in verification_cfg and "docker_file_timeout_seconds" in migration_cfg:
            verification_cfg["file_timeout"] = migration_cfg["docker_file_timeout_seconds"]
        if "repo_timeout" not in verification_cfg and "docker_repo_timeout_seconds" in migration_cfg:
            verification_cfg["repo_timeout"] = migration_cfg["docker_repo_timeout_seconds"]

    # Environment variable overrides
    if os.getenv("ASTON_MODEL"):
        config["llm"]["model"] = os.getenv("ASTON_MODEL")

    # Apply runtime CLI overrides on top
    if _RUNTIME_OVERRIDES:
        for section, values in _RUNTIME_OVERRIDES.items():
            if (
                section in config
                and isinstance(values, dict)
                and isinstance(config[section], dict)
            ):
                config[section].update(values)
            else:
                config[section] = values

    return config


def save_default_config() -> Path:
    """Initialize ~/.config/aston/config.yaml with default settings."""
    get_config_dir()
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False)
        rich_success(f"Initialized default configuration at: {CONFIG_FILE}")
    else:
        info(f"Configuration file already exists at: {CONFIG_FILE}")
    return CONFIG_FILE


def redact_secrets(data: any) -> any:
    """Recursively redact sensitive keys for safe display."""
    sensitive_keys = {"api_key", "token", "secret", "password", "github_token"}
    if isinstance(data, dict):
        return {
            k: ("********" if k.lower() in sensitive_keys else redact_secrets(v))
            for k, v in data.items()
        }
    elif isinstance(data, list):
        return [redact_secrets(item) for item in data]
    return data