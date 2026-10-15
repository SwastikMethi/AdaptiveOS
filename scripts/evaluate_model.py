"""Run frozen readiness contexts through the same private helper as the app."""
import argparse
from pathlib import Path
import sys
import time

from adaptive_os.model_readiness import ReadinessRun, frozen_contexts
from adaptive_os.model_runtime import HelperProcess, runtime_paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=('jev', 'laya'), default='laya')
    parser.add_argument('--device', choices=('mps', 'cpu'), default='mps')
    parser.add_argument('--resource', choices=('cpu', 'memory', 'network'), default='cpu')
    parser.add_argument('--output', type=Path, default=Path('output/model-readiness'))
    parser.add_argument('--connection-only', action='store_true')
    parser.add_argument('--bundle', type=Path, help='Evaluate the standalone helper inside a relocated .app')
    parser.add_argument('--helper', type=Path, help='Evaluate a standalone helper before app assembly')
    parser.add_argument('--diagnose-laya', action='store_true', help='Print local Laya build diagnostics to stderr')
    args = parser.parse_args()
    command, checkpoint = runtime_paths()
    if args.bundle:
        contents = args.bundle.resolve() / 'Contents'
        command = [str(contents / 'Helpers' / 'model_helper' / 'model_helper')]
        checkpoint = contents / 'Resources' / 'laya'
    elif args.helper:
        command = [str(args.helper.resolve())]
    config = {'provider': args.provider}
    if args.provider == 'jev':
        import getpass
        config['key'] = getpass.getpass('Jev API key (memory only): ')
    else:
        config.update(checkpoint=str(checkpoint), support=str(args.output / 'working'),
                      device=args.device, warmups=[frozen_contexts(args.resource)[i] for i in (35, 71, 107)])
    if args.diagnose_laya and args.provider != 'laya':
        parser.error('Diagnostics are available only for local Laya')
    if args.diagnose_laya:
        command.append('--diagnose-laya')
    helper = HelperProcess(command, config, diagnostic_stream=sys.stderr if args.diagnose_laya else None)
    run = ReadinessRun(helper, args.provider, args.device if args.provider == 'laya' else 'hosted',
                       resource=args.resource, connection_only=args.connection_only)
    try:
        last = -1
        while not run.done:
            run.poll()
            if len(run.rows) != last:
                last = len(run.rows)
                print(f'{last}/{len(run.contexts)} evaluated', flush=True)
            time.sleep(.005)
        path = run.save(args.output)
        print(path, {key: value for key, value in run.report.items() if key != 'rows'}, flush=True)
        if (run.report['valid_in_deadline'] == 0 if args.connection_only else not run.report['ready']):
            raise SystemExit(1)
    finally:
        helper.close()


if __name__ == '__main__':
    main()
