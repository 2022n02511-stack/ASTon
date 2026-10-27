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


def stage0(package_name: str, repo_index: dict) -> set[str]:
    """
    Extracts canonical usages for `package_name` by traversing 
    the repository index 'files' structure.
    """
    flattened_usages = []
    
    files_dict = repo_index.get("files", {})
    for file_path, file_data in files_dict.items():
        if isinstance(file_data, dict):
            usages = file_data.get("usages", [])
            flattened_usages.extend(usages)

    # Filter records specifically for the target package
    stage0_records = [p for p in flattened_usages if p.get("package") == package_name]
    
    # Generate canonical symbols (e.g., 'openai.ChatCompletion.create')
    dynamic_stage0_symbols = {canonicalize_usage(record) for record in stage0_records}
    
    print(f"📁 Dynamic AST Scanned Usages for {package_name}: {dynamic_stage0_symbols}")
    return dynamic_stage0_symbols



def man_extract(rules: list, parses: list[dict], source_file: str,) -> list[dict]:
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
    filename = Path(source_file).name

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
    repo_rules,
    symbols,
    relationships,
    dependencies,
    repair_context: dict | None = None,
    raw_code_examples: list[str] | None = None,
):
    """Stage 3 migration and Stage 5 verification-guided repair."""

    if not contains_items(rules) or not contains_items(matches):
        print("No valid migration rules or matching usages. Retaining original code unmodified.")
        return source_code

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

    REPOSITORY_FILE_USAGES:
    {json.dumps(repo_rules, indent=2, default=str)}

    AST_SYMBOLS_INDEX:
    {json.dumps(symbols, indent=2, default=str)}

    CROSS_FILE_RELATIONSHIPS:
    {json.dumps(relationships, indent=2, default=str)}

    DEPENDENCIES_MAP:
    {json.dumps(dependencies, indent=2, default=str)}

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
    {source_code}
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
        if clean == source_code:
            print("❌ Repair returned unchanged source; rejecting candidate.")
            return source_code
        if clean in failed_sources:
            print("❌ Repair repeated a previously failed source; rejecting candidate.")
            return source_code

    # with open(source_code, "w") as f:
    #     f.write(clean)

    return clean



