"""Discover migration documentation and extract evidence-grounded migration rules.
Candidates that cannot be safely established are preserved with
validation_status='needs_review'; they must not be automatically applied.
"""
import base64
import hashlib
import html
import json
import os
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv
from mistralai.client import Mistral
from pydantic import BaseModel, Field
from aston.changes.chain import get_migration_chain, merge_rule_chain

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

API_KEY = os.getenv("MISTRAL_API_KEY")
client = Mistral(api_key=API_KEY) if API_KEY else None

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 20

EXTRACTION_SCHEMA_VERSION = "v7-discussion-chunked-cache-safe"
CACHE_DIR = Path(".sma/rule_cache_v7")


class MigrationRule(BaseModel):
    package: str
    old_api: str
    new_api: str | None = None
    requires_instantiation: bool | None = None
    migration_type: str | None = None
    description: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    example_before: str | None = None
    example_after: str | None = None
    evidence: str | None = None
    source_url: str | None = None
    validation_status: str | None = None


class MigrationRuleset(BaseModel):
    rules: list[MigrationRule] = Field(default_factory=list)
    raw_example_blocks: list[str] = Field(default_factory=list)

prompt = """

You are an expert static-analysis and code-migration engineer.

Extract migration candidates supported by the supplied documentation only.

The caller may supply FOCUS SYMBOLS observed in the user's codebase.

For each focus symbol, actively search the supplied documentation for:

- explicit removals or deprecations;

- replacement APIs and migration examples;

- changes to imports, clients, initialization, configuration, and call structure;

- whether the symbol remains supported in the target version.

Prioritize finding evidence for each focus symbol, but do not invent

a replacement if the documentation does not establish one.

If no migration evidence exists for a focus symbol in the supplied source,

do not fabricate a rule for it. Extract other documented candidates normally.

Return each distinct documented API rename, removal, signature change,

parameter replacement, configuration change, client-instantiation change,

or coordinated migration strategy. Do not invent a replacement API.

For every rule:

- package: use the package name supplied by the user.

- old_api: old fully qualified API symbol when the source supports it.

- new_api: documented replacement, or null if no replacement is established.

- requires_instantiation: boolean or null.

- migration_type: rename, structural, initialization, configuration,
  client_migration, signature_change, removal, manual, or unknown.

- description: explain non-trivial transformations and unresolved details.

- from_version and to_version: leave null; Python assigns the actual boundary.

- evidence: an exact excerpt from the supplied source, not a paraphrase.

- source_url: only an exact URL shown in a SOURCE URL marker in the input;

  otherwise null.

- example_before / example_after: minimal snippets explicitly shown in

  the source for this rule; otherwise null.

- validation_status: use needs_review when replacement/evidence/strategy is

  uncertain; do not claim validation merely because a name looks plausible.

Preserve candidates with unknown replacements. Use null for new_api when no

replacement is documented. Do not turn a configuration migration into a simple

rename without direct supporting evidence. Preserve competing strategies as

separate candidates rather than overwriting them.

Also return raw_example_blocks: distinct before/after code blocks copied

verbatim from the source. Do not invent or complete examples. Return [] when

there are no such blocks.

Output only data matching the response schema.

"""

RENAME_CONTRADICTION_SIGNALS = (
    "via", "through", "align", "pass to", "passed to", "configure",
    "instantiat", "client", "constructor", "parameter of", "http client",
)

ALLOWED_MIGRATION_TYPES = {
    "rename", "structural", "initialization", "configuration",
    "client_migration", "signature_change", "removal", "manual", "unknown",
}

ALLOWED_VALIDATION_STATUSES = {"validated", "needs_review", "rejected"}


class ValidatedRule(BaseModel):
    old_api: str
    new_api: str | None = None
    migration_type: str | None = None
    requires_instantiation: bool | None = None
    description: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    evidence: str | None = None
    source_url: str | None = None
    validation_status: str


class ValidatedRuleset(BaseModel):
    rules: list[ValidatedRule] = Field(default_factory=list)

