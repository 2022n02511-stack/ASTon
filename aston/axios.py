import ast
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
from aston.relevance import normalize_api_symbol
from aston.cli.console import info, error, warning, success, step, debug, data
load_dotenv()

API_KEY = os.environ.get("MISTRAL_API_KEY")
client = Mistral(api_key=API_KEY) if API_KEY else None

with open("patcher_prompt.txt", "r", encoding="utf-8") as f:
    prompt = f.read()


def contains_items(items) -> bool:
    """Check whether a list or nested dictionary contains rule/match records."""
    if isinstance(items, dict):
        return any(contains_items(value) for value in items.values())

    return (
        isinstance(items, list)
        and len(items) > 0
        and any(isinstance(item, dict) for item in items)
    )


def clean_mistral_code(raw_response: str) -> str:
    """Remove markdown code fences from the LLM response."""
    if not isinstance(raw_response, str):
        return ""

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
    Extract canonical usages for a package across the entire repository.

    This remains global because Stage 0 discovers the package APIs used
    anywhere in the repository.
    """
    flattened_usages = []

    files_dict = repo_index.get("files", {})

    for file_path, file_data in files_dict.items():
        if isinstance(file_data, dict):
            usages = file_data.get("usages", [])

            if isinstance(usages, list):
                flattened_usages.extend(
                    usage
                    for usage in usages
                    if isinstance(usage, dict)
                )

    stage0_records = [
        record
        for record in flattened_usages
        if record.get("package") == package_name
    ]

    dynamic_stage0_symbols = {
        canonicalize_usage(record)
        for record in stage0_records
    }

    info(
        f"Dynamic AST Scanned Usages for {package_name}: "
        f"{dynamic_stage0_symbols}"
    )

    return dynamic_stage0_symbols


def man_extract(
    rules: list,
    parses: list[dict],
    source_file: str,
) -> list[dict]:
    """Match AST usages to migration candidates using longest-prefix matching."""
    from collections import defaultdict

    extracted = defaultdict(list)

    for rule in rules:
        package = rule.get("package")
        old_api = normalize_api_symbol(rule.get("old_api", ""))

        if not package or not old_api:
            continue

        extracted[(package, old_api)].append(dict(rule))

    report = []
    filename = Path(source_file).as_posix()

    rule_keys_by_pkg = defaultdict(list)

    for package, old_api in extracted.keys():
        rule_keys_by_pkg[package].append(old_api)

    for package in rule_keys_by_pkg:
        rule_keys_by_pkg[package].sort(
            key=len,
            reverse=True,
        )

    for parse in parses:
        if not isinstance(parse, dict):
            continue

        package = parse.get("package")
        canonical_usage = canonicalize_usage(parse)
        normalized_usage = normalize_api_symbol(canonical_usage)

        matched_old_api = None

        for old_api in rule_keys_by_pkg.get(package, []):
            if (
                normalized_usage == old_api
                or normalized_usage.startswith(old_api + ".")
            ):
                matched_old_api = old_api
                break

        if not matched_old_api:
            continue

        candidates = extracted.get((package, matched_old_api))

        if not candidates:
            continue

        report.append(
            {
                "file_name": filename,
                "Line": parse.get("line"),
                "alias": parse.get("local_alias"),
                "package": package,
                "detected_usage": parse.get("api_usage"),
                "canonical_usage": canonical_usage,
                "instance": parse.get("instance"),
                "kwargs": parse.get("kwargs"),
                "change": {
                    "candidates": candidates,
                },
            }
        )

    info(
        f"man_extract: {filename} | "
        f"input usages={len(parses)} | matched usages={len(report)}"
    )

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

    if not client:
        raise RuntimeError(
            "MISTRAL_API_KEY is not set; cannot run code patcher."
        )

    # Require rules and matches for a normal migration, but don't
    # block repair mode.
    if not repair_context and (
        not contains_items(rules)
        or not contains_items(matches)
    ):
        warning(
            "No valid migration rules or matching usages. "
            "Retaining original code unmodified."
        )
        return source_code

    system_prompt = prompt

    if repair_context:
        repair_instructions = repair_context.get(
            "repair_instructions",
            "",
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
            system_prompt += (
                "\n\nPROJECT-SPECIFIC REPAIR INSTRUCTIONS:\n"
                + repair_instructions
            )

    cont = f"""
MIGRATION_RULES_FOR_EACH_DEPENDENCY:
{json.dumps(rules, indent=2, default=str)}

