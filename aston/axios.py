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


import re
import json


ALLOWED_MIGRATION_TYPES = {
    "rename",
    "structural",
    "initialization",
    "configuration",
    "removal",
    "manual",
}

ALLOWED_VALIDATION_STATUSES = {
    "validated",
    "needs_review",
    "rejected",
}


def validate_and_normalize_rules(
    package: str,
    current_version: str,
    target_version: str,
    rules: list,
    raw_code_examples: list[str],
) -> list[dict]:
    """
    Validate extracted migration rules using Codestral.

    Returns only rules with validation_status == "validated".
    Uncertain, rejected, or malformed rules are excluded so they
    cannot reach the automatic patching stage.

    Each accepted rule contains:
        old_api
        new_api
        type
        requires_instantiation
        description
        evidence
        validation_status
    """

    if not rules:
        print("⚠️ Rule validation skipped: no extracted rules.")
        return []

    # Preserve the original extraction output as validation context.
    rules_json = json.dumps(rules, indent=2, default=str)
    examples_json = json.dumps(
        raw_code_examples or [],
        indent=2,
        default=str,
    )

    system_prompt = """
You are a Python dependency migration rule validator.

Your task is to validate and normalize candidate migration rules.
Do not generate or modify application source code.

For every candidate rule:

1. Check whether old_api and new_api accurately represent the
   migration described by the supplied information.

2. Do not assume a replacement is valid because its name looks
   plausible. For example, do not infer that an attribute named
   "proxy" became "proxies" without supporting evidence.

3. Preserve the distinction between a direct rename and a structural
   migration. If a client object must be created, configured, or
   substituted for a module-level API, classify the rule accordingly.

4. Use exactly one of these types:
   - rename
   - structural
   - initialization
   - configuration
   - removal
   - manual

5. Set requires_instantiation to:
   - true if the migration requires constructing a client or object
   - false if it does not
   - null if this cannot be determined

6. Write a concise, actionable description explaining what changes
   and any relevant prerequisites.

7. Preserve source evidence and references provided in the input.
   Do not fabricate quotations, URLs, documentation, or evidence.
   Code examples show how APIs are used; they do not, by themselves,
   prove that a migration is valid.

8. Set validation_status to exactly one of:
   - validated: the proposed migration is supported by the supplied
     evidence and its instructions are sufficiently clear
   - needs_review: the evidence is incomplete, ambiguous, or
     insufficient to safely approve the rule
   - rejected: the rule contradicts the supplied evidence or is
     demonstrably incorrect

9. If a rule is uncertain, do not invent a replacement just to fill
   new_api. Use null when there is no confirmed replacement.
   Classify migrations requiring human judgment as manual.

10. Keep every candidate rule in the output, including rejected
    and uncertain ones, so the caller can audit the results.

Return ONLY valid JSON in this exact structure:

{
  "rules": [
    {
      "old_api": "string",
      "new_api": "string or null",
      "type": "one allowed type",
      "requires_instantiation": true,
      "description": "string",
      "evidence": "existing supporting evidence or null",
      "validation_status": "validated, needs_review, or rejected"
    }
  ]
}

The requires_instantiation field may also be false or null.
Do not wrap the JSON in Markdown fences.
"""

    user_prompt = f"""
PACKAGE: {package}
CURRENT VERSION: {current_version}
TARGET VERSION: {target_version}

CANDIDATE MIGRATION RULES:
{rules_json}

RAW CODE EXAMPLES FOR USAGE CONTEXT ONLY:
{examples_json}

Validate and normalize every candidate rule.
Do not silently discard any candidate.
"""

    try:
        response = client.chat.complete(
            model="codestral-latest",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        content = response.choices[0].message.content

        if not isinstance(content, str) or not content.strip():
            print("❌ Rule validation failed: empty model response.")
            return []

        # Allow JSON surrounded by Markdown fences, despite the prompt.
        content = content.strip()
        content = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            content,
            flags=re.IGNORECASE,
        ).strip()

        result = json.loads(content)

        if not isinstance(result, dict):
            raise ValueError("Response must be a JSON object.")

        normalized_rules = result.get("rules")

        if not isinstance(normalized_rules, list):
            raise ValueError("'rules' must be a JSON list.")

        validated = []
        needs_review = 0
        rejected = 0
        malformed = 0

        for rule in normalized_rules:
            if not isinstance(rule, dict):
                malformed += 1
                continue

            old_api = rule.get("old_api")
            new_api = rule.get("new_api")
            migration_type = rule.get("type")
            status = rule.get("validation_status")
            requires_instantiation = rule.get(
                "requires_instantiation"
            )
            description = rule.get("description")
            evidence = rule.get("evidence")

            # Reject malformed or unexpected model output.
            if (
                not isinstance(old_api, str)
                or not old_api.strip()
                or migration_type not in ALLOWED_MIGRATION_TYPES
                or status not in ALLOWED_VALIDATION_STATUSES
                or not isinstance(description, str)
                or not description.strip()
            ):
                malformed += 1
                continue

            if new_api is not None and not isinstance(new_api, str):
                malformed += 1
                continue

            if requires_instantiation not in (True, False, None):
                malformed += 1
                continue

            if evidence is not None and not isinstance(evidence, str):
                malformed += 1
                continue

            # A rule cannot be automatically applied without a
            # concrete replacement or an explicit removal operation.
            if status == "validated":
                if migration_type == "removal":
                    # Removal still needs an actionable explanation.
                    if not description.strip():
                        malformed += 1
                        continue
                elif not new_api or not new_api.strip():
                    needs_review += 1
                    continue

                # Require evidence or a reference preserved in the
                # original candidate data. Do not trust a newly
                # invented evidence statement from the model.
                original_text = rules_json
                original_evidence = evidence and (
                    evidence in original_text
                )

                if not original_evidence:
                    needs_review += 1
                    continue

                validated.append({
                    "old_api": old_api.strip(),
                    "new_api": (
                        new_api.strip()
                        if isinstance(new_api, str)
                        else None
                    ),
                    "type": migration_type,
                    "requires_instantiation": requires_instantiation,
                    "description": description.strip(),
                    "evidence": evidence,
                    "validation_status": "validated",
                })

            elif status == "needs_review":
                needs_review += 1

            else:
                rejected += 1

        print("\n========== Rule Validation Summary ==========")
        print(f"Candidate rules: {len(rules)}")
        print(f"Validated and accepted: {len(validated)}")
        print(f"Needs review / insufficient evidence: {needs_review}")
        print(f"Rejected by validator: {rejected}")
        print(f"Malformed or invalid output: {malformed}")
        print("==============================================\n")

        return validated

    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"❌ Rule validation returned invalid output: {exc}")
        return []

    except Exception as exc:
        print(f"❌ Rule validation failed: {exc}")
        return []

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

    if not rules or not matches:
        print(
            "⚠️ No valid rules/matches provided. "
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
    
    
    
    if not composed_rules:
        print(
            "⚠️ Warning: No migration rules extracted across the version chain!"
        )
        return [], [], []
    
    print("\n==================================================")
    print("--- [Stage 2] Relevance & Completeness Audit ---")
    print("==================================================")
    dynamic_stage0_symbols = stage0_symbols
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

    print(report, relevant_rules, target_version)

    return report, relevant_rules, raw_code_examples






