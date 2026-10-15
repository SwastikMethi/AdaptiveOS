"""Unprivileged guest decisions. Models choose actions; the service owns values."""

from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import time
import uuid

from ..decision_controller import ModelProposal, MAX_INPUT_BYTES
from ..model_providers import JevProvider, JEV_MODEL
from ..model_runtime import HelperProcess
from .resources import MEMORY_PARENT, NETWORK_LIMITS, counters, initial_lows

SCHEMA = 'guest-control-v1'


def control_of(snapshot):
    return snapshot.get('resource_control') or snapshot.get('cpu_control') or {}


def pressure(value):
    for line in (value or '').splitlines():
        if line.startswith('some '):
            number = float(dict(item.split('=') for item in line.split()[1:])['avg10'])
            if math.isfinite(number) and number >= 0:
                return number
    return None


def public_context(snapshot, previous=None):
    control = control_of(snapshot)
    resource = control.get('resource', 'cpu')
    foreground = control['foreground_id']
    previous_rows = {t['id']: t for t in (previous or {}).get('tasks', [])}
    seconds = (snapshot.get('observed_at_ns', 0) - (previous or {}).get('observed_at_ns', 0)) / 1e9 if previous else 0
    rows = []
    for task in sorted(snapshot['tasks'], key=lambda t: (t['id'] != foreground, t['id'])):
        old = previous_rows.get(task['id'], {})
        usage = (task.get('cpu_usage_usec', 0) - old.get('cpu_usage_usec', 0)) / (seconds * 10_000) if seconds > 0 and old else None
        network = task.get('network', {})
        refault = counters(task.get('memory_stat', {})).get('workingset_refault_file', 0)
        old_refault = counters(old.get('memory_stat', {})).get('workingset_refault_file', 0)
        rows.append({'label': 'foreground' if task['id'] == foreground else f'b{len(rows)}',
                     'importance': control['importance'][task['id']], 'cpu_weight': task['cpu_weight'],
                     'cpu_percent': max(0., usage) if usage is not None else None,
                     'cpu_pressure_percent': pressure(task.get('cpu_pressure')),
                     'memory_current_bytes': task.get('memory_current'), 'memory_low_bytes': task.get('memory_low'),
                     'memory_high_bytes': 'max' if task.get('memory_high') == 'max' else int(task.get('memory_high', 0)),
                     'memory_pressure_percent': pressure(task.get('memory_pressure')),
                     'refaults_delta': max(0, refault - old_refault) if old else None,
                     'upload_backlog_bytes': network.get('upload_backlog'), 'download_backlog_bytes': network.get('download_backlog'),
                     **{direction + '_share_basis_points': control['expected'][direction]['shares'][task['id']]
                        if resource == 'network' else None for direction in ('upload', 'download')},
                     **{direction + '_bits_per_second': max(0, network.get(direction + '_bytes', 0) -
                        old.get('network', {}).get(direction + '_bytes', 0)) * 8 / seconds if seconds > 0 and old else None
                        for direction in ('upload', 'download')}})
    if resource == 'cpu':
        changed = any(task['cpu_weight'] != 100 for task in snapshot['tasks'])
    elif resource == 'memory':
        starts = initial_lows({t['id']: 'max' if t['memory_high'] == 'max' else int(t['memory_high']) for t in snapshot['tasks']})
        changed = any(t['memory_low'] != starts[t['id']] for t in snapshot['tasks'])
    else:
        from .resources import equal_shares
        changed = any(control['expected'][direction]['shares'] != equal_shares(control['importance'])
                      for direction in ('upload', 'download'))
    available = ['hold', 'restore']
    if control.get('ready') and not control.get('headroom_required'):
        available.append('favor')
    context = {'schema': SCHEMA, 'backend': 'guest-linux', 'resource': resource, 'placement': '1-3',
               'parent_memory_max_bytes': MEMORY_PARENT['max'], 'network_parent_bps': NETWORK_LIMITS['parent_bps'],
               'window_seconds': max(0, seconds), 'tasks': rows, 'changed': changed,
               'available_actions': available, 'headroom_required': control.get('headroom_required', False)}
    if len(json.dumps(context, allow_nan=False).encode()) > MAX_INPUT_BYTES:
        raise ValueError('Guest model input exceeds bound; truncation forbidden')
    return context


