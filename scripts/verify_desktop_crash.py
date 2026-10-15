"""Force-kill the real session host, verify lease shutdown and scoped recovery."""

import argparse
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.sandbox.protocol import VERSION
from adaptive_os.sandbox_runtime import SandboxRuntime
from adaptive_os.session_store import SessionStore


def child(database, resource='cpu'):
    worker = SessionWorker(database)
    worker.start()
    presets = ('periodic', 'cpu') if resource == 'cpu' else (('cache',) * 4 if resource == 'memory' else ('network',) * 4)
    rows = [{'id': str(uuid.uuid4()), 'name': preset, 'preset': preset,
             'config': {'foreground': index == 0} if resource == 'network' else {}}
            for index, preset in enumerate(presets)]
    worker.submit({'op': 'start_session', 'workloads': rows, 'resource': resource})
    started = 0
    while True:
        message = worker.updates.get(timeout=180)
        if message['kind'] == 'error':
            raise RuntimeError(message['message'])
        if message['kind'] == 'ack' and message.get('op') == ('priority_preflight' if resource == 'cpu' else 'configure'):
            assert message['ok'], message
            for row in rows:
                worker.submit({'op': 'control', 'workload': row, 'command': {
                    'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': 'start', 'workload_id': row['id'],
                    'preset': row['preset'], 'config': row['config']}})
        if message['kind'] == 'ack' and message['op'] == 'start':
            assert message['ok'], message
            started += 1
            if started == len(rows):
                print(json.dumps({'session_id': worker.session_id, 'container_id': worker.runtime.container_id}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', type=Path)
    parser.add_argument('--resource', choices=('cpu', 'memory', 'network'), default='cpu')
    args = parser.parse_args()
    if args.child:
        child(args.child, args.resource)
        return
    output = Path('build/crash-verification') / str(uuid.uuid4())
    output.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, __file__, '--child', str(output / 'sessions-v2.sqlite3'),
                                '--resource', args.resource],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    received = queue.Queue()
    threading.Thread(target=lambda: received.put(process.stdout.readline()), daemon=True).start()
    runtime = SandboxRuntime()
    session_id = None
    try:
        line = received.get(timeout=180)
        assert line, 'Session host exited before ready'
        ready = json.loads(line)
        session_id = ready['session_id']
        print('Force-killing owned session host:', session_id, flush=True)
        time.sleep(3)
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 15
        while runtime.inspect_container(ready['container_id'], session_id)['State']['Running']:
            assert time.monotonic() < deadline, 'Workloads survived heartbeat lease'
            time.sleep(.5)
        store = SessionStore(output / 'sessions-v2.sqlite3')
        assert store.session(session_id)['status'] == 'interrupted'
        assert store.db.execute("SELECT count(*) FROM messages WHERE kind='sample'").fetchone()[0] > 0
        store.close()
        assert session_id in [row['session_id'] for row in runtime.leftovers()]
        print('PASS: sandbox exited, recording recovered as interrupted.', flush=True)
        (output / 'report.json').write_text(json.dumps(dict(passed=True, resource=args.resource,
            session_id=session_id, lease_seconds=10, recording_interrupted=True), indent=2))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            stream.close()
        if session_id:
            runtime.cleanup_session(session_id)
            assert session_id not in [row['session_id'] for row in runtime.leftovers()]
            print('Exact-session resources removed; recording retained at', output, flush=True)


if __name__ == '__main__':
    main()
