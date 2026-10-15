"""Frozen shadow corpus and per-configuration readiness evidence."""
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import resource
import time
import uuid

from .cpu_rule import CpuWindow, ManagedTask
from .decision_controller import (DecisionContext, RequestBinding, ModelProposal,
                                  FEATURE_VERSION, GUARD_VERSION, PROMPT_VERSION)
from .model_providers import JEV_MODEL, QUESTIONS
from .model_runtime import LAYA_REVISION, runtime_paths


def configuration_fingerprint(provider, device, resource='cpu'):
    from .sandbox.protocol import RESOURCES, VERSION
    from .model_providers import questions_for
    if resource not in RESOURCES:
        raise ValueError('Unknown resource')
    _, checkpoint = runtime_paths()
    lock = checkpoint.parent / 'requirements-laya.lock'
    if not lock.is_file():
        lock = Path(__file__).resolve().parents[2] / 'requirements-laya.lock'
    dependency_digest = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else 'missing'
    identity = {'provider': provider, 'model': JEV_MODEL if provider == 'jev' else LAYA_REVISION,
        'runtime': 'stdlib-http-v1' if provider == 'jev' else 'laya-0.3.27-torch-2.9.1-transformers-4.57.6-fixed-eager-fp32-v1',
        'resource': resource, 'protocol': VERSION, 'warmup_backgrounds': [1, 3, 7],
        'prompt_version': PROMPT_VERSION, 'questions': questions_for(resource), 'feature': FEATURE_VERSION,
        'guards': GUARD_VERSION, 'device': device, 'deadline_ms': 1000,
        'max_tokens': 2048, 'head_tokens': 512, 'threads': 2, 'compile': False,
        'dynamo_disabled': True, 'corpus': 2, 'readiness_cadence_seconds': 5,
        'dependencies': dependency_digest if provider == 'laya' else 'stdlib-http-v1'}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def frozen_contexts(resource='cpu'):
    contexts = []
    for count in (1, 3, 7):
        for case in range(36):
            miss = (0, .004, .01, .03, .2, .8, None, .02, .005)[case % 9]
            cpu = (15, 30, 90)[case % 3] + (case % 5) * .137468231
            tasks = tuple(ManagedTask(str(i), ('Low', 'Normal', 'High')[i % 3],
                          None if miss is None and i % 2 == 0 else cpu / count, 0 if case % 2 else 10,
                          (0 if case % 2 else 10) if case % 4 else 19, i + 1)
                          for i in range(count))
            window = CpuWindow(15, 20, miss, cpu, miss is not None)
            trend = tuple({'seconds': 5, 'miss_rate': value, 'p95_lateness_ms': 0 if value == 0 else 2,
                           'background_cpu_pct': cpu, 'valid': value is not None}
                          for value in (0, .01, miss)[:case % 4])
            binding = RequestBinding('frozen', str(case), 1, str(case), ('f', 100), ())
            context = DecisionContext(binding, window, tasks, trend, (0, 2, 20)[case % 3],
                {str(i): None if miss is None else (case + 1) * 27173.534636 / count for i in range(count)},
                ('hold', 'lower', 'restore') if miss is not None else ('hold',))
            payload = context.public_payload()
            if resource != 'cpu':
                if resource not in ('memory', 'network'):
                    raise ValueError('Unknown resource')
                allocation = (512 // (count + 1) // 16 * 16) if resource == 'memory' else 10000 // (count + 1)
                payload['resource'] = resource
                payload['allocation_unit'] = 'MiB' if resource == 'memory' else 'basis_points'
                payload['foreground_allocation'] = allocation
                payload['window']['metric'] = 'cache_miss_rate' if resource == 'memory' else 'deadline_miss_rate'
                for task in payload['backgrounds']:
                    task.pop('original_nice')
                    task.pop('current_nice')
                    task.update(original_allocation=allocation, current_allocation=allocation)
                payload['available_actions'] = ['hold', 'favor', 'restore'] if miss is not None else ['hold']
            contexts.append(payload)
    return contexts


def _percentile(values, p):
    return sorted(values)[max(0, math.ceil(p * len(values)) - 1)] if values else None


def readiness_report(rows, fingerprint, *, cold_start_ms=None):
    valid = [row for row in rows if row.get('valid') and
             type(row.get('latency_ms')) in (float, int) and 0 <= row['latency_ms'] < 1000]
    audited = all(row.get('audited') is True for row in rows)
    unauthorized = sum(row.get('unauthorized_actions', 0) for row in rows)
    latency = [row['latency_ms'] for row in rows if type(row.get('latency_ms')) in (float, int)
               and math.isfinite(row['latency_ms'])]
    return {'readiness_version': 1, 'fingerprint': fingerprint, 'contexts': len(rows),
            'ready': len(rows) >= 100 and len(valid) / len(rows) >= .95 and audited and unauthorized == 0,
            'valid_in_deadline': len(valid), 'valid_fraction': len(valid) / len(rows) if rows else 0,
            'timeout_fraction': sum(row.get('error') == 'deadline' or row.get('latency_ms', 0) >= 1000
                                    for row in rows) / len(rows) if rows else 0,
            'complete_audit': audited, 'unauthorized_actions': unauthorized,
            'cold_start_ms': cold_start_ms,
            'warm_latency_ms': {name: _percentile(latency, p) for name, p in (('p50', .5), ('p95', .95), ('p99', .99))},
            'rows': rows}


def readiness_matches(report, fingerprint):
    return (report.get('fingerprint') == fingerprint and report.get('ready') is True
            and readiness_report(report.get('rows', []), fingerprint)['ready'])


def latest_reports(directory):
    """Newest full validation wins, including incomplete or failed full runs."""
    reports, order = {}, {}
    for path in sorted(Path(directory).glob('readiness-*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name)):
        try:
            report = json.loads(path.read_text())
            if report.get('validation_kind') == 'connection' or (
                    'validation_kind' not in report and report.get('contexts') == 1):
                continue
            fingerprint = report['fingerprint']
            if isinstance(fingerprint, str):
                completed = report.get('completed_at_ns')
                stamped = type(completed) is int and completed >= 0
                # Legacy evidence cannot supersede dated evidence. With only
                # legacy files, a failure wins because completion order is unknown.
                rank = ((1, completed, not bool(report.get('ready'))) if stamped else
                        (0, not bool(report.get('ready')), path.stat().st_mtime_ns))
                if fingerprint not in order or rank > order[fingerprint]:
                    reports[fingerprint], order[fingerprint] = report, rank
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return reports


def live_readiness_matches(report, fingerprint, provider):
    if not readiness_matches(report, fingerprint):
        return False
    if provider == 'laya':
        checks = report.get('native_checks', {})
        return checks.get('fingerprint') == fingerprint and all(
            checks.get(name) is True for name in ('shadow', 'visible_soak', 'minimized_soak'))
    return True


class ReadinessRun:
    """Polled by UI; never blocks telemetry and never emits a control command."""
    def __init__(self, helper, provider, device, *, resource='cpu', connection_only=False, clock=time.monotonic):
        self.helper, self.clock = helper, clock
        self.resource, self.connection_only = resource, connection_only
        self.fingerprint = configuration_fingerprint(provider, device, resource)
        self.contexts = frozen_contexts(resource)[:1] if connection_only else frozen_contexts(resource)
        self.rows = []
        self.pending = None
        self.done = False
        self.started = clock()
        self.next_dispatch = self.started
        self.cadence_seconds = 0 if connection_only else 5
        self.report = None
        self.helper_startups = []
        self._awaiting_startup = True

    def poll(self):
        if self.done:
            return
        if not self.helper.ready:
            self.helper.poll()
            if self.helper.metadata.get('kind') == 'startup_error' or self.clock() - self.started > 180:
                self.rows.append({'valid': False, 'error': 'startup_failed', 'audited': True, 'unauthorized_actions': 0})
                self.finish()
            return
        if self._awaiting_startup:
            cold_start = self.helper.metadata.get('host_cold_start_ms', self.helper.metadata.get('cold_start_ms'))
            if type(cold_start) not in (int, float) or not math.isfinite(cold_start) or cold_start < 0:
                cold_start = None
            self.helper_startups.append({'before_context': len(self.rows), 'cold_start_ms': cold_start})
            self._awaiting_startup = False
        if self.pending:
            request_id, started = self.pending
            response = self.helper.poll()
            elapsed = (self.clock() - started) * 1000
            if response is None and elapsed < 1000:
                return
            row = {'context_index': len(self.rows), 'context': self.contexts[len(self.rows)],
                   'request_id': request_id, 'latency_ms': elapsed, 'audited': True,
                   'unauthorized_actions': 0, 'valid': False}
            row['input_bytes'] = len(json.dumps(row['context'], separators=(',', ':')).encode())
            if elapsed >= 1000:
                row['error'] = 'deadline'
            elif response.get('request_id') != request_id:
                row['error'] = 'stale_request'
            elif 'error' in response:
                row['error'] = response['error']
            else:
                try:
                    row['proposal'] = asdict(ModelProposal.parse(response.get('proposal')) if self.resource == 'cpu'
                                            else ModelProposal.parse(response.get('proposal'), resource=self.resource))
                    row['valid'] = True
                    row['helper_cpu_seconds'] = response.get('helper_cpu_seconds')
                    row['helper_max_rss'] = response.get('helper_max_rss')
                except ValueError:
                    row['error'] = 'invalid_proposal'
            row['latency_ms'] = (self.clock() - started) * 1000
            if row['latency_ms'] >= 1000:
                row.update(valid=False, error='deadline')
            if row.get('error') == 'deadline':
                cleanup_started = self.clock()
                self.helper.cancel()
                row['cleanup_ms'] = (self.clock() - cleanup_started) * 1000
            row['controller_cpu_seconds'] = time.process_time() - self.dispatch_cpu
            row['controller_max_rss'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            self.rows.append(row)
            self.pending = None
            self.next_dispatch = started + self.cadence_seconds
            if len(self.rows) >= len(self.contexts) or row.get('error') == 'authentication':
                self.finish()
                return
            if not self.helper.ready:
                self.helper.start()
                self.started = self.clock()
                self._awaiting_startup = True
                return
        if self.helper.ready and self.pending is None and self.clock() >= self.next_dispatch:
            request_id = uuid.uuid4().hex
            self.pending = (request_id, self.clock())
            self.dispatch_cpu = time.process_time()
            self.helper.submit(request_id, self.contexts[len(self.rows)])

    def finish(self):
        self.done = True
        self.report = readiness_report(self.rows, self.fingerprint,
            cold_start_ms=self.helper_startups[0]['cold_start_ms'] if self.helper_startups else None)
        self.report['readiness_version'] = 2
        self.report.update(resource=self.resource, validation_kind='connection' if self.connection_only else 'full',
                           completed_at_ns=time.time_ns())
        self.report['latency_semantics'] = 'dispatch_through_validation_excluding_cleanup'
        self.report['helper_startups'] = list(self.helper_startups)
        cold_starts = [row['cold_start_ms'] for row in self.helper_startups if row['cold_start_ms'] is not None]
        self.report['cold_start_latency_ms'] = {
            name: _percentile(cold_starts, p) for name, p in (('p50', .5), ('p95', .95), ('p99', .99))}
        self.report['dispatch_cadence_seconds'] = self.cadence_seconds

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'readiness-{self.fingerprint[:12]}-{uuid.uuid4().hex[:8]}.json'
        with path.open('x') as stream:
            json.dump(self.report, stream, indent=2, allow_nan=False)
        return path
