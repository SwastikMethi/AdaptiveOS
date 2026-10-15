"""Real sandbox, seven guarded interventions, deterministic private-pipe provider.

This is a test harness, never a production provider or scheduling-quality result.
It uses the native window's controller/acknowledgement/restoration paths offscreen.
"""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import time
import uuid

from PySide6.QtWidgets import QApplication
from adaptive_os.cpu_rule import ManagedTask
from adaptive_os.desktop import MainWindow, workload
from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.model_runtime import HelperProcess


def main():
    directory = Path('output/deterministic-control') / str(uuid.uuid4())
    directory.mkdir(parents=True)
    app = QApplication([])
    worker = SessionWorker(directory / 'sessions.sqlite3')
    window = MainWindow(worker)
    window.workloads.clear()
    foreground = workload('periodic', 'Test foreground', {'budget_ms': 19.9})
    window.add_workload(foreground)
    for index, importance in enumerate(('Low', 'Low', 'Low', 'Normal', 'Normal', 'High', 'High')):
        row = workload('cpu', f'Test background {index + 1}')
        row['importance'] = importance
        window.add_workload(row)
    fake = ('from adaptive_os import model_helper\n'
            'class Deterministic:\n'
            ' def __init__(self,key): pass\n'
            ' def predict(self,context):\n'
            '  return {"action":"lower","probabilities":{"hold":0,"lower":1,"restore":0},'
            '"confidence":1,"model":"deterministic-test-v1","inference_ms":0}\n'
            'model_helper.JevProvider=Deterministic\nmodel_helper.main()\n')
    helper = HelperProcess([sys.executable, '-u', '-c', fake], {'provider': 'jev'})
    window.model_helper = helper
    worker.start()

    def until(predicate, seconds=30):
        end = time.monotonic() + seconds
        while not predicate():
            app.processEvents()
            if worker.error:
                raise AssertionError(worker.error)
            if time.monotonic() > end:
                raise AssertionError('Timed out: ' + window.controller_status.text())
            time.sleep(.01)

    try:
        until(lambda: helper.ready)
        window.start_session()
        until(lambda: window.active and window.preflight_complete, 180)
        assert window.priority_supported, 'Reversibility preflight did not pass'
        window.start_all()
        until(lambda: not window.pending and all(row['status'] == 'running' for row in window.workloads.values()))
        backgrounds = [row for row in window.workloads.values() if row['role'] == 'Background']
        # The explicit test injection does not alter the production readiness gate.
        # "model" in every response identifies this as deterministic-test-v1.
        window.policy.helper = helper
        window.policy.activate(window.session_id, 'deterministic-seven-test',
            (foreground['id'], foreground['identity']),
            [ManagedTask(row['id'], row['importance'], None, 0, 0, row['identity']) for row in backgrounds],
            provider='laya')
        window.auto_active = True
        window.last_observation_host_ns = time.monotonic_ns()
        started, deadline, previous = time.monotonic(), time.monotonic() + 340, -1
        while True:
            app.processEvents()
            assert worker.active and not worker.error, worker.error
            assert window.auto_active, window.controller_status.text()
            count = sum(row['nice'] == 10 for row in backgrounds)
            if count != previous:
                print(json.dumps({'elapsed_s': round(time.monotonic() - started, 2),
                                  'verified_changes': count, 'real_model': False}), flush=True)
                previous = count
            if count == 7 and not window.pending:
                break
            assert time.monotonic() < deadline, window.controller_status.text()
            time.sleep(.01)
        window.set_auto(False)
        until(lambda: not window.pending)
        assert all(row['nice'] == 0 for row in backgrounds)
        assert foreground['nice'] == 0
        window.action('stop_session')
        until(lambda: not window.active and not window.busy)
        path = directory / 'recording.json'
        worker.submit({'op': 'export', 'session_id': worker.session_id, 'path': path, 'format': 'json'})
        until(path.exists)
        recording = json.loads(path.read_text())
        messages = recording['messages']
        ids = {row['id'] for row in backgrounds}
        changes = [message['command'] for message in messages if message['kind'] == 'control'
                   and message['command'].get('op') == 'priority' and message['command'].get('workload_id') in ids]
        acks = [message for message in messages if message['kind'] == 'decision_acknowledgement']
        assert len(changes) == 14 and len(acks) == 7, (len(changes), len(acks))
        assert len({row['workload_id'] for row in changes[:7]}) == 7
        assert all(row['nice'] == 10 for row in changes[:7])
        assert all(row['nice'] == 0 for row in changes[7:])
        assert all(right['acknowledged_at'] - left['acknowledged_at'] >= 30 for left, right in zip(acks, acks[1:]))
        lowered = [window.workloads[row['workload_id']] for row in changes[:7]]
        assert [row['importance'] for row in lowered] == ['Low'] * 3 + ['Normal'] * 2 + ['High'] * 2
        expected_restore = [row['id'] for row in sorted(lowered,
            key=lambda row: (('High', 'Normal', 'Low').index(row['importance']),
                             -next(index for index, item in enumerate(lowered) if item['id'] == row['id'])))]
        assert [row['workload_id'] for row in changes[7:]] == expected_restore
        assert all(row.get('model_request_id') for row in acks)
        report = {'passed': True, 'real_model': False, 'model': 'deterministic-test-v1',
                  'session_id': worker.session_id, 'verified_interventions': 7, 'verified_restorations': 7,
                  'cooldown_seconds': 30, 'foreground_unchanged': True, 'recording': str(path)}
        (directory / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        helper.close()
        worker.submit({'op': 'quit'})
        end = time.monotonic() + 30
        while worker.is_alive() and time.monotonic() < end:
            app.processEvents()
            time.sleep(.01)
        worker.join(timeout=1)
        window.active = False
        window.close()
        assert not worker.is_alive(), 'worker did not clean up'


if __name__ == '__main__':
    main()
