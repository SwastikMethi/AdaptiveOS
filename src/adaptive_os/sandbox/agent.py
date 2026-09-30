"""Linux supervisor: one sandbox, scoped child processes and monotonic telemetry."""

from collections import deque
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from .protocol import MAX_WORKLOADS, VERSION, encode, validate_command


def read_process(pid, root=Path('/proc'), ticks_per_second=None):
    try:
        # comm may contain spaces and ')'; numeric fields follow its last ')'.
        fields = (root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        values = {'identity': int(fields[19]), 'state': fields[0], 'nice': int(fields[16]),
                  'cpu_ns': round((int(fields[11]) + int(fields[12])) * 1e9 /
                                  (ticks_per_second or os.sysconf('SC_CLK_TCK'))),
                  'mono_ns': time.monotonic_ns(), 'rss_mib': None,
                  'read_bytes': None, 'write_bytes': None}
        try:
            for line in (root / str(pid) / 'status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    values['rss_mib'] = int(line.split()[1]) / 1024
        except (OSError, ValueError):
            pass
        try:
            counters = dict(line.split(':', 1) for line in (root / str(pid) / 'io').read_text().splitlines())
            for key in ('read_bytes', 'write_bytes'):
                values[key] = int(counters[key]) if key in counters else None
        except (OSError, ValueError):
            pass
        return values
    except (OSError, ValueError, IndexError):
        return None


def cpu_percent(previous, current):
    if not previous or not current or previous['identity'] != current['identity']:
        return None
    elapsed = current['mono_ns'] - previous['mono_ns']
    used = current['cpu_ns'] - previous['cpu_ns']
    return 100 * used / elapsed if elapsed > 0 and used >= 0 else None


def latency_window(ticks, start_ns, now_ns):
    result = dict(miss_rate=None, p95_lateness_ms=None, p95_wakeup_ms=None)
    if now_ns - start_ns < 10_000_000_000:
        return result
    ticks = [t for t in ticks if now_ns - 5_000_000_000 <= t['scheduled_ns'] < now_ns
             and t['scheduled_ns'] >= start_ns + 5_000_000_000]
    if ticks:
        lateness = [max(0, t['completion_ns'] - t['deadline_ns']) / 1e6 for t in ticks]
        wakeup = [max(0, t['wakeup_ns'] - t['scheduled_ns']) / 1e6 for t in ticks]
        rank = math.ceil(.95 * len(ticks)) - 1
        result.update(miss_rate=sum(t['skipped'] or t['completion_ns'] > t['deadline_ns'] for t in ticks) / len(ticks),
                      p95_lateness_ms=sorted(lateness)[rank], p95_wakeup_ms=sorted(wakeup)[rank])
    return result


@dataclass
class Child:
    process: subprocess.Popen
    preset: str
    config: dict
    identity: int
    start_ns: int
    previous: dict | None = None
    progress: dict | None = None
    prior_progress: dict | None = None
    ticks: deque = field(default_factory=lambda: deque(maxlen=1000))


class Supervisor:
    def __init__(self):
        self.selector = selectors.DefaultSelector()
        self.buffers = {}
        self.children = {}
        self.sequence = 0
        self.last_heartbeat = time.monotonic()
        self.stopping = False
        self.allowed_cpus = sorted(os.sched_getaffinity(0))
        self.cpu = self.allowed_cpus[0]
        self.collector_cpu = self.allowed_cpus[min(1, len(self.allowed_cpus) - 1)]
        self.priority_supported, self.priority_reason = self.probe_priority()
        os.sched_setaffinity(0, {self.collector_cpu})

    def emit(self, kind, **fields):
        print(encode({'v': VERSION, 'sequence': self.sequence, 'mono_ns': time.monotonic_ns(),
                      'kind': kind, **fields}).rstrip(), flush=True)
        self.sequence += 1

    def probe_priority(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, process_group=0)
        try:
            for nice in (0, 10, 19, 0):
                os.setpriority(os.PRIO_PROCESS, child.pid, nice)
                if os.getpriority(os.PRIO_PROCESS, child.pid) != nice:
                    raise OSError('Priority readback did not match')
            return True, 'Verified 0 → 10 → 19 → 0 on an owned child'
        except OSError as exc:
            return False, f'Monitoring only: reversible priority unavailable ({exc})'
        finally:
            child.terminate()
            child.wait(timeout=2)

    def register(self, stream, workload_id, diagnostic=False):
        os.set_blocking(stream.fileno(), False)
        self.selector.register(stream, selectors.EVENT_READ, (workload_id, diagnostic))
        self.buffers[stream] = b''

    def owned_child(self, workload_id):
        child = self.children.get(workload_id)
        current = read_process(child.process.pid) if child and child.process.poll() is None else None
        if not current or current['identity'] != child.identity:
            raise ValueError('Workload is no longer running (process identity changed)')
        return child

    def stop_child(self, workload_id):
        child = self.children[workload_id]
        if child.process.poll() is None:
            self.owned_child(workload_id)
            os.killpg(child.process.pid, signal.SIGTERM)
            try:
                child.process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                self.owned_child(workload_id)
                os.killpg(child.process.pid, signal.SIGKILL)
                child.process.wait(timeout=1)
        for stream in (child.process.stdout, child.process.stderr):
            if stream in self.buffers:
                self.selector.unregister(stream)
                self.buffers.pop(stream)
            stream.close()
        if child.preset == 'io':
            (Path('/scratch') / (workload_id + '.bin')).unlink(missing_ok=True)
        del self.children[workload_id]
        return child.process.returncode

    def handle(self, raw):
        request_id = raw.get('request_id') if isinstance(raw, dict) else None
        try:
            command = validate_command(raw)
            op = command['op']
            result = {}
            if op == 'heartbeat':
                self.last_heartbeat = time.monotonic()
                return
            if op == 'shutdown':
                self.stopping = True
            elif op == 'start':
                workload_id = command['workload_id']
                if workload_id in self.children:
                    raise ValueError('Workload is already running')
                if len(self.children) >= MAX_WORKLOADS:
                    raise ValueError('At most 8 workloads can run')
                memory = sum(c.config.get('mib', 0) for c in self.children.values())
                if memory + command['config'].get('mib', 0) > 512:
                    raise ValueError('Combined memory buffers cannot exceed 512 MiB')
                process = subprocess.Popen([sys.executable, '-m', 'adaptive_os.sandbox.presets',
                    command['preset'], json.dumps(command['config']), workload_id, str(self.cpu)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, process_group=0)
                try:
                    current = read_process(process.pid)
                    if not current:
                        raise ValueError('Workload exited before its identity could be read')
                    self.children[workload_id] = Child(process, command['preset'], command['config'],
                                                       current['identity'], time.monotonic_ns())
                    self.register(process.stdout, workload_id)
                    self.register(process.stderr, workload_id, True)
                except Exception:
                    process.kill()
                    process.wait()
                    raise
                result = {'workload_id': workload_id, 'pid': process.pid, 'identity': current['identity'],
                          'nice': current['nice'], 'cpu': self.cpu, 'status': 'running',
                          'preset': command['preset'], 'config': command['config']}
            elif op == 'stop':
                workload_id = command['workload_id']
                if workload_id not in self.children:
                    raise ValueError('Workload is not running')
                code = self.stop_child(workload_id)
                result = {'workload_id': workload_id, 'status': 'stopped', 'exit_code': code}
            elif op == 'priority':
                if not self.priority_supported:
                    raise ValueError(self.priority_reason)
                child = self.owned_child(command['workload_id'])
                os.setpriority(os.PRIO_PROCESS, child.process.pid, command['nice'])
                actual = os.getpriority(os.PRIO_PROCESS, child.process.pid)
                if actual != command['nice']:
                    raise OSError('Priority readback did not match')
                result = {'workload_id': command['workload_id'], 'nice': actual}
            self.emit('ack', request_id=request_id, op=op, ok=True, **result)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.emit('ack', request_id=request_id, op=raw.get('op') if isinstance(raw, dict) else None,
                      ok=False, error=str(exc))

    def read_stream(self, key):
        stream = key.fileobj
        workload_id, diagnostic = key.data
        data = os.read(stream.fileno(), 65536)
        if not data:
            self.selector.unregister(stream)
            self.buffers.pop(stream, None)
            if workload_id is None:
                self.stopping = True
            return
        buffer = self.buffers[stream] + data
        if len(buffer) > 262144:
            raise ValueError('Protocol input exceeded buffer bound')
        lines = buffer.split(b'\n')
        self.buffers[stream] = lines.pop()
        for line in lines:
            if diagnostic:
                self.emit('event', workload_id=workload_id, message=line.decode(errors='replace')[:2000])
                continue
            try:
                value = json.loads(line)
                if workload_id is None:
                    self.handle(value)
                    continue
                child = self.children.get(workload_id)
                if not child:
                    continue
                if (not isinstance(value, dict) or type(value.get('sequence')) is not int or
                    value['sequence'] != (child.progress['sequence'] + 1 if child.progress else 0) or
                    type(value.get('units')) is not int or value['units'] < 0 or
                    type(value.get('mono_ns')) is not int or type(value.get('start_ns')) is not int or
                    not isinstance(value.get('ticks'), list) or len(value['ticks']) > 200):
                    raise ValueError('Invalid workload progress')
                for tick in value['ticks']:
                    times = [tick[name] for name in ('scheduled_ns', 'wakeup_ns', 'completion_ns', 'deadline_ns')]
                    if (any(type(t) is not int for t in times) or not times[0] <= times[1] <= times[2]
                            or times[3] != times[0] + 20_000_000 or type(tick.get('skipped')) is not bool):
                        raise ValueError('Invalid periodic tick')
                child.prior_progress = child.progress
                child.progress = value
                child.ticks.extend(value['ticks'])
                self.emit('raw', workload_id=workload_id, data=value)
            except (ValueError, KeyError, TypeError) as exc:
                self.emit('event', workload_id=workload_id, message=f'Invalid telemetry/command: {exc}')
                if workload_id in self.children:
                    self.stop_child(workload_id)
                    self.emit('exit', workload_id=workload_id, status='failed', error=str(exc))

    def sample(self):
        for workload_id, child in list(self.children.items()):
            if child.process.poll() is not None:
                code = self.stop_child(workload_id)
                self.emit('exit', workload_id=workload_id, exit_code=code, status='failed')
                continue
            current = read_process(child.process.pid)
            if not current or current['identity'] != child.identity:
                self.emit('event', workload_id=workload_id, message='Process telemetry unavailable')
                continue
            now = current['mono_ns']
            rate = None
            progress, prior = child.progress, child.prior_progress
            fresh = bool(progress and 0 <= now - progress['mono_ns'] < 3_000_000_000)
            if fresh and prior:
                elapsed = progress['mono_ns'] - prior['mono_ns']
                count = progress['units'] - prior['units']
                if elapsed > 0 and count >= 0:
                    rate = count * 1e9 / elapsed
                    if child.preset in ('memory', 'io'):
                        rate /= 1024 * 1024
            metrics = latency_window(child.ticks, child.start_ns, now) if fresh else latency_window([], 0, 0)
            self.emit('sample', workload_id=workload_id, cpu_pct=cpu_percent(child.previous, current),
                      rss_mib=current['rss_mib'], read_bytes=current['read_bytes'], write_bytes=current['write_bytes'],
                      nice=current['nice'], state=current['state'], app_rate=rate,
                      app_unit='MiB/s' if child.preset in ('memory', 'io') else 'hashes/s',
                      warmup=now - child.start_ns < 5_000_000_000, progress_fresh=fresh, **metrics)
            child.previous = current

    def run(self):
        self.register(sys.stdin.buffer, None)
        self.emit('ready', allowed_cpus=self.allowed_cpus, workload_cpu=self.cpu,
                  collector_cpu=self.collector_cpu, shared_collector=self.cpu == self.collector_cpu,
                  priority_supported=self.priority_supported, priority_reason=self.priority_reason,
                  platform=sys.platform, uid=os.getuid(), lease_seconds=10)
        next_sample = time.monotonic() + 1
        try:
            while not self.stopping:
                for key, _ in self.selector.select(.1):
                    self.read_stream(key)
                now = time.monotonic()
                if now - self.last_heartbeat > 10:
                    self.emit('event', message='Host heartbeat lease expired; stopping workloads')
                    break
                if now >= next_sample:
                    self.sample()
                    next_sample = now + 1
        finally:
            for workload_id in list(self.children):
                try:
                    self.stop_child(workload_id)
                except (OSError, ValueError) as exc:
                    print(str(exc), file=sys.stderr)
            self.emit('closed')
            self.selector.close()


def main():
    supervisor = Supervisor()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: setattr(supervisor, 'stopping', True))
    supervisor.run()


if __name__ == '__main__':
    main()
