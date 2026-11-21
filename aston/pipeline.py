import json
import logging
import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

from aston.artifacts.save_attempt import _save_verification_result, save_code
from aston.axios import code_patcher, scraper, stage0
from aston.cli.console import error, info, rich_success, warning
from aston.cli_ui.rich import print_aston_banner, print_rich_aston_banner, run_with_animation
from aston.docker import VerificationOutput, VerificationResult, verify_single_file_in_docker
from aston.exceptions import ConfigurationError
from aston.repo_index import repo_map
from aston.rule_validation import (
    reconcile_report,
    validate_all_rules,
    validate_rules_against_target,
)
from aston.scanner import parser
from aston.watcher import watcher

logger = logging.getLogger(__name__)


def _safe_read_text(path: Path) -> str:
    """Read UTF-8 source text and fail clearly if unavailable."""
    return path.read_text(encoding="utf-8")


def _write_candidate(path: Path, source: str) -> bool:
    """Write a candidate only when it contains non-empty source code."""
    if not isinstance(source, str) or not source.strip():
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return True


def _candidate_path(path: str | Path, run_id: str, run_dir: Path) -> Path:
    source_path = Path(path).expanduser()
    # Place candidates safely inside .aston/runs/<run_id>/, away from source code
    safe_name = (
        f"{source_path.stem}.aston_candidate_{uuid4().hex[:6]}"
        f"{source_path.suffix or '.py'}"
    )
    return (run_dir / safe_name).resolve()


def _record_failure(path: str, failed_files: list) -> None:
    if path not in failed_files:
        failed_files.append(path)


def _record_success(path: str, passed_files: list, failed_files: list) -> None:
    if path in failed_files:
        failed_files.remove(path)

    if path not in passed_files:
        passed_files.append(path)


def _save_successful_migration(
    path: str,
    verified_code: str,
    file_run_dir: Path,
    root_path: str,
) -> None:
    """Save verified source to artifacts and staged migration workspace."""
    save_code(file_run_dir / "final.py", verified_code)

    repo_root = Path(root_path).expanduser().resolve()
    source_path = Path(path)

    if not source_path.is_absolute():
        source_path = repo_root / source_path

    try:
        relative_path = source_path.resolve().relative_to(repo_root)
    except ValueError as exc:
        raise ConfigurationError(
            message=f"Migration source is outside repository root: {source_path}",
            details="Ensure target files reside within the active repository directory.",
        ) from exc

    staging_root = repo_root / ".aston" / "staging"
    migrated_path = staging_root / relative_path

    migrated_path.parent.mkdir(parents=True, exist_ok=True)
    migrated_path.write_text(verified_code, encoding="utf-8")
    logger.debug("Staged migrated code to %s", migrated_path)

    gitignore_path = repo_root / ".gitignore"
    ignore_entry = ".aston/"
    if gitignore_path.is_file():
        content = gitignore_path.read_text(encoding="utf-8", errors="replace")
        if ignore_entry not in content:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write(f"\n# ASTon AI Migration Staging\n{ignore_entry}\n")
    else:
        gitignore_path.write_text(
            f"# ASTon AI Migration Staging\n{ignore_entry}\n", encoding="utf-8"
        )

    rich_success(f"Code successfully patched and staged to {migrated_path.relative_to(repo_root)}")


def _find_file_entry(repo_index: dict, path: str, root_path: str) -> dict:
    files_dict = repo_index.get("files", {})
    if not isinstance(files_dict, dict):
        return {}

    if path in files_dict and isinstance(files_dict[path], dict):
        return files_dict[path]

    target = Path(path)
    if not target.is_absolute():
        target = Path(root_path) / target

    target_resolved = target.resolve()

    for indexed_path, file_data in files_dict.items():
        if not isinstance(file_data, dict):
            continue
        indexed = Path(indexed_path)
        if not indexed.is_absolute():
            indexed = Path(root_path) / indexed

        if indexed.resolve() == target_resolved:
            return file_data

    return {}


def _get_file_usages(
    repo_index: dict,
    path: str,
    root_path: str,
    package_name: str,
) -> list[dict]:
    file_entry = _find_file_entry(repo_index, path, root_path)
    usages = file_entry.get("usages", [])

    if not isinstance(usages, list):
        return []

    return [
        usage
        for usage in usages
        if isinstance(usage, dict) and usage.get("package") == package_name
    ]


def _error_key(pr) -> str:
    error_type = getattr(pr, "error_type", None)
    if error_type is None:
        return ""
    return (
        str(getattr(error_type, "value", error_type))
        .lower()
        .replace("_", "")
        .replace(" ", "")
    )


