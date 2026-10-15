"""Owned, physically allocated cache and acknowledgement-driven transfers."""
from collections import OrderedDict, deque
import mmap
import random
import time

from .protocol import bounded_int, validate_command

MIB = 1048576
CHUNK = 16 * MIB
ENTRY = 64 * 1024


class Cache:
    def __init__(self, mib, *, seed, working_set_mib, refill_ms):
        self.chunks = []
        self.entries = OrderedDict()
        self.free = deque()
        self.random = random.Random(seed)
        self.keys = working_set_mib * MIB // ENTRY
        self.refill_seconds = refill_ms / 1000
        self.hits = self.misses = 0
        self.resize(mib)

    @property
    def allocated_mib(self):
        return len(self.chunks) * 16

    def resize(self, mib):
        bounded_int(mib, 16, 512, 16)
        count = mib // 16
        old_slots = len(self.chunks) * CHUNK // ENTRY
        slots = mib * MIB // ENTRY
        added = []
        try:
            for _ in range(max(0, count - len(self.chunks))):
                chunk = mmap.mmap(-1, CHUNK)
                added.append(chunk)
                # Commit every page; configured capacity is never reported as RSS.
                zero = bytes(MIB)
                for offset in range(0, CHUNK, MIB):
                    chunk[offset:offset + MIB] = zero
        except (OSError, MemoryError):
            for chunk in added:
                chunk.close()
            raise
        self.chunks.extend(added)
        if slots < old_slots:
            self.entries = OrderedDict((key, slot) for key, slot in self.entries.items() if slot < slots)
            self.free = deque(slot for slot in self.free if slot < slots)
            for chunk in self.chunks[count:]:
                chunk.close()
            del self.chunks[count:]
        else:
            self.free.extend(range(old_slots, slots))

    def request(self, key=None):
        key = self.random.randrange(self.keys) if key is None else bounded_int(key, 0, self.keys - 1)
        if key in self.entries:
            self.hits += 1
            slot = self.entries.pop(key)
        else:
            self.misses += 1
            time.sleep(self.refill_seconds)
            slot = self.free.popleft() if self.free else self.entries.popitem(last=False)[1]
            chunk, offset = divmod(slot * ENTRY, CHUNK)
            self.chunks[chunk][offset:offset + ENTRY] = key.to_bytes(8, 'little') + bytes(ENTRY - 8)
        self.entries[key] = slot
        chunk, offset = divmod(slot * ENTRY, CHUNK)
        return int.from_bytes(self.chunks[chunk][offset:offset + 8], 'little')

    def close(self):
        for chunk in self.chunks:
            chunk.close()
        self.chunks.clear()
        self.entries.clear()
        self.free.clear()


class MemoryTransaction:
    """No recipient allocation until the donor's release is acknowledged."""
    def __init__(self, command):
        validate_command(command)
        self.command = command
        self.pending = sorted(command['changes'], key=lambda c: c['requested'] - c['previous'])
        self.applied = []
        self.failed = self.done = self.rolled_back = False

    @property
    def ok(self):
        return self.done and not self.failed

    def next_change(self):
        return self.pending[0] if self.pending else None

    def acknowledge(self, workload_id, identity, allocated, ok):
        expected = self.next_change()
        if not expected or (workload_id, identity) != (expected['workload_id'], expected['identity']):
            raise ValueError('Allocation acknowledgement identity/order mismatch')
        if type(allocated) is not int or allocated != expected['requested' if ok else 'previous']:
            raise ValueError('Allocation readback mismatch; cleanup required')
        self.pending.pop(0)
        if not ok:
            if self.failed:
                raise ValueError('Rollback failed; cleanup required')
            self.failed = True
            self.pending = [{**c, 'previous': c['requested'], 'requested': c['previous']}
                            for c in reversed(self.applied)]
        elif not self.failed:
            self.applied.append(expected)
        if not self.pending:
            self.done = True
            self.rolled_back = self.failed


class MeasurementWindow:
    """Fixed five-second intervals with bounded latency histograms."""
    bounds_ms = (.05, .1, .2, .5, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1000, float('inf'))

    def __init__(self, start_ns):
        self.start = start_ns
        self.count = self.misses = self.failures = 0
        self.histogram = [0] * len(self.bounds_ms)
        self.max_latency = 0

    def add(self, latency_ms, missed=False, failed=False):
        self.count += 1
        self.misses += int(missed)
        self.failures += int(failed)
        self.max_latency = max(self.max_latency, latency_ms)
        for index, bound in enumerate(self.bounds_ms):
            if latency_ms <= bound:
                self.histogram[index] += 1
                break

    def finish(self):
        accumulated = 0
        p95 = None
        for bound, count in zip(self.bounds_ms, self.histogram):
            accumulated += count
            if self.count and accumulated >= .95 * self.count:
                p95 = min(bound, self.max_latency)
                break
        return {'window_start_ns': self.start, 'window_end_ns': self.start + 5_000_000_000,
                'miss_rate': self.misses / self.count if self.count else None,
                'requests': self.count, 'misses': self.misses, 'failed_transfers': self.failures,
                'p95_lateness_ms': p95, 'p95_latency_ms': p95, 'latency_histogram': self.histogram}
