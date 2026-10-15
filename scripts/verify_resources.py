"""Real Docker checks for owned resource modes; retains JSON/CSV evidence."""
import argparse
import csv
import json
from pathlib import Path
import time
import uuid

from adaptive_os.sandbox_runtime import SandboxRuntime
from adaptive_os.session_store import SessionStore


def verify(mode, backgrounds, lease=False, fault=None):
    runtime = SandboxRuntime()
    session = str(uuid.uuid4())
    output = Path('output/resource-verification') / session
    store = SessionStore(output / 'session.sqlite3')
    store.begin(session, {'resource': mode, 'backgrounds': backgrounds, 'verification': True})
    samples, replies, messages = {}, {}, []
    last_heartbeat = 0.
    def pump(seconds, heartbeats=True):
        nonlocal last_heartbeat
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if heartbeats and time.monotonic() - last_heartbeat >= 1:
                runtime.command('heartbeat')
                last_heartbeat = time.monotonic()
            item = runtime.poll_message(.05)
            if item:
                message, receipt = item
                store.record(session, message, receipt)
                messages.append(message)
                if message['kind'] == 'ack':
                    replies[message['request_id']] = message
                elif message['kind'] == 'sample':
                    samples[message['workload_id']] = message
            store.flush()
    def send(op, ok=True, **fields):
        command = runtime.command(op, **fields)
        store.record(session, {'kind': 'control', 'command': command}, time.monotonic_ns())
        return wait(command, ok), command
    def wait(command, ok=True):
        deadline = time.monotonic() + (30 if command['op'] == 'start' else 5)
        while command['request_id'] not in replies and time.monotonic() < deadline:
            pump(.05)
        result = replies.pop(command['request_id'], None)
        assert result and result['ok'] is ok, (command, result, messages[-5:])
        return result
    passed = False
    try:
        runtime.prepare()
        runtime.start(session)
        store.record(session, runtime.ready, runtime.ready['receipt_ns'])
        send('configure', resource=mode, link_mbps=20)
        count = backgrounds + 1
        amount = 512 // count // 16 * 16 if mode == 'memory' else 10000 // count
        ids = [str(uuid.uuid4()) for _ in range(count)]
        children = []
        for index, key in enumerate(ids):
            config = dict(mib=amount, working_set_mib=amount + 16, seed=20261008 + index) if mode == 'memory' else dict(share=amount, foreground=index == 0)
            row = dict(id=key, name='Foreground' if index == 0 else f'Background {index}', preset='cache' if mode == 'memory' else 'network', config=config)
            store.configure(session, row)
            result, _ = send('start', workload_id=key, preset=row['preset'], config=config)
            children.append(result)
            assert result['allocated'] == amount, result
        inspection = runtime.inspect_container(runtime.container_id, session)
        assert inspection['HostConfig']['NetworkMode'] == 'none'
        assert inspection['HostConfig']['Memory'] == 1024**3
        assert inspection['HostConfig']['ReadonlyRootfs']
        pump(12)
        assert len(samples) == count and all(s['progress_fresh'] for s in samples.values()), samples
        assert all(s['nice'] == 0 for s in samples.values()), samples
        assert all(s['allocated'] == amount for s in samples.values()), samples
        if mode == 'memory':
            assert all(s['rss_mib'] >= amount - 2 for s in samples.values()), samples
        else:
            assert all(s['app_rate'] > 0 for s in samples.values()), samples
        before = dict(samples)
        send('priority', ok=False, workload_id=ids[0], identity=children[0]['identity'], nice=10)
        step = 16 if mode == 'memory' else 500
        changes = [dict(workload_id=ids[i], identity=children[i]['identity'], previous=amount,
                        requested=amount + delta) for i, delta in [(1, -step), (0, step)]]
        op = 'memory_budget' if mode == 'memory' else 'network_share'
        result, command = send(op, foreground_id=ids[0], changes=changes)
        assert result['confirmed'] == {c['workload_id']: c['requested'] for c in changes}
        runtime.send(command)
        assert wait(command)['confirmed'] == result['confirmed']  # Same ID cannot transfer twice.
        pump(6)
        assert samples[ids[0]]['allocated'] == amount + step
        assert samples[ids[1]]['allocated'] == amount - step
        after = dict(samples)
        stale = [dict(c, identity=c['identity'] + 1) for c in changes]
        send(op, ok=False, foreground_id=ids[0], changes=stale)
        reverse = [dict(c, previous=c['requested'], requested=c['previous']) for c in changes]
        send(op, foreground_id=ids[0], changes=reverse)
        pump(2)
        assert all(s['allocated'] == amount and s['nice'] == 0 for s in samples.values()), samples
        if fault:
            assert mode == 'memory', 'Allocation faults require Memory mode'
            pid = children[0]['pid']
            if fault == 'allocation_failure':
                code = ('import resource,pathlib; pid=' + str(pid) + '; '
                    'line=next(x for x in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines() if x.startswith("VmSize:")); '
                    'limit=int(line.split()[1])*1024+8*1024*1024; '
                    'resource.prlimit(pid,resource.RLIMIT_AS,(limit,resource.prlimit(pid,resource.RLIMIT_AS)[1]))')
                runtime._run(['exec', runtime.container_id, 'python', '-c', code])
                failed, _ = send(op, ok=False, foreground_id=ids[0], changes=changes)
                assert failed['rolled_back'] and failed['confirmed'] == {c['workload_id']: amount for c in changes}, failed
                send('shutdown')
                pump(1, heartbeats=False)
            else:
                runtime._run(['exec', runtime.container_id, 'python', '-c',
                              'import os,signal; os.kill(' + str(pid) + ',signal.SIGSTOP)'])
                command = runtime.command(op, foreground_id=ids[0], changes=changes)
                store.record(session, {'kind': 'control', 'command': command}, time.monotonic_ns())
                pump(6, heartbeats=False)
                assert any(m['kind'] == 'closed' for m in messages), 'Missing allocation ack did not stop sandbox'
                assert samples[ids[0]]['allocated'] == amount, 'Recipient grew without an acknowledgement'
        elif lease:
            pump(12, heartbeats=False)
            assert any(m['kind'] == 'closed' for m in messages), 'lease did not clean up'
        else:
            send('stop', workload_id=ids[-1])
            send(op, ok=False, foreground_id=ids[0], changes=[
                dict(workload_id=ids[-1], identity=children[-1]['identity'], previous=amount, requested=amount-step),
                dict(workload_id=ids[0], identity=children[0]['identity'], previous=amount, requested=amount+step)])
            send('shutdown')
            pump(1, heartbeats=False)
        store.export(session, output / 'recording.json', 'json')
        store.export(session, output / 'recording.csv', 'csv')
        exported = json.loads((output / 'recording.json').read_text())
        with (output / 'recording.csv').open() as stream:
            csv_rows = list(csv.DictReader(stream))
        assert len(exported['messages']) == len(csv_rows)
        assert all({k: v for k, v in a.items() if k != 'host_receipt_ns'} == json.loads(b['payload'])
                   for a, b in zip(exported['messages'], csv_rows))
        passed = True
        report = dict(passed=True, mode=mode, backgrounds=backgrounds, lease=lease, fault=fault,
                      before=before, after=after, samples=len([m for m in messages if m['kind'] == 'sample']))
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        print(output / 'report.json', flush=True)
    finally:
        runtime.cleanup_session(session)
        runtime.close_transport()
        store.finish(session, 'completed' if passed else 'failed')
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('memory', 'network'), required=True)
    parser.add_argument('--backgrounds', type=int, choices=(1, 3, 7), default=3)
    parser.add_argument('--lease', action='store_true')
    parser.add_argument('--fault', choices=('allocation_failure', 'missing_ack'))
    args = parser.parse_args()
    verify(args.mode, args.backgrounds, args.lease, args.fault)
