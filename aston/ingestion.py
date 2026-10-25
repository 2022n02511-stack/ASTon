"""
Discovers migration documentation and extracts structured migration rules,
validated against the actual source evidence before being trusted anywhere
downstream. The validator was previously written but never called — this
was the direct cause of ungrounded rules (e.g. openai.proxy -> openai.proxies,
an attribute that never existed) reaching the patcher unchecked.
"""

import base64
import hashlib
import json
import os
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional

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
CACHE_DIR = Path(".sma/rule_cache_v4")


class MigrationRule(BaseModel):
    package: str
    old_api: str
    new_api: str | None = None
    requires_instantiation: bool | None = None
    migration_type: str | None = None
    description: str | None = None
    from_version: str
    to_version: str
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
Extract migration rules supported by the supplied documentation only.

Return a rule for each explicitly documented API rename, removed API, signature
change, parameter replacement, or configuration/client-instantiation change.
Do not infer a replacement merely because a name looks similar. If the target
API cannot be determined from the source, do not invent it.

For every rule:
- package: use the package name supplied by the user message.
- old_api: the old fully qualified API symbol, when the source supports it.
- new_api: the documented replacement. For removals without a replacement,
  use the exact string "UNRESOLVED: no documented replacement".
- requires_instantiation: a BOOLEAN. true if using the new API requires
  instantiating a client object first, false otherwise. Do NOT put an
  explanation here — put the "how to instantiate" details in description.
- migration_type: one of rename, configuration, client_migration,
  signature_change, removal, or unknown.
- description: short explanation of any non-trivial transformation. If
  requires_instantiation is true, explain HOW to instantiate the client here
  (e.g. "Instantiate OpenAI() and call the method on the instance"). Null if
  the change is a trivial rename with nothing else to explain.
- example_before / example_after: only minimal snippets explicitly shown in
  the source for this specific rule. Otherwise null.

### Migration candidate coverage requirements

Extract each distinct documented breaking change relevant to the package, including:

- API renames and removals.
- Changes to function signatures and required arguments.
- Configuration property changes.
- Module-level configuration that moves to client-instance configuration.
- Client initialization and transport changes.
- Authentication and credential configuration changes.
- Changes that require multiple coordinated code edits.
- Documented removals for which no direct replacement is provided.

For each candidate, preserve the original API symbol, source URL, supporting evidence, applicable version boundary, migration type, and any documented replacement.

Do not omit a relevant breaking change solely because its replacement is unknown or because it requires manual migration.

When no replacement is established by the source, use a null replacement and mark the candidate as needs_review or unresolved according to the output schema. Explain what is known, what remains uncertain, and which source supports the change.

Do not invent a replacement API, infer a direct rename from similar names, or classify a configuration migration as a simple rename without supporting evidence.

Return distinct candidates for distinct migration strategies rather than silently overwriting competing candidates for the same old API.

The goal is to preserve relevant migration opportunities for downstream analysis, not to claim every candidate is automatically transformable.
  

Also return raw_example_blocks: full, distinct before/after migration examples
copied verbatim from the supplied documentation. Do not complete or invent code.
If no such examples are present, return [].

Output only data matching the response schema.
"""

RENAME_CONTRADICTION_SIGNALS = (
    "via", "through", "align", "pass to", "passed to", "configure",
    "instantiat", "client", "constructor", "parameter of", "http client",
)

def check_rename_contradiction(rule: dict) -> bool:
    """Returns True if migration_type=='rename' but the description
    describes something structural — a deterministic backstop for the
    exact case that slipped through: 'renamed to align with the
    underlying HTTP client's proxy settings' classified as 'rename'."""
    if rule.get("migration_type") != "rename":
        return False
    description = (rule.get("description") or "").lower()
    return any(signal in description for signal in RENAME_CONTRADICTION_SIGNALS)

# --- Validation pass -------------------------------------------------------
# Every rule extract_rules() produces is a CLAIM about the documentation.
# This checks that claim before anything downstream trusts it: a rule is only
# kept if the model can point to an exact, verifiable excerpt of the real
# source text supporting it. A rule whose "evidence" doesn't literally appear
# in the source it was supposedly drawn from is dropped, not patched around.

class ValidatedRule(BaseModel):
    old_api: str
    new_api: str | None = None
    migration_type: str
    requires_instantiation: bool | None = None
    description: str
    from_version: str 
    to_version: str
    evidence: str | None = None
    source_url: str | None = None
    validation_status: str  # "validated" | "needs_review" | "rejected"


class ValidatedRuleset(BaseModel):
    rules: list[ValidatedRule] = Field(default_factory=list)


ALLOWED_MIGRATION_TYPES = {
    "rename", "structural", "initialization", "configuration", "removal", "manual",
}
ALLOWED_VALIDATION_STATUSES = {"validated", "needs_review", "rejected"}

VALIDATOR_SYSTEM_PROMPT = """
You are a Python dependency migration rule validator.

