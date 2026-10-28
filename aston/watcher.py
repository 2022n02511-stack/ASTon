from aston.ingestion import get_latest_pypi_version
from pathlib import Path
from packaging.version import Version
import re
import json

def is_major_version_change(current_version, target_version):
    current_major = int(current_version.split(".")[0])
    target_major = int(target_version.split(".")[0])
    # print(current_version)
    # print(target_version)

    return target_major > current_major

def watcher(path: str) -> dict:
    dependencies = {}
    folder_path = Path(path)

    if not folder_path.exists():
        return dependencies

    for file in folder_path.iterdir():
        if file.name == 'requirements.txt':
            for line in file.read_text(encoding="utf-8").splitlines():
                line = line.strip()    
                if not line or line.startswith("#") or line.startswith("-"):
                    continue

                # Extract package name and check if it's an exact version pin vs range/constraint
                match = re.match(r"^([a-zA-Z0-9\-_]+)", line)
                if match:
                    pkg_name = match.group(1)
                    pkg_key = pkg_name.lower()
                    
                    # Check for exact version pin (e.g., openai==0.28.1)
                    exact_match = re.match(r"^[a-zA-Z0-9\-_]+\s*==\s*([0-9\.]+)$", line)
                    exact_version = exact_match.group(1) if exact_match else None

                    target_version = get_latest_pypi_version(pkg_key)

                    dependencies[pkg_key] = {
                        "declared": {
                            "specifier": line,
                            "version": exact_version,  # Null if it's a constraint range like 2.31,<3
                            "source_file": file.name,
                            "source_type": "requirements"
                        },
                        "resolved": None,
                        "installed": None,
                        "target_version": target_version if target_version else exact_version,
                        "provenance_status": "declared"
                    }

    # print(json.dumps(dependencies, indent=3))
    return dependencies 


# sot = watcher('demo/')