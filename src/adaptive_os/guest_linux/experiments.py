"""Guest-only paired comparisons. Functional probes never count as scored trials."""

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import random
import resource as usage
import statistics
import subprocess
import sys
import threading
import time
import uuid

from ..session_store import SessionStore
from .client import request
from .policy import GuestPolicy, control_of, pressure
from .readiness import GuestReadiness, fingerprint, qualified, save_report, validate_report
from .resources import MEMORY_PARENT, counters, memory_values


def schedule(seed, *, model=False):
    rng, rows = random.Random(seed), []
    for block in range(5):
        profiles = ['light', 'contention']
        rng.shuffle(profiles)
        for profile in profiles:
            arms = ['default', 'manual', 'rule'] + (['jev'] if model else [])
            rng.shuffle(arms)
            rows.extend({'block': block, 'profile': profile, 'arm': arm, 'warmup_seconds': 30,
                         'measurement_seconds': 180} for arm in arms)
    return rows


def percentile(values, q=.95):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def network_checks(phases):
    return {'both_directions': {p['direction'] for p in phases} == {'upload', 'download'},
            'steady_windows': bool(phases) and all(p['seconds'] >= 30 for p in phases),
            'shared_parent_rate': all(19 <= p['parent_mbps'] <= 21 for p in phases if not p['borrower']),
            'child_guarantees': all(abs(p['shares'].get(key, 0) - share) <= .1 * share
                                    for p in phases if not p['borrower'] for key, share in p['expected_shares'].items()),
            'idle_borrowing': {p['direction'] for p in phases if p['borrower']} == {'upload', 'download'} and
                              all(18 <= p['parent_mbps'] <= 21 for p in phases if p['borrower'])}


def comparison_report(trials):
    report = {'trials': trials, 'invalid_trials': [t for t in trials if not t.get('valid')], 'profiles': {}, 'ai_useful': False}
    gates = []
    for profile in ('light', 'contention'):
        arms = {}
        for arm in ('manual', 'rule', 'jev'):
            pairs = []
            for trial in trials:
                if not trial.get('valid') or trial['profile'] != profile or trial['arm'] != arm:
                    continue
                default = next((t for t in trials if t.get('valid') and t['profile'] == profile and
                                t['block'] == trial['block'] and t['arm'] == 'default'), None)
                if not default or not default['foreground_p95_ms']:
                    continue
                costs = {key: 1 - trial['background_throughput'].get(key, 0) / value
                         for key, value in default['background_throughput'].items() if value > 0}
                pairs.append({'block': trial['block'], 'foreground_improvement':
                              1 - trial['foreground_p95_ms'] / default['foreground_p95_ms'], 'background_loss': costs})
            arms[arm] = {'paired_blocks': len(pairs), 'pairs': pairs,
                         'foreground_improvement': statistics.mean(p['foreground_improvement'] for p in pairs) if pairs else None,
                         'worst_background_loss': max((v for p in pairs for v in p['background_loss'].values()), default=None)}
        report['profiles'][profile] = arms
        ai, rule = arms['jev'], arms['rule']
        gates.append(ai['paired_blocks'] == rule['paired_blocks'] == 5 and ai['foreground_improvement'] >= .2
                     and ai['worst_background_loss'] is not None and ai['worst_background_loss'] <= .4
                     and ai['foreground_improvement'] > rule['foreground_improvement'])
    report['ai_useful'] = all(gates) and not report['invalid_trials']
    return report


