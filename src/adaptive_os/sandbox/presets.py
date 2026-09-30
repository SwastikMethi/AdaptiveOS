"""Single-threaded trusted workloads. Only launched by the Linux supervisor."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from .protocol import encode, identifier, validate_config


def io_step(file, block, state):
    writing, offset = state
    if writing:
        amount = file.write(block)
        if amount != len(block):
            raise OSError('Incomplete scratch write')
    else:
        amount = len(file.read(len(block)))
        if amount != len(block):
            raise OSError('Incomplete scratch read')
    offset += 1
    if offset == 64:
        if writing:
            os.fsync(file.fileno())
        file.seek(0)
        writing, offset = not writing, 0
    return amount, (writing, offset)


def run(preset, config, workload_id, cpu):
    os.sched_setaffinity(0, {cpu})
    start = last = time.monotonic_ns()
    scheduled = start
    units = sequence = 0
    ticks = []
    digest = bytes(32)
    buffer = bytearray(config['mib'] * 1024 * 1024) if preset == 'memory' else None
    block = bytes(1024 * 1024) if preset == 'io' else None
    scratch = Path('/scratch') / (workload_id + '.bin')
    file = scratch.open('w+b', buffering=0) if preset == 'io' else None
    io_state = (True, 0)
    try:
        while True:
            if preset == 'periodic':
                remaining = (scheduled - time.monotonic_ns()) / 1e9
                if remaining > 0:
                    time.sleep(remaining)
                wake = time.monotonic_ns()
                deadline = scheduled + 20_000_000
                skipped = wake >= deadline
                if not skipped:
                    cpu_end = time.thread_time_ns() + round(config['budget_ms'] * 1e6)
                    while time.thread_time_ns() < cpu_end:
                        digest = hashlib.sha256(digest).digest()
                        units += 1
                ticks.append({'scheduled_ns': scheduled, 'wakeup_ns': wake,
                              'completion_ns': time.monotonic_ns(), 'deadline_ns': deadline,
                              'skipped': skipped})
                scheduled += 20_000_000
            elif preset == 'cpu':
                for _ in range(1000):
                    digest = hashlib.sha256(digest).digest()
                units += 1000
            elif preset == 'memory':
                # Touch every page; report bytes actually touched, not allocated bytes.
                for offset in range(0, len(buffer), 4096):
                    buffer[offset] = (buffer[offset] + 1) & 255
                units += len(buffer) // 4096
            else:
                amount, io_state = io_step(file, block, io_state)
                units += amount
            now = time.monotonic_ns()
            if now - last >= 1_000_000_000 or len(ticks) >= 200:
                print(encode({'sequence': sequence, 'start_ns': start, 'mono_ns': now,
                              'units': units, 'ticks': ticks, 'checksum': digest.hex()}).rstrip(), flush=True)
                sequence += 1
                ticks = []
                last = now
    finally:
        if file:
            file.close()
            scratch.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('preset')
    parser.add_argument('config')
    parser.add_argument('workload_id')
    parser.add_argument('cpu', type=int)
    args = parser.parse_args()
    run(args.preset, validate_config(args.preset, json.loads(args.config)),
        identifier(args.workload_id), args.cpu)


if __name__ == '__main__':
    main()