def _is_repairable_failure(pr) -> bool:
    if pr.status == VerificationResult.PASS:
        return False

    if pr.status != VerificationResult.MIGRATION_ERROR:
        return False

    error_type = _error_key(pr)
    error_message = str(getattr(pr, "error_message", "") or "").lower()

    non_code_errors = {
        "targetfilenotfound",
        "environmenterror",
        "externalserviceerror",
        "timeout",
        "timeouterror",
    }

    if error_type in non_code_errors:
        return False

    if "target file not found" in error_message or "no such file or directory" in error_message:
        return False

    return True


def _verify_candidate(
    candidate_path: Path,
    temp_req_path: Path,
    root_path: str,
    docker_time: int,
    python_image: str,
    network_mode: str,
    build_timeout: int,
) -> VerificationOutput:
    logger.debug(
        "Verifying candidate %s in Docker (image=%s, network=%s, timeout=%s)",
        candidate_path,
        python_image,
        network_mode,
        docker_time,
    )
    return verify_single_file_in_docker(
        str(candidate_path),
        temp_req_path,
        repo_root=root_path,
        timeout=docker_time,
        build_timeout=build_timeout,
        python_image=python_image,
        network_mode=network_mode,
    )


def repair_file(
    path: str,
    temp_req_path: Path,
    context_data: dict,
    root_path: str,
    docker_time,
    initial_result=None,
    max_retries: int = 2,
    passed_files: list = None,
    failed_files: list = None,
    verification_enabled: bool = True,
    python_image: str = "python:3.11-slim",
    network_mode: str = "auto",
    build_timeout: int = 600,
):
    """Repair a candidate, preserving the original until verification passes."""
    if passed_files is None:
        passed_files = []

    if failed_files is None:
        failed_files = []

    retry_count = 0
    previous_attempts = []

    file_run_dir = Path(context_data["file_run_dir"])
    all_reports = context_data["all_reports"]
    validated_all_rules_dict = context_data["validated_all_rules_dict"]
    all_tar_vers = context_data["all_tar_vers"]
    all_cur_vers = context_data["all_cur_vers"]
    raw_code_examples = context_data["raw_code_examples"]
    rules = context_data["rules"]
    symbols = context_data["symbols"]
    relationships = context_data["relationships"]
    dependencies = context_data["dependencies"]

    candidate_path = Path(context_data.get("candidate_path", path))
    original_path = Path(context_data.get("original_path", path))
    display_path = context_data.get("display_path", str(original_path))

    if not verification_enabled:
        warning(
            f"Verification disabled (verification.enabled=false); staging {display_path} without Docker verification."
        )
        logger.info("Skipping Docker verification for %s (verification_enabled=False)", display_path)
        try:
            candidate_code = _safe_read_text(candidate_path)
        except (OSError, UnicodeError) as exc:
            error(f"Cannot read candidate source {candidate_path}: {exc}")
            logger.error("Failed reading candidate source %s: %s", candidate_path, exc)
            _record_failure(display_path, failed_files)
            return False

        _save_successful_migration(path, candidate_code, file_run_dir, root_path)
        candidate_path.unlink(missing_ok=True)
        _record_success(display_path, passed_files, failed_files)
        return True

    # Safely resolve repair instructions without NameError
    rep_con = context_data.get("repair_instructions")
    repair_context_path = Path(__file__).parent / "repair_context.txt"

    if rep_con is None and repair_context_path.exists():
        rep_con = repair_context_path.read_text(encoding="utf-8")

    pr = initial_result
    if pr is not None:
        _save_verification_result(file_run_dir, "initial", pr)

    if pr is None:
        pr = _verify_candidate(
            candidate_path=candidate_path,
            temp_req_path=temp_req_path,
            root_path=root_path,
            docker_time=docker_time,
            python_image=python_image,
            network_mode=network_mode,
            build_timeout=build_timeout,
        )

    while pr.status != VerificationResult.PASS and retry_count < max_retries:
        if not _is_repairable_failure(pr):
            warning(
                "Not sending this failure to code repair: "
                f"status={pr.status}, "
                f"type={getattr(pr, 'error_type', None)}, "
                f"message={getattr(pr, 'error_message', None)}"
            )
            logger.warning(
                "Non-repairable failure for %s: status=%s type=%s message=%s",
                display_path,
                pr.status,
                getattr(pr, "error_type", None),
                getattr(pr, "error_message", None),
            )
            break

        retry_count += 1

        try:
            failed_source = _safe_read_text(candidate_path)
        except (OSError, UnicodeError) as exc:
            error(f"Cannot read candidate source {candidate_path}: {exc}")
            logger.error("Failed reading candidate source %s during repair: %s", candidate_path, exc)
            _record_failure(display_path, failed_files)
            return False

        previous_attempts.append(
            {
                "attempt": retry_count,
                "source_code": failed_source,
                "error_type": getattr(pr, "error_type", None),
                "error_message": getattr(pr, "error_message", None),
                "traceback": getattr(pr, "raw_stderr", None),
            }
        )

        warning(
            f"Stage 4 Failed with "
            f"{getattr(pr, 'error_type', None)} "
            f"(Repair Attempt {retry_count}/{max_retries})"
        )
        info("Routing to Stage 5 Self-Healing Repair Loop...")
        logger.info(
            "Repair attempt %d/%d for %s (error_type=%s)",
            retry_count,
            max_retries,
            display_path,
            getattr(pr, "error_type", None),
        )

        repair_context = {
            "file_name": str(candidate_path),
            "original_path": str(original_path),
            "display_path": display_path,
            "error_type": getattr(pr, "error_type", None),
            "error_message": getattr(pr, "error_message", None),
            "traceback": getattr(pr, "raw_stderr", None),
            "attempted_code": failed_source,
            "previous_attempts": previous_attempts,
            "repair_instructions": rep_con,
        }

        context_artifact_path = file_run_dir / f"repair_context_{retry_count}.json"
        context_artifact_path.write_text(
            json.dumps(repair_context, indent=3, default=str),
            encoding="utf-8",
        )
        logger.debug("Wrote repair context artifact to %s", context_artifact_path)

        factored_code = code_patcher(
            source_code=failed_source,
            matches=all_reports,
            rules=validated_all_rules_dict,
            target_version=all_tar_vers,
            current_version=all_cur_vers,
            repo_rules=rules,
            symbols=symbols,
            relationships=relationships,
            dependencies=dependencies,
            repair_context=repair_context,
            raw_code_examples=raw_code_examples,
        )

        if not isinstance(factored_code, str) or not factored_code.strip():
            warning("Repair returned empty or invalid source. Stopping.")
            logger.warning("Repair patcher returned empty/non-string source for %s", display_path)
            _record_failure(display_path, failed_files)
            return False

        if factored_code == failed_source:
            warning("Repair returned unchanged source. Stopping.")
            logger.warning("Repair patcher returned unchanged source for %s", display_path)
            _record_failure(display_path, failed_files)
            return False

        repaired_attempt_path = file_run_dir / f"repaired_attempt_{retry_count}.py"
        save_code(repaired_attempt_path, factored_code)

        if not _write_candidate(candidate_path, factored_code):
            warning("Could not write repaired candidate. Stopping.")
            logger.error("Failed writing repaired candidate to %s", candidate_path)
            _record_failure(display_path, failed_files)
            return False

        pr = _verify_candidate(
            candidate_path=candidate_path,
            temp_req_path=temp_req_path,
            root_path=root_path,
            docker_time=docker_time,
            python_image=python_image,
            network_mode=network_mode,
            build_timeout=build_timeout,
        )

        _save_verification_result(
            file_run_dir,
            f"repair_{retry_count}",
            pr,
        )

    if pr.status == VerificationResult.PASS:
        try:
            verified_code = _safe_read_text(candidate_path)
        except (OSError, UnicodeError) as exc:
            error(f"Cannot read verified candidate {candidate_path}: {exc}")
            logger.error("Failed reading verified candidate %s: %s", candidate_path, exc)
            _record_failure(display_path, failed_files)
            return False

        _save_successful_migration(
            path,
            verified_code,
            file_run_dir,
            root_path,
        )

        _record_success(display_path, passed_files, failed_files)
        rich_success("Sandboxed Verification Successful!")
        logger.info("Verification passed for %s after %d repair attempt(s)", display_path, retry_count)
        return True

    info(
        f"Verification stopped after {retry_count} repair attempts. "
        "Original source was preserved."
    )
    logger.warning(
        "Verification stopped for %s after %d repair attempt(s) with status=%s",
        display_path,
        retry_count,
        pr.status,
    )

    _record_failure(display_path, failed_files)
    return False


