"""Live memory/network mechanism and recovery evidence, separate from scored comparisons."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .client import request
from .experiments import GuestRun, network_checks, contention_checks
from .fixtures import BLOCK
from .policy import control_of
from .resources import MEMORY_PARENT, POOL, MIN_LOW, high_for
from .verify import recovery_checks


def memory_checks(snapshot, profile):
    tasks = snapshot['tasks']
    parent = snapshot['memory_parent']
    return {'direct_root_hierarchy': all(Path(t['cgroup']).parent == Path('/adaptiveos.slice') for t in tasks),
            'parent_protection_and_safety': all(parent[k] == v for k, v in MEMORY_PARENT.items()),
            'child_pool': sum(t['memory_low'] for t in tasks) <= POOL and all(t['memory_low'] >= MIN_LOW for t in tasks),
            'independent_profiled_highs': profile['suitable'] and all(int(t['memory_high']) == high_for(profile['peaks'][t['id']]) for t in tasks),
            'low_bounded_by_high': all(t['memory_low'] <= int(t['memory_high']) for t in tasks)}


def packet_checks(captures, mappings, task_address, host='192.168.64.1', port=8766):
    def headers(text):
        return [(a, int(b), c, int(d)) for a, b, c, d in re.findall(r'IP ([0-9.]+)\.(\d+) > ([0-9.]+)\.(\d+):', text)]
    packets = {key: headers(value) for key, value in captures.items()}
    uploads = [p for p in packets.get('task', []) if p[0] == task_address and p[2:] == (host, port)]
    correlated = []
    for source, source_port, destination, destination_port in uploads:
        pattern = (rf'src={re.escape(source)} dst={re.escape(destination)} sport={source_port} dport={destination_port}.*?'
                   rf'src={re.escape(destination)} dst=([0-9.]+) sport={destination_port} dport=(\d+)')
        mapping = re.search(pattern, mappings)
        if not mapping:
            continue
        mapped, mapped_port = mapping[1], int(mapping[2])
        correlated.append((source, source_port, destination, destination_port) in packets.get('gateway', []) and
                          (source, source_port, destination, destination_port) in packets.get('transit', []) and
                          (mapped, mapped_port, destination, destination_port) in packets.get('uplink', []) and
                          (destination, destination_port, mapped, mapped_port) in packets.get('uplink', []) and
                          (destination, destination_port, source, source_port) in packets.get('transit', []))
    return {'task_headers_captured': bool(uploads), 'source_before_snat_and_destination_after_reverse_nat': any(correlated)}


def capture_path(run):
    task = next(t for t in run.latest['tasks'] if t['id'] == run.foreground)
    net, processes, logs = task['network'], [], []
    locations = {'task': ['ip', 'netns', 'exec', net['namespace'], 'tcpdump', '-ni', f't{task["slot"]}'],
                 'gateway': ['ip', 'netns', 'exec', net['gateway'], 'tcpdump', '-ni', 'g-up'],
                 'transit': ['tcpdump', '-ni', 'h-down'], 'uplink': ['tcpdump', '-ni', net['uplink']]}
    result = {'captures': {}}
    try:
        for name, command in locations.items():
            log = (run.directory / (name + '.capture.log')).open('w')
            logs.append(log)
            process = subprocess.Popen(['sudo', '-n', 'timeout', '--signal=INT', '8', *command, '-s', '128',
                                        '-U', '-w', str(run.directory / (name + '.pcap')), 'host', '192.168.64.1', 'and', 'port', '8766'],
                                        stdout=log, stderr=log)
            processes.append(process)
        run.wait(4)
        result['conntrack'] = subprocess.check_output(['sudo', '-n', 'conntrack', '-L', '-f', 'ipv4'], text=True, stderr=subprocess.DEVNULL)
        run.wait(5)
        for process in processes:
            process.wait(timeout=10)
            if process.returncode not in (0, 124):
                raise RuntimeError('Packet capture failed')
        for name in locations:
            result['captures'][name] = subprocess.check_output(['sudo', '-n', 'tcpdump', '-nn', '-r',
                str(run.directory / (name + '.pcap'))], text=True, stderr=subprocess.DEVNULL, timeout=15)
        gateway_rules = subprocess.check_output(['sudo', '-n', 'ip', 'netns', 'exec', net['gateway'], 'nft', 'list', 'ruleset'], text=True)
        result['checks'] = packet_checks(result['captures'], result['conntrack'], net['address'])
        result['checks']['gateway_has_no_nat'] = not gateway_rules.strip()
        result['gateway_rules'] = gateway_rules
    finally:
        for process in processes:
            if process.poll() is None:
                process.wait(timeout=12)
        for log in logs:
            log.close()
    return result


def public_network(snapshot, directory, endpoint):
    rows = []
    for task in snapshot['tasks']:
        entered = ['sudo', '-n', 'nsenter', '-t', str(task['main_pid']), '-n', '-m', '--',
                   'setpriv', '--reuid=' + str(os.getuid()), '--regid=' + str(os.getgid()), '--init-groups']
        dns = subprocess.check_output(entered + ['getent', 'ahostsv4', 'example.com'], text=True, timeout=20)
        subprocess.run(entered + ['curl', '-fsS', '--max-time', '20', 'https://example.com/', '-o', '/dev/null'], check=True, timeout=25)
        path = directory.resolve() / (task['id'] + '-restored-download.bin')
        subprocess.run(entered + ['curl', '-fsS', '--max-time', '20', endpoint + '/?bytes=1048576', '-o', str(path)], check=True, timeout=25)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ipv6 = subprocess.check_output(['sudo', '-n', 'ip', '-n', task['network']['namespace'], '-6', 'route'], text=True)
        if digest != hashlib.sha256(BLOCK).hexdigest() or ipv6.strip() or not dns:
            raise RuntimeError('Restored namespace DNS/download/IPv6 path check failed')
        rows.append({'task_id': task['id'], 'dns': dns, 'https': True, 'sha256': digest, 'bytes': path.stat().st_size, 'ipv6_routes': ipv6})
    return rows


def verify(resource, scenario, output, config):
    run = GuestRun(output, resource)
    report = {'resource': resource, 'scenario': scenario, 'passed': False, 'performance_comparison': False, 'time_ns': time.time_ns()}
    try:
        run.begin()
        report['before'] = run.before
        if resource == 'memory':
            run.start_fixtures('contention' if scenario == 'contention' else 'light', config)
            run.wait(35)
            sample = run.sample()
            profile = run.command('memory_profile', generation=sample['generation'],
                                  observation_id=sample['resource_control']['observation_id'], action='establish')['profile']
            if not profile['suitable']:
                raise RuntimeError('Representative memory profile unsuitable: ' + profile['reason'])
            sample = run.sample()
            report['profile'], report['memory_checks'] = profile, memory_checks(sample, profile)
            if scenario == 'contention':
                run.start_pressure(config['pressure_mib'])
                first = run.sample()
                run.wait(30)
                report['memory_checks'].update(contention_checks('memory', first, run.sample()))
            before_lows = {t['id']: t['memory_low'] for t in sample['tasks']}
            report['favor_ack'] = run.favor()
            changed = run.sample()
            report['protection_changes'] = {t['id']: t['memory_low'] - before_lows[t['id']] for t in changed['tasks']}
            report['memory_checks']['foreground_gained_64_mib'] = report['protection_changes'][run.foreground] == 64 * 1024**2
            report['memory_checks']['highs_unchanged_by_favor'] = all(int(t['memory_high']) == profile['highs'][t['id']] for t in changed['tasks'])
            report['changed'] = changed
        else:
            run.start_fixtures('contention', config, saturated=True)
            run.wait(5)
            if scenario == 'contention':
                report['packet_path'] = capture_path(run)
                phases = []
                def measure(label, borrower=False):
                    run.wait(5)
                    first = run.sample()
                    run.wait(30)
                    last = run.sample()
                    seconds = (last['observed_at_ns'] - first['observed_at_ns']) / 1e9
                    for direction in ('upload', 'download'):
                        delta = {t['id']: t['network'][direction + '_bytes'] - next(
                                 row for row in first['tasks'] if row['id'] == t['id'])['network'][direction + '_bytes'] for t in last['tasks']}
                        total = sum(delta.values())
                        expected = {run.foreground: 1.} if borrower else {key: value / 10000 for key, value in
                            first['resource_control']['expected'][direction]['shares'].items()}
                        phases.append({'direction': direction, 'phase': label, 'seconds': seconds, 'borrower': borrower,
                                       'parent_mbps': total * 8 / seconds / 1e6, 'expected_shares': expected,
                                       'shares': {key: value / total if total else 0 for key, value in delta.items()}})
                measure('equal')
                report['favor_ack'] = run.favor()
                measure('favored')
                for fixture in list(run.fixtures):
                    if fixture.task['id'] != run.foreground:
                        fixture.close()
                        run.fixtures.remove(fixture)
                measure('sole_borrower', True)
                report['phases'], report['network_checks'] = phases, network_checks(phases)
            else:
                report['favor_ack'] = run.favor()
        run.policy.cancel()
        if scenario in ('normal', 'contention'):
            report['restoration'] = run.command('end')
        else:
            run.stop.set()
            run.pulse.join(3)
            if scenario == 'controller-loss':
                time.sleep(11)
            elif scenario == 'service-restart':
                subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'adaptive-os-guest.service'], check=True, timeout=30)
            else:
                subprocess.run(['sudo', '-n', 'systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL',
                                'adaptive-os-guest.service'], check=True, timeout=10)
        until = time.monotonic() + 25
        while time.monotonic() < until:
            try:
                after = request('snapshot')
                if not after['run_id'] and not after['recovery_error']:
                    break
            except (OSError, RuntimeError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('Resource recovery did not complete')
        run.run_id = None
        report['after'], report['recovery_checks'] = after, recovery_checks(run.before, after)
        if resource == 'memory':
            original = {t['id']: t for t in run.before['tasks']}
            report['recovery_checks']['child_memory_restored'] = all(all(str(t['memory_' + k]) == str(original[t['id']]['memory_' + k])
                for k in ('low', 'min', 'high', 'max')) for t in after['tasks'])
            report['recovery_checks']['parent_memory_restored'] = after['preflight']['parent'] == run.before['preflight']['parent']
        report['restored_connectivity'] = public_network(after, output, config['endpoint'])
        report['passed'] = (all(report['recovery_checks'].values()) and all(report.get('memory_checks', {}).values())
                            and all(report.get('network_checks', {}).values()) and all(report.get('packet_path', {}).get('checks', {}).values()))
    except Exception as exc:
        report['error'] = run.error = str(exc)
    finally:
        run.close()
        if run.error:
            report.update(passed=False, error=run.error)
        (output / 'verification.json').write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource', choices=('memory', 'network'), required=True)
    parser.add_argument('--scenario', choices=('normal', 'controller-loss', 'service-restart', 'service-crash', 'contention'), required=True)
    parser.add_argument('--calibration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as the ordinary guest user')
    os.sched_setaffinity(0, {0})
    report = verify(args.resource, args.scenario, args.output, json.loads(args.calibration.read_text()))
    print(json.dumps({key: value for key, value in report.items() if key in ('passed', 'resource', 'scenario', 'error', 'memory_checks', 'network_checks', 'recovery_checks')}))
    return int(not report['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
