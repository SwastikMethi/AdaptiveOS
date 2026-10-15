"""Offscreen application integration/soak test using the real worker and Docker."""

import argparse
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import resource
import time
import uuid

from PySide6.QtWidgets import QApplication
from adaptive_os.desktop import MainWindow, workload
from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.session_store import SessionStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    output = Path('build/ui-verification') / str(uuid.uuid4())
    app = QApplication([])
    worker = SessionWorker(output / 'sessions-v2.sqlite3')
    window = MainWindow(worker)
    for preset in ('memory', 'io', 'periodic', 'cpu', 'memory', 'io'):
        window.add_workload(workload(preset))
    worker.start()

    def until(condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            if worker.error:
                raise AssertionError(worker.error)
            if time.monotonic() > deadline:
                raise AssertionError('Timed out: ' + window.status.text())
            time.sleep(.02)

    try:
        window.start_session()
        until(lambda: window.active, 180)
        until(lambda: window.preflight_complete)
        window.start_all()
        until(lambda: all(w['status'] == 'running' for w in window.workloads.values()))
        key = next(iter(window.workloads))
        if window.priority_supported:
            for nice in (10, 19, 0):
                window.request_priority(key, nice)
                until(lambda: not window.pending)
                assert window.workloads[key]['nice'] == nice
        start = time.monotonic()
        next_report = start
        memory = []
        while time.monotonic() - start < args.seconds:
            app.processEvents()
            if worker.error:
                raise AssertionError(worker.error)
            assert worker.active
            assert all(len(s) <= 300 for s in window.cpu_chart.series.values())
            assert all(len(w.get('series', [])) <= 300 for w in window.workloads.values())
            if time.monotonic() >= next_report:
                row = {'elapsed': round(time.monotonic() - start),
                       'maxrss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       'ui_queue': worker.updates.qsize(),
                       'points': sum(len(s) for s in window.cpu_chart.series.values())}
                memory.append(row)
                print(json.dumps(row), flush=True)
                next_report += 30
            time.sleep(.02)
        assert len(window.cpu_chart.series) == 8
        assert all(w['sample'].get('rss_mib', 0) > 0 for w in window.workloads.values())
        window.action('stop_session')
        until(lambda: not window.active and not window.busy)
        worker.submit({'op': 'load', 'session_id': worker.session_id})
        until(lambda: window.history_mode)
        assert window.cpu_chart.series
        worker.submit({'op': 'export', 'session_id': worker.session_id, 'path': output / 'recording.json', 'format': 'json'})
        until(lambda: (output / 'recording.json').exists())
        if args.seconds >= 600:
            # After the 300-point rings fill, the UI must plateau, not track session length.
            tail = [r['maxrss'] for r in memory if r['elapsed'] >= 330]
            assert tail[-1] - tail[0] < 40 * 1024 ** 2, memory
        print('PASS: desktop controls, bounded plots, history, export;', output, flush=True)
    finally:
        worker.submit({'op': 'quit'})
        deadline = time.monotonic() + 30
        while worker.is_alive() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.02)
        worker.join(timeout=1)
        assert not worker.is_alive(), 'Worker failed to close'
        window.active = False
        window.close()


if __name__ == '__main__':
    main()
