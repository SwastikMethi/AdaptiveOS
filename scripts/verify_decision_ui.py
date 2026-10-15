"""Render 1/3/7-background UI fixtures and prepare isolated native replay data.

All decisions/measurements in these fixtures are simulated, not model benchmarks.
Run from the repository: .venv/bin/python scripts/verify_decision_ui.py
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from pathlib import Path
import time
import uuid

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication
from adaptive_os.cpu_rule import CpuWindow
from adaptive_os.desktop import MainWindow, workload
from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.session_store import SessionStore


class ScriptedHelper:
    ready = True

    def __init__(self):
        self.sent = []
        self.replies = []

    def submit(self, request_id, context):
        self.sent.append((request_id, context))

    def poll(self):
        return self.replies.pop(0) if self.replies else None

    def cancel(self):
        pass

    def close(self):
        pass


def main():
    output = Path('output/decision-ui')
    data = output / 'native-data'
    data.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    store = SessionStore(data / 'sessions-v2.sqlite3')
    results = []
    for backgrounds in (1, 3, 7):
        worker = SessionWorker(data / 'unused.sqlite3')
        w = MainWindow(worker)
        w.timer.stop()
        w.setWindowTitle('Decision UI verification — SIMULATED evidence')
        w.workloads.clear()
        for index in range(backgrounds + 1):
            row = workload('periodic' if index == 0 else 'cpu',
                           '[UI fixture] Foreground' if index == 0 else f'[UI fixture] Background {index}')
            row.update(status='running', identity=index + 1, original_nice=0, nice=0,
                       stale=False, sample={'cpu_pct': 50 if index == 0 else 50 / backgrounds})
            w.add_workload(row)
        w.session_id = str(uuid.uuid4())
        w.active = w.preflight_complete = w.priority_supported = True
        w.preflight_reason = 'UI fixture · simulated evidence only'
        w.origin_ns = 0
        base = time.monotonic() - 60
        clock = [base]
        w.clock_anchor = (0, int(base * 1e9))
        w.model_helper = helper = ScriptedHelper()
        w.policy.helper = helper
        w.policy.clock = lambda: clock[0]
        w.policy_choice.setCurrentIndex(w.policy_choice.findData('jev'))
        foreground = next(iter(w.workloads.values()))
        identity = (foreground['id'], foreground['identity'])
        w.policy.activate(w.session_id, 'ui-fixture', identity, w.current_model_tasks(), 'jev')
        w.auto_active = True
        w.table.setCurrentCell(0, 0)
        w.show_details()
        for seconds in range(60):
            for i, row in enumerate(w.workloads.values()):
                cpu = (80 if 10 < seconds < 45 else 50) if i == 0 else ((20 if 10 < seconds < 45 else 50) / backgrounds)
                w.cpu_chart.append(row['id'], seconds, cpu)

        def snapshot(end, name):
            previous_anchor = w.clock_anchor
            w.clock_anchor = (0, time.monotonic_ns() - int((end + .5) * 1e9))
            for row in w.workloads.values():
                row['receipt_ns'] = time.monotonic_ns()
            w.refresh_decision_activity()
            w.refresh_table()
            w.show()
            for width, height in ((1280, 900), (1000, 740)):
                w.resize(width, height)
                app.processEvents()
                assert (w.width(), w.height()) == (width, height), (w.size(), width, height)
                assert w.cpu_chart.height() >= 160
                assert w.cpu_chart.parentWidget().rect().contains(w.cpu_chart.geometry())
                assert w.decision_panel.timeline.height() >= 90
                assert w.decision_panel.isAncestorOf(w.policy_choice)
                assert w.cpu_chart.mapTo(w, QPoint(0, w.cpu_chart.height())).y() <= w.decision_panel.mapTo(w, QPoint()).y()
                assert w.decision_panel.timeline.mapTo(w, QPoint(0, w.decision_panel.timeline.height())).y() <= w.decision_panel.older.mapTo(w, QPoint()).y()
                w.grab().save(str(output / f'{backgrounds}-backgrounds-{name}-{width}.png'))
            w.resize(1280, 900)
            w.clock_anchor = previous_anchor

        for end, action in ((5, 'hold'), (10, 'lower'), (15, 'lower'), (45, 'restore'), (50, None), (55, None)):
            clock[0] = base + end
            helper.ready = action is not None
            decision = w.policy.observe(CpuWindow(end-5, end, .4, 50, True), w.current_model_tasks(), identity,
                                         foreground_p95_ms=3., work={key: 1000. for key in w.workloads})
            if decision:
                w.apply_policy_decision(decision)
            if action is not None:
                helper.replies.append({'request_id': helper.sent[-1][0], 'proposal': {
                    'action': action, 'probabilities': {a: .8 if a == action else .1 for a in ('hold', 'lower', 'restore')},
                    'confidence': .8, 'model': 'ui-fixture', 'inference_ms': 380, 'input_tokens': 1238}})
                clock[0] += .403
                w.poll_model()
            for request in list(w.pending.values()):
                if request.get('op') == 'priority' and not request.get('queued'):
                    w.handle_update({'kind': 'ack', 'op': 'priority', 'ok': True, 'session_id': w.session_id,
                        'request_id': request['request_id'], 'workload_id': request['workload_id'],
                        'nice': request['nice'], 'identity': request['identity'], 'mono_ns': int((end+.5)*1e9)})
            if end == 45:
                snapshot(end, 'live')
        self_entries = list(w.decision_panel.live.values())
        assert len(self_entries) == 6
        assert any('cooldown' in entry.columns(0, {})[3] for entry in self_entries)
        assert sum(bool(entry.acknowledgement) for entry in self_entries) == 2
        assert w.policy.effective == 'rule'
        snapshot(55, 'fallback')
        assert 'Paused' in w.decision_panel.status_labels['next_call'].text()
        assert all(entry.columns(0, {})[-1] == '—' for entry in self_entries[-2:])
        first_call = self_entries[1]
        details = first_call.details(0, w.decision_panel.names)
        assert f'b{backgrounds} / ' in details
        item = w.decision_panel.items[first_call.cycle_id]
        w.decision_panel.timeline.setCurrentItem(item)
        item.setExpanded(True)
        app.processEvents()
        w.grab().save(str(output / f'{backgrounds}-backgrounds-expanded.png'))

        store.begin(w.session_id, {'ui_fixture': True, 'backgrounds': backgrounds})
        for row in w.workloads.values():
            store.configure(w.session_id, row)
        while not worker.requests.empty():
            req = worker.requests.get_nowait()
            if req['op'] == 'audit':
                store.record(w.session_id, req['message'], req['message']['host_monotonic_ns'])
        for seconds in range(60):
            for i, row in enumerate(w.workloads.values()):
                cpu = (80 if 10 < seconds < 45 else 50) if i == 0 else ((20 if 10 < seconds < 45 else 50) / backgrounds)
                sample = {'kind': 'sample', 'workload_id': row['id'], 'mono_ns': seconds * 10**9,
                          'cpu_pct': cpu, 'rss_mib': 18.5, 'nice': 10 if i and 10 < seconds < 45 else 0,
                          'app_rate': 1000., 'app_unit': 'units/s', 'miss_rate': .4, 'p95_lateness_ms': 3.}
                store.record(w.session_id, sample, int((base + seconds) * 1e9))
        store.finish(w.session_id, 'completed')
        recording = store.load(w.session_id)
        assert len(recording['decisions']['cycles']) == 6
        w.active = False
        w.auto_active = False
        w.load_recording(recording)
        w.refresh_decision_activity()
        app.processEvents()
        w.grab().save(str(output / f'{backgrounds}-backgrounds-replay.png'))
        assert 'read-only' in w.controller_status.text()
        results.append({'backgrounds': backgrounds, 'session_id': w.session_id, 'cycles': 6,
                        'confirmed_changes': 2, 'simulated': True, 'sizes': [[1280, 900], [1000, 740]]})
        w.close()
    store.close()
    (output / 'ui-verification.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
