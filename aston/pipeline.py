# sma/pipeline.py
from aston.axios import code_patcher, stage0, scraper
from aston.docker import verify_single_file_in_docker, VerificationResult
from aston.watcher import watcher
from aston.scanner import parser
from pathlib import Path
import json


def run_for_repo(path: str):
    rep_con = None

    with open("repair_context.txt", "r") as f:
        rep_con = f.read()

    breaking_changes = watcher(path)

    if isinstance(breaking_changes, str):
        print(breaking_changes)
        return

    parse = parser()

    print(f"\n==================================================")
    print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    print(f"==================================================")
    parses = parse

    all_rules = []
    all_reports = []
    all_cur_vers = []
    all_tar_vers = []
    raw_code_examples = []

    for change in breaking_changes:
        stage0_symbols = stage0(change["pkg_name"], parses)

        report, relevant_rules, raw_code, doc_text = scraper(
            change["pkg_name"],
            change["current_version"],
            change["target_version"],
            stage0_symbols,
        )

        # ---------------------------------------------------------
        # BYPASSING VALIDATOR: Passing extracted rules directly
        # ---------------------------------------------------------
        all_rules.append({change["pkg_name"]: relevant_rules})
        all_reports.append({change["pkg_name"]: report})
        all_cur_vers.append({change["pkg_name"]: change["current_version"]})
        all_tar_vers.append({change["pkg_name"]: change["target_version"]})
        
        if isinstance(raw_code, list):
            raw_code_examples.extend(raw_code)

    has_valid_rules = any(
        isinstance(group, dict)
        and any(
            isinstance(rule_list, list) and len(rule_list) > 0
            for rule_list in group.values()
        )
        for group in all_rules
    )

    has_valid_matches = any(
        isinstance(group, dict)
        and any(
            isinstance(match_list, list) and len(match_list) > 0
            for match_list in group.values()
        )
        for group in all_reports
    )

    if not has_valid_rules or not has_valid_matches:
        print(
            "⚠️ No migration rules or matching usages found. "
            "Skipping patch generation."
        )
        return

    print("\n========== RULES SENT TO PATCHER ==========")
    print(json.dumps(all_rules, indent=2, default=str))
    print("\n========== MATCHES SENT TO PATCHER ==========")
    print(json.dumps(all_reports, indent=2, default=str))

    factored_code = code_patcher(
        "before.py",
        all_reports,
        all_rules,
        all_tar_vers,
        all_cur_vers,
        None,
        raw_code_examples=raw_code_examples,
    )

    if factored_code:
        MAX_RETRIES = 3
        retry_count = 0
        previous_attempts = []


        temp_req_path = Path("temp_requirements.txt")
        req_lines = [
            f"{change['pkg_name']}=={change['target_version']}"
            for change in breaking_changes
        ]
        temp_req_path.write_text("\n".join(req_lines) + "\n")
        
        pr = verify_single_file_in_docker("before.py", temp_req_path)

        while (
            pr.status == VerificationResult.MIGRATION_ERROR
            and retry_count < MAX_RETRIES
        ):
            retry_count += 1

            failed_source = Path(pr.target_file).read_text()
            previous_attempts.append({
                "attempt": retry_count,
                "source_code": failed_source,
                "error_type": pr.error_type,
                "error_message": pr.error_message,
                "traceback": pr.raw_stderr,
            })

            print(
                f"\n⚠️ Stage 4 Failed with {pr.error_type} "
                f"(Attempt {retry_count}/{MAX_RETRIES})"
            )
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
                all_rules,
                all_tar_vers,
                all_cur_vers,
                repair_context=repair_context,
                raw_code_examples=raw_code_examples,
            )

            current_source = Path("before.py").read_text()
            if current_source == failed_source:
                print("❌ Repair produced no new source. Stopping.")
                break

            pr = verify_single_file_in_docker("before.py", temp_req_path)

        if pr.status == VerificationResult.PASS:
            print("\n✅ Sandboxed Verification Successful!")
            yes_no = input("Do you wanna patch it? [Y/n]: ").strip().lower()

            if yes_no in ["y", "yes", ""]:
                Path("sample.py").write_text(factored_code)
                print("🎉 Code successfully patched and saved to sample.py")
            else:
                print("Exit without applying patch.")

        elif pr.status == VerificationResult.MIGRATION_ERROR:
            print(
                f"\n❌ Stage 5 stopped after {retry_count} repair attempts. "
                "Manual intervention required."
            )
        else:
            print(f"\n⚠️ Pipeline halted due to verification status: {pr.status}")