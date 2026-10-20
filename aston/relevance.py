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


def canonicalize_usage(record):
    package = record.get("package")
    alias = record.get("local_alias")
    usage = record["api_usage"]

    if alias and usage.startswith(alias + "."):
        return package + usage[len(alias):]

    return usage


def determine_expected_affected_symbols(
    stage0_symbols: set[str],
    migration_doc_text: str
) -> set[str]:
    """
    Independently checks whether Stage 0 symbols are referenced
    in the migration text.
    """
    expected = set()
    doc_lower = migration_doc_text.lower()

    for symbol in stage0_symbols:
        normalized = normalize_api_symbol(symbol)

        parts = normalized.split(".")

        if len(parts) == 1:
            short_name = parts[-1]
        else:
            short_name = f"{parts[-2]}.{parts[-1]}"

        if (
            normalized.lower() in doc_lower
            or short_name.lower() in doc_lower
        ):
            expected.add(normalized)

    return expected





def evaluate_relevance_and_completeness(
    stage0_symbols: set[str],
    extracted_records: list[dict],
    migration_doc_text: str,
) -> dict:
    observed_symbols = {
        normalize_api_symbol(symbol)
        for symbol in stage0_symbols
        if normalize_api_symbol(symbol)
    }

    # Index rules by their old and target APIs.
    old_to_records = {}
    known_target_apis = set()

    for record in extracted_records:
        old_api = normalize_api_symbol(record.get("old_api", ""))
        new_api = normalize_api_symbol(record.get("new_api", ""))

        if old_api:
            old_to_records.setdefault(old_api, []).append(record)

        if new_api:
            known_target_apis.add(new_api)

    known_old_apis = set(old_to_records)

    # Directly applicable candidates: observed source symbol matches
    # the old API of an extracted migration rule.
    relevant_records = []
    directly_covered_symbols = set()

    for symbol in observed_symbols:
        for record in old_to_records.get(symbol, []):
            relevant_records.append({
                **record,
                "old_api": normalize_api_symbol(
                    record.get("old_api", "")
                ),
            })
            directly_covered_symbols.add(symbol)

    # Symbols already recognized as migration targets are not missing
    # old-API rules.
    already_target_symbols = observed_symbols & known_target_apis

    # Keep these separate: not finding a rule does not prove that a
    # symbol needs migration.
    unclassified_symbols = (
        observed_symbols
        - directly_covered_symbols
        - already_target_symbols
    )

    return {
        # This is coverage completeness, not proof that the entire
        # migration is safe or validated.
        "is_complete": not unclassified_symbols,
        "expected_affected_symbols": sorted(observed_symbols),
        "missing_symbols": sorted(unclassified_symbols),
        "relevant_records": relevant_records,
        "directly_covered_symbols": sorted(directly_covered_symbols),
        "already_target_symbols": sorted(already_target_symbols),
        "unclassified_symbols": sorted(unclassified_symbols),
    }