VALIDATOR_SYSTEM_PROMPT = """

You are a Python dependency migration rule validator. Validate every candidate

against the supplied source text and original extracted data.

Never invent APIs, source URLs, evidence, version boundaries, or examples.

Classify migration_type as one of: rename, structural, initialization,

configuration, client_migration, signature_change, removal, manual, unknown.

Set validation_status to exactly one of: validated, needs_review, rejected.

A rename is only a direct symbol substitution with the same calling convention

and surrounding behavior. If the description mentions a client, constructor,

configuration, passing a value elsewhere, or instantiation, it is not a simple

rename. If a rule's type and description contradict each other, mark it

needs_review.

Evidence must be an exact excerpt appearing in the supplied documentation.

Verbatim presence alone is insufficient if the excerpt is speculative, a

question, hedged, or an unconfirmed user comment. In those cases use

needs_review. A rule is validated only if evidence supports a concrete

replacement and the migration strategy is sufficiently established.

A missing or unresolved replacement is not grounds for rejection. Preserve

removals and configuration changes requiring investigation as needs_review;

use null for new_api when there is no documented replacement. Do not label a

rule validated solely because the proposed target symbol looks plausible.

Return one result per input candidate, preserving old_api and the input order.

Do not omit uncertain candidates. Use needs_review when uncertain. A candidate

may be rejected only when demonstrably invalid or contradicted by reliable

supplied evidence.

"""


def check_rename_contradiction(rule: dict) -> bool:
    """Detect a rename classification whose description implies structural work."""
    if (rule.get("migration_type") or "").strip().lower() != "rename":
        return False
    description = (rule.get("description") or "").lower()
    return any(signal in description for signal in RENAME_CONTRADICTION_SIGNALS)


def normalize_evidence_text(text: str) -> str:
    """Normalize whitespace and common Markdown formatting for comparison."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\\\\", "")
    text = re.sub(r"`+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def _normalise_api(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("()", "").strip()


def _is_unresolved_replacement(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    normalized = value.strip().casefold()
    return normalized.startswith(("unresolved:", "unknown:", "no replacement"))


def _candidate_key(rule: dict) -> tuple[str, str, str]:
    """Identity includes strategy so competing migrations for one symbol survive."""
    return (
        _normalise_api(rule.get("old_api")).casefold(),
        _normalise_api(rule.get("new_api")).casefold(),
        str(rule.get("migration_type") or "").strip().casefold(),
    )


def validate_and_normalize_rules(
    package: str,
    current_version: str,
    target_version: str,
    rules: list[dict],
    raw_code_examples: list[str],
    source_evidence: str = "",
) -> list[dict]:
    """Validate candidates; retain uncertain candidates with needs_review."""
    if not rules:
        print("Rule validation skipped: no extracted rules.")
        return []
    if not client:
        raise RuntimeError("MISTRAL_API_KEY is not set; cannot validate migration rules.")
    def preserve_for_review(source_rules: list[dict], reason: str) -> list[dict]:
        preserved = []
        for source_rule in source_rules:
            rule = dict(source_rule)
            # Extraction-stage status is untrusted; only the validator may reject.
            old_api = _normalise_api(rule.get("old_api"))
            if not old_api:
                continue
            new_api = rule.get("new_api")
            if _is_unresolved_replacement(new_api):
                new_api = None
            note = f"Validation could not establish trust: {reason}"
            description = rule.get("description")
            source_url = rule.get("source_url")
            if not isinstance(source_url, str) or source_url not in source_evidence:
                source_url = None
            rule.update({
                "package": package,
                "old_api": old_api,
                "new_api": new_api,
                "from_version": current_version,
                "to_version": target_version,
                "source_url": source_url,
                "validation_status": "needs_review",
                "description": f"{description.strip()} {note}" if isinstance(description, str) and description.strip() else note,
            })
            preserved.append(rule)
        return preserved
    user_prompt = f"""
