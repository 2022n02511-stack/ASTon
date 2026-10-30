from aston.axios import code_patcher, stage0, scraper
from aston.docker import verify_single_file_in_docker, VerificationResult
from aston.watcher import watcher
from aston.scanner import parser
from pathlib import Path
import json
import os
from uuid import uuid4
from aston.repo_index import repo_map


from aston.rule_validation import (
    validate_rules_against_target,
    validate_all_rules,
    reconcile_report,
)
from aston.artifacts.save_attempt import save_code
from aston.cli_ui.rich import (
    print_rich_aston_banner,
    run_with_animation,
    print_aston_banner,
)


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


def _candidate_path(path: str | Path, run_id: str) -> Path:
    source_path = Path(path).expanduser()

    return source_path.with_name(
        f".{source_path.stem}.aston_candidate_{run_id}"
        f"{source_path.suffix or '.py'}"
    )


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
) -> None:
    """Save verified source to artifacts and succ_migrations."""
    save_code(file_run_dir / "final.py", verified_code)

    migrated_path = Path("succ_migrations") / path
    migrated_path.parent.mkdir(parents=True, exist_ok=True)
    migrated_path.write_text(verified_code, encoding="utf-8")

    print(f"Code successfully patched and saved to {migrated_path}")


def _find_file_entry(repo_index: dict, path: str, root_path: str) -> dict:
    """
    Find the current file's entry in repo_index.

    Supports an exact key match and comparisons between relative and absolute
    paths. It deliberately avoids basename-only matching because repositories
    can contain multiple files with the same name.
    """
    files_dict = repo_index.get("files", {})

    if not isinstance(files_dict, dict):
        return {}

    # Fast path: exact key.
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
    """Return usages for one file and one package, not the entire repository."""
    file_entry = _find_file_entry(repo_index, path, root_path)

    usages = file_entry.get("usages", [])

    if not isinstance(usages, list):
        return []

    print(f"[DEBUG] File: {path}")
    print(f"[DEBUG] Indexed usage count: {len(usages)}")
    print(
        "[DEBUG] Indexed package values:",
        sorted({
            str(u.get("package"))
            for u in usages
            if isinstance(u, dict)
        }),
    )
    print(f"[DEBUG] Requested package: {package_name}")

    return [
        usage
        for usage in usages
        if isinstance(usage, dict)
        and usage.get("package") == package_name
    ]


def _error_key(pr) -> str:
    """Normalize verifier error type for classification."""
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
    """
    Only route likely source-code migration errors to the code repair loop.

    Path, environment, external-service, and timeout errors should be
    diagnosed separately rather than blindly prompting the model to rewrite
    Python source.
    """
    if pr.status == VerificationResult.PASS:
        return False

    if pr.status != VerificationResult.MIGRATION_ERROR:
        return False

    error_type = _error_key(pr)
    error_message = str(
        getattr(pr, "error_message", "") or ""
    ).lower()

    non_code_errors = {
        "targetfilenotfound",
        "environmenterror",
        "externalserviceerror",
        "timeout",
        "timeouterror",
    }

    if error_type in non_code_errors:
        return False

    if "target file not found" in error_message:
        return False

    if "no such file or directory" in error_message:
        return False

    return True