class Fixture:
    def __init__(self, task, resource, role, profile, directory, config, *, direction='both'):
        self.task, self.rows, self.events = task, [], queue.Queue()
        command = ['sudo', '-n', '/usr/bin/python3', str(Path(__file__).with_name('fixtures.py')),
                   '--task', task['id'], '--identity', task['identity'], '--resource', resource, '--role', role,
                   '--profile', profile, '--seconds', '900', '--directory', str(directory.parent / 'fixture-data'),
                   '--iterations', str(config.get('iterations', 100000)), '--memory-mib',
                   str(config.get('memory_mib', {}).get(task['id'], 1024)), '--endpoint', config['endpoint'],
                   '--direction', direction]
        self.errors = (directory / (task['id'] + '.fixture.log')).open('w')
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, text=True, bufsize=1)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        try:
            first = self.events.get(timeout=120)
        except queue.Empty:
            self.close()
            raise RuntimeError('Fixture startup timed out') from None
        if first.get('kind') != 'fixture_ready':
            self.close()
            raise RuntimeError('Fixture startup failed: ' + str(first))
        self.ready = first
        if not first['cgroup'].endswith(task['cgroup']) or first['cpus'] != [1, 2, 3]:
            self.close()
            raise RuntimeError('Fixture placement differs from its owned application')

    def read(self):
        try:
            for line in self.process.stdout:
                event = json.loads(line)
                self.events.put(event)
        except Exception as exc:
            self.events.put({'kind': 'fixture_error', 'error': str(exc)})

    def drain(self):
        added = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.get('kind') == 'fixture_error':
                raise RuntimeError(event['error'])
            for row in event.get('rows', []):
                if 'error' in row:
                    raise RuntimeError(row['error'])
                self.rows.append(row)
                added.append(row)
        if self.process.poll() is not None:
            raise RuntimeError('Workload fixture exited before measurement finished')
        return added

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=30)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.errors.close()


