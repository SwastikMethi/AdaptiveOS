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
import resource

from .protocol import MAX_WORKLOADS, VERSION, encode, validate_command


def flush_output(buffer, fd=1):
    if buffer:
        try:
            written = os.write(fd, buffer)
            del buffer[:written]
        except BlockingIOError:
            pass


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
    result = dict(miss_rate=None, p95_lateness_ms=None, p95_wakeup_ms=None,
                  window_start_ns=None, window_end_ns=None)
    if now_ns - start_ns < 10_000_000_000:
        return result
    # Last fully delivered fixed 5-second window, not a partial rolling window
    # ending at collection time (child reports can trail by a second).
    latest_deadline = ticks[-1]['deadline_ns'] if ticks else None
    available_end = (now_ns if latest_deadline is None or now_ns - latest_deadline > 2_000_000_000
                     else min(now_ns, latest_deadline))
    window_end = start_ns + ((available_end - start_ns) // 5_000_000_000) * 5_000_000_000
    if window_end < start_ns + 10_000_000_000 or now_ns - window_end > 8_000_000_000:
        return result
    result.update(window_start_ns=window_end - 5_000_000_000, window_end_ns=window_end)
    if not ticks:
        return result
    ticks = [t for t in ticks if window_end - 5_000_000_000 <= t['scheduled_ns'] < window_end]
    expected = list(range(window_end - 5_000_000_000, window_end, 20_000_000))
    if [t['scheduled_ns'] for t in ticks] == expected:
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
    allocated: int | None = None
    start_command: dict | None = None


class Supervisor:
    def __init__(self):
        self.selector = selectors.DefaultSelector()
        self.buffers = {}
        self.children = {}
        self.sequence = 0
        self.output = bytearray()
        os.set_blocking(sys.stdout.fileno(), False)
        self.last_heartbeat = time.monotonic()
        self.stopping = False
        self.allowed_cpus = sorted(os.sched_getaffinity(0))
        self.cpu = self.allowed_cpus[0]
        self.collector_cpu = self.allowed_cpus[min(1, len(self.allowed_cpus) - 1)]
        self.priority_supported = False
        self.priority_reason = 'Reversible priority preflight pending'
        self.preflight_complete = False
        self.resource = 'cpu'
        self.transaction = None
        self.allocation_deadline = None
        self.seen_allocations = {}
        self.relay = None
        os.sched_setaffinity(0, {self.collector_cpu})

    def emit(self, kind, **fields):
        self.output.extend(encode({'v': VERSION, 'sequence': self.sequence, 'mono_ns': time.monotonic_ns(),
                                   'kind': kind, **fields}).encode())
        if len(self.output) > 1024 * 1024:
            raise RuntimeError('Output backpressure exceeded 1 MiB; stopping the sandbox')
        flush_output(self.output)
        self.sequence += 1

    def _transition(self, process, identity, nice):
        before = read_process(process.pid)
        if not before or before['identity'] != identity:
            raise ValueError('Process identity changed before priority update')
        os.setpriority(os.PRIO_PROCESS, process.pid, nice)
        actual = os.getpriority(os.PRIO_PROCESS, process.pid)
        after = read_process(process.pid)
        if actual != nice or not after or after['identity'] != identity or after['nice'] != nice:
            raise OSError('Priority acknowledgement/readback did not match')
        return {'from': before['nice'], 'to': nice, 'actual': actual, 'identity': identity}

    def set_priority(self, workload_id, identity, nice):
        child = self.owned_child(workload_id)
        if child.identity != identity:
            raise ValueError('Workload process identity is stale')
        return self._transition(child.process, identity, nice)

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
            if stream is None:
                continue
            if stream in self.buffers:
                self.selector.unregister(stream)
                self.buffers.pop(stream)
            stream.close()
        if getattr(child.process, 'stdin', None):
            child.process.stdin.close()
        if getattr(self, 'relay', None):
            self.relay.remove_flow(workload_id)
        if child.preset == 'io':
            (Path('/scratch') / (workload_id + '.bin')).unlink(missing_ok=True)
        del self.children[workload_id]
        return child.process.returncode

    def allocation_step(self):
        change = self.transaction.next_change()
        child = self.owned_child(change['workload_id'])
        if child.identity != change['identity']:
            raise ValueError('Allocation process identity changed')
        payload = encode({'request_id': self.transaction.command['request_id'], 'mib': change['requested']}).encode()
        if os.write(child.process.stdin.fileno(), payload) != len(payload):
            raise OSError('Incomplete cache command write')
        self.allocation_deadline = time.monotonic() + 3

    def allocation_ack(self, workload_id, value):
        child = self.owned_child(workload_id)
        if type(value.get('ok')) is not bool or type(value.get('allocated_mib')) is not int:
            raise ValueError('Invalid allocation acknowledgement')
        if value.get('request_id') == 'initial' and child.start_command:
            if not value['ok'] or value['allocated_mib'] != child.config['mib']:
                raise ValueError('Initial cache allocation failed')
            child.allocated = value['allocated_mib']
            request_id = child.start_command['request_id']
            child.start_command = None
            self.emit('ack', request_id=request_id, op='start', ok=True, workload_id=workload_id,
                      identity=child.identity, pid=child.process.pid, nice=0, status='running',
                      allocated=child.allocated, configured_mib=child.config['mib'])
            return
        if not self.transaction or value.get('request_id') != self.transaction.command['request_id']:
            raise ValueError('Stale allocation acknowledgement')
        self.transaction.acknowledge(workload_id, child.identity, value['allocated_mib'], value['ok'])
        child.allocated = value['allocated_mib']
        if self.transaction.done:
            tx, self.transaction = self.transaction, None
            self.allocation_deadline = None
            result = dict(request_id=tx.command['request_id'], op='memory_budget', ok=tx.ok,
                          changes=tx.command['changes'], foreground_id=tx.command['foreground_id'],
                          rolled_back=tx.rolled_back,
                          confirmed={c['workload_id']: self.children[c['workload_id']].allocated for c in tx.command['changes']})
            if not tx.ok:
                result['error'] = 'Allocation failed; transaction rolled back; automatic control must stop'
            self.seen_allocations[tx.command['request_id']] = (tx.command, result)
            self.emit('ack', **result)
        else:
            self.allocation_step()

    def transfer_allocation(self, command):
        previous = self.seen_allocations.get(command['request_id'])
        if previous:
            if previous[0] != command:
                raise ValueError('Request ID reused with a different allocation')
            if previous[1]:
                self.emit('ack', **previous[1])
            return
        if self.transaction or any(c.start_command for c in self.children.values()):
            raise ValueError('Awaiting allocation acknowledgement')
        if len(self.seen_allocations) >= 4096:
            raise ValueError('Session allocation command limit reached; restart sandbox')
        expected = 'cache' if command['op'] == 'memory_budget' else 'network'
        if self.resource != ('memory' if expected == 'cache' else 'network'):
            raise ValueError('Resource mode mismatch')
        if expected == 'network' and not self.owned_child(command['foreground_id']).config['foreground']:
            raise ValueError('Transfer does not bind the configured foreground flow')
        for change in command['changes']:
            child = self.owned_child(change['workload_id'])
            if child.identity != change['identity'] or child.preset != expected or child.allocated != change['previous']:
                raise ValueError('Stale allocation or process identity')
        if command['op'] == 'memory_budget':
            from .resources import MemoryTransaction
            self.seen_allocations[command['request_id']] = (command, None)
            self.transaction = MemoryTransaction(command)
            self.allocation_step()
        else:
            self.relay.update_shares(command['changes'])
            for change in command['changes']:
                self.children[change['workload_id']].allocated = change['requested']
            result = dict(request_id=command['request_id'], op='network_share', ok=True,
                          foreground_id=command['foreground_id'], changes=command['changes'],
                          confirmed={c['workload_id']: c['requested'] for c in command['changes']})
            self.seen_allocations[command['request_id']] = (command, result)
            self.emit('ack', **result)

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
            elif op == 'configure':
                if self.children or self.preflight_complete:
                    raise ValueError('Resource selector is locked during a run')
                self.resource = command['resource']
                self.preflight_complete = self.resource != 'cpu'
                self.link_mbps = command['link_mbps']
                if self.resource == 'network':
                    from .network import Relay
                    self.relay = Relay(self.link_mbps)
                result = {'resource': self.resource, 'resource_supported': True}
            elif op == 'probe_start':
                if self.preflight_complete or self.children:
                    raise ValueError('Disposable priority probe is not isolated')
                process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, process_group=0)
                os.sched_setaffinity(process.pid, {self.cpu})
                if os.sched_getaffinity(process.pid) != {self.cpu}:
                    process.terminate()
                    process.wait(timeout=2)
                    raise OSError('Preflight probe CPU affinity readback did not match')
                current = read_process(process.pid)
                if not current:
                    process.terminate()
                    process.wait(timeout=2)
                    raise ValueError('Preflight probe exited before identity snapshot')
                child = Child(process, 'probe', {}, current['identity'], time.monotonic_ns())
                self.children[command['workload_id']] = child
                if command['nice'] != current['nice']:
                    self._transition(process, current['identity'], command['nice'])
                    current = read_process(process.pid)
                result = {'workload_id': command['workload_id'], 'pid': process.pid,
                          'identity': current['identity'], 'nice': current['nice'], 'status': 'running'}
            elif op == 'probe_stop':
                child = self.owned_child(command['workload_id'])
                if child.preset != 'probe' or child.identity != command['identity']:
                    raise ValueError('Disposable probe identity is stale')
                code = self.stop_child(command['workload_id'])
                result = {'workload_id': command['workload_id'], 'identity': command['identity'],
                          'status': 'stopped', 'exit_code': code}
            elif op == 'preflight_complete':
                if self.children:
                    raise ValueError('Priority probes were not removed')
                self.preflight_complete = self.priority_supported = True
                self.priority_reason = 'Reversible priority preflight passed through acknowledged control path'
                result = {'priority_supported': True, 'priority_reason': self.priority_reason}
            elif op == 'preflight_abort':
                if any(child.preset != 'probe' for child in self.children.values()):
                    raise ValueError('Cannot abort preflight with user workloads present')
                for workload_id in list(self.children):
                    self.stop_child(workload_id)
                self.preflight_complete = True
                self.priority_supported = False
                self.priority_reason = 'Monitoring only: reversible priority preflight failed'
                result = {'priority_supported': False, 'priority_reason': self.priority_reason}
            elif op == 'start':
                if self.transaction:
                    raise ValueError('Cannot start a workload during an allocation transaction')
                if not self.preflight_complete:
                    raise ValueError('Priority preflight has not completed')
                workload_id = command['workload_id']
                if self.resource != 'cpu' and command['preset'] != {'memory': 'cache', 'network': 'network'}[self.resource]:
                    raise ValueError('Workload does not match locked resource mode')
                if self.resource == 'cpu' and command['preset'] in ('cache', 'network'):
                    raise ValueError('Select Memory or Network mode before starting this workload')
                if workload_id in self.children:
                    raise ValueError('Workload is already running')
                if len(self.children) >= MAX_WORKLOADS:
                    raise ValueError('At most 8 workloads can run')
                memory = sum((c.allocated if c.allocated is not None else c.config.get('mib', 0))
                             for c in self.children.values() if c.preset in ('memory', 'cache'))
                if memory + command['config'].get('mib', 0) > 512:
                    raise ValueError('Combined memory buffers cannot exceed 512 MiB')
                if command['preset'] == 'network' and sum(c.allocated or c.config.get('share', 0) for c in self.children.values()) + command['config']['share'] > 10000:
                    raise ValueError('Combined network shares cannot exceed 100%')
                process = subprocess.Popen([sys.executable, '-m', 'adaptive_os.sandbox.presets',
                    command['preset'], json.dumps(command['config']), workload_id, str(self.cpu),
                    str(self.relay.port if self.relay else 0)],
                    stdin=subprocess.PIPE if command['preset'] == 'cache' else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, process_group=0)
                try:
                    # Set and verify before acknowledging; child Python startup can
                    # otherwise briefly inherit the collector's CPU affinity.
                    os.sched_setaffinity(process.pid, {self.cpu})
                    if os.sched_getaffinity(process.pid) != {self.cpu}:
                        raise OSError('Workload CPU affinity readback did not match')
                    current = read_process(process.pid)
                    if not current:
                        raise ValueError('Workload exited before its identity could be read')
                    self.children[workload_id] = Child(process, command['preset'], command['config'],
                                                       current['identity'], time.monotonic_ns())
                    if command['preset'] == 'cache':
                        self.children[workload_id].start_command = command
                        os.set_blocking(process.stdin.fileno(), False)
                    if command['preset'] == 'network':
                        self.children[workload_id].allocated = command['config']['share']
                        self.relay.add_flow(workload_id, current['identity'], command['config']['share'])
                    self.register(process.stdout, workload_id)
                    self.register(process.stderr, workload_id, True)
                except Exception:
                    process.kill()
                    process.wait()
                    raise
                if command['preset'] == 'cache':
                    return  # Confirm physical allocation before acknowledging start.
                result = {'workload_id': workload_id, 'pid': process.pid, 'identity': current['identity'],
                          'nice': current['nice'], 'cpu': self.cpu, 'status': 'running',
                          'preset': command['preset'], 'config': command['config']}
                if command['preset'] == 'network':
                    result['allocated'] = command['config']['share']
            elif op == 'stop':
                if self.transaction:
                    raise ValueError('Awaiting memory transaction acknowledgement before stopping a workload')
                workload_id = command['workload_id']
                if workload_id not in self.children:
                    raise ValueError('Workload is not running')
                code = self.stop_child(workload_id)
                result = {'workload_id': workload_id, 'status': 'stopped', 'exit_code': code}
            elif op == 'priority':
                child = self.owned_child(command['workload_id'])
                if getattr(self, 'resource', 'cpu') != 'cpu':
                    raise ValueError('CPU priorities are fixed in Memory and Network mode')
                if not self.priority_supported and child.preset != 'probe':
                    raise ValueError(self.priority_reason)
                transition = self.set_priority(command['workload_id'], command['identity'], command['nice'])
                result = {'workload_id': command['workload_id'], 'identity': command['identity'],
                          'nice': transition['actual']}
            elif op in ('memory_budget', 'network_share'):
                self.transfer_allocation(command)
                return
            self.emit('ack', request_id=request_id, op=op, ok=True, **result)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.emit('ack', request_id=request_id, op=raw.get('op') if isinstance(raw, dict) else None,
                      ok=False, error=str(exc))

    def read_stream(self, key):
        stream = key.fileobj
        # An earlier event in this select batch may have stopped the child.
        if stream not in self.buffers:
            return
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
                if isinstance(value, dict) and value.get('kind') == 'allocation_ack':
                    self.allocation_ack(workload_id, value)
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
                child.start_ns = value['start_ns']
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
            rate = units_delta = interval_start_ns = interval_end_ns = None
            progress, prior = child.progress, child.prior_progress
            if progress and prior is None:
                prior = {'mono_ns': progress['start_ns'], 'units': 0}
            fresh = bool(progress and 0 <= now - progress['mono_ns'] < 3_000_000_000)
            if fresh and prior:
                elapsed = progress['mono_ns'] - prior['mono_ns']
                count = progress['units'] - prior['units']
                if elapsed > 0 and count >= 0:
                    rate = count * 1e9 / elapsed
                    units_delta = count
                    interval_start_ns, interval_end_ns = prior['mono_ns'], progress['mono_ns']
                    if child.preset in ('memory', 'io'):
                        rate /= 1024 * 1024
                        units_delta /= 1024 * 1024
                    elif child.preset == 'network':
                        rate *= 8 / 1e6
            metrics = latency_window(child.ticks, child.start_ns, now) if fresh else latency_window([], child.start_ns, now)
            if child.preset in ('cache', 'network'):
                metrics = (progress.get('resource_window') or {}) if fresh else {}
                metrics = {**metrics, 'resource': 'memory' if child.preset == 'cache' else 'network',
                           'allocated': child.allocated, 'configured_capacity': child.config.get('mib', child.config.get('share'))}
                if fresh:
                    metrics.update({k: progress.get(k) for k in ('cache_hits', 'cache_misses', 'failed_transfers',
                        'completed_records', 'queue_delay_ms', 'delivered_mbps')})
            self.emit('sample', workload_id=workload_id, cpu_pct=cpu_percent(child.previous, current),
                      rss_mib=current['rss_mib'], read_bytes=current['read_bytes'], write_bytes=current['write_bytes'],
                      nice=current['nice'], state=current['state'], app_rate=rate,
                      app_units_delta=units_delta, app_interval_start_ns=interval_start_ns,
                      app_interval_end_ns=interval_end_ns,
                      app_unit=('requests/s' if child.preset == 'cache' else 'Mbit/s' if child.preset == 'network'
                                else 'MiB/s' if child.preset in ('memory', 'io') else 'hashes/s'),
                      warmup=now - child.start_ns < 5_000_000_000, progress_fresh=fresh, **metrics)
            child.previous = current

    def run(self):
        self.register(sys.stdin.buffer, None)
        self.emit('ready', allowed_cpus=self.allowed_cpus, workload_cpu=self.cpu,
                  collector_cpu=self.collector_cpu, shared_collector=self.cpu == self.collector_cpu,
                  priority_supported=self.priority_supported, priority_reason=self.priority_reason,
                  platform=sys.platform, uid=os.getuid(), lease_seconds=10,
                  capabilities={'cpu': True, 'memory': True, 'network': True, 'memory_pool_mib': 512},
                  resource_limits={'nice': list(resource.getrlimit(resource.RLIMIT_NICE))})
        next_sample = time.monotonic() + 1
        try:
            while not self.stopping:
                for key, _ in self.selector.select(.1):
                    self.read_stream(key)
                flush_output(self.output)
                now = time.monotonic()
                if self.allocation_deadline is not None and now >= self.allocation_deadline:
                    raise RuntimeError('Allocation acknowledgement timed out; cleaning up owned workloads')
                if self.relay and self.relay.error:
                    raise RuntimeError('Network relay failed: ' + self.relay.error)
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
            if self.relay:
                self.relay.close()
            self.emit('closed')
            deadline = time.monotonic() + 1
            while self.output and time.monotonic() < deadline:
                flush_output(self.output)
                time.sleep(.01)
            self.selector.close()


def main():
    supervisor = Supervisor()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: setattr(supervisor, 'stopping', True))
    supervisor.run()


if __name__ == '__main__':
    main()
