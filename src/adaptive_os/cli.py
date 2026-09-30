"""Command-line entry point for the experiment."""

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import signal
import sys
import threading
import uuid

from adaptive_os.docker_runtime import DockerError, DockerRuntime, PROJECT, run_preflight
from adaptive_os.storage import RunStore
from adaptive_os.workloads import TrialConfig, WORKLOAD_IMAGE, WorkloadProfile, run_trial, select_profiles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native desktop playground and legacy CPU-share experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    desktop = commands.add_parser('desktop', help='launch the native single-sandbox desktop app')
    desktop.add_argument('--data-dir', type=Path)
    for command in ("preflight", "calibrate", "run", "report", "cleanup"):
        subparser = commands.add_parser(command, help='legacy multi-container benchmark workflow')
        subparser.add_argument("--runs-dir", type=Path, default=Path("runs"))
        if command == "cleanup":
            subparser.add_argument("--run-id", help="exact run UUID to restore and remove; omit to list only")
        if command in ("calibrate", "run"):
            subparser.add_argument("--cpu", default="auto")
        if command == "calibrate":
            subparser.add_argument("--measurement-sec", type=int, default=15)
            subparser.add_argument("--budgets-ms", type=float, nargs="+", default=[4, 6, 8, 10, 12, 14],
                                   help="bounded work budgets to test before profiles are frozen")
        if command == "run":
            subparser.add_argument("--policy", action="append", choices=["default"])
            subparser.add_argument("--profile", action="append", choices=["light", "contention"])
            subparser.add_argument("--trials", type=int, default=5)
            subparser.add_argument("--duration-sec", type=int, default=60)
            subparser.add_argument("--seed", type=int, default=20260929)
            subparser.add_argument("--smoke", action="store_true")
    return parser


@contextmanager
def stopping():
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def experiment_environment(runtime, args, stop):
    preflight = run_preflight(runtime, args.runs_dir, stop_event=stop)
    if not preflight.supported:
        raise ValueError(f"preflight failed: {preflight.path}: {'; '.join(preflight.reasons)}")
    data = json.loads(preflight.path.read_text())
    cpu = data["selected_cpu"] if args.cpu == "auto" else int(args.cpu)
    if cpu not in data["allowed_cpus"]:
        raise ValueError(f"CPU {cpu} is not in Docker's affinity list")
    image = runtime._run(["image", "inspect", "--format", "{{.Id}}", WORKLOAD_IMAGE])
    if not image.startswith("sha256:"):
        raise ValueError("build Dockerfile.workloads before calibrating")
    return cpu, image, {key: data[key] for key in ("host", "docker_host", "docker_version", "cgroup_version", "allowed_cpus")}


def calibrate(runtime, args, stop):
    if args.measurement_sec < 5 or args.measurement_sec % 5:
        raise ValueError("calibration measurement seconds must be a positive multiple of five")
    budgets = sorted(set(args.budgets_ms))
    if not budgets or any(not math.isfinite(budget) or not 0 < budget < 20 for budget in budgets):
        raise ValueError("calibration budgets must be finite and between 0 and 20 ms")
    cpu, image, environment = experiment_environment(runtime, args, stop)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.runs_dir / f"calibration-{stamp}.json"
    settings = {"valid": False, "cpu": cpu, "image_id": image, "created_at_utc": stamp,
                "environment": environment, "budgets_ms": budgets, "attempts": [], "profiles": {}}
    profiles_path = args.runs_dir / "profiles.json"
    profiles_path.write_text(json.dumps(settings, indent=2) + "\n")
    try:
        with RunStore(args.runs_dir / "runs.sqlite3") as store:
            for index, budget in enumerate(budgets):
                row = {"budget_ms": budget}
                settings["attempts"].append(row)
                for arm in ("solo", "paired"):
                    config = TrialConfig(WorkloadProfile(f"calibration-{budget}-{arm}", 20, budget), cpu,
                                         measurement_sec=args.measurement_sec, background=arm == "paired",
                                         image=image, block_index=index, smoke=True)
                    print(f"Calibration {budget} ms {arm}: {config.run_id}", flush=True)
                    result = run_trial(config, runtime, store, metadata=environment, stop_event=stop)
                    row[arm] = {"run_id": result.run_id, "valid": result.valid, "reason": result.reason, **result.metrics}
                    output.write_text(json.dumps({**settings, "current_attempt": row}, indent=2) + "\n")
                    if not result.valid:
                        raise ValueError(f"calibration trial invalid: {result.reason}; evidence: {output}")
                    print(f"  miss rate {result.metrics['miss_rate']:.2%}; p95 lateness {result.metrics['p95_lateness_ms']:.3f} ms", flush=True)
                output.write_text(json.dumps(settings, indent=2) + "\n")
            settings["profiles"] = select_profiles(settings["attempts"])
            settings["valid"] = True
    except (ValueError, DockerError, OSError) as exc:
        settings["reason"] = str(exc)
        raise
    finally:
        output.write_text(json.dumps(settings, indent=2) + "\n")
        profiles_path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Calibrated profiles: {profiles_path}")
    return 0


def run_baselines(runtime, args, stop):
    if args.trials < 1 or (not args.smoke and (args.trials < 5 or not 45 <= args.duration_sec <= 60)):
        raise ValueError("registered trials need at least five 45–60 second measurements; use --smoke for shorter checks")
    profiles = json.loads((args.runs_dir / "profiles.json").read_text())
    if not profiles.get("valid"):
        raise ValueError("calibration has not passed; inspect calibration JSON before running comparisons")
    cpu, image, environment = experiment_environment(runtime, args, stop)
    if profiles["cpu"] != cpu or profiles["image_id"] != image:
        raise ValueError("CPU/image changed since calibration; recalibrate")
    with RunStore(args.runs_dir / "runs.sqlite3") as store:
        for block in range(args.trials):
            for name in args.profile or ["contention"]:
                profile = WorkloadProfile(**profiles["profiles"][name])
                config = TrialConfig(profile, cpu, measurement_sec=args.duration_sec, image=image,
                                     seed=args.seed, block_index=block, smoke=args.smoke)
                print(f"Default {name} block {block}: {config.run_id}", flush=True)
                result = run_trial(config, runtime, store, metadata=environment, stop_event=stop)
                print(json.dumps(asdict(result)), flush=True)
                if not result.valid:
                    return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'desktop':
        try:
            from adaptive_os.desktop import main as desktop_main
        except ImportError:
            print('Install desktop support: python -m pip install -e ".[desktop]"', file=sys.stderr)
            return 1
        return desktop_main(['--data-dir', str(args.data_dir)] if args.data_dir else [])
    runtime = DockerRuntime()
    try:
        if args.command == "preflight":
            run_id = str(uuid.uuid4())
            print(f"Preflight run ID: {run_id}", flush=True)
            with stopping() as stop:
                result = run_preflight(runtime, args.runs_dir, run_id=run_id, stop_event=stop)
            print(f"{'SUPPORTED' if result.supported else 'NOT SUPPORTED'}: {result.path}")
            for reason in result.reasons:
                print(reason, file=sys.stderr)
            return result.exit_code
        if args.command in ("calibrate", "run"):
            with stopping() as stop:
                return (calibrate if args.command == "calibrate" else run_baselines)(runtime, args, stop)
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
        parser.error("comparison reporting is planned for M3; raw calibration evidence is in the runs directory")
    except (DockerError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
