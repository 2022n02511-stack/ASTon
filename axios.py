import json
from main import parser
from pathlib import Path
import requests
import os
from google import genai
from dotenv import load_dotenv
from ingestion import *
from relevance import evaluate_relevance_and_completeness
from docker import verify_single_file_in_docker, VerificationResult

load_dotenv()

# API_KEY = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY")
API_KEY = os.environ.get("GEMINI_API_KEY_1")

client = genai.Client(api_key=API_KEY)

prompt = None
report = None
rules = None

with open('patcher_prompt.txt', 'r') as f:
    prompt = f.read();

def man_extract(rules: list):
    """Maps extracted LLM rules against actual AST dynamic parser output."""
    extracted = {}
    for rule in rules:
        extracted[(rule['package'], rule['old_api'])] = {
            "new_api": rule['new_api'],
            "requires_instantiation": rule.get('requires_instantiation')
        }

    parses = parser()
    pexs = []
    liner = {}

    for parse in parses:
        pexs.append((parse['package'], parse['api_usage']))
        liner[(parse['package'], parse['api_usage'])] = parse['line']

    report = []
    path = 'before.py'
    filename = Path(path).name

    for item in pexs:
        if item in extracted:
            report.append({
                'file_name': filename,
                "Line": liner[item],
                "package": item[0],
                "detected_usage": item[1],
                "change": extracted[item]
            })

    return report        

def code_patcher(source_code: str, matches: list, rules: list, targt_version, repair_context : dict | None = None):
    """
    Stage 3 AST/LLM Code Patcher.
    ISOLATION GUARANTEE: If rules or matches are empty, strictly return original code!
    """
    with open(source_code, 'r') as f:
        code = f.read()

    if not rules or not matches:
        print("⚠️ Stage 3 Isolation Enforcement: No valid rules/matches provided. Retaining original code unmodified.")
        return code

    cont = f"""

    system_prompt: {prompt}

    
    MIGRATION_RULES: {json.dumps(rules, indent=2)}
    MATCHES/AFFECTED_USAGES: {json.dumps(matches, indent=2)}

    TARGET_DEPENDENCY_VERSION: {targt_version}

    REPAIR_CONTEXT: {repair_context}
    
    ORIGINAL SOURCE CODE:
    ```python
    {code}
    ```

    Output ONLY the refactored Python code inside ```python fences.
    """

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=cont
    )    

    raw_text = response.text
    patched = raw_text.split("```python")[1].split("```")[0].strip() if "```python" in raw_text else raw_text.strip()

    with open('before.py', 'w') as f:
        f.write(patched)

    return patched

def run_single_step_migration(package_name: str, target_major_tag: str):
    """
    Runs an isolated migration step for a single target major version (e.g. v1.0.0).
    """
    print(f"\n==================================================")
    print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    print(f"==================================================")
    parses = parser()
    dynamic_stage0_symbols = {parse['api_usage'] for parse in parses if parse['package'] == package_name}
    print(f" Dynamic AST Scanned Usages: {dynamic_stage0_symbols}")

    print(f"\n==================================================")
    print(f"--- [Stage 1] Stepwise Ingestion for {target_major_tag} ---")
    print(f"==================================================")
    repo = resolve_github_repo(package_name)
    if not repo:
        print(" Failed to resolve GitHub repository from PyPI.")
        return

    doc_text = find_migration_source_for_step(package_name, repo, target_major_tag)

    if not doc_text or len(doc_text.strip()) < 30:
        print(" Warning: Migration documentation source is empty or insufficient!")
        return

    print("\n--- CANONICAL MIGRATION SOURCE DIAGNOSTIC ---")
    for symbol in dynamic_stage0_symbols:
        short_name = symbol.split(".")[-1]
        present = symbol.lower() in doc_text.lower() or short_name.lower() in doc_text.lower()
        print(f" Diagnostic check for symbol '{symbol}': {present}")

    print("\n--- [Stage 1] Executing LLM Change Record Extraction ---")
    raw_rules = extract_rules(doc_text)
    print(f" Extracted raw rules from LLM: {raw_rules}")

    print("\n==================================================")
    print("--- [Stage 2] Non-Circular Relevance & Completeness Audit ---")
    print("==================================================")
    audit = evaluate_relevance_and_completeness(dynamic_stage0_symbols, raw_rules, doc_text)
    
    print(f" Expected Affected Symbols: {audit['expected_affected_symbols']}")
    print(f" Missing Symbols: {audit['missing_symbols']}")
    
    if not audit["is_complete"]:
        print(f"🚨 WARNING: Incomplete rule extraction detected! Missing: {audit['missing_symbols']}")
    else:
        print("✅ Extraction Completeness Audit Passed!")

    rules = audit["relevant_records"]
    rules2 = rules

    with open("changes.json", "w") as f:
        json.dump(rules, f, indent=2)
    print("📁 Canonical contract saved to changes.json")

    print("\n==================================================")
    print("--- [Stage 3] Matching Usages & Patching Code ---")
    print("==================================================")
    report = man_extract(rules)
    report2 = report
    print(f" Matches report generated for patcher: {json.dumps(report, indent=2)}")

    factored = code_patcher('before.py', report, rules, target_major_tag)
    print("\n--- Patched Code Output ---")
    print(factored)


    return factored;
    # print("\n--- [Stage 4] Ready for Sandboxed Docker pytest execution ---")

# Run strictly for the 0.28.1 -> v1.0.0 migration step first
fixed = run_single_step_migration('openai', 'v1.0.0')

MAX_RETRIES = 3
retry_count = 0

pr = verify_single_file_in_docker();

# --- Stage 5: Repair Loop ---
while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < MAX_RETRIES:
    retry_count += 1
    print(f"\n⚠️ Stage 4 Failed with {pr.error_type} (Attempt {retry_count}/{MAX_RETRIES})")
    print("Routing to Stage 5 Repair Loop...")

    repair_context = {
        "file_name": pr.target_file,
        "error_type": pr.error_type,
        "error_message": pr.error_message,
        "traceback": pr.raw_stderr,
        "attempted_code": open(pr.target_file).read()
    }

    # Generate repaired code and overwrite target file
    fixed = code_patcher('before.py', report, rules, repair_context)
    print(rules, report)

    # Re-verify in sandbox
    pr = verify_single_file_in_docker()

# --- Final Decision & User Prompt ---
if pr.status == VerificationResult.PASS:
    print("\n✅ Verification Successful!")
    yes_no = input("Do you wanna patch it? [Y/n]: ").strip().lower()
    
    if yes_no in ['y', 'yes', '']:
        with open("sample.py", 'w') as f:
            f.write(fixed)
        print("🎉 Code successfully patched to sample.py")
    else:
        print("Exit without applying patch.")

elif pr.status == VerificationResult.MIGRATION_ERROR:
    print(f"\n❌ Stage 5 Repair Loop exhausted after {MAX_RETRIES} attempts. Manual intervention required.")

else:
    print(f"\n⚠️ Pipeline aborted due to status: {pr.status}") 



# while(pr.status != VerificationResult.PASS):
        
#     print(f"⚠️ Stage 4 Failed with {pr.error_type}. Routing to Stage 5 Repair Loop...")
        
#     repair_context = {
#                 "file_name": pr.target_file,
#                 "error_type": pr.error_type,
#                 "error_message": pr.error_message,
#                 "traceback": pr.raw_stderr,
#                 "attempted_code": open(pr.target_file).read()
#             }
        
#     code_patcher('before.py', report2, rules2, repair_context)

#     pr = verify_single_file_in_docker()







