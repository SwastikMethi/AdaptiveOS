"""Single-threaded trusted workloads. Only launched by the Linux supervisor."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import sys
import time

from .protocol import encode, identifier, validate_config
from .resources import Cache, MeasurementWindow


def run_cache(config):
    cache = Cache(config['mib'], seed=config['seed'], working_set_mib=config['working_set_mib'], refill_ms=config['refill_ms'])
    start = last = time.monotonic_ns()
    window = MeasurementWindow(start)
    latest = None
    sequence = units = checksum = 0
    control_buffer = b''
    os.set_blocking(sys.stdin.fileno(), False)
    print(encode({'kind': 'allocation_ack', 'request_id': 'initial', 'allocated_mib': cache.allocated_mib, 'ok': True}).rstrip(), flush=True)
    try:
        while True:
            if select.select([sys.stdin], [], [], 0)[0]:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    return
                control_buffer += data
                if len(control_buffer) > 4096:
                    raise ValueError('Cache command bound exceeded')
                while b'\n' in control_buffer:
                    line, control_buffer = control_buffer.split(b'\n', 1)
                    command = json.loads(line)
                    ok = True
                    try:
                        cache.resize(command['mib'])
                    except (OSError, MemoryError):
                        ok = False
                    print(encode({'kind': 'allocation_ack', 'request_id': command['request_id'],
                                  'allocated_mib': cache.allocated_mib, 'ok': ok}).rstrip(), flush=True)
            now = time.monotonic_ns()
            while now >= window.start + 5_000_000_000:
                latest = window.finish()
                window = MeasurementWindow(window.start + 5_000_000_000)
            before = cache.misses
            checksum ^= cache.request()
            completed = time.monotonic_ns()
            units += 1
            window.add((completed - now) / 1e6, cache.misses != before)
            if completed - last >= 1_000_000_000:
                print(encode({'sequence': sequence, 'start_ns': start, 'mono_ns': completed,
                    'units': units, 'ticks': [], 'cache_hits': cache.hits, 'cache_misses': cache.misses,
                    'configured_mib': config['mib'], 'allocated_mib': cache.allocated_mib,
                    'resource_window': latest, 'checksum': checksum}).rstrip(), flush=True)
                sequence += 1
                last = completed
    finally:
        cache.close()


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


def run_network(config, workload_id, port):
    from .network import connect_flow, request_record, receive_record
    identity = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
    stream = connect_flow(port, workload_id, identity)
    start = last = scheduled = time.monotonic_ns()
    window = MeasurementWindow(start)
    latest = None
    sequence = units = records = failures = 0
    delay = 0.
    foreground = config['foreground']
    size = config['response_kib'] * 1024 if foreground else 256 * 1024
    period = config['period_ms'] * 1_000_000
    try:
        while True:
            if foreground:
                time.sleep(max(0, (scheduled - time.monotonic_ns()) / 1e9))
            now = time.monotonic_ns()
            while now >= window.start + 5_000_000_000:
                latest = window.finish()
                window = MeasurementWindow(window.start + 5_000_000_000)
            failed = False
            try:
                request_record(stream, records, size)
                delay = receive_record(stream, records, size)
                units += size
                records += 1
            except (OSError, ValueError):
                failures += 1
                failed = True
            completed = time.monotonic_ns()
            latency = (completed - (scheduled if foreground else now)) / 1e6
            window.add(latency, failed or (foreground and latency > config['deadline_ms']), failed)
            if foreground:
                scheduled += period
            if completed - last >= 1_000_000_000 or failed:
                print(encode({'sequence': sequence, 'start_ns': start, 'mono_ns': completed,
                    'units': units, 'ticks': [], 'resource_window': latest, 'completed_records': records,
                    'failed_transfers': failures, 'queue_delay_ms': delay,
                    'delivered_mbps': units * 8 / max(1, completed - start) * 1000}).rstrip(), flush=True)
                sequence += 1
                last = completed
            if failed:
                raise OSError('Owned network transfer failed')
    finally:
        stream.close()


def run(preset, config, workload_id, cpu, relay_port=0):
    os.sched_setaffinity(0, {cpu})
    if preset == 'cache':
        return run_cache(config)
    if preset == 'network':
        return run_network(config, workload_id, relay_port)
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
    parser.add_argument('relay_port', type=int, nargs='?', default=0)
    args = parser.parse_args()
    run(args.preset, validate_config(args.preset, json.loads(args.config)),
        identifier(args.workload_id), args.cpu, args.relay_port)


if __name__ == '__main__':
    main()
