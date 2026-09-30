"""Opt-in real Docker verification; only creates/removes its exact session resources."""

import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import time
import uuid

from adaptive_os.sandbox_runtime import SandboxRuntime, LABELS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=15)
    parser.add_argument('--workloads', type=int, choices=(2, 8), default=2)
    parser.add_argument('--lease', action='store_true')
    args = parser.parse_args()
    runtime = SandboxRuntime()
    session_id = str(uuid.uuid4())
    counts = Counter()
    acks = {}
    samples = {}
    last_heartbeat = 0
    closed = False
    print('Verification session:', session_id, flush=True)

    def pump(seconds, heartbeat=True):
        nonlocal last_heartbeat, closed
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if heartbeat and time.monotonic() - last_heartbeat >= 1:
                runtime.command('heartbeat')
                last_heartbeat = time.monotonic()
            item = runtime.poll_message(.1)
            if item:
                message, _ = item
                counts[message['kind']] += 1
                if message['kind'] == 'ack':
                    acks[message['request_id']] = message
                if message['kind'] == 'sample':
                    samples[message['workload_id']] = message
                if message['kind'] == 'closed':
                    closed = True
                    return

    def command(op, ok=True, **fields):
        request = runtime.command(op, **fields)
        deadline = time.monotonic() + 5
        while request['request_id'] not in acks and time.monotonic() < deadline:
            pump(.1)
        result = acks.pop(request['request_id'])
        assert result['ok'] is ok, result
        return result

    try:
        runtime.prepare(lambda line: print(line, flush=True))
        runtime.start(session_id)
        print(json.dumps(runtime.ready, indent=2), flush=True)
        ids = [str(uuid.uuid4()) for _ in range(args.workloads)]
        presets = ['periodic', 'cpu'] if args.workloads == 2 else ['periodic', 'cpu', 'memory', 'io'] * 2
        children = [command('start', workload_id=i, preset=p, config={}) for i, p in zip(ids, presets)]
        container_ids = runtime._run(['ps', '-q', '--no-trunc', '--filter', f'label=session_id={session_id}']).splitlines()
        assert container_ids == [runtime.container_id], container_ids
        state = runtime.inspect_container(runtime.container_id, session_id)
        assert state['HostConfig']['Memory'] == 1024 ** 3
        assert state['HostConfig']['ReadonlyRootfs'] and state['Config']['User'] == '65534:65534'
        script = 'import os,json; print(json.dumps([sorted(os.sched_getaffinity(p)) for p in ' + repr([c['pid'] for c in children]) + ']))'
        # Inspect inside the same container; never create a probe container.
        pump(.2)
        affinity = json.loads(runtime._run(['exec', runtime.container_id, 'python', '-c', script]))
        assert affinity == [[runtime.ready['workload_cpu']]] * args.workloads, affinity
        if runtime.ready['priority_supported']:
            for nice in (10, 19, 0):
                assert command('priority', workload_id=ids[0], nice=nice)['nice'] == nice
        else:
            command('priority', ok=False, workload_id=ids[0], nice=10)
        if args.workloads == 8:
            command('start', ok=False, workload_id=str(uuid.uuid4()), preset='cpu', config={})
        remaining = args.seconds
        while remaining > 0:
            duration = min(30, remaining)
            pump(duration)
            remaining -= duration
            print(json.dumps({'remaining_seconds': remaining, 'samples': counts['sample'],
                              'host_maxrss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), flush=True)
        assert set(samples) == set(ids), samples
        assert all(sample['rss_mib'] > 0 for sample in samples.values()), samples
        assert all(sample['app_rate'] is not None for sample in samples.values()), samples
        command('stop', workload_id=ids[0])
        pump(2)
        command('priority', ok=False, workload_id=ids[0], nice=10)
        assert samples[ids[1]]['mono_ns'] > samples[ids[0]]['mono_ns']
        if args.lease:
            pump(13, heartbeat=False)
            assert closed, 'Supervisor did not close on heartbeat lease expiry'
        else:
            command('shutdown')
        print('PASS:', dict(counts), flush=True)
    finally:
        try:
            runtime.cleanup_session(session_id)
        finally:
            runtime.close_transport()
        assert not [s for s in runtime.leftovers() if s['session_id'] == session_id]
        print('Owned container and volume removed.', flush=True)


if __name__ == '__main__':
    main()
