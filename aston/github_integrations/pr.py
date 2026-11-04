import os
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv
from aston.cli.console import rich_success, error, warning, info
import re
import shutil
import tempfile
from aston.exceptions import GitHubError, ConfigurationError
from aston.Utils.utility import request_with_backoff
import subprocess
from aston.exceptions import ConfigurationError

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_API = "https://api.github.com"

def verify_clean_git_workspace(repo_path: Path):
    """Ensure the user has no uncommitted changes before starting."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.stdout.strip():
        raise ConfigurationError(
            message="Uncommitted changes detected in repository workspace.",
            details="Please commit or stash your changes before running ASTon migration."
        )

# \# def clone_repo_from_github(github_repo: str, target_dir: str = "./workspace") -> Path | None:
# \#     """
# \#     Clones a GitHub repository locally using the GITHUB_TOKEN for authentication.
# \#     Example github_repo: 'octocat/Hello-World' or full URL.
# \#     """
# \#     target_path = Path(target_dir).expanduser().resolve()
# \#     target_path.mkdir(parents=True, exist_ok=True)

# \#     # Normalize inputs like '[https://github.com/owner/repo.git](https://github.com/owner/repo.git)' down to 'owner/repo'
# \#     clean_slug = github_repo.strip()
# \#     if "github.com" in clean_slug:
# \#         clean_slug = clean_slug.rstrip("/").removesuffix(".git")
# \#         clean_slug = "/".join(clean_slug.split("/")[-2:])

# \#     # Construct authenticated clone URL if token exists
# \#     if GITHUB_TOKEN:
# \#         clone_url = f"[https://x-access-token:{GITHUB_TOKEN}@github.com/{clean_slug}.git](https://x-access-token:{GITHUB_TOKEN}@github.com/{clean_slug}.git)"
# \#     else:
# \#         clone_url = f"[https://github.com/{clean_slug}.git](https://github.com/{clean_slug}.git)"

# \#     repo_folder_name = clean_slug.split("/")[-1]
# \#     repo_local_path = target_path / repo_folder_name

# \#     # If already cloned, pull latest changes; otherwise clone fresh
# \#     if repo_local_path.exists() and (repo_local_path / ".git").exists():
# \#         info(f"Repository already exists locally at {repo_local_path}. Pulling latest changes...")
# \#         try:
# \#             subprocess.run(["git", "pull"], cwd=repo_local_path, check=True, text=True, capture_output=True)
# \#         except subprocess.CalledProcessError as e:
# \#             warning(f"'git pull' failed: {e.stderr.strip()}. Continuing with existing local state.")
# \#     else:
# \#         info(f"Cloning repository '{clean_slug}' into {repo_local_path}...")
# \#         result = subprocess.run(
# \#             ["git", "clone", clone_url, str(repo_local_path)],
# \#             stdout=subprocess.PIPE,
# \#             stderr=subprocess.PIPE,
# \#             text=True
# \#         )
# \#         if result.returncode != 0:
# \#             error(f"Failed to clone repository: {result.stderr.strip()}")
# \#             return None

# \#     success(f"Successfully prepared repository at: {repo_local_path}")
# \#     return repo_local_path

def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Aston-dependency-migration-tool",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def run_git_command(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """Runs a git command in the repository workspace."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return True, result.stdout.strip()

    except subprocess.CalledProcessError as e:
        message = (e.stderr or e.stdout or "").strip()
        if not message:
            message = f"Git exited with code {e.returncode}"
        return False, message


