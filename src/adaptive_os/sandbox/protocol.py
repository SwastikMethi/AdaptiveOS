"""Small, closed command surface shared by the desktop and Linux supervisor."""

import json
import math
import uuid

VERSION = 2
MAX_WORKLOADS = 8
PRIORITIES = {0: 'Normal', 10: 'Reduced', 19: 'Minimal'}
PRESETS = {'periodic': 'Periodic task', 'cpu': 'CPU hashing',
           'memory': 'Memory touch', 'io': 'File I/O'}


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Expected a canonical UUID')
    return value


def validate_config(preset, config):
    if not isinstance(preset, str) or preset not in PRESETS or not isinstance(config, dict):
        raise ValueError('Choose a built-in workload preset')
    allowed = {'periodic': {'budget_ms'}, 'memory': {'mib'}, 'cpu': set(), 'io': set()}[preset]
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
    return {}


def validate_command(command):
    if not isinstance(command, dict) or type(command.get('v')) is not int or command['v'] != VERSION:
        raise ValueError('Unsupported protocol version')
    identifier(command.get('request_id'))
    op = command.get('op')
    fields = {'heartbeat': set(), 'shutdown': set(), 'start': {'workload_id', 'preset', 'config'},
              'stop': {'workload_id'}, 'priority': {'workload_id', 'nice'}}
    if not isinstance(op, str) or op not in fields or set(command) != fields[op] | {'v', 'request_id', 'op'}:
        raise ValueError('Unsupported command or fields')
    if 'workload_id' in command:
        identifier(command['workload_id'])
    if op == 'start':
        command = {**command, 'config': validate_config(command['preset'], command['config'])}
    if op == 'priority' and (type(command['nice']) is not int or command['nice'] not in PRIORITIES):
        raise ValueError('Allowed priorities are 0, 10 and 19')
    return command


def encode(message):
    return json.dumps(message, allow_nan=False, separators=(',', ':')) + '\n'
