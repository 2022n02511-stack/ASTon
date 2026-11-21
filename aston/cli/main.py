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
from aston.ledger import log_notification, show_notifications
from aston.Utils.guard import _has_completed_migration
import logging

logger = logging.getLogger(__name__)
from aston.ledger import setup_logging
from aston.report import load_migration_report, render_migration_report


def main():
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show detailed debug logs in the terminal",
    )
    common_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress non-error terminal output",
    )

    parser = argparse.ArgumentParser(
        prog="aston",
        description="ASTon: Automated repo analyzer and migration CLI tool.",
        parents=[common_parser],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Init command
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize ASTon workspace and .gitignore protection",
        parents=[common_parser],
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
        "--force",
        action="store_true",
        help="Re-run migration even if a completed migration already exists in .aston",
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
    check_parser.add_argument(
    "--no-verify",
    action="store_true",
    help="Disable Docker verification for this run",
    )
    check_parser.add_argument(
        "--python-image",
        type=str,
        default=None,
        help="Override Docker Python image (e.g., python:3.11-slim)",
    )
    check_parser.add_argument(
        "--network-mode",
        type=str,
        choices=["auto", "none", "bridge"],
        default=None,
        help="Override container network mode (auto, none, bridge)",
    )
    check_parser.add_argument(
        "--requirements-file",
        type=str,
        default=None,
        help="Override requirements file path",
    )
    check_parser.add_argument(
        "--test-command",
        type=str,
        default=None,
        help="Override repository test command",
    )
    check_parser.add_argument(
        "--build-timeout",
        type=int,
        default=None,
        help="Docker timeout in seconds for image builds",
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

    #ledger command
    # Notifications ledger view command
    notif_parser = subparsers.add_parser(
        "notifications",
        help="View local workspace notification history ledger"
    )
    notif_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Repository directory (defaults to CWD discovery)",
    )

    # Report command
    report_parser = subparsers.add_parser(
        "report",
        help="Show summary report for a migration run",
        parents=[common_parser],
    )
    report_parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Target repository directory (defaults to CWD discovery)",
    )
    report_parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Specific run ID to display (defaults to latest run)",
    )
    report_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the full report as JSON",
    )

    args = parser.parse_args()

    try:
        log_file = setup_logging(
            verbose=getattr(args, "verbose", False),
            quiet=getattr(args, "quiet", False),
        )
        logger.debug("Logging initialized at %s for command '%s'", log_file, args.command)

        # Helper to resolve repo path dynamically for any command
        def get_target_repo():
            return Path(args.path).expanduser().resolve() if args.path else find_repo_root()

        if args.command == "init":
            repo_path = get_target_repo()
            handle_init(repo_path)  # Pass path if your handle_init supports it, or let it bind

        elif args.command == "notifications":
            repo_path = Path(args.path).expanduser().resolve() if args.path else find_repo_root()
            show_notifications(repo_path)    

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

        elif args.command == "report":
            repo_path = get_target_repo()
            logger.debug(
                "Loading migration report for repo=%s run_id=%s as_json=%s",
                repo_path,
                args.run_id,
                args.json,
            )
            report_data, report_path = load_migration_report(
                repo_root=repo_path,
                run_id=args.run_id,
            )
            render_migration_report(
                report=report_data,
                report_path=report_path,
                as_json=args.json,
            )    

        elif args.command == "check":
            repo_path = get_target_repo()
            config = load_config(repo_path)
            mig_config = config.get("migration", {})
            llm_config = config.get("llm", {})
            ver_config = config.get("verification", {})

            provider_name = args.provider or llm_config.get("provider", "mistral")
            model_name = args.model or llm_config.get("model", "codestral-latest")
            per_file_retries = args.repair_retries if args.repair_retries is not None else mig_config.get("per_file_repair_retries", 3)
            sweep_passes = args.sweep_passes if args.sweep_passes is not None else mig_config.get("failed_files_sweep_passes", 2)

            ver_enabled = False if args.no_verify else ver_config.get("enabled", True)
            python_image = args.python_image or ver_config.get("python_image", "python:3.11-slim")
            network_mode = args.network_mode or ver_config.get("network_mode", "auto")
            req_file = args.requirements_file or ver_config.get("requirements_file", "requirements.txt")
            test_cmd = args.test_command if args.test_command is not None else ver_config.get("test_command", "python -m pytest -q")
            file_timeout = (
                args.file_timeout
                if args.file_timeout is not None
                else ver_config.get("file_timeout", mig_config.get("docker_file_timeout_seconds", 300))
            )
            repo_timeout = (
                args.repo_timeout
                if args.repo_timeout is not None
                else ver_config.get("repo_timeout", mig_config.get("docker_repo_timeout_seconds", 300))
            )
            build_timeout = (
                args.build_timeout
                if args.build_timeout is not None
                else ver_config.get("build_timeout", 600)
            )

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
                },
                "verification": {
                    "enabled": ver_enabled,
                    "python_image": python_image,
                    "network_mode": network_mode,
                    "requirements_file": req_file,
                    "test_command": test_cmd,
                    "file_timeout": file_timeout,
                    "repo_timeout": repo_timeout,
                    "build_timeout": build_timeout,
                },
            })

            if args.run:
                if not args.force and _has_completed_migration(repo_path):
                    warning("An existing migration was found in .aston.")
                    info("Run `aston clean` first to clear previous migration artifacts and start fresh.")
                    return

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