# aston/notifications.py

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from aston.cli.console import rich_success, warning, info, error

import logging
from aston.config import LOGS_DIR, get_config_dir


def setup_logging(verbose: bool = False, quiet: bool = False) -> Path:
    """Configure file logging under ~/.config/aston/logs/ and console output."""
    get_config_dir()
    log_file = LOGS_DIR / "aston.log"

    root_logger = logging.getLogger("aston")
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    if quiet:
        stream_handler.setLevel(logging.ERROR)
    elif verbose:
        stream_handler.setLevel(logging.DEBUG)
    else:
        stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(stream_handler)

    return log_file

def get_notification_log_path(repo_root: Path) -> Path:
    """Resolve path to the local notification ledger inside .aston/"""
    aston_dir = repo_root / ".aston"
    aston_dir.mkdir(parents=True, exist_ok=True)
    return aston_dir / "notifications.json"

def log_notification(repo_root: Path, level: str, title: str, message: str) -> None:
    """
    Append an event to the local .aston/notifications.json ledger 
    and print a color-coded message to the CLI.
    """
    timestamp = datetime.now().isoformat()
    entry = {
        "timestamp": timestamp,
        "level": level.upper(),
        "title": title,
        "message": message
    }

    log_path = get_notification_log_path(repo_root)
    history = []
    if log_path.is_file():
        try:
            history = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            history = []

    history.append(entry)
    # Write pretty-printed JSON back to ledger
    log_path.write_text(json.dumps(history, indent=3), encoding="utf-8")

    # Instant CLI feedback
    formatted = f"[{title}] {message}"
    lvl = level.lower()
    if lvl == "success":
        rich_success(formatted)
    elif lvl == "warning":
        warning(formatted)
    elif lvl == "error":
        error(formatted)
    else:
        info(formatted)

def show_notifications(repo_root: Path, limit: int = 15) -> None:
    """Display recent notifications stored in the workspace ledger."""
    log_path = get_notification_log_path(repo_root)
    if not log_path.is_file():
        info("No notification history found in this workspace.")
        return

    try:
        history = json.loads(log_path.read_text(encoding="utf-8"))
    except Exception:
        error("Could not parse notification history log.")
        return

    print("\n" + "=" * 60)
    print(" 📋 ASTon Workspace Notification Ledger")
    print("=" * 60)
    
    if not history:
        print(" Ledger is currently empty.")
    else:
        for note in history[-limit:]:
            ts = note.get("timestamp", "")[:19].replace("T", " ")
            lvl = note.get("level", "INFO")
            title = note.get("title", "")
            msg = note.get("message", "")
            print(f"[{ts}] [{lvl}] {title} — {msg}")
            
    print("-" * 60)