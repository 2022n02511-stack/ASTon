from aston.axios import code_patcher, stage0, scraper
from aston.docker import verify_single_file_in_docker, VerificationResult
from aston.watcher import watcher
from aston.scanner import parser
from pathlib import Path
import json
from aston.rule_validation import validate_rules_against_target, validate_all_rules, reconcile_report
from uuid import uuid4
from pathlib import Path
from aston.artifacts.save_attempt import save_code


def run_for_repo(path: str):
    # FIXED: previously read unconditionally — crashed on a clean checkout
    # with no repair history yet. Only needed once something is actually
    # in repair mode.

    run_id = uuid4().hex[:12]

    run_dir = Path("aston/artifacts") / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    rep_con = None
    repair_context_path = Path("repair_context.txt")
    if repair_context_path.exists():
        rep_con = repair_context_path.read_text()

    breaking_changes = watcher(path)

    if isinstance(breaking_changes, str):
        print(breaking_changes)
        return

    parses = parser()

    print("\n========== RAW PARSER OUTPUT ==========")
    for record in parses:
        if record.get("package") == "openai":
            print(record)

    print(f"\n==================================================")
    print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    print(f"==================================================")

    # FIXED: flattened from list-of-single-key-dicts to plain dicts keyed
    # by package name — same information, much easier to read in
    # changes.json / the patcher prompt, and no list-walking needed below.
    all_rules: dict[str, list] = {}
    all_reports: dict[str, list] = {}
    all_cur_vers: dict[str, str] = {}
    all_tar_vers: dict[str, str] = {}
    raw_code_examples: list = []
    incomplete_packages: list[str] = []

    for change in breaking_changes:
        stage0_symbols = stage0(change["pkg_name"], parses)

        report, relevant_rules, raw_code, doc_text, is_complete = scraper(
            change["pkg_name"],
            change["current_version"],
            change["target_version"],
            stage0_symbols,
            parses,  # FIXED: thread the existing parse through instead of
                     # letting man_extract() re-parse the file internally
        )

        if not is_complete:
            incomplete_packages.append(change["pkg_name"])

        all_rules[change["pkg_name"]] = relevant_rules
        all_reports[change["pkg_name"]] = report
        all_cur_vers[change["pkg_name"]] = change["current_version"]
        all_tar_vers[change["pkg_name"]] = change["target_version"]

        if isinstance(raw_code, list):
            raw_code_examples.extend(raw_code)

    # has_valid_rules = any(len(rules) > 0 for rules in all_rules.values())
    # has_valid_matches = any(len(matches) > 0 for matches in all_reports.values())

    # if not has_valid_rules or not has_valid_matches:
    #     print("⚠️ No migration rules or matching usages found. Skipping patch generation.")
    #     return

    # FIXED: completeness was computed in scraper() but never surfaced here —
    # pipeline proceeded regardless. Now at minimum it's visible before you
    # trust the result; tighten this to an early return once you trust the
    # validator enough to treat incomplete coverage as a hard stop.
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

    validated_rules = validate_all_rules(
        all_rules=flat_all_rules,
        all_target_versions=all_tar_vers,
        req_file=Path(path) / "requirements.txt",
    )

    final_report_dict = reconcile_report(
        extraction_report={"rules": validated_rules},
        validated_rules=validated_rules,
    )

    with open("changes.json", "w") as f:
        json.dump(final_report_dict, f, indent=2)
    print("📁 Canonical audit contract saved to changes.json")

    # 4. Rebuild the package-keyed dictionary with ALL validated rules (keeping statuses)
    validated_all_rules_dict = {}
    for rule in validated_rules:
        pkg = rule.get("package")
        if pkg not in validated_all_rules_dict:
            validated_all_rules_dict[pkg] = []
        validated_all_rules_dict[pkg].append(rule)

    has_valid_rules = any(len(rules) > 0 for rules in validated_all_rules_dict.values())
    has_valid_matches = any(len(matches) > 0 for matches in all_reports.values())

    if not has_valid_rules or not has_valid_matches:
        print("⚠️ No migration rules or matching usages found. Skipping patch generation.")
        return    

    print("\n========== ALL VALIDATED RULES (WITH STATUSES) SENT TO PATCHER ==========")
    print(json.dumps(validated_all_rules_dict, indent=2, default=str))

    original_source = None

    with open('before.py', 'r') as f:
        original_source = f.read()

    save_code(run_dir / "original.py", original_source)

    MAX_RETRIES = 3
    retry_count = 0
    previous_attempts = []

    factored_code = code_patcher(
        "before.py",
        all_reports,
        validated_all_rules_dict,
        all_tar_vers,
        all_cur_vers,
        None,
        raw_code_examples=raw_code_examples,
    )

    save_code(run_dir / "first_attempt.py", factored_code)


    temp_req_path = Path("temp_requirements.txt")
    original_req_path = Path(path) / "requirements.txt"
    
    # 1. Start with everything already in the project's requirements.txt
    req_lines = original_req_path.read_text().splitlines() if original_req_path.exists() else []
    
    # 2. Ensure each target breaking change package and version is updated/added
    for change in breaking_changes:
        pkg = change["pkg_name"]
        target_ver = change["target_version"]
        new_req_line = f"{pkg}=={target_ver}"
        
        found = False
        for i, line in enumerate(req_lines):
            cleaned = line.strip().lower()
            if cleaned == pkg.lower() or cleaned.startswith(f"{pkg.lower()}==") or cleaned.startswith(f"{pkg.lower()}>="):
                req_lines[i] = new_req_line
                found = True
                break
        if not found:
            req_lines.append(new_req_line)
            
    # 3. Write out the fully populated dependencies list ONCE (fixed indentation)
    temp_req_path.write_text("\n".join(req_lines) + "\n")

    pr = verify_single_file_in_docker("before.py", temp_req_path)

    while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < MAX_RETRIES:
        retry_count += 1


        failed_source = Path(pr.target_file).read_text()

        # save_code(
        #         run_dir / f"repaired_attempt_{retry_count}.py",
        #         failed_source,
        # )

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
            "before.py",
            all_reports,
            validated_all_rules_dict,  # ✅ Fixed: pass validated rules here too!
            all_tar_vers,
            all_cur_vers,
            repair_context=repair_context,
            raw_code_examples=raw_code_examples,
        )

        if not factored_code or factored_code == failed_source:
            print("❌ Repair returned no new source. Stopping.")
            break

# Preserve the output of this repair attempt.
        save_code(
            run_dir / f"repaired_attempt_{retry_count}.py",
            factored_code,
        )

        Path("before.py").write_text(factored_code, encoding="utf-8")


        pr = verify_single_file_in_docker("before.py", temp_req_path)

    if pr.status == VerificationResult.PASS:
        save_code(run_dir / "final.py", Path("before.py").read_text())
        print("\n✅ Sandboxed Verification Successful!")
        yes_no = input("Do you wanna patch it? [Y/n]: ").strip().lower()
        if yes_no in ["y", "yes", ""]:
            Path("sample.py").write_text(factored_code)
            print("🎉 Code successfully patched and saved to sample.py")
        else:
            print("Exit without applying patch.")
    elif pr.status == VerificationResult.MIGRATION_ERROR:
        print(f"\n❌ Stage 5 stopped after {retry_count} repair attempts. Manual intervention required.")
    else:
        print(f"\n⚠️ Pipeline halted due to verification status: {pr.status}")