"""Collect guest CPU mechanism evidence; never qualify benchmarks or Jev."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def load_target(task_id, identity, state=Path('/var/lib/adaptive-os/journal.sqlite3'),
                config=Path('/etc/adaptive-os/guest.json')):
    if str(uuid.UUID(task_id)) != task_id:
        raise ValueError('Invalid registered task ID')
    with sqlite3.connect(state.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
    task = next((t for t in json.loads(row[0])['tasks'] if t['id'] == task_id), None) if row else None
    if not task or task.get('identity') != identity:
        raise ValueError('Unregistered task identity')
    unit = 'adaptiveos-task-' + task_id + '.service'
    values = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', unit, '--property=ControlGroup,InvocationID,MainPID'], text=True, timeout=8).splitlines() if '=' in line)
    if identity != boot_id() + ':' + values.get('InvocationID', ''):
        raise ValueError('Task invocation identity changed')
    if values.get('ControlGroup') != '/adaptiveos.slice/' + unit or int(values.get('MainPID', 0)) <= 0:
        raise ValueError('Task is outside its live owned cgroup')
    uid = json.loads(config.read_text())['uid']
    if type(uid) is not int or uid < 1000:
        raise ValueError('CPU helpers require the ordinary guest user')
    return {'uid': uid, 'gid': pwd.getpwuid(uid).pw_gid,
            'cgroup': '/sys/fs/cgroup' + values['ControlGroup'], 'namespace': '/proc/' + values['MainPID'] + '/ns/net'}


def run_load(task_id, identity):
    """Admin-only fixture: three bounded children, never signal application PIDs."""
    if os.geteuid() != 0:
        raise PermissionError('CPU load setup requires root')
    target = load_target(task_id, identity)
    os.sched_setaffinity(0, {0})
    children = []
    namespace = os.open(target['namespace'], os.O_RDONLY)
    try:
        for _ in range(3):
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    Path(target['cgroup'], 'cgroup.procs').write_text(str(os.getpid()))
                    os.setns(namespace, 0)
                    os.close(namespace)
                    os.setgroups([])
                    os.setgid(target['gid'])
                    os.setuid(target['uid'])
                    os.sched_setaffinity(0, {1, 2, 3})
                    os.write(write_fd, b'1')
                    os.close(write_fd)
                    until = time.monotonic() + 600
                    while time.monotonic() < until:
                        hashlib.pbkdf2_hmac('sha256', b'cpu-proof', b'bounded-load', 100000)
                    os._exit(0)
                except BaseException:
                    import traceback
                    traceback.print_exc()
                    os._exit(1)
            children.append(pid)
            os.close(write_fd)
            try:
                if not select.select([read_fd], [], [], 8)[0] or os.read(read_fd, 1) != b'1':
                    raise RuntimeError('CPU helper failed to enter its task cgroup')
            finally:
                os.close(read_fd)
        def stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        print(json.dumps({'task_id': task_id, 'identity': identity, 'pids': children}), flush=True)
        select.select([sys.stdin], [], [], 600)
    finally:
        os.close(namespace)
        for pid in children:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for pid in children:
            os.waitpid(pid, 0)


def distribution_checks(phases):
    baseline, half, quarter, restored = phases
    return {'steady_windows': all(p['wall_seconds'] >= 30 for p in phases),
            'saturated': all(p['cpu_seconds'] / p['wall_seconds'] >= 2.7 for p in phases),
            'proportional_shares': all(abs(p['foreground_share'] - p['expected_share']) <= .10 for p in phases),
            'weight_50_improves_share': half['foreground_share'] >= baseline['foreground_share'] + .08,
            'weight_25_improves_share': quarter['foreground_share'] >= half['foreground_share'] + .08,
            'restored_distribution': abs(restored['foreground_share'] - baseline['foreground_share']) <= .08}


def verify(scenario, output, saved_file=None):
    from .client import request
    from .verify import recovery_checks
    report = {'backend': 'guest-linux', 'milestone': 2, 'scenario': scenario, 'time_ns': time.time_ns(),
              'passed': False, 'performance_comparison': False, 'samples': [], 'acks': [], 'helpers': []}
    run_id, loaders = None, []
    stop = threading.Event()
    pulse = None
    output.parent.mkdir(parents=True, exist_ok=True)

    def snapshot():
        result = request('snapshot')
        report['samples'].append(result)
        return result

    def heartbeat():
        while not stop.wait(1):
            try:
                request('heartbeat', run_id=run_id)
            except Exception as exc:
                report.setdefault('heartbeat_errors', []).append(str(exc))

    def ready():
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            result = snapshot()
            if (result.get('cpu_control') or {}).get('ready'):
                return result
            time.sleep(1)
        raise RuntimeError('CPU observation or cooldown gate did not become ready')

    def favor(check_replay=False):
        result = ready()
        fields = {'request_id': str(uuid.uuid4()), 'run_id': run_id, 'generation': result['generation'],
                  'observation_id': result['cpu_control']['observation_id'], 'action': 'favor'}
        ack = request('cpu_decide', **fields)
        report['acks'].append(ack)
        if ack.get('action') != 'favor':
            raise RuntimeError('CPU favor did not change an eligible background')
        if check_replay:
            for name, update, expected in [('duplicate', {}, 'duplicate'),
                                          ('stale_observation', {'request_id': str(uuid.uuid4())}, 'observation'),
                                          ('stale_generation', {'request_id': str(uuid.uuid4()), 'generation': str(uuid.uuid4())}, 'generation')]:
                try:
                    request('cpu_decide', **{**fields, **update})
                except RuntimeError as exc:
                    if expected not in str(exc).lower():
                        raise
                    report[name + '_rejected'] = True
                else:
                    raise RuntimeError(name + ' command was accepted')
        return ack

    def measure(label):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            snapshot()
            time.sleep(1)
        start = snapshot()
        start_ns = start['observed_at_ns']
        while True:
            time.sleep(1)
            end = snapshot()
            if (end['observed_at_ns'] - start_ns) / 1e9 >= 30:
                break
        first, last = ({t['id']: t for t in s['tasks']} for s in (start, end))
        seconds = {key: (last[key]['cpu_usage_usec'] - row['cpu_usage_usec']) / 1e6 for key, row in first.items()}
        total = sum(seconds.values())
        if total <= 0:
            raise RuntimeError('No CPU work was observed')
        return {'label': label, 'wall_seconds': (end['observed_at_ns'] - start_ns) / 1e9,
                'cpu_seconds': total, 'tasks_cpu_seconds': seconds,
                'foreground_share': seconds[foreground] / total,
                'expected_share': first[foreground]['cpu_weight'] / sum(t['cpu_weight'] for t in first.values()),
                'weights': {key: row['cpu_weight'] for key, row in first.items()}}

    try:
        if sys.platform != 'linux' or os.geteuid() == 0:
            raise RuntimeError('Run this verifier as the ordinary user inside the configured guest')
        os.sched_setaffinity(0, {0})
        before = snapshot()
        report['before'] = before
        tasks = before['tasks']
        if not before['preflight']['passed'] or before['recovery_error'] or before['run_id']:
            raise RuntimeError('Healthy guest with no active controller run required')
        if len(tasks) != 2 or {t['app'] for t in tasks} != {'firefox', 'geany'}:
            raise RuntimeError('Keep exactly one managed Firefox and one managed Geany open for this proof')
        foreground = next(t['id'] for t in tasks if t['app'] == 'firefox')
        if saved_file:
            report['saved_sha256_before'] = hashlib.sha256(saved_file.read_bytes()).hexdigest()
        run_id = request('begin_cpu', generation=before['generation'], foreground_id=foreground,
                         tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'} for t in tasks])['run_id']
        report['run_id'] = run_id
        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        if scenario == 'contention':
            for task in tasks:
                with output.with_name(output.stem + '-' + task['id'][:8] + '-load.log').open('w') as errors:
                    process = subprocess.Popen(['sudo', '-n', '/usr/bin/python3', str(Path(__file__).resolve()),
                                                '--load', task['id'], task['identity']],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
                loaders.append(process)
                if not select.select([process.stdout], [], [], 12)[0]:
                    raise RuntimeError('CPU load setup timed out')
                report['helpers'].append(json.loads(process.stdout.readline()))
            phases = [measure('100')]
            favor(check_replay=True)
            phases.append(measure('50'))
            favor()
            phases.append(measure('25'))
            report['restore'] = request('restore', run_id=run_id)
            phases.append(measure('restored_100'))
            report['phases'] = phases
            report['distribution_checks'] = distribution_checks(phases)
        else:
            favor(check_replay=True)
            favor()
        stop.set()
        pulse.join(3)
        if scenario in ('contention', 'normal'):
            report['end'] = request('end', run_id=run_id)
        elif scenario == 'controller-loss':
            time.sleep(11)
        elif scenario == 'service-restart':
            subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'adaptive-os-guest.service'], check=True, timeout=30)
        else:
            subprocess.run(['sudo', '-n', 'systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL',
                            'adaptive-os-guest.service'], check=True, timeout=10)
        until = time.monotonic() + 25
        while time.monotonic() < until:
            try:
                after = snapshot()
                if after['run_id'] is None and not after['recovery_error']:
                    break
            except (OSError, RuntimeError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('CPU recovery did not finish')
        report['after'] = after
        report['weight_steps_verified'] = [ack.get('weight') for ack in report['acks']] == [50, 25]
        report['recovery_checks'] = recovery_checks(before, after)
        identities = {t['id']: t['identity'] for t in before['tasks']}
        report['placement_and_identity_all_samples'] = all(
            {t['id']: t.get('identity') for t in s['tasks']} == identities and
            all(t.get('cpus_effective') == '1-3' and all(p['cpus_allowed'] == '1-3' for p in t.get('processes', []))
                for t in s['tasks']) for s in report['samples'])
        if saved_file:
            report['saved_sha256_after'] = hashlib.sha256(saved_file.read_bytes()).hexdigest()
            report['saved_file_preserved'] = report['saved_sha256_after'] == report['saved_sha256_before']
        report['passed'] = (report['weight_steps_verified'] and all(report['recovery_checks'].values()) and report['placement_and_identity_all_samples']
                            and all(report.get('distribution_checks', {}).values())
                            and report.get('saved_file_preserved', True) and not report.get('heartbeat_errors'))
        run_id = None
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        stop.set()
        if pulse:
            pulse.join(3)
        if run_id:
            try:
                report['final_restoration'] = request('end', run_id=run_id)
            except Exception as exc:
                report['final_restoration_error'] = str(exc)
        for process in loaders:
            try:
                process.communicate('stop\n', timeout=12)
                if process.returncode:
                    raise RuntimeError('CPU helper exited with code ' + str(process.returncode))
            except Exception as exc:
                report['passed'] = False
                report.setdefault('helper_cleanup_errors', []).append(str(exc))
        report['helpers_stopped'] = all(p.poll() == 0 for p in loaders)
        output.write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--load', nargs=2, metavar=('TASK_ID', 'IDENTITY'), help=argparse.SUPPRESS)
    parser.add_argument('--scenario', choices=('contention', 'normal', 'controller-loss', 'service-restart', 'service-crash'), default='contention')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--saved-file', type=Path)
    args = parser.parse_args()
    if args.load:
        run_load(*args.load)
        return 0
    if not args.output:
        parser.error('--output is required')
    report = verify(args.scenario, args.output, args.saved_file)
    print(json.dumps({key: report[key] for key in ('passed', 'scenario', 'error', 'distribution_checks', 'recovery_checks') if key in report}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
