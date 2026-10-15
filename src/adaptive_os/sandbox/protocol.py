"""Small, closed command surface shared by the desktop and Linux supervisor."""

import json
import math
import uuid

VERSION = 5
MAX_WORKLOADS = 8
PRIORITIES = {0: 'Normal', 10: 'Reduced', 19: 'Minimal'}
PRESETS = {'periodic': 'Periodic task', 'cpu': 'CPU hashing',
           'memory': 'Memory touch', 'io': 'File I/O', 'cache': 'Resizable cache',
           'network': 'Loopback transfer'}
RESOURCES = ('cpu', 'memory', 'network')


def bounded_int(value, low, high, step=1):
    if type(value) is not int or not low <= value <= high or value % step:
        raise ValueError(f'Expected integer {low}–{high} in steps of {step}')
    return value


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Expected a canonical UUID')
    return value


def validate_config(preset, config):
    if not isinstance(preset, str) or preset not in PRESETS or not isinstance(config, dict):
        raise ValueError('Choose a built-in workload preset')
    allowed = {'periodic': {'budget_ms'}, 'memory': {'mib'}, 'cpu': set(), 'io': set(),
               'cache': {'mib', 'seed', 'working_set_mib', 'refill_ms'},
               'network': {'share', 'foreground', 'response_kib', 'period_ms', 'deadline_ms'}}[preset]
    if set(config) - allowed:
        raise ValueError('Unsupported workload setting')
    if preset == 'periodic':
        budget = config.get('budget_ms', 4.0)
        if type(budget) not in (float, int) or not math.isfinite(budget) or not 0 < budget < 20:
            raise ValueError('CPU budget must be finite and between 0 and 20 ms')
        return {'budget_ms': float(budget)}
    if preset == 'memory':
        mib = config.get('mib', 64)
        if type(mib) is not int or not 1 <= mib <= 128:
            raise ValueError('Memory allocation must be 1–128 MiB')
        return {'mib': mib}
    if preset == 'cache':
        refill = config.get('refill_ms', 2.0)
        if type(refill) not in (int, float) or not math.isfinite(refill) or not .1 <= refill <= 20:
            raise ValueError('Cache refill must be 0.1–20 ms')
        return {'mib': bounded_int(config.get('mib', 128), 16, 512, 16),
                'seed': bounded_int(config.get('seed', 20261008), 0, 2**31 - 1),
                'working_set_mib': bounded_int(config.get('working_set_mib', 144), 16, 1024, 16),
                'refill_ms': float(refill)}
    if preset == 'network':
        foreground = config.get('foreground', False)
        if type(foreground) is not bool:
            raise ValueError('Foreground must be explicit')
        return {'share': bounded_int(config.get('share', 2500), 500, 8000 if foreground else 9500),
                'foreground': foreground,
                'response_kib': bounded_int(config.get('response_kib', 64), 1, 256),
                'period_ms': bounded_int(config.get('period_ms', 100), 20, 1000),
                'deadline_ms': bounded_int(config.get('deadline_ms', 100), 20, 1000)}
    return {}


def validate_command(command):
    if not isinstance(command, dict) or type(command.get('v')) is not int or command['v'] != VERSION:
        raise ValueError('Unsupported protocol version')
    identifier(command.get('request_id'))
    op = command.get('op')
    fields = {'heartbeat': set(), 'shutdown': set(), 'preflight_complete': set(), 'preflight_abort': set(),
              'configure': {'resource', 'link_mbps'},
              'probe_start': {'workload_id', 'nice'}, 'probe_stop': {'workload_id', 'identity'},
              'start': {'workload_id', 'preset', 'config'}, 'stop': {'workload_id'},
              'priority': {'workload_id', 'identity', 'nice'},
              'memory_budget': {'foreground_id', 'changes'},
              'network_share': {'foreground_id', 'changes'}}
    if not isinstance(op, str) or op not in fields or set(command) != fields[op] | {'v', 'request_id', 'op'}:
        raise ValueError('Unsupported command or fields')
    if 'workload_id' in command:
        identifier(command['workload_id'])
    if op == 'configure':
        if command['resource'] not in RESOURCES:
            raise ValueError('Unknown resource')
        bounded_int(command['link_mbps'], 1, 100)
    if op == 'start':
        command = {**command, 'config': validate_config(command['preset'], command['config'])}
    if op in ('priority', 'probe_start') and (type(command['nice']) is not int or command['nice'] not in PRIORITIES):
        raise ValueError('Allowed priorities are 0, 10 and 19')
    if op == 'probe_start' and command['nice'] not in (0, 10):
        raise ValueError('Probe priorities are 0 or 10')
    if op in ('priority', 'probe_stop') and (type(command['identity']) is not int or command['identity'] <= 0):
        raise ValueError('Expected a positive process identity')
    if op in ('memory_budget', 'network_share'):
        identifier(command['foreground_id'])
        changes = command['changes']
        if not isinstance(changes, list) or len(changes) != 2:
            raise ValueError('A transfer needs exactly two allocations')
        low, high, step = (16, 512, 16) if op == 'memory_budget' else (500, 9500, 1)
        delta = 16 if op == 'memory_budget' else 500
        for change in changes:
            if not isinstance(change, dict) or set(change) != {'workload_id', 'identity', 'previous', 'requested'}:
                raise ValueError('Unsupported allocation fields')
            identifier(change['workload_id'])
            bounded_int(change['identity'], 1, 2**63 - 1)
            for field in ('previous', 'requested'):
                bounded_int(change[field], low, high, step)
            if abs(change['requested'] - change['previous']) != delta:
                raise ValueError('Allocation change exceeds one bounded step')
        ids = {c['workload_id'] for c in changes}
        if len(ids) != 2 or command['foreground_id'] not in ids:
            raise ValueError('Transfer must bind foreground and a distinct background')
        if sum(c['previous'] for c in changes) != sum(c['requested'] for c in changes):
            raise ValueError('Transfer must conserve capacity')
        if op == 'network_share' and next(c for c in changes if c['workload_id'] == command['foreground_id'])['requested'] > 8000:
            raise ValueError('Foreground share cannot exceed 80%')
    return command


def encode(message):
    return json.dumps(message, allow_nan=False, separators=(',', ':')) + '\n'