PACKAGE: {package}
CURRENT VERSION: {current_version}
TARGET VERSION: {target_version}
ORIGINAL EXTRACTED RULES:
{json.dumps(rules, indent=2, default=str)}
RAW CODE EXAMPLES (context only):
{json.dumps(raw_code_examples or [], indent=2, default=str)}
SOURCE MIGRATION DOCUMENTATION:
{source_evidence or "No source documentation supplied."}
Validate each candidate independently. Preserve source metadata and exact
version boundary values supplied by the caller. Do not invent evidence or URLs.
"""
    try:
        response = client.chat.parse(
            model="codestral-latest",
            messages=[
                {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=ValidatedRuleset,
            temperature=0,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            print("Validator returned no parsed result; preserving candidates as needs_review.")
            return preserve_for_review(rules, "the validator returned no parsed result")
        # Match validator output back to source candidates. If the validator
        # omitted any input, preserve it for review instead of silently losing it.
        unmatched = list(enumerate(rules))
        matched_results: list[tuple[dict, dict]] = []
        for validated_model in parsed.rules:
            validated = validated_model.model_dump()
            key = _candidate_key(validated)
            match_index = next(
                (i for i, (_, source) in enumerate(unmatched) if _candidate_key(source) == key),
                None,
            )
            if match_index is None:
                old_key = _normalise_api(validated.get("old_api")).casefold()
                match_index = next(
                    (i for i, (_, source) in enumerate(unmatched)
                     if _normalise_api(source.get("old_api")).casefold() == old_key),
                    None,
                )
            if match_index is None:
                print(f"Validator returned an unmatched candidate {validated.get('old_api')!r}; ignoring extra output.")
                continue
            _, source = unmatched.pop(match_index)
            matched_results.append((source, validated))
        output: list[dict] = []
        rejected = 0
        omitted = [source for _, source in unmatched]
        for source, validated in matched_results:
            # Keep identity and source metadata from the original extraction.
            rule = dict(source)
            rule.update({k: v for k, v in validated.items() if v is not None})
            rule["package"] = package
            rule["old_api"] = _normalise_api(source.get("old_api"))
            rule["from_version"] = current_version
            rule["to_version"] = target_version
            status = rule.get("validation_status")
            if status not in ALLOWED_VALIDATION_STATUSES:
                status = "needs_review"
            if status == "rejected":
                rejected += 1
                continue
            # Do not let model output replace known source metadata with invented values.
            source_url = source.get("source_url")
            candidate_url = validated.get("source_url")
            if candidate_url and candidate_url in source_evidence:
                rule["source_url"] = candidate_url
            else:
                rule["source_url"] = source_url if isinstance(source_url, str) and source_url in source_evidence else None
            evidence = rule.get("evidence")
            normalized_evidence = normalize_evidence_text(evidence or "")
            normalized_source = normalize_evidence_text(source_evidence)
            evidence_is_grounded = bool(normalized_evidence and normalized_source and normalized_evidence in normalized_source)
            reasons = []
            if check_rename_contradiction(rule):
                reasons.append("Rename classification conflicts with a structural/configuration description.")
            if not evidence_is_grounded:
                reasons.append("Evidence could not be verified verbatim against the source documentation.")
            if rule.get("migration_type") not in ALLOWED_MIGRATION_TYPES:
                reasons.append("Migration type is missing or outside the supported classifications.")
            if rule.get("migration_type") in {"removal", "manual", "unknown"}:
                reasons.append("This migration strategy requires manual review.")
            if _is_unresolved_replacement(rule.get("new_api")):
                rule["new_api"] = None
                reasons.append("No concrete replacement API has been established.")
            if reasons:
                status = "needs_review"
            rule["validation_status"] = status
            if reasons:
                description = rule.get("description") or ""
                rule["description"] = (description + "\nValidation concerns: " + " ".join(reasons)).strip()
            if rule.get("old_api"):
                # Only copy known fields, preserving the existing schema.
                output.append({
                    "package": package,
                    "old_api": rule["old_api"],
                    "new_api": rule.get("new_api"),
                    "migration_type": rule.get("migration_type"),
                    "requires_instantiation": rule.get("requires_instantiation"),
                    "description": rule.get("description"),
                    "from_version": current_version,
                    "to_version": target_version,
                    "example_before": source.get("example_before"),
                    "example_after": source.get("example_after"),
                    "evidence": rule.get("evidence"),
                    "source_url": rule.get("source_url"),
                    "validation_status": status,
                })
        if omitted:
            print(f"Validator omitted {len(omitted)} input candidate(s); preserving them as needs_review.")
            output.extend(preserve_for_review(omitted, "the validator omitted this input candidate"))
        print("\n========== Rule Validation Summary ==========")
        print(f"Candidate rules: {len(rules)}")
        print(f"Validated: {sum(r.get('validation_status') == 'validated' for r in output)}")
        print(f"Needs review: {sum(r.get('validation_status') == 'needs_review' for r in output)}")
        print(f"Rejected: {rejected}")
        print("==============================================\n")
        return output
    except Exception as exc:
        print(f"Rule validation failed: {exc}")
        print("Preserving extracted candidates as needs_review; none will be marked validated by this fallback.")
        return preserve_for_review(rules, f"validator failure: {type(exc).__name__}: {exc}")


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Aston-dependency-migration-tool",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _cache_path(
    package: str,
    tag: str,
    doc_text: str,
    stage0_symbols: set[str],
    current_version: str = "",
    target_version: str = "",
) -> Path:
    """Cache by schema, evidence, symbols, and the exact version transition."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest_input = "\n".join((
        EXTRACTION_SCHEMA_VERSION,
        f"current={current_version}",
        f"target={target_version}",
        doc_text,
        "--SYMBOLS--",
        "\n".join(sorted(stage0_symbols)),
    )).encode("utf-8", errors="replace")
    digest = hashlib.sha256(digest_input).hexdigest()[:16]
    safe_package = re.sub(r"[^A-Za-z0-9_.-]", "_", package)
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    return CACHE_DIR / f"{safe_package}__{safe_tag}__{digest}.json"


