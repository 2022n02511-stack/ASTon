import json
import subprocess
import tempfile
from pathlib import Path

def validate_all_rules(
    all_rules: list[dict],
    all_target_versions: dict[str, str],
    req_file: str,
    distribution_names: dict[str, str] | None = None,
) -> list[dict]:
    from collections import defaultdict

    distribution_names = distribution_names or {}
    groups = defaultdict(list)

    for index, rule in enumerate(all_rules):
        package = rule.get("package")
        if not package:
            rule.setdefault("target_api_status", "unprobed")
            rule.setdefault("validation_status", "not_validated")
            rule.setdefault("resolution_reason", "Rule has no package identifier")
            continue

        target_version = all_target_versions.get(package)
        if not target_version:
            rule["target_api_status"] = "unprobed"
            rule["validation_status"] = "not_validated"
            rule["resolution_reason"] = f"No target version configured for {package}"
            continue

        groups[(package, target_version)].append((index, rule))

    validated_by_index = {}

    for (package, target_version), indexed_rules in groups.items():
        rules_for_package = [rule for _, rule in indexed_rules]

        # PASS all_target_versions HERE:
        validated = validate_rules_against_target(
            rules=rules_for_package,
            pkg_name=package,
            target_version=target_version,
            all_target_versions=all_target_versions,
            req_file=req_file,
            distribution_name=distribution_names.get(package),
        )

        if len(validated) != len(indexed_rules):
            raise RuntimeError(
                f"Rule validator returned {len(validated)} rules for "
                f"{len(indexed_rules)} inputs for {package}=={target_version}. "
                "Refusing to silently pass unvalidated rules downstream."
        )

        for (original_index, _), validated_rule in zip(indexed_rules, validated):
            validated_by_index[original_index] = validated_rule

    for index, rule in enumerate(all_rules):
        if index not in validated_by_index:
            validated_by_index[index] = rule

    return [validated_by_index[index] for index in range(len(all_rules))]


def reconcile_report(
    extraction_report: dict,
    validated_rules: list[dict],
) -> dict:
    """
    Create a report consistent with the validated rule set.
    Preserve extraction-stage information.
    """
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

    return report

