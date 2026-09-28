# sma/cli/main.py

import argparse
from aston.pipeline import run_for_repo


def main():
    parser = argparse.ArgumentParser(prog="aston")

    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--run", action="store_true")

    args = parser.parse_args()

    if args.command == "check":
        if args.run:
            run_for_repo()
        else:
            print("Use: aston check --run")

if __name__ == "__main__":
    main()