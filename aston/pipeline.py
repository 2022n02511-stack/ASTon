# sma/pipeline.py
from aston.axios import code_patcher, stage0, scraper, validate_and_normalize_rules
from aston.docker import verify_single_file_in_docker, VerificationResult
from aston.watcher import watcher
from aston.scanner import parser
from pathlib import Path


def run_for_repo(path : str):

    rep_con = None

    with open('repair_context.txt', 'r') as f:
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
        report, relevant_rules, raw_code = scraper(change["pkg_name"], change["current_version"], change["target_version"], stage0_symbols)

        validated_rules = validate_and_normalize_rules(
        package=change["pkg_name"],
        current_version=change["current_version"],
        target_version=change["target_version"],
        rules=relevant_rules,
        raw_code_examples=raw_code,
    )

    all_rules.append({
        change["pkg_name"]: validated_rules
    })
    all_reports.append({
        change["pkg_name"]: report
    })
    all_cur_vers.append({
        change["pkg_name"]: change["current_version"]
    })
    all_tar_vers.append({
        change["pkg_name"]: change["target_version"]
    })
    raw_code_examples.append(raw_code)


    # folder_path = Path()
    # source_code = None

    # with open('/demo/sample.py', "r") as f:
    #     source_code = f.read()

    # raw_code_examples = MigrationRuleset.raw_example_blocks

    factored_code = code_patcher('before.py', all_reports, all_rules, all_tar_vers, all_cur_vers, None, raw_code_examples=raw_code_examples)



        

    # fixed_code = None
    # report = None
    # rules = None
    # target_version = None

    # print("Starting ASTon...")

    # fixed_code, report, rules, target_version = run_chain_migration('openai')



    if factored_code:
        MAX_RETRIES = 3
        retry_count = 0
        previous_attempts = []

        pr = verify_single_file_in_docker(
            target_file="before.py",
            req_file=str(Path(path) / "requirements.txt"),
        )

        while (
            pr.status == VerificationResult.MIGRATION_ERROR
            and retry_count < MAX_RETRIES
        ):
            retry_count += 1

            # Capture the exact source that just failed verification.
            failed_source = Path(pr.target_file).read_text()

            failed_attempt = {
                "attempt": retry_count,
                "source_code": failed_source,
                "error_type": pr.error_type,
                "error_message": pr.error_message,
                "traceback": pr.raw_stderr,
            }
            previous_attempts.append(failed_attempt)

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

            # Stop if the patcher did not produce a new candidate.
            current_source = Path("before.py").read_text()
            if current_source == failed_source:
                print(
                    "❌ Repair produced no new source. "
                    "Stopping to avoid repeating the same failure."
                )
                break

            pr = verify_single_file_in_docker(
                target_file="before.py",
                req_file=str(Path(path) / "requirements.txt"),
            )

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
