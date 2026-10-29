from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version
from aston.ingestion import get_latest_pypi_version


MAX_WORKERS = 10


def is_major_version_change(
    current_version: str,
    target_version: str,
) -> bool:
    """Return whether the target crosses a major-version boundary."""
    try:
        current = Version(current_version)
        target = Version(target_version)

        # For pre-1.0 packages, minor releases can introduce breaking changes.
        if current.major == 0 and target.major == 0:
            return target.minor > current.minor

        return target.major > current.major

    except InvalidVersion:
        return False


def _check_package(
    line: str,
) -> tuple[str, str | None, str | None, str | None] | None:
    """Fetch the latest PyPI version for one requirement."""
    line = line.strip()

    if not line or line.startswith("#") or line.startswith("-"):
        return None

    try:
        req = Requirement(line)
        package_name = req.name

        # Exact pins are the most reliable declared-version baseline.
        # For ranges, choose a lower bound when available.
        declared_version = None

        for spec in req.specifier:
            if spec.operator == "==":
                declared_version = spec.version
                break

        if declared_version is None:
            lower_bounds = [
                spec.version
                for spec in req.specifier
                if spec.operator in (">=", "~=")
            ]
            if lower_bounds:
                declared_version = lower_bounds[0]

        latest_version = get_latest_pypi_version(package_name)

        if latest_version is None:
            return package_name, declared_version, None, "PyPI lookup failed"

        # Validate the version returned by the registry.
        Version(latest_version)

        return package_name, declared_version, latest_version, None

    except (InvalidVersion, ValueError) as exc:
        return line, None, None, f"Invalid requirement/version: {exc}"
    except Exception as exc:
        return line, None, None, str(exc)


def watcher(repo_path: str | Path) -> None:
    """
    Check requirements against PyPI and report potential major-version changes.

    This is a one-time check, not a continuously running polling loop.
    """
    repo_dir = Path(repo_path).expanduser().resolve()
    req_file = repo_dir / "requirements.txt"

    if not req_file.is_file():
        print(f" No requirements.txt found in {repo_dir}")
        return

    try:
        lines = req_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f" Could not read {req_file}: {exc}")
        return

    # Ignore blank lines, comments, and pip directives.
    requirement_lines = [
        line for line in lines
        if line.strip() and not line.strip().startswith(("#", "-"))
    ]

    if not requirement_lines:
        print("No package requirements found.")
        return

    print(
        f"Checking {len(requirement_lines)} requirement lines "
        f"for {repo_dir}..."
    )

    results = []
    failures = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_check_package, line): line
            for line in requirement_lines
        }

        total = len(futures)

        for completed, future in enumerate(as_completed(futures), start=1):
            try:
                result = future.result()
                if result:
                    package, declared, latest, error = result

                    if error:
                        failures.append((package, error))
                    else:
                        results.append((package, declared, latest))

            except Exception as exc:
                failures.append((futures[future], str(exc)))

            print(
                f"\rProgress: {completed}/{total} requirement lines",
                end="",
                flush=True,
            )

    print("\n")

    breaking_changes = []
    unpinned = []

    for package, declared, latest in results:
        if not declared:
            unpinned.append(package)
            print(
                f" {package}: latest version is {latest}; "
                "no exact declared version is available for comparison."
            )
            continue

        try:
            current = Version(declared)
            target = Version(latest)
        except InvalidVersion:
            failures.append((package, "Could not compare versions"))
            continue

        if target <= current:
            continue

        if is_major_version_change(str(current), str(target)):
            breaking_changes.append((package, str(current), str(target)))
            print(
                f" Potential breaking upgrade: {package} "
                f"{current} -> {target}"
            )

    if failures:
        print(f"\n {len(failures)} package lookup(s) failed:")
        for package, error in failures:
            print(f"  - {package}: {error}")

    if breaking_changes:
        print(
            "\nPotential major-version changes found. "
            "A major version change does not guarantee a breaking change."
        )
        print(
            "Review the migration before running: "
            f"aston check --run --path \"{repo_dir}\""
        )
    elif failures:
        print(
            "\n Check incomplete: some packages could not be checked. "
            "Do not treat this result as confirmation of compatibility."
        )
    else:
        print("\n No newer major-version changes detected.")

    if unpinned:
        print(
            f"\n {len(unpinned)} package(s) lack a usable version "
            "baseline; their compatibility could not be established."
        )