def should_extract(doc_text: str, stage0_symbols: set[str]) -> bool:
    """Use specific migration signals or a scanned-symbol match as a cheap gate."""
    if not doc_text or len(doc_text.strip()) < 30:
        return False
    doc_lower = doc_text.lower()
    migration_indicators = (
        "migration", "upgrade guide", "breaking change", "breaking changes",
        "changelog", "deprecated", "deprecation", "no longer supported",
        "removed", "renamed", "signature change", "migration guide",
    )
    if any(indicator in doc_lower for indicator in migration_indicators):
        return True
    if not stage0_symbols:
        return True
    for symbol in stage0_symbols:
        normalized = symbol.strip().lower()
        if normalized and normalized in doc_lower:
            return True
        leaf = normalized.rsplit(".", 1)[-1]
        if len(leaf) >= 4 and re.search(rf"(?\<![a-z0-9_]){re.escape(leaf)}(?![a-z0-9_])", doc_lower):
            return True
    return False


def normalize_rule_symbols(rules: list[dict]) -> list[dict]:
    normalized = []
    for source in rules:
        rule = dict(source)
        for key in ("old_api", "new_api"):
            value = rule.get(key)
            if isinstance(value, str):
                rule[key] = value.replace("()", "").strip()
        normalized.append(rule)
    return normalized


