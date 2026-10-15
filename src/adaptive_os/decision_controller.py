"""Policy coordination. Models propose actions; deterministic guards own control.

No Qt, Docker, credentials, model imports, or blocking inference lives here.
Linux timestamps govern observation/cooldown; host monotonic time governs replies.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
import json
import math
import re
import time
import uuid
from typing import Protocol

from .cpu_rule import CpuDecision, CpuWindow, ManagedTask, RuleState, acknowledge, evaluate_rule
from .resource_control import allocation_decision, evaluate_resource_rule

FEATURE_VERSION = 'metrics-v1'
GUARD_VERSION = 'resources-v6'
PROMPT_VERSION = 'decision-v1'
DEADLINE_SECONDS = 1.0
MAX_INPUT_BYTES = 16_384
ACTIONS = ('hold', 'lower', 'restore')


def actions_for(resource):
    if resource not in ('cpu', 'memory', 'network'):
        raise ValueError('Unknown resource')
    return ACTIONS if resource == 'cpu' else ('hold', 'favor', 'restore')


class DecisionProvider(Protocol):
    def predict(self, context: dict) -> dict: ...


def _number(value, *, low=0, high=None, unknown=False):
    if value is None and unknown:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < low or (
            high is not None and value > high):
        raise ValueError('invalid finite measurement')
    return value


@dataclass(frozen=True)
class ModelProposal:
    action: str
    probabilities: dict
    confidence: float
    model: str
    inference_ms: float
    input_tokens: int | None = None

    @classmethod
    def parse(cls, value, resource='cpu'):
        actions = actions_for(resource)
        try:
            if not isinstance(value, dict) or value['action'] not in actions:
                raise ValueError('invalid action')
            probabilities = value['probabilities']
            if not isinstance(probabilities, dict) or set(probabilities) != set(actions):
                raise ValueError('incomplete probabilities')
            probabilities = {key: _number(probabilities[key], high=1) for key in actions}
            # Laya rounds each of three probabilities to four decimal places.
            if abs(sum(probabilities.values()) - 1) > .00015:
                raise ValueError('probabilities do not sum to one')
            if probabilities[value['action']] + 1e-8 < max(probabilities.values()):
                raise ValueError('choice disagrees with probabilities')
            model = value['model']
            if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._/@:+-]{1,160}', model):
                raise ValueError('invalid model identity')
            tokens = value.get('input_tokens')
            if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= 32768):
                raise ValueError('invalid token count')
            return cls(value['action'], probabilities, _number(value['confidence'], high=1),
                       model, _number(value['inference_ms']), tokens)
        except (KeyError, TypeError) as exc:
            raise ValueError('incomplete proposal') from exc


@dataclass(frozen=True)
class RequestBinding:
    session: str
    trial: str
    generation: int
    window_id: str
    foreground: tuple
    processes: tuple


@dataclass(frozen=True)
class DecisionContext:
    binding: RequestBinding
    window: CpuWindow
    tasks: tuple
    trend: tuple
    foreground_p95_ms: float | None
    background_work: dict
    available_actions: tuple
    resource: str = 'cpu'
    foreground_allocation: int | None = None

    def public_payload(self):
        """Strict allowlist: internal process/session identifiers never leave host."""
        if not 1 <= len(self.tasks) <= 7:
            raise ValueError('requires one to seven backgrounds')
        current = {'seconds': _number(self.window.end - self.window.start),
                   'miss_rate': _number(self.window.miss_rate, high=1, unknown=True),
                   'p95_lateness_ms': _number(self.foreground_p95_ms, unknown=True),
                   'background_cpu_pct': _number(self.window.background_cpu, high=700, unknown=True),
                   'valid': bool(self.window.valid)}
        backgrounds = []
        for index, task in enumerate(self.tasks, 1):
            if task.importance not in ('Low', 'Normal', 'High') or task.original_nice not in (0, 10, 19) or task.current_nice not in (0, 10, 19):
                raise ValueError('invalid managed configuration')
            backgrounds.append({'label': f'b{index}', 'importance': task.importance,
                'cpu_pct': _number(task.cpu_pct, high=100, unknown=True),
                'work_per_second': _number(self.background_work.get(task.workload_id), unknown=True),
                'original_nice': task.original_nice, 'current_nice': task.current_nice})
        payload = {'feature_version': FEATURE_VERSION, 'window': current,
                   'trend': list(self.trend), 'backgrounds': backgrounds,
                   'available_actions': list(self.available_actions)}
        if self.resource != 'cpu':
            payload.update(resource=self.resource, foreground_allocation=self.foreground_allocation,
                           allocation_unit='MiB' if self.resource == 'memory' else 'basis_points')
            current['metric'] = 'cache_miss_rate' if self.resource == 'memory' else 'deadline_miss_rate'
            for row, task in zip(backgrounds, self.tasks):
                row.pop('original_nice')
                row.pop('current_nice')
                row.update(original_allocation=task.original_allocation, current_allocation=task.current_allocation)
        if len(json.dumps(payload, allow_nan=False).encode()) > MAX_INPUT_BYTES:
            raise ValueError('context too large; truncation forbidden')
        return payload


class PolicyController:
    def __init__(self, helper=None, *, clock=time.monotonic, audit=None, resource='cpu'):
        actions_for(resource)
        self.resource = resource
        self.foreground_task = None
        self.helper, self.clock, self.audit = helper, clock, audit or (lambda **event: None)
        self.state = RuleState()
        self.active = False
        self.requested = self.effective = 'rule'
        self.shadow = False
        self.generation = 0
        self.captures = {}
        self.outstanding = self.command = None
        self.expired = None
        self.failures = 0
        self.fallback_reason = ''
        self.latest_proposal = None
        self.model_request_id = None
        self.latest_applied = None
        self.latency_ms = None
        self.trend = deque(maxlen=3)
        self.window = None
        self.reference = CpuDecision('hold')
        self.command_audit = {}

    @property
    def cycle_id(self):
        return f'{self.generation}:{self.window.start}:{self.window.end}' if self.window else None

    def event(self, kind, **fields):
        event = dict(kind=kind, event_version=2, guard_version=GUARD_VERSION,
                   resource=self.resource,
                   session=getattr(self, 'session', None), trial=getattr(self, 'trial', None),
                   generation=self.generation, cycle_id=self.cycle_id,
                   host_monotonic_ns=int(self.clock() * 1e9),
                   requested=self.requested, effective=self.effective, shadow=self.shadow,
                   decision_window=f'{self.window.start}:{self.window.end}' if self.window else None)
        self.audit(**(event | fields))

    def activate(self, session, trial, foreground, tasks, provider='rule', *, shadow=False, foreground_task=None):
        changed = any(t.current_nice != t.original_nice for t in tasks)
        if self.resource != 'cpu':
            changed = changed or any(t.current_allocation != t.original_allocation for t in tasks)
            changed = changed or (foreground_task is not None and
                                  foreground_task.current_allocation != foreground_task.original_allocation)
        if self.active and (self.command or changed):
            raise ValueError('restore existing interventions before changing policy')
        self.stop()
        if provider not in ('rule', 'jev', 'laya') or len({t.workload_id for t in tasks}) != len(tasks):
            raise ValueError('invalid policy configuration')
        if not 1 <= len(tasks) <= 7 or foreground[0] in {t.workload_id for t in tasks}:
            raise ValueError('invalid managed set')
        self.session, self.trial, self.foreground = session, trial, foreground
        if self.resource != 'cpu' and (foreground_task is None or
                (foreground_task.workload_id, foreground_task.identity) != foreground):
            raise ValueError('Resource control requires foreground allocation identity')
        self.foreground_task = foreground_task
        self.captures = {t.workload_id: t for t in tasks}
        self.requested = self.effective = provider
        self.shadow, self.active = shadow, True
        self.state, self.failures, self.fallback_reason = RuleState(), 0, ''
        self.trend.clear()
        self.command = None
        self.window = None
        self.latest_proposal = self.latest_applied = self.latency_ms = None
        self.model_request_id = None
        self.event('policy_started', session=session, trial=trial, generation=self.generation,
                   foreground=foreground, captures=[asdict(t) for t in tasks])

    def stop(self):
        was_active, self.active = self.active, False
        if was_active:
            self.event('policy_stopped')
        self.generation += 1
        outstanding, self.outstanding = self.outstanding, None
        if outstanding:
            binding = outstanding[1].binding
            self.event('model_rejection', request_id=outstanding[0], reason='cancelled',
                       cycle_id=f'{binding.generation}:{binding.window_id}')
        if (outstanding or self.expired) and self.helper:
            self.helper.cancel()
        self.expired = None

    def discard_late_response(self):
        """Drain one expired request without granting it control authority."""
        if not self.expired:
            return
        request_id, context, dispatched = self.expired
        try:
            response = self.helper.poll()
        except (ValueError, OSError, RuntimeError):
            self.helper.cancel()
            self.expired = None
            self.event('model_rejection', request_id=request_id, reason='late_helper_failed',
                       cycle_id=f'{context.binding.generation}:{context.binding.window_id}')
            return
        if response is None:
            return
        if response.get('request_id') != request_id:
            self.event('model_rejection', request_id=request_id, reason='stale_request')
            return
        self.expired = None
        self.event('model_rejection', request_id=request_id, reason='late_response',
                   expired_window=context.binding.window_id,
                   cycle_id=f'{context.binding.generation}:{context.binding.window_id}',
                   latency_ms=(self.clock() - dispatched) * 1000,
                   helper_cpu_seconds=response.get('helper_cpu_seconds'),
                   helper_max_rss=response.get('helper_max_rss'))

    def identities_match(self, tasks, foreground):
        return (foreground == self.foreground and len(tasks) == len(self.captures)
                and {t.workload_id for t in tasks} == set(self.captures)
                and all(t.identity == self.captures[t.workload_id].identity and
                        t.importance == self.captures[t.workload_id].importance and
                        t.original_nice == self.captures[t.workload_id].original_nice and
                        (self.resource == 'cpu' or t.original_allocation ==
                         self.captures[t.workload_id].original_allocation) for t in tasks))

    def guarded(self, action, tasks, foreground, valid=True):
        if not self.active or not self.window:
            return CpuDecision('hold', reason='inactive')
        if not self.identities_match(tasks, foreground):
            return CpuDecision('restore_all', reason='managed process or configuration changed')
        try:
            for task in tasks:
                _number(task.cpu_pct, high=100, unknown=True)
                if type(task.current_nice) is not int or task.current_nice not in (0, 10, 19):
                    raise ValueError('invalid priority')
        except ValueError:
            return CpuDecision('restore_all', reason='invalid current measurements')
        if self.resource != 'cpu':
            from .sandbox.protocol import bounded_int
            try:
                low, high, step = (16, 512, 16) if self.resource == 'memory' else (500, 9500, 1)
                for task in [*tasks, self.foreground_task]:
                    bounded_int(task.current_allocation, low, high, step)
                    if task.current_nice != task.original_nice:
                        raise ValueError('CPU priority changed during resource run')
                if sum(t.current_allocation for t in [*tasks, self.foreground_task]) > (512 if self.resource == 'memory' else 10000):
                    raise ValueError('Allocation pool exceeded')
            except (ValueError, TypeError):
                return CpuDecision('restore_all', reason='invalid resource allocation')
        if action == 'restore_all':
            return CpuDecision('restore_all', reason='invalid telemetry')
        if self.command:
            return CpuDecision('hold', reason='awaiting acknowledgement')
        if not valid or not self.window.valid or self.state.disabled:
            return CpuDecision('hold', reason='invalid telemetry')
        if self.state.valid_seconds < 10:
            return CpuDecision('hold', reason='initial observation')
        if self.state.last_change is not None and self.window.end - self.state.last_change < 30:
            return CpuDecision('hold', reason='cooldown')
        if self.resource != 'cpu':
            return allocation_decision(action, tasks, self.foreground_task, self.resource)
        if action == 'lower':
            if any(t.cpu_pct is None for t in tasks) or sum(t.cpu_pct for t in tasks) < 20:
                return CpuDecision('hold', reason='background CPU below guard or unknown')
            eligible = [t for t in tasks if t.current_nice == t.original_nice and t.original_nice in (0, 10)]
            if eligible:
                target = min(eligible, key=lambda t: (('Low', 'Normal', 'High').index(t.importance), -t.cpu_pct, t.workload_id))
                return CpuDecision('lower', target.workload_id, {0: 10, 10: 19}[target.original_nice], 'guarded lower')
        elif action == 'restore':
            changed = [t for t in tasks if t.current_nice != t.original_nice]
            if changed:
                target = min(changed, key=lambda t: (('High', 'Normal', 'Low').index(t.importance),
                                                     -(t.intervention_order or 0), t.workload_id))
                return CpuDecision('restore', target.workload_id, target.original_nice, 'guarded restore')
        return CpuDecision('hold', reason='hold' if action == 'hold' else 'control limit reached')

    def output(self, decision):
        result = (CpuDecision('hold', reason='shadow: ' + decision.action)
                  if self.shadow and decision.action != 'restore_all' else decision)
        self.event('decision_result', decision=asdict(decision), result=asdict(result),
                   next_window_end=self.window.end + 5 if self.window else None,
                   call_duration_ms=self.latency_ms, guard_result=decision.reason,
                   source=self.requested if self.model_request_id else 'rule',
                   model_request_id=self.model_request_id)
        return result

    def observe(self, window, tasks, foreground, *, foreground_p95_ms=None, work=None):
        try:
            _number(window.start)
            _number(window.end)
            _number(window.miss_rate, high=1, unknown=True)
            _number(window.background_cpu, high=700, unknown=True)
            for task in tasks:
                _number(task.cpu_pct, high=100, unknown=True)
            if abs(window.end - window.start - 5) > .000001:
                raise ValueError('invalid window duration')
        except ValueError:
            self.event('model_rejection', reason='invalid_context')
            return CpuDecision('restore_all', reason='invalid measurements')
        if not self.active or (self.state.last_window_end is not None and window.end <= self.state.last_window_end):
            return None
        self.discard_late_response()
        if self.outstanding:
            self.fail('superseded', tasks, foreground)
        # Unknown task CPU invalidates Rule input; never coerce it to zero.
        valid = window.valid and (self.resource != 'cpu' or all(t.cpu_pct is not None for t in tasks))
        self.window = replace(window, valid=valid)
        self.model_request_id = None
        if self.resource == 'cpu':
            self.reference, self.state = evaluate_rule(self.window, tasks, self.state)
        else:
            self.reference, self.state = evaluate_resource_rule(self.window, tasks, self.state, self.foreground_task, self.resource)
        self.event('rule_reference', window=asdict(self.window), decision=asdict(self.reference),
                   tasks=[asdict(task) for task in tasks], foreground_p95_ms=foreground_p95_ms,
                   background_work=work or {})
        if self.reference.action == 'restore_all':
            return self.reference
        if self.effective == 'rule':
            return self.output(self.guarded(self.reference.action, tasks, foreground, valid))
        binding = RequestBinding(self.session, self.trial, self.generation,
                                 f'{window.start}:{window.end}', foreground,
                                 tuple((t.workload_id, t.identity) for t in tasks))
        available = tuple(action for action in actions_for(self.resource) if action == 'hold' or
                          self.guarded(action, tasks, foreground, valid).action == action)
        context = DecisionContext(binding, self.window, tuple(tasks), tuple(self.trend),
                                  foreground_p95_ms, work or {}, available, self.resource,
                                  self.foreground_task.current_allocation if self.foreground_task else None)
        request_id, dispatched = uuid.uuid4().hex, self.clock()
        self.outstanding = (request_id, context, dispatched)
        try:
            payload = context.public_payload()
            self.trend.append(payload['window'])
            self.event('decision_context', request_id=request_id, binding=asdict(binding), context=payload,
                       feature_version=FEATURE_VERSION, prompt_version=PROMPT_VERSION)
            if self.expired:
                return self.fail('helper_busy', tasks, foreground)
            if not self.helper or not self.helper.ready:
                return self.fail('helper_unavailable', tasks, foreground)
            self.helper.submit(request_id, payload)
            self.event('model_dispatched', request_id=request_id)
        except (ValueError, OSError, RuntimeError):
            return self.fail('dispatch_failed', tasks, foreground)
        return None

    def poll(self, tasks, foreground, *, telemetry_valid=True):
        if not self.active:
            return None
        if not self.outstanding:
            self.discard_late_response()
            return None
        request_id, context, dispatched = self.outstanding
        if self.clock() - dispatched >= DEADLINE_SECONDS:
            return self.fail('deadline', tasks, foreground, telemetry_valid)
        try:
            response = self.helper.poll()
        except (ValueError, OSError, RuntimeError):
            return self.fail('helper_failed', tasks, foreground, telemetry_valid)
        if response is None:
            return None
        if response.get('request_id') != request_id:
            self.event('model_rejection', reason='stale_request', request_id=request_id)
            return None
        if 'error' in response:
            error = response['error'] if response['error'] in (
                'authentication', 'rate_limit', 'unavailable', 'input_too_large', 'model_version') else 'provider_failed'
            return self.fail(error, tasks, foreground, telemetry_valid)
        try:
            proposal = (ModelProposal.parse(response.get('proposal')) if self.resource == 'cpu' else
                        ModelProposal.parse(response.get('proposal'), resource=self.resource))
        except ValueError:
            return self.fail('invalid_proposal', tasks, foreground, telemetry_valid)
        self.latency_ms = (self.clock() - dispatched) * 1000
        if self.latency_ms >= DEADLINE_SECONDS * 1000:
            return self.fail('deadline', tasks, foreground, telemetry_valid, response_consumed=True)
        if context.binding.generation != self.generation:
            return self.fail('stale_generation', tasks, foreground, telemetry_valid)
        decision = self.guarded(proposal.action, tasks, foreground, telemetry_valid)
        self.latency_ms = (self.clock() - dispatched) * 1000
        if self.latency_ms >= DEADLINE_SECONDS * 1000:
            return self.fail('deadline', tasks, foreground, telemetry_valid, response_consumed=True)
        self.outstanding = None
        self.failures = 0
        self.latest_proposal = proposal
        self.model_request_id = request_id
        self.fallback_reason = ''
        self.event('model_proposal', request_id=request_id, proposal=asdict(proposal),
                   latency_ms=self.latency_ms, guarded_decision=asdict(decision),
                   helper_cpu_seconds=response.get('helper_cpu_seconds'), helper_max_rss=response.get('helper_max_rss'))
        if decision.action != proposal.action:
            self.event('model_rejection', request_id=request_id, reason=decision.reason)
        return self.output(decision)

    def fail(self, reason, tasks, foreground, valid=True, *, response_consumed=False):
        request_id = self.outstanding[0] if self.outstanding else None
        if self.outstanding:
            self.latency_ms = (self.clock() - self.outstanding[2]) * 1000
        if reason == 'deadline' and not response_consumed:
            self.expired = self.outstanding
        self.outstanding = None
        self.model_request_id = None
        self.failures += 1
        self.fallback_reason = reason
        self.event('model_rejection', request_id=request_id, reason=reason, latency_ms=self.latency_ms)
        if self.failures >= 2 or reason == 'authentication':
            self.effective = 'rule'
        if self.helper and (self.effective == 'rule' or reason not in (
                'deadline', 'authentication', 'rate_limit', 'unavailable', 'provider_failed', 'invalid_proposal', 'model_version')):
            self.helper.cancel()
            self.expired = None
        self.event('policy_fallback', reason=reason, failures=self.failures,
                   latched=self.effective == 'rule', request_id=request_id)
        return self.output(self.guarded(self.reference.action, tasks, foreground, valid))

    def command_sent(self, request_id, decision):
        if self.command or self.shadow or decision.action not in ('lower', 'favor', 'restore'):
            raise ValueError('overlapping or unauthorized command')
        self.command = (request_id, decision, self.captures[decision.target].identity,
                        self.model_request_id, self.window.end)
        self.command_audit = {'cycle_id': self.cycle_id,
                              'source': self.requested if self.model_request_id else 'rule'}
        self.event('decision_command', request_id=request_id, decision=asdict(decision),
                   model_request_id=self.model_request_id, **self.command_audit)

    def acknowledge_allocation(self, request_id, changes, at):
        if not self.command or self.resource == 'cpu':
            return False
        expected, decision, _, model_request_id, window_end = self.command
        if request_id != expected or tuple(changes) != decision.changes or at < window_end:
            return False
        _number(at)
        if self.state.last_change is not None and at < self.state.last_change:
            return False
        self.command = None
        self.state = acknowledge(self.state, at)
        self.latest_applied = decision
        self.event('decision_acknowledgement', request_id=request_id, acknowledged_at=at,
                   decision=asdict(decision), model_request_id=model_request_id, **self.command_audit)
        return True

    def acknowledge(self, request_id, workload_id, identity, nice, at):
        if not self.command:
            return False
        expected, decision, captured_identity, model_request_id, command_window_end = self.command
        if (request_id, workload_id, identity, nice) != (expected, decision.target, captured_identity, decision.nice):
            return False
        _number(at)
        if at < command_window_end or (self.state.last_change is not None and at < self.state.last_change):
            return False
        self.command = None
        self.state = acknowledge(self.state, at)
        self.latest_applied = decision
        self.event('decision_acknowledgement', request_id=request_id, acknowledged_at=at,
                   decision=asdict(decision), model_request_id=model_request_id, **self.command_audit)
        return True
