# sma/pipeline.py
from aston.axios import run_chain_migration, code_patcher
from aston.docker import verify_single_file_in_docker, VerificationResult


def run_for_repo():

    fixed_code = None
    report = None
    rules = None
    target_version = None

    print("Starting ASTon...")

    fixed_code, report, rules, target_version = run_chain_migration('openai')


    if fixed_code:
        MAX_RETRIES = 3
        retry_count = 0

        # Execute Stage 4 Sandboxed Docker Verification
        pr = verify_single_file_in_docker(target_file="before.py", package_req="openai>=1.0.0")

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
            }

            # Re-patch code using error context
            fixed_code = code_patcher(
                'before.py', 
                report, 
                rules, 
                target_version, 
                repair_context=repair_context
            )

            # Re-verify in Docker sandbox
            pr = verify_single_file_in_docker(target_file="before.py", package_req="openai>=1.0.0")

        # --- Final Decision & User Prompt ---
        if pr.status == VerificationResult.PASS:
            print("\n✅ Sandboxed Verification Successful!")
            yes_no = input("Do you wanna patch it? [Y/n]: ").strip().lower()
            
            if yes_no in ['y', 'yes', '']:
                with open("sample.py", 'w') as f:
                    f.write(fixed_code)
                print("🎉 Code successfully patched and saved to sample.py")
            else:
                print("Exit without applying patch.")

        elif pr.status == VerificationResult.MIGRATION_ERROR:
            print(f"\n❌ Stage 5 Repair Loop exhausted after {MAX_RETRIES} attempts. Manual intervention required.")

        else:
            print(f"\n⚠️ Pipeline halted due to verification status: {pr.status}")
