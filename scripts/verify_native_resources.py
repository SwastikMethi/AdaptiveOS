"""Exercise the real Qt/worker/sandbox path; optionally run full resource calibration."""
import argparse
import json
from pathlib import Path
import time
import uuid

from PySide6.QtWidgets import QApplication
from adaptive_os.desktop import MainWindow
from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.experiments import recompute_recording
from adaptive_os.session_store import SessionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('memory', 'network'), required=True)
    parser.add_argument('--calibration', action='store_true')
    args = parser.parse_args()
    output = Path('output/native-resource-verification') / uuid.uuid4().hex
    output.mkdir(parents=True)
    app = QApplication([])
    worker = SessionWorker(output / 'session.sqlite3')
    worker.start()
    window = MainWindow(worker)
    window.setWindowTitle('Adaptive OS Controller — resource verification')
    window.resource_choice.setCurrentIndex(window.resource_choice.findData(args.mode))
    window.show()
    def wait(predicate, timeout=60):
        until = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            if worker.error:
                raise RuntimeError(worker.error)
            if time.monotonic() > until:
                raise TimeoutError(window.status.text() + ' / ' + window.experiment_progress.text())
            time.sleep(.02)
    report = dict(mode=args.mode, native_qt=True, passed=False)
    try:
        if args.calibration:
            window.experiment_kind.setCurrentIndex(window.experiment_kind.findData('resource_calibration'))
            window.start_experiment()
            wait(lambda: window.experiment and window.experiment['phase'] == 'done' and not window.active, 900)
            report['calibration'] = window.experiment['report']
            assert len(window.experiment['results']) == 6, report
            assert all(r['valid'] for r in window.experiment['results']), report
        else:
            window.start_session()
            wait(lambda: window.active and window.preflight_complete)
            window.start_all()
            wait(lambda: not window.pending and all(row['status'] == 'running' for row in window.workloads.values()), 90)
            window.set_auto(True)
            assert window.auto_active, window.status.text()
            wait(lambda: window.policy.latest_applied is not None, 120)
            report['applied'] = window.policy.latest_applied.__dict__
            assert window.policy.latest_applied.action == 'favor'
            assert window.allocation_chart.markers
            assert all(row['nice'] == 0 for row in window.workloads.values())
            assert not window.priority.isEnabled() and not window.resource_choice.isEnabled()
            wait(lambda: any(e.last('resource_effect') for e in window.decision_panel.live.values()), 20)
            window.metric_tabs.setCurrentWidget(window.allocation_chart)
            app.processEvents()
            window.grab().save(str(output / 'live-allocation.png'))
            window.set_auto(False)
            wait(lambda: not window.pending and not window.resource_restoring)
            assert all(row['allocated'] == row['original_allocation'] for row in window.workloads.values())
            # Stop clicked before a manual transaction's acknowledgement must
            # defer until both allocations have been restored.
            window.table.setCurrentCell(0, 0)
            foreground = next(row for row in window.workloads.values() if row['role'] == 'Foreground')
            window.manual_allocation('favor')
            window.stop_selected()
            wait(lambda: foreground['status'] == 'stopped' and not window.pending)
            assert all(row['allocated'] == row['original_allocation'] for row in window.workloads.values())
            report['manual_stop_restoration'] = True
            window.stop_session_requested()
            wait(lambda: not window.active)
        session = window.session_id
        window.grab().save(str(output / 'native.png'))
        window.close()
        wait(lambda: not worker.is_alive())
        store = SessionStore(worker.database)
        for kind in ('json', 'csv'):
            store.export(session, output / f'recording.{kind}', kind)
        recorded = store.load(session)
        store.close()
        recording = json.loads((output / 'recording.json').read_text())
        if args.calibration:
            recomputed = recompute_recording(recording)
            for original, result in zip(report['calibration']['trials'], recomputed['results']):
                for key in ('valid', 'miss_rate', 'per_task_throughput', 'p95_lateness_ms'):
                    assert result[key] == original[key], key
            (output / 'recomputed.json').write_text(json.dumps(recomputed, indent=2))
        replay = MainWindow(SessionWorker(output / 'replay.sqlite3'))
        replay.timer.stop()
        replay.load_recording(recorded)
        replay.show()
        app.processEvents()
        assert replay.resource == args.mode
        assert replay.allocation_chart.series
        replay.grab().save(str(output / 'playback.png'))
        replay.close()
        report.update(passed=True, session=session, raw_recomputation_parity=bool(args.calibration))
    finally:
        if worker.is_alive():
            worker.submit({'op': 'quit'})
            worker.join(10)
        (output / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
        print(output / 'report.json', flush=True)
    assert report['passed']


if __name__ == '__main__':
    main()
