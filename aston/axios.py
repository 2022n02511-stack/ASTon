import json
import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai
import re
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

def normalize_evidence_text(text: str) -> str:
    """Normalize whitespace and common Markdown formatting for comparison."""
    if not isinstance(text, str):
        return ""

    text = text.replace("\\", "")
    text = re.sub(r"`+", "", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip().casefold()



def contains_items(items):
    """Return True when items is a non-empty list of dictionaries."""
    return (
        isinstance(items, list)
        and len(items) > 0
        and any(isinstance(item, dict) for item in items)
    )


# ALLOWED_MIGRATION_TYPES = {
#     "rename",
#     "structural",
#     "initialization",
#     "configuration",
#     "removal",
#     "manual",
# }

# ALLOWED_VALIDATION_STATUSES = {
#     "validated",
#     "needs_review",
#     "rejected",
# }



# def validate_and_normalize_rules(
#     package: str,
#     current_version: str,
#     target_version: str,
#     rules: list,
#     raw_code_examples: list[str],
#     source_evidence: str = "",
# ) -> list[dict]:
#     """
#     Validate and normalize extracted migration rules.

#     Only rules with validated status, a concrete migration, and
#     supporting evidence from the supplied input are returned.
#     """

#     if not rules:
#         print("⚠️ Rule validation skipped: no extracted rules.")
#         return []

#     allowed_types = {
#         "rename",
#         "structural",
#         "initialization",
#         "configuration",
#         "removal",
#         "manual",
#     }

#     allowed_statuses = {
#         "validated",
#         "needs_review",
#         "rejected",
#     }

#     rules_json = json.dumps(rules, indent=2, default=str)

#     system_prompt = """
# You are a Python dependency migration rule validator.

# Validate and normalize every candidate migration rule using the
# provided source migration documentation and original extracted data.

# Rules:
# 1. Never assume an API replacement is valid because its name looks plausible.
# 2. Do not invent APIs, source URLs, quotations, or evidence.
# 3. Classify each rule as exactly one of:
#    rename, structural, initialization, configuration, removal, manual.
# 4. Definitions:
#    - rename: a direct symbol replacement where nothing else changes (e.g. openai.proxy -> openai.proxies).
#    - configuration: global module settings or attributes that must now be passed as client parameters or instance attributes (e.g. openai.api_key -> client.api_key or openai.base_url -> openai.base_url passed to client).
#    - initialization / client_migration: requires instantiating a client object first.
# 5. Set requires_instantiation to true, false, or null.
# 6. Provide an actionable description explaining WHY it's a configuration or structural change.
# 7. Preserve the original API and source evidence where possible.
# 8. Set validation_status to exactly one of:
#    validated, needs_review, rejected.
# 9. A rule may be validated only when the supplied evidence supports the migration.
# 10. If evidence is insufficient, use needs_review.
# 11. If the old API was removed without a confirmed replacement, do not invent new_api. Use null.
# 12. Keep every input rule in the output for auditability.

# Return only valid JSON with this structure:

# {
#   "rules": [
#     {
#       "old_api": "string",
#       "new_api": "string or null",
#       "type": "configuration",
#       "requires_instantiation": true,
#       "description": "string",
#       "evidence": "exact supporting excerpt or null",
#       "source_url": "existing source URL or null",
#       "validation_status": "validated"
#     }
#   ]
# }

# Do not wrap the JSON in Markdown fences.
# """

#     user_prompt = f"""
# PACKAGE: {package}
# CURRENT VERSION: {current_version}
# TARGET VERSION: {target_version}

# ORIGINAL EXTRACTED RULES:
# {rules_json}

# RAW CODE EXAMPLES (usage context only):
# {json.dumps(raw_code_examples or [], indent=2, default=str)}

# SOURCE MIGRATION DOCUMENTATION:
# {source_evidence or "No source documentation supplied."}

# Validate every rule against the available source evidence.
# Preserve original evidence and source URLs. Do not invent either.
# """

#     try:
#         response = client.chat.complete(
#             model="codestral-latest",
#             messages=[
#                 {"role": "system", "content": system_prompt},
#                 {"role": "user", "content": user_prompt},
#             ],
#         )

#         content = response.choices[0].message.content

#         if not isinstance(content, str) or not content.strip():
#             print("❌ Rule validation failed: empty model response.")
#             return []

#         content = content.strip()

#         if content.startswith("```"):
#             content = re.sub(
#                 r"^```(?:json)?\s*|\s*```$",
#                 "",
#                 content,
#                 flags=re.IGNORECASE,
#             ).strip()

#         result = json.loads(content)

#         if not isinstance(result, dict):
#             raise ValueError("Response must be a JSON object.")

#         normalized_rules = result.get("rules")

#         if not isinstance(normalized_rules, list):
#             raise ValueError("'rules' must be a list.")

#         # Build evidence from the supplied sources, not from claims
#         # invented by the model.
#         source_text = source_evidence or ""
#         original_text = rules_json

#         accepted = []
#         needs_review = 0
#         rejected = 0
#         malformed = 0

#         for rule in normalized_rules:
#             if not isinstance(rule, dict):
#                 malformed += 1
#                 continue

#             old_api = rule.get("old_api")
#             new_api = rule.get("new_api")
#             migration_type = rule.get("type")
#             status = rule.get("validation_status")
#             requires_instantiation = rule.get(
#                 "requires_instantiation"
#             )
#             description = rule.get("description")
#             evidence = rule.get("evidence")
#             source_url = rule.get("source_url")

#             if (
#                 not isinstance(old_api, str)
#                 or not old_api.strip()
#                 or migration_type not in allowed_types
#                 or status not in allowed_statuses
#                 or not isinstance(description, str)
#                 or not description.strip()
#             ):
#                 malformed += 1
#                 continue

#             if new_api is not None and not isinstance(new_api, str):
#                 malformed += 1
#                 continue

#             if requires_instantiation not in (True, False, None):
#                 malformed += 1
#                 continue

#             if evidence is not None and not isinstance(evidence, str):
#                 malformed += 1
#                 continue

#             if source_url is not None and not isinstance(source_url, str):
#                 malformed += 1
#                 continue

#             if status == "rejected":
#                 rejected += 1
#                 continue

#             if status == "needs_review":
#                 needs_review += 1
#                 continue

#             # Require a concrete replacement for automatic migrations.
#             # Removal/manual rules need separate human-reviewed handling.
#             if migration_type in {"removal", "manual"}:
#                 needs_review += 1
#                 continue

#             if not isinstance(new_api, str) or not new_api.strip():
#                 needs_review += 1
#                 continue

#             # A rule must be supported by an actual excerpt from the
#             # migration documentation. A URL or the original rule alone
#             # does not prove that the proposed replacement is correct.
            
#             normalized_evidence = normalize_evidence_text(evidence)
#             normalized_source = normalize_evidence_text(source_evidence)

#             evidence_is_grounded = bool(
#                 normalized_evidence
#                 and normalized_source
#                 and normalized_evidence in normalized_source
#             )

#             if not evidence_is_grounded:
#                 print(
#                     f"⚠️ Evidence mismatch: {old_api} -> {new_api}"
#                 )
#                 print(f"   Evidence returned: {evidence!r}")
#                 print(f"   Source length: {len(source_evidence)}")
#                 needs_review += 1
#                 continue

#             accepted.append({
#                 "package": package,
#                 "old_api": old_api.strip(),
#                 "new_api": new_api.strip(),
#                 "type": migration_type,
#                 "requires_instantiation": requires_instantiation,
#                 "description": description.strip(),
#                 "evidence": evidence if evidence_is_grounded else None,
#                 "source_url": source_url or None,
#                 "validation_status": "validated",
#             })

#         print("\n========== Rule Validation Summary ==========")
#         print(f"Candidate rules: {len(rules)}")
#         print(f"Validated and accepted: {len(accepted)}")
#         print(f"Needs review / insufficient evidence: {needs_review}")
#         print(f"Rejected: {rejected}")
#         print(f"Malformed: {malformed}")
#         print("==============================================\n")

#         return accepted

#     except (json.JSONDecodeError, ValueError, TypeError) as exc:
#         print(f"❌ Rule validation returned invalid output: {exc}")
#         return []

#     except Exception as exc:
#         print(f"❌ Rule validation failed: {exc}")
#         return []

def stage0(package_name: str, parse):
    """
    Executes Stages 0–3 utilizing multi-boundary version discovery and rule composition.
    """
    # print(f"\n==================================================")
    # print(f"--- [Stage 0] Fetching Dynamic AST Scanned Usages ---")
    # print(f"==================================================")
    parses = parse

    stage0_records = [
    parse
    for parse in parses
    if parse["package"] == package_name
    ]

    dynamic_stage0_symbols =  { canonicalize_usage(record) 
                               for record in stage0_records }

    # dynamic_stage0_symbols = {parse['api_usage'] for parse in parses if parse['package'] == package_name}
    print(f" Dynamic AST Scanned Usages: {dynamic_stage0_symbols}")

    return dynamic_stage0_symbols


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
with open('patcher_prompt.txt', 'r') as f:
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
            "instnace": parse["instance"],
            "kwargs": parse["kwargs"],
            "change": extracted[key]
        })

    return report      