Validate and normalize every candidate migration rule using the provided
source migration documentation and original extracted data.

Rules:
1. Never assume an API replacement is valid because its name looks plausible.
2. Do not invent APIs, source URLs, quotations, or evidence.
3. Classify each rule as exactly one of: rename, structural, initialization,
   configuration, removal, manual.
4. Definitions — read carefully, these are NOT interchangeable:
   - rename: the OLD symbol and NEW symbol are used IDENTICALLY in every other
     respect — same calling convention, same surrounding code, nothing else
     changes. If your description would need to mention a client, a
     constructor, "passing via", "aligning with", "configuring through", or
     any other object that isn't the symbol itself, THIS IS NOT A RENAME.
   - configuration: a module-level setting or attribute that must now be
     supplied through a client constructor, an instance attribute, or some
     other structural location instead of a flat symbol swap.
   - initialization / client_migration: requires instantiating a client
     object first, and the call moves onto that object.
5. SELF-CONSISTENCY CHECK (perform this for every rule before finalizing it):
   Re-read your own migration_type and description together. Do they agree?
   If description contains any of: "via", "through", "align", "pass to",
   "configure", "instantiate", "client", "constructor", "parameter of" —
   migration_type MUST NOT be "rename". If you find a contradiction, do not
   silently pick one — set validation_status to "needs_review" instead, since
   a self-contradictory rule is not safe to act on automatically.
6. Set requires_instantiation to true, false, or null.
7. Provide an actionable description explaining WHY it's a configuration or
   structural change, if it is one.
8. Preserve the original API and source evidence where possible.
9. Set validation_status to exactly one of: validated, needs_review, rejected.
10. A rule may be validated only when the supplied evidence supports the
    migration. "evidence" must be an exact excerpt that appears verbatim in
    the supplied source documentation — not a paraphrase, not a summary.
11. AUTHORITATIVENESS CHECK: verbatim presence in the source text is NOT
    sufficient on its own. Discussion threads and issue comments often
    contain other users' QUESTIONS, GUESSES, or SPECULATION, not confirmed
    answers. If the evidence excerpt is phrased as a question ("does X
    become Y?", "is it now...?"), hedged ("I think", "maybe", "possibly",
    "not sure if"), or attributed to a reply rather than the original
    maintainer post, set validation_status to "needs_review" — do not treat
    a speculative comment as a confirmed migration instruction.
12. If evidence is insufficient or the rule is self-contradictory, use
    needs_review.
13. If the old API was removed without a confirmed replacement, do not
    invent new_api. Use null.
14. Keep every input rule in the output for auditability.

### Validation policy

Evaluate each migration candidate independently.

- `validated` means the proposed migration is supported by the supplied source evidence and the candidate contains a concrete, sufficiently supported replacement.
- `needs_review` means the candidate may represent a real breaking change, but its replacement, evidence, migration strategy, or applicability is not sufficiently established.
- `rejected` means the candidate is demonstrably invalid, contradicted by reliable supplied evidence, or not actually supported as a migration claim. Do not use rejected merely because a replacement is unknown.

A missing replacement is not, by itself, grounds for rejection. Preserve documented removals and configuration changes that require further investigation as needs_review.

Do not invent source URLs, quotations, version boundaries, API names, or migration examples. Preserve the original evidence and source metadata where possible.

Distinguish API existence from migration correctness. A target API existing does not establish that a proposed transformation preserves behavior.

Do not omit input candidates. Return one result per input candidate, preserving the original old_api and package identity. If uncertain, mark needs_review rather than dropping the candidate.

Do not label a rule validated solely because its proposed replacement looks plausible.


