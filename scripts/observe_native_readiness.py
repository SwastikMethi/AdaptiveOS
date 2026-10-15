"""Observe a UI-started readiness run without reading credentials or controlling it."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid


def command(arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout.strip()


def resources(pid):
    rows = command(['/bin/ps', '-Ao', 'pid=,ppid=,pcpu=,rss=,time=,comm=']).splitlines()
    owned = []
    for row in rows:
        fields = row.split(None, 5)
        if len(fields) == 6 and (fields[0] == str(pid) or
                (fields[1] == str(pid) and fields[5].endswith('/Contents/Helpers/model_helper/model_helper'))):
            owned.append({'pid': int(fields[0]), 'parent_pid': int(fields[1]),
                          'cpu_pct_sample': float(fields[2]), 'rss_kib': int(fields[3]),
                          'cumulative_cpu_time': fields[4], 'executable': fields[5]})
    return {'processes': owned, 'vm_stat': command(['/usr/bin/vm_stat']),
            'swap_usage': command(['/usr/sbin/sysctl', '-n', 'vm.swapusage'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--fingerprint', required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    after = args.after.stat().st_mtime_ns
    destination = Path('output/native-readiness-observations') / uuid.uuid4().hex
    destination.mkdir(parents=True)
    started = time.monotonic()
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    report = {'app_pid': args.pid, 'fingerprint': args.fingerprint,
              'awake_assertion': 'caffeinate -dimsu', 'samples': []}
    next_sample = started
    print(destination, flush=True)
    try:
        while time.monotonic()-started < args.timeout:
            if time.monotonic() >= next_sample:
                sample = {'elapsed_s': time.monotonic()-started, 'unix_time': time.time(), **resources(args.pid)}
                report['samples'].append(sample)
                print(json.dumps({k: sample[k] for k in ('elapsed_s', 'processes', 'swap_usage')}), flush=True)
                next_sample += 30
            for path in args.directory.glob(f'readiness-{args.fingerprint[:12]}-*.json'):
                if path.stat().st_mtime_ns <= after:
                    continue
                try:
                    readiness = json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue  # Exclusive report creation may still be writing its final bytes.
                assert readiness['fingerprint'] == args.fingerprint
                shutil.copy2(path, destination / path.name)
                report.update(completed=True, readiness_file=path.name, ready=readiness['ready'],
                              valid_in_deadline=readiness['valid_in_deadline'], contexts=readiness['contexts'])
                print(json.dumps({k: v for k, v in readiness.items() if k != 'rows'}), flush=True)
                return
            time.sleep(1)
        raise TimeoutError('No new completed native readiness report')
    finally:
        report['elapsed_s'] = time.monotonic()-started
        (destination/'observation.json').write_text(json.dumps(report, indent=2, allow_nan=False))
        awake.terminate()
        awake.wait(timeout=5)
        print(destination/'observation.json', flush=True)


if __name__ == '__main__':
    main()
