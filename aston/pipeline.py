# sma/pipeline.py
from aston.axios import code_patcher, stage0, scraper
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

    for change in breaking_changes:

        stage0_symbols = stage0(change["pkg_name"], parses)
        report, relevant_rules = scraper(change["pkg_name"], change["current_version"], change["target_version"], stage0_symbols)

        all_rules.append({
            change["pkg_name"] : relevant_rules
        })

        all_reports.append({
            change["pkg_name"] : report
        })

        all_cur_vers.append({
            change["pkg_name"] : change["current_version"]
        })

        all_tar_vers.append({
            change["pkg_name"] : change["target_version"]
        })


    # folder_path = Path()
    # source_code = None

    # with open('/demo/sample.py', "r") as f:
    #     source_code = f.read()

    factored_code = code_patcher('before.py', all_reports, all_rules, all_tar_vers, all_cur_vers)



        

    # fixed_code = None
    # report = None
    # rules = None
    # target_version = None

    # print("Starting ASTon...")

    # fixed_code, report, rules, target_version = run_chain_migration('openai')


    if factored_code:
        MAX_RETRIES = 3
        retry_count = 0

        # Execute Stage 4 Sandboxed Docker Verification
        pr = verify_single_file_in_docker(target_file="before.py", req_file = str(Path(path) / "requirements.txt"))

        # --- Stage 5: Self-Healing Repair Loop ---
        while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < MAX_RETRIES:
            retry_count += 1
            print(f"\n⚠️ Stage 4 Failed with {pr.error_type} (Attempt {retry_count}/{MAX_RETRIES})")
            print("Routing to Stage 5 Self-Healing Repair Loop...")

            repair_context = {
                "file_name": pr.target_file,
                "error_type": pr.error_type,
                "error_message": pr.error_message,
                "traceback": pr.raw_stderr,
                "attempted_code": open(pr.target_file).read()
                "repair_context_as_system_prompt" : rep_con
            }

            # Re-patch code using error context
            factored_code = code_patcher(
                'before.py', 
                all_reports, 
                all_rules, 
                all_tar_vers,
                all_cur_vers, 
                repair_context=repair_context
            )

            # Re-verify in Docker sandbox
            pr = verify_single_file_in_docker(target_file="before.py", req_file=str(Path(path) / "requirements.txt"))

        # --- Final Decision & User Prompt ---
        if pr.status == VerificationResult.PASS:
            print("\n✅ Sandboxed Verification Successful!")
            yes_no = input("Do you wanna patch it? [Y/n]: ").strip().lower()
            
            if yes_no in ['y', 'yes', '']:
                with open("sample.py", 'w') as f:
                    f.write(factored_code)
                print("🎉 Code successfully patched and saved to sample.py")
            else:
                print("Exit without applying patch.")

        elif pr.status == VerificationResult.MIGRATION_ERROR:
            print(f"\n❌ Stage 5 Repair Loop exhausted after {MAX_RETRIES} attempts. Manual intervention required.")

        else:
            print(f"\n⚠️ Pipeline halted due to verification status: {pr.status}")
