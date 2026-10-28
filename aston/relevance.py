import re

def normalize_api_symbol(symbol: str) -> str:
    """
    Normalize formatting without changing Python identifier casing.
    """
    if not isinstance(symbol, str):
        return ""

    symbol = symbol.strip()

    if symbol.endswith("()"):
        symbol = symbol[:-2]

    return symbol


def canonicalize_usage(record: dict) -> str:
    """
    Convert a locally aliased API usage to its fully qualified form,
    correctly resolving from-imports using the explicit 'imported' field.
    """
    package = record.get("package")
    alias = record.get("local_alias")
    imported = record.get("imported")
    usage = record.get("api_usage", "")

    if not isinstance(usage, str):
        return ""

    if alias and usage.startswith(alias + "."):
        if imported:
            return imported + usage[len(alias):]
        elif package:
            return package + usage[len(alias):]

    return usage


def determine_expected_affected_symbols(
    stage0_symbols: set[str],
    migration_doc_text: str,
) -> set[str]:
    """
    Find scanned symbols explicitly referenced in the migration text.
    """
    expected = set()
    doc_lower = (migration_doc_text or "").lower()

    for symbol in stage0_symbols:
        normalized = normalize_api_symbol(symbol)
        if not normalized:
            continue

        parts = normalized.split(".")
        short_name = ".".join(parts[-2:])

        if (
            normalized.lower() in doc_lower
            or short_name.lower() in doc_lower
        ):
            expected.add(normalized)

    return expected


def find_longest_prefix_match(symbol: str, rules_dict: dict) -> list[dict]:
    """
    Find matching rules using longest-prefix matching.
    E.g., rule for 'openai.ChatCompletion' will match 'openai.ChatCompletion.create'.
    """
    # Sort old_apis by length descending so longer/more specific prefixes match first
    sorted_old_apis = sorted(rules_dict.keys(), key=len, reverse=True)
    for old_api in sorted_old_apis:
        if symbol == old_api or symbol.startswith(old_api + "."):
            return rules_dict[old_api]
    return []


def evaluate_relevance_and_completeness(
    stage0_symbols: set[str],
    extracted_records: list[dict],
    migration_doc_text: str,
) -> dict:
    """
    Evaluate migration-rule coverage using longest-prefix matching and
    strict validated-only target sets.
    """
    observed_symbols = {
        normalize_api_symbol(symbol)
        for symbol in stage0_symbols
        if normalize_api_symbol(symbol)
    }

    # Index extracted rules by old API
    old_to_records = {}
    validated_target_apis = set()

    auto_migration_statuses = {"validated", "valid"}

    for record in extracted_records or []:
        old_api = normalize_api_symbol(record.get("old_api", ""))
        new_api = normalize_api_symbol(record.get("new_api", ""))
        status = str(record.get("validation_status", "")).strip().lower()

        if old_api:
            old_to_records.setdefault(old_api, []).append(record)

        # Item 8: Build known target APIs ONLY from validated rules
        if new_api and status in auto_migration_statuses:
            validated_target_apis.add(new_api)

    relevant_records = []
    review_records = []
    directly_covered_symbols = set()
    review_covered_symbols = set()

    for symbol in observed_symbols:
        matching_records = find_longest_prefix_match(symbol, old_to_records)
        if not matching_records:
            continue

        for record in matching_records:
            status = str(record.get("validation_status", "")).strip().lower()

            if status in auto_migration_statuses:
                relevant_records.append({
                    **record,
                    "old_api": normalize_api_symbol(record.get("old_api", "")),
                })
                directly_covered_symbols.add(symbol)
            else:
                review_records.append({
                    **record,
                    "old_api": normalize_api_symbol(record.get("old_api", "")),
                })
                review_covered_symbols.add(symbol)

    # Item 8: Target compatibility established strictly from validated rules
    known_target_symbols = observed_symbols & validated_target_apis

    expected_affected_symbols = determine_expected_affected_symbols(
        observed_symbols,
        migration_doc_text,
    )

    uncovered_symbols = (
        observed_symbols
        - directly_covered_symbols
        - known_target_symbols
    )

    missing_documented_symbols = (
        expected_affected_symbols
        - directly_covered_symbols
        - known_target_symbols
    )

    is_complete = (
        not uncovered_symbols
        and not review_covered_symbols
    )

    return {
        "is_complete": is_complete,
        "observed_symbols": sorted(observed_symbols),
        "expected_affected_symbols": sorted(expected_affected_symbols),
        "directly_covered_symbols": sorted(directly_covered_symbols),
        "already_target_symbols": sorted(known_target_symbols),
        "review_covered_symbols": sorted(review_covered_symbols),
        "uncovered_symbols": sorted(uncovered_symbols),
        "missing_symbols": sorted(missing_documented_symbols),
        "relevant_records": relevant_records,
        "review_records": review_records,
    }