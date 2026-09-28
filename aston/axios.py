import json
import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai

from aston.scanner import parser
from aston.ingestion import run_stage1_pipeline, get_latest_pypi_version
from aston.relevance import evaluate_relevance_and_completeness, canonicalize_usage
from aston.docker import verify_single_file_in_docker, VerificationResult
from mistralai.client import Mistral


load_dotenv()

# API_KEY = os.environ.get("GEMINI_API_KEY_1") or os.environ.get("GEMINI_API_KEY")
API_KEY = os.environ.get("MISTRAL_API_KEY")
client = Mistral(api_key=API_KEY)
# client = genai.Client(api_key=API_KEY)

def clean_mistral_code(raw_response: str) -> str:
    """Removes markdown code fences from the LLM string response."""
    lines = raw_response.strip().splitlines()
    
    # If the response starts with a python code block marker, skip it
    if lines and lines[0].startswith("```python"):
        lines.pop(0)
    elif lines and lines[0].startswith("```"):
        lines.pop(0)
        
    # If the response ends with a code block marker, remove it
    if lines and lines[-1].strip() == "```":
        lines.pop()
        
    return "\n".join(lines).strip()

# Load Stage 3 system prompt
with open('aston/patcher_prompt.txt', 'r') as f:
    prompt = f.read()

def man_extract(rules: list):
    """Maps extracted LLM rules against actual AST parser output."""

    extracted = {}

    for rule in rules:
        extracted[(rule["package"], rule["old_api"])] = {
            "new_api": rule["new_api"],
            "requires_instantiation": rule.get("requires_instantiation")
        }

    parses = parser()

    report = []
    filename = Path("before.py").name

    for parse in parses:

        canonical_usage = canonicalize_usage(parse)

        key = (
            parse["package"],
            canonical_usage
        )

        if key not in extracted:
            continue

        report.append({
            "file_name": filename,
            "Line": parse["line"],
            "alias" : parse["local_alias"],
            "package": parse["package"],
            "detected_usage": parse["api_usage"],
            "canonical_usage": canonical_usage,
            "change": extracted[key]
        })

    return report      

def code_patcher(source_code: str, matches: list, rules: list, target_version: str, repair_context: dict | None = None):
    """
    Stage 3 / Stage 5 AST and LLM Code Patcher.
    ISOLATION GUARANTEE: If rules or matches are empty, strictly return original code!
    """
    with open(source_code, 'r') as f:
        code = f.read()

    if not rules or not matches:
        print("⚠️ Stage 3 Isolation Enforcement: No valid rules/matches provided. Retaining original code unmodified.")
        return code

    # cont = f"""
    # SYSTEM_PROMPT:
    # {prompt}

    cont = f"""

    MIGRATION_RULES: 
    {json.dumps(rules, indent=2)}

    MATCHES/AFFECTED_USAGES: 
    {json.dumps(matches, indent=2)}

    TARGET_DEPENDENCY_VERSION: 
    {target_version}

    REPAIR_CONTEXT (Stage 5 Diagnostics if present): 
    {json.dumps(repair_context, indent=2) if repair_context else "None"}
    
    ORIGINAL SOURCE CODE:
    ```python
    {code}
    ```

    Output ONLY the refactored Python code inside ```python fences.
    """

    # response = client.models.generate_content(
    #     model="gemini-3.6-flash",
    #     contents=cont
    # )    

    response = client.chat.complete(
        model="codestral-latest",
        messages=[
            {
                "role": "system",
                "content": prompt,
            },
            {
                "role": "user",
                "content": cont,
            },
        ]
    )

    # raw_text = response.text
    # patched = raw_text.split("```python")[1].split("```")[0].strip() if "```python" in raw_text else raw_text.strip()
    patched = response.choices[0].message.content
    # print(patched)

    clean = clean_mistral_code(patched)

    # print(clean)

    with open('before.py', 'w') as f:
        f.write(clean)

    return clean

