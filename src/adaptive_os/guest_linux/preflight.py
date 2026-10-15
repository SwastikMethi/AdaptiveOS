"""Read-only host prerequisites; no disk allocation or downloads."""

import argparse
import json
from pathlib import Path
import platform
import shutil
import time


def host_report(storage):
    free = shutil.disk_usage(storage).free
    utm = next((str(p) for p in (Path('/Applications/UTM.app'), Path.home() / 'Applications/UTM.app')
                if p.is_dir()), None)
    checks = {'storage': free >= 180 * 1024**3, 'apple_silicon': platform.machine() == 'arm64',
              'macos': platform.system() == 'Darwin', 'utm': bool(utm)}
    return {'backend': 'guest-linux', 'milestone': 1, 'time_ns': time.time_ns(),
            'passed': all(checks.values()), 'vm_verified': False, 'checks': checks,
            'storage': {'path': str(Path(storage).resolve()), 'free_bytes': free,
                        'required_bytes': 180 * 1024**3,
                        'budget_gib': {'working_disk': 64, 'recovery_disk': 64,
                                       'installers': 8, 'evidence': 12, 'host_headroom': 32}}, 'utm': utm,
            'vm': {'hypervisor': 'UTM', 'os': 'Ubuntu 24.04 LTS',
                   'architecture': 'arm64', 'desktop': 'KDE Plasma/X11',
                   'vcpus': 4, 'ram_bytes': 8 * 1024**3, 'disk_bytes': 64 * 1024**3},
            'next_gate': 'Install and verify the VM; host prerequisites do not qualify live control'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = host_report(args.storage)
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end='')
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
