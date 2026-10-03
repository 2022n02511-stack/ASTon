import argparse
from pathlib import Path
from aston.pipeline import run_for_repo


def main():
    parser = argparse.ArgumentParser(prog="aston")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--run", action="store_true")
    check_parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd(),
        help="Repository directory to analyze",
    )

    args = parser.parse_args()

    if args.command == "check" and args.run:
        run_for_repo(args.path)


if __name__ == "__main__":
    main()