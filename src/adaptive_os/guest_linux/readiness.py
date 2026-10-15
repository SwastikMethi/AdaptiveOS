"""Fresh VM-specific qualification; Docker reports and overrides are ineligible."""

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import uuid

from ..decision_controller import ModelProposal
from ..model_providers import JEV_MODEL
from .policy import SCHEMA
from .resources import MEMORY_PARENT, NETWORK_LIMITS

FRESH_NS = 24 * 3600 * 1_000_000_000


def fingerprint(snapshot, resource):
    if resource not in ('cpu', 'memory', 'network') or snapshot.get('backend') != 'guest-linux':
        raise ValueError('Guest resource baseline required')
    baseline = snapshot['preflight']['baseline']['fingerprint']
    root = Path(__file__).parent
    installed = snapshot.get('service_fingerprint')
    if not isinstance(installed, str) or len(installed) != 64:
        raise ValueError('Installed guest service fingerprint required')
    sources = {'guest/' + path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob('*.py')}
    sources.update({name: hashlib.sha256((root.parent / (name + '.py')).read_bytes()).hexdigest()
                    for name in ('model_helper', 'model_providers', 'model_runtime', 'decision_controller', 'session_store')})
    value = {'backend': 'guest-linux', 'schema': SCHEMA, 'resource': resource, 'vm_baseline': baseline,
             'model': JEV_MODEL, 'python': sys.version, 'contexts': 108, 'cadence_seconds': 5, 'deadline_ms': 1000,
             'parent_memory': MEMORY_PARENT, 'network': NETWORK_LIMITS, 'placement': '1-3', 'sources': sources,
             'installed_service': installed}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def corpus(resource):
    if resource not in ('cpu', 'memory', 'network'):
        raise ValueError('Unknown resource')
    contexts = []
    for count in (1, 3, 7):
        for case in range(36):
            pressure = (0, 1, 5, 10, 30, 70)[case % 6]
            changed = case % 3 == 1
            headroom = resource == 'memory' and case % 9 == 8
            tasks = [{'label': 'foreground' if index == 0 else f'b{index}',
                      'importance': 'High' if index == 0 else ('Low', 'Normal', 'High')[(case + index) % 3],
                      'cpu_weight': 100 if index == 0 or not changed else (50, 25)[case % 2],
                      'cpu_percent': (60 + pressure) if pressure else 2,
                      'cpu_pressure_percent': pressure, 'memory_current_bytes': (128 + (index + case) % 3 * 64) * 1024**2,
                      'memory_low_bytes': min(384 * 1024**2, 3 * 1024**3 // (count + 1) // (64 * 1024**2) * (64 * 1024**2)),
                      'memory_high_bytes': (384 + (index + case) % 3 * 64) * 1024**2, 'memory_pressure_percent': pressure / 10,
                      'refaults_delta': pressure * 20, 'upload_backlog_bytes': pressure * 1500,
                      'download_backlog_bytes': pressure * 3000,
                      'upload_bits_per_second': 20_000_000 / (count + 1) if pressure else 0,
                      'download_bits_per_second': 20_000_000 / (count + 1) if pressure else 0}
                     for index in range(count + 1)]
            contexts.append({'schema': SCHEMA, 'backend': 'guest-linux', 'resource': resource, 'placement': '1-3',
                             'parent_memory_max_bytes': MEMORY_PARENT['max'], 'network_parent_bps': NETWORK_LIMITS['parent_bps'],
                             'window_seconds': 10, 'tasks': tasks, 'changed': changed, 'headroom_required': headroom,
                             'available_actions': ['hold', 'restore'] if headroom or case % 7 == 6 else ['hold', 'restore', 'favor']})
    return contexts


def valid_response(row):
    try:
        proposal = ModelProposal.parse(row.get('proposal'), resource='memory')
        latency = row['latency_ms']
        return (row['valid'] is True and type(latency) in (int, float) and math.isfinite(latency)
                and 0 <= latency < 1000 and proposal.inference_ms < 1000 and proposal.model == JEV_MODEL
                and row['response_request_id'] == row['request_id']
                and proposal.action in row['context']['available_actions'])
    except (ValueError, TypeError, KeyError):
        return False


def validate_report(report, snapshot, resource, *, now_ns=None, shadow=False):
    try:
        if report.get('passed') is False or report.get('error'):
            return False
        now = time.time_ns() if now_ns is None else now_ns
        rows = report['rows']
        expected_kind = 'guest_shadow' if shadow else 'guest_readiness'
        if (report['kind'] != expected_kind or report['backend'] != 'guest-linux' or report['resource'] != resource
                or report['fingerprint'] != fingerprint(snapshot, resource)
                or not 0 <= now - report['completed_at_ns'] <= FRESH_NS
                or len(rows) < (36 if shadow else 108) or not shadow and len(rows) != 108):
            return False
        if len({row['request_id'] for row in rows}) != len(rows):
            return False
        expected_contexts = corpus(resource)
        for index, row in enumerate(rows):
            if row.get('audited') is not True or row.get('unauthorized_actions') != 0:
                return False
            if row.get('valid') and row.get('response_request_id') != row['request_id']:
                return False
            if type(row['dispatch_seconds']) not in (int, float) or not math.isfinite(row['dispatch_seconds']):
                return False
            if index and row['dispatch_seconds'] - rows[index - 1]['dispatch_seconds'] < 4.999:
                return False
            if shadow:
                if (row.get('source') != 'live_guest' or row.get('kernel_guard_passed') is not True
                        or not row.get('binding') or row['context'].get('resource') != resource):
                    return False
            elif row['index'] != index or row['context'] != expected_contexts[index]:
                return False
        return sum(valid_response(row) for row in rows) / len(rows) >= .95
    except (KeyError, TypeError, ValueError, OSError):
        return False


def qualified(directory, snapshot, resource):
    reports = {}
    for path in Path(directory).glob('guest-*.json'):
        try:
            if path.stat().st_size > 4 * 1024**2:
                continue
            report = json.loads(path.read_text())
            kind = report.get('kind')
            if report.get('resource') == resource and kind in ('guest_readiness', 'guest_shadow'):
                if report.get('completed_at_ns', 0) > reports.get(kind, {}).get('completed_at_ns', -1):
                    reports[kind] = report
        except (OSError, ValueError, TypeError):
            continue
    return (validate_report(reports.get('guest_readiness', {}), snapshot, resource)
            and validate_report(reports.get('guest_shadow', {}), snapshot, resource, shadow=True))


def save_report(directory, report):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    destination = path / f'guest-{report["resource"]}-{report["kind"]}-{uuid.uuid4().hex}.json'
    with destination.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    return destination


class GuestReadiness:
    def __init__(self, helper, snapshot, resource, *, clock=time.monotonic):
        self.helper, self.snapshot, self.resource, self.clock = helper, snapshot, resource, clock
        self.contexts, self.rows, self.pending = corpus(resource), [], None
        self.fingerprint = fingerprint(snapshot, resource)
        self.next_dispatch, self.started = 0., clock()
        self.done, self.report = False, None

    def finish(self):
        self.done = True
        self.helper.close()
        self.report = {'kind': 'guest_readiness', 'backend': 'guest-linux', 'resource': self.resource,
                       'fingerprint': self.fingerprint, 'rows': self.rows, 'completed_at_ns': time.time_ns(),
                       'cadence_seconds': 5, 'deadline_ms': 1000}
        self.report['passed'] = validate_report(self.report, self.snapshot, self.resource)

    def poll(self):
        if self.done:
            return
        now = self.clock()
        try:
            response = self.helper.poll()
        except (OSError, RuntimeError, ValueError):
            response = {'error': 'helper_unavailable'}
        if self.pending:
            request_id, started = self.pending
            elapsed = (now - started) * 1000
            if response is None and elapsed < 1000:
                return
            row = {'index': len(self.rows), 'context': self.contexts[len(self.rows)], 'request_id': request_id,
                   'response_request_id': (response or {}).get('request_id'), 'dispatch_seconds': started,
                   'latency_ms': elapsed, 'audited': True, 'unauthorized_actions': 0, 'valid': False}
            if elapsed >= 1000:
                row['error'] = 'deadline'
            elif response.get('request_id') != request_id:
                row['error'] = 'stale_request'
            elif response.get('error'):
                row['error'] = response['error']
            else:
                try:
                    row['proposal'] = asdict(ModelProposal.parse(response.get('proposal'), resource='memory'))
                    row['valid'] = True
                except ValueError:
                    row['error'] = 'invalid_proposal'
            row['latency_ms'] = (self.clock() - started) * 1000
            row['valid'] = valid_response(row)
            row.update({key: (response or {}).get(key) for key in ('helper_cpu_seconds', 'helper_max_rss')})
            self.rows.append(row)
            self.pending, self.next_dispatch = None, started + 5
            if len(self.rows) == 108 or row.get('error') == 'authentication':
                self.finish()
                return
            if elapsed >= 1000:
                self.helper.start()
                self.started = now
        if not self.helper.ready:
            if now - self.started >= 15 or self.helper.metadata.get('kind') == 'startup_error':
                self.finish()
            return
        if not self.pending and now >= self.next_dispatch:
            request_id = str(uuid.uuid4())
            self.pending = (request_id, now)
            self.helper.submit(request_id, self.contexts[len(self.rows)])
