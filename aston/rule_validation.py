"""Validate extracted migration rules against a target dependency environment.
Documentation evidence and runtime API probing are deliberately tracked
separately: finding an attribute in the target package does not prove that a
migration rule is semantically correct.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from aston.cli_ui.rich import console

logger = logging.getLogger(__name__)

_PROJECT_ROOT_MARKERS = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    ".aston",
)


def _decode_output(value: str | bytes | None) -> str:
    """Safely decode subprocess output that may be str, bytes, or None."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _write_debug_artifact(
    base_dir: Path,
    pkg_name: str,
    target_version: str,
    content: str,
) -> Path | None:
    """Persist Docker/worker stdout, stderr, and diagnostics under .aston/debug."""
    try:
        debug_dir = base_dir / ".aston" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_pkg = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in pkg_name)
        safe_ver = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in target_version)
        artifact_path = debug_dir / f"rule_validation_{safe_pkg}_{safe_ver}_{timestamp}.log"
        artifact_path.write_text(content, encoding="utf-8")
        return artifact_path
    except OSError as exc:
        logger.debug("Could not write debug artifact for %s==%s: %s", pkg_name, target_version, exc)
        return None


def _run_container_with_progress(
    command: list[str],
    pkg_name: str,
    target_version: str,
    timeout: int,
) -> tuple[int, str, str, bool]:
    """Execute validation container with a progress bar while suppressing terminal text."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )

    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    outputs: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def _read_stream(stream_name: str, pipe) -> None:
        try:
            if pipe is None:
                return
            for line in iter(pipe.readline, ""):
                events.put((stream_name, line))
        finally:
            if pipe is not None:
                pipe.close()
            events.put((stream_name, None))

    threads = [
        threading.Thread(target=_read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=_read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    start_time = time.monotonic()
    total_timeout = max(1, timeout)
    deadline = start_time + total_timeout
    finished_streams = 0
    timed_out = False
    phase_label = f"Validating {pkg_name}=={target_version}"

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task(phase_label, total=total_timeout)

        while finished_streams < 2:
            remaining = deadline - time.monotonic()

            if remaining <= 0 and process.poll() is None and not timed_out:
                timed_out = True
                logger.warning(
                    "Target validation Docker run timed out after %ds for %s==%s; terminating process",
                    total_timeout,
                    pkg_name,
                    target_version,
                )
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=5)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait()

            try:
                stream_name, line = events.get(
                    timeout=0.1 if timed_out else max(0.01, min(0.25, remaining))
                )
            except queue.Empty:
                progress.update(
                    task_id,
                    description=phase_label,
                    completed=min(total_timeout, time.monotonic() - start_time),
                )
                continue

            if line is None:
                finished_streams += 1
            else:
                outputs[stream_name].append(line)
                stripped = line.strip()
                if stripped == "ASTON_REQUIREMENTS_INSTALL_START":
                    phase_label = f"Installing requirements ({pkg_name}=={target_version})"
                elif stripped == "ASTON_TARGET_UPGRADE_START":
                    phase_label = f"Upgrading {pkg_name}=={target_version}"
                elif stripped == "ASTON_PACKAGE_IMPORT_START":
                    phase_label = f"Probing {pkg_name}=={target_version}"

                progress.update(
                    task_id,
                    description=phase_label,
                    completed=min(total_timeout, time.monotonic() - start_time),
                )

    for thread in threads:
        thread.join()

    returncode = process.wait()
    return (
        returncode,
        "".join(outputs["stdout"]),
        "".join(outputs["stderr"]),
        timed_out,
    )


def _resolve_project_mount_root(req_path: Path) -> tuple[Path, str]:
    """Resolve the host directory to mount at /project and the container req path."""
    parent = req_path.parent.resolve()
    for candidate in (parent, *parent.parents):
        if any((candidate / marker).exists() for marker in _PROJECT_ROOT_MARKERS):
            try:
                rel_req = req_path.relative_to(candidate)
                return candidate, f"/project/{rel_req.as_posix()}"
            except ValueError:
                break

    logger.warning(
        "Could not identify project root for %s; mounting requirements directory %s. "
        "Relative references outside this directory may fail.",
        req_path,
        parent,
    )
    return parent, f"/project/{req_path.name}"


def _mark_unprobed(rule: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark a rule as not runtime-validated while preserving its prior status."""
    rule["target_api_status"] = "unprobed"
    rule["validation_status"] = rule.get(
        "documentation_validation_status",
        "not_validated",
    )
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
            doc_status = rule.get("documentation_validation_status", "not_validated")
            rule["target_api_status"] = status
            rule["validation_status"] = (
                "needs_review" if doc_status == "validated" else doc_status
            )
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

    logger.debug(
        "Grouping %d rule(s) for target validation (req_file=%s, image=%s)",
        len(all_rules),
        req_path,
        python_image,
    )

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
            logger.debug("Rule at index %d has no package identifier; marking unprobed", index)
            result_by_index[index] = _mark_unprobed(
                rule, "Rule has no package identifier"
            )
            continue

        target_version = all_target_versions.get(package)
        if not target_version:
            logger.debug(
                "No target version configured for package %s (rule index %d); marking unprobed",
                package,
                index,
            )
            result_by_index[index] = _mark_unprobed(
                rule, f"No target version configured for import package {package}"
            )
            continue

        groups[(package, str(target_version))].append((index, rule))

    for (package, target_version), indexed_rules in groups.items():
        logger.info(
            "Validating %d rule(s) for %s==%s in Docker",
            len(indexed_rules),
            package,
            target_version,
        )
        group_target_versions = {package: target_version}
        group_dist_names = (
            {package: distribution_names[package]}
            if package in distribution_names
            else {}
        )
        validated = validate_rules_against_target(
            rules=[rule for _, rule in indexed_rules],
            pkg_name=package,
            target_version=target_version,
            all_target_versions=group_target_versions,
            req_file=req_path,
            distribution_name=distribution_names.get(package),
            distribution_names=group_dist_names,
            python_image=python_image,
            timeout=timeout,
        )

        if len(validated) != len(indexed_rules):
            logger.error(
                "Validator count mismatch for %s==%s: expected %d, got %d",
                package,
                target_version,
                len(indexed_rules),
                len(validated),
            )
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
        rule.get("target_api_status") in {
            "pending",
            "unprobed",
            "probe_failed",
        }
        for rule in validated_rules
    )
    report["documentation_validated_rules"] = sum(
        rule.get("documentation_validation_status") == "validated"
        for rule in validated_rules
    )

    logger.debug(
        "Reconciled rule report: total=%d exists=%d missing=%d unverifiable=%d doc_validated=%d",
        report["total_rules"],
        report["target_api_exists"],
        report["target_api_missing"],
        report["unverifiable_rules"],
        report["documentation_validated_rules"],
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

        if (
            not isinstance(candidate, str)
            or not candidate.strip()
            or "UNRESOLVED" in candidate.upper()
        ):
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
        logger.warning("Requirements file not found for target validation: %s", req_path)
        return _mark_pending_as_failed(
            prepared_rules,
            f"Requirements file not found: {req_path}",
            status="unprobed",
        )

    project_mount_dir, container_req_path = _resolve_project_mount_root(req_path)
    debug_base_dir = Path.cwd().resolve()

    # The worker installs distribution names but imports and probes Python
    # import names. The version map sent to it is keyed by distribution name.
    install_version_map: dict[str, str] = {}
    for import_name, version in all_target_versions.items():
        dist_name = distribution_names.get(import_name, import_name)
        if (
            dist_name in install_version_map
            and install_version_map[dist_name] != version
        ):
            logger.error(
                "Conflicting target versions for distribution %r: %r vs %r",
                dist_name,
                install_version_map[dist_name],
                version,
            )
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
import time

requirements_path, import_name, version_map_json, rules_path = sys.argv[1:5]
version_map = json.loads(version_map_json)


def install(args):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--default-timeout=120",
            "--retries=5",
            "--progress-bar=off",
            *args,
        ],
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

    # Instance-bound paths (e.g., client.chat.completions.create) live on an
    # instantiated client rather than the top-level package module.
    if api == "client" or api.startswith("client."):
        return (
            "unverifiable",
            "Instance-bound API requires instantiated client context",
        )

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
    # Install the project's base environment, then pin target distributions.
    print("ASTON_REQUIREMENTS_INSTALL_START", file=sys.stderr, flush=True)
    t0 = time.monotonic()
    install(["-r", requirements_path])
    print(
        f"ASTON_REQUIREMENTS_INSTALL_SECONDS={time.monotonic() - t0:.2f}",
        file=sys.stderr,
        flush=True,
    )

    upgrade_args = ["--upgrade", "--upgrade-strategy", "only-if-needed"]
    for dist_name, version in version_map.items():
        upgrade_args.append(f"{dist_name}=={version}")

    print("ASTON_TARGET_UPGRADE_START", file=sys.stderr, flush=True)
    t1 = time.monotonic()
    install(upgrade_args)
    print(
        f"ASTON_TARGET_UPGRADE_SECONDS={time.monotonic() - t1:.2f}",
        file=sys.stderr,
        flush=True,
    )

    check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
    )
    if check.returncode:
        print(json.dumps({
            "environment_status": "dependency_conflict",
            "details": (check.stdout + "\n" + check.stderr)[-12000:],
            "rules": [],
        }))
        sys.exit(0)

    try:
        print("ASTON_PACKAGE_IMPORT_START", file=sys.stderr, flush=True)
        t2 = time.monotonic()
        module = importlib.import_module(import_name)
        print(
            f"ASTON_PACKAGE_IMPORT_SECONDS={time.monotonic() - t2:.2f}",
            file=sys.stderr,
            flush=True,
        )
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
        if (
            not isinstance(candidate, str)
            or not candidate.strip()
            or "UNRESOLVED" in candidate.upper()
        ):
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
        os.chmod(temp_dir, 0o755)
        script_path = temp_dir / "validate.py"
        rules_path = temp_dir / "rules.json"
        script_path.write_text(worker_script, encoding="utf-8")
        rules_path.write_text(json.dumps(prepared_rules), encoding="utf-8")
        os.chmod(script_path, 0o644)
        os.chmod(rules_path, 0o644)

        command = [
            "docker",
            "run",
            "--rm",
            "-e",
            "PIP_CACHE_DIR=/root/.cache/pip",
            "-v",
            "aston-pip-cache:/root/.cache/pip:Z",
            "-v",
            f"{project_mount_dir}:/project:ro,Z",
            "-v",
            f"{temp_dir}:/work:Z",
            "-w",
            "/work",
            python_image,
            "python",
            "/work/validate.py",
            container_req_path,
            pkg_name,
            json.dumps(install_version_map),
            "/work/rules.json",
        ]
        logger.debug("Executing target validation Docker container for %s==%s", pkg_name, target_version)

        try:
            returncode, stdout_text, stderr_text, timed_out = _run_container_with_progress(
                command=command,
                pkg_name=pkg_name,
                target_version=target_version,
                timeout=timeout,
            )
        except OSError as exc:
            logger.error("Failed starting Docker target validation for %s: %s", pkg_name, exc)
            return _mark_pending_as_failed(
                prepared_rules, f"Could not start Docker validation: {exc}"
            )

        out_str = _decode_output(stdout_text)
        err_str = _decode_output(stderr_text)
        combined_debug = f"=== STDERR ===\n{err_str}\n\n=== STDOUT ===\n{out_str}"

        if timed_out:
            artifact = _write_debug_artifact(
                debug_base_dir, pkg_name, target_version, combined_debug
            )
            logger.warning(
                "Target validation Docker run timed out after %ds for %s==%s (debug_log=%s)",
                timeout,
                pkg_name,
                target_version,
                artifact,
            )
            return _mark_pending_as_failed(
                prepared_rules, f"Validation timed out after {timeout} seconds"
            )

        if err_str:
            artifact = _write_debug_artifact(
                debug_base_dir, pkg_name, target_version, combined_debug
            )
            logger.debug(
                "Target validation diagnostics for %s==%s saved to %s",
                pkg_name,
                target_version,
                artifact,
            )

        if returncode != 0:
            artifact = _write_debug_artifact(
                debug_base_dir, pkg_name, target_version, combined_debug
            )
            logger.error(
                "Docker target validation exited with rc=%d for %s==%s (debug_log=%s)",
                returncode,
                pkg_name,
                target_version,
                artifact,
            )
            return _mark_pending_as_failed(
                prepared_rules, f"Docker validation failed (rc={returncode})"
            )

        try:
            report = json.loads(out_str.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            artifact = _write_debug_artifact(
                debug_base_dir, pkg_name, target_version, combined_debug
            )
            logger.error(
                "Failed parsing Docker target validation output for %s==%s (%s; debug_log=%s)",
                pkg_name,
                target_version,
                exc,
                artifact,
            )
            return _mark_pending_as_failed(
                prepared_rules, "Could not parse Docker validation report"
            )

    if report.get("environment_status") != "ready":
        details = report.get(
            "details", report.get("environment_status", "unknown error")
        )
        artifact = _write_debug_artifact(
            debug_base_dir,
            pkg_name,
            target_version,
            f"{combined_debug}\n\n=== REPORT ===\n{json.dumps(report, indent=2)}",
        )
        logger.warning(
            "Target validation worker environment_status=%s for %s==%s (debug_log=%s)",
            report.get("environment_status"),
            pkg_name,
            target_version,
            artifact,
        )
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
                "validated"
                if documentation_status == "validated"
                else documentation_status
            )
        elif status == "missing":
            rule["target_api_status"] = "target_api_missing"
            rule["validation_status"] = "needs_review"
        else:
            rule["target_api_status"] = "unverifiable"
            rule["validation_status"] = (
                "needs_review"
                if documentation_status == "validated"
                else documentation_status
            )

        rule["resolution_reason"] = reason
        rule["environment_status"] = "ready"

    # A malformed/partial worker report must not leave any rule pending.
    for index, rule in enumerate(prepared_rules):
        if index not in seen_indices and rule.get("target_api_status") == "pending":
            logger.warning(
                "Rule index %d missing from target validation report for %s==%s",
                index,
                pkg_name,
                target_version,
            )
            rule["target_api_status"] = "probe_failed"
            rule["validation_status"] = "unverifiable"
            rule["resolution_reason"] = "Rule missing from Docker validation report"

    return prepared_rules