def code_patcher(
    source_code: str,
    matches: list,
    rules: list,
    target_version: list,
    current_version: list,
    repair_context: dict | None = None,
    raw_code_examples: list[str] | None = None,
):
    """Stage 3 migration and Stage 5 verification-guided repair."""

    with open(source_code, "r") as f:
        code = f.read()

    
    if not contains_items(rules) or not contains_items(matches):
        print(
            "No valid migration rules or matching usages. "
            "Retaining original code unmodified."
        )
        return code

    system_prompt = prompt

    if repair_context:
        repair_instructions = repair_context.get(
            "repair_instructions", ""
        )

        system_prompt += """

REPAIR MODE — OVERRIDES CONFLICTING MIGRATION INSTRUCTIONS

The current source has already failed target-environment verification.

Treat the current error and all previous attempts as evidence.
Trace the failing expression to the migration decision that produced it.

Never repeat an expression that a previous verification attempt
demonstrated to be invalid unless new, specific evidence explains
why it should now work.

Do not alternate between previously failed replacements.
Do not assume a migration rule is a valid literal substitution.
Inspect surrounding configuration, initialization, imports, and
object ownership before changing the failing code.

Preserve successful changes and unrelated code.
Do not invent APIs, attributes, constructor arguments, or configuration.
If the supplied evidence is insufficient for a safe replacement,
do not fabricate one.

Return the complete source file only.
"""

        if repair_instructions:
            system_prompt += "\n\nPROJECT-SPECIFIC REPAIR INSTRUCTIONS:\n"
            system_prompt += repair_instructions

    cont = f"""
MIGRATION_RULES_FOR_EACH_DEPENDENCY:
{json.dumps(rules, indent=2)}

MATCHES_AND_AFFECTED_USAGES:
{json.dumps(matches, indent=2)}

CURRENT_DEPENDENCY_VERSIONS:
{json.dumps(current_version, indent=2)}

TARGET_DEPENDENCY_VERSIONS:
{json.dumps(target_version, indent=2)}

RAW_CODE_EXAMPLES_FOR_CONTEXT_NOT_AUTHORITATIVE_RULES:
{json.dumps(raw_code_examples, indent=2, default=str)}

REPAIR_CONTEXT:
{json.dumps(repair_context, indent=2, default=str)
 if repair_context else "None"}

CURRENT SOURCE CODE TO MIGRATE OR REPAIR:
```python
{code}
```

Return ONLY the complete Python source code.
"""

    response = client.chat.complete(
        model="codestral-latest",
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": cont,
            },
        ],
    )

    patched = response.choices[0].message.content
    clean = clean_mistral_code(patched)

    # Do not accept an unchanged result or a previously failed candidate.
    if repair_context:
        failed_sources = {
            attempt.get("source_code")
            for attempt in repair_context.get("previous_attempts", [])
            if attempt.get("source_code")
        }

        if clean == code:
            print("❌ Repair returned unchanged source; rejecting candidate.")
            return code

        if clean in failed_sources:
            print(
                "❌ Repair repeated a previously failed source; "
                "rejecting candidate."
            )
            return code

    print(clean)

    with open(source_code, "w") as f:
        f.write(clean)

    return clean


    # print(f"\n==================================================")
    # print(f"--- [Stage 1] Multi-Boundary Chain Ingestion ---")
    # print(f"==================================================")

    # target_version = target_version
    
    # # Executes get_migration_chain + merge_rule_chain across all major version jumps
    # composed_rules, doc_text = run_stage1_pipeline(package_name, current_version=current_version, target_version=target_version)



    # if not composed_rules:
    #     print(
    #     "⚠️ Warning: No migration rules extracted across the version chain!"
    #     )
    #     return None, [], [], target_version

    # print("\n==================================================")
    # print("--- [Stage 2] Relevance & Completeness Audit ---")
    # print("==================================================")
    # audit = evaluate_relevance_and_completeness(dynamic_stage0_symbols, composed_rules, doc_text)

    # raw_missing = audit.get('missing_symbols', [])
    # filtered_missing = [
    #     sym for sym in raw_missing 
    #     if not (sym.endswith(".chat.completions.create") and not sym.startswith("openai."))
    # ]
    
    # print(f" Expected Affected Symbols: {audit['expected_affected_symbols']}")
    # print(f" Missing Symbols: {audit['missing_symbols']}")

    # is_genuinely_complete = len(filtered_missing) == 0
    # if not is_genuinely_complete:
    #     print(f"🚨 WARNING: Genuinely missing rule extractions detected: {filtered_missing}")
    # else:
    #     print("✅ Extraction Completeness Audit Passed (ignoring already-migrated client calls)!")

    # relevant_rules = audit["relevant_records"]

    # with open("changes.json", "w") as f:
    #     json.dump(relevant_rules, f, indent=2)
    # print("📁 Canonical contract saved to changes.json")

    # print("\n==================================================")
    # print("--- [Stage 3] Matching Usages & Patching Code ---")
    # print("==================================================")
    # report = man_extract(relevant_rules)
    # print(f" Matches report generated for patcher: {json.dumps(report, indent=2)}")

    # target_version = "v1.0.0"  # Target major version boundary

    # print(report, relevant_rules, target_version)

    # factored_code = code_patcher('before.py', report, relevant_rules, target_version)
    
    # print("\n--- Patched Code Output ---")
    # print(factored_code)

    # return factored_code, report, relevant_rules, target_version


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


def scraper(pkg_name, current_version, target_version, stage0_symbols):

    print(f"\n==================================================")
    print(f"--- [Stage 1] Multi-Boundary Chain Ingestion ---")
    print(f"==================================================")
    
    target_version = target_version
        
    # Executes get_migration_chain + merge_rule_chain across all major version jumps
    composed_rules, doc_text, raw_code_examples = run_stage1_pipeline(pkg_name, current_version=current_version, target_version=target_version, stage0_symbols=stage0_symbols)

    print(composed_rules)
    print(raw_code_examples)
    
    
    
    if not composed_rules:
        print(
            "⚠️ Warning: No migration rules extracted across the version chain!"
        )
        return [], [], []
    
    print("\n==================================================")
    print("--- [Stage 2] Relevance & Completeness Audit ---")
    print("==================================================")
    dynamic_stage0_symbols = stage0_symbols
    print(stage0_symbols)
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

    # target_version = "v1.0.0"  # Target major version boundary

    # print(doc_text)

    print(report, relevant_rules, target_version)

    return report, relevant_rules, raw_code_examples, doc_text






