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
CACHE_DIR = Path(".sma/rule_cache_v3")


class MigrationRule(BaseModel):
    package: str
    old_api: str
    new_api: str
    requires_instantiation: bool | None = None
    migration_type: str | None = None
    description: str | None = None
    example_before: str | None = None
    example_after: str | None = None


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
- requires_instantiation: explain the required client setup briefly, or null.
- migration_type: one of rename, configuration, client_migration,
  signature_change, removal, or unknown.
- description: short explanation of any non-trivial transformation, or null.
- example_before / example_after: only minimal snippets explicitly shown in
  the source for this specific rule. Otherwise null.

Also return raw_example_blocks: full, distinct before/after migration examples
copied verbatim from the supplied documentation. Do not complete or invent code.
If no such examples are present, return [].

Output only data matching the response schema.
"""


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
) -> Path:
    """Cache by source text and scanner-symbol set so stale/empty cache entries
    do not survive changes to the documentation or the relevant usages."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest_input = (
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

        # A final-name fallback helps docs that omit the package prefix, but
        # avoid very generic identifiers that cause needless extraction calls.
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
    """Only retain exact snippets present in the source passed to the model."""
    result = []
    seen = set()
    for block in blocks:
        cleaned = block.strip()
        if cleaned and cleaned in source_text and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def extract_rules_cached(
    package: str,
    tag: str,
    doc_text: str,
    stage0_symbols: set[str],
) -> MigrationRuleset:
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

    result = extract_rules(package, doc_text)
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

        # This is a lightweight HTML fallback, not a full Markdown/HTML parser.
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

    # Contents API may provide a download URL for larger files.
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
            "MIGRATION.md",
            "migration.md",
            "CHANGELOG.md",
            "CHANGELOG.rst",
            "CHANGES.md",
            "docs/migration.md",
            "docs/MIGRATION.md",
            "docs/CHANGELOG.md",
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


def extract_rules(package: str, doc_text: str) -> MigrationRuleset:
    if not client:
        raise RuntimeError("MISTRAL_API_KEY is not set; cannot extract migration rules.")
    if not doc_text or len(doc_text.strip()) < 30:
        return MigrationRuleset()

    # Keep a hard limit because providers enforce request/token limits.
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

    # print(parsed)
    if parsed is None:
        raise RuntimeError(f"Codestral returned no parsed ruleset for {package}")

    normalized = normalize_rule_symbols([rule.model_dump() for rule in parsed.rules])
    # print(normalized)
    for rule in normalized:
        # The package is known by the caller; do not trust a model-generated value.
        rule["package"] = package

    

    blocks = _validate_raw_blocks(parsed.raw_example_blocks, truncated_text)
    return MigrationRuleset(
        rules=[MigrationRule.model_validate(rule) for rule in normalized],
        raw_example_blocks=blocks,
    )


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
        )
        rulesets_in_order.append([rule.model_dump() for rule in ruleset.rules])

        for block in ruleset.raw_example_blocks:
            all_raw_example_blocks.append({
                "boundary": tag,
                "source": "release_notes_or_repository_migration_docs",
                "code": block,
            })

        print(f"  └─ Processed {len(ruleset.rules)} rules for {tag}")

    if unresolved_boundaries:
        print(
            "⚠️ Unresolved documentation boundaries: "
            + ", ".join(unresolved_boundaries)
        )
        print("⚠️ Coverage is incomplete until these boundaries have usable sources.")

    merged_rules = merge_rule_chain(rulesets_in_order)
    migration_doc_text = "\n\n".join(migration_docs)

    print(f"\n✅ Rule Chain Composition Complete. Total Merged Rules: {len(merged_rules)}")
    print(f"📄 Combined Migration Documentation: {len(migration_doc_text)} characters")

    # print(merged_rules)
    # print(migration_doc_text)

    return merged_rules, migration_doc_text, all_raw_example_blocks