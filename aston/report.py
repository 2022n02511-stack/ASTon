from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aston.cli_ui.rich import console
from aston.config import LOGS_DIR
from aston.exceptions import AstonError

logger = logging.getLogger(__name__)

ASTON_DIR_NAME = ".aston"
RUNS_DIR_NAME = "runs"
REPORT_FILENAME = "report.json"
LATEST_REPORT_FILENAME = "latest_report.json"


def _aston_workspace(repo_path: Path) -> Path:
    return repo_path.expanduser().resolve() / ASTON_DIR_NAME


def _summarize_changes_payload(changes_data: dict[str, Any] | None) -> dict[str, Any]:
    """Extract key counts and rule statuses from a per-file changes JSON."""
    if not isinstance(changes_data, dict):
        return {}

    rules = changes_data.get("rules") if isinstance(changes_data.get("rules"), list) else []
    rule_pairs = [
        {
            "package": r.get("package"),
            "old_api": r.get("old_api"),
            "new_api": r.get("new_api"),
            "validation_status": r.get("validation_status"),
            "target_api_status": r.get("target_api_status"),
        }
        for r in rules
        if isinstance(r, dict)
    ]

    return {
        "total_rules": changes_data.get("total_rules", len(rule_pairs)),
        "target_api_exists": changes_data.get("target_api_exists", 0),
        "target_api_missing": changes_data.get("target_api_missing", 0),
        "unverifiable_rules": changes_data.get("unverifiable_rules", 0),
        "not_probed_rules": changes_data.get("not_probed_rules", 0),
        "documentation_validated_rules": changes_data.get("documentation_validated_rules", 0),
        "rules": rule_pairs,
    }


def build_migration_report(
    *,
    run_id: str,
    repo_root: str | Path,
    provider: str,
    model: str,
    passed_files: list[str] | None = None,
    failed_files: list[str] | None = None,
    staged_files: list[str] | None = None,
    file_details: dict[str, dict[str, Any]] | None = None,
    single_file_verification: list[dict[str, Any]] | None = None,
    full_repo_verification: dict[str, Any] | None = None,
    verification_config: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    log_file: str | Path | None = None,
) -> dict[str, Any]:
    """Construct a structured run report combining file outcomes, rules, and Docker checks."""
    passed = list(passed_files or [])
    failed = list(failed_files or [])
    staged = list(staged_files or [])

    normalized_files: dict[str, Any] = {}
    for file_key, meta in (file_details or {}).items():
        entry = dict(meta)
        if "changes" in entry:
            entry["changes_summary"] = _summarize_changes_payload(entry.pop("changes"))
        normalized_files[file_key] = entry

    success = (
        len(failed) == 0
        and error is None
        and (
            full_repo_verification is None
            or bool(full_repo_verification.get("success", False))
        )
    )

    return {
        "schema_version": 1,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(Path(repo_root).expanduser().resolve()),
        "success": success,
        "llm": {
            "provider": provider,
            "model": model,
        },
        "verification_config": verification_config or {},
        "summary": {
            "passed_count": len(passed),
            "failed_count": len(failed),
            "staged_count": len(staged),
            "full_repo_status": (
                full_repo_verification.get("status")
                if isinstance(full_repo_verification, dict)
                else None
            ),
        },
        "passed_files": passed,
        "failed_files": failed,
        "staged_files": staged,
        "files": normalized_files,
        "single_file_verification": single_file_verification or [],
        "full_repo_verification": full_repo_verification,
        "error": error,
        "log_file": str(log_file or (LOGS_DIR / "aston.log")),
    }