class GuestRun:
    def __init__(self, directory, resource, *, transport=request, key='', readiness_directory=None):
        self.directory, self.resource, self.transport = Path(directory), resource, transport
        self.directory.mkdir(parents=True, exist_ok=False)
        self.store = SessionStore(self.directory / 'recording.sqlite3')
        self.session = str(uuid.uuid4())
        self.store.begin(self.session, {'backend': 'guest-linux', 'resource': resource, 'measurement': 'owned synthetic probes'})
        self.run_id, self.stop, self.pulse, self.fixtures = None, threading.Event(), None, []
        self.before, self.latest = None, None
        self.policy = GuestPolicy(readiness_directory or self.directory.parent / 'readiness', key=key)
        self.error = None
        self.pressure_process, self.pressure_unit = None, None

    def audit(self, **event):
        self.store.record(self.session, {'backend': 'guest-linux', **event}, time.monotonic_ns())
        self.store.flush()

    def command(self, op, **fields):
        if op in ('resource_decide', 'memory_profile', 'restore', 'end'):
            fields['run_id'] = self.run_id
        self.audit(kind='guest_command', op=op, **fields)
        result = self.transport(op, **fields)
        self.audit(kind='guest_ack', op=op, **result)
        if op == 'end':
            self.run_id = None
        if self.latest:
            control_of(self.latest)['ready'] = False
        return result

    def begin(self, profile_id=None, *, scored=False):
        self.before = self.transport('snapshot')
        if not self.before['preflight']['passed'] or self.before['run_id'] or self.before['recovery_error']:
            raise ValueError('Healthy guest without an active run required')
        tasks = self.before['tasks']
        if len(tasks) != 2 or {t['app'] for t in tasks} != {'firefox', 'geany'}:
            raise ValueError('Comparisons require one managed Firefox and one managed Geany')
        self.foreground = next(t['id'] for t in tasks if t['app'] == 'firefox')
        self.run_id = self.command('begin_control', resource=self.resource, generation=self.before['generation'],
            foreground_id=self.foreground, profile_id=profile_id, scored=scored,
            tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'} for t in tasks])['run_id']
        self.pulse = threading.Thread(target=self.heartbeat, daemon=True)
        self.pulse.start()
        return self.sample()

    def heartbeat(self):
        while not self.stop.wait(1):
            if self.run_id:
                try:
                    self.transport('heartbeat', run_id=self.run_id)
                except (OSError, RuntimeError, ValueError):
                    pass

    def sample(self):
        snapshot = self.transport('snapshot')
        self.audit(kind='guest_snapshot', **snapshot)
        if self.run_id and snapshot['run_id'] != self.run_id:
            raise RuntimeError('Controller lease/run was lost')
        identities = {t['id']: t['identity'] for t in self.before['tasks']}
        if ({t['id']: t.get('identity') for t in snapshot['tasks']} != identities
                or any(t.get('cpus_effective') != '1-3' or t.get('inspection_error') for t in snapshot['tasks'])
                or snapshot['preflight'].get('baseline') != self.before['preflight'].get('baseline')
                or snapshot['recovery_error'] or not snapshot['preflight']['passed']):
            raise RuntimeError('VM baseline, task identity, placement or telemetry changed')
        control = control_of(snapshot)
        expected = control.get('expected', {})
        settings_match = not control.get('error')
        if self.resource == 'cpu':
            settings_match &= {t['id']: t['cpu_weight'] for t in snapshot['tasks']} == expected
        else:
            settings_match &= all(t['cpu_weight'] == 100 for t in snapshot['tasks'])
            if self.resource == 'memory':
                settings_match &= all(snapshot.get('memory_parent', {}).get(k) == v for k, v in expected.get('parent', {}).items())
                try:
                    settings_match &= {t['id']: memory_values(t) for t in snapshot['tasks']} == expected.get('tasks')
                except (KeyError, TypeError, ValueError):
                    settings_match = False
            else:
                settings_match &= snapshot.get('network_parent', {}).get('settings') == expected
        if not settings_match or control.get('scored') and not control.get('scoring_valid'):
            raise RuntimeError('Resource settings changed or frozen profile became invalid')
        original = {t['id']: t for t in self.before['tasks']}
        if any(counters(t.get('memory_events', {})).get(event, 0) >
               counters(original[t['id']].get('memory_events', {})).get(event, 0)
               for t in snapshot['tasks'] for event in ('oom', 'oom_kill')):
            raise RuntimeError('OOM invalidated the run')
        for fixture in self.fixtures:
            rows = fixture.drain()
            if rows:
                self.audit(kind='guest_fixture_metrics', task_id=fixture.task['id'], rows=rows)
        self.latest = snapshot
        return snapshot

    def wait(self, seconds, *, policy=False, on_sample=None):
        until, next_sample = time.monotonic() + seconds, 0.
        while time.monotonic() < until:
            now = time.monotonic()
            if now >= next_sample and not self.policy.pending:
                result = self.sample()
                if on_sample:
                    on_sample(result)
                next_sample = now + 1
            if policy:
                self.policy.tick(self.latest, self.command, self.audit)
            time.sleep(.05)

    def favor(self):
        limit = time.monotonic() + 50
        while time.monotonic() < limit:
            result = self.sample()
            control = control_of(result)
            if control.get('ready'):
                return self.command('resource_decide', generation=result['generation'], observation_id=control['observation_id'], action='favor')
            time.sleep(1)
        raise RuntimeError('Resource observation/cooldown/headroom gate did not become ready')

    def start_fixtures(self, profile, config, *, only=None, direction='both', saturated=False):
        for task in self.before['tasks']:
            if only and task['id'] not in only:
                continue
            role = 'background' if saturated or task['id'] != self.foreground else 'foreground'
            fixture = Fixture(task, self.resource, role, profile, self.directory, config, direction=direction)
            self.fixtures.append(fixture)
            self.audit(**{**fixture.ready, 'kind': 'guest_fixture'})

    def start_pressure(self, mib):
        if not 128 <= mib <= 5120:
            raise ValueError('Invalid calibrated pressure fixture')
        self.pressure_unit = 'aos-pressure-' + uuid.uuid4().hex + '.service'
        command = ['sudo', '-n', 'systemd-run', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + self.pressure_unit,
                   '--property=User=' + str(os.getuid()), '--property=Group=' + str(os.getgid()),
                   '--property=MemoryMax=6G', '--property=MemorySwapMax=0', '--property=AllowedCPUs=0',
                   '/usr/bin/python3', str(Path(__file__).with_name('fixtures.py')), '--pressure-mib', str(mib), '--seconds', '900']
        self.pressure_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE, text=True)
        import select
        if not select.select([self.pressure_process.stdout], [], [], 120)[0]:
            raise RuntimeError('Pressure fixture did not initialize')
        ready = json.loads(self.pressure_process.stdout.readline())
        if ready.get('kind') != 'pressure_ready':
            raise RuntimeError('Pressure fixture startup failed')
        self.audit(**{**ready, 'kind': 'guest_pressure_fixture', 'unit': self.pressure_unit})

    def close(self):
        errors = [self.error] if self.error else []
        try:
            self.policy.close()
        except Exception as exc:
            errors.append('Policy cleanup: ' + str(exc))
        if self.pressure_process:
            try:
                self.pressure_process.stdin.close()
                try:
                    self.pressure_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    subprocess.run(['sudo', '-n', 'systemctl', 'stop', self.pressure_unit], check=True, timeout=15)
                    self.pressure_process.wait(timeout=5)
            except Exception as exc:
                errors.append('Pressure cleanup: ' + str(exc))
            finally:
                for stream in (self.pressure_process.stdout, self.pressure_process.stderr):
                    stream.close()
        if self.run_id:
            try:
                self.command('end')
            except Exception as exc:
                errors.append('Restoration pending: ' + str(exc))
        self.stop.set()
        if self.pulse:
            self.pulse.join(2)
        for fixture in self.fixtures:
            try:
                fixture.close()
            except Exception as exc:
                errors.append('Fixture cleanup: ' + str(exc))
        self.error = '; '.join(errors) or None
        self.store.finish(self.session, 'failed' if self.error else 'completed')
        self.store.export(self.session, self.directory / 'recording.json', 'json')
        self.store.export(self.session, self.directory / 'recording.csv', 'csv')
        self.store.close()


