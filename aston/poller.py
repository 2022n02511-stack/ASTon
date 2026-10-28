from pathlib import Path
from packaging.requirements import Requirement
from packaging.version import Version
from aston.ingestion import get_latest_pypi_version

def is_major_version_change(current_version: str, target_version: str) -> bool:
    try:
        return Version(target_version).major > Version(current_version).major
    except Exception:
        try:
            return int(target_version.split(".")[0]) > int(current_version.split(".")[0])
        except Exception:
            return False

def watcher(repo_path: str | Path) -> None:
    """
    Reads the repository's requirements.txt, checks installed versus target package versions,
    and prints whether breaking changes are detected requiring migration.
    """
    repo_dir = Path(repo_path)
    req_file = repo_dir / "requirements.txt"
    
    if not req_file.is_file():
        print(f"⚠️ No requirements.txt found in {repo_dir}")
        return

    breaking_changes_detected = False

    for line in req_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue

        try:
            req = Requirement(line)
            pkg_name = req.name
            pkg_key = pkg_name.lower()

            declared_version = None
            for spec in req.specifier:
                if spec.operator == "==":
                    declared_version = spec.version
                    break

            target_version = get_latest_pypi_version(pkg_key)

            if declared_version and target_version:
                if is_major_version_change(declared_version, target_version):
                    breaking_changes_detected = True
                    print(f"🚨 Breaking change detected for '{pkg_name}': declared {declared_version} -> target {target_version}")
            elif declared_version and Version(declared_version) < Version("1.0.0"):
                breaking_changes_detected = True
                print(f"🚨 Legacy pre-v1 version detected for '{pkg_name}': {declared_version}")

        except Exception:
            continue

    if breaking_changes_detected:
        print("\n⚠️ Breaking changes detected. Run migration command to perform migration.")
        print("Command: aston check --run --path (path_value)")
    else:
        print("✨ No breaking changes detected. All dependencies are compatible.")