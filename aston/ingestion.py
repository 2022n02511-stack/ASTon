import os
import re
import requests
from typing import Optional
from importlib.metadata import version, PackageNotFoundError
from dotenv import load_dotenv
from google import genai
from google.genai import errors
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_random_exponential, retry_if_exception_type
from aston.changes.chain import get_migration_chain, merge_rule_chain
from mistralai.client import Mistral


load_dotenv()

# API_KEY = os.environ.get("GEMINI_API_KEY_1") or os.environ.get("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# client = genai.Client(api_key=API_KEY)

API_KEY = os.environ.get("MISTRAL_API_KEY")
client = Mistral(api_key=API_KEY)

# ==================================================
# --- SCHEMAS & PROMPTS ---
# ==================================================

class MigrationRule(BaseModel):
    package: str
    old_api: str
    new_api: str
    requires_instantiation: str | None = None

class MigrationRuleset(BaseModel):
    rules: list[MigrationRule]

prompt = '''
You are an expert static analysis and code refactoring engineer.
Extract ALL breaking API signature updates, function renames, client instantiation changes, 
and parameter replacements from this migration text.

Output strictly a JSON structure matching the schema.
'''

# ==================================================
# --- HELPER UTILITIES ---
# ==================================================

def normalize_rule_symbols(rules: list[dict]) -> list[dict]:
    for rule in rules:
        rule["old_api"] = rule["old_api"].replace("()", "").strip()
        rule["new_api"] = rule["new_api"].replace("()", "").strip()
    return rules

def get_installed_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None

def get_latest_pypi_version(package_name: str) -> str | None:
    url = f"https://pypi.org/pypi/{package_name}/json"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()["info"]["version"]
    return None    

def resolve_github_repo(package_name: str) -> Optional[str]:
    url = f"https://pypi.org/pypi/{package_name}/json"
    res = requests.get(url)
    if res.status_code != 200:
        return None
    
    info = res.json().get("info", {})
    project_urls = info.get("project_urls") or {}
    
    for key, target_url in project_urls.items():
        if "github.com" in target_url:
            match = re.search(r"github\.com/([^/]+/[^/]+)", target_url)
            if match:
                return match.group(1).rstrip(".git")
    return None

# ==================================================
# --- LINK-FOLLOWING INGESTION ENGINE ---
# ==================================================

def is_candidate_url(url: str) -> bool:
    """Filter candidate URLs for GitHub discussions, issues, PRs, or migration docs."""
    url_lower = url.lower()
    return (
        "github.com" in url_lower and any(path in url_lower for path in ["/issues/", "/discussions/", "/pull/", "/wiki/"])
    ) or any(term in url_lower for term in ["migration", "upgrade", "breaking", "changelog"])

def extract_candidate_urls_from_text(text: str) -> list[str]:
    """Extracts external candidate URLs and cleans trailing punctuation."""
    url_pattern = r'https?://[^\s\)\>\]]+'
    raw_urls = re.findall(url_pattern, text)
    return [u.rstrip('.,;') for u in set(raw_urls) if is_candidate_url(u)]