def run_for_repo(
    root_path: str,
    path: str,
    rules,
    symbols,
    relationships,
    dependencies,
    source_code,
    temp_req_path,
    passed_files: list,
    failed_files: list,
    max_tries: int,
    docker_file_time,
    run_dir: Path,
    verification_enabled: bool = True,
    python_image: str = "python:3.11-slim",
    network_mode: str = "auto",
    build_timeout: int = 600,
):
    root = Path(root_path).expanduser().resolve()
    aston_dir = root / ".aston"

    repo_index_path = aston_dir / "repo_index.json"
    migrations_path = aston_dir / "migrations.json"

    with open(repo_index_path, "r", encoding="utf-8") as f:
        repo_index = json.load(f)

    with open(migrations_path, "r", encoding="utf-8") as f:
        migrations = json.load(f)

    logger.info("Stage 0: Inspecting repository usages for %s", path)

    all_rules: dict[str, list] = {}
    all_reports: dict[str, list] = {}
    all_cur_vers: dict[str, str] = {}
    all_tar_vers: dict[str, str] = {}
    incomplete_packages: list[str] = []

    for module, mod_data in migrations.items():
        stage0_symbols = stage0(module, repo_index)

        dep_info = repo_index.get("dependencies", {}).get(module, {})
        declared_version = dep_info.get("declared", {}).get("version")
        target_version = dep_info.get("target_version")

        if not declared_version or not target_version:
            raise ConfigurationError(
                message=f"Missing version specification for package '{module}'.",
                details=f"declared={declared_version!r}, target={target_version!r}",
            )

        current_version = declared_version
        composed_rules = mod_data.get("composed_rules", [])
        doc_text = mod_data.get("doc_text", "")

        file_usages = _get_file_usages(
            repo_index=repo_index,
            path=path,
            root_path=root_path,
            package_name=module,
        )

        if not file_usages:
            warning(
                f"[SKIP] {path} has no indexed usages of {module}; "
                "skipping per-file audit."
            )
            logger.debug("Skipping %s for module %s: no indexed usages", path, module)
            continue

        report, relevant_rules, is_complete = scraper(
            module,
            current_version,
            target_version,
            stage0_symbols,
            file_usages,
            composed_rules,
            doc_text,
            source_file=path,
        )

        if not report:
            warning(
                f"No legacy usages matched in {path}. "
                "Skipping migration for this file."
            )
            logger.debug("No legacy usages matched in %s for module %s", path, module)
            continue

        if not is_complete:
            incomplete_packages.append(module)

        all_rules[module] = relevant_rules
        all_reports[module] = report
        all_cur_vers[module] = current_version
        all_tar_vers[module] = target_version

    flat_all_rules = []
    for pkg, pkg_rules in all_rules.items():
        for rule in pkg_rules:
            rule = dict(rule)
            rule.setdefault("package", pkg)
            flat_all_rules.append(rule)

    known_target_versions = {
        pkg: version
        for pkg, version in all_tar_vers.items()
        if version and version != "unknown"
    }

    validation_req_file = (
        Path(temp_req_path)
        if temp_req_path is not None and Path(temp_req_path).is_file()
        else Path(root_path) / "requirements.txt"
    )

    if flat_all_rules and known_target_versions:
        validated_rules = run_with_animation(
            "Running sandboxed batch validation across target environments...",
            validate_all_rules,
            all_rules=flat_all_rules,
            all_target_versions=known_target_versions,
            req_file=validation_req_file,
        )
    else:
        validated_rules = []

    final_report_dict = reconcile_report(
        extraction_report={"rules": validated_rules},
        validated_rules=validated_rules,
    )

    file_slug = path.replace("/", "_").replace(".py", "")
    file_run_dir = run_dir / file_slug

    output_path = root / ".aston" / "reports" / f"{file_slug}_changes.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(final_report_dict, indent=3), encoding="utf-8")

    # Also store a copy inside the active run directory so per-run reports can read it directly
    if run_dir is not None:
        run_changes_path = Path(run_dir) / "changes" / f"{file_slug}_changes.json"
        run_changes_path.parent.mkdir(parents=True, exist_ok=True)
        run_changes_path.write_text(json.dumps(final_report_dict, indent=3), encoding="utf-8")

    validated_all_rules_dict = {}
    for rule in validated_rules:
        pkg = rule.get("package")
        if not pkg:
            continue
        validated_all_rules_dict.setdefault(pkg, []).append(rule)

    has_valid_rules = any(
        bool(package_rules) for package_rules in validated_all_rules_dict.values()
    )
    has_valid_matches = any(
        isinstance(report, list) and len(report) > 0
        for report in all_reports.values()
    )

    if not has_valid_rules or not has_valid_matches:
        info(f"[SKIP] No applicable migration for {path}")
        logger.info("Skipping %s: no valid rules or legacy matches", path)
        return True, {}

    file_run_dir.mkdir(parents=True, exist_ok=True)
    save_code(file_run_dir / "original.py", source_code)

    raw_code_examples = []
    for mod_data in migrations.values():
        examples = mod_data.get("raw_code_examples", [])
        if isinstance(examples, list):
            raw_code_examples.extend(examples)

    source_path = Path(path).expanduser()
    if not source_path.is_absolute():
        source_path = root / source_path

    source_path = source_path.resolve()

    try:
        source_path.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(
            message=f"Source file {source_path} is outside repository root {root}.",
            details="All files targeted for migration must be located within the active repository workspace.",
        ) from exc

    original_path = source_path
    run_id = run_dir.name
    candidate_path = _candidate_path(source_path, run_id, run_dir).resolve()

    if not _write_candidate(candidate_path, source_code):
        error("Original source input is empty or invalid; refusing to patch.")
        logger.error("Refusing to patch %s: original source is empty/invalid", path)
        _record_failure(path, failed_files)
        return False, {}

    try:
        logger.info("Generating migrated candidate code for %s", path)
        factored_code = code_patcher(
            source_code=source_code,
            matches=all_reports,
            rules=validated_all_rules_dict,
            target_version=all_tar_vers,
            current_version=all_cur_vers,
            repo_rules=rules,
            symbols=symbols,
            relationships=relationships,
            dependencies=dependencies,
            repair_context=None,
            raw_code_examples=raw_code_examples,
        )

        if not isinstance(factored_code, str) or not factored_code.strip():
            warning("Patcher returned empty or invalid source. Original source preserved.")
            logger.warning("Code patcher returned empty/invalid code for %s", path)
            _record_failure(path, failed_files)
            return False, {}

        if factored_code == source_code:
            warning("Patcher returned unchanged source. Skipping verification.")
            logger.info("Code patcher returned unchanged source for %s; skipping verification", path)
            _record_success(path, passed_files, failed_files)
            return True, {}

        save_code(file_run_dir / "first_attempt.py", factored_code)

        if not _write_candidate(candidate_path, factored_code):
            warning("Could not write candidate source. Original source preserved.")
            logger.error("Failed writing candidate source for %s to %s", path, candidate_path)
            _record_failure(path, failed_files)
            return False, {}

        if not verification_enabled:
            warning(
                f"Verification disabled (verification.enabled=false); "
                f"staging {path} without Docker verification."
            )
            logger.info("Staging %s without Docker verification (verification_enabled=False)", path)
            _save_successful_migration(path, factored_code, file_run_dir, str(root))
            candidate_path.unlink(missing_ok=True)
            _record_success(path, passed_files, failed_files)
            return True, {}

        pr = _verify_candidate(
            candidate_path=candidate_path,
            temp_req_path=temp_req_path,
            root_path=str(root),
            docker_time=docker_file_time,
            python_image=python_image,
            network_mode=network_mode,
            build_timeout=build_timeout,
        )

        _save_verification_result(file_run_dir, "initial", pr)

        context_data = {
            "file_run_dir": file_run_dir,
            "all_reports": all_reports,
            "candidate_path": str(candidate_path),
            "validated_all_rules_dict": validated_all_rules_dict,
            "all_tar_vers": all_tar_vers,
            "all_cur_vers": all_cur_vers,
            "raw_code_examples": raw_code_examples,
            "rules": rules,
            "symbols": symbols,
            "relationships": relationships,
            "dependencies": dependencies,
            "source_code": source_code,
            "original_path": str(original_path),
            "display_path": path,
            "repair_instructions": None,
        }

        if pr.status == VerificationResult.PASS:
            verified_code = _safe_read_text(candidate_path)
            _save_successful_migration(
                path,
                verified_code,
                file_run_dir,
                str(root),
            )
            candidate_path.unlink(missing_ok=True)
            _record_success(path, passed_files, failed_files)
            rich_success("Sandboxed Verification Successful!")
            logger.info("Initial Docker verification passed for %s", path)
            return True, context_data

        info(f"Initial verification failed for {path}. Starting repair loop...")
        logger.warning(
            "Initial Docker verification failed for %s (status=%s); entering repair loop",
            path,
            pr.status,
        )

        success = repair_file(
            path=path,
            temp_req_path=temp_req_path,
            context_data=context_data,
            root_path=str(root),
            docker_time=docker_file_time,
            initial_result=pr,
            max_retries=max_tries,
            passed_files=passed_files,
            failed_files=failed_files,
            verification_enabled=verification_enabled,
            python_image=python_image,
            network_mode=network_mode,
            build_timeout=build_timeout,
        )

        return success, context_data

    finally:
        if path in passed_files and candidate_path.exists():
            candidate_path.unlink()