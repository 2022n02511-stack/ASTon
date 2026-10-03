"""
changes/chain.py

Solves the gap: extracting rules for a single target_major_tag only
catches breaking changes introduced in THAT release. If the installed
code predates that tag by several major versions, or the latest release
is several majors AHEAD of it, rules from every other major boundary in
between are silently missing — which is exactly why real repos (further
from 0.28.1 than the original single-step fixture) were failing.

Two jobs:
  1. get_migration_chain() — find every major-version-boundary release
     between old_version and new_version, in order.
  2. merge_rule_chain() — extract rules at each boundary, then COMPOSE
     them: if v1 renames A->B and v2 later renames B->C, a piece of code
     still calling A needs to end up mapped to C, not stuck at B.
"""

import os
import re
import requests
from packaging.version import Version, InvalidVersion
from tenacity import retry, stop_after_attempt, wait_random_exponential, retry_if_exception_type
from requests.exceptions import Timeout, ConnectionError
from urllib3.exceptions import ProtocolError



def _github_headers() -> dict:
    """
    Unauthenticated GitHub API calls are capped at 60/hour — easy to hit
    once chain resolution + discover.py's link-following are both making
    calls in the same run. Set GITHUB_TOKEN (a plain personal access
    token, no special scopes needed for public repos) to raise that to
    5000/hour. Falls back to unauthenticated if unset, same as before.
    """
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_tag_version(tag_name: str) -> Version | None:
    """GitHub tags are often 'v1.0.0', PyPI versions are '1.0.0' — strip
    a leading 'v' before parsing so both compare correctly."""
    cleaned = re.sub(r"^[vV]", "", tag_name.strip())
    try:
        return Version(cleaned)
    except InvalidVersion:
        return None


@retry(
    wait=wait_random_exponential(min=5, max=30),
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type((Timeout, ConnectionError, ProtocolError))
)
def _fetch_tag_page(session: requests.Session, repo: str, page: int) -> list:
    """Fetches a single page of tags with retry protection."""
    res = session.get(
        f"https://api.github.com/repos/{repo}/tags",
        params={"per_page": 100, "page": page},
        timeout=30,
    )
    if res.status_code == 200:
        return res.json()
    return []


def get_release_tags(repo: str) -> list[dict]:
    """
    Fetches every git tag for `repo` (owner/name) via GitHub's /tags API,
    parsed to (tag_name, Version). Skips tags that don't parse as a
    version (e.g. codename tags) rather than crashing the whole chain.

    BUGFIX: originally used the /releases endpoint, but that only returns
    the newest ~N (paginated) releases — for a package with hundreds of
    patch releases (openai-python has 300+), that never reaches back far
    enough to find v1.0.0/v2.0.0 boundaries. /tags is lighter per-request
    and reliably covers the FULL tag history with enough pagination.
    """
    tags = []
    page = 1
    while True:
        res = requests.get(
            f"https://api.github.com/repos/{repo}/tags",
            params={"per_page": 100, "page": page},
            headers=_github_headers(),
            timeout=30,
        )
        if res.status_code != 200:
            break
        batch = res.json()
        if not batch:
            break
        for tag in batch:
            tag_name = tag.get("name", "")
            version = _parse_tag_version(tag_name)
            if version is not None:
                tags.append({"tag_name": tag_name, "version": version})
        page += 1
        if page > 30:  # 3000 tags is plenty; stop paginating past that
            break
    return tags


def get_migration_chain(repo: str, old_version: str, new_version: str) -> list[str]:
    """
    Returns the ordered list of GitHub release tags representing every
    MAJOR-version boundary strictly after old_version and up to and
    including new_version. Minor/patch releases are intentionally
    skipped — per your own architecture doc, breaking changes worth a
    migration rule almost always land on a major bump, and processing
    every minor release would multiply LLM calls for little benefit.

    Example: old=0.28.1, new=3.2.0, releases include v1.0.0, v1.4.0,
    v2.0.0, v2.1.0, v3.0.0, v3.2.0 -> returns ["v1.0.0", "v2.0.0", "v3.0.0"]
    """
    try:
        old_v = Version(old_version)
    except InvalidVersion:
        old_v = Version("0")

    try:
        new_v = Version(new_version)
    except InvalidVersion:
        return []

    all_tags = get_release_tags(repo)
    in_range = [t for t in all_tags if old_v < t["version"] <= new_v]
    in_range.sort(key=lambda t: t["version"])

    chain = []
    last_major = old_v.major
    for t in in_range:
        if t["version"].major > last_major:
            chain.append(t["tag_name"])
            last_major = t["version"].major

    return chain


def merge_rule_chain(rulesets_in_order: list[list[dict]]) -> list[dict]:
    """
    Composes rules across multiple version-boundary steps into one final
    mapping. Each ruleset in `rulesets_in_order` is what extract_rules()
    returned for one step, in chain order (earliest boundary first).

    Core logic: an existing tracked rule's `old_api` stays fixed at the
    ORIGINAL symbol a codebase might still be calling, but its `new_api`
    gets updated forward each time a later step shows that intermediate
    symbol got renamed again. A rule whose old_api was never seen before
    is a genuinely new tracked rename, starting fresh at this step.
    """
    merged: dict[str, dict] = {}  # keyed by ORIGINAL old_api

    for ruleset in rulesets_in_order:
        step_by_old = {r["old_api"]: r for r in ruleset}

        # 1. Forward-compose: does this step rename something we're
        #    already tracking as an intermediate new_api? Track exactly
        #    which of THIS STEP's old_api keys got consumed this way —
        #    that's the set to exclude below, not a snapshot of merged's
        #    final new_api values (which had already moved past the
        #    intermediate symbol by the time we'd compute it that way,
        #    causing the intermediate to leak through as its own entry).
        consumed_this_step: set[str] = set()
        for original_old_api, rule in merged.items():
            current_new_api = rule["new_api"]
            if current_new_api in step_by_old:
                later_rule = step_by_old[current_new_api]
                consumed_this_step.add(current_new_api)
                rule["new_api"] = later_rule["new_api"]
                rule["requires_instantiation"] = (
                    later_rule.get("requires_instantiation")
                    or rule.get("requires_instantiation")
                )

        # 2. Track genuinely new rules from this step — skip anything
        #    that was just consumed above as an intermediate symbol.
        for old_api, rule in step_by_old.items():
            if old_api not in merged and old_api not in consumed_this_step:
                merged[old_api] = dict(rule)

    return list(merged.values())