def get_git_remote_repo(repo_path: Path) -> str | None:
    """Attempts to extract 'owner/repo' from the local git remote config."""

    ok, output = run_git_command(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_path,
    )

    if not ok or not output:
        return None

    match = re.search(
        r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$",
        output.strip(),
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return None



def create_migration_pr(
    files_path: str,
    repo_path: str,
    branch_name: str,
    title: str,
    body: str,
) -> str | None:
    """
    Commits selected migration files in an isolated Git worktree,
    pushes them to GitHub, and creates a pull request.
    """

    root = Path(repo_path).expanduser().resolve()
    migration_path = Path(files_path).expanduser().resolve()

    if not GITHUB_TOKEN:
        raise ConfigurationError(
            message="GITHUB_TOKEN environment variable is missing.",
            details="Set your GITHUB_TOKEN in your environment before attempting automated PR creation."
        )

    if not root.is_dir():
        raise ConfigurationError(
            message=f"Git repository directory not found: {root}",
            details="Ensure the target repository path points to a valid local directory."
        )

    if not migration_path.is_dir():
        raise ConfigurationError(
            message=f"Migration output directory not found: {migration_path}",
            details="Ensure the migration runner has successfully generated outputs prior to PR creation."
        )

    source_files = [
        path for path in migration_path.rglob("*")
        if path.is_file()
    ]

    if not source_files:
        warning(f"No migration files found in: {migration_path}")
        return None

    github_repo = get_git_remote_repo(root)

    if not github_repo:
        raise GitHubError(
            message="Could not detect a valid GitHub 'origin' remote URL.",
            details=f"Inspect git remotes in repository root: {root}"
        )

    info(
        f"\nPreparing Pull Request for '{github_repo}' "
        f"on branch '{branch_name}'..."
    )

    repo_check_url = f"{GITHUB_API}/repos/{github_repo}"

    try:
        check_response = request_with_backoff(
            "GET",
            repo_check_url,
            max_retries=3,
            backoff_factor=2.0,
            headers=_github_headers(),
        )
    except requests.RequestException as exc:
        raise GitHubError(
            message=f"GitHub API connectivity check failed for repository '{github_repo}'.",
            details=str(exc)
        ) from exc

    if check_response.status_code != 200:
        raise GitHubError(
            message=f"Target repository '{github_repo}' does not exist or your GITHUB_TOKEN lacks access permissions.",
            details=f"HTTP Status: {check_response.status_code}\n{check_response.text}"
        )

    worktree_path = None

    try:
        with tempfile.TemporaryDirectory(prefix="aston_pr_") as temp_dir:

            worktree_path = Path(temp_dir) / "repo"

            # Fetch the latest target branch.
            ok, err = run_git_command(
                ["git", "fetch", "origin", "main"],
                cwd=root,
            )

            if not ok:
                raise GitHubError(
                    message="Failed to fetch origin/main from remote repository.",
                    details=err
                )

            # Create an isolated branch based on origin/main.
            ok, err = run_git_command(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    "origin/main",
                ],
                cwd=root,
            )

            if not ok:
                raise GitHubError(
                    message=f"Failed to create isolated migration worktree branch '{branch_name}'.",
                    details=err
                )

            # Copy migrated files into the isolated worktree.
            relative_files = []

            for source_file in source_files:
                relative_path = source_file.relative_to(migration_path)
                destination = worktree_path / relative_path

                destination.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                shutil.copy2(source_file, destination)
                relative_files.append(relative_path.as_posix())

            info(
                f"Copied {len(relative_files)} migrated file(s) "
                "into isolated Git worktree."
            )

            # Stage only migrated files.
            ok, err = run_git_command(
                ["git", "add", "--", *relative_files],
                cwd=worktree_path,
            )

            if not ok:
                raise GitHubError(
                    message="Failed to stage migration files in worktree.",
                    details=err
                )

            # Commit changes.
            ok, err = run_git_command(
                ["git", "commit", "-m", title],
                cwd=worktree_path,
            )

            if not ok:
                raise GitHubError(
                    message="Failed to commit migrated files.",
                    details=err
                )

            # Push isolated branch.
            info(f"Pushing branch {branch_name} to remote...")

            ok, err = run_git_command(
                ["git", "push", "-u", "origin", branch_name],
                cwd=worktree_path,
            )

            if not ok:
                raise GitHubError(
                    message=f"Failed to push branch '{branch_name}' to remote origin.",
                    details=err
                )

            # Create GitHub pull request.
            pr_url = f"{GITHUB_API}/repos/{github_repo}/pulls"

            payload = {
                "title": title,
                "body": body,
                "head": branch_name,
                "base": "main",
            }

            try:
                response = request_with_backoff(
                    "POST",
                    pr_url,
                    max_retries=3,
                    backoff_factor=2.0,
                    headers=_github_headers(),
                    json=payload,
                )
            except requests.RequestException as exc:
                raise GitHubError(
                    message="Network request failed while creating GitHub Pull Request.",
                    details=str(exc)
                ) from exc

            if response.status_code in (201, 200):
                html_url = response.json().get("html_url")
                rich_success(
                    f"Successfully created Pull Request: {html_url}"
                )
                return html_url

            raise GitHubError(
                message=f"GitHub API rejected pull request creation (HTTP {response.status_code}).",
                details=response.text
            )

    finally:
        # Remove the temporary worktree without touching the original repo.
        if worktree_path is not None and worktree_path.exists():
            run_git_command(
                [
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                ],
                cwd=root,
            )