def repair_file(
    path: str,
    temp_req_path: Path,
    context_data: dict,
    root_path: str,
    initial_result=None,
    max_retries: int = 2,
    passed_files: list = None,
    failed_files: list = None,
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

    candidate_path = Path(path)
    original_path = Path(context_data.get("original_path", path))
    display_path = context_data.get("display_path", str(original_path))

    rep_con = context_data.get("repair_instructions")

    if rep_con is None:
        repair_context_path = Path("repair_context.txt")

        if repair_context_path.exists():
            rep_con = repair_context_path.read_text(encoding="utf-8")

    # Reuse the initial result instead of verifying the same candidate twice.
    pr = initial_result

    if pr is None:
        pr = verify_single_file_in_docker(
            str(candidate_path),
            temp_req_path,
            repo_root=root_path,
        )

    while pr.status != VerificationResult.PASS and retry_count < max_retries:
        if not _is_repairable_failure(pr):
            print(
                "\nNot sending this failure to code repair: "
                f"status={pr.status}, "
                f"type={getattr(pr, 'error_type', None)}, "
                f"message={getattr(pr, 'error_message', None)}"
            )
            break

        retry_count += 1

        # Always read the candidate being repaired. pr.target_file is
        # diagnostic metadata, not the authoritative source path.
        try:
            failed_source = _safe_read_text(candidate_path)
        except (OSError, UnicodeError) as exc:
            print(f"Cannot read candidate source {candidate_path}: {exc}")
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

        print(
            f"\nStage 4 Failed with "
            f"{getattr(pr, 'error_type', None)} "
            f"(Repair Attempt {retry_count}/{max_retries})"
        )
        print("Routing to Stage 5 Self-Healing Repair Loop...")

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
            print("Repair returned empty or invalid source. Stopping.")
            _record_failure(display_path, failed_files)
            return False

        if factored_code == failed_source:
            print("Repair returned unchanged source. Stopping.")
            _record_failure(display_path, failed_files)
            return False

        repaired_attempt_path = (
            file_run_dir / f"repaired_attempt_{retry_count}.py"
        )
        save_code(repaired_attempt_path, factored_code)

        if not _write_candidate(candidate_path, factored_code):
            print("Could not write repaired candidate. Stopping.")
            _record_failure(display_path, failed_files)
            return False

        # Verify the newly written candidate; use this result for the next
        # repair decision.
        pr = verify_single_file_in_docker(
            str(candidate_path),
            temp_req_path,
            repo_root=root_path,
        )

    if pr.status == VerificationResult.PASS:
        try:
            verified_code = _safe_read_text(candidate_path)
        except (OSError, UnicodeError) as exc:
            print(f"Cannot read verified candidate {candidate_path}: {exc}")
            _record_failure(display_path, failed_files)
            return False

        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_text(verified_code, encoding="utf-8")

        _save_successful_migration(
            path,
            verified_code,
            file_run_dir,
        )

        _record_success(display_path, passed_files, failed_files)

        print("\nSandboxed Verification Successful!")
        return True

    print(
        f"\nVerification stopped after {retry_count} repair attempts. "
        "Original source was preserved."
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
):
    with open("repo_index.json", "r", encoding="utf-8") as f:
        repo_index = json.load(f)

    with open("migrations.json", "r", encoding="utf-8") as f:
        migrations = json.load(f)

    run_id = uuid4().hex[:12]
    run_dir = Path("aston/artifacts") / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print("\n==================================================")
    print("--- [Stage 0] Global Repository Scanned Usages ---")
    print("==================================================")

    all_rules: dict[str, list] = {}
    all_reports: dict[str, list] = {}
    all_cur_vers: dict[str, str] = {}
    all_tar_vers: dict[str, str] = {}
    incomplete_packages: list[str] = []

    # Stage 0 remains repository-wide. Actual matching in scraper is
    # restricted to the current file.
    for module, mod_data in migrations.items():
        stage0_symbols = stage0(module, repo_index)

        dep_info = repo_index.get("dependencies", {}).get(module, {})
        declared_version = dep_info.get("declared", {}).get("version")
        target_version = dep_info.get("target_version")

        if not declared_version or not target_version:
            raise ValueError(
                f"Missing version specification for package '{module}': "
                f"declared={declared_version!r}, "
                f"target={target_version!r}"
            )

        current_version = declared_version

        composed_rules = mod_data.get("composed_rules", [])
        doc_text = mod_data.get("doc_text", "")

        # IMPORTANT: only this file's usages are passed to scraper/man_extract.
        file_usages = _get_file_usages(
            repo_index=repo_index,
            path=path,
            root_path=root_path,
            package_name=module,
        )

        
        print(
            f"File-specific usages for {path} / {module}: "
            f"{len(file_usages)}"
        )

        # Do not run relevance/completeness analysis for a package
        # that has no indexed usages in the current file.
        if not file_usages:
            print(
                f"[SKIP] {path} has no indexed usages of {module}; "
                "skipping per-file audit."
            )
            continue

        report, relevant_rules, is_complete = run_with_animation(
            f"Evaluating relevance and completeness for {module}...",
            scraper,
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
            print(
                f"No legacy usages matched in {path}. "
                "Skipping migration for this file."
            )
            continue

        if not is_complete:
            incomplete_packages.append(module)

        # Each report now represents matches from the current file only.
        all_rules[module] = relevant_rules
        all_reports[module] = report
        all_cur_vers[module] = current_version
        all_tar_vers[module] = target_version

    # Flatten rules and ensure every rule has a package identifier.
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

    if flat_all_rules and known_target_versions:
        validated_rules = run_with_animation(
            "Running sandboxed batch validation across target environments...",
            validate_all_rules,
            all_rules=flat_all_rules,
            all_target_versions=known_target_versions,
            req_file=Path(root_path) / "requirements.txt",
        )
    else:
        validated_rules = []

        if flat_all_rules and not known_target_versions:
            print(
                "Target versions are unknown; "
                "skipping target-environment batch validation."
            )

    final_report_dict = reconcile_report(
        extraction_report={"rules": validated_rules},
        validated_rules=validated_rules,
    )

    flat_filename = path.replace("/", "_")
    output_path = (
        Path("final_reports")
        / f"{Path(flat_filename).stem}_changes.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report_dict, f, indent=3)

    print(f"Canonical audit contract saved to {output_path}")

    validated_all_rules_dict = {}

    for rule in validated_rules:
        pkg = rule.get("package")

        if not pkg:
            continue

        validated_all_rules_dict.setdefault(pkg, []).append(rule)

    has_valid_rules = any(
        bool(package_rules)
        for package_rules in validated_all_rules_dict.values()
    )

    # Reports are now file-specific, so this checks whether this file has
    # actual matched usages instead of merely checking that an audit exists.
    has_valid_matches = any(
        isinstance(report, list) and len(report) > 0
        for report in all_reports.values()
    )

    if not has_valid_rules or not has_valid_matches:
        print(f"[SKIP] No applicable migration for {path}")
        return True, {}

    file_slug = path.replace("/", "_").replace(".py", "")
    file_run_dir = run_dir / file_slug
    file_run_dir.mkdir(parents=True, exist_ok=True)

    save_code(file_run_dir / "original.py", source_code)

    raw_code_examples = []

    for mod_data in migrations.values():
        examples = mod_data.get("raw_code_examples", [])

        if isinstance(examples, list):
            raw_code_examples.extend(examples)

        # Normalize repository and source paths before creating the candidate.
    root_path = Path(root_path).expanduser().resolve()
    source_path = Path(path).expanduser()

    if not source_path.is_absolute():
        source_path = root_path / source_path

    source_path = source_path.resolve()

    try:
        source_path.relative_to(root_path)
    except ValueError:
        raise ValueError(
            f"Source file {source_path} is outside repository root "
            f"{root_path}"
        )

    original_path = source_path
    candidate_path = _candidate_path(source_path, run_id).resolve()

    print("\n--- CANDIDATE DIAGNOSTICS ---")
    print(f"Repository root: {root_path}")
    print(f"Original path: {original_path}")
    print(f"Candidate path: {candidate_path}")
    print(f"Candidate exists: {candidate_path.is_file()}")
    print("-----------------------------\n")

    print("\n--- CANDIDATE DIAGNOSTICS ---")
    print(f"Candidate path: {candidate_path}")
    print(f"Candidate exists: {candidate_path.is_file()}")

    if candidate_path.is_file():
        print(f"Candidate size: {candidate_path.stat().st_size} bytes")
    else:
        print("ERROR: Candidate was not written before verification.")

    print("-----------------------------\n")

    if not _write_candidate(candidate_path, source_code):
        print(
            "Original source input is empty or invalid; "
            "refusing to patch."
        )
        _record_failure(path, failed_files)
        return False, {}

    try:
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
            print(
                "Patcher returned empty or invalid source. "
                "Original source preserved."
            )
            _record_failure(path, failed_files)
            return False, {}

        if factored_code == source_code:
            print("Patcher returned unchanged source. Skipping verification.")
            _record_success(path, passed_files, failed_files)
            return True, {}

        save_code(file_run_dir / "first_attempt.py", factored_code)

        if not _write_candidate(candidate_path, factored_code):
            print(
                "Could not write candidate source. "
                "Original source preserved."
            )
            _record_failure(path, failed_files)
            return False, {}

        # Initial verification is performed exactly once.
        pr = verify_single_file_in_docker(
            str(candidate_path),
            temp_req_path,
            repo_root=root_path,
        )

        context_data = {
            "file_run_dir": file_run_dir,
            "all_reports": all_reports,
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

            # Preserve the original source file.
            # Save the verified migrated code to succ_migrations instead.
            _save_successful_migration(
                path,
                verified_code,
                file_run_dir,
            )

            # Remove the candidate only after its successful migration is saved.
            candidate_path.unlink(missing_ok=True)

            _record_success(path, passed_files, failed_files)

            print("\nSandboxed Verification Successful!")
            return True, context_data

        print(
            f"\nInitial verification failed for {path}. "
            "Starting repair loop..."
        )

        success = repair_file(
            path=str(candidate_path),
            temp_req_path=temp_req_path,
            context_data=context_data,
            root_path=root_path,
            initial_result=pr,
            max_retries=3,
            passed_files=passed_files,
            failed_files=failed_files,
        )

        return success, context_data

    finally:
        # Remove candidate after success. Keep it after failure for diagnosis.
        if path in passed_files and candidate_path.exists():
            candidate_path.unlink()