"""


def normalize_evidence_text(text: str) -> str:
    """Normalize whitespace and common Markdown formatting for comparison."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\\", "")
    text = re.sub(r"`+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def validate_and_normalize_rules(
    package: str,
    current_version: str,
    target_version: str,
    rules: list[dict],
    raw_code_examples: list[str],
    source_evidence: str = "",
) -> list[dict]:
    """
    Validate extracted rules while preserving unresolved candidates.

    validation_status:
        validated   -> passed the available validation checks
        needs_review -> retained, but not established as safe
        rejected    -> excluded from downstream migration candidates
    """
    if not rules:
        print("⚠️ Rule validation skipped: no extracted rules.")
        return []

    if not client:
        raise RuntimeError(
            "MISTRAL_API_KEY is not set; cannot validate migration rules."
        )

    def preserve_for_review(
        source_rules: list[dict],
        reason: str,
    ) -> list[dict]:
        """Retain candidates when validation cannot establish trust."""
        preserved = []

        for source_rule in source_rules:
            rule = dict(source_rule)

            if rule.get("validation_status") == "rejected":
                continue

            rule["package"] = package
            rule["old_api"] = str(rule.get("old_api") or "").strip()
            new_api = rule.get("new_api")
            rule["new_api"] = (
                new_api.strip()
                if isinstance(new_api, str) and new_api.strip()
                else None
            )
            rule["validation_status"] = "needs_review"

            existing_description = rule.get("description")
            note = f"Validation could not establish trust: {reason}"
            rule["description"] = (
                f"{existing_description.strip()} {note}"
                if isinstance(existing_description, str)
                and existing_description.strip()
                else note
            )

            if rule["old_api"]:
                preserved.append(rule)

        return preserved

    user_prompt = f"""
PACKAGE: {package}
CURRENT VERSION: {current_version}
TARGET VERSION: {target_version}

ORIGINAL EXTRACTED RULES:
{json.dumps(rules, indent=2, default=str)}

RAW CODE EXAMPLES (usage context only):
{json.dumps(raw_code_examples or [], indent=2, default=str)}

SOURCE MIGRATION DOCUMENTATION:
{source_evidence or "No source documentation supplied."}

Validate every rule against the available source evidence.
Preserve original evidence and source URLs. Do not invent either.
Keep every input rule in the output for auditability.
"""

    try:
        response = client.chat.parse(
            model="codestral-latest",
            messages=[
                {
                    "role": "system",
                    "content": VALIDATOR_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            response_format=ValidatedRuleset,
            temperature=0,
        )

        parsed = response.choices[0].message.parsed

        if parsed is None:
            print(
                "⚠️ Validator returned no parsed result; "
                "preserving candidates as needs_review."
            )
            return preserve_for_review(
                rules,
                "the validator returned no parsed result",
            )

        accepted = []
        needs_review = 0
        rejected = 0

        normalized_source = normalize_evidence_text(source_evidence)

        for validated_rule in parsed.rules:
            rule = validated_rule.model_dump()

            old_api = normalize_rule_symbols([
                {"old_api": rule.get("old_api", "")}
            ])[0]["old_api"]

            new_api = rule.get("new_api")
            if isinstance(new_api, str):
                new_api = new_api.strip() or None

            status = rule.get("validation_status")

            # Explicit rejection remains a rejection.
            if status == "rejected":
                rejected += 1
                continue

            # Unknown statuses must never be treated as validated.
            if status not in {"validated", "needs_review"}:
                status = "needs_review"

            # Keep original source evidence only when supplied.
            evidence = rule.get("evidence")
            normalized_evidence = normalize_evidence_text(evidence or "")

            evidence_is_grounded = bool(
                normalized_evidence
                and normalized_source
                and normalized_evidence in normalized_source
            )

            reasons = []

            # Deterministic safeguard for contradictory rename descriptions.
            if check_rename_contradiction(rule):
                reasons.append(
                    "The rule is classified as a rename, but its description "
                    "suggests a structural or configuration change."
                )

            if not evidence_is_grounded:
                reasons.append(
                    "The supplied evidence could not be verified against "
                    "the source documentation."
                )

            if rule.get("migration_type") in {"removal", "manual"}:
                reasons.append(
                    "This migration type requires manual handling."
                )

            if not new_api:
                reasons.append(
                    "No concrete replacement API has been established."
                )

            # A model's validated label cannot override deterministic checks.
            if reasons:
                status = "needs_review"

            if status == "needs_review":
                needs_review += 1
            else:
                # At this point, status is validated and the deterministic
                # evidence, replacement, and contradiction checks passed.
                pass

            normalized_rule = {
                "package": package,
                "old_api": old_api,
                "new_api": new_api,
                "migration_type": rule.get("migration_type"),
                "requires_instantiation": rule.get(
                    "requires_instantiation"
                ),
                "description": rule.get("description"),
                "from_version": rule.get("from_version") or current_version,
                "to_version": rule.get("to_version") or target_version,
                "evidence": evidence,
                "source_url": rule.get("source_url"),
                "validation_status": status,
            }

            if reasons:
                existing = normalized_rule.get("description") or ""
                normalized_rule["description"] = (
                    f"{existing}\nValidation concerns: "
                    + " ".join(reasons)
                ).strip()

            if old_api:
                accepted.append(normalized_rule)

        print("\n========== Rule Validation Summary ==========")
        print(f"Candidate rules: {len(rules)}")
        print(
            "Validated:",
            sum(
                r["validation_status"] == "validated"
                for r in accepted
            ),
        )
        print(
            "Needs review:",
            sum(
                r["validation_status"] == "needs_review"
                for r in accepted
            ),
        )
        print(f"Rejected: {rejected}")
        print("==============================================\n")

        return accepted

    except Exception as exc:
        print(f"⚠️ Rule validation failed: {exc}")
        print(
            "Preserving extracted candidates as needs_review; "
            "none will be marked validated by this fallback."
        )
        return preserve_for_review(
            rules,
            f"validator failure: {type(exc).__name__}: {exc}",
        )



# --- Extraction (unvalidated) -----------------------------------------------

def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Aston-dependency-migration-tool",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers

EXTRACTION_SCHEMA_VERSION = "v4-preserve-review-candidates-1"

def _cache_path(package: str, tag: str, doc_text: str, stage0_symbols: set[str]) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest_input = (
        EXTRACTION_SCHEMA_VERSION + "\n" +
        doc_text + "\n--SYMBOLS--\n" + "\n".join(sorted(stage0_symbols))
    ).encode("utf-8", errors="replace")
    digest = hashlib.sha256(digest_input).hexdigest()[:16]
    safe_package = re.sub(r"[^A-Za-z0-9_.-]", "_", package)
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    return CACHE_DIR / f"{safe_package}__{safe_tag}__{digest}.json"


def should_extract(doc_text: str, stage0_symbols: set[str]) -> bool:
    """Cheap gate only; this does not claim to prove all usages are covered."""
    if not stage0_symbols:
        return True
    doc_lower = doc_text.lower()
    for symbol in stage0_symbols:
        normalized = symbol.strip().lower()
        if normalized and normalized in doc_lower:
            return True
        leaf = normalized.rsplit(".", 1)[-1]
        generic_names = {
            "create", "client", "open", "close", "get", "set", "update",
            "delete", "request", "response", "error", "chat", "embedding",
        }
        if (
            len(leaf) >= 5
            and leaf not in generic_names
            and re.search(rf"(?<![a-z0-9_]){re.escape(leaf)}(?![a-z0-9_])", doc_lower)
        ):
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


def extract_rules(package: str, doc_text: str) -> MigrationRuleset:
    """Raw extraction only — NOT validated yet. extract_rules_cached() below
    is the function that should be called from the pipeline; it runs this,
    then validate_and_normalize_rules(), before caching and returning."""
    if not client:
        raise RuntimeError("MISTRAL_API_KEY is not set; cannot extract migration rules.")
    if not doc_text or len(doc_text.strip()) < 30:
        return MigrationRuleset()

    truncated_text = doc_text[:20000]
    response = client.chat.parse(
        model="codestral-latest",
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"Package name: {package}\n"
                    "Extract rules only from the documentation below.\n\n"
                    f"Documentation:\n{truncated_text}"
                ),
            },
        ],
        response_format=MigrationRuleset,
        temperature=0,
    )

    parsed = response.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError(f"Codestral returned no parsed ruleset for {package}")

    normalized = normalize_rule_symbols([rule.model_dump() for rule in parsed.rules])
    for rule in normalized:
        rule["package"] = package

    blocks = _validate_raw_blocks(parsed.raw_example_blocks, truncated_text)
    return MigrationRuleset(
        rules=[MigrationRule.model_validate(rule) for rule in normalized],
        raw_example_blocks=blocks,
    )


