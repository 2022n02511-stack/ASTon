import re
from pathlib import Path
from aston.ingestion import get_latest_pypi_version


# --- 1. Updated Watcher (Dependency Provenance) ---
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

                match = re.match(r"^([a-zA-Z0-9\-_]+)\s*(?:[><=!]=?~?=?\s*([0-9\.]+.*))?", line)
                if match:
                    pkg_name, current_version = match.groups()
                    pkg_key = pkg_name.lower()
                    cur_ver = current_version.strip() if current_version else None
                    target_version = get_latest_pypi_version(pkg_key)

                    specifier = f"{pkg_key}=={cur_ver}" if cur_ver else pkg_key

                    dependencies[pkg_key] = {
                        "declared": {
                            "specifier": specifier,
                            "version": cur_ver,
                            "source_file": file.name,
                            "source_type": "requirements"
                        },
                        "target_version": target_version if target_version else cur_ver,
                        "provenance_status": "declared"
                    }

    return dependencies