def trial(config, row, directory, *, key=''):
    run = GuestRun(directory, config['resource'], key=key, readiness_directory=directory.parent.parent / 'readiness')
    result = {**row, 'resource': config['resource'], 'valid': False, 'calibration_id': config['id'],
              'measurement_kind': 'owned synthetic probe latency, not GUI latency'}
    try:
        run.begin(config.get('memory_profile_id'), scored=True)
        if fingerprint(run.before, config['resource']) != config['fingerprint']:
            raise ValueError('Calibration is from another VM or implementation baseline')
        run.start_fixtures(row['profile'], config)
        if config['resource'] == 'memory' and row['profile'] == 'contention':
            run.start_pressure(config['pressure_mib'])
        if row['arm'] == 'manual':
            warm_started = time.monotonic()
            run.favor()
            elapsed = time.monotonic() - warm_started
            if elapsed >= 30:
                raise RuntimeError('Manual configuration was not established within the warm-up')
            run.wait(30 - elapsed)
        else:
            run.policy.set_mode('manual' if row['arm'] == 'default' else row['arm'], run.latest)
            run.wait(30, policy=True)
        start = time.monotonic()
        cpu_start = time.process_time()
        first = run.sample()
        peaks = defaultdict(int)
        def check(snapshot):
            control = control_of(snapshot)
            if control.get('scored') and not control.get('scoring_valid'):
                raise RuntimeError('Frozen high profile needs adjustment; retain this invalid trial and recalibrate')
            for task in snapshot['tasks']:
                peaks[task['id']] = max(peaks[task['id']], task.get('memory_current', 0))
                old = next(t for t in first['tasks'] if t['id'] == task['id'])
                if any(counters(task.get('memory_events', {})).get(event, 0) > counters(old.get('memory_events', {})).get(event, 0)
                       for event in ('oom', 'oom_kill')):
                    raise RuntimeError('OOM invalidated the trial')
        run.wait(180, policy=True, on_sample=check)
        finish = time.monotonic()
        last = run.sample()
        outcomes = {}
        for fixture in run.fixtures:
            metrics = [m for m in fixture.rows if start <= m['at'] <= finish]
            if not metrics:
                raise RuntimeError('Missing workload completion evidence')
            deadline = config['deadline_ms']
            outcomes[fixture.task['id']] = {'p95_ms': percentile([m['latency_ms'] for m in metrics]),
                'completed': len(metrics), 'throughput': sum(m['work'] for m in metrics) / (finish - start),
                'deadline_miss_rate': sum(m['latency_ms'] > deadline for m in metrics) / len(metrics)}
        result.update(valid=True, seconds=finish-start, outcomes=outcomes,
            foreground_p95_ms=outcomes[run.foreground]['p95_ms'],
            foreground_deadline_miss_rate=outcomes[run.foreground]['deadline_miss_rate'],
            background_throughput={key: value['throughput'] for key, value in outcomes.items() if key != run.foreground},
            peak_memory_bytes=dict(peaks), before=first, after=last,
            fallback_proportion=run.policy.fallbacks / max(1, run.policy.decisions),
            controller_cpu_seconds=time.process_time()-cpu_start,
            controller_max_rss=usage.getrusage(usage.RUSAGE_SELF).ru_maxrss)
    except Exception as exc:
        result['error'] = run.error = str(exc)
    finally:
        run.close()
        if run.error:
            result.update(valid=False, error=run.error)
        (run.directory / 'trial.json').write_text(json.dumps(result, indent=2))
    return result


