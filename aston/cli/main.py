import argparse
from pathlib import Path
from aston.repo_index.repo_map import migrate_repo
from aston.poller import watcher  
from aston.github_integrations.pr import create_migration_pr

def main():
    parser = argparse.ArgumentParser(prog="aston")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Check command
    check_parser = subparsers.add_parser("check", help="Scan dependencies and optionally run migration")
    check_parser.add_argument("--run", action="store_true", help="Execute migration if breaking changes are found")
    check_parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Repository directory to analyze",
    )

    # PR command with signature-matching arguments
    pr_parser = subparsers.add_parser("pr", help="Create a pull request commit bundle for the migration")
    pr_parser.add_argument(
        "--path",
        type=str,
        default=".",
        help="Repository directory",
    )
    pr_parser.add_argument(
        "--branch",
        type=str,
        default="chore/openai-v1-migration",
        help="Branch name for the migration",
    )
    pr_parser.add_argument(
        "--title",
        type=str,
        default="chore: migrate OpenAI SDK to v1.0+ via ASTon",
        help="Commit/PR title",
    )
    pr_parser.add_argument(
        "--body",
        type=str,
        default="Automated migration refactoring legacy OpenAI v0.28 patterns to modern v1.0+ SDK client syntax.",
        help="Commit/PR description body",
    )

    args = parser.parse_args()

    if args.command == "check":
        watcher(args.path)
        if args.run:
            print("\n🚀 Initiating migration pipeline...")
            migrate_repo(args.path)
            
    elif args.command == "pr":
        create_migration_pr(
            repo_path=args.path,
            branch_name=args.branch,
            title=args.title,
            body=args.body
        )

if __name__ == "__main__":
    main()