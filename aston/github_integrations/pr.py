import os
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_API = "https://api.github.com"

# def clone_repo_from_github(github_repo: str, target_dir: str = "./workspace") -> Path | None:
#     """
#     Clones a GitHub repository locally using the GITHUB_TOKEN for authentication.
#     Example github_repo: 'octocat/Hello-World' or full URL.
#     """
#     target_path = Path(target_dir).expanduser().resolve()
#     target_path.mkdir(parents=True, exist_ok=True)

#     # Normalize inputs like 'https://github.com/owner/repo.git' down to 'owner/repo'
#     clean_slug = github_repo.strip()
#     if "github.com" in clean_slug:
#         clean_slug = clean_slug.rstrip("/").removesuffix(".git")
#         clean_slug = "/".join(clean_slug.split("/")[-2:])

#     # Construct authenticated clone URL if token exists
#     if GITHUB_TOKEN:
#         clone_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{clean_slug}.git"
#     else:
#         clone_url = f"https://github.com/{clean_slug}.git"

#     repo_folder_name = clean_slug.split("/")[-1]
#     repo_local_path = target_path / repo_folder_name

#     # If already cloned, pull latest changes; otherwise clone fresh
#     if repo_local_path.exists() and (repo_local_path / ".git").exists():
#         print(f"📥 Repository already exists locally at {repo_local_path}. Pulling latest changes...")
#         try:
#             subprocess.run(["git", "pull"], cwd=repo_local_path, check=True, text=True, capture_output=True)
#         except subprocess.CalledProcessError as e:
#             print(f"⚠️ Warning: 'git pull' failed: {e.stderr.strip()}. Continuing with existing local state.")
#     else:
#         print(f"📥 Cloning repository '{clean_slug}' into {repo_local_path}...")
#         result = subprocess.run(
#             ["git", "clone", clone_url, str(repo_local_path)],
#             stdout=subprocess.PIPE,
#             stderr=subprocess.PIPE,
#             text=True
#         )
#         if result.returncode != 0:
#             print(f"❌ Failed to clone repository: {result.stderr.strip()}")
#             return None

#     print(f"✅ Successfully prepared repository at: {repo_local_path}")
#     return repo_local_path

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


def get_git_remote_repo(repo_path: Path) -> str | None:
    """Attempts to extract 'owner/repo' from the local git remote config."""
    success, output = run_git_command(["git", "remote", "get-url", "origin"], cwd=repo_path)
    if not success or not output:
        return None
    
    # Parse github.com/owner/repo from URL (supports HTTPS and SSH)
    # e.g., https://github.com/owner/repo.git or git@github.com:owner/repo.git
    import re
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", output)
    if match:
        return match.group(1)
    return None


def create_migration_pr(repo_path: str, branch_name: str, title: str, body: str) -> str | None:
    """
    Safely creates a git branch, commits changes, pushes to remote, and opens a GitHub PR.
    """
    root = Path(repo_path).expanduser().resolve()

    if not GITHUB_TOKEN:
        print("⚠️ GITHUB_TOKEN is not set. Skipping automated PR creation.")
        return None

    # 1. Automatically detect the target GitHub repository from git remote
    github_repo = get_git_remote_repo(root)
    if not github_repo:
        print("❌ Could not detect a valid GitHub 'origin' remote in this repository.")
        print("   Automated PR creation requires a linked GitHub remote repository.")
        return None

    print(f"\n🚀 Preparing Pull Request for '{github_repo}' on branch '{branch_name}'...")

    # 2. Check if repo exists on GitHub via API before pushing
    repo_check_url = f"{GITHUB_API}/repos/{github_repo}"
    check_response = requests.get(repo_check_url, headers=_github_headers())
    if check_response.status_code != 200:
        print(f"❌ Target repository '{github_repo}' does not exist on GitHub or token lacks access.")
        return None

    # 3. Checkout new branch
    success, err = run_git_command(["git", "checkout", "-b", branch_name], cwd=root)
    if not success:
        success, err = run_git_command(["git", "checkout", branch_name], cwd=root)
        if not success:
            print(f"❌ Failed to create/checkout git branch: {err}")
            return None

    # 4. Stage changes
    success, err = run_git_command(["git", "add", "."], cwd=root)
    if not success:
        print(f"❌ Failed to stage files for commit: {err}")
        return None

    # 5. Commit changes
    success, err = run_git_command(["git", "commit", "-m", title], cwd=root)
    if not success and "nothing to commit" not in err:
        print(f"❌ Failed to commit changes: {err}")
        return None

    # 6. Push branch to remote
    print(f"Pushing branch {branch_name} to remote...")
    success, err = run_git_command(["git", "push", "-u", "origin", branch_name], cwd=root)
    if not success:
        print(f"❌ Failed to push branch to remote: {err}")
        return None

    # 7. Call GitHub API to create Pull Request
    pr_url = f"{GITHUB_API}/repos/{github_repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": branch_name,
        "base": "main"  # Adjust if the default branch is 'master'
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