def fetch_page_content(url: str) -> str | None:
    """
    Fetches content via GitHub REST API for discussions and issues;
    falls back to plain HTTP for standard markdown/web pages.
    """
    try:
        headers = {
            "User-Agent": "Self-Maintaining-APIs-Pipeline",
            "Accept": "application/vnd.github+json"
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        # Handle GitHub Discussions via REST API
        match_discussion = re.search(r"github\.com/([^/]+)/([^/]+)/discussions/(\d+)", url)
        if match_discussion:
            owner, repo, number = match_discussion.groups()
            api_url = f"https://api.github.com/repos/{owner}/{repo}/discussions/{number}"
            res = requests.get(api_url, headers=headers, timeout=30)
            if res.status_code == 200:
                data = res.json()
                return f"{data.get('title', '')}\n\n{data.get('body', '')}"[:15000]

        # Handle GitHub Issues via REST API
        match_issue = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
        if match_issue:
            owner, repo, number = match_issue.groups()
            api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
            res = requests.get(api_url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                return f"{data.get('title', '')}\n\n{data.get('body', '')}"[:15000]

        # Standard web/raw content fallback
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            text = re.sub(r"<[^>]+>", " ", res.text)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return "\n".join(lines)[:15000]

    except Exception as e:
        print(f"  ⚠️ Failed to fetch {url}: {e}")

    return None

def collect_linked_sources(initial_text: str, max_depth: int = 2) -> list[str]:
    """Follow migration-document links recursively up to max_depth."""
    collected = []
    visited = set()
    current_level = [initial_text]

    for depth in range(max_depth + 1):
        next_level = []

        for text in current_level:
            collected.append(text)
            urls = extract_candidate_urls_from_text(text)

            for url in urls:
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

def discover_release_doc(repo: str, tag: str) -> str | None:
    """Discovers release notes and recursively follows linked documents for a tag boundary."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    release_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    res = requests.get(release_url, headers=headers, timeout=30)
    
    if res.status_code == 200:
        body = res.json().get("body", "")
        if body:
            print(f"  📥 Collected release notes for tag: {tag}")
            linked_docs = collect_linked_sources(body, max_depth=2)
            return "\n\n--- NEXT DOCUMENT IN CHAIN ---\n\n".join(linked_docs)[:30000]

    return None

# ==================================================
# --- GEMINI RULE EXTRACTION ---
# ==================================================

@retry(
    wait=wait_random_exponential(min=5, max=60),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type((errors.ClientError, errors.ServerError))
)
def extract_rules(doc_text: str) -> list[dict]:
    """Extracts structured breaking change rules using Gemini structured outputs."""
    if not doc_text or len(doc_text.strip()) < 30:
        return []

    truncated_text = doc_text[:20000]

    # response = client.models.generate_content(
    #     model="gemini-3.6-flash",
    #     contents=f"{prompt}\n\nDocumentation:\n{truncated_text}",
    #     config={
    #         "response_mime_type": "application/json",
    #         "response_schema": MigrationRuleset,
    #     }
    # )

    response = client.chat.parse(
        model="codestral-latest",
        messages=[
            {
                "role": "system",
                "content": prompt,
            },
            {
                "role": "user",
                "content": f"Documentation:\n{truncated_text}",
            },
        ],
        response_format=MigrationRuleset,
        temperature=0,
    )

    ruleset: MigrationRuleset = response.choices[0].message.parsed
    new_rules = [rule.model_dump() for rule in ruleset.rules]
    return normalize_rule_symbols(new_rules)

# ==================================================
# --- STAGE 1 ORCHESTRATOR ENTRYPOINT ---
# ==================================================

def run_stage1_pipeline(
    package_name: str,
    current_version: str | None = None,
    target_version: str | None = None
) -> tuple[list[dict], str]:
    """
    Stage 1 Entrypoint.

    Resolves the migration chain from current_version to target_version,
    extracts rules at each major-version boundary, merges the rules, and
    returns both:

        1. merged structured migration rules
        2. combined raw migration documentation

    The raw documentation is required by the existing Stage 2 relevance
    engine.
    """

    current_version = current_version

    target_version = target_version

    repo = resolve_github_repo(package_name)

    if not repo:
        print(
            f"❌ Failed to resolve GitHub repository "
            f"for package {package_name}"
        )
        return [], ""

    print(
        f"📦 Resolving migration chain for "
        f"'{package_name}' ({current_version} -> {target_version}) @ {repo}"
    )

    # ==================================================
    # 1. Discover major-version boundaries
    # ==================================================

    tags_in_chain = get_migration_chain(
        repo,
        current_version,
        target_version
    )

    if not tags_in_chain:
        tags_in_chain = [f"v{target_version}"]

    print(f"🔗 Target Version Boundaries: {tags_in_chain}")

    # ==================================================
    # 2. Extract rules + preserve raw migration documents
    # ==================================================

    rulesets_in_order = []
    migration_docs = []

    for tag in tags_in_chain:

        print(f"\n🔎 Ingesting boundary: {tag}...")

        doc_content = discover_release_doc(repo, tag)

        if doc_content:

            # Keep the raw migration material for Stage 2.
            migration_docs.append(
                f"=== MIGRATION BOUNDARY: {tag} ===\n"
                f"{doc_content}"
            )

            step_rules = extract_rules(doc_content)

            rulesets_in_order.append(step_rules)

            print(
                f"  └─ Extracted {len(step_rules)} rules for {tag}"
            )

        else:

            print(
                f"  └─ ⚠️ No migration docs found for {tag}, "
                f"skipping step."
            )

    # ==================================================
    # 3. Compose rules across version steps
    # ==================================================

    merged_rules = merge_rule_chain(
        rulesets_in_order
    )

    # ==================================================
    # 4. Compose raw migration document
    # ==================================================

    migration_doc_text = "\n\n".join(
        migration_docs
    )

    print(
        f"\n✅ Rule Chain Composition Complete. "
        f"Total Merged Rules: {len(merged_rules)}"
    )

    print(
        f"📄 Combined Migration Documentation: "
        f"{len(migration_doc_text)} characters"
    )

    return merged_rules, migration_doc_text