REPOSITORY_FILE_USAGES:
{json.dumps(repo_rules, indent=2, default=str)}

AST_SYMBOLS_INDEX:
{json.dumps(symbols, indent=2, default=str)}

CROSS_FILE_RELATIONSHIPS:
{json.dumps(relationships, indent=2, default=str)}

DEPENDENCIES_MAP:
{json.dumps(dependencies, indent=2, default=str)}

MATCHES_AND_AFFECTED_USAGES:
{json.dumps(matches, indent=2, default=str)}

CURRENT_DEPENDENCY_VERSIONS:
{json.dumps(current_version, indent=2, default=str)}

TARGET_DEPENDENCY_VERSIONS:
{json.dumps(target_version, indent=2, default=str)}

RAW_CODE_EXAMPLES_FOR_CONTEXT_NOT_AUTHORITATIVE_RULES:
{json.dumps(raw_code_examples, indent=2, default=str)}

REPAIR_CONTEXT:
{json.dumps(repair_context, indent=2, default=str) if repair_context else "None"}

CURRENT SOURCE CODE TO MIGRATE OR REPAIR:
{source_code}
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

    try:
        ast.parse(clean)
    except SyntaxError as se:
        error(
            "Patcher returned syntactically invalid Python code "
            f"({se}); rejecting candidate."
        )
        return source_code

    if repair_context:
        failed_sources = {
            attempt.get("source_code")
            for attempt in repair_context.get("previous_attempts", [])
            if attempt.get("source_code")
        }

        if clean == source_code:
            error(
                "Repair returned unchanged source; "
                "rejecting candidate."
            )
            return source_code

        if clean in failed_sources:
            error(
                "Repair repeated a previously failed source; "
                "rejecting candidate."
            )
            return source_code

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
    """Run migration-rule coverage auditing, targeted extraction, and matching."""

    print("\n========== [Stage 1] Multi-Boundary Chain Ingestion ==========")

    def normalize_set(values) -> set[str]:
        if not values:
            return set()

        return {
            normalize_api_symbol(value)
            for value in values
            if (
                isinstance(value, str)
                and normalize_api_symbol(value)
            )
        }

    def rule_key(rule: dict) -> tuple:
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

    print("\n========== Stage 2: Initial Audit ==========")
    info(f"Stage 0 symbols: {sorted(stage0_symbols)}")
    info(f"Initial rule count: {len(working_rules)}")

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

    targeted_symbols = set()

    for field in ("missing_symbols", "uncovered_symbols"):
        targeted_symbols.update(
            normalize_set(audit.get(field, []))
        )

    targeted_symbols.update(
        observed_symbols - directly_covered - already_target
    )

    if targeted_symbols:
        print("\n========== Stage 2.5: Targeted Rule Extraction ==========")
        info(f"Focus symbols: {sorted(targeted_symbols)}")

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
                    working_doc_text + "\n\n" + extra_doc_text
                ).strip()

            info(f"Targeted candidates returned: {len(extra_rules)}")
            info(f"New distinct rules added: {added_count}")

        except Exception as exc:
            warning(f"Targeted extraction failed: {exc!r}")

        audit = evaluate_relevance_and_completeness(
            stage0_symbols,
            working_rules,
            working_doc_text,
        )

    print("\n========== Stage 2: Final Relevance & Completeness Audit ==========")
    info(f"Observed: {sorted(observed_symbols)}")
    info(f"Total rules: {len(working_rules)}")
    print(json.dumps(audit, indent=2, default=str))

    relevant_rules = audit.get("relevant_records", [])
    missing = normalize_set(audit.get("missing_symbols", []))
    uncovered = normalize_set(audit.get("uncovered_symbols", []))

    is_genuinely_complete = (
        bool(audit.get("is_complete", False))
        and not missing
        and not uncovered
    )

    info(f"Coverage complete: {is_genuinely_complete}")

    with open("changes.json", "w", encoding="utf-8") as f:
        json.dump(relevant_rules, f, indent=2, default=str)

    success("Canonical contract saved in changes.json")

    print("\n========== Stage 3: Matching Usages ==========")

    # The pipeline must pass usages from the current file here.
    # Stage 0 remains global; this matching operation is file-specific.
    report = man_extract(
        relevant_rules,
        parses,
        source_file,
    )

    info(f"Matched usages: {len(report)}")
    info(f"Relevant rules: {len(relevant_rules)}")

    return report, relevant_rules, is_genuinely_complete