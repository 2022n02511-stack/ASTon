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
        "model": "gpt-4o",
        "temperature": 0.0,
        "max_retries": 3,
    },
    "migration": {
        "max_repair_retries": 2,
        "docker_timeout_seconds": 300,
    },
    "github": {
        "default_base_branch": "main",
        "draft_prs": False,
    },
}


def get_config_dir() -> Path:
    """Ensure and return the user config directory (~/.config/aston/)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def load_config() -> dict:
    """
    Load configuration from ~/.config/aston/config.yaml,
    falling back to defaults and applying environment variable overrides.
    """
    config = DEFAULT_CONFIG.copy()

    if CONFIG_FILE.is_file():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f)
                if isinstance(user_config, dict):
                    # Deep merge user config into defaults
                    for section, values in user_config.items():
                        if section in config and isinstance(values, dict):
                            config[section].update(values)
                        else:
                            config[section] = values
        except Exception as exc:
            warning(f"Failed to parse {CONFIG_FILE}: {exc}. Using default configuration.")

    # Environment variable overrides
    if os.getenv("ASTON_MODEL"):
        config["llm"]["model"] = os.getenv("ASTON_MODEL")

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