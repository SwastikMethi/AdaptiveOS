"""Command-line entry point for the experiment."""

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthetic Docker CPU-share experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "calibrate", "run", "report", "cleanup"):
        commands.add_parser(command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    parser.error(f"{args.command} is not implemented yet")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
