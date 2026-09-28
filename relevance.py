import re

def normalize_api_symbol(symbol: str) -> str:
    """
    Normalize API symbols so syntactic differences such as
    'foo.bar()' vs 'foo.bar' do not cause false mismatches.
    """
    symbol = symbol.strip()

    # Remove trailing call parentheses.
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
    migration_doc_text: str
) -> dict:
    """
    Non-circular audit.

    Stage 0 determines what the codebase actually uses.
    Migration documentation independently determines which of
    those usages appear relevant.
    LLM extraction is then compared against that expected set.
    """

    expected_affected = determine_expected_affected_symbols(
        stage0_symbols,
        migration_doc_text
    )

    extracted_old_apis = {
        normalize_api_symbol(rec["old_api"])
        for rec in extracted_records
    }

    missing_symbols = expected_affected - extracted_old_apis

    is_complete = len(missing_symbols) == 0

    relevant_records = []

    for rec in extracted_records:
        normalized_old_api = normalize_api_symbol(rec["old_api"])

        if normalized_old_api in expected_affected:
            # Normalize the record too so Stage 3 receives
            # the same representation as Stage 0.
            normalized_record = rec.copy()
            normalized_record["old_api"] = normalized_old_api
            relevant_records.append(normalized_record)

    return {
        "is_complete": is_complete,
        "expected_affected_symbols": list(expected_affected),
        "missing_symbols": list(missing_symbols),
        "relevant_records": relevant_records
    }
