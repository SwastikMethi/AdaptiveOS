"""Profile the pinned source runtime over owned private pipes, without controls.

This is a diagnostic, not readiness evidence: synchronizing MPS after the forward
pass changes timing. The parent waits up to ten seconds to observe late results,
but still marks whether each result meets the production one-second deadline.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def worker():
    from adaptive_os.model_providers import LayaProvider
    original_predict = LayaProvider.predict

    def predict(self, context):
        import resource
        import torch
        stages = []

        def measured(name, function):
            def run(*args, **kwargs):
                start, cpu = time.monotonic(), time.process_time()
                before = resource.getrusage(resource.RUSAGE_SELF)
                try:
                    result = function(*args, **kwargs)
                    if name == 'forward' and self.device == 'mps':
                        torch.mps.synchronize()
                    return result
                finally:
                    after = resource.getrusage(resource.RUSAGE_SELF)
                    stages.append({'stage': name, 'wall_ms': (time.monotonic()-start)*1000,
                        'cpu_seconds': time.process_time()-cpu,
                        'minor_faults': after.ru_minflt-before.ru_minflt,
                        'major_faults': after.ru_majflt-before.ru_majflt})
            return run

        encode, infer = self.agent._encode_state, self.agent._infer
        self.agent._encode_state = measured('encode', encode)
        self.agent._infer = measured('forward', infer)
        try:
            result = original_predict(self, context)
            return {**result, 'diagnostic_stages': stages}
        finally:
            self.agent._encode_state, self.agent._infer = encode, infer

    LayaProvider.predict = predict
    from adaptive_os.model_helper import main
    main()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--readiness', type=Path)
    parser.add_argument('--device', choices=('mps', 'cpu'), default='mps')
    parser.add_argument('--repeats', type=int, default=4)
    args = parser.parse_args()
    if args.worker:
        worker()
        return
    if not args.readiness or not 1 <= args.repeats <= 10:
        parser.error('A readiness report and 1–10 repeats are required')
    from adaptive_os.model_runtime import HelperProcess
    from adaptive_os.model_readiness import configuration_fingerprint, frozen_contexts
    source = json.loads(args.readiness.read_text())
    contexts = []
    for count in (1, 3, 7):
        candidates = [row for row in source['rows'] if len(row.get('context', {}).get('backgrounds', [])) == count]
        failed = [row for row in candidates if not row.get('valid')]
        row = (failed or candidates)[0]
        contexts.append((row['context_index'], row['context']))
    directory = ROOT / 'output/laya-stage-profiles' / uuid.uuid4().hex
    directory.mkdir(parents=True)
    report = {'source_readiness': str(args.readiness), 'runtime': 'pinned source interpreter',
        'fingerprint': configuration_fingerprint('laya', args.device),
        'device': args.device, 'diagnostic_only': True, 'mps_forward_synchronized': args.device == 'mps',
        'production_deadline_ms': 1000, 'observation_limit_seconds': 10,
        'dispatch_cadence_seconds': 5, 'rows': []}

    def host_sample():
        return {name: subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
                for name, command in (('vm_stat', ['/usr/bin/vm_stat']),
                                      ('swap_usage', ['/usr/sbin/sysctl', '-n', 'vm.swapusage']))}

    report['host_before'] = host_sample()
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    helper = None
    try:
        helper = HelperProcess([str(ROOT/'build/laya-venv/bin/python'), str(Path(__file__).resolve()), '--worker'],
            {'provider': 'laya', 'device': args.device,
             'checkpoint': str(ROOT/'dist/Adaptive OS Controller.app/Contents/Resources/laya'),
             'support': str(directory/'working'), 'warmups': [frozen_contexts()[i] for i in (35, 71, 107)]})
        deadline = time.monotonic() + 180
        while not helper.ready and time.monotonic() < deadline:
            helper.poll()
            if helper.metadata.get('kind') == 'startup_error':
                raise RuntimeError('Helper startup failed: ' + helper.metadata.get('error', 'unknown'))
            time.sleep(.005)
        if not helper.ready:
            raise TimeoutError('Helper startup exceeded 180 seconds')
        report['startup'] = dict(helper.metadata)
        next_dispatch = time.monotonic()
        last_response = next_dispatch
        for index, context in contexts * args.repeats:
            while time.monotonic() < next_dispatch:
                time.sleep(min(.05, max(0, next_dispatch-time.monotonic())))
            started = time.monotonic()
            idle_ms = (started - last_response) * 1000
            request = uuid.uuid4().hex
            helper.submit(request, context)
            response = None
            while time.monotonic()-started < 10:
                response = helper.poll()
                if response is not None:
                    break
                time.sleep(.005)
            elapsed = (time.monotonic()-started)*1000
            validation_start = time.monotonic()
            from adaptive_os.decision_controller import ModelProposal
            valid = False
            if response and 'proposal' in response:
                try:
                    ModelProposal.parse(response['proposal'])
                    valid = True
                except ValueError:
                    pass
            validation_ms = (time.monotonic() - validation_start) * 1000
            last_response = time.monotonic()
            row = {'context_index': index, 'backgrounds': len(context['backgrounds']),
                   'latency_ms': elapsed + validation_ms, 'idle_ms': idle_ms, 'validation_ms': validation_ms,
                   'within_production_deadline': valid and elapsed + validation_ms < 1000, 'response': response}
            report['rows'].append(row)
            print(json.dumps(row), flush=True)
            if response is None or response.get('request_id') != request:
                raise TimeoutError('No matching response within diagnostic observation limit')
            next_dispatch = started + 5
    finally:
        if helper:
            helper.close()
        awake.terminate()
        awake.wait(timeout=5)
        report['host_after'] = host_sample()
        (directory/'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
        print(directory/'report.json', flush=True)


if __name__ == '__main__':
    main()