def save_migration_report(repo_root: str | Path, report: dict[str, Any]) -> Path:
    """Persist report under .aston/runs/<run_id>/report.json and update latest_report.json."""
    repo_path = Path(repo_root).expanduser().resolve()
    aston_dir = _aston_workspace(repo_path)
    run_id = str(report["run_id"])
    run_dir = aston_dir / RUNS_DIR_NAME / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    run_report_path = run_dir / REPORT_FILENAME
    latest_report_path = aston_dir / LATEST_REPORT_FILENAME

    serialized = json.dumps(report, indent=2, default=str)
    run_report_path.write_text(serialized, encoding="utf-8")
    latest_report_path.write_text(serialized, encoding="utf-8")

    logger.debug("Saved migration report to %s and updated %s", run_report_path, latest_report_path)
    return run_report_path


def load_migration_report(
    repo_root: str | Path,
    run_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Load a specific run's report or the latest report for the repository."""
    repo_path = Path(repo_root).expanduser().resolve()
    aston_dir = _aston_workspace(repo_path)

    if run_id:
        candidate = aston_dir / RUNS_DIR_NAME / run_id / REPORT_FILENAME
        if not candidate.is_file():
            raise AstonError(
                f"No migration report found for run_id '{run_id}'.",
                details=f"Expected file at: {candidate}",
                exit_code=1,
            )
        report_path = candidate
    else:
        latest = aston_dir / LATEST_REPORT_FILENAME
        if latest.is_file():
            report_path = latest
        else:
            runs_dir = aston_dir / RUNS_DIR_NAME
            candidates = sorted(
                runs_dir.glob(f"*/{REPORT_FILENAME}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ) if runs_dir.is_dir() else []
            if not candidates:
                raise AstonError(
                    "No migration reports found in this repository.",
                    details=f"Run `aston check --run` first inside {repo_path}.",
                    exit_code=1,
                )
            report_path = candidates[0]

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstonError(
            f"Failed to read migration report at {report_path}.",
            details=str(exc),
            exit_code=1,
        ) from exc

    return data, report_path


def render_migration_report(
    report: dict[str, Any],
    report_path: Path,
    as_json: bool = False,
) -> None:
    """Render a migration report to the console as JSON or a Rich summary."""
    if as_json:
        console.print_json(json.dumps(report, default=str))
        return

    summary = report.get("summary", {})
    llm = report.get("llm", {})
    repo_ver = report.get("full_repo_verification") or {}

    console.print(f"\n[bold cyan]ASTon Migration Report[/bold cyan] — Run [bold]{report.get('run_id')}[/bold]")
    console.print(f"Timestamp : {report.get('timestamp')}")
    console.print(f"Repository: {report.get('repo_root')}")
    console.print(f"LLM       : {llm.get('provider')} / {llm.get('model')}")
    console.print(
        f"Outcome   : {'[bold green]PASS[/bold green]' if report.get('success') else '[bold red]NEEDS ATTENTION[/bold red]'}"
        f"  | Passed: {summary.get('passed_count', 0)}"
        f"  | Failed: {summary.get('failed_count', 0)}"
        f"  | Staged: {summary.get('staged_count', 0)}"
    )

    if repo_ver:
        console.print(
            f"Full Repo : status={repo_ver.get('status')} "
            f"network={repo_ver.get('network_mode')} "
            f"exit={repo_ver.get('exit_code')}"
        )

    files_map = report.get("files") or {}
    if files_map:
        console.print("\n[bold]Per-File Summary:[/bold]")
        for rel_path, meta in files_map.items():
            status = meta.get("status", "unknown")
            ch = meta.get("changes_summary") or {}
            rules_info = (
                f"rules={ch.get('total_rules', 0)} "
                f"(unverifiable={ch.get('unverifiable_rules', 0)}, "
                f"validated={ch.get('documentation_validated_rules', 0)})"
                if ch
                else ""
            )
            console.print(f"  - {rel_path}: [bold]{status}[/bold] {rules_info}".rstrip())

    if report.get("error"):
        err = report["error"]
        console.print(f"\n[bold red]Error:[/bold red] [{err.get('type')}] {err.get('message')}")
        if err.get("details"):
            console.print(f"  Details: {err.get('details')}")

    console.print(f"\nReport file: {report_path}")
    console.print(f"Log file   : {report.get('log_file')}")