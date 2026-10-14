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
        normalize_api_symbol(s)
        for s in stage0_symbols
        if normalize_api_symbol(s)
    }

    old_to_new = {}
    for record in extracted_records:
        old_api = normalize_api_symbol(record.get("old_api", ""))
        new_api = normalize_api_symbol(record.get("new_api", ""))

        if old_api:
            old_to_new.setdefault(old_api, []).append(new_api)

    known_old_apis = set(old_to_new)
    known_target_apis = {
        target
        for targets in old_to_new.values()
        for target in targets
        if target
    }

    relevant_records = [
        {
            **record,
            "old_api": normalize_api_symbol(record.get("old_api", "")),
        }
        for record in extracted_records
        if normalize_api_symbol(record.get("old_api", ""))
        in observed_symbols
    ]

    # Observed symbols already recognized as migration targets are not
    # missing old-API rules.
    unresolved_symbols = observed_symbols - known_old_apis - known_target_apis

    return {
        "is_complete": not unresolved_symbols,
        "expected_affected_symbols": sorted(observed_symbols),
        "missing_symbols": sorted(unresolved_symbols),
        "relevant_records": relevant_records,
    }
