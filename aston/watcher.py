from aston.ingestion import get_latest_pypi_version
from pathlib import Path
from packaging.version import Version
import re
import json
from aston.exceptions import ConfigurationError

def is_major_version_change(current_version, target_version):
    current_major = int(current_version.split(".")[0])
    target_major = int(target_version.split(".")[0])
    # print(current_version)
    # print(target_version)

    return target_major > current_major

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

        # Extract package name and check if it's an exact version pin vs range/constraint
        # Extract package name and version constraints dynamically
        match = re.match(r"^([a-zA-Z0-9\-_]+)(.*?)$", line)
        if match:
            pkg_name = match.group(1)
            pkg_key = pkg_name.lower()
            
            # Extract any version numbers found in the line (e.g., from '==1.2.3' or '>=1.0.0')
            version_match = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", line)
            parsed_version = version_match.group(1) if version_match else None

            target_version = get_latest_pypi_version(pkg_key) or parsed_version

            dependencies[pkg_key] = {
                "declared": {
                    "specifier": line,
                    "version": parsed_version,  # Falls back to extracted version instead of strict ==
                    "source_file": req_file.name,
                    "source_type": "requirements"
                },
                "resolved": None,
                "installed": None,
                "target_version": target_version,
                "provenance_status": "declared"
            }

    return dependencies


# sot = watcher('demo/')