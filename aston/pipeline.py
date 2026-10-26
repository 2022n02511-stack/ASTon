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


def run_for_repo(root_path: str, path: str, rules, symbols, relationships, dependencies, source_code, temp_req_path, passed_files: list, failed_files: list):
    
    # 1. Load JSON correctly as dictionaries
    with open('repo_index.json', 'r', encoding='utf-8') as f:
        repo_index = json.load(f)

    with open('migrations.json', 'r', encoding='utf-8') as f:
        migrations = json.load(f)

    run_id = uuid4().hex[:12]
    run_dir = Path("aston/artifacts") / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    rep_con = None
    repair_context_path = Path("repair_context.txt")
    if repair_context_path.exists():
        rep_con = repair_context_path.read_text()

    print(f"\n==================================================")
    print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    print(f"==================================================")

    all_rules: dict[str, list] = {}
    all_reports: dict[str, list] = {}
    all_cur_vers: dict[str, str] = {}
    all_tar_vers: dict[str, str] = {}
    incomplete_packages: list[str] = []

    # Iterate through the migrations dictionary/list depending on format
    # Assuming migrations is stored as a dict or iterable of module keys
    for module, mod_data in migrations.items():
        file_usages = repo_index.get("files", {}).get(path, {}).get("usages", [])
        stage0_symbols = stage0(module, file_usages)

        # FIXED: Use dynamic loop variable `module` instead of literal string 'module'
        dep_info = repo_index.get('dependencies', {}).get(module, {})
        current_version = dep_info.get('declared', {}).get('version', '0.0.0')
        target_version = dep_info.get('target_version', '1.0.0')

        report, relevant_rules, is_complete = run_with_animation(
            "Ingesting multi-boundary migration chain from GitHub...",
            scraper,
            module,
            current_version,
            target_version,
            stage0_symbols,
            file_usages
        )

        if not is_complete:
            incomplete_packages.append(module)

        all_rules[module] = relevant_rules
        all_reports[module] = report
        all_cur_vers[module] = current_version
        all_tar_vers[module] = target_version

    if incomplete_packages:
        print(
            f"⚠️ Proceeding with KNOWN-INCOMPLETE rule coverage for: "
            f"{', '.join(incomplete_packages)}"
        )

    print("\n========== RULES SENT TO PATCHER ==========")
    print(json.dumps(all_rules, indent=2, default=str))
    print("\n========== MATCHES SENT TO PATCHER ==========")
    print(json.dumps(all_reports, indent=2, default=str))

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
        return    

    original_source = None
    with open(path, 'r', encoding='utf-8') as f:
        original_source = f.read()

    file_slug = path.replace("/", "_").replace(".py", "")   
    file_run_dir = run_dir / file_slug
    file_run_dir.mkdir(parents=True, exist_ok=True) 

    save_code(file_run_dir / "original.py", original_source)

    MAX_RETRIES = 3
    retry_count = 0
    previous_attempts = []

    # Extract raw code examples cleanly from migrations dict
    raw_code_examples = []
    for module, mod_data in migrations.items():
        examples = mod_data.get('raw_code_examples', [])
        if isinstance(examples, list):
            raw_code_examples.extend(examples)

    factored_code = code_patcher(
        path,
        all_reports,
        validated_all_rules_dict,
        all_tar_vers,
        all_cur_vers,
        None,
        raw_code_examples=raw_code_examples,
    )

    save_code(file_run_dir / "first_attempt.py", factored_code)
    Path(path).write_text(factored_code, encoding="utf-8")

    pr = verify_single_file_in_docker(path, temp_req_path)

    while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < MAX_RETRIES:
        retry_count += 1
        failed_source = Path(pr.target_file).read_text(encoding="utf-8")

        previous_attempts.append({
            "attempt": retry_count,
            "source_code": failed_source,
            "error_type": pr.error_type,
            "error_message": pr.error_message,
            "traceback": pr.raw_stderr,
        })

        print(f"\n⚠️ Stage 4 Failed with {pr.error_type} (Attempt {retry_count}/{MAX_RETRIES})")
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

        factored_code = code_patcher(
            path,
            all_reports,
            validated_all_rules_dict,
            all_tar_vers,
            all_cur_vers,
            repair_context=repair_context,
            raw_code_examples=raw_code_examples,
        )

        if not factored_code or factored_code == failed_source:
            print("❌ Repair returned no new source. Stopping.")
            failed_files.append(path)
            break

        save_code(
            file_run_dir / f"repaired_attempt_{retry_count}.py",
            factored_code,
        )

        Path(path).write_text(factored_code, encoding="utf-8")
        pr = verify_single_file_in_docker(path, temp_req_path)

    if pr.status == VerificationResult.PASS:
        save_code(file_run_dir / "final.py", Path(path).read_text(encoding="utf-8"))
        migrated_path = Path("succ_migrations")
        target_migrated_file = migrated_path / path
        target_migrated_file.parent.mkdir(parents=True, exist_ok=True)
        print("\n✅ Sandboxed Verification Successful!")
        target_migrated_file.write_text(factored_code, encoding="utf-8")
        print(f"🎉 Code successfully patched and saved to {target_migrated_file}")
        passed_files.append(path)
    elif pr.status == VerificationResult.MIGRATION_ERROR:
        print(f"\n❌ Stage 5 stopped after {retry_count} repair attempts. Manual intervention required.")
        failed_files.append(path)
    else:
        print(f"\n⚠️ Pipeline halted due to verification status: {pr.status}")
        failed_files.append(path)