def calibrate(resource, output, endpoint):
    """Fresh profiles are measured in the same owned groups used by comparisons."""
    source = request('snapshot')
    config = {'id': str(uuid.uuid4()), 'backend': 'guest-linux', 'resource': resource, 'passed': False,
              'fingerprint': fingerprint(source, resource), 'endpoint': endpoint, 'iterations': 50000,
              'deadline_ms': {'cpu': 65., 'memory': .5, 'network': 45.}[resource], 'pressure_mib': 5120,
              'time_ns': time.time_ns(), 'attempts': [], 'memory_mib': {}}
    for task in source['tasks']:
        target = 1984 if task['app'] == 'firefox' else 1024
        config['memory_mib'][task['id']] = max(64, int(target - task.get('memory_current', 0) / 1024**2) // 64 * 64)
    for attempt in range(3 if resource == 'cpu' else 1):
        run = GuestRun(output / ('calibration-' + config['id'][:8] + '-' + str(attempt)), resource)
        evidence = {'attempt': attempt, 'passed': False}
        try:
            run.begin()
            run.start_fixtures('contention' if resource == 'memory' else 'light', config)
            run.wait(35)
            sample = run.sample()
            foreground = next(f for f in run.fixtures if f.task['id'] == run.foreground)
            recent = [r['latency_ms'] for r in foreground.rows[-100:]]
            p95 = percentile(recent)
            evidence.update(foreground_p95_ms=p95, configuration={k: v for k, v in config.items() if k != 'attempts'}, sample=sample)
            if resource == 'cpu' and not 30 <= p95 <= 50:
                config['iterations'] = max(1000, min(2_000_000, int(config['iterations'] * min(1.8, max(.3, 40 / p95)))))
                evidence['error'] = 'CPU request duration requires recalibration'
            else:
                if resource == 'memory':
                    control = sample['resource_control']
                    profile = run.command('memory_profile', generation=sample['generation'],
                                          observation_id=control['observation_id'], action='establish')['profile']
                    evidence['memory_profile'] = profile
                    if not profile['suitable']:
                        raise RuntimeError('Memory headroom profile unsuitable: ' + profile['reason'])
                    config['memory_profile_id'] = profile['id']
                    config['memory_highs'] = profile['highs']
                    config['deadline_ms'] = max(.25, 3 * p95)
                evidence['passed'] = config['passed'] = True
        except Exception as exc:
            evidence['error'] = run.error = str(exc)
        finally:
            run.close()
            if run.error:
                evidence.update(passed=False, error=run.error)
                config['passed'] = False
            config['attempts'].append(evidence)
        if config['passed']:
            break
    if config['passed']:
        config['contention'] = contention_pilot(config, output)
        config['passed'] = config['contention']['passed']
    output.mkdir(parents=True, exist_ok=True)
    destination = output / ('calibration-' + resource + '-' + config['id'] + '.json')
    destination.write_text(json.dumps(config, indent=2))
    return config, destination


def contention_checks(resource, first, last):
    seconds = (last['observed_at_ns'] - first['observed_at_ns']) / 1e9
    rows = {t['id']: t for t in first['tasks']}
    checks = {'steady_seconds': seconds >= 30}
    if resource == 'cpu':
        checks['cpu_saturated'] = sum(t['cpu_usage_usec'] - rows[t['id']]['cpu_usage_usec'] for t in last['tasks']) / max(seconds, .001) >= 2_500_000
    elif resource == 'memory':
        checks['memory_pressure'] = any((pressure(t.get('memory_pressure')) or 0) >= 1 for t in last['tasks'])
        checks['reclaim_observed'] = any(counters(t.get('memory_stat', {})).get('pgscan', 0) >
            counters(rows[t['id']].get('memory_stat', {})).get('pgscan', 0) for t in last['tasks'])
    else:
        for direction in ('upload', 'download'):
            bps = sum(t['network'][direction + '_bytes'] - rows[t['id']]['network'][direction + '_bytes']
                      for t in last['tasks']) * 8 / max(seconds, .001)
            checks[direction + '_saturated'] = 19_000_000 <= bps <= 21_000_000
    return checks


def contention_pilot(config, output):
    run = GuestRun(output / ('contention-' + config['id'][:8]), config['resource'])
    evidence = {'passed': False}
    try:
        run.begin(config.get('memory_profile_id'))
        run.start_fixtures('contention', config)
        if config['resource'] == 'memory':
            run.start_pressure(config['pressure_mib'])
        run.wait(5)
        first = run.sample()
        run.wait(30)
        last = run.sample()
        checks = contention_checks(config['resource'], first, last)
        checks['headroom_stable'] = not control_of(last).get('headroom_required')
        evidence.update(before=first, after=last, checks=checks, passed=all(checks.values()))
        if not evidence['passed']:
            run.error = 'Contention or frozen headroom not established; recalibrate before comparisons'
    except Exception as exc:
        run.error = str(exc)
    finally:
        run.close()
        if run.error:
            evidence.update(passed=False, error=run.error)
    return evidence


def compare(config, directory, seed, *, model=False, key=''):
    if not config.get('passed') or not config.get('contention', {}).get('passed') or config.get('backend') != 'guest-linux':
        raise ValueError('A passing guest calibration is required')
    source = request('snapshot')
    if fingerprint(source, config['resource']) != config['fingerprint']:
        raise ValueError('Calibration baseline changed')
    if model and not qualified(directory.parent / 'readiness', source, config['resource']):
        raise ValueError('Fresh guest readiness and shadow evidence are required for four-arm comparisons')
    directory.mkdir(parents=True, exist_ok=False)
    plan = schedule(seed, model=model)
    (directory / 'schedule.json').write_text(json.dumps({'seed': seed, 'model': model, 'trials': plan}, indent=2))
    results = []
    for index, row in enumerate(plan):
        print(json.dumps({'event': 'trial_started', 'index': index + 1, 'total': len(plan), **row}), flush=True)
        result = trial(config, row, directory / f'{index + 1:02d}-{row["profile"]}-{row["arm"]}', key=key)
        results.append(result)
        report = comparison_report(results)
        (directory / 'comparison.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'event': 'trial_finished', 'index': index + 1, 'valid': result['valid'],
                          'foreground_p95_ms': result.get('foreground_p95_ms'), 'error': result.get('error')}), flush=True)
        if not result['valid']:
            # A broken baseline invalidates later pairing; retain the failed trial and explicit unrun slots.
            report['unrun_trials'] = plan[index + 1:]
            (directory / 'comparison.json').write_text(json.dumps(report, indent=2))
            break
    return report