def validate_rules_against_target(
    rules: list[dict],
    pkg_name: str,
    target_version: str,
    all_target_versions: dict[str, str] | None = None,
    req_file: str | Path = "temp_requirements.txt",
    distribution_name: str | None = None,
    python_image: str = "python:3.11-slim",
    timeout: int = 600,
) -> list[dict]:
    req_file = Path(req_file).resolve()
    distribution_name = distribution_name or pkg_name
    all_target_versions = all_target_versions or {pkg_name: target_version}

    prepared_rules = []
    for original in rules:
        rule = dict(original)
        rule.setdefault("evidence_status", "unknown")
        rule.setdefault("chain_status", "unresolved")
        rule.setdefault("target_api_status", "unprobed")
        rule.setdefault("verification_status", "not_run")

        candidate = rule.get("candidate_new_api") or rule.get("new_api")
        rule["candidate_new_api"] = candidate

        if not candidate or "UNRESOLVED" in str(candidate).upper():
            rule["target_api_status"] = "unverifiable"
            rule["validation_status"] = "not_validated"
            rule["resolution_reason"] = rule.get("resolution_reason") or "No documented replacement API"
        else:
            rule["target_api_status"] = "pending"
            rule["validation_status"] = "pending"

        prepared_rules.append(rule)

    if not prepared_rules or not req_file.is_file():
        return prepared_rules

    # Worker script updated to loop through and install ALL target versions simultaneously
    worker_script = r'''
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
        raise RuntimeError("pip install failed:\n" + result.stderr[-12000:])

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
    # 1. Install project base requirements
    install(["-r", requirements_path])

    # 2. Upgrade ALL target packages together simultaneously
    upgrade_args = ["--upgrade", "--upgrade-strategy", "only-if-needed"]
    for pkg, ver in version_map.items():
        upgrade_args.append(f"{pkg}=={ver}")
    install(upgrade_args)

    # 3. Check for dependency conflicts
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    if check.returncode:
        print(json.dumps({"environment_status": "dependency_conflict", "details": (check.stdout + "\n" + check.stderr)[-12000:], "rules": []}))
        sys.exit(0)

    try:
        module = importlib.import_module(import_name)
    except Exception as exc:
        print(json.dumps({"environment_status": "import_failed", "details": f"{type(exc).__name__}: {exc}", "rules": []}))
        sys.exit(0)

    with open(rules_path, encoding="utf-8") as file:
        rules = json.load(file)

    results = []
    for index, rule in enumerate(rules):
        candidate = rule.get("candidate_new_api") or rule.get("new_api")
        if not candidate or "UNRESOLVED" in str(candidate).upper():
            results.append({"index": index, "status": "unverifiable", "reason": "No documented replacement API"})
            continue
        status, reason = probe_api(module, candidate)
        results.append({"index": index, "status": status, "reason": reason})

    print(json.dumps({"environment_status": "ready", "rules": results}))

except Exception as exc:
    print(json.dumps({"environment_status": "install_failed", "details": f"{type(exc).__name__}: {exc}", "rules": []}))
'''

    with tempfile.TemporaryDirectory(prefix="sma-validation-") as tmp:
        temp_dir = Path(tmp)
        script_path = temp_dir / "validate.py"
        rules_path = temp_dir / "rules.json"

        script_path.write_text(worker_script, encoding="utf-8")
        rules_path.write_text(json.dumps(prepared_rules), encoding="utf-8")

        req_parent = req_file.parent
        container_req = f"/project/{req_file.name}"

        command = [
            "docker", "run", "--rm",
            "-v", f"{req_parent}:/project",
            "-v", f"{temp_dir}:/work",
            "-w", "/work",
            python_image,
            "python", "/work/validate.py",
            container_req,
            pkg_name,
            json.dumps(all_target_versions),  # Pass full map of all target versions
            "/work/rules.json",
        ]

        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            for rule in prepared_rules:
                if rule["target_api_status"] == "pending":
                    rule["target_api_status"] = "probe_failed"
                    rule["validation_status"] = "unverifiable"
                    rule["resolution_reason"] = f"Validation timed out after {timeout} seconds"
            return prepared_rules

        if result.returncode != 0:
            details = (result.stderr or result.stdout)[-4000:]
            for rule in prepared_rules:
                if rule["target_api_status"] == "pending":
                    rule["target_api_status"] = "probe_failed"
                    rule["validation_status"] = "unverifiable"
                    rule["resolution_reason"] = f"Docker validation failed: {details}"
            return prepared_rules

        try:
            report = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            for rule in prepared_rules:
                if rule["target_api_status"] == "pending":
                    rule["target_api_status"] = "probe_failed"
                    rule["validation_status"] = "unverifiable"
                    rule["resolution_reason"] = "Could not parse validation report"
            return prepared_rules

    environment_status = report.get("environment_status")
    if environment_status != "ready":
        details = report.get("details", environment_status)
        for rule in prepared_rules:
            if rule["target_api_status"] == "pending":
                rule["target_api_status"] = "probe_failed"
                rule["validation_status"] = "unverifiable"
                rule["resolution_reason"] = str(details)
        return prepared_rules

    for item in report.get("rules", []):
        index = item["index"]
        rule = prepared_rules[index]
        status = item["status"]

        rule["target_api_status"] = {
            "exists": "target_api_exists",
            "missing": "target_api_missing",
            "unverifiable": "unverifiable",
        }.get(status, "unverifiable")

        # Preserve the ingestion-stage assessment. A successful attribute probe
# proves only that the target exists, not that the migration is correct.
        documentation_status = rule.get("validation_status")

        if status == "exists":
            rule["target_api_status"] = "target_api_exists"

            if documentation_status == "validated":
                rule["validation_status"] = "validated"
            else:
                rule["validation_status"] = documentation_status or "needs_review"

        else:
            rule["validation_status"] = (
                "needs_review"
                if documentation_status == "validated"
                else documentation_status or "needs_review"
        )
        rule["resolution_reason"] = item["reason"]
        rule["environment_status"] = "ready"

    return prepared_rules