def rule_action(context):
    front = context['tasks'][0]
    if context['resource'] == 'cpu':
        pressured = (front.get('cpu_pressure_percent') or 0) >= 5 and any(
            (t.get('cpu_percent') or t.get('cpu_pressure_percent') or 0) >= 1 for t in context['tasks'][1:])
    elif context['resource'] == 'memory':
        pressured = (front.get('memory_pressure_percent') or 0) >= 1 or (front.get('refaults_delta') or 0) > 0
    else:
        pressured = (front.get('upload_backlog_bytes') or 0) + (front.get('download_backlog_bytes') or 0) > 0
    if context.get('headroom_required'):
        return 'hold'
    if pressured and 'favor' in context['available_actions']:
        return 'favor'
    return 'restore' if not pressured and context['changed'] else 'hold'


class GuestJevProvider(JevProvider):
    def questions(self, context):
        if context.get('schema') != SCHEMA or context.get('backend') != 'guest-linux':
            raise ValueError('Guest context required')
        mechanisms = {'cpu': 'CPU weights, with fixed CPU placement',
                      'memory': 'memory.low best-effort reclaim protection only; memory.high is independent measured headroom, not allocation',
                      'network': 'shared upload/download guaranteed shares with idle borrowing'}
        criteria = {'hold': 'Keep settings; evidence is insufficient or a change has no benefit.',
                    'favor': 'Make one eligible bounded intervention to help the foreground.',
                    'restore': 'Restore the run starting settings when intervention costs are no longer justified.'}
        available = context.get('available_actions')
        if not isinstance(available, list) or set(available) not in ({'hold', 'restore'}, set(criteria)):
            raise ValueError('Invalid guest available actions')
        return {'action': {'type': 'choice', 'instructions':
            'Choose using the anonymous Linux VM observations and available actions. Protect foreground responsiveness '
            'while preserving every background task and its importance. Missing values are unknown. '
            'The resource is ' + mechanisms[context['resource']] + '. Code selects tasks and bounded values; '
            'do not change CPU placement, high thresholds, or the parent safety cap.',
            'criteria': {action: description for action, description in criteria.items() if action in available}}}

    def proposal_resource(self, context):
        return 'memory'  # Shared validator's Hold/Favor/Restore vocabulary, for all guest resources.


def new_helper(key):
    return HelperProcess([sys.executable, '-m', 'adaptive_os.model_helper'],
                         {'provider': 'jev', 'backend': 'guest-linux', 'key': key})


