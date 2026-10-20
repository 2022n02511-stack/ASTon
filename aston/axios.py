import json
import os
from pathlib import Path
from dotenv import load_dotenv
from mistralai.client import Mistral
from aston.rule_validation import validate_rules_against_target
from aston.scanner import parser
from aston.ingestion import run_stage1_pipeline, get_latest_pypi_version
from aston.relevance import evaluate_relevance_and_completeness, canonicalize_usage
from aston.docker import verify_single_file_in_docker, VerificationResult

load_dotenv()

API_KEY = os.environ.get("MISTRAL_API_KEY")
client = Mistral(api_key=API_KEY)

with open("patcher_prompt.txt", "r") as f:
    prompt = f.read()


def contains_items(items) -> bool:
    """FIXED: now handles both the old list shape and the new
    dict[str, list] shape introduced when all_rules/all_reports were
    flattened — previously only checked isinstance(items, list), so
    every call against the new dict shape silently failed closed."""
    if isinstance(items, dict):
        return any(contains_items(v) for v in items.values())
    return (
        isinstance(items, list)
        and len(items) > 0
        and any(isinstance(item, dict) for item in items)
    )


def clean_mistral_code(raw_response: str) -> str:
    """Removes markdown code fences from the LLM string response."""
    lines = raw_response.strip().splitlines()
    if lines and lines[0].startswith("```python"):
        lines.pop(0)
    elif lines and lines[0].startswith("```"):
        lines.pop(0)
    if lines and lines[-1].strip() == "```":
        lines.pop()
    return "\n".join(lines).strip()


def stage0(package_name: str, parses: list[dict]) -> set[str]:
    """Stage 0: the set of canonical usages this repo actually has for `package_name`."""
    stage0_records = [p for p in parses if p["package"] == package_name]
    dynamic_stage0_symbols = {canonicalize_usage(record) for record in stage0_records}
    print(f" Dynamic AST Scanned Usages: {dynamic_stage0_symbols}")
    return dynamic_stage0_symbols



def man_extract(rules: list, parses: list[dict]) -> list[dict]:
    """Match AST usages to all relevant migration candidates without
    dropping candidate status, evidence, or alternative replacements."""

    from collections import defaultdict

    extracted = defaultdict(list)

    for rule in rules:
        package = rule.get("package")
        old_api = rule.get("old_api")

        if not package or not old_api:
            continue

        # Preserve every candidate for the same old API.
        extracted[(package, old_api)].append(dict(rule))

    report = []
    filename = Path("before.py").name

    for parse in parses:
        canonical_usage = canonicalize_usage(parse)
        key = (parse.get("package"), canonical_usage)
        candidates = extracted.get(key)

        if not candidates:
            continue

        report.append({
            "file_name": filename,
            "Line": parse.get("line"),
            "alias": parse.get("local_alias"),
            "package": parse.get("package"),
            "detected_usage": parse.get("api_usage"),
            "canonical_usage": canonical_usage,
            "instance": parse.get("instance"),
            "kwargs": parse.get("kwargs"),
            "change": {
                "candidates": candidates,
            },
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
        print("No valid migration rules or matching usages. Retaining original code unmodified.")
        return code

    system_prompt = prompt

    if repair_context:
        repair_instructions = repair_context.get("repair_instructions", "")

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
{json.dumps(repair_context, indent=2, default=str) if repair_context else "None"}

CURRENT SOURCE CODE TO MIGRATE OR REPAIR:
```python
{code}
```

Return ONLY the complete Python source code.
"""

    response = client.chat.complete(
        model="codestral-latest",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": cont},
        ],
    )

    patched = response.choices[0].message.content
    clean = clean_mistral_code(patched)

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
            print("❌ Repair repeated a previously failed source; rejecting candidate.")
            return code

    with open(source_code, "w") as f:
        f.write(clean)

    return clean


def scraper(pkg_name: str, current_version: str, target_version: str,
            stage0_symbols: set[str], parses: list[dict]):
    """
    Runs Stages 1-3 for one package. Returns is_genuinely_complete now too —
    previously computed here but never returned, so pipeline.py had no way
    to know coverage was incomplete and proceeded regardless.
    """
    print("\n==================================================")
    print("--- [Stage 1] Multi-Boundary Chain Ingestion ---")
    print("==================================================")

    composed_rules, doc_text, raw_code_examples = run_stage1_pipeline(
        pkg_name, current_version=current_version,
        target_version=target_version, stage0_symbols=stage0_symbols,
    )

    for rule in composed_rules:
        old_api = str(rule.get("old_api", "")).lower()
        if "proxy" in old_api or "api_key" in old_api:
            print(
                "CONFIG CANDIDATE:",
                json.dumps(rule, indent=2, default=str),
            )

    if not composed_rules:
        print("⚠️ Warning: No migration rules extracted across the version chain!")
        return [], [], [], "", False

    print("\n========== STAGE 2 DEBUG ==========")

    print("\nSTAGE 0 SYMBOLS:")
    print(sorted(stage0_symbols))

    print("\nCOMPOSED RULE COUNT:")
    print(len(composed_rules))

    print("\nCOMPOSED RULE SAMPLE:")
    print(json.dumps(composed_rules[:10], indent=2, default=str))

    print("\nRULE OLD_API VALUES:")
    print([
        rule.get("old_api")
        for rule in composed_rules
    ])

    print("\n==================================================")
    print("--- [Stage 2] Relevance & Completeness Audit ---")
    print("==================================================")
    audit = evaluate_relevance_and_completeness(stage0_symbols, composed_rules, doc_text)

    print("OBSERVED:", sorted(stage0_symbols))
    print("RELEVANT:", [
        r["old_api"] for r in audit["relevant_records"]
    ])
    print("MISSING:", sorted(audit["missing_symbols"]))

    print("\nAUDIT RESULT:")
    print(json.dumps(audit, indent=2, default=str))

    # Generalized replacement for the old hardcoded ".chat.completions.create"
    # suffix check: a scanned symbol isn't "missing a rule" if it already
    # matches some rule's new_api — it's already migrated, not unmigrated.

    already_migrated_targets = {
        r.get("new_api") for r in composed_rules if r.get("new_api")
    }
    raw_missing = audit.get("missing_symbols", [])
    filtered_missing = [sym for sym in raw_missing if sym not in already_migrated_targets]

    print(f" Expected Affected Symbols: {audit['expected_affected_symbols']}")
    print(f" Missing Symbols: {audit['missing_symbols']}")

    is_genuinely_complete = len(filtered_missing) == 0
    if not is_genuinely_complete:
        print(f"🚨 WARNING: Genuinely missing rule extractions detected: {filtered_missing}")
    else:
        print("✅ Extraction Completeness Audit Passed (ignoring already-migrated calls)!")

    relevant_rules = audit["relevant_records"]

    # print(relevant_rules)


    with open("changes.json", "w") as f:
        json.dump(relevant_rules, f, indent=2)
    print("📁 Canonical contract saved to changes.json")

    print("\n==================================================")
    print("--- [Stage 3] Matching Usages & Patching Code ---")
    print("==================================================")
    report = man_extract(relevant_rules, parses)

    print(report, relevant_rules, raw_code_examples)

    return report, relevant_rules, raw_code_examples, doc_text, is_genuinely_complete