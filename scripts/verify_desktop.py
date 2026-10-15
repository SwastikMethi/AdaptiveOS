"""Opt-in real Docker verification; only creates/removes its exact session resources."""

import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import time
import uuid

from adaptive_os.sandbox_runtime import SandboxRuntime, LABELS
from adaptive_os.session_store import SessionStore


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
    probe_ids = set()
    observed_rates = set()
    last_heartbeat = 0
    closed = False
    output = Path('build/verification') / session_id
    store = SessionStore(output / 'sessions-v2.sqlite3')
    store.begin(session_id, {'verification': True})
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
                message, receipt_ns = item
                store.record(session_id, message, receipt_ns)
                counts[message['kind']] += 1
                if message['kind'] == 'ack':
                    acks[message['request_id']] = message
                if message['kind'] == 'sample' and message['workload_id'] not in probe_ids:
                    samples[message['workload_id']] = message
                    if message.get('app_rate') is not None:
                        observed_rates.add(message['workload_id'])
                if message['kind'] == 'closed':
                    closed = True
                    return
            store.flush()

    def command(op, ok=True, **fields):
        request = runtime.command(op, **fields)
        store.record(session_id, {'kind': 'control', 'command': request}, time.monotonic_ns())
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
        store.record(session_id, runtime.ready, runtime.ready['receipt_ns'])
        for original, lowered in ((0, 10), (10, 19)):
            probe_id = str(uuid.uuid4())
            probe_ids.add(probe_id)
            probe = command('probe_start', workload_id=probe_id, nice=original)
            command('priority', workload_id=probe_id, identity=probe['identity'], nice=lowered)
            command('priority', workload_id=probe_id, identity=probe['identity'], nice=original)
            command('probe_stop', workload_id=probe_id, identity=probe['identity'])
        preflight = command('preflight_complete')
        assert preflight['priority_supported']
        ids = [str(uuid.uuid4()) for _ in range(args.workloads)]
        presets = ['periodic', 'cpu'] if args.workloads == 2 else ['periodic', 'cpu', 'memory', 'io'] * 2
        for i, p in zip(ids, presets):
            store.configure(session_id, {'id': i, 'name': p, 'preset': p, 'config': {}})
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
        if preflight['priority_supported']:
            for nice in (10, 19, 0):
                assert command('priority', workload_id=ids[0], identity=children[0]['identity'], nice=nice)['nice'] == nice
        else:
            command('priority', ok=False, workload_id=ids[0], identity=children[0]['identity'], nice=10)
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
        deadline = time.monotonic() + 90
        while not set(ids).issubset(observed_rates) and time.monotonic() < deadline:
            pump(1)
        assert set(ids).issubset(observed_rates), 'No application progress within the bounded startup allowance'
        if args.workloads == 8:
            sizes = json.loads(runtime._run(['exec', runtime.container_id, 'python', '-c',
                'import pathlib,json; print(json.dumps([p.stat().st_size for p in pathlib.Path("/scratch").glob("*.bin")]))']))
            assert len(sizes) == 2 and all(size <= 64 * 1024 ** 2 for size in sizes), sizes
        command('stop', workload_id=ids[0])
        pump(2)
        command('priority', ok=False, workload_id=ids[0], identity=children[0]['identity'], nice=10)
        assert samples[ids[1]]['mono_ns'] > samples[ids[0]]['mono_ns']
        if args.workloads == 8:
            for remaining_id in ids[1:]:
                command('stop', workload_id=remaining_id)
            memory_ids = [str(uuid.uuid4()) for _ in range(4)]
            for memory_id in memory_ids:
                command('start', workload_id=memory_id, preset='memory', config={'mib': 128})
            command('start', ok=False, workload_id=str(uuid.uuid4()), preset='memory', config={'mib': 128})
            for memory_id in memory_ids:
                command('stop', workload_id=memory_id)
            command('start', workload_id=str(uuid.uuid4()), preset='cpu', config={})
        if args.lease:
            pump(13, heartbeat=False)
            assert closed, 'Supervisor did not close on heartbeat lease expiry'
        else:
            command('shutdown')
        print('PASS:', dict(counts), flush=True)
        store.finish(session_id, 'completed')
        store.export(session_id, output / 'recording.json', 'json')
        store.export(session_id, output / 'recording.csv', 'csv')
        recorded = json.loads((output / 'recording.json').read_text())
        assert sum(m['kind'] == 'sample' for m in recorded['messages']) == counts['sample']
        print('Recording/export verified:', output, flush=True)
    finally:
        try:
            runtime.cleanup_session(session_id)
        finally:
            runtime.close_transport()
        assert not [s for s in runtime.leftovers() if s['session_id'] == session_id]
        print('Owned container and volume removed.', flush=True)
        store.close()


if __name__ == '__main__':
    main()
