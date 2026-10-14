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
    """
    Compose migration rules across version boundaries while preserving
    validation status and unresolved candidates.

    Safety rules:
    - Never turn None into the literal string "None".
    - Never compose a rule without a concrete destination.
    - A composed rule is validated only if every component is validated.
    - Preserve needs_review candidates for downstream review/patching.
    - Keep the earliest from_version and latest to_version.
    """
    merged: list[dict] = []

    def clean_symbol(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def status_of(rule: dict) -> str:
        status = rule.get("validation_status")
        # Missing status must not be assumed to mean validated.
        if status not in {"validated", "needs_review", "rejected"}:
            return "needs_review"
        return status

    def has_concrete_target(rule: dict) -> bool:
        target = clean_symbol(rule.get("new_api"))
        return bool(
            target
            and not target.startswith("UNRESOLVED:")
        )

    def merge_status(first: dict, second: dict) -> str:
        statuses = {status_of(first), status_of(second)}

        if "rejected" in statuses:
            return "rejected"

        if statuses == {"validated"}:
            return "validated"

        return "needs_review"

    for ruleset in rulesets_in_order:
        incoming_rules = []

        for source in ruleset:
            rule = dict(source)
            old_api = clean_symbol(rule.get("old_api"))

            if not old_api:
                continue

            rule["old_api"] = old_api
            rule["new_api"] = clean_symbol(rule.get("new_api"))

            if status_of(rule) == "rejected":
                continue

            # Do not treat a missing status as validated.
            rule["validation_status"] = status_of(rule)
            incoming_rules.append(rule)

        # Multiple candidates may share an old API. Preserve the candidates
        # rather than silently selecting one based on input order.
        step_by_old: dict[str, list[dict]] = {}

        for rule in incoming_rules:
            step_by_old.setdefault(rule["old_api"], []).append(rule)

        consumed_indices: set[int] = set()
        next_merged: list[dict] = []

        for existing in merged:
            destination = clean_symbol(existing.get("new_api"))

            # Unresolved rules cannot be safely composed. Keep them intact.
            if not destination or destination.startswith("UNRESOLVED:"):
                next_merged.append(existing)
                continue

            later_candidates = step_by_old.get(destination, [])

            if not later_candidates:
                next_merged.append(existing)
                continue

            # Compose each possible continuation. If several rules propose
            # different targets, retain each alternative with its own status.
            for later in later_candidates:
                composed = dict(existing)
                composed["new_api"] = later.get("new_api")

                composed["validation_status"] = merge_status(existing, later)

                # Preserve the earliest source version.
                if not composed.get("from_version"):
                    composed["from_version"] = later.get("from_version")

                # Carry the latest boundary's target version.
                if later.get("to_version"):
                    composed["to_version"] = later["to_version"]

                # Retain both pieces of evidence for auditability.
                evidence_parts = [
                    item for item in (
                        existing.get("evidence"),
                        later.get("evidence"),
                    )
                    if isinstance(item, str) and item.strip()
                ]
                if evidence_parts:
                    composed["evidence"] = "\n\n--- COMPOSED RULE EVIDENCE ---\n\n".join(
                        dict.fromkeys(evidence_parts)
                    )

                source_urls = [
                    item for item in (
                        existing.get("source_url"),
                        later.get("source_url"),
                    )
                    if isinstance(item, str) and item.strip()
                ]
                if source_urls:
                    composed["source_url"] = " | ".join(
                        dict.fromkeys(source_urls)
                    )

                # Later transformation requirements may add structural steps.
                if later.get("requires_instantiation") is True:
                    composed["requires_instantiation"] = True
                elif (
                    composed.get("requires_instantiation") is None
                    and later.get("requires_instantiation") is not None
                ):
                    composed["requires_instantiation"] = (
                        later["requires_instantiation"]
                    )

                if later.get("migration_type"):
                    composed["migration_type"] = later["migration_type"]

                if later.get("description"):
                    previous_description = composed.get("description")
                    if (
                        previous_description
                        and previous_description != later["description"]
                    ):
                        composed["description"] = (
                            f"{previous_description}\n"
                            f"Later boundary transformation: "
                            f"{later['description']}"
                        )
                    else:
                        composed["description"] = later["description"]

                if not composed.get("example_before") and later.get("example_before"):
                    composed["example_before"] = later["example_before"]

                if later.get("example_after"):
                    composed["example_after"] = later["example_after"]

                next_merged.append(composed)

            # Mark these intermediate rules as consumed by composition.
            for index, candidate in enumerate(incoming_rules):
                if candidate in later_candidates:
                    consumed_indices.add(index)

        # Add rules not used as intermediate steps. This also preserves
        # unresolved candidates whose replacement is missing.
        existing_old_apis = {rule["old_api"] for rule in next_merged}

        for index, rule in enumerate(incoming_rules):
            if index in consumed_indices:
                continue

            # Preserve a distinct candidate even if another rule has the
            # same old API. Avoid exact duplicates only.
            duplicate = any(
                existing.get("old_api") == rule.get("old_api")
                and existing.get("new_api") == rule.get("new_api")
                and existing.get("validation_status") == rule.get("validation_status")
                for existing in next_merged
            )

            if not duplicate:
                next_merged.append(dict(rule))

        merged = next_merged

    return merged