def _validate_raw_blocks(blocks: list[str], source_text: str) -> list[str]:
    normalized_source = re.sub(r"\s+", " ", source_text).casefold()
    result = []
    seen = set()
    for block in blocks:
        cleaned = block.strip()
        normalized_block = re.sub(r"\s+", " ", cleaned).casefold()
        if cleaned and normalized_block in normalized_source and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def _split_document_chunks(
    doc_text: str,
    max_chars: int = 12000,
    overlap: int = 1200,
) -> list[tuple[int, str]]:
    """Split documentation into overlapping chunks without dropping the tail."""
    if not doc_text:
        return []
    max_chars = max(1000, max_chars)
    overlap = max(0, min(overlap, max_chars // 3))
    chunks: list[tuple[int, str]] = []
    start = 0
    index = 0
    text_length = len(doc_text)
    while start < text_length:
        end = min(text_length, start + max_chars)
        if end < text_length:
            # Prefer ending at a paragraph/line boundary, but never make a tiny chunk.
            boundary = max(doc_text.rfind("\n\n", start + max_chars // 2, end),
                           doc_text.rfind("\n", start + max_chars // 2, end))
            if boundary > start + max_chars // 2:
                end = boundary
        chunk = doc_text[start:end].strip()
        if chunk:
            chunks.append((index, chunk))
            index += 1
        if end >= text_length:
            break
        next_start = max(start + 1, end - overlap)
        start = next_start
    return chunks


def _chunk_priority(chunk: str, focus_symbols: set[str]) -> int:
    """Higher scores mean the chunk directly mentions more observed symbols."""
    lower = chunk.casefold()
    return sum(1 for symbol in focus_symbols if symbol.casefold() in lower)


def _rule_fallback_detected(rules: list[dict]) -> bool:
    """Recognize validator fallback candidates so they are returned but not cached."""
    markers = (
        "validation could not establish trust",
        "validator failure:",
        "validator returned no parsed result",
    )
    for rule in rules:
        description = str(rule.get("description") or "").casefold()
        if any(marker in description for marker in markers):
            return True
    return False


def _validation_evidence_for_rules(
    doc_text: str,
    rules: list[dict],
    max_chars: int = 30000,
) -> str:
    """Build a bounded evidence bundle for validation without discarding late sources."""
    if not doc_text:
        return ""
    folded = doc_text.casefold()
    source_marker = "=== SOURCE URL:"
    snippets: list[str] = []
    seen: set[str] = set()
    total = 0
    for rule in rules:
        evidence = rule.get("evidence")
        old_api = str(rule.get("old_api") or "").strip()
        position = -1
        span = 0
        if isinstance(evidence, str) and evidence.strip():
            position = folded.find(evidence.strip().casefold())
            if position >= 0:
                span = len(evidence.strip())
        if position < 0 and old_api:
            position = folded.find(old_api.casefold())
            span = len(old_api)
        if position < 0:
            continue
        marker_position = doc_text.rfind(source_marker, 0, position)
        start = marker_position if marker_position >= 0 else max(0, position - 500)
        end = min(len(doc_text), position + max(span, 1) + 500)
        snippet = doc_text[start:end].strip()
        normalized = re.sub(r"\s+", " ", snippet).casefold()
        if not snippet or normalized in seen:
            continue
        if total + len(snippet) > max_chars:
            # Keep evidence atomic; don't truncate an excerpt mid-claim.
            continue
        snippets.append(snippet)
        seen.add(normalized)
        total += len(snippet)
    if not snippets:
        return doc_text[:min(50000, max_chars)]
    return "\n\n--- VALIDATION EVIDENCE EXCERPT ---\n\n".join(snippets)


def _github_discussion_markdown(url: str) -> str | None:
    """Fetch a GitHub Discussion's Markdown body through the GraphQL API."""
    parsed = urlparse(url)
    if parsed.hostname != "github.com" or not GITHUB_TOKEN:
        return None
    match = re.match(r"^/([^/]+)/([^/]+)/discussions/(\d+)(?:/|$)", parsed.path)
    if not match:
        return None
    owner, repository, number = match.groups()
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        discussion(number: $number) { title body url }
      }
    }
    """
    try:
        response = requests.post(
            "https://api.github.com/graphql",
            headers={**_github_headers(), "Accept": "application/vnd.github+json"},
            json={"query": query, "variables": {
                "owner": owner, "name": repository, "number": int(number)
            }},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            print(f"GitHub GraphQL returned {response.status_code} for {url}")
            return None
        payload = response.json()
        if payload.get("errors"):
            print(f" GitHub GraphQL could not retrieve discussion {url}: {payload['errors']}")
            return None
        discussion = ((payload.get("data") or {}).get("repository") or {}).get("discussion")
        if not discussion or not isinstance(discussion.get("body"), str):
            return None
        title = (discussion.get("title") or "").strip()
        body = discussion["body"].strip()
        return (f"# {title}\n\n{body}" if title else body) or None
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"GitHub Discussion GraphQL fetch failed for {url}: {exc}")
        return None


def _changelog_section_for_tag(content: str, tag: str) -> str | None:
    """Return the Markdown heading section corresponding to a release tag."""
    version_text = tag.strip()
    if version_text.startswith(("v", "V")):
        version_text = version_text[1:]
    escaped_version = re.escape(version_text)
    version_pattern = re.compile(
        rf"(?<![A-Za-z0-9])v?{escaped_version}(?![A-Za-z0-9])", re.IGNORECASE
    )
    headings = list(re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)\s*$", content))
    for position, heading in enumerate(headings):
        heading_text = heading.group(2)
        if not version_pattern.search(heading_text):
            continue
        level = len(heading.group(1))
        section_end = len(content)
        for following in headings[position + 1:]:
            if len(following.group(1)) <= level:
                section_end = following.start()
                break
        section = content[heading.start():section_end].strip()
        return section or None
    return None


def extract_rules(
    package: str,
    doc_text: str,
    focus_symbols: set[str] | None = None,
) -> MigrationRuleset:
    """Extract evidence-grounded rules across the full document in focused chunks."""
    if not client:
        raise RuntimeError(
            "MISTRAL_API_KEY is not set; cannot extract migration rules."
        )
    if not doc_text or len(doc_text.strip()) < 30:
        return MigrationRuleset()
    focus_symbols = {
        symbol.strip()
        for symbol in (focus_symbols or set())
        if isinstance(symbol, str) and symbol.strip()
    }
    chunks = _split_document_chunks(doc_text)
    if focus_symbols:
        # Process the most relevant chunks first, but still process every chunk.
        chunks.sort(key=lambda item: (-_chunk_priority(item[1], focus_symbols), item[0]))
    print(
        f"Extraction input: {len(doc_text)} total characters across "
        f"{len(chunks)} chunks; focus symbols: {len(focus_symbols)}"
    )
    lower_doc = doc_text.casefold()
    for symbol in sorted(focus_symbols):
        position = lower_doc.find(symbol.casefold())
        if position >= 0:
            print(f"  Found {symbol!r} in combined docs at character {position}")
        else:
            print(f"{symbol!r} does not appear literally in combined docs")
    focus_section = (
        "\n".join(f"- {symbol}" for symbol in sorted(focus_symbols))
        if focus_symbols else "(No specific focus symbols supplied.)"
    )
    combined_rules: list[dict] = []
    seen_rule_keys: set[tuple[str, str, str]] = set()
    combined_blocks: list[str] = []
    seen_blocks: set[str] = set()
    for chunk_number, chunk_text in chunks:
        print(f"Extracting chunk {chunk_number + 1}/{len(chunks)} ({len(chunk_text)} chars)")
        response = client.chat.parse(
            model="codestral-latest",
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": (
                        f"Package name: {package}\n\n"
                        "FOCUS SYMBOLS OBSERVED IN THE CODEBASE:\n"
                        f"{focus_section}\n\n"
                        f"DOCUMENT CHUNK: {chunk_number + 1} of {len(chunks)}\n"
                        "Extract only rules supported by this chunk. Prioritize evidence "
                        "relevant to the focus symbols. Version boundaries are assigned "
                        "by Python. Do not invent replacements when the source is uncertain.\n\n"
                        f"Documentation chunk:\n{chunk_text}"
                    ),
                },
            ],
            response_format=MigrationRuleset,
            temperature=0,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(
                f"Codestral returned no parsed ruleset for {package}, chunk {chunk_number + 1}"
            )
        normalized = normalize_rule_symbols(
            [rule.model_dump() for rule in parsed.rules]
        )
        for rule in normalized:
            rule["package"] = package
            source_url = rule.get("source_url")
            if not isinstance(source_url, str) or source_url not in chunk_text:
                rule["source_url"] = None
            key = _candidate_key(rule)
            if key not in seen_rule_keys:
                seen_rule_keys.add(key)
                combined_rules.append(rule)
        for block in _validate_raw_blocks(parsed.raw_example_blocks, chunk_text):
            normalized_block = re.sub(r"\s+", " ", block).strip().casefold()
            if normalized_block and normalized_block not in seen_blocks:
                seen_blocks.add(normalized_block)
                combined_blocks.append(block)
    return MigrationRuleset(
        rules=[MigrationRule.model_validate(rule) for rule in combined_rules],
        raw_example_blocks=combined_blocks,
    )


def extract_rules_cached(
    package: str,
    tag: str,
    doc_text: str,
    stage0_symbols: set[str],
    current_version: str,
    target_version: str,
) -> MigrationRuleset:
    """Extract and validate rules; cache only nonempty, non-fallback results."""
    cache_file = _cache_path(
        package, tag, doc_text, stage0_symbols, current_version, target_version
    )
    if cache_file.exists():
        try:
            cached = MigrationRuleset.model_validate_json(
                cache_file.read_text(encoding="utf-8")
            )
            for rule in cached.rules:
                rule.from_version = current_version
                rule.to_version = target_version
            return cached
        except Exception as exc:
            print(f"Invalid cache for {tag}; re-extracting ({exc})")
    if not should_extract(doc_text, stage0_symbols):
        print(
            f"{tag}: no relevant migration signal or scanned symbol; "
            "skipping LLM extraction without caching the empty result."
        )
        return MigrationRuleset()
    raw = extract_rules(package, doc_text, focus_symbols=stage0_symbols)
    if not raw.rules:
        print(f"{tag}: extraction returned no rules; result will not be cached.")
        return raw
    raw_dicts = []
    for model in raw.rules:
        rule = model.model_dump()
        rule["package"] = package
        rule["from_version"] = current_version
        rule["to_version"] = target_version
        raw_dicts.append(rule)

    validated_dicts = validate_and_normalize_rules(
        package=package,
        current_version=current_version,
        target_version=target_version,
        rules=[r.model_dump() for r in raw.rules],
        raw_code_examples=raw.raw_example_blocks,
        source_evidence=doc_text,
    )

    result = MigrationRuleset(
        rules=[MigrationRule.model_validate(r) for r in validated_dicts],
        raw_example_blocks=raw.raw_example_blocks,
    )
    
    # Only cache if validation succeeded cleanly (no fallback errors or mass needs_review)
    has_fallback_warning = any("Validation could not establish trust" in str(r.get("description")) for r in validated_dicts)
    if not has_fallback_warning:
        cache_file.write_text(result.model_dump_json(indent=2))
    else:
        print(f"Skipping cache write for {tag} due to validation fallback/concerns.")

    return result


def get_installed_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def get_latest_pypi_version(package_name: str) -> str | None:
    try:
        response = requests.get(
            f"https://pypi.org/pypi/{package_name}/json",
            timeout=(3, 5),
        )
        response.raise_for_status()
        return response.json().get("info", {}).get("version")
    except (requests.RequestException, ValueError) as exc:
        print(f"PyPI lookup failed for {package_name}: {exc}")
        return None


def resolve_github_repo(package_name: str) -> Optional[str]:
    response = requests.get(f"https://pypi.org/pypi/{package_name}/json", timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        return None
    project_urls = response.json().get("info", {}).get("project_urls") or {}
    for target_url in project_urls.values():
        match = re.search(r"github\.com/([^/]+/[^/]+)", target_url)
        if match:
            return match.group(1).removesuffix(".git").rstrip("/")
    return None


def is_candidate_url(url: str) -> bool:
    """Permit major migration discussions, changelogs, and upgrade guides,
    while continuing to filter out noisy individual bug reports or PR diffs."""
    lower = url.lower()
    
    # Block noisy PRs and issue comments, but KEEP discussions and migration pages
    if any(path in lower for path in ("/issues/", "/pull/")):
        return False

    return any(
        term in lower for term in ("migration", "upgrade", "breaking", "changelog", "changes", "discussions")
    ) or lower.endswith((".md", ".rst"))


def extract_candidate_urls_from_text(text: str) -> list[str]:
    pattern = r"https?://[^\s<>]+"
    urls = {item.rstrip(".,;:!?)]}") for item in re.findall(pattern, text)}
    return sorted(url for url in urls if is_candidate_url(url))


def fetch_page_content(url: str) -> str | None:
    """Fetch a linked page, using raw GitHub Discussion Markdown when possible."""
    parsed = urlparse(url)
    if parsed.hostname == "github.com" and re.search(
        r"/discussions/\d+(?:/|$)", parsed.path
    ):
        markdown = _github_discussion_markdown(url)
        if markdown:
            print(f"  ✓ Retrieved GitHub Discussion Markdown ({len(markdown)} characters)")
            return markdown
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Aston-dependency-migration-tool"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            print(f"HTTP {response.status_code} while fetching {url}")
            return None
        page = response.text
        # GitHub's article contains the useful body; discard page chrome when possible.
        article = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", page)
        if article:
            page = article.group(1)
        page = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>", " ", page)
        page = re.sub(
            r"(?i)<br\s*/?>|</(p|div|li|pre|h[1-6]|blockquote|tr)\s*>",
            "\n", page,
        )
        page = re.sub(r"<[^>]+>", " ", page)
        page = html.unescape(page)
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        cleaned = "\n".join(lines)
        # This cap is only for HTML fallback; GraphQL Discussion Markdown is not truncated.
        return cleaned[:100000]
    except requests.RequestException as exc:
        print(f"Failed to fetch {url}: {exc}")
        return None


def collect_linked_sources(initial_text: str, max_depth: int = 2) -> list[str]:
    """Collect linked docs with explicit URL markers and a strict depth limit."""
    collected: list[str] = []
    visited: set[str] = set()
    current_level: list[tuple[str | None, str]] = [(None, initial_text)]
    for depth in range(max_depth + 1):
        next_level: list[tuple[str | None, str]] = []
        for source_url, source_text in current_level:
            if source_url:
                collected.append(f"=== SOURCE URL: {source_url} ===\n{source_text}")
            else:
                collected.append(source_text)
            # Do not fetch a level whose content would be outside max_depth.
            if depth >= max_depth:
                continue
            for url in extract_candidate_urls_from_text(source_text):
                if url in visited:
                    continue
                visited.add(url)
                print(f"  🔗 Following migration link (depth {depth + 1}): {url}")
                content = fetch_page_content(url)
                if content and len(content) > 100:
                    print(f"    ✓ Retrieved {len(content)} characters")
                    next_level.append((url, content))
        current_level = next_level
        if not current_level:
            break
    return collected


def _fetch_repo_file(repo: str, tag: str, filename: str) -> str | None:
    url = f"{GITHUB_API}/repos/{repo}/contents/{filename}"
    response = requests.get(
        url,
        params={"ref": tag},
        headers=_github_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        return None
    data = response.json()
    if data.get("encoding") == "base64" and data.get("content"):
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
    download_url = data.get("download_url")
    if download_url:
        raw = requests.get(download_url, timeout=REQUEST_TIMEOUT)
        if raw.status_code == 200:
            return raw.text
    return None


def discover_release_doc(repo: str, tag: str) -> str | None:
    """Retrieve release notes with robust fallback to changelog and migration files."""
    release_url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    body = ""
    try:
        response = requests.get(release_url, headers=_github_headers(), timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            body = response.json().get("body", "")
    except requests.RequestException as exc:
        print(f"Release lookup failed for {tag}: {exc}")

    # Always check repository files if release body is short or missing comprehensive migration text
    if not body or len(body.strip()) < 200 or "migration" not in body.lower():
        print(f"Checking repository migration files for {tag}...")
        candidates = (
            "MIGRATION.md", "migration.md", "CHANGELOG.md", "CHANGELOG.rst",
            "CHANGES.md", "docs/migration.md", "docs/MIGRATION.md", "docs/CHANGELOG.md",
        )
        found = []
        for filename in candidates:
            try:
                content = _fetch_repo_file(repo, tag, filename)
            except Exception:
                continue
            if content and len(content.strip()) > 50:
                found.append(f"=== SOURCE FILE: {filename} @ {tag} ===\n{content}")
        if found:
            body = "\n\n".join(found) + "\n\n" + body

    if not body or len(body.strip()) < 20:
        print(f"Unresolved boundary: no documentation source found for {tag}")
        return None

    print(f"Ingested documentation source for tag: {tag}")
    linked_docs = collect_linked_sources(body, max_depth=2)
    return "\n\n--- NEXT DOCUMENT IN CHAIN ---\n\n".join(linked_docs)[:50000]


def run_stage1_pipeline(
    package_name: str,
    current_version: str | None = None,
    target_version: str | None = None,
    stage0_symbols: set[str] | None = None,
) -> tuple[list[dict], str, list[dict]]:
    """Return (merged_rules, combined_docs, boundary-tagged raw examples)."""
    if not current_version or not target_version:
        raise ValueError("current_version and target_version are required for migration-chain resolution.")
    repo = resolve_github_repo(package_name)
    if not repo:
        print(f"Failed to resolve GitHub repository for package {package_name}")
        return [], "", []
    print(f"Resolving migration chain for '{package_name}' ({current_version} -> {target_version}) @ {repo}")
    try:
        tags_in_chain = get_migration_chain(repo, current_version, target_version)
    except (requests.RequestException, ValueError) as exc:
        print(f"Could not resolve migration chain: {exc}")
        return [], "", []
    if not tags_in_chain:
        print("No version transition to process.")
        return [], "", []
    print(f"Target Version Boundaries: {tags_in_chain}")
    rulesets_in_order: list[list[dict]] = []
    migration_docs: list[str] = []
    all_raw_example_blocks: list[dict] = []
    unresolved_boundaries: list[str] = []
    boundary_from_version = current_version
    for tag in tags_in_chain:
        print(f"\nIngesting boundary: {tag}...")
        doc_content = discover_release_doc(repo, tag)
        if not doc_content:
            unresolved_boundaries.append(tag)
            # Keep chain progression deterministic even if this boundary's docs are missing.
            boundary_from_version = tag
            continue
        migration_docs.append(f"=== MIGRATION BOUNDARY: {boundary_from_version} -> {tag} ===\n{doc_content}")
        ruleset = extract_rules_cached(
            package_name,
            tag,
            doc_content,
            stage0_symbols or set(),
            boundary_from_version,
            tag,
        )
        rulesets_in_order.append([rule.model_dump() for rule in ruleset.rules])
        for block in ruleset.raw_example_blocks:
            all_raw_example_blocks.append({
                "boundary": tag,
                "source": "release_notes_or_repository_migration_docs",
                "code": block,
            })
        validated_count = sum(rule.validation_status == "validated" for rule in ruleset.rules)
        review_count = sum(rule.validation_status == "needs_review" for rule in ruleset.rules)
        print(f"  └─ Processed {len(ruleset.rules)} candidates for {tag}: {validated_count} validated, {review_count} needs_review")
        boundary_from_version = tag
    if unresolved_boundaries:
        print("Unresolved documentation boundaries: " + ", ".join(unresolved_boundaries))
        print("Coverage is incomplete until these boundaries have usable sources.")
    merged_rules = merge_rule_chain(rulesets_in_order)
    print("merged_rules", merged_rules)
    migration_doc_text = "\n\n".join(migration_docs)
    print(f"\nRule Chain Composition Complete. Total Merged Rules: {len(merged_rules)}")
    # print(f"Combined Migration Documentation: {len(migration_doc_text)} characters")
    
    return merged_rules, migration_doc_text, all_raw_example_blocks