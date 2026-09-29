"""Synthetic periodic CPU work. Overdue periods are emitted as missed ticks."""

import argparse
import hashlib
import json
import math
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-sec", type=float, default=60)
    parser.add_argument("--period-ms", type=float, default=20)
    parser.add_argument("--work-budget-ms", type=float, default=4)
    parser.add_argument("--start-ns", type=int)
    args = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in (args.duration_sec, args.period_ms, args.work_budget_ms)):
        parser.error("duration, period and budget must be finite and positive")
    if args.work_budget_ms >= args.period_ms or (args.start_ns is not None and args.start_ns < 0):
        parser.error("budget must be below period; start time must be nonnegative")
    period = round(args.period_ms * 1_000_000)
    budget = round(args.work_budget_ms * 1_000_000)
    duration = round(args.duration_sec * 1_000_000_000)
    if min(period, budget) < 1 or duration < period:
        parser.error("duration must cover one period; budget and period must be at least 1 ns")
    start = args.start_ns if args.start_ns is not None else time.monotonic_ns()
    delay = start - time.monotonic_ns()
    if delay > 0:
        time.sleep(delay / 1_000_000_000)
    cpu_start = time.thread_time_ns()
    digest = bytes(32)
    summary_start, summary_sequence, ticks, misses, units = start, 0, 0, 0, 0

    def emit(kind, now, **values):
        print(json.dumps({"schema_version": 1, "type": kind, "start_ns": start,
                          "monotonic_ns": now, "process_cpu_ns": time.thread_time_ns() - cpu_start,
                          "checksum": digest.hex(), **values}, separators=(",", ":")), flush=True)

    def summary(now):
        nonlocal summary_start, summary_sequence, ticks, misses, units
        emit("foreground_summary", now, sequence=summary_sequence, interval_start_ns=summary_start,
             interval_ns=now - summary_start, ticks=ticks, misses=misses, work_units=units)
        summary_start, summary_sequence = now, summary_sequence + 1
        ticks, misses, units = 0, 0, 0

    for sequence in range(duration // period):
        scheduled = start + sequence * period
        delay = scheduled - time.monotonic_ns()
        if delay > 0:
            time.sleep(delay / 1_000_000_000)
        wakeup = time.monotonic_ns()
        deadline = scheduled + period
        work_units = 0
        skipped = wakeup >= deadline
        if not skipped:
            cpu_before = time.thread_time_ns()
            while time.thread_time_ns() - cpu_before < budget:
                digest = hashlib.sha256(digest).digest()
                work_units += 1
        completion = time.monotonic_ns()
        met = completion <= deadline
        emit("foreground_tick", completion, sequence=sequence, scheduled_ns=scheduled,
             deadline_ns=deadline, wakeup_ns=wakeup, completion_ns=completion,
             deadline_met=met, skipped=skipped, work_units=work_units)
        ticks, misses, units = ticks + 1, misses + int(not met), units + work_units
        if completion - summary_start >= 1_000_000_000:
            summary(completion)
    delay = start + duration - time.monotonic_ns()
    if delay > 0:
        time.sleep(delay / 1_000_000_000)
    summary(time.monotonic_ns())


if __name__ == "__main__":
    main()