def scraper(
    pkg_name: str,
    current_version: str,
    target_version: str,
    stage0_symbols: set[str],
    parses: list[dict],
    composed_rules: list[dict],
    doc_text: str,
    source_file: str,
):
    """
    Run migration-rule coverage auditing, targeted extraction, and matching.

    Returns:
        (report, relevant_rules, is_genuinely_complete)
    """
    print("\n==================================================")
    print("--- [Stage 1] Multi-Boundary Chain Ingestion ---")
    print("==================================================")

    from aston.relevance import normalize_api_symbol

    def normalize_set(values) -> set[str]:
        """Normalize valid API symbols before set comparisons."""
        if not values:
            return set()

        return {
            normalize_api_symbol(value)
            for value in values
            if isinstance(value, str)
            and normalize_api_symbol(value)
        }

    def rule_key(rule: dict) -> tuple:
        """Deduplicate identical rules without discarding alternatives."""
        return (
            rule.get("package"),
            normalize_api_symbol(rule.get("old_api") or ""),
            normalize_api_symbol(rule.get("new_api") or ""),
            rule.get("from_version"),
            rule.get("to_version"),
            rule.get("migration_type"),
        )

    working_rules = [
        dict(rule)
        for rule in (composed_rules or [])
        if isinstance(rule, dict)
    ]
    working_doc_text = doc_text or ""

    for rule in working_rules:
        old_api = str(rule.get("old_api", "")).lower()
        if "proxy" in old_api or "api_key" in old_api:
            print(
                "CONFIG CANDIDATE:",
                json.dumps(rule, indent=2, default=str),
            )

    print("\n========== STAGE 2: INITIAL AUDIT ==========")
    print("STAGE 0 SYMBOLS:", sorted(stage0_symbols))
    print("INITIAL RULE COUNT:", len(working_rules))

    audit = evaluate_relevance_and_completeness(
        stage0_symbols,
        working_rules,
        working_doc_text,
    )

    observed_symbols = normalize_set(stage0_symbols)
    directly_covered = normalize_set(
        audit.get("directly_covered_symbols", [])
    )
    already_target = normalize_set(
        audit.get("already_target_symbols", [])
    )

    # Gather every symbol the audit explicitly identifies as uncovered,
    # missing, or unclassified.
    targeted_symbols = set()

    for field in (
        "missing_symbols",
        "uncovered_symbols",
        "unclassified_symbols",
    ):
        targeted_symbols.update(normalize_set(audit.get(field, [])))

    # Defensive fallback: do not silently omit observed APIs from targeted
    # extraction just because the audit omitted them from its output lists.
    #
    # already_target is excluded here only because the audit classifies it
    # separately. That classification must be based on actual target-version
    # compatibility evidence, not merely a matching new_api string.
    targeted_symbols.update(
        observed_symbols - directly_covered - already_target
    )

    if targeted_symbols:
        print("\n==================================================")
        print("--- [Stage 2.5] Targeted Rule Extraction ---")
        print("==================================================")
        print("FOCUS SYMBOLS:", sorted(targeted_symbols))

        try:
            extra_rules, extra_doc_text, extra_examples = (
                run_stage1_pipeline(
                    pkg_name,
                    current_version=current_version,
                    target_version=target_version,
                    stage0_symbols=targeted_symbols,
                )
            )

            extra_rules = extra_rules or []
            existing_keys = {
                rule_key(rule)
                for rule in working_rules
            }

            added_count = 0

            for rule in extra_rules:
                if not isinstance(rule, dict):
                    continue

                candidate = dict(rule)
                key = rule_key(candidate)

                if key not in existing_keys:
                    working_rules.append(candidate)
                    existing_keys.add(key)
                    added_count += 1

            if extra_doc_text:
                working_doc_text = (
                    working_doc_text
                    + "\n\n"
                    + extra_doc_text
                ).strip()

            print("TARGETED CANDIDATES RETURNED:", len(extra_rules))
            print("NEW DISTINCT RULES ADDED:", added_count)

            if extra_examples:
                print(
                    "TARGETED EXTRACTION ALSO RETURNED EXAMPLES:",
                    len(extra_examples),
                )

        except Exception as exc:
            print(
                "⚠️ Targeted extraction failed:",
                repr(exc),
            )

        # Re-evaluate coverage using the expanded rules and documentation.
        audit = evaluate_relevance_and_completeness(
            stage0_symbols,
            working_rules,
            working_doc_text,
        )

    print("\n==================================================")
    print("--- [Stage 2] Final Relevance & Completeness Audit ---")
    print("==================================================")
    print("OBSERVED:", sorted(observed_symbols))
    print("TOTAL RULES:", len(working_rules))
    print(
        "RELEVANT:",
        [
            rule.get("old_api")
            for rule in audit.get("relevant_records", [])
        ],
    )
    print("MISSING:", sorted(normalize_set(audit.get("missing_symbols", []))))
    print("UNCOVERED:", sorted(normalize_set(audit.get("uncovered_symbols", []))))
    print(
        "UNCLASSIFIED:",
        sorted(normalize_set(audit.get("unclassified_symbols", []))),
    )
    print("\nFINAL AUDIT RESULT:")
    print(json.dumps(audit, indent=2, default=str))

    relevant_rules = audit.get("relevant_records", [])

    missing = normalize_set(audit.get("missing_symbols", []))
    uncovered = normalize_set(audit.get("uncovered_symbols", []))
    unclassified = normalize_set(audit.get("unclassified_symbols", []))

    # Require both the audit's decision and empty unresolved-coverage sets.
    is_genuinely_complete = (
        bool(audit.get("is_complete", False))
        and not missing
        and not uncovered
        and not unclassified
    )

    print("\n========== FINAL COVERAGE STATUS ==========")
    print("Complete:", is_genuinely_complete)

    if not is_genuinely_complete:
        print("🚨 Migration coverage remains incomplete.")
    else:
        print("✅ Migration coverage audit passed.")

    # Save the rules actually selected by the final audit.
    with open("changes.json", "w", encoding="utf-8") as f:
        json.dump(relevant_rules, f, indent=2, default=str)

    print("📁 Canonical contract saved in changes.json")

    print("\n==================================================")
    print("--- [Stage 3] Matching Usages ---")
    print("==================================================")

    report = man_extract(
        relevant_rules,
        parses,
        source_file,
    )

    print("MATCHED USAGES:", len(report))
    print("RELEVANT RULES:", len(relevant_rules))

    if relevant_rules and not report:
        print(
            "⚠️ Rules were selected, but none matched the canonical "
            "AST usages. Check package names and old_api normalization."
        )

    return report, relevant_rules, is_genuinely_complete

