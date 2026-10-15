"""Run the unchanged held-out calibration through the desktop controller offscreen.

This supplies real workload evidence, not native computer-use acceptance. It
does not transfer qualification to another running app or bypass its gates.
"""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import subprocess
import time
import uuid

from PySide6.QtWidgets import QApplication
from adaptive_os.desktop import MainWindow
from adaptive_os.desktop_worker import SessionWorker


def main():
    directory = Path('output/heldout-calibration') / uuid.uuid4().hex
    directory.mkdir(parents=True)
    app = QApplication([])
    worker = SessionWorker(directory / 'sessions.sqlite3')
    window = MainWindow(worker)
    window.experiment_kind.setCurrentIndex(window.experiment_kind.findData('heldout_calibration'))
    window.experiment_backgrounds.setValue(3)
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    worker.start()
    try:
        window.start_experiment()
        deadline, previous = time.monotonic() + 900, None
        while window.active or not window.experiment or window.experiment.get('phase') != 'done' or window.busy:
            app.processEvents()
            assert not worker.error, worker.error
            assert time.monotonic() < deadline, window.experiment_progress.text()
            experiment = window.experiment or {}
            state = (experiment.get('index'), experiment.get('phase'))
            if state != previous:
                previous = state
                print(json.dumps({'state': state, 'progress': window.experiment_progress.text()}), flush=True)
            time.sleep(.02)
        report = {**window.experiment['report'], 'verification_surface': 'offscreen desktop controller',
                  'session_id': worker.session_id}
        path = directory / 'report.json'
        path.write_text(json.dumps(report, indent=2))
        recording = directory / 'recording.json'
        worker.submit({'op': 'export', 'session_id': worker.session_id, 'path': recording, 'format': 'json'})
        deadline = time.monotonic() + 30
        while not recording.exists():
            app.processEvents()
            assert time.monotonic() < deadline, 'Export did not finish'
            time.sleep(.02)
        assert not report['cancelled'] and len(report['trials']) == 9, report
        print(json.dumps({'report': str(path), 'valid_trials': sum(row['valid'] for row in report['trials']),
                          'qualified_candidates': report['qualified_candidates']}), flush=True)
    finally:
        worker.submit({'op': 'quit'})
        deadline = time.monotonic() + 30
        while worker.is_alive() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.02)
        worker.join(timeout=1)
        awake.terminate()
        awake.wait(timeout=5)
        window.active = False
        window.close()
        assert not worker.is_alive(), 'Owned worker cleanup did not finish'


if __name__ == '__main__':
    main()
