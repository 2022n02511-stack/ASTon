import os
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_API = "https://api.github.com"

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
            check=True
        )
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()


def create_migration_pr(repo_path: str, github_repo: str, branch_name: str, title: str, body: str) -> str | None:
    """
    Creates a git branch, commits changes, pushes to remote, and opens a GitHub PR.
    """
    root = Path(repo_path).expanduser().resolve()

    if not GITHUB_TOKEN:
        print("⚠️ GITHUB_TOKEN is not set. Skipping automated PR creation.")
        return None

    print(f"\n🚀 Preparing Pull Request for {github_repo} on branch '{branch_name}'...")

    # 1. Checkout new branch
    success, err = run_git_command(["git", "checkout", "-b", branch_name], cwd=root)
    if not success:
        # Try checking out if branch already exists
        success, err = run_git_command(["git", "checkout", branch_name], cwd=root)
        if not success:
            print(f"❌ Failed to create/checkout git branch: {err}")
            return None

    # 2. Stage changes (migrated files and updated requirements)
    success, err = run_git_command(["git", "add", "."], cwd=root)
    if not success:
        print(f"❌ Failed to stage files for commit: {err}")
        return None

    # 3. Commit changes
    success, err = run_git_command(["git", "commit", "-m", title], cwd=root)
    if not success and "nothing to commit" not in err:
        print(f"❌ Failed to commit changes: {err}")
        return None

    # 4. Push branch to remote
    print(f"pushing branch {branch_name} to remote...")
    success, err = run_git_command(["git", "push", "-u", "origin", branch_name], cwd=root)
    if not success:
        print(f"❌ Failed to push branch to remote: {err}")
        return None

    # 5. Call GitHub API to create Pull Request
    pr_url = f"{GITHUB_API}/repos/{github_repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": branch_name,
        "base": "main"  # or 'master' depending on your default branch
    }

    response = requests.post(pr_url, headers=_github_headers(), json=payload)
    if response.status_code in (201, 200):
        pr_data = response.json()
        html_url = pr_data.get("html_url")
        print(f"🎉 Successfully created Pull Request: {html_url}")
        return html_url
    else:
        print(f"❌ Failed to create GitHub PR ({response.status_code}): {response.text}")
        return None