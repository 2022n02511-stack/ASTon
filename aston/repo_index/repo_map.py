import json
import logging
from pathlib import Path
from uuid import uuid4

from aston.artifacts.save_attempt import save_full_repo_verification_result
from aston.axios import scraper
from aston.cli.console import error, info, rich_success, warning
from aston.cli_ui.rich import print_aston_banner, print_rich_aston_banner, run_with_animation
from aston.config import LOGS_DIR, load_config
from aston.exceptions import ConfigurationError, MigrationError, VerificationError
from aston.github_integrations.pr import create_migration_pr
from aston.ingestion import resolve_github_repo, run_stage1_pipeline
from aston.pipeline import repair_file, run_for_repo
from aston.report import build_migration_report, save_migration_report
from aston.repo_verification import verify_full_repo_in_docker
from aston.scanner import parser
from aston.Utils.guard import _has_completed_migration
from aston.watcher import watcher

logger = logging.getLogger(__name__)


def discover_python_files(root: Path) -> list[Path]:
    """Return original project Python files, excluding generated artifacts."""
    excluded_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "reports",
        "build",
        "dist",
    }

    discovered = []

    for file_path in root.rglob("*.py"):
        relative = file_path.relative_to(root)

        if file_path.name == "__init__.py":
            continue

        # Exclude generated candidate files.
        if ".aston_candidate_" in file_path.name:
            continue

        # Exclude files inside generated or environment directories.
        if any(part in excluded_dirs for part in relative.parts):
            continue

        discovered.append(file_path)

    return sorted(discovered)


def repo_map(repo_path: str) -> dict:
    rel_path = Path(repo_path)
    root = rel_path.expanduser().resolve()

    logger.debug("Scanning dependencies for repository at %s", root)
    dependencies = watcher(repo_path)

    all_files_data = {}
    global_symbols = {}
    all_local_calls = []
    all_diagnostics = []

    py_files = discover_python_files(root)
    logger.debug("Discovered %d Python files for indexing", len(py_files))

    for py_file in py_files:
        relative_path = str(py_file.relative_to(root))
        parsed = parser(py_file, relative_path=relative_path)

        all_files_data[relative_path] = {
            "parse_status": parsed["file_meta"]["parse_status"],
            "sha256": parsed["file_meta"]["sha256"],
            "imports": parsed["imports"],
            "usages": parsed["usages"],
            "diagnostics": parsed["diagnostics"],
        }

        global_symbols.update(parsed["symbols"])
        all_local_calls.extend(parsed["local_calls"])
        all_diagnostics.extend(parsed["diagnostics"])

    # Populate watched_usage_ids into symbols based on direct usages
    for file_path, file_data in all_files_data.items():
        for usage in file_data["usages"]:
            enclosing = usage.get("enclosing_symbol")
            if enclosing:
                sym_key = f"{file_path}::{enclosing}"
                if sym_key in global_symbols:
                    if usage["id"] not in global_symbols[sym_key]["watched_usage_ids"]:
                        global_symbols[sym_key]["watched_usage_ids"].append(usage["id"])

    # Resolve Relationships across files with correct span & classification
    relationships = []
    for call in all_local_calls:
        target_symbol_key = None
        target_file = None

        for sym_key, sym_data in global_symbols.items():
            if sym_data["qualified_name"] == call["callee_name"]:
                target_symbol_key = sym_data["qualified_name"]
                target_file = sym_data["file"]
                break

        if call["is_parameter"]:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_line": call["end_line"],
                "end_column": call["end_column"],
                "target_file": None,
                "target_symbol": None,
                "resolution_status": "unresolved",
                "reason": "callee_is_parameter",
            })
        elif target_symbol_key and target_file:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_line": call["end_line"],
                "end_column": call["end_column"],
                "target_file": target_file,
                "target_symbol": target_symbol_key,
                "resolution_status": "resolved",
            })
        else:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_line": call["end_line"],
                "end_column": call["end_column"],
                "target_file": None,
                "target_symbol": None,
                "resolution_status": "unresolved",
                "reason": "unknown_symbol",
            })

    repo_index = {
        "schema_version": "1.1",
        "repository": {
            "root": root.name,
            "language": "python",
            "index_status": "complete",
        },
        "dependencies": dependencies,
        "symbols": global_symbols,
        "relationships": relationships,
        "files": all_files_data,
        "diagnostics": all_diagnostics,
    }

    return repo_index


