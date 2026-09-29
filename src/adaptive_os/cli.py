"""Command-line entry point for the experiment."""

import argparse
from pathlib import Path
import signal
import sys
import threading
import uuid

from adaptive_os.docker_runtime import DockerError, DockerRuntime, PROJECT, run_preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthetic Docker CPU-share experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "calibrate", "run", "report", "cleanup"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--runs-dir", type=Path, default=Path("runs"))
        if command == "cleanup":
            subparser.add_argument("--run-id", help="exact run UUID to restore and remove; omit to list only")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = DockerRuntime()
    try:
        if args.command == "preflight":
            run_id = str(uuid.uuid4())
            print(f"Preflight run ID: {run_id}", flush=True)
            stop = threading.Event()
            previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                result = run_preflight(runtime, args.runs_dir, run_id=run_id, stop_event=stop)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            print(f"{'SUPPORTED' if result.supported else 'NOT SUPPORTED'}: {result.path}")
            for reason in result.reasons:
                print(reason, file=sys.stderr)
            return result.exit_code
        if args.command == "cleanup":
            if not args.run_id:
                rows = runtime._run(["ps", "-a", "--filter", f"label=project={PROJECT}",
                                     "--format", '{{.ID}}  {{.Label "run_id"}}  {{.Status}}'])
                print(rows or "No project containers found.")
                print("To remove one run: adaptive-os cleanup --run-id RUN_UUID")
            else:
                for container_id in runtime.cleanup_run(args.run_id):
                    print(f"Removed {container_id}")
            return 0
        parser.error(f"{args.command} is not implemented yet; M0 must pass first")
    except (DockerError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
