"""Measure a recorded anonymous context without issuing any control commands.

The diagnostic observes late responses for up to ten seconds so their actual
latency is visible. Production's one-second deadline is never changed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from adaptive_os.model_readiness import frozen_contexts
from adaptive_os.model_runtime import HelperProcess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('context', type=Path)
    parser.add_argument('--bundle', type=Path, default=Path('dist/Adaptive OS Controller.app'))
    parser.add_argument('--repeats', type=int, default=6)
    args = parser.parse_args()
    context = json.loads(args.context.read_text())['context']
    directory = Path('output/laya-live-diagnostics') / uuid.uuid4().hex
    directory.mkdir(parents=True)
    contents = args.bundle.resolve() / 'Contents'
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    helper = HelperProcess([str(contents / 'Helpers/model_helper/model_helper')],
                           {'provider': 'laya', 'device': 'mps',
                            'checkpoint': str(contents / 'Resources/laya'),
                            'support': str(directory.parent / 'working'),
                            'warmup': frozen_contexts()[-1]})
    rows = []
    try:
        deadline = time.monotonic() + 120
        while not helper.ready and time.monotonic() < deadline:
            helper.poll()
            assert helper.metadata.get('kind') != 'startup_error', helper.metadata
            time.sleep(.005)
        assert helper.ready, 'Startup timed out'
        print('cold_start', helper.metadata, flush=True)
        for index in range(args.repeats):
            time.sleep(5)
            request = uuid.uuid4().hex
            started = time.monotonic()
            helper.submit(request, context)
            response = None
            while response is None and time.monotonic()-started < 10:
                response = helper.poll()
                time.sleep(.005)
            elapsed = (time.monotonic()-started)*1000
            row = {'index': index, 'latency_ms': elapsed,
                   'within_production_deadline': elapsed < 1000, 'response': response}
            rows.append(row)
            print(json.dumps(row), flush=True)
            assert response is not None, 'No diagnostic response within ten seconds'
    finally:
        helper.close()
        awake.terminate()
        awake.wait(timeout=5)
        (directory / 'report.json').write_text(json.dumps(
            {'cold_start': helper.metadata, 'source_context': str(args.context), 'rows': rows}, indent=2))
        print(directory / 'report.json', flush=True)


if __name__ == '__main__':
    main()
