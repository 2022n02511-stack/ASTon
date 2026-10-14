"""
Resolve migration boundaries and compose API rules across a version upgrade.

The chain includes:
- the first stable tag for each crossed major version; and
- the requested target tag, so changes introduced after the latest major boundary
  are not silently missed.
"""

import os
import re
from typing import Any

import requests
from packaging.version import InvalidVersion, Version
from requests.exceptions import RequestException


GITHUB_API = "https://api.github.com"
MAX_TAG_PAGES = 30
REQUEST_TIMEOUT = 20


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Aston-dependency-migration-tool",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_tag_version(tag_name: str) -> Version | None:
    cleaned = re.sub(r"^[vV]", "", tag_name.strip())
    try:
        return Version(cleaned)
    except InvalidVersion:
        return None


def get_release_tags(repo: str) -> list[dict[str, Any]]:
    """Return parseable tags. Raise on API/network failure instead of pretending
    that an empty result means there are no version tags."""
    tags: list[dict[str, Any]] = []
    headers = _github_headers()

    with requests.Session() as session:
        for page in range(1, MAX_TAG_PAGES + 1):
            response = session.get(
                f"{GITHUB_API}/repos/{repo}/tags",
                params={"per_page": 100, "page": page},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break

            for item in batch:
                name = item.get("name", "")
                parsed = _parse_tag_version(name)
                if parsed is not None:
                    tags.append({"tag_name": name, "version": parsed})

            if len(batch) < 100:
                break

    # Same version can occasionally have differently named tags. Keep one.
    by_version: dict[Version, dict[str, Any]] = {}
    for tag in tags:
        existing = by_version.get(tag["version"])
        if existing is None or (
            existing["tag_name"].startswith("v") is False
            and tag["tag_name"].startswith("v")
        ):
            by_version[tag["version"]] = tag

    return sorted(by_version.values(), key=lambda item: item["version"])


def get_migration_chain(
    repo: str,
    old_version: str,
    new_version: str,
) -> list[str]:
    """Return stable major-boundary tags crossed plus the target tag.

    Pre-release tags (alpha/beta/rc/dev) are excluded as migration boundaries.
    If the target version is represented by a tag, use that exact tag spelling.
    If it is not represented, append a conventional v-prefixed target tag; the
    caller must treat missing documentation for that tag as unresolved.
    """
    try:
        old_v = Version(old_version)
        new_v = Version(new_version)
    except InvalidVersion as exc:
        raise ValueError(
            f"Invalid package version range: {old_version!r} -> {new_version!r}"
        ) from exc

    if new_v < old_v:
        raise ValueError(
            f"Target version {new_v} is older than installed version {old_v}"
        )
    if new_v == old_v:
        return []

    tags = get_release_tags(repo)
    stable = [
        tag for tag in tags
        if not tag["version"].is_prerelease
        and not tag["version"].is_devrelease
        and old_v < tag["version"] <= new_v
    ]
    stable.sort(key=lambda item: item["version"])

    chain: list[str] = []
    crossed_majors: set[int] = set()
    for tag in stable:
        version = tag["version"]
        if version.major > old_v.major and version.major not in crossed_majors:
            chain.append(tag["tag_name"])
            crossed_majors.add(version.major)

    # Include target release to catch changes made after its major boundary.
    exact_target = next(
        (tag for tag in tags if tag["version"] == new_v),
        None,
    )
    target_tag = exact_target["tag_name"] if exact_target else f"v{new_version}"
    if target_tag not in chain:
        chain.append(target_tag)

    return chain


def merge_rule_chain(rulesets_in_order: list[list[dict]]) -> list[dict]:
    """Compose A->B followed by B->C into A->C.

    Rules are copied on input so this function does not mutate caller-owned
    dictionaries. Carries forward version bounds (from_version, target_version).
    """
    merged: dict[str, dict] = {}

    for ruleset in rulesets_in_order:
        step_by_old: dict[str, dict] = {}
        for incoming in ruleset:
            old_api = str(incoming.get("old_api", "")).strip()
            new_api = str(incoming.get("new_api", "")).strip()
            if old_api and new_api:
                step_by_old[old_api] = dict(incoming)

        consumed: set[str] = set()

        # Compose all prior paths whose current destination is changed in this step.
        for old_api, existing in list(merged.items()):
            current_destination = existing.get("new_api")
            later = step_by_old.get(current_destination)
            if later is None:
                continue

            consumed.add(current_destination)
            existing["new_api"] = later["new_api"]

            # Update fields, ensuring target_version takes the latest boundary
            for field in ("requires_instantiation", "migration_type", "description", "target_version"):
                if later.get(field) is not None:
                    existing[field] = later[field]

            # Preserve earliest from_version, latest target_version
            if later.get("from_version") and not existing.get("from_version"):
                existing["from_version"] = later["from_version"]

            # Preserve the earliest before-example; prefer the latest after-example.
            if not existing.get("example_before") and later.get("example_before"):
                existing["example_before"] = later["example_before"]
            if later.get("example_after"):
                existing["example_after"] = later["example_after"]

        # Add rules that begin in this step.
        for old_api, incoming in step_by_old.items():
            if old_api in consumed:
                continue
            if old_api not in merged:
                merged[old_api] = dict(incoming)

    return list(merged.values())