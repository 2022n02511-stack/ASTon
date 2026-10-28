import argparse
from pathlib import Path
from aston.repo_index.repo_map import migrate_repo
from aston.poller import watcher  
from aston.github_integrations.pr import create_pull_request  

def main():
    parser = argparse.ArgumentParser(prog="aston")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Keep your exact check command structure with --run and --path
    check_parser = subparsers.add_parser("check", help="Scan dependencies and optionally run migration")
    check_parser.add_argument("--run", action="store_true", help="Execute migration if breaking changes are found")
    check_parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Repository directory to analyze",
    )

    # PR creation command
    pr_parser = subparsers.add_parser("pr", help="Create a pull request with the migrated code")
    pr_parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Repository directory",
    )

    args = parser.parse_args()

    if args.command == "check":
        # First, run the dependency watcher check
        breaking_detected = watcher(args.path)
        
        # If --run was passed, trigger the migration pipeline
        if args.run:
            print("\n🚀 Initiating migration pipeline...")
            migrate_repo(args.path)
            
    elif args.command == "pr":
        create_pull_request(args.path)

if __name__ == "__main__":
    main()