def _load_file_changes_artifact(root: Path, run_dir: Path, relative_path: str) -> dict | None:
    """Load per-file rule/change JSON if saved in run_dir or .aston/reports."""
    stem = Path(relative_path).stem
    candidates = [
        run_dir / "changes" / f"{stem}_changes.json",
        run_dir / f"{stem}_changes.json",
        root / ".aston" / "reports" / f"{stem}_changes.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("Could not read changes artifact %s: %s", candidate, exc)
    return None


def migrate_repo(path: str):
    print_aston_banner()

    config = load_config()
    mig_cfg = config.get("migration", {})
    llm_cfg = config.get("llm", {})
    ver_cfg = config.get("verification", {})

    provider_name = llm_cfg.get("provider", "mistral")
    model_name = llm_cfg.get("model", "codestral-latest")

    per_file_retries = mig_cfg.get("per_file_repair_retries", 3)
    sweep_passes = mig_cfg.get("failed_files_sweep_passes", 2)

    ver_enabled = bool(ver_cfg.get("enabled", True))
    python_image = ver_cfg.get("python_image", "python:3.11-slim")
    network_mode = ver_cfg.get("network_mode", "auto")
    req_filename = ver_cfg.get("requirements_file", "requirements.txt")
    test_command = ver_cfg.get("test_command", "python -m pytest -q")
    docker_file_timeout = ver_cfg.get(
        "file_timeout", mig_cfg.get("docker_file_timeout_seconds", 300)
    )
    docker_repo_timeout = ver_cfg.get(
        "repo_timeout", mig_cfg.get("docker_repo_timeout_seconds", 300)
    )
    docker_build_timeout = ver_cfg.get("build_timeout", 600)

    root = Path(path).expanduser().resolve()
    run_id = uuid4().hex[:8]
    aston_dir = root / ".aston"
    runs_dir = aston_dir / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "aston.log"

    verification_config_snapshot = {
        "enabled": ver_enabled,
        "python_image": python_image,
        "network_mode": network_mode,
        "requirements_file": req_filename,
        "test_command": test_command,
        "file_timeout": docker_file_timeout,
        "repo_timeout": docker_repo_timeout,
        "build_timeout": docker_build_timeout,
    }

    logger.info(
        "Starting migration run=%s on repo=%s (provider=%s, model=%s, verify=%s, network=%s)",
        run_id,
        root,
        provider_name,
        model_name,
        ver_enabled,
        network_mode,
    )

    # 1. Generate and save repo index inside .aston/
    repo_index = repo_map(str(root))
    repo_index_path = aston_dir / "repo_index.json"
    repo_index_path.write_text(json.dumps(repo_index, indent=3), encoding="utf-8")
    logger.debug("Wrote repo index to %s", repo_index_path)

    # 2. Build and save migrations dictionary inside .aston/
    migrations = {}
    file_contexts = {}
    file_details: dict[str, dict] = {}

    for module, dep_data in repo_index.get("dependencies", {}).items():
        declared_version = dep_data.get("declared", {}).get("version")
        target_version = dep_data.get("target_version")

        if declared_version is not None:
            logger.debug(
                "Running stage 1 pipeline for module=%s (%s -> %s)",
                module,
                declared_version,
                target_version,
            )
            composed_rules, doc_text, raw_code_examples = run_stage1_pipeline(
                module,
                declared_version,
                target_version,
                repo_index["symbols"],
            )
            migrations[module] = {
                "composed_rules": composed_rules,
                "doc_text": doc_text,
                "raw_code_examples": raw_code_examples,
            }

    migrations_path = aston_dir / "migrations.json"
    migrations_path.write_text(json.dumps(migrations, indent=3), encoding="utf-8")
    logger.debug("Wrote migrations dictionary to %s", migrations_path)

    # 3. Build target requirements inside .aston/runs/ and .aston/staging/
    original_req_path = root / req_filename
    temp_req_path = runs_dir / "temp_requirements.txt"

    if not original_req_path.is_file():
        err_msg = f"Original requirements file not found: {original_req_path}"
        logger.error(err_msg)
        report = build_migration_report(
            run_id=run_id,
            repo_root=root,
            provider=provider_name,
            model=model_name,
            verification_config=verification_config_snapshot,
            error={"type": "ConfigurationError", "message": err_msg},
            log_file=log_file,
        )
        save_migration_report(root, report)
        raise ConfigurationError(
            message=err_msg,
            details="Ensure you are running 'aston check' inside a valid repository root containing requirements.txt.",
        )

    req_lines = original_req_path.read_text(encoding="utf-8").splitlines()

    for module, dep_data in repo_index.get("dependencies", {}).items():
        target_ver = dep_data.get("target_version")
        if not target_ver:
            continue

        new_req_line = f"{module}=={target_ver}"
        found = False

        for i, line in enumerate(req_lines):
            cleaned = line.strip().lower()

            if (
                cleaned == module.lower()
                or cleaned.startswith(f"{module.lower()}==")
                or cleaned.startswith(f"{module.lower()}>=")
                or cleaned.startswith(f"{module.lower()}<=")
                or cleaned.startswith(f"{module.lower()}~=")
                or cleaned.startswith(f"{module.lower()}!=")
                or cleaned.startswith(f"{module.lower()}<")
                or cleaned.startswith(f"{module.lower()}>")
            ):
                req_lines[i] = new_req_line
                found = True
                break

        if not found:
            req_lines.append(new_req_line)

    temp_req_path.write_text("\n".join(req_lines) + "\n", encoding="utf-8")

    staging_root = root / ".aston" / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    success_req_path = staging_root / "requirements.txt"
    success_req_path.write_text(
        temp_req_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    rich_success(f"Target requirements saved to: {success_req_path.relative_to(root)}")
    logger.debug(
        "Requirements paths — original=%s (exists=%s), temp=%s (exists=%s)",
        original_req_path,
        original_req_path.is_file(),
        temp_req_path,
        temp_req_path.is_file(),
    )

    # 4. Gather Python files and execute per-file pipeline
    py_files = discover_python_files(root)

    passed_files: list[str] = []
    failed_files: list[str] = []

    for py_file in py_files:
        relative_path = str(py_file.relative_to(root))

        file_data = repo_index.get("files", {}).get(relative_path, {})
        rules = file_data.get("usages", [])
        symbols = repo_index.get("symbols", {})
        relationships = repo_index.get("relationships", [])
        dependencies = repo_index.get("dependencies", {})
        source_code = py_file.read_text(encoding="utf-8")

        logger.info("Processing file: %s", relative_path)
        success, context_data = run_for_repo(
            root_path=str(root),
            path=relative_path,
            rules=rules,
            symbols=symbols,
            relationships=relationships,
            dependencies=dependencies,
            source_code=source_code,
            temp_req_path=temp_req_path,
            passed_files=passed_files,
            failed_files=failed_files,
            max_tries=per_file_retries,
            docker_file_time=docker_file_timeout,
            run_dir=run_dir,
            verification_enabled=ver_enabled,
            python_image=python_image,
            network_mode=network_mode,
            build_timeout=docker_build_timeout,
        )

        if context_data:
            file_contexts[relative_path] = context_data

        file_details[relative_path] = {
            "status": "passed" if success else "failed",
            "usages_count": len(rules),
            "changes": _load_file_changes_artifact(root, run_dir, relative_path),
        }

    # 5. Sweep Repair Loop for Failed Files
    if failed_files:
        logger.info(
            "Starting sweep repair loop for %d failed file(s) (max_retries=%d)",
            len(failed_files),
            sweep_passes,
        )
        info(f"Starting Sweep Repair Loop for {len(failed_files)} failed file(s)...")

        still_failed = []
        for target_rel_path in failed_files.copy():
            logger.info("Running repair loop on %s", target_rel_path)
            info(f"[Running Repair Loop]: {target_rel_path}")
            context_data = file_contexts.get(target_rel_path)

            if not context_data:
                logger.warning("Missing context_data for failed file %s; skipping repair", target_rel_path)
                still_failed.append(target_rel_path)
                continue

            recovered = repair_file(
                path=target_rel_path,
                temp_req_path=temp_req_path,
                context_data=context_data,
                root_path=str(root),
                docker_time=docker_file_timeout,
                max_retries=sweep_passes,
                passed_files=passed_files,
                failed_files=failed_files,
                verification_enabled=ver_enabled,
                python_image=python_image,
                network_mode=network_mode,
                build_timeout=docker_build_timeout,
            )

            if recovered:
                rich_success(f"Successfully recovered: {target_rel_path}")
                logger.info("Recovered file after repair: %s", target_rel_path)
                file_details[target_rel_path] = {
                    "status": "recovered",
                    "usages_count": len(repo_index.get("files", {}).get(target_rel_path, {}).get("usages", [])),
                    "changes": _load_file_changes_artifact(root, run_dir, target_rel_path),
                }
            else:
                still_failed.append(target_rel_path)
                error(f"Failed after retry attempts: {target_rel_path}")
                logger.error("File failed after repair attempts: %s", target_rel_path)
                file_details[target_rel_path] = {
                    "status": "failed",
                    "usages_count": len(repo_index.get("files", {}).get(target_rel_path, {}).get("usages", [])),
                    "changes": _load_file_changes_artifact(root, run_dir, target_rel_path),
                }

        failed_files = still_failed

    # 6. Final Summary & Exit Check
    logger.info(
        "Migration run=%s summary — passed=%d, failed=%d",
        run_id,
        len(passed_files),
        len(failed_files),
    )
    info(f"Final Migration Summary — Passed: {len(passed_files)}, Failed: {len(failed_files)}")

    if failed_files:
        warning(
            f"Migration completed with partial failures. "
            f"{len(failed_files)} file(s) failed after repair attempts."
        )
        info("Detailed debugging artifacts remain preserved in .aston/runs/.")
        info("Proceeding with full-repo verification and PR creation for successfully migrated files...")
    else:
        rich_success("All files migrated successfully!")

    staged_files = [
        str(p.relative_to(staging_root))
        for p in staging_root.rglob("*.py")
        if p.is_file()
    ]

    if not passed_files:
        err_msg = "Migration failed across all target files."
        logger.error("%s Failed files: %s", err_msg, ", ".join(failed_files))
        error("No files passed migration. Aborting full-repo verification and PR creation.")

        report = build_migration_report(
            run_id=run_id,
            repo_root=root,
            provider=provider_name,
            model=model_name,
            passed_files=passed_files,
            failed_files=failed_files,
            staged_files=staged_files,
            file_details=file_details,
            verification_config=verification_config_snapshot,
            error={"type": "MigrationError", "message": err_msg, "details": ", ".join(failed_files)},
            log_file=log_file,
        )
        save_migration_report(root, report)

        raise MigrationError(
            message=err_msg,
            details=", ".join(failed_files),
        )

    # 7. Repo-Wide Integration Verification
    logger.info("Stage 6: Repo-Wide Integration Verification")
    info("Stage 6: Repo-Wide Integration Verification")

    migration_root = root / ".aston" / "staging"
    test_result: dict | None = None

    if not ver_enabled:
        warning("Verification disabled via configuration (verification.enabled=false); skipping full-repo Docker check.")
        logger.info("Full-repo Docker verification skipped because verification.enabled is false")
        test_result = {
            "status": "SKIPPED",
            "success": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "network_mode": network_mode,
            "attempts": [],
        }
    else:
        test_result = verify_full_repo_in_docker(
            str(root),
            migration_root,
            migration_root / "requirements.txt",
            python_image=python_image,
            timeout=docker_repo_timeout,
            test_command=test_command,
            network_mode=network_mode,
        )

    save_full_repo_verification_result(run_dir, test_result)

    if test_result.get("status") == "PASS":
        temp_req_path.unlink(missing_ok=True)

    if not test_result.get("success", False):
        status_label = test_result.get("status")
        err_details = test_result.get("stderr") or test_result.get("stdout") or ""
        logger.error(
            "Full-repo verification failed (status=%s, exit_code=%s)",
            status_label,
            test_result.get("exit_code"),
        )
        error("Full-Repo Verification Failed!")
        info(f"Exit Code: {test_result.get('exit_code')}")
        if test_result.get("stdout"):
            logger.debug("Full-repo verification STDOUT:\n%s", test_result["stdout"])
        if test_result.get("stderr"):
            logger.error("Full-repo verification STDERR:\n%s", test_result["stderr"])

        report = build_migration_report(
            run_id=run_id,
            repo_root=root,
            provider=provider_name,
            model=model_name,
            passed_files=passed_files,
            failed_files=failed_files,
            staged_files=staged_files,
            file_details=file_details,
            full_repo_verification=test_result,
            verification_config=verification_config_snapshot,
            error={
                "type": "VerificationError",
                "message": f"Full-Repo Verification Failed with status: {status_label}",
                "details": err_details,
            },
            log_file=log_file,
        )
        save_migration_report(root, report)

        raise VerificationError(
            message=f"Full-Repo Verification Failed with status: {status_label}",
            details=err_details,
        )

    report = build_migration_report(
        run_id=run_id,
        repo_root=root,
        provider=provider_name,
        model=model_name,
        passed_files=passed_files,
        failed_files=failed_files,
        staged_files=staged_files,
        file_details=file_details,
        full_repo_verification=test_result,
        verification_config=verification_config_snapshot,
        log_file=log_file,
    )
    report_path = save_migration_report(root, report)
    logger.info("Saved final migration report to %s", report_path)

    rich_success("Full-Repo Test Suite Passed Successfully (Exit Code 0)!")
    info(f"Report saved to: {report_path.relative_to(root)} (run `aston report`)")
    info(f"Run `aston pr --path {staging_root}` for PR creation")