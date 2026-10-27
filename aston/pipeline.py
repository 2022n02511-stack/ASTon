from aston.axios import code_patcher, stage0, scraper
from aston.docker import verify_single_file_in_docker, VerificationResult
from aston.watcher import watcher
from aston.scanner import parser
from pathlib import Path
import json
import os
from uuid import uuid4
from aston.rule_validation import validate_rules_against_target, validate_all_rules, reconcile_report
from aston.artifacts.save_attempt import save_code
from aston.cli_ui.rich import print_rich_aston_banner, run_with_animation, print_aston_banner
from aston.repo_index import repo_map


def _safe_read_text(path: Path) -> str:
    """Read UTF-8 source text with a clear error if the file is unavailable."""
    return path.read_text(encoding="utf-8")


def _write_candidate(path: Path, source: str) -> bool:
    """Write a candidate only when it contains non-empty source code."""
    if not isinstance(source, str) or not source.strip():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return True


def _candidate_path(path: str, run_id: str) -> Path:
    """Create a temporary candidate path next to the source file."""
    source_path = Path(path)
    return source_path.with_name(
        f".{source_path.stem}.aston_candidate_{run_id}{source_path.suffix or '.py'}"
    )


def _record_failure(path: str, failed_files: list) -> None:
    if path not in failed_files:
        failed_files.append(path)


def _record_success(path: str, passed_files: list, failed_files: list) -> None:
    if path in failed_files:
        failed_files.remove(path)
    if path not in passed_files:
        passed_files.append(path)


def _save_successful_migration(path: str, source_path: Path, file_run_dir: Path) -> None:
    """Save verified source and copy it to succ_migrations without changing source paths."""
    verified_code = _safe_read_text(source_path)
    save_code(file_run_dir / "final.py", verified_code)
    migrated_path = Path("succ_migrations") / path
    migrated_path.parent.mkdir(parents=True, exist_ok=True)
    migrated_path.write_text(verified_code, encoding="utf-8")
    print(f"🎉 Code successfully patched and saved to {migrated_path}")


