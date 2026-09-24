import re
import json
import requests
from typing import Optional, Dict, Any
from importlib.metadata import version, PackageNotFoundError
import os
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel
from google.genai import errors
from tenacity import retry, stop_after_attempt, wait_random_exponential, retry_if_exception_type

load_dotenv()

# API_KEY = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY")
API_KEY = os.environ.get("GEMINI_API_KEY_1")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

class MigrationRule(BaseModel):
    package: str
    old_api: str
    new_api: str
    requires_instantiation: str | None = None

class MigrationRuleset(BaseModel):
    rules: list[MigrationRule]    

client = genai.Client(api_key=API_KEY)

prompt = '''
You are an expert static analysis and code refactoring engineer.
Extract ALL breaking API signature updates, function renames, client instantiation changes, 
and parameter replacements from this migration text.

Output strictly a JSON structure matching the schema.
'''

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

def is_major_bump(current_version: str, new_version: str) -> bool:
    def parse_version(v: str):
        cleaned = re.sub(r'^[^\d]+', '', v)
        parts = cleaned.split('.')
        return int(parts[0]) if parts[0].isdigit() else 0

    return parse_version(new_version) > parse_version(current_version)

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

def is_candidate_url(url: str) -> bool:
    """Filter candidate URLs for GitHub discussions, issues, PRs, or migration docs."""
    url_lower = url.lower()
    return (
        "github.com" in url_lower and any(path in url_lower for path in ["/issues/", "/discussions/", "/pull/", "/wiki/"])
    ) or any(term in url_lower for term in ["migration", "upgrade", "breaking", "changelog", "v1", "v2", "v3", "742", "631"])

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
            "Accept": "application/vnd.github+json",
            "Authorization" : "fBearer {GITHUB_TOKEN}"
        }

        # Handle GitHub Discussions via REST API
        match_discussion = re.search(r"github\.com/([^/]+)/([^/]+)/discussions/(\d+)", url)
        if match_discussion:
            owner, repo, number = match_discussion.groups()
            api_url = f"https://api.github.com/repos/{owner}/{repo}/discussions/{number}"
            res = requests.get(api_url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                title = data.get("title", "")
                body = data.get("body", "")
                return f"{title}\n\n{body}"[:15000]

        # Handle GitHub Issues via REST API
        match_issue = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
        if match_issue:
            owner, repo, number = match_issue.groups()
            api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
            res = requests.get(api_url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                title = data.get("title", "")
                body = data.get("body", "")
                return f"{title}\n\n{body}"[:15000]

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
    """
    Follow migration-document links recursively up to max_depth with per-hop diagnostics.
    """
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
                    print(f"      Contains ChatCompletion: {'ChatCompletion' in content}")
                    print(f"      Contains Embedding: {'Embedding' in content}")
                    next_level.append(content)

        current_level = next_level
        if not current_level:
            break

    return collected

def find_migration_source_for_step(package_name: str, repo: str, target_major_tag: str) -> str | None:
    """
    Discovers migration text strictly for a single target major version transition.
    """
    # 1. Primary Check: Dedicated MIGRATION guides
    conventional_files = ["MIGRATION.md", "UPGRADING.md", "docs/MIGRATION.md", "docs/migration.md"]
    for filename in conventional_files:
        raw_url = f"https://raw.githubusercontent.com/{repo}/main/{filename}"
        res = requests.get(raw_url)
        if res.status_code == 200 and len(res.text.strip()) > 100:
            print(f" Found comprehensive migration guide: {filename}")
            return res.text[:25000]

    # 2. Major Tag Fetching + Recursive Link Traversal
    release_url = f"https://api.github.com/repos/{repo}/releases/tags/{target_major_tag}"
    res = requests.get(release_url)
    
    if res.status_code == 200:
        body = res.json().get("body", "")
        if body:
            print(f" Collected release notes for tag: {target_major_tag}")
            linked_docs = collect_linked_sources(body, max_depth=2)
            combined_text = "\n\n--- NEXT DOCUMENT IN CHAIN ---\n\n".join(linked_docs)
            return combined_text[:30000]

    return None

@retry(
    wait=wait_random_exponential(min=5, max=60),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type((errors.ClientError, errors.ServerError))
)
def extract_rules(doc_text: str) -> list[dict]:
    if not doc_text or len(doc_text.strip()) < 30:
        return []

    truncated_text = doc_text[:20000]

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=f"{prompt}\n\nDocumentation:\n{truncated_text}",
        config={
            "response_mime_type": "application/json",
            "response_schema": MigrationRuleset,
        }
    )

    ruleset: MigrationRuleset = response.parsed

    new_rules = [rule.model_dump() for rule in ruleset.rules]

    return normalize_rule_symbols(new_rules)