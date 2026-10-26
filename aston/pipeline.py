from aston.axios import code_patcher, stage0, scraper
from aston.docker import verify_single_file_in_docker, VerificationResult
from aston.watcher import watcher
from aston.scanner import parser
from pathlib import Path
import json
from aston.rule_validation import validate_rules_against_target, validate_all_rules, reconcile_report
from uuid import uuid4
from aston.artifacts.save_attempt import save_code
from aston.cli_ui.rich import print_rich_aston_banner, run_with_animation, print_aston_banner
from aston.repo_index import repo_map

def repair_file(
    path: str,
    temp_req_path: Path,
    context_data: dict,
    root_path: str,
    max_retries: int = 2,
    passed_files: list = None,
    failed_files: list = None
):
    """Runs the self-healing retry repair loop using pre-computed context data."""
    if passed_files is None:
        passed_files = []
    if failed_files is None:
        failed_files = []

    retry_count = 0
    previous_attempts = []

    file_run_dir = context_data["file_run_dir"]
    all_reports = context_data["all_reports"]
    validated_all_rules_dict = context_data["validated_all_rules_dict"]
    all_tar_vers = context_data["all_tar_vers"]
    all_cur_vers = context_data["all_cur_vers"]
    raw_code_examples = context_data["raw_code_examples"]
    
    # Required parameters for code_patcher
    rules = context_data["rules"]
    symbols = context_data["symbols"]
    relationships = context_data["relationships"]
    dependencies = context_data["dependencies"]
    source_code = context_data["source_code"]

    rep_con = None
    repair_context_path = Path("repair_context.txt")
    if repair_context_path.exists():
        rep_con = repair_context_path.read_text(encoding="utf-8")

    pr = verify_single_file_in_docker(path, temp_req_path, repo_root=root_path)

    while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < max_retries:
        retry_count += 1
        failed_source = Path(pr.target_file).read_text(encoding="utf-8")

        previous_attempts.append({
            "attempt": retry_count,
            "source_code": failed_source,
            "error_type": pr.error_type,
            "error_message": pr.error_message,
            "traceback": pr.raw_stderr,
        })

        print(f"\n⚠️ Stage 4 Failed with {pr.error_type} (Repair Attempt {retry_count}/{max_retries})")
        print("Routing to Stage 5 Self-Healing Repair Loop...")

        repair_context = {
            "file_name": pr.target_file,
            "error_type": pr.error_type,
            "error_message": pr.error_message,
            "traceback": pr.raw_stderr,
            "attempted_code": failed_source,
            "previous_attempts": previous_attempts,
            "repair_instructions": rep_con,
        }

        # Update source code in context
        context_data["source_code"] = failed_source

        # Explicitly passing rules, symbols, relationships, dependencies, source_code
        factored_code = code_patcher(
            source_code=failed_source,
            matches=all_reports,
            rules=validated_all_rules_dict,
            target_version=all_tar_vers,
            current_version=all_cur_vers,
            # Explicitly requested parameters:
            repo_rules=rules,
            symbols=symbols,
            relationships=relationships,
            dependencies=dependencies,
            repair_context=repair_context,
            raw_code_examples=raw_code_examples
        )

        if not factored_code or factored_code == failed_source:
            print("❌ Repair returned no new source. Stopping.")
            if path not in failed_files:
                failed_files.append(path)
            return False

        save_code(
            file_run_dir / f"repaired_attempt_{retry_count}.py",
            factored_code,
        )

        Path(path).write_text(factored_code, encoding="utf-8")
        pr = verify_single_file_in_docker(path, temp_req_path, repo_root=root_path)

    if pr.status == VerificationResult.PASS:
        save_code(file_run_dir / "final.py", Path(path).read_text(encoding="utf-8"))
        migrated_path = Path("succ_migrations")
        target_migrated_file = migrated_path / path
        target_migrated_file.parent.mkdir(parents=True, exist_ok=True)
        print("\n✅ Sandboxed Verification Successful!")
        target_migrated_file.write_text(Path(path).read_text(encoding="utf-8"), encoding="utf-8")
        print(f"🎉 Code successfully patched and saved to {target_migrated_file}")
        if path in failed_files:
            failed_files.remove(path)
        if path not in passed_files:
            passed_files.append(path)
        return True
    else:
        print(f"\n❌ Stage 5 stopped after {retry_count} repair attempts. Manual intervention required.")
        if path not in failed_files:
            failed_files.append(path)
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
    with open('repo_index.json', 'r', encoding='utf-8') as f:
        repo_index = json.load(f)

    with open('migrations.json', 'r', encoding='utf-8') as f:
        migrations = json.load(f)

    run_id = uuid4().hex[:12]
    run_dir = Path("aston/artifacts") / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n==================================================")
    print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    print(f"==================================================")

    all_rules: dict[str, list] = {}
    all_reports: dict[str, list] = {}
    all_cur_vers: dict[str, str] = {}
    all_tar_vers: dict[str, str] = {}
    incomplete_packages: list[str] = []

    for module, mod_data in migrations.items():
        file_usages = repo_index.get("files", {}).get(path, {}).get("usages", [])
        stage0_symbols = stage0(module, file_usages)

        dep_info = repo_index.get('dependencies', {}).get(module, {})
        current_version = dep_info.get('declared', {}).get('version', '0.0.0')
        target_version = dep_info.get('target_version', '1.0.0')

        composed_rules = mod_data.get('composed_rules', [])
        doc_text = mod_data.get('doc_text', '')

        report, relevant_rules, is_complete = run_with_animation(
            "Evaluating relevance and completeness against pre-fetched rules...",
            scraper,
            module,
            current_version,
            target_version,
            stage0_symbols,
            file_usages,
            composed_rules,
            doc_text,
            path
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

    validated_rules = run_with_animation(
        "Running sandboxed batch validation across target environments...",
        validate_all_rules,
        all_rules=flat_all_rules,
        all_target_versions=all_tar_vers,
        req_file=Path(root_path) / "requirements.txt",
    )

    final_report_dict = reconcile_report(
        extraction_report={"rules": validated_rules},
        validated_rules=validated_rules,
    )

    flat_filename = path.replace("/", "_")
    output_path = Path("final_reports") / f"{Path(flat_filename).stem}_changes.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report_dict, f, indent=3)
        print("📁 Canonical audit contract saved to changes.json")

    validated_all_rules_dict = {}
    for rule in validated_rules:
        pkg = rule.get("package")
        if pkg not in validated_all_rules_dict:
            validated_all_rules_dict[pkg] = []
        validated_all_rules_dict[pkg].append(rule)

    has_valid_rules = any(len(r) > 0 for r in validated_all_rules_dict.values())
    has_valid_matches = any(len(m) > 0 for m in all_reports.values())

    if not has_valid_rules or not has_valid_matches:
        print("⚠️ No migration rules or matching usages found. Skipping patch generation.")
        if path not in failed_files:
            failed_files.append(path)
        return False, {}    

    file_slug = path.replace("/", "_").replace(".py", "")   
    file_run_dir = run_dir / file_slug
    file_run_dir.mkdir(parents=True, exist_ok=True) 

    save_code(file_run_dir / "original.py", source_code)

    raw_code_examples = []
    for module, mod_data in migrations.items():
        examples = mod_data.get('raw_code_examples', [])
        if isinstance(examples, list):
            raw_code_examples.extend(examples)

    # First code generation attempt — explicitly passing rules, symbols, relationships, dependencies, source_code
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

    save_code(file_run_dir / "first_attempt.py", factored_code)
    Path(path).write_text(factored_code, encoding="utf-8")

    # Store everything including source_code in context_data for repair_file
    # Initial verification & delegation to repair loop if needed
    pr = verify_single_file_in_docker(path, temp_req_path, repo_root=root_path)

    # Store everything including source_code in context_data for repair_file
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
    }

    if pr.status == VerificationResult.PASS:
        save_code(file_run_dir / "final.py", Path(path).read_text(encoding="utf-8"))
        migrated_path = Path("succ_migrations")
        target_migrated_file = migrated_path / path
        target_migrated_file.parent.mkdir(parents=True, exist_ok=True)
        target_migrated_file.write_text(factored_code, encoding="utf-8")
        if path not in passed_files:
            passed_files.append(path)
        return True, context_data
    else:
        # Hand off to repair function with max 3 retries during the initial pass
        print(f"\n⚠️ Initial verification failed for {path}. Starting initial repair loop (max 3 retries)...")
        success = repair_file(
            path=path,
            temp_req_path=temp_req_path,
            context_data=context_data,
            root_path=root_path,
            max_retries=3,
            passed_files=passed_files,
            failed_files=failed_files
        )
        return success, context_data