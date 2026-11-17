import argparse
from pathlib import Path
from aston.repo_index.repo_map import migrate_repo
from aston.poller import watcher  
from aston.github_integrations.pr import create_migration_pr, verify_clean_git_workspace
from aston.cli.console import rich_success, error, warning, info
from aston.exceptions import AstonError
import sys
from aston.paths import find_repo_root
from aston.cli.commands import (
    handle_config_show,
    handle_doctor,
    handle_init,
    handle_status,
    handle_diff,
    handle_clean,
)
from aston.config import load_config, set_runtime_config

def main():
    parser = argparse.ArgumentParser(
        prog="aston",
        description="ASTon: Automated repo analyzer and migration CLI tool."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Init command
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize ASTon workspace and .gitignore protection"
    )
    init_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Target repository directory (defaults to CWD discovery)",
    )

    # Status command
    status_parser = subparsers.add_parser(
        "status",
        help="List all staged migration files pending review or PR creation"
    )
    status_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Target repository directory (defaults to CWD discovery)",
    )

    # Diff command
    diff_parser = subparsers.add_parser(
        "diff",
        help="Compare working tree files against staged migrations"
    )
    diff_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Target repository directory (defaults to CWD discovery)",
    )

    # Clean command
    clean_parser = subparsers.add_parser(
        "clean",
        help="Wipe out staging area and temporary run artifacts"
    )
    clean_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Target repository directory (defaults to CWD discovery)",
    )

    # Config command
    config_parser = subparsers.add_parser(
        "config",
        help="Manage ASTon configuration settings"
    )
    config_subparsers = config_parser.add_subparsers(dest="subcommand", required=True)
    config_subparsers.add_parser(
        "show",
        help="Display current configuration and environment status"
    )

    # Doctor command
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run system diagnostics and environment health checks"
    )
    doctor_parser.add_argument(
        "--test-llm",
        action="store_true",
        help="Perform a live API round-trip ping to the configured LLM provider"
    )
    doctor_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Path to repository workspace to check (defaults to CWD discovery)"
    )

    # Check command
    check_parser = subparsers.add_parser(
        "check", 
        help="Scan repo dependencies and optionally run migration"
    )
    check_parser.add_argument(
        "--run", 
        action="store_true", 
        help="Execute migration if breaking changes are found"
    )
    check_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Repository directory to analyze (defaults to CWD discovery)",
    )
    check_parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Override LLM provider (e.g., mistral, openai, anthropic)",
    )
    check_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override LLM model (e.g., codestral-latest, gpt-4o)",
    )
    check_parser.add_argument(
        "--repair-retries",
        type=int,
        default=None,
        help="Max per-file LLM repair attempts (overrides config)",
    )
    check_parser.add_argument(
        "--sweep-passes",
        type=int,
        default=None,
        help="Max pipeline sweep passes for remaining failed files (overrides config)",
    )
    check_parser.add_argument(
        "--file-timeout",
        type=int,
        default=None,
        help="Docker timeout in seconds for single file checks",
    )
    check_parser.add_argument(
        "--repo-timeout",
        type=int,
        default=None,
        help="Docker timeout in seconds for repo-wide tests",
    )

    # Watcher command
    watcher_parser = subparsers.add_parser(
        "watcher", 
        help="Poll/watch repository for dependency changes"
    )
    watcher_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Repository directory to watch (defaults to CWD discovery)",
    )

    # PR command
    pr_parser = subparsers.add_parser(
        "pr",
        help="Create a pull request for staged migrated files",
    )
    pr_parser.add_argument(
        "--path",
        type=str,
        default=None,
        required=False,
        help="Directory containing migrated files (defaults to .aston/staging)",
    )
    pr_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        required=False,
        help="Target Git repository root (defaults to CWD discovery)",
    )
    pr_parser.add_argument(
        "--branch",
        type=str,
        default="chore/dependency-migration",
        help="Branch name for the migration",
    )
    pr_parser.add_argument(
        "--base",
        type=str,
        default=None,
        help="Target base branch for the PR (defaults to config value)",
    )
    pr_parser.add_argument(
        "--draft",
        action="store_true",
        help="Create the PR as a draft",
    )
    pr_parser.add_argument(
        "--title",
        type=str,
        default="chore: automated dependency migration via ASTon",
        help="Commit/PR title",
    )
    pr_parser.add_argument(
        "--body",
        type=str,
        default="Automated dependency refactoring and upgrade generated by ASTon.",
        help="Commit/PR description body",
    )

    args = parser.parse_args()

    try:
        # Helper to resolve repo path dynamically for any command
        def get_target_repo():
            return Path(args.path).expanduser().resolve() if args.path else find_repo_root()

        if args.command == "init":
            repo_path = get_target_repo()
            handle_init(repo_path)  # Pass path if your handle_init supports it, or let it bind

        elif args.command == "status":
            repo_path = get_target_repo()
            handle_status(repo_path)

        elif args.command == "diff":
            repo_path = get_target_repo()
            handle_diff(repo_path)

        elif args.command == "clean":
            repo_path = get_target_repo()
            handle_clean(repo_path)

        elif args.command == "config":
            if args.subcommand == "show":
                handle_config_show()

        elif args.command == "doctor":
            repo_path = Path(args.path).expanduser().resolve() if args.path else find_repo_root()
            handle_doctor(test_llm=args.test_llm, repo_path=repo_path)

        elif args.command == "check":
            repo_path = get_target_repo()
            
            config = load_config()
            mig_config = config.get("migration", {})
            llm_config = config.get("llm", {})

            provider_name = args.provider or llm_config.get("provider", "mistral")
            model_name = args.model or llm_config.get("model", "codestral-latest")
            per_file_retries = args.repair_retries if args.repair_retries is not None else mig_config.get("per_file_repair_retries", 3)
            sweep_passes = args.sweep_passes if args.sweep_passes is not None else mig_config.get("failed_files_sweep_passes", 2)
            file_timeout = args.file_timeout if args.file_timeout is not None else mig_config.get("docker_file_timeout_seconds", 300)
            repo_timeout = args.repo_timeout if args.repo_timeout is not None else mig_config.get("docker_repo_timeout_seconds", 300)

            set_runtime_config({
                "llm": {
                    "provider": provider_name,
                    "model": model_name,
                },
                "migration": {
                    "per_file_repair_retries": per_file_retries,
                    "failed_files_sweep_passes": sweep_passes,
                    "docker_file_timeout_seconds": file_timeout,
                    "docker_repo_timeout_seconds": repo_timeout,
                }
            })

            if args.run:
                info("Checking git workspace status...")
                verify_clean_git_workspace(repo_path)

                info(f"Initiating migration pipeline [Provider: {provider_name}, Model: {model_name}, Per-File Retries: {per_file_retries}]...")
                
                migrate_repo(
                    path=repo_path,
                )
            else:
                info(f"Scanning repository at: {repo_path}")
                
        elif args.command == "watcher":
            repo_path = get_target_repo()
            info(f"Starting watcher on: {repo_path}")
            watcher(repo_path)

        elif args.command == "pr":
            config = load_config()
            github_config = config.get("github", {})

            target_base = args.base or github_config.get("default_base_branch", "main")
            is_draft = args.draft or github_config.get("draft_prs", False)

            info(f"Creating Pull Request targeting base branch: '{target_base}' (Draft: {is_draft})")

            create_migration_pr(
                files_path=args.path,
                repo_path=args.repo,
                branch_name=args.branch,
                base_branch=target_base,
                draft=is_draft,
                title=args.title,
                body=args.body,
            )

    except AstonError as exc:
        error(f"[{type(exc).__name__}] {exc.message}")
        if exc.details:
            info(f"Details: {exc.details}")
        sys.exit(exc.exit_code)

    except KeyboardInterrupt:
        info("\nOperation cancelled by user.")
        sys.exit(130)

    except Exception as exc:
        error(f"[UncaughtError] An unexpected error occurred: {exc}")
        sys.exit(1)

if __name__ == "__main__":
    main()