class GuestPolicy:
    def __init__(self, directory, *, key='', clock=time.monotonic, helper=None, require_qualification=True):
        self.directory, self.key, self.clock = Path(directory), key, clock
        self.require_qualification = require_qualification
        self.helper, self.mode, self.pending = helper, 'manual', None
        self.next_decision = 0.
        self.previous = None
        self.decisions, self.fallbacks = 0, 0
        self.shadow_rows = []
        self.authentication_failed = False
        self.rejected_binding = None

    def set_mode(self, mode, snapshot, resource=None):
        if mode not in ('manual', 'rule', 'jev', 'shadow'):
            raise ValueError('Unknown guest policy')
        from .readiness import qualified
        resource = resource or control_of(snapshot).get('resource', 'cpu')
        needs_qualification = self.require_qualification or control_of(snapshot).get('scored', False)
        if mode == 'jev' and needs_qualification and not qualified(self.directory, snapshot, resource):
            raise ValueError('Fresh VM-specific readiness and passing resource shadow checks are required')
        if mode in ('jev', 'shadow') and not self.key and not self.helper:
            raise ValueError('A Jev key is required; it is never recorded')
        self.cancel()
        self.mode, self.next_decision, self.previous = mode, 0., None
        self.shadow_rows = []
        if mode in ('jev', 'shadow') and self.helper is None:
            self.helper = new_helper(self.key)

    def cancel(self):
        self.pending = None
        if self.helper:
            self.helper.close()
            self.helper = None

    def close(self):
        self.cancel()
        self.key = ''

    def tick(self, snapshot, apply, audit):
        now, control = self.clock(), control_of(snapshot)
        if not snapshot.get('run_id') or self.mode == 'manual':
            return
        binding = (snapshot['generation'], snapshot['run_id'], control.get('observation_id'))
        if binding == self.rejected_binding:
            return
        if self.pending:
            pending = self.pending
            response, reason, proposal = None, '', None
            try:
                response = self.helper.poll()
            except (OSError, RuntimeError, ValueError):
                reason = 'helper_unavailable'
            elapsed = (self.clock() - pending['started']) * 1000
            if elapsed >= 1000:
                reason = 'deadline'
            if binding != pending['binding']:
                reason = 'stale_observation'
            if not response and not reason:
                return
            if not reason:
                if response.get('request_id') != pending['request_id']:
                    reason = 'stale_request'
                elif response.get('error'):
                    reason = response['error']
                else:
                    try:
                        proposal = asdict(ModelProposal.parse(response.get('proposal'), resource='memory'))
                        if proposal['model'] != JEV_MODEL or proposal['action'] not in pending['context']['available_actions']:
                            raise ValueError('Unavailable model action')
                    except ValueError:
                        reason = 'invalid_proposal'
            elapsed = (self.clock() - pending['started']) * 1000
            if elapsed >= 1000:
                reason = 'deadline'
            self.pending = None
            if reason:
                self.fallbacks += 1
                if reason == 'authentication':
                    self.authentication_failed = True
                if reason != 'rate_limit':
                    self.cancel()
            action = pending['reference'] if reason or self.mode == 'shadow' else proposal['action']
            event = {'kind': 'guest_model_audit', **pending, 'binding': list(pending['binding']),
                     'latency_ms': elapsed, 'proposal': proposal, 'valid': not bool(reason),
                     'fallback_reason': reason, 'action': action, 'mode': self.mode,
                     'unauthorized_actions': 0, 'audited': True, 'response_request_id': (response or {}).get('request_id')}
            event.update(source='live_guest', kernel_guard_passed=False)
            try:
                if binding == pending['binding']:
                    event['acknowledgement'] = self.apply_action(action, snapshot, apply, audit, fallback_counted=bool(reason))
                    event['kernel_guard_passed'] = isinstance(event['acknowledgement'], dict)
                    if event['acknowledgement'] is None and not reason:
                        event.update(valid=False, fallback_reason='stale_observation', action='hold')
            finally:
                audit(**event)
                if self.mode == 'shadow':
                    self.shadow_rows.append(event)
            return
        observable = (control.get('valid_seconds', 0) >= 10 and not control.get('decision_wait_seconds', 0)
                      and not control.get('headroom_required'))
        if now < self.next_decision or not (control.get('ready') or observable):
            return
        context = public_context(snapshot, self.previous)
        self.previous = snapshot
        self.next_decision, self.decisions = now + 5, self.decisions + 1
        reference = rule_action(context)
        if self.mode == 'jev' and (self.require_qualification or control.get('scored', False)):
            from .readiness import qualified
            if not qualified(self.directory, snapshot, context['resource']):
                self.fallbacks += 1
                audit(kind='guest_policy_fallback', fallback_reason='qualification_expired', mode=self.mode)
                self.apply_action(reference, snapshot, apply, audit, fallback_counted=True)
                return
        if self.mode in ('jev', 'shadow'):
            if self.helper is None and self.key and not self.authentication_failed:
                self.helper = new_helper(self.key)
            if self.helper:
                try:
                    self.helper.poll()
                    if self.helper.ready:
                        request_id = str(uuid.uuid4())
                        self.pending = {'request_id': request_id, 'started': now, 'dispatch_seconds': now,
                                        'binding': binding, 'reference': reference, 'context': context}
                        self.helper.submit(request_id, context)
                        audit(kind='guest_model_request', **self.pending)
                        return
                except (OSError, RuntimeError, ValueError):
                    self.cancel()
            self.fallbacks += 1
            audit(kind='guest_policy_fallback', fallback_reason='helper_starting_or_unavailable', mode=self.mode)
        self.apply_action(reference, snapshot, apply, audit, fallback_counted=self.mode in ('jev', 'shadow'))

    def apply_action(self, action, snapshot, apply, audit, *, fallback_counted=False):
        control = control_of(snapshot)
        audit(kind='guest_policy_decision', mode=self.mode, resource=control.get('resource', 'cpu'),
              action=action, run_id=snapshot['run_id'], observation_id=control['observation_id'])
        if action == 'restore':
            return apply('restore')
        else:
            try:
                return apply('resource_decide', generation=snapshot['generation'], observation_id=control['observation_id'], action=action)
            except RuntimeError as exc:
                # A pre-write freshness rejection requires a new observation, never a replay.
                if str(exc) not in ('Fresh resource observation required', 'Fresh observation with ten valid seconds required'):
                    raise
                self.rejected_binding = (snapshot['generation'], snapshot['run_id'], control['observation_id'])
                if not fallback_counted:
                    self.fallbacks += 1
                audit(kind='guest_policy_fallback', mode=self.mode, fallback_reason='stale_observation',
                      error=str(exc), run_id=snapshot['run_id'], observation_id=control['observation_id'],
                      requested_action=action, action='hold', kernel_guard_passed=False)
                return None
