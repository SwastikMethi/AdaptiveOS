"""Relocate the app and evaluate its helper with no venv, cache, or network."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from adaptive_os.model_readiness import ReadinessRun, configuration_fingerprint, frozen_contexts
from adaptive_os.model_runtime import HelperProcess, verify_checkpoint


@contextmanager
def clean_runtime(directory):
    previous, cwd = dict(os.environ), Path.cwd()
    clean = {key: previous[key] for key in ('HOME', 'USER', 'LOGNAME', 'TMPDIR') if key in previous}
    clean.update(PATH='/usr/bin:/bin', HF_HOME=str(directory / 'empty-hf-cache'))
    os.environ.clear()
    os.environ.update(clean)
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(previous)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, default=Path('dist/Adaptive OS Controller.app'))
    parser.add_argument('--device', choices=('mps', 'cpu'), default='mps')
    parser.add_argument('--resource', choices=('cpu', 'memory', 'network'), default='cpu')
    parser.add_argument('--packaging-only', action='store_true',
                        help='One offline connection check plus packaging faults; never grants readiness')
    parser.add_argument('--resume-root', type=Path,
                        help='Finish packaging checks for an existing relocated readiness run; do not repeat inference')
    args = parser.parse_args()
    source = args.bundle.resolve()
    root = (args.resume_root or Path('build/bundle-acceptance') / uuid.uuid4().hex).resolve()
    if not args.resume_root:
        root.mkdir(parents=True)
    destination = root / 'Relocated Controller.app'
    if not args.resume_root:
        subprocess.run(['/bin/cp', '-cR', str(source), str(destination)], check=True)
    checkpoint = destination / 'Contents/Resources/laya'
    before = verify_checkpoint(checkpoint)
    profile = root / 'offline.sb'
    profile.write_text('(version 1)\n(allow default)\n(deny network*)\n')
    command = ['/usr/bin/sandbox-exec', '-f', str(profile),
               str(destination / 'Contents/Helpers/model_helper/model_helper')]
    report = {'source': str(source), 'relocated': str(destination), 'network_denied': True,
              'activated_environment': False, 'device': args.device, 'resource': args.resource,
              'packaging_only': args.packaging_only}
    config = {'provider': 'laya', 'device': args.device, 'checkpoint': str(checkpoint),
              'support': str(root / 'working'),
              'warmups': [frozen_contexts(args.resource)[i] for i in (35, 71, 107)]}
    with clean_runtime(root):
        if args.resume_root:
            reports = list(root.glob('readiness-*.json'))
            assert len(reports) == 1, 'Expected exactly one completed readiness report'
            readiness = json.loads(reports[0].read_text())
            assert readiness['fingerprint'] == configuration_fingerprint('laya', args.device, args.resource)
            report.update(readiness=str(reports[0]), ready=readiness['ready'], readiness_reused=True)
        else:
            helper = HelperProcess(command, config)
            try:
                run = ReadinessRun(helper, 'laya', args.device, resource=args.resource,
                                   connection_only=args.packaging_only)
                previous = -1
                while not run.done:
                    run.poll()
                    if len(run.rows) != previous:
                        previous = len(run.rows)
                        print(f'{previous}/{len(run.contexts)} offline relocated contexts', flush=True)
                    time.sleep(.005)
                report['readiness'] = str(run.save(root))
                report['ready'] = run.report['ready']
                report['offline_connection_valid'] = any(r.get('valid') for r in run.rows)
            finally:
                helper.close()
        # Keep the signed copy intact; corrupt only an independent test checkpoint.
        corrupt = root / ('corrupt-checkpoint-' + uuid.uuid4().hex)
        subprocess.run(['/bin/cp', '-cR', str(checkpoint), str(corrupt)], check=True)
        with (corrupt / 'model.safetensors').open('r+b') as stream:
            original = stream.read(1)
            stream.seek(0)
            stream.write(bytes([original[0] ^ 1]))
        for name, settings, expected in (
                ('corrupt_weights', {**config, 'checkpoint': str(corrupt)}, 'unavailable'),
                ('missing_weights', {**config, 'checkpoint': str(root / 'absent')}, 'unavailable'),
                ('missing_jev_key', {'provider': 'jev'}, 'authentication')):
            helper = HelperProcess(command, settings)
            try:
                until = time.monotonic() + 30
                while not helper.metadata and time.monotonic() < until:
                    helper.poll()
                    time.sleep(.005)
                assert not helper.ready and helper.metadata.get('error') == expected, (name, helper.metadata)
                report[name] = 'disabled cleanly'
            finally:
                helper.close()
    assert verify_checkpoint(checkpoint) == before
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(destination)], check=True)
    report['signed_bundle_unchanged'] = True
    report['external_cache_files'] = len(list((root / 'empty-hf-cache').rglob('*')))
    report['packaging_passed'] = True
    report['passed'] = (report.get('offline_connection_valid', False) if args.packaging_only else report['ready'])
    target = Path('output/bundle-acceptance') / (root.name + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x') as stream:
        json.dump(report, stream, indent=2)
    print(target, json.dumps(report), flush=True)
    if not report['passed']:
        raise SystemExit('Packaging checks passed, but model readiness did not pass; full evidence retained.')


if __name__ == '__main__':
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    try:
        main()
    finally:
        awake.terminate()
        awake.wait(timeout=5)