def repair_file(
    path: str,
    temp_req_path: Path,
    context_data: dict,
    root_path: str,
    max_retries: int = 2,
    passed_files: list = None,
    failed_files: list = None
):
    """Run the self-healing retry loop against a candidate, preserving the original source."""
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

    pr = verify_single_file_in_docker(
        str(candidate_path), temp_req_path, repo_root=root_path
    )

    # Change this line in repair_file:
    # while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < max_retries:

    # To this:
    while pr.status != VerificationResult.PASS and retry_count < max_retries:
        retry_count += 1
        try:
            failed_source = _safe_read_text(Path(pr.target_file or candidate_path))
        except (OSError, UnicodeError):
            failed_source = _safe_read_text(candidate_path)

        previous_attempts.append({
            "attempt": retry_count,
            "source_code": failed_source,
            "error_type": pr.error_type,
            "error_message": pr.error_message,
            "traceback": pr.raw_stderr,
        })

        print(
            f"\n⚠️ Stage 4 Failed with {pr.error_type} "
            f"(Repair Attempt {retry_count}/{max_retries})"
        )
        print("Routing to Stage 5 Self-Healing Repair Loop...")

        repair_context = {
            "file_name": str(candidate_path),
            "error_type": pr.error_type,
            "error_message": pr.error_message,
            "traceback": pr.raw_stderr,
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
            print("❌ Repair returned empty or invalid source. Stopping.")
            _record_failure(display_path, failed_files)
            return False

        if factored_code == failed_source:
            print("❌ Repair returned unchanged source. Stopping.")
            _record_failure(display_path, failed_files)
            return False

        repaired_attempt_path = file_run_dir / f"repaired_attempt_{retry_count}.py"
        save_code(repaired_attempt_path, factored_code)

        if not _write_candidate(candidate_path, factored_code):
            print("❌ Could not write repaired candidate. Stopping.")
            _record_failure(display_path, failed_files)
            return False

        pr = verify_single_file_in_docker(
            str(candidate_path), temp_req_path, repo_root=root_path
        )

    if pr.status == VerificationResult.PASS:
        # Robustly locate the verified source file
        resolved_candidate = candidate_path
        if not resolved_candidate.exists() and pr.target_file:
            resolved_candidate = Path(pr.target_file)
        
        verified_code = _safe_read_text(resolved_candidate) if resolved_candidate.exists() else _safe_read_text(original_path)
        
        original_path.write_text(verified_code, encoding="utf-8")
        _save_successful_migration(display_path, original_path, file_run_dir)
        _record_success(display_path, passed_files, failed_files)
        print("\n✅ Sandboxed Verification Successful!")
        return True

    print(
        f"\n❌ Verification stopped after {retry_count} repair attempts. "
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
    failed_files: list
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

    # Item 12: Run Stage 0 and Stage 1 once globally per package across the entire repo index
    for module, mod_data in migrations.items():
        stage0_symbols = stage0(module, repo_index)

        dep_info = repo_index.get("dependencies", {}).get(module, {})
        declared_version = dep_info.get("declared", {}).get("version")
        target_version = dep_info.get("target_version")

        # Item 14: Enforce strict version validation rather than silent fallbacks
        if not declared_version or not target_version:
            raise ValueError(
                f"Missing version specification for package '{module}': "
                f"declared={declared_version!r}, target={target_version!r}"
            )

        current_version = declared_version
        target_version = target_version

        composed_rules = mod_data.get("composed_rules", [])
        doc_text = mod_data.get("doc_text", "")

        # For global file usages collection across the repo
        file_usages = []
        for file_data in repo_index.get("files", {}).values():
            if isinstance(file_data, dict):
                for usage in file_data.get("usages", []):
                    if usage.get("package") == module:
                        file_usages.append(usage)

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
        pkg: version for pkg, version in all_tar_vers.items()
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
            print("⚠️ Target versions are unknown; skipping target-environment batch validation.")

    final_report_dict = reconcile_report(
        extraction_report={"rules": validated_rules},
        validated_rules=validated_rules,
    )

    flat_filename = path.replace("/", "_")
    output_path = Path("final_reports") / f"{Path(flat_filename).stem}_changes.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report_dict, f, indent=3)
    print(f"📁 Canonical audit contract saved to {output_path}")

    validated_all_rules_dict = {}
    for rule in validated_rules:
        pkg = rule.get("package")
        if not pkg:
            continue
        validated_all_rules_dict.setdefault(pkg, []).append(rule)

    has_valid_rules = any(bool(r) for r in validated_all_rules_dict.values())
    has_valid_matches = any(bool(m) for m in all_reports.values())

    # Item 15: Handle "Nothing found" cleanly as an unresolved/no-evidence outcome
    if not has_valid_rules or not has_valid_matches:
        print("ℹ️ No migration rules or matching usages found for this file. Skipping patch generation (unresolved/no evidence).")
        _record_success(path, passed_files, failed_files) if path not in failed_files else None
        return True, {}

    file_slug = path.replace("/", "_").replace(".py", "")
    file_run_dir = run_dir / file_slug
    file_run_dir.mkdir(parents=True, exist_ok=True)

    save_code(file_run_dir / "original.py", source_code)

    raw_code_examples = []
    for module, mod_data in migrations.items():
        examples = mod_data.get("raw_code_examples", [])
        if isinstance(examples, list):
            raw_code_examples.extend(examples)

    original_path = Path(path)
    candidate_path = _candidate_path(path, run_id)
    if not _write_candidate(candidate_path, source_code):
        print("❌ Original source input is empty or invalid; refusing to patch.")
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
            print("❌ Patcher returned empty or invalid source. Original source preserved.")
            _record_failure(path, failed_files)
            return False, {}

        if factored_code == source_code:
            print("⚠️ Patcher returned unchanged source. Skipping verification.")
            _record_success(path, passed_files, failed_files)
            return True, {}

        first_attempt_path = file_run_dir / "first_attempt.py"
        save_code(first_attempt_path, factored_code)
        if not _write_candidate(candidate_path, factored_code):
            print("❌ Could not write candidate source. Original source preserved.")
            _record_failure(path, failed_files)
            return False, {}

        pr = verify_single_file_in_docker(
            str(candidate_path), temp_req_path, repo_root=root_path
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
        }

        if pr.status == VerificationResult.PASS:
            original_path.write_text(
                _safe_read_text(candidate_path), encoding="utf-8"
            )
            _save_successful_migration(path, original_path, file_run_dir)
            _record_success(path, passed_files, failed_files)
            print("\n✅ Sandboxed Verification Successful!")
            return True, context_data

        print(
            f"\n⚠️ Initial verification failed for {path}. "
            "Starting repair loop (max 3 retries)..."
        )
        context_data["repair_instructions"] = None
        success = repair_file(
            path=str(candidate_path),
            temp_req_path=temp_req_path,
            context_data=context_data,
            root_path=root_path,
            max_retries=3,
            passed_files=passed_files,
            failed_files=failed_files,
        )
        return success, context_data

    finally:
        if path in passed_files and candidate_path.exists():
            candidate_path.unlink()