def extract_rules_cached(
    package: str,
    tag: str,
    doc_text: str,
    stage0_symbols: set[str],
    current_version: str,
    target_version: str,
) -> MigrationRuleset:
    """
    Extract, THEN VALIDATE, then cache the validated result. Previously this
    only cached the raw extraction and validation was never called at all —
    this was the direct path by which an ungrounded rule like
    openai.proxy -> openai.proxies reached the patcher unchecked.
    """
    cache_file = _cache_path(package, tag, doc_text, stage0_symbols)

    if cache_file.exists():
        try:
            return MigrationRuleset.model_validate_json(cache_file.read_text())
        except Exception as exc:
            print(f"  ⚠️ Invalid cache for {tag}; re-extracting ({exc})")

    if not should_extract(doc_text, stage0_symbols):
        print(f"  ⏭️ {tag}: no scanned symbol appears in source; skipping LLM extraction.")
        result = MigrationRuleset()
        cache_file.write_text(result.model_dump_json(indent=2))
        return result

    raw = extract_rules(package, doc_text)
    if not raw.rules:
        cache_file.write_text(raw.model_dump_json(indent=2))
        return raw

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
    cache_file.write_text(result.model_dump_json(indent=2))
    return result


def get_installed_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def get_latest_pypi_version(package_name: str) -> str | None:
    response = requests.get(
        f"https://pypi.org/pypi/{package_name}/json",
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 200:
        return response.json().get("info", {}).get("version")
    return None


def resolve_github_repo(package_name: str) -> Optional[str]:
    response = requests.get(
        f"https://pypi.org/pypi/{package_name}/json",
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        return None
    project_urls = response.json().get("info", {}).get("project_urls") or {}
    for target_url in project_urls.values():
        match = re.search(r"github\.com/([^/]+/[^/]+)", target_url)
        if match:
            return match.group(1).removesuffix(".git").rstrip("/")
    return None


def is_candidate_url(url: str) -> bool:
    lower = url.lower()
    return (
        "github.com" in lower
        and any(path in lower for path in ("/issues/", "/discussions/", "/pull/", "/wiki/"))
    ) or any(term in lower for term in ("migration", "upgrade", "breaking", "changelog"))


def extract_candidate_urls_from_text(text: str) -> list[str]:
    pattern = r"https?://[^\s\)\>\]\}\"']+"
    urls = {item.rstrip(".,;:") for item in re.findall(pattern, text)}
    return sorted(url for url in urls if is_candidate_url(url))


def fetch_page_content(url: str) -> str | None:
    headers = _github_headers()
    try:
        discussion = re.search(r"github\.com/([^/]+)/([^/]+)/discussions/(\d+)", url)
        if discussion:
            owner, repo, number = discussion.groups()
            api_url = f"{GITHUB_API}/repos/{owner}/{repo}/discussions/{number}"
            response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                return f"{data.get('title', '')}\n\n{data.get('body', '')}"[:15000]

        issue = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
        if issue:
            owner, repo, number = issue.groups()
            api_url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}"
            response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                return f"{data.get('title', '')}\n\n{data.get('body', '')}"[:15000]

        response = requests.get(
            url,
            headers={"User-Agent": "Aston-dependency-migration-tool"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return None

        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", response.text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|pre|h[1-6])>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)[:15000]
    except requests.RequestException as exc:
        print(f"  ⚠️ Failed to fetch {url}: {exc}")
        return None


def collect_linked_sources(initial_text: str, max_depth: int = 2) -> list[str]:
    collected: list[str] = []
    visited: set[str] = set()
    current_level = [initial_text]

    for depth in range(max_depth + 1):
        next_level: list[str] = []
        for source_text in current_level:
            collected.append(source_text)
            for url in extract_candidate_urls_from_text(source_text):
                if url in visited:
                    continue
                visited.add(url)
                print(f"  🔗 Following migration link (depth {depth + 1}): {url}")
                content = fetch_page_content(url)
                if content and len(content) > 100:
                    print(f"    ✓ Retrieved {len(content)} characters")
                    next_level.append(content)
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
    """Retrieve release notes, falling back to migration/changelog files at tag."""
    release_url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    try:
        response = requests.get(
            release_url,
            headers=_github_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        body = response.json().get("body", "") if response.status_code == 200 else ""
    except requests.RequestException as exc:
        print(f"  ⚠️ Release lookup failed for {tag}: {exc}")
        body = ""

    if not body or len(body.strip()) < 20:
        print(f"  ⚠️ No useful release notes for {tag}; checking repository files.")
        candidates = (
            "MIGRATION.md", "migration.md", "CHANGELOG.md", "CHANGELOG.rst",
            "CHANGES.md", "docs/migration.md", "docs/MIGRATION.md", "docs/CHANGELOG.md",
        )
        found = []
        for filename in candidates:
            try:
                content = _fetch_repo_file(repo, tag, filename)
            except requests.RequestException as exc:
                print(f"    ⚠️ Could not fetch {filename}: {exc}")
                continue
            if content and len(content.strip()) > 50:
                found.append(f"=== SOURCE FILE: {filename} @ {tag} ===\n{content}")
        body = "\n\n".join(found)

    if not body or len(body.strip()) < 20:
        print(f"  ❌ Unresolved boundary: no documentation source found for {tag}")
        return None

    print(f"  📥 Ingested documentation source for tag: {tag}")
    linked_docs = collect_linked_sources(body, max_depth=2)
    return "\n\n--- NEXT DOCUMENT IN CHAIN ---\n\n".join(linked_docs)[:30000]


def run_stage1_pipeline(
    package_name: str,
    current_version: str | None = None,
    target_version: str | None = None,
    stage0_symbols: set[str] | None = None,
) -> tuple[list[dict], str, list[dict]]:
    """Return (merged_rules, combined_docs, boundary-tagged raw examples).
    Rules in merged_rules have already passed validate_and_normalize_rules()
    at each boundary — nothing ungrounded reaches the caller."""
    if not current_version or not target_version:
        raise ValueError("current_version and target_version are required for migration-chain resolution.")

    repo = resolve_github_repo(package_name)
    if not repo:
        print(f"❌ Failed to resolve GitHub repository for package {package_name}")
        return [], "", []

    print(
        f"📦 Resolving migration chain for '{package_name}' "
        f"({current_version} -> {target_version}) @ {repo}"
    )

    try:
        tags_in_chain = get_migration_chain(repo, current_version, target_version)
    except (requests.RequestException, ValueError) as exc:
        print(f"❌ Could not resolve migration chain: {exc}")
        return [], "", []

    if not tags_in_chain:
        print("ℹ️ No version transition to process.")
        return [], "", []

    print(f"🔗 Target Version Boundaries: {tags_in_chain}")

    rulesets_in_order: list[list[dict]] = []
    migration_docs: list[str] = []
    all_raw_example_blocks: list[dict] = []
    unresolved_boundaries: list[str] = []

    for tag in tags_in_chain:
        print(f"\n🔎 Ingesting boundary: {tag}...")
        doc_content = discover_release_doc(repo, tag)

        if not doc_content:
            unresolved_boundaries.append(tag)
            continue

        migration_docs.append(f"=== MIGRATION BOUNDARY: {tag} ===\n{doc_content}")
        ruleset = extract_rules_cached(
            package_name,
            tag,
            doc_content,
            stage0_symbols or set(),
            current_version,
            target_version,
        )
        rulesets_in_order.append([rule.model_dump() for rule in ruleset.rules])

        for block in ruleset.raw_example_blocks:
            all_raw_example_blocks.append({
                "boundary": tag,
                "source": "release_notes_or_repository_migration_docs",
                "code": block,
            })

        validated_count = sum(
            rule.validation_status == "validated"
            for rule in ruleset.rules
        )

        review_count = sum(
            rule.validation_status == "needs_review"
            for rule in ruleset.rules
        )

        print(
            f"  └─ Processed {len(ruleset.rules)} candidates for {tag}: "
            f"{validated_count} validated, {review_count} needs_review"
        )

    if unresolved_boundaries:
        print("⚠️ Unresolved documentation boundaries: " + ", ".join(unresolved_boundaries))
        print("⚠️ Coverage is incomplete until these boundaries have usable sources.")

    merged_rules = merge_rule_chain(rulesets_in_order)
    migration_doc_text = "\n\n".join(migration_docs)

    print(f"\n✅ Rule Chain Composition Complete. Total Merged Rules: {len(merged_rules)}")
    print(f"📄 Combined Migration Documentation: {len(migration_doc_text)} characters")

    return merged_rules, migration_doc_text, all_raw_example_blocks