def run_chain_migration(package_name: str):
    """
    Executes Stages 0–3 utilizing multi-boundary version discovery and rule composition.
    """
    print(f"\n==================================================")
    print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    print(f"==================================================")
    parses = parser()

    stage0_records = [
    parse
    for parse in parses
    if parse["package"] == package_name
    ]

    dynamic_stage0_symbols =  { canonicalize_usage(record) 
                               for record in stage0_records }

    # dynamic_stage0_symbols = {parse['api_usage'] for parse in parses if parse['package'] == package_name}
    print(f" Dynamic AST Scanned Usages: {dynamic_stage0_symbols}")

    print(f"\n==================================================")
    print(f"--- [Stage 1] Multi-Boundary Chain Ingestion ---")
    print(f"==================================================")

    target_version = get_latest_pypi_version(package_name)
    
    # Executes get_migration_chain + merge_rule_chain across all major version jumps
    composed_rules, doc_text = run_stage1_pipeline(package_name, current_version="0.28.1", target_version=target_version)



    if not composed_rules:
        print(
        "⚠️ Warning: No migration rules extracted across the version chain!"
        )
        return None, [], [], target_version

    print("\n==================================================")
    print("--- [Stage 2] Relevance & Completeness Audit ---")
    print("==================================================")
    audit = evaluate_relevance_and_completeness(dynamic_stage0_symbols, composed_rules, doc_text)

    raw_missing = audit.get('missing_symbols', [])
    filtered_missing = [
        sym for sym in raw_missing 
        if not (sym.endswith(".chat.completions.create") and not sym.startswith("openai."))
    ]
    
    print(f" Expected Affected Symbols: {audit['expected_affected_symbols']}")
    print(f" Missing Symbols: {audit['missing_symbols']}")

    is_genuinely_complete = len(filtered_missing) == 0
    if not is_genuinely_complete:
        print(f"🚨 WARNING: Genuinely missing rule extractions detected: {filtered_missing}")
    else:
        print("✅ Extraction Completeness Audit Passed (ignoring already-migrated client calls)!")

    relevant_rules = audit["relevant_records"]

    with open("changes.json", "w") as f:
        json.dump(relevant_rules, f, indent=2)
    print("📁 Canonical contract saved to changes.json")

    print("\n==================================================")
    print("--- [Stage 3] Matching Usages & Patching Code ---")
    print("==================================================")
    report = man_extract(relevant_rules)
    print(f" Matches report generated for patcher: {json.dumps(report, indent=2)}")

    target_version = "v1.0.0"  # Target major version boundary

    print(report, relevant_rules, target_version)
    factored_code = code_patcher('before.py', report, relevant_rules, target_version)
    
    print("\n--- Patched Code Output ---")
    print(factored_code)

    return factored_code, report, relevant_rules, target_version


# ==================================================
# --- PIPELINE ENTRYPOINT & REPAIR LOOP (Stage 4 & 5) ---
# ==================================================

# Execute Stages 0 through 3
# fixed_code, report, rules, target_version = run_chain_migration('openai')

# if fixed_code:
#     MAX_RETRIES = 3
#     retry_count = 0

#     # Execute Stage 4 Sandboxed Docker Verification
#     pr = verify_single_file_in_docker(target_file="before.py", package_req="openai>=1.0.0")

#     # --- Stage 5: Self-Healing Repair Loop ---
#     while pr.status == VerificationResult.MIGRATION_ERROR and retry_count < MAX_RETRIES:
#         retry_count += 1
#         print(f"\n⚠️ Stage 4 Failed with {pr.error_type} (Attempt {retry_count}/{MAX_RETRIES})")
#         print("Routing to Stage 5 Self-Healing Repair Loop...")

#         repair_context = {
#             "file_name": pr.target_file,
#             "error_type": pr.error_type,
#             "error_message": pr.error_message,
#             "traceback": pr.raw_stderr,
#             "attempted_code": open(pr.target_file).read()
#         }

#         # Re-patch code using error context
#         fixed_code = code_patcher(
#             'before.py', 
#             report, 
#             rules, 
#             target_version, 
#             repair_context=repair_context
#         )

#         # Re-verify in Docker sandbox
#         pr = verify_single_file_in_docker(target_file="before.py", package_req="openai>=1.0.0")

#     # --- Final Decision & User Prompt ---
#     if pr.status == VerificationResult.PASS:
#         print("\n✅ Sandboxed Verification Successful!")
#         yes_no = input("Do you wanna patch it? [Y/n]: ").strip().lower()
        
#         if yes_no in ['y', 'yes', '']:
#             with open("sample.py", 'w') as f:
#                 f.write(fixed_code)
#             print("🎉 Code successfully patched and saved to sample.py")
#         else:
#             print("Exit without applying patch.")

#     elif pr.status == VerificationResult.MIGRATION_ERROR:
#         print(f"\n❌ Stage 5 Repair Loop exhausted after {MAX_RETRIES} attempts. Manual intervention required.")

#     else:
#         print(f"\n⚠️ Pipeline halted due to verification status: {pr.status}")