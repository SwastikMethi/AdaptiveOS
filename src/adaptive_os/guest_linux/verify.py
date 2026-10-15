"""Capture Milestone 1 evidence in the guest; never qualify later controls."""

import argparse
import json
from pathlib import Path
import subprocess
import time

from .client import request


def recovery_checks(before, after):
    starting = {task['id']: task for task in before['tasks']}
    ending = {task['id']: task for task in after['tasks']}
    same = bool(starting) and starting.keys() == ending.keys()
    return {'application_identities': same and all(ending[key]['identity'] == task['identity'] for key, task in starting.items()),
            'weights_restored': same and all(ending[key]['cpu_weight'] == task['cpu_weight'] for key, task in starting.items()),
            'placement_unchanged': same and all(ending[key]['cpus_effective'] == task['cpus_effective'] == '1-3'
                                              for key, task in starting.items()),
            'namespaces_retained': same and all(ending[key]['network'] == task['network'] for key, task in starting.items()),
            'profiles_retained': same and all(ending[key]['profile'] == task['profile'] for key, task in starting.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('observe', 'normal', 'controller-loss', 'service-restart', 'service-crash'),
                        default='observe')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'backend': 'guest-linux', 'milestone': 1, 'scenario': args.scenario,
              'time_ns': time.time_ns(), 'passed': False, 'milestone_gate_passed': False,
              'pending': ['DNS, HTTPS and checksum-verified download', 'packet-path and descendant audit',
                          'saved files, unsaved Geany text and Firefox page state through each interruption']}
    run_id = None
    try:
        before = request('snapshot')
        report['before'] = before
        if not before['preflight']['passed'] or before['recovery_error']:
            raise RuntimeError('Guest preflight or reconciliation is not healthy')
        if {t['app'] for t in before['tasks']} != {'firefox', 'geany'}:
            raise RuntimeError('Launch Firefox and Geany in the guest UI before collecting evidence')
        if args.scenario != 'observe':
            if before['run_id']:
                raise RuntimeError('End the UI probe run before invoking this verifier')
            task = next(t for t in before['tasks'] if t['app'] == 'firefox')
            run_id = request('begin')['run_id']
            report['probe'] = request('probe', run_id=run_id, task_id=task['id'],
                                      identity=task['identity'], expected_weight=100)
            if args.scenario == 'normal':
                report['end'] = request('end', run_id=run_id)
            elif args.scenario == 'controller-loss':
                time.sleep(11)
            elif args.scenario == 'service-restart':
                subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'adaptive-os-guest.service'], check=True, timeout=30)
            else:
                subprocess.run(['sudo', '-n', 'systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL',
                                'adaptive-os-guest.service'], check=True, timeout=10)
            deadline = time.monotonic() + 20
            while True:
                try:
                    after = request('snapshot')
                    if after['run_id'] is None:
                        break
                except (OSError, RuntimeError, ValueError):
                    if time.monotonic() >= deadline:
                        raise
                if time.monotonic() >= deadline:
                    raise RuntimeError('Restoration was not confirmed after interruption')
                time.sleep(.5)
            report['after'] = after
            report['checks'] = recovery_checks(before, after)
            report['passed'] = all(report['checks'].values()) and report['probe']['weight'] == 90
            run_id = None
        else:
            report['passed'] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc)
    finally:
        if run_id:
            try:
                current = request('snapshot')
                if current['run_id'] == run_id:
                    report['emergency_restore'] = request('end', run_id=run_id)
            except (OSError, RuntimeError, ValueError) as exc:
                report['restoration_error'] = str(exc)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'passed': report['passed'], 'milestone_gate_passed': False, 'evidence': str(args.output)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
