"""Continuous deterministic SHA-256 work with one-second throughput samples."""

import argparse
import hashlib
import json
import math
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-sec", type=float, default=60)
    parser.add_argument("--start-ns", type=int)
    args = parser.parse_args()
    if not math.isfinite(args.duration_sec) or args.duration_sec <= 0:
        parser.error("duration must be finite and positive")
    if args.start_ns is not None and args.start_ns < 0:
        parser.error("start time must be nonnegative")
    start = args.start_ns if args.start_ns is not None else time.monotonic_ns()
    delay = start - time.monotonic_ns()
    if delay > 0:
        time.sleep(delay / 1_000_000_000)
    cpu_start = time.thread_time_ns()
    end = start + round(args.duration_sec * 1_000_000_000)
    interval_start, sequence, units, digest = start, 0, 0, bytes(32)

    def emit(now):
        nonlocal interval_start, sequence, units
        print(json.dumps({"schema_version": 1, "type": "background_summary", "start_ns": start,
                          "monotonic_ns": now, "process_cpu_ns": time.thread_time_ns() - cpu_start,
                          "sequence": sequence, "interval_start_ns": interval_start,
                          "interval_ns": now - interval_start, "work_units": units,
                          "checksum": digest.hex()}, separators=(",", ":")), flush=True)
        interval_start, sequence, units = now, sequence + 1, 0

    now = time.monotonic_ns()
    while now < end:
        for _ in range(256):
            digest = hashlib.sha256(digest).digest()
        units += 256
        now = time.monotonic_ns()
        if now - interval_start >= 1_000_000_000:
            emit(now)
    if now > interval_start:
        emit(now)


if __name__ == "__main__":
    main()
