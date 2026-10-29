"""Validate extracted migration rules against a target dependency environment.
Documentation evidence and runtime API probing are deliberately tracked
separately: finding an attribute in the target package does not prove that a
migration rule is semantically correct.
"""

from __future__ import annotations
import json
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


def _mark_unprobed(rule: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark a rule as not runtime-validated without discarding its evidence."""
    rule["target_api_status"] = "unprobed"
    rule["validation_status"] = "not_validated"
    rule["resolution_reason"] = reason
    return rule


def _mark_pending_as_failed(
    rules: list[dict[str, Any]],
    reason: str,
    *,
    status: str = "probe_failed",
) -> list[dict[str, Any]]:
    """Give every rule whose API probe did not finish an explicit outcome."""
    for rule in rules:
        if rule.get("target_api_status") == "pending":
            rule["target_api_status"] = status
            rule["validation_status"] = "unverifiable"
            rule["resolution_reason"] = reason
    return rules


def validate_all_rules(
    all_rules: list[dict[str, Any]],
    all_target_versions: dict[str, str],
    req_file: str | Path,
    distribution_names: dict[str, str] | None = None,
    *,
    python_image: str = "python:3.11-slim",
    timeout: int = 600,
) -> list[dict[str, Any]]:
    """Validate shared rules once per import-package/target-version group.

    Args:
        all_rules: Extracted migration rules. Each rule should include
            ``package`` (the Python import name), ``old_api`` and ``new_api``.
        all_target_versions: Target versions keyed by Python import name.
        req_file: Requirements file used to build the validation environment.
        distribution_names: Optional mapping from Python import names to pip
            distribution names, e.g. ``{"yaml": "PyYAML"}``.
        python_image: Docker image used for validation.
        timeout: Maximum seconds allowed for each Docker validation run.

    Returns:
        A new list of rules with runtime-probe results attached. Input rule
        dictionaries are not mutated in place.
    """
    distribution_names = distribution_names or {}
    req_path = Path(req_file).expanduser().resolve()
    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    result_by_index: dict[int, dict[str, Any]] = {}

    for index, original in enumerate(all_rules):
        rule = dict(original)
        # Keep the original documentation/rule assessment separate from the
        # result of probing whether the destination API exists at runtime.
        rule.setdefault("evidence_status", "unknown")
        rule.setdefault("chain_status", "unresolved")
        rule.setdefault("verification_status", "not_run")
        rule["documentation_validation_status"] = rule.get(
            "documentation_validation_status",
            rule.get("validation_status", "not_validated"),
        )
        rule.setdefault("target_api_status", "unprobed")

        package = rule.get("package")
        if not package:
            result_by_index[index] = _mark_unprobed(
                rule, "Rule has no package identifier"
            )
            continue

        target_version = all_target_versions.get(package)
        if not target_version:
            result_by_index[index] = _mark_unprobed(
                rule, f"No target version configured for import package {package}"
            )
            continue

        groups[(package, str(target_version))].append((index, rule))

    for (package, target_version), indexed_rules in groups.items():
        validated = validate_rules_against_target(
            rules=[rule for _, rule in indexed_rules],
            pkg_name=package,
            target_version=target_version,
            all_target_versions=all_target_versions,
            req_file=req_path,
            distribution_name=distribution_names.get(package),
            distribution_names=distribution_names,
            python_image=python_image,
            timeout=timeout,
        )

        if len(validated) != len(indexed_rules):
            raise RuntimeError(
                f"Rule validator returned {len(validated)} rules for "
                f"{len(indexed_rules)} inputs for {package}=={target_version}. "
                "Refusing to silently pass unvalidated rules downstream."
            )

        for (original_index, _), validated_rule in zip(indexed_rules, validated):
            result_by_index[original_index] = validated_rule

    # Preserve original ordering, including rules that could not be grouped.
    return [result_by_index[index] for index in range(len(all_rules))]


def reconcile_report(
    extraction_report: dict[str, Any],
    validated_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an extraction report whose rules and counts match validation."""
    report = dict(extraction_report)
    report["rules"] = validated_rules
    report["total_rules"] = len(validated_rules)

    report["target_api_exists"] = sum(
        rule.get("target_api_status") == "target_api_exists"
        for rule in validated_rules
    )
    report["target_api_missing"] = sum(
        rule.get("target_api_status") == "target_api_missing"
        for rule in validated_rules
    )
    report["unverifiable_rules"] = sum(
        rule.get("target_api_status") in {
            "unverifiable",
            "probe_failed",
            "unprobed",
        }
        for rule in validated_rules
    )
    report["not_probed_rules"] = sum(
        rule.get("target_api_status") in {"pending", "unprobed"}
        for rule in validated_rules
    )
    report["documentation_validated_rules"] = sum(
        rule.get("documentation_validation_status") == "validated"
        for rule in validated_rules
    )
    return report


def validate_rules_against_target(
    rules: list[dict[str, Any]],
    pkg_name: str,
    target_version: str,
    all_target_versions: dict[str, str] | None = None,
    req_file: str | Path = "temp_requirements.txt",
    distribution_name: str | None = None,
    distribution_names: dict[str, str] | None = None,
    python_image: str = "python:3.11-slim",
    timeout: int = 600,
) -> list[dict[str, Any]]:
    """Probe candidate destination APIs in an isolated target-version env.

    ``pkg_name`` and rule ``package`` values are Python import names.
    ``distribution_names`` maps those import names to pip distribution names.
    """
    req_path = Path(req_file).expanduser().resolve()
    distribution_names = dict(distribution_names or {})
    if distribution_name:
        distribution_names.setdefault(pkg_name, distribution_name)
    all_target_versions = dict(all_target_versions or {pkg_name: target_version})

    prepared_rules: list[dict[str, Any]] = []
    for original in rules:
        rule = dict(original)
        rule.setdefault("evidence_status", "unknown")
        rule.setdefault("chain_status", "unresolved")
        rule.setdefault("verification_status", "not_run")
        rule["documentation_validation_status"] = rule.get(
            "documentation_validation_status",
            rule.get("validation_status", "not_validated"),
        )

        candidate = rule.get("candidate_new_api") or rule.get("new_api")
        rule["candidate_new_api"] = candidate

        if not isinstance(candidate, str) or not candidate.strip() or "UNRESOLVED" in candidate.upper():
            rule["target_api_status"] = "unverifiable"
            rule["validation_status"] = "not_validated"
            rule["resolution_reason"] = (
                rule.get("resolution_reason") or "No documented replacement API"
            )
        else:
            rule["target_api_status"] = "pending"
            # Do not overwrite documentation_validation_status here.
            rule["validation_status"] = "pending"
        prepared_rules.append(rule)

    if not prepared_rules:
        return []

    if not req_path.is_file():
        return _mark_pending_as_failed(
            prepared_rules,
            f"Requirements file not found: {req_path}",
            status="unprobed",
        )

    # The worker installs distribution names but imports and probes Python
    # import names. The version map sent to it is keyed by distribution name.
    install_version_map: dict[str, str] = {}
    for import_name, version in all_target_versions.items():
        dist_name = distribution_names.get(import_name, import_name)
        if dist_name in install_version_map and install_version_map[dist_name] != version:
            raise ValueError(
                f"Conflicting target versions for distribution {dist_name!r}: "
                f"{install_version_map[dist_name]!r} and {version!r}"
            )
        install_version_map[dist_name] = str(version)

    worker_script = r'''\
import importlib
import json
import subprocess
import sys

requirements_path, import_name, version_map_json, rules_path = sys.argv[1:5]
version_map = json.loads(version_map_json)


def install(args):
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("pip install failed:\\n" + result.stderr[-12000:])


def resolve_attribute(root, path):
    current = root
    for part in path.split("."):
        if not part:
            raise AttributeError("Empty attribute in API path")
        current = getattr(current, part)
    return current


def probe_api(module, api):
    if not isinstance(api, str) or not api.strip():
        return "unverifiable", "No usable candidate API"
    api = api.strip()

    # Generic probing cannot instantiate arbitrary client objects safely.
    if api.startswith("client."):
        return "unverifiable", "Instance-based API requires a package-specific adapter"

    if api == import_name:
        path = ""
    elif api.startswith(import_name + "."):
        path = api[len(import_name) + 1:]
    else:
        path = api

    if not path:
        return "exists", "Package module imported successfully"

    try:
        resolve_attribute(module, path)
        return "exists", "Destination attribute resolved"
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        return "missing", f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        return "unverifiable", f"{type(exc).__name__}: {exc}"


try:
    # Install the project's base environment, then pin all target distributions.
    install(["-r", requirements_path])
    upgrade_args = ["--upgrade", "--upgrade-strategy", "only-if-needed"]
    for dist_name, version in version_map.items():
        upgrade_args.append(f"{dist_name}=={version}")
    install(upgrade_args)

    check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
    )
    if check.returncode:
        print(json.dumps({
            "environment_status": "dependency_conflict",
            "details": (check.stdout + "\\n" + check.stderr)[-12000:],
            "rules": [],
        }))
        sys.exit(0)

    try:
        module = importlib.import_module(import_name)
    except Exception as exc:
        print(json.dumps({
            "environment_status": "import_failed",
            "details": f"{type(exc).__name__}: {exc}",
            "rules": [],
        }))
        sys.exit(0)

    with open(rules_path, encoding="utf-8") as file:
        rules = json.load(file)

    results = []
    for index, rule in enumerate(rules):
        candidate = rule.get("candidate_new_api") or rule.get("new_api")
        if not isinstance(candidate, str) or not candidate.strip() or "UNRESOLVED" in candidate.upper():
            results.append({
                "index": index,
                "status": "unverifiable",
                "reason": "No documented replacement API",
            })
            continue
        status, reason = probe_api(module, candidate)
        results.append({"index": index, "status": status, "reason": reason})

    print(json.dumps({"environment_status": "ready", "rules": results}))
except Exception as exc:
    print(json.dumps({
        "environment_status": "install_failed",
        "details": f"{type(exc).__name__}: {exc}",
        "rules": [],
    }))
'''

    with tempfile.TemporaryDirectory(prefix="aston-validation-") as tmp:
        temp_dir = Path(tmp)
        script_path = temp_dir / "validate.py"
        rules_path = temp_dir / "rules.json"
        script_path.write_text(worker_script, encoding="utf-8")
        rules_path.write_text(json.dumps(prepared_rules), encoding="utf-8")

        command = [
            "docker", "run", "--rm",
            "-v", f"{req_path.parent}:/project:ro",
            "-v", f"{temp_dir}:/work:ro",
            "-w", "/work",
            python_image,
            "python", "/work/validate.py",
            f"/project/{req_path.name}",
            pkg_name,
            json.dumps(install_version_map),
            "/work/rules.json",
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _mark_pending_as_failed(
                prepared_rules, f"Validation timed out after {timeout} seconds"
            )
        except OSError as exc:
            return _mark_pending_as_failed(
                prepared_rules, f"Could not start Docker validation: {exc}"
            )

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "No Docker output")[-4000:]
            return _mark_pending_as_failed(
                prepared_rules, f"Docker validation failed: {details}"
            )

        try:
            report = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return _mark_pending_as_failed(
                prepared_rules, "Could not parse Docker validation report"
            )

    if report.get("environment_status") != "ready":
        details = report.get("details", report.get("environment_status", "unknown error"))
        return _mark_pending_as_failed(prepared_rules, str(details))

    seen_indices: set[int] = set()
    for item in report.get("rules", []):
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(prepared_rules):
            continue
        seen_indices.add(index)
        rule = prepared_rules[index]
        status = item.get("status", "unverifiable")
        reason = str(item.get("reason", "No probe explanation supplied"))
        documentation_status = rule.get(
            "documentation_validation_status", "not_validated"
        )

        if status == "exists":
            rule["target_api_status"] = "target_api_exists"
            # Runtime existence is necessary but not sufficient for rule validity.
            rule["validation_status"] = (
                "validated" if documentation_status == "validated"
                else documentation_status
            )
        elif status == "missing":
            rule["target_api_status"] = "target_api_missing"
            rule["validation_status"] = "needs_review"
        else:
            rule["target_api_status"] = "unverifiable"
            rule["validation_status"] = (
                "needs_review" if documentation_status == "validated"
                else documentation_status
            )

        rule["resolution_reason"] = reason
        rule["environment_status"] = "ready"

    # A malformed/partial worker report must not leave any rule pending.
    for index, rule in enumerate(prepared_rules):
        if index not in seen_indices and rule.get("target_api_status") == "pending":
            rule["target_api_status"] = "probe_failed"
            rule["validation_status"] = "unverifiable"
            rule["resolution_reason"] = "Rule missing from Docker validation report"

    return prepared_rules