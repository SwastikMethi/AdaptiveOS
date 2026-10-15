"""Durable ownership and recovery; all kernel writes follow committed intent."""

import copy
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .protocol import LEASE_SECONDS, PROTOCOL, service_fingerprint, validate
from .resources import (MEMORY_PARENT, NETWORK_LIMITS, SAFETY, STEP, counters, equal_shares, favor_memory, favor_network, high_for,
                        initial_lows, memory_profile, memory_values, pressured)


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(Path(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')

    def load(self):
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {'tasks': [], 'run': None}

    def save(self, state):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',
                            (json.dumps(state, allow_nan=False),))

    def claim(self, request_id):
        try:
            with self.db:
                self.db.execute('INSERT INTO requests VALUES (?)', (request_id,))
        except sqlite3.IntegrityError:
            raise ValueError('duplicate request_id') from None

    def metadata(self, name):
        row = self.db.execute('SELECT payload FROM metadata WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_metadata(self, name, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                            (name, json.dumps(value, allow_nan=False)))

    def event(self, kind, **fields):
        payload = {'kind': kind, 'backend': 'guest-linux', 'protocol': PROTOCOL,
                   'time_ns': time.time_ns(), **fields}
        with self.db:
            cursor = self.db.execute('INSERT INTO events(payload) VALUES (?)',
                                     (json.dumps(payload, allow_nan=False),))
        return cursor.lastrowid

    def events(self):
        return [{'journal_id': row[0], **json.loads(row[1])} for row in self.db.execute(
            'SELECT id,payload FROM (SELECT * FROM events ORDER BY id DESC LIMIT 100) ORDER BY id')]

    def memory_profiles(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM metadata WHERE name LIKE 'memory_profile:%' ORDER BY rowid DESC LIMIT 32")]

    def close(self):
        self.db.close()


class Engine:
    """One serialized resource writer, with a separate in-memory heartbeat lock."""

    def __init__(self, journal, host, clock=time.monotonic):
        self.journal, self.host, self.clock = journal, host, clock
        self.state = journal.load()
        self.generation = str(uuid.uuid4())
        self.service_fingerprint = service_fingerprint()
        self.lease_lock = threading.Lock()
        self.observation_lock = threading.Lock()
        self.cpu_observation = None
        self.resource_observation = None
        self.memory_fault = None
        self.lease_run, self.deadline = None, 0
        self.recovery_error = None
        try:
            if self.state['run']:
                self.restore('service_restart', end=True)
            self.reconcile()
        except (OSError, RuntimeError, ValueError) as exc:
            self.recovery_error = str(exc)
            try:
                self.journal.event('recovery_failed', error=self.recovery_error)
            except (OSError, sqlite3.Error):
                pass

    def heartbeat(self, run_id):
        with self.lease_lock:
            if run_id != self.lease_run or self.clock() >= self.deadline:
                raise ValueError('Controller run expired; reconcile and begin a fresh run')
            self.deadline = self.clock() + LEASE_SECONDS

    def current_run(self, run_id):
        run = self.state['run']
        with self.lease_lock:
            valid = self.lease_run == run_id and self.clock() < self.deadline
        if not run or run['id'] != run_id or not valid:
            raise ValueError('Inactive or expired controller run')
        return run

    def owned(self, task):
        observed = self.host.identity(task)
        if observed is not None and task.get('identity') and observed['identity'] != task['identity']:
            raise RuntimeError('Application identity changed; refusing resource writes or cleanup')
        return observed

    def reconcile(self):
        for task in list(self.state['tasks']):
            observed = self.owned(task)
            if observed is None:
                if self.state['run']:
                    self.restore('task_set_changed', end=True)
                self.journal.event('cleanup_intent', task_id=task['id'])
                self.host.cleanup(task)
                self.state['tasks'].remove(task)
                self.journal.save(self.state)
                self.journal.event('task_exited', task_id=task['id'])
            elif not task.get('identity'):
                task['identity'] = observed['identity']
                self.journal.save(self.state)
                self.journal.event('launch_reconciled', task=observed)

    def restore(self, reason, end=False):
        run = self.state['run']
        if not run:
            return []
        results, errors, journal_errors = [], [], []
        try:
            self.commit_observation_faults()
        except (OSError, sqlite3.Error) as exc:
            journal_errors.append(str(exc))
        # The complete baseline is durable before any write, including a torn write.
        try:
            self.journal.event('restore_intent', run_id=run['id'], reason=reason, baseline=run['baseline'])
        except (OSError, sqlite3.Error) as exc:
            journal_errors.append(str(exc))
        if run.get('resource') in ('memory', 'network'):
            try:
                target = run['resource_baseline'] if end else run['starting']
                self.restore_resource(run, target)
                results.append({'resource': run['resource'], 'status': 'restored', 'settings': target})
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append({'resource': run['resource'], 'error': str(exc)})
        for task_id, baseline in run['baseline'].items():
            try:
                task = next((t for t in self.state['tasks'] if t['id'] == task_id), None)
                if task is None:
                    results.append({'task_id': task_id, 'status': 'exited'})
                    continue
                if task.get('identity') != baseline['identity']:
                    raise RuntimeError('Captured application identity changed')
                observed = self.owned(task)
                if observed is None:
                    results.append({'task_id': task_id, 'status': 'exited'})
                    continue
                if observed['cpu_weight'] != baseline['cpu_weight']:
                    self.host.restore_weight(task, baseline['cpu_weight'])
                    observed = self.owned(task)
                if observed is None or observed['cpu_weight'] != baseline['cpu_weight']:
                    raise RuntimeError('Restore kernel read-back mismatch')
                results.append({'task_id': task_id, 'status': 'restored', 'cpu_weight': observed['cpu_weight']})
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append({'task_id': task_id, 'error': str(exc)})
        try:
            self.journal.event('restoration', run_id=run['id'], reason=reason, results=results, errors=errors)
        except (OSError, sqlite3.Error) as exc:
            journal_errors.append(str(exc))
        if errors or journal_errors:
            self.recovery_error = '; '.join([row['error'] for row in errors] +
                                           ['Recovery journal pending: ' + error for error in journal_errors])
            raise RuntimeError(self.recovery_error)
        if run.get('resource') == 'cpu':
            run['expected'] = {key: row['cpu_weight'] for key, row in run['baseline'].items()}
            run['last_change'] = self.clock()
            with self.observation_lock:
                self.cpu_observation = None
        elif run.get('resource') in ('memory', 'network'):
            run['expected'] = copy.deepcopy(run['starting'])
            run['last_change'] = self.clock()
            with self.observation_lock:
                self.resource_observation = None
        next_state = {**self.state, 'run': None} if end else self.state
        try:
            self.journal.save(next_state)
        except (OSError, sqlite3.Error) as exc:
            self.recovery_error = 'Recovery journal pending: ' + str(exc)
            raise RuntimeError(self.recovery_error) from exc
        self.state = next_state
        if end:
            with self.lease_lock:
                self.lease_run, self.deadline = None, 0
        self.recovery_error = None
        return results

    def tick(self):
        self.commit_observation_faults()
        with self.lease_lock:
            expired = self.clock() >= self.deadline
        if self.state['run'] and (expired or self.recovery_error):
            self.restore('controller_lost' if expired else 'recovery_retry', end=True)
        self.reconcile()
        run = self.state['run']
        if run and run.get('resource') and set(run['baseline']) != {t['id'] for t in self.state['tasks']}:
            self.restore('task_set_changed', end=True)
        if not self.state['run']:
            self.recovery_error = None

    def commit_observation_faults(self):
        """Called only by the serialized writer, before acknowledging failed evidence."""
        with self.observation_lock:
            fault = copy.deepcopy(self.memory_fault)
        run = self.state['run']
        if (not fault or not run or run.get('resource') != 'memory' or run['id'] != fault['run_id']
                or not run['profile'] or run['profile']['id'] != fault['profile_id']
                or fault['highs'] != {key: row['high'] for key, row in run['expected']['tasks'].items()}
                or run.get('headroom_fault') == fault):
            return
        profile = {**run['profile'], 'valid': False, 'reason': 'Observed growth/throttling requires recalibration'}
        self.journal.save_metadata('memory_profile:' + profile['id'], profile)
        run.update(scoring_valid=False, profile=profile, headroom_fault=fault)
        self.journal.save(self.state)
        self.journal.event('memory_profile_invalidated', run_id=run['id'], profile_id=profile['id'], evidence=fault)

    def snapshot_seed(self):
        run = self.state['run'] or {}
        return {'backend': 'guest-linux', 'protocol': PROTOCOL, 'generation': self.generation,
                'service_fingerprint': self.service_fingerprint,
                'capabilities': ['launch', 'cpu_weight_probe', 'cpu_weight_control', 'memory_control', 'network_control'], 'model_control': False,
                'recovery_error': self.recovery_error, 'run_id': (self.state['run'] or {}).get('id'),
                'cpu_control': ({key: dict(run[key]) if isinstance(run[key], dict) else run[key]
                                 for key in ('foreground_id', 'importance', 'expected', 'last_change', 'last_decision')}
                                if run.get('resource') == 'cpu' else None),
                'resource_control': (copy.deepcopy({key: run[key] for key in
                    ('resource', 'foreground_id', 'importance', 'expected', 'last_change', 'last_decision',
                     'profile', 'scored', 'scoring_valid', 'headroom_fault')}) if run.get('resource') in ('memory', 'network') else None),
                'memory_profiles': self.journal.memory_profiles(),
                'tasks': [dict(t) for t in self.state['tasks']], 'journal': self.journal.events()}

    def snapshot(self, seed=None):
        result = self.snapshot_seed() if seed is None else seed
        started = self.clock()
        result['observed_at_ns'] = time.monotonic_ns()
        try:
            result['preflight'] = self.host.preflight()
        except Exception as exc:
            result['preflight'] = {'passed': False, 'error': str(exc)}
        for index, task in enumerate(result['tasks']):
            try:
                observed = self.host.inspect(task)
                if observed is None or observed['identity'] != task.get('identity'):
                    raise RuntimeError('Task exited or identity changed during observation')
                result['tasks'][index] = observed
            except Exception as exc:
                result['tasks'][index] = {**task, 'inspection_error': str(exc)}
        if result['cpu_control'] is not None:
            self.observe_cpu(result, started)
        if result['resource_control'] is not None:
            try:
                if result['resource_control']['resource'] == 'memory':
                    result['memory_parent'] = self.host.memory_parent()
                else:
                    result['network_parent'] = self.host.network_telemetry(result['tasks'])
                    for task in result['tasks']:
                        task.setdefault('network', {}).update(result['network_parent']['tasks'].get(task['id'], {}), shaped=True)
                self.observe_resource(result, started)
            except (OSError, RuntimeError, ValueError, KeyError) as exc:
                result['resource_control'].update(ready=False, error=str(exc))
                with self.observation_lock:
                    self.resource_observation = None
        return result

    def prepare_memory(self, run, message, preflight):
        baseline = self.host.memory_configuration(self.state['tasks'])
        profile = None
        if message['profile_id']:
            profile = self.journal.metadata('memory_profile:' + message['profile_id'])
            if (not profile or not profile['suitable'] or not profile.get('valid')
                    or profile['baseline'] != preflight.get('baseline', {}).get('fingerprint')
                    or profile['identities'] != {t['id']: t['identity'] for t in self.state['tasks']}):
                raise ValueError('Memory profile is unsuitable, invalid or belongs to another VM/task set')
        highs = profile['highs'] if profile else {t['id']: 'max' for t in self.state['tasks']}
        lows = initial_lows(highs)
        target = {**baseline, 'parent': dict(MEMORY_PARENT),
                  'tasks': {key: {'low': lows[key], 'min': 0, 'high': value, 'max': 'max'} for key, value in highs.items()}}
        run.update(resource_baseline=baseline, expected=copy.deepcopy(target), starting=copy.deepcopy(target),
                   profile=profile, vm_baseline=preflight.get('baseline', {}).get('fingerprint'))

    def restore_resource(self, run, target):
        if run['resource'] == 'memory':
            errors = []
            for task in self.state['tasks']:
                try:
                    if task['id'] not in target['tasks'] or self.owned(task) is None:
                        continue
                    if task.get('identity') != run['baseline'][task['id']]['identity']:
                        raise RuntimeError('Memory restoration identity changed')
                    self.host.set_memory(task, target['tasks'][task['id']], restore=True)
                except (OSError, ValueError, RuntimeError) as exc:
                    errors.append(str(exc))
            try:
                self.host.set_memory(None, target['parent'], restore=True)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(str(exc))
            if errors:
                raise RuntimeError('; '.join(errors))
        else:
            self.host.set_network(self.state['tasks'], target, restore=True)

    def prepare_network(self, run):
        baseline = self.host.network_configuration(self.state['tasks'])
        if baseline != {'upload': None, 'download': None}:
            raise ValueError('Begin requires the captured unshaped owned transit devices')
        shares = equal_shares(run['baseline'])
        target = {direction: {**NETWORK_LIMITS, 'shares': dict(shares)} for direction in ('upload', 'download')}
        run.update(resource_baseline=baseline, expected=copy.deepcopy(target), starting=copy.deepcopy(target))

    def apply_resource(self, run, target, request_id, reason):
        intent = self.journal.event('resource_intent', request_id=request_id, run_id=run['id'],
                                    resource=run['resource'], before=run['expected'], after=target, reason=reason)
        try:
            self.current_run(run['id'])
            if run['resource'] == 'memory':
                self.host.set_memory(None, target['parent'])
                # Reduce donor protection before granting it; highs never fund a transfer.
                tasks = sorted(self.state['tasks'], key=lambda t: (
                    target['tasks'][t['id']]['low'] - self.host.memory_settings(t)['low'], t['id']))
                for task in tasks:
                    self.current_run(run['id'])
                    self.owned(task)
                    settings = target['tasks'][task['id']]
                    if self.host.memory_settings(task) != settings:
                        self.host.set_memory(task, settings)
                if self.host.memory_configuration(self.state['tasks']) != target:
                    raise RuntimeError('Memory kernel read-back mismatch')
            else:
                self.host.set_network(self.state['tasks'], target)
                if self.host.network_configuration(self.state['tasks']) != target:
                    raise RuntimeError('Network kernel read-back mismatch')
            self.current_run(run['id'])
            run['expected'] = copy.deepcopy(target)
            self.journal.save(self.state)
            self.journal.event('resource_ack', request_id=request_id, run_id=run['id'], intent=intent,
                               resource=run['resource'], settings=target, reason=reason)
            with self.observation_lock:
                self.resource_observation = None
        except Exception:
            self.restore('resource_failed', end=True)
            raise

    def observe_resource(self, snapshot, started):
        control, now = snapshot['resource_control'], self.clock()
        rows = {t['id']: t for t in snapshot['tasks']}
        expected = control['expected']
        with self.lease_lock:
            active = self.lease_run == snapshot['run_id'] and now < self.deadline
        valid = (active and now - started <= 3 and snapshot['preflight']['passed']
                 and not snapshot['recovery_error'] and set(rows) == set(control['importance'])
                 and all(not row.get('inspection_error') and row.get('cpus_effective') == '1-3'
                         and row.get('cpu_weight') == 100 for row in rows.values()))
        clean, needs_headroom, throttled = False, False, set()
        if valid and control['resource'] == 'network':
            valid = (snapshot['network_parent']['settings'] == expected and
                     all(type(snapshot['network_parent']['tasks'][key].get(direction + '_bytes')) is int
                         for key in rows for direction in ('upload', 'download')))
        if valid and control['resource'] == 'memory':
            parent = snapshot['memory_parent']
            valid = (all(parent[k] == v for k, v in expected['parent'].items())
                     and all(memory_values(row) == expected['tasks'][key]
                             and type(row['memory_current']) is int and row['memory_current'] >= 0 for key, row in rows.items()))
            clean = (valid and not pressured(parent['pressure'])
                     and not pressured(parent.get('host_pressure', parent['pressure']))
                     and all(row['memory_high'] == 'max' and not pressured(row['memory_pressure']) for row in rows.values()))
            needs_headroom = any(value['high'] != 'max' and high_for(rows[key]['memory_current']) > value['high']
                                 for key, value in expected['tasks'].items())
        with self.observation_lock:
            previous, seconds, profile_seconds = self.resource_observation, 0., 0.
            peaks = {key: row.get('memory_current', 0) for key, row in rows.items()}
            if valid and control['resource'] == 'memory':
                fault = self.memory_fault if self.memory_fault and self.memory_fault['run_id'] == snapshot['run_id'] else control.get('headroom_fault')
                if fault and fault['highs'] == {key: row['high'] for key, row in expected['tasks'].items()}:
                    needs_headroom = True
                    throttled.update(fault['throttled'])
                if previous and previous['run_id'] == snapshot['run_id']:
                    throttled.update(key for key, row in rows.items() if key in previous['rows']
                        and row['identity'] == previous['rows'][key]['identity'] and expected['tasks'][key]['high'] != 'max'
                        and counters(row['memory_events']).get('high', 0) > counters(previous['rows'][key]['memory_events']).get('high', 0))
                needs_headroom |= bool(throttled)
            if valid and previous and previous['run_id'] == snapshot['run_id']:
                elapsed = started - previous['at']
                continuous = (0 <= elapsed <= 3 and set(rows) == set(previous['rows'])
                              and all(row['identity'] == previous['rows'][key]['identity'] for key, row in rows.items()))
                if continuous:
                    seconds = previous['valid_seconds'] + elapsed
                    if control['resource'] == 'memory':
                        events_stable = all(counters(row['memory_events']).get(event, 0) ==
                                            counters(previous['rows'][key]['memory_events']).get(event, 0)
                                            for key, row in rows.items() for event in ('high', 'max', 'oom', 'oom_kill'))
                        parent_stable = all(counters(snapshot['memory_parent']['events']).get(event, 0) ==
                                            counters(previous['parent']['events']).get(event, 0)
                                            for event in ('high', 'max', 'oom', 'oom_kill'))
                        if clean and previous['clean'] and events_stable and parent_stable:
                            profile_seconds = previous['profile_seconds'] + elapsed
                            peaks = {key: max(value, previous['peaks'][key]) for key, value in peaks.items()}
                        throttled |= set(previous.get('throttled', [])) | {
                            key for key, row in rows.items() if expected['tasks'][key]['high'] != 'max' and
                            counters(row['memory_events']).get('high', 0) > counters(previous['rows'][key]['memory_events']).get('high', 0)}
                        needs_headroom |= previous['headroom_required'] or bool(throttled)
            if valid and control['resource'] == 'memory' and needs_headroom and control['profile']:
                self.memory_fault = {'run_id': snapshot['run_id'], 'profile_id': control['profile']['id'],
                    'highs': {key: row['high'] for key, row in expected['tasks'].items()}, 'throttled': sorted(throttled)}
            observation_id = str(uuid.uuid4())
            self.resource_observation = ({'id': observation_id, 'run_id': snapshot['run_id'], 'at': started,
                'valid_seconds': seconds, 'rows': copy.deepcopy(rows), 'used': False, 'clean': clean,
                'profile_seconds': profile_seconds, 'peaks': peaks, 'headroom_required': needs_headroom,
                'throttled': sorted(throttled),
                'parent': snapshot.get('memory_parent')} if valid else None)
        cooldown = max(0., 30 - (now - control['last_change'])) if control['last_change'] is not None else 0.
        spacing = max(0., 5 - (now - control['last_decision'])) if control['last_decision'] is not None else 0.
        control.update(observation_id=observation_id, valid_seconds=seconds, profile_seconds=profile_seconds,
                       cooldown_seconds=cooldown, decision_wait_seconds=spacing, headroom_required=needs_headroom,
                       scoring_valid=control['scoring_valid'] and not needs_headroom,
                       ready=bool(valid and seconds >= 10 and not cooldown and not spacing and not needs_headroom
                                  and (control['resource'] != 'memory' or control['profile'] and control['profile']['suitable'])))

    def resource_observed(self, message, run, *, profiling=False):
        if message['generation'] != self.generation:
            raise ValueError('Stale service generation')
        with self.observation_lock:
            observation = self.resource_observation
            if (not observation or observation['run_id'] != run['id'] or observation['used']
                    or observation['id'] != message['observation_id'] or self.clock() - observation['at'] > 3
                    or not profiling and observation['valid_seconds'] < 10):
                raise ValueError('Fresh resource observation required')
            observation['used'] = True
            observation = copy.deepcopy(observation)
        for task in self.state['tasks']:
            observed = self.owned(task)
            if (not observed or observed['identity'] != observation['rows'][task['id']]['identity']
                    or observed['cpus_effective'] != '1-3' or observed['cpu_weight'] != 100):
                raise ValueError('Stale resource identity or placement')
        if run['resource'] == 'memory' and self.host.memory_configuration(self.state['tasks']) != run['expected']:
            raise ValueError('Memory settings changed after observation')
        if run['resource'] == 'network' and self.host.network_configuration(self.state['tasks']) != run['expected']:
            raise ValueError('Network settings changed after observation')
        return observation

    def profile_memory(self, message, run):
        if run.get('resource') != 'memory':
            raise ValueError('Begin a Memory run before profiling')
        observation = self.resource_observed(message, run, profiling=True)
        target = copy.deepcopy(run['expected'])
        if message['action'] == 'establish':
            if run['scored'] or observation['profile_seconds'] < 30 or run['profile']:
                raise ValueError('Memory profiling requires 30 valid pressure-free seconds with high=max in an unscored run')
            profile = memory_profile(observation['peaks'])
            profile.update(id=str(uuid.uuid4()), valid=profile['suitable'], baseline=run['vm_baseline'],
                           identities={t['id']: t['identity'] for t in self.state['tasks']},
                           observed_seconds=observation['profile_seconds'])
            if profile['suitable']:
                lows = initial_lows(profile['highs'])
                for key, high in profile['highs'].items():
                    target['tasks'][key].update(high=high, low=lows[key])
        else:
            if not run['profile'] or not observation['headroom_required']:
                raise ValueError('No profiled headroom adjustment required')
            profile = copy.deepcopy(run['profile'])
            profile.update(valid=False, reason='Observed growth/throttling requires recalibration')
            run['scoring_valid'] = False
            highs = {key: max(int(row['high']) + (STEP if key in observation['throttled'] else 0),
                             high_for(observation['rows'][key]['memory_current']))
                     for key, row in target['tasks'].items() if row['high'] != 'max'}
            suitable = len(highs) == len(target['tasks']) and sum(highs.values()) <= SAFETY
            for key, high in highs.items():
                target['tasks'][key]['high'] = high if suitable else 'max'
            profile.update(suitable=suitable, highs={key: row['high'] for key, row in target['tasks'].items()})
        self.journal.save_metadata('memory_profile:' + profile['id'], {**profile, 'valid': False})
        self.apply_resource(run, target, message['request_id'], 'memory_' + message['action'])
        self.journal.save_metadata('memory_profile:' + profile['id'], profile)
        run['profile'] = profile
        if message['action'] == 'establish' and profile['suitable']:
            run['starting'] = copy.deepcopy(target)
        elif message['action'] == 'grow':
            # Policy disable restores protection, but cannot undo a safety headroom increase.
            for key, row in target['tasks'].items():
                run['starting']['tasks'][key]['high'] = row['high']
            run['headroom_fault'] = None
            with self.observation_lock:
                self.memory_fault = None
        self.journal.save(self.state)
        return {'profile': profile, 'settings': target, 'scoring_valid': run['scoring_valid']}

    def decide_resource(self, message, run):
        if run.get('resource') == 'cpu':
            return self.decide_cpu(message, run)
        if run.get('resource') not in ('memory', 'network'):
            raise ValueError('Begin a resource run before decisions')
        observation = self.resource_observed(message, run)
        now = self.clock()
        if run['last_decision'] is not None and now - run['last_decision'] < 5:
            raise ValueError('Resource decisions must be five seconds apart')
        if message['action'] == 'favor' and run['last_change'] is not None and now - run['last_change'] < 30:
            raise ValueError('Resource intervention cooldown is thirty seconds')
        target = copy.deepcopy(run['expected'])
        if message['action'] == 'favor' and run['resource'] == 'memory':
            if not run['profile'] or not run['profile']['suitable']:
                raise ValueError('Memory requires a suitable headroom profile')
            if observation['headroom_required']:
                raise ValueError('Resolve memory high headroom before protection redistribution')
            target['tasks'] = favor_memory(target['tasks'], run['foreground_id'], run['importance'])
        if message['action'] == 'favor' and run['resource'] == 'network':
            for direction in ('upload', 'download'):
                target[direction]['shares'] = favor_network(target[direction]['shares'], run['foreground_id'], run['importance'])
        run['last_decision'] = now
        self.journal.save(self.state)
        changed = target != run['expected']
        if changed:
            self.apply_resource(run, target, message['request_id'], 'favor')
            run['last_change'] = now
            self.journal.save(self.state)
        result = {'action': 'favor' if changed else 'hold', 'settings': target}
        self.journal.event('resource_decision', request_id=message['request_id'], run_id=run['id'], **result)
        return result

    def observe_cpu(self, snapshot, started):
        """Bind complete telemetry outside the writer; no resource or journal writes."""
        control, now = snapshot['cpu_control'], self.clock()
        rows = {t['id']: {key: t.get(key) for key in ('identity', 'cpu_weight', 'cpus_effective', 'cpu_usage_usec')}
                for t in snapshot['tasks']}
        with self.lease_lock:
            active = self.lease_run == snapshot['run_id'] and now < self.deadline
        valid = (active and now - started <= 3 and snapshot['preflight']['passed']
                 and not snapshot['recovery_error'] and set(rows) == set(control['expected'])
                 and all(not t.get('inspection_error') and t.get('identity')
                         and t.get('cpus_effective') == '1-3'
                         and t.get('cpu_weight') == control['expected'].get(t['id'])
                         and type(t.get('cpu_usage_usec')) is int and t['cpu_usage_usec'] >= 0
                         for t in snapshot['tasks']))
        with self.observation_lock:
            previous = self.cpu_observation
            seconds = 0.
            if valid and previous and previous['run_id'] == snapshot['run_id']:
                elapsed = started - previous['at']
                continuous = (0 <= elapsed <= 3 and rows.keys() == previous['rows'].keys()
                              and all(row['identity'] == previous['rows'][key]['identity']
                                      and row['cpu_weight'] == previous['rows'][key]['cpu_weight']
                                      and row['cpu_usage_usec'] >= previous['rows'][key]['cpu_usage_usec']
                                      for key, row in rows.items()))
                if continuous:
                    seconds = previous['valid_seconds'] + elapsed
            observation_id = str(uuid.uuid4())
            self.cpu_observation = ({'id': observation_id, 'run_id': snapshot['run_id'], 'at': started,
                                     'valid_seconds': seconds, 'rows': rows, 'used': False} if valid else None)
        cooldown = max(0., 30 - (now - control['last_change'])) if control['last_change'] is not None else 0.
        spacing = max(0., 5 - (now - control['last_decision'])) if control['last_decision'] is not None else 0.
        control.update(observation_id=observation_id, valid_seconds=seconds, cooldown_seconds=cooldown,
                       decision_wait_seconds=spacing, ready=bool(valid and seconds >= 10 and not cooldown and not spacing))

    def decide_cpu(self, message, run):
        if run.get('resource') != 'cpu':
            raise ValueError('Begin a CPU run before making CPU decisions')
        if message['generation'] != self.generation:
            raise ValueError('Stale service generation')
        now = self.clock()
        with self.observation_lock:
            observation = self.cpu_observation
            if (not observation or observation['id'] != message['observation_id'] or observation['used']
                    or observation['run_id'] != run['id'] or now - observation['at'] > 3
                    or observation['valid_seconds'] < 10):
                raise ValueError('Fresh observation with ten valid seconds required')
            if message['action'] == 'favor' and run['last_change'] is not None and now - run['last_change'] < 30:
                raise ValueError('CPU intervention cooldown is thirty seconds')
            if run['last_decision'] is not None and now - run['last_decision'] < 5:
                raise ValueError('CPU decisions must be five seconds apart')
            observation['used'] = True
        observed = []
        for task in self.state['tasks']:
            actual = self.owned(task)
            if not actual or actual['identity'] != observation['rows'].get(task['id'], {}).get('identity'):
                raise ValueError('Stale application identity observation')
            if actual.get('cpus_effective') != '1-3':
                raise ValueError('Task CPU placement changed')
            if actual['cpu_weight'] != observation['rows'][task['id']]['cpu_weight']:
                raise ValueError('Stale weight observation')
            observed.append(actual)
        run['last_decision'] = now
        self.journal.save(self.state)
        eligible = [t for t in observed if t['id'] != run['foreground_id'] and t['cpu_weight'] in (100, 50)]
        if message['action'] == 'hold' or not eligible:
            reason = 'requested hold' if message['action'] == 'hold' else 'control limit reached'
            self.journal.event('cpu_decision', request_id=message['request_id'], run_id=run['id'], action='hold', reason=reason)
            return {'action': 'hold', 'reason': reason}
        target = min(eligible, key=lambda t: (('Low', 'Normal', 'High').index(run['importance'][t['id']]),
                                               -t['cpu_weight'], t['id']))
        return {'action': 'favor', **self.write_weight(target, run, message['request_id'], target['cpu_weight'],
                                                       {100: 50, 50: 25}[target['cpu_weight']])}

    def write_weight(self, task, run, request_id, before, after):
        intent = self.journal.event('weight_intent', request_id=request_id, run_id=run['id'],
                                    task_id=task['id'], identity=task['identity'], before=before, after=after)
        try:
            self.current_run(run['id'])
            self.host.set_weight(task, after)
            actual = self.owned(task)
            if not actual or actual['cpu_weight'] != after:
                raise RuntimeError('CPU kernel read-back mismatch')
            if run.get('resource') == 'cpu':
                if actual.get('cpus_effective') != '1-3':
                    raise RuntimeError('CPU placement changed during write')
                run['expected'][task['id']] = after
                run['last_change'] = self.clock()
                self.journal.save(self.state)
            self.current_run(run['id'])
            self.journal.event('weight_ack', intent=intent, request_id=request_id,
                               task_id=task['id'], identity=task['identity'], weight=after)
        except Exception:
            self.restore('weight_failed', end=True)
            raise
        return {'task_id': task['id'], 'identity': task['identity'], 'weight': after}

    def handle(self, message):
        try:
            return self._handle(message)
        except (sqlite3.Error, OSError) as exc:
            self.recovery_error = 'journal or resource operation failed: ' + str(exc)
            if self.state['run']:
                self.restore('operation_failed', end=True)
            raise

    def _handle(self, message):
        message = validate(message)
        op = message['op']
        if op == 'heartbeat':
            self.heartbeat(message['run_id'])
            return {'run_id': message['run_id']}
        try:
            self.tick()
        except (OSError, RuntimeError, ValueError) as exc:
            self.recovery_error = str(exc)
            if op != 'snapshot':
                raise
        if op == 'snapshot':
            result = self.snapshot()
            self.commit_observation_faults()
            return result
        self.journal.claim(message['request_id'])
        if self.recovery_error:
            raise RuntimeError('Recovery incomplete: ' + self.recovery_error)
        if op == 'launch':
            if self.state['run']:
                raise ValueError('End the controller run before adding applications')
            if len(self.state['tasks']) >= 8:
                raise ValueError('At most eight managed applications')
            used = {t['slot'] for t in self.state['tasks']}
            task = {'id': str(uuid.uuid4()), 'app': message['app'],
                    'slot': next(i for i in range(1, 9) if i not in used)}
            self.state['tasks'].append(task)
            self.journal.save(self.state)
            self.journal.event('launch_intent', task=task)
            observed = self.host.launch(task)
            if not observed:
                raise RuntimeError('Application exited before registration; inspect guest service logs')
            task['identity'] = observed['identity']
            self.journal.save(self.state)
            self.journal.event('launched', task=observed)
            return {'task': observed}
        if op in ('begin', 'begin_cpu', 'begin_control'):
            if self.state['run'] or not self.state['tasks']:
                raise ValueError('Begin requires managed applications and no active run')
            preflight = self.host.preflight()
            if not preflight['passed']:
                raise ValueError('Guest preflight failed')
            configured = {row['task_id']: row for row in message.get('tasks', [])}
            if op != 'begin':
                if message['generation'] != self.generation:
                    raise ValueError('Stale service generation')
                if set(configured) != {t['id'] for t in self.state['tasks']}:
                    raise ValueError('CPU configuration must include every registered task')
            baseline = {}
            for task in self.state['tasks']:
                observed = self.owned(task)
                if not observed or observed['cpu_weight'] != 100:
                    raise ValueError('Begin requires every task at weight 100')
                if op != 'begin':
                    if configured[task['id']]['identity'] != observed['identity']:
                        raise ValueError('Stale application identity')
                    if observed.get('cpus_effective') != '1-3':
                        raise ValueError('Task CPU placement must be 1-3')
                baseline[task['id']] = {'identity': observed['identity'], 'cpu_weight': observed['cpu_weight']}
            run_id = str(uuid.uuid4())
            self.state['run'] = {'id': run_id, 'baseline': baseline}
            if op != 'begin':
                resource = message.get('resource', 'cpu')
                self.state['run'].update(resource=resource, foreground_id=message['foreground_id'],
                                         importance={key: row['importance'] for key, row in configured.items()},
                                         expected={key: row['cpu_weight'] for key, row in baseline.items()},
                                         last_change=None, last_decision=None, profile=None,
                                         scored=message.get('scored', False), scoring_valid=True, headroom_fault=None)
                with self.observation_lock:
                    self.cpu_observation = None
                    self.resource_observation = None
                    self.memory_fault = None
                try:
                    if resource == 'memory':
                        self.prepare_memory(self.state['run'], message, preflight)
                    elif resource == 'network':
                        self.prepare_network(self.state['run'])
                except Exception:
                    self.state['run'] = None
                    raise
            self.journal.save(self.state)
            with self.lease_lock:
                self.lease_run, self.deadline = run_id, self.clock() + LEASE_SECONDS
            self.journal.event('run_started', run_id=run_id, baseline=baseline, configuration=self.state['run'])
            if self.state['run'].get('resource') in ('memory', 'network'):
                self.apply_resource(self.state['run'], self.state['run']['starting'], message['request_id'], 'begin')
            return {'run_id': run_id}
        run = self.current_run(message['run_id'])
        if op in ('restore', 'end'):
            return {'restoration': self.restore(op, end=op == 'end')}
        if op == 'cpu_decide':
            return self.decide_cpu(message, run)
        if op == 'resource_decide':
            return self.decide_resource(message, run)
        if op == 'memory_profile':
            return self.profile_memory(message, run)
        if run.get('resource'):
            raise ValueError('The feasibility probe is unavailable during a resource run')
        task = next((t for t in self.state['tasks'] if t['id'] == message['task_id']), None)
        if not task or task.get('identity') != message['identity'] or task['id'] not in run['baseline']:
            raise ValueError('Unknown or stale task identity')
        observed = self.owned(task)
        if not observed or observed['cpu_weight'] != message['expected_weight']:
            raise ValueError('stale weight observation')
        return self.write_weight(task, run, message['request_id'], 100, 90)