def readiness_check(resource, directory, key):
    from .policy import new_helper
    source = request('snapshot')
    if source['run_id'] or not source['preflight']['passed']:
        raise ValueError('Readiness requires a healthy guest without an active run')
    check = GuestReadiness(new_helper(key), source, resource)
    count = -1
    try:
        while not check.done:
            check.poll()
            if len(check.rows) != count:
                count = len(check.rows)
                print(json.dumps({'event': 'readiness_progress', 'resource': resource, 'contexts': count}), flush=True)
            time.sleep(.05)
    finally:
        if not check.done:
            check.finish()
        path = save_report(directory / 'readiness', check.report)
    return check.report, path


def shadow_check(config, directory, key):
    run = GuestRun(directory / ('shadow-' + config['resource'] + '-' + uuid.uuid4().hex[:8]), config['resource'], key=key)
    report = None
    try:
        run.begin(config.get('memory_profile_id'))
        run.start_fixtures('contention', config)
        if config['resource'] == 'memory':
            run.start_pressure(config['pressure_mib'])
        run.policy.set_mode('shadow', run.latest)
        until = time.monotonic() + 720
        while len(run.policy.shadow_rows) < 36 and time.monotonic() < until:
            run.wait(1, policy=True)
            if control_of(run.latest).get('headroom_required'):
                raise RuntimeError('Shadow run needs memory recalibration')
        report = {'kind': 'guest_shadow', 'backend': 'guest-linux', 'resource': config['resource'],
                  'fingerprint': fingerprint(run.before, config['resource']), 'completed_at_ns': time.time_ns(),
                  'rows': list(run.policy.shadow_rows)}
        report['passed'] = validate_report(report, run.before, config['resource'], shadow=True)
    except Exception as exc:
        run.error = str(exc)
        report = {'kind': 'guest_shadow', 'backend': 'guest-linux', 'resource': config['resource'],
                  'fingerprint': fingerprint(run.before, config['resource']) if run.before else '',
                  'completed_at_ns': time.time_ns(), 'rows': list(run.policy.shadow_rows), 'passed': False, 'error': str(exc)}
    finally:
        run.close()
        if report is not None:
            if run.error:
                report.update(passed=False, error=run.error)
            path = save_report(directory / 'readiness', report)
    return report, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('calibrate', 'compare', 'readiness', 'shadow'))
    parser.add_argument('--resource', choices=('cpu', 'memory', 'network'), default='cpu')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--calibration', type=Path)
    parser.add_argument('--endpoint', default='http://192.168.64.1:8766')
    parser.add_argument('--seed', type=int, default=20261010)
    parser.add_argument('--model', action='store_true')
    parser.add_argument('--jev-env-file', type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() == 0:
        parser.error('Run as the ordinary desktop user inside the guest')
    os.sched_setaffinity(0, {0})
    key = ''
    if args.jev_env_file:
        from adaptive_os.desktop import read_jev_env_file
        key = read_jev_env_file(args.jev_env_file)
    if args.operation == 'calibrate':
        config, path = calibrate(args.resource, args.output, args.endpoint)
        print(json.dumps({'passed': config['passed'], 'calibration': str(path)}))
        return int(not config['passed'])
    if args.operation == 'readiness':
        report, path = readiness_check(args.resource, args.output, key)
        print(json.dumps({'passed': report['passed'], 'report': str(path)}))
        return int(not report['passed'])
    if not args.calibration:
        parser.error('--calibration is required')
    if args.operation == 'shadow':
        report, path = shadow_check(json.loads(args.calibration.read_text()), args.output, key)
        print(json.dumps({'passed': report['passed'], 'report': str(path)}))
        return int(not report['passed'])
    report = compare(json.loads(args.calibration.read_text()), args.output, args.seed, model=args.model, key=key)
    return int(bool(report.get('unrun_trials') or report['invalid_trials']))


if __name__ == '__main__':
    raise SystemExit(main())
