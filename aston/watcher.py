from aston.ingestion import get_latest_pypi_version
from pathlib import Path
from packaging.requirements import Requirement, InvalidRequirement
from packaging.version import Version, InvalidVersion
import re
import json
from aston.exceptions import ConfigurationError

def is_major_version_change(current_version, target_version):
    try:
        current = Version(current_version)
        target = Version(target_version)
        
        # For pre-1.0 packages, minor releases can introduce breaking changes.
        if current.major == 0 and target.major == 0:
            return target.minor > current.minor

        return target.major > current.major
    except (InvalidVersion, AttributeError):
        # Fallback split logic if version string is unconventional
        try:
            current_major = int(str(current_version).split(".")[0])
            target_major = int(str(target_version).split(".")[0])
            return target_major > current_major
        except Exception:
            return False

def watcher(path: str) -> dict:
    dependencies = {}
    folder_path = Path(path).expanduser().resolve()

    if not folder_path.exists():
        raise ConfigurationError(
            message=f"Repository directory not found: {folder_path}",
            details="Ensure the target workspace path exists before running dependency scanning."
        )

    req_file = folder_path / "requirements.txt"
    if not req_file.is_file():
        raise ConfigurationError(
            message=f"No requirements.txt found in repository path: {folder_path}",
            details="Ensure the target directory contains a valid requirements.txt file before running dependency analysis."
        )

    for line in req_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()    
        if not line or line.startswith("#") or line.startswith("-"):
            continue

        try:
            # Use packaging library for reliable specifier & package name parsing
            req = Requirement(line)
            pkg_name = req.name
            pkg_key = pkg_name.lower()

            declared_version = None
            
            # 1. Prioritize exact pins (==)
            for spec in req.specifier:
                if spec.operator == "==":
                    declared_version = spec.version
                    break

            # 2. Fallback to lower bounds (>=, ~=, >) if no exact pin exists
            if declared_version is None:
                for spec in req.specifier:
                    if spec.operator in (">=", "~=", ">"):
                        declared_version = spec.version
                        break

            # 3. Fallback to any specifier version if bounds aren't directional
            if declared_version is None and req.specifier:
                specs_list = list(req.specifier)
                if specs_list:
                    declared_version = specs_list[0].version

            target_version = get_latest_pypi_version(pkg_key) or declared_version

            dependencies[pkg_key] = {
                "declared": {
                    "specifier": line,
                    "version": declared_version,
                    "source_file": req_file.name,
                    "source_type": "requirements"
                },
                "resolved": None,
                "installed": None,
                "target_version": target_version,
                "provenance_status": "declared"
            }

        except InvalidRequirement:
            # Fallback regex parsing for malformed lines or custom directives
            match = re.match(r"^([a-zA-Z0-9\-_.]+)(.*?)$", line)
            if match:
                pkg_name = match.group(1)
                pkg_key = pkg_name.lower()
                
                version_match = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", line)
                parsed_version = version_match.group(1) if version_match else None

                target_version = get_latest_pypi_version(pkg_key) or parsed_version

                dependencies[pkg_key] = {
                    "declared": {
                        "specifier": line,
                        "version": parsed_version,
                        "source_file": req_file.name,
                        "source_type": "requirements"
                    },
                    "resolved": None,
                    "installed": None,
                    "target_version": target_version,
                    "provenance_status": "declared"
                }

    return dependencies