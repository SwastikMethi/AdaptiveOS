"""Versioned allowlist for the privileged guest boundary."""

import json
import hashlib
from pathlib import Path
import sys
import uuid

PROTOCOL = 'guest-linux/1'
SOCKET = '/run/adaptive-os/guest.sock'
MAX_MESSAGE = 65536
MAX_RESPONSE = 4 * 1024 * 1024
LEASE_SECONDS = 10


def service_fingerprint():
    root = Path(__file__).parent
    sources = {name: hashlib.sha256((root / (name + '.py')).read_bytes()).hexdigest()
               for name in ('__init__', 'protocol', 'core', 'linux', 'resources', 'network', 'service')}
    return hashlib.sha256(json.dumps({'sources': sources, 'python': sys.version}, sort_keys=True).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str):
        raise ValueError('Expected a UUID')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('Expected a canonical UUID') from None
    return value


def validate(message):
    if not isinstance(message, dict) or message.get('protocol') != PROTOCOL:
        raise ValueError('Unsupported guest protocol')
    identifier(message.get('request_id'))
    op = message.get('op')
    fields = {'snapshot': set(), 'launch': {'app'}, 'begin': set(),
              'begin_cpu': {'generation', 'foreground_id', 'tasks'},
              'begin_control': {'resource', 'generation', 'foreground_id', 'tasks', 'profile_id', 'scored'},
              'cpu_decide': {'run_id', 'generation', 'observation_id', 'action'},
              'resource_decide': {'run_id', 'generation', 'observation_id', 'action'},
              'memory_profile': {'run_id', 'generation', 'observation_id', 'action'},
              'heartbeat': {'run_id'}, 'restore': {'run_id'}, 'end': {'run_id'},
              'probe': {'run_id', 'task_id', 'identity', 'expected_weight'}}
    if not isinstance(op, str) or op not in fields:
        raise ValueError('Unsupported guest operation')
    if set(message) != {'protocol', 'request_id', 'op'} | fields[op]:
        raise ValueError('Unexpected or missing command fields')
    if op == 'launch' and message['app'] not in ('firefox', 'geany'):
        raise ValueError('Only Firefox and Geany are registered applications')
    if 'run_id' in message:
        identifier(message['run_id'])
    if op in ('begin_cpu', 'begin_control', 'cpu_decide', 'resource_decide', 'memory_profile'):
        identifier(message['generation'])
    if op == 'begin_control':
        if message['resource'] not in ('cpu', 'memory', 'network') or type(message['scored']) is not bool:
            raise ValueError('Invalid guest resource configuration')
        if message['profile_id'] is not None:
            identifier(message['profile_id'])
            if message['resource'] != 'memory':
                raise ValueError('Only Memory uses a headroom profile')
        if message['scored'] and message['resource'] == 'memory' and not message['profile_id']:
            raise ValueError('Scored Memory requires a calibrated profile')
    if op in ('begin_cpu', 'begin_control'):
        identifier(message['foreground_id'])
        tasks = message['tasks']
        if not isinstance(tasks, list) or not 2 <= len(tasks) <= 8:
            raise ValueError('CPU control requires two to eight registered tasks')
        ids = set()
        for task in tasks:
            if not isinstance(task, dict) or set(task) != {'task_id', 'identity', 'importance'}:
                raise ValueError('Invalid CPU task configuration')
            identifier(task['task_id'])
            if task['task_id'] in ids:
                raise ValueError('Duplicate CPU task identity')
            ids.add(task['task_id'])
            if not isinstance(task['identity'], str) or not 1 <= len(task['identity']) <= 128:
                raise ValueError('Invalid application identity')
            if task['importance'] not in ('Low', 'Normal', 'High'):
                raise ValueError('Invalid task importance')
        if message['foreground_id'] not in ids:
            raise ValueError('Foreground must be a registered task')
    if op in ('cpu_decide', 'resource_decide', 'memory_profile'):
        identifier(message['observation_id'])
        allowed = ('establish', 'grow') if op == 'memory_profile' else ('hold', 'favor')
        if message['action'] not in allowed:
            raise ValueError('Invalid bounded resource action; use restore for recovery')
    if op == 'probe':
        identifier(message['task_id'])
        if not isinstance(message['identity'], str) or not 1 <= len(message['identity']) <= 128:
            raise ValueError('Invalid application identity')
        if type(message['expected_weight']) is not int or message['expected_weight'] != 100:
            raise ValueError('Milestone 1 permits only the 100 to 90 probe')
    return message


def encode(message, limit=MAX_MESSAGE):
    data = (json.dumps(message, allow_nan=False, separators=(',', ':')) + '\n').encode()
    if len(data) > limit:
        raise ValueError('Guest message too large')
    return data
