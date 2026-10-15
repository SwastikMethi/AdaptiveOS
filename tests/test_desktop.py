import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    raise unittest.SkipTest('Install .[desktop] to run the optional Qt tests')
from adaptive_os.desktop import MainWindow
from adaptive_os.charts import TimeSeriesChart
from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.cli import build_parser


class DesktopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_starter_workloads_and_pending_priority_wait_for_ack(self):
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3')
            window = MainWindow(worker)
            self.assertEqual([row['preset'] for row in window.workloads.values()], ['periodic', 'cpu'])
            # Qt Cocoa 6.11 can invalidate sibling interfaces while enumerating
            # an entire selected row. Keep one accessible cell selected.
            self.assertEqual(len(window.table.selectedItems()), 1)
            self.assertEqual(build_parser().parse_args(['desktop']).command, 'desktop')
            workload_id = next(iter(window.workloads))
            row = window.workloads[workload_id]
            window.handle_update({'kind': 'ready', 'session_id': 'test', 'priority_supported': True,
                                  'priority_reason': 'verified', 'workload_cpu': 0, 'collector_cpu': 1,
                                  'mono_ns': 0})
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority_preflight',
                                  'priority_supported': True, 'priority_reason': 'verified'})
            start_id = window.control('start', workload_id)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'start', 'workload_id': workload_id,
                                  'nice': 0, 'pid': 10, 'identity': 123,
                                  'request_id': start_id, 'status': 'running'})
            request_id = window.request_priority(workload_id, 19)
            self.assertEqual(row['nice'], 0)
            self.assertIn(request_id, window.pending)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority', 'request_id': request_id,
                                  'workload_id': workload_id, 'identity': 123, 'nice': 19})
            self.assertEqual(row['nice'], 19)
            window.active = False
            window.close()

    def test_rule_auto_is_preflight_gated_and_issues_identity_bound_change(self):
        import json
        import uuid
        from types import SimpleNamespace
        from adaptive_os.session_store import SessionStore
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3')
            worker.store = SessionStore(worker.database)
            self.addCleanup(worker.store.close)
            worker.session_id, worker.active = str(uuid.uuid4()), True
            worker.store.begin(worker.session_id, {})
            sent = []
            worker.runtime = SimpleNamespace(send=sent.append)

            def drain_commands():
                while not worker.requests.empty():
                    try:
                        worker.handle(worker.requests.get_nowait())
                    except ValueError as exc:
                        self.fail(f'Queued command rejected: {exc}')

            window = MainWindow(worker)
            foreground, background = list(window.workloads)
            window.handle_update({'kind': 'ready', 'session_id': worker.session_id, 'priority_supported': False,
                                  'priority_reason': 'pending', 'workload_cpu': 0, 'collector_cpu': 1,
                                  'mono_ns': 0})
            window.set_auto(True)
            self.assertFalse(window.auto_active)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority_preflight',
                                  'session_id': 'stale', 'priority_supported': True,
                                  'priority_reason': 'passed elsewhere'})
            self.assertFalse(window.preflight_complete)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority_preflight',
                                  'priority_supported': True, 'priority_reason': 'passed'})
            for key, identity in ((foreground, 101), (background, 202)):
                request_id = window.control('start', key)
                window.handle_update({'kind': 'ack', 'ok': True, 'op': 'start', 'workload_id': key,
                    'nice': 0, 'pid': identity, 'identity': identity,
                    'request_id': request_id, 'status': 'running'})
            window.pending['manual'] = {'op': 'priority', 'workload_id': background,
                                        'identity': 202, 'nice': 10}
            window.set_auto(True)
            self.assertFalse(window.auto_active)
            window.pending.pop('manual')
            window.set_auto(True)
            self.assertTrue(window.auto_active)
            for start in (0, 5_000_000_000):
                for offset in range(1_000_000_000, 5_000_000_001, 1_000_000_000):
                    window.handle_update({'kind': 'sample', 'workload_id': background,
                        'mono_ns': start + offset, 'host_receipt_ns': time.monotonic_ns(),
                        'cpu_pct': 50, 'rss_mib': 1, 'nice': 0, 'app_rate': 1, 'app_unit': 'hashes/s'})
                window.handle_update({'kind': 'sample', 'workload_id': foreground,
                    'mono_ns': start + 5_000_000_000, 'host_receipt_ns': time.monotonic_ns(),
                    'cpu_pct': 50, 'rss_mib': 1, 'nice': 0, 'app_rate': 1, 'app_unit': 'hashes/s',
                    'miss_rate': .03, 'window_start_ns': start,
                    'window_end_ns': start + 5_000_000_000})
            command = list(window.pending.values())[-1]
            self.assertEqual((command['op'], command['workload_id'], command['identity'], command['nice']),
                             ('priority', background, 202, 10))
            request_id = command['request_id']
            window.set_auto(False)
            drain_commands()
            self.assertTrue(command['restore_after_ack'])
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority',
                                  'request_id': request_id, 'workload_id': background,
                                  'identity': 202, 'nice': 10, 'mono_ns': 20_000_000_000})
            self.assertEqual(window.rule_state.last_change, 20)
            restoration = list(window.pending.values())[-1]
            self.assertTrue(restoration['restoration'])
            self.assertEqual(restoration['nice'], 0)
            drain_commands()
            priorities = [item for item in sent if item['op'] == 'priority']
            self.assertEqual([item['nice'] for item in priorities], [10, 0])
            for item in priorities:
                self.assertEqual(set(item), {'v', 'request_id', 'op', 'workload_id', 'identity', 'nice'})
                self.assertEqual((item['workload_id'], item['identity']), (background, 202))
            recorded = [json.loads(row['payload'])['command'] for row in worker.store.db.execute(
                "select payload from messages where kind='control' order by id")]
            self.assertEqual([item for item in recorded if item['op'] == 'priority'], priorities)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority',
                                  'request_id': restoration['request_id'], 'workload_id': background,
                                  'identity': 202, 'nice': 0})
            self.assertEqual(window.workloads[background]['nice'], 0)
            self.assertFalse(window.pending)
            window.active = False
            window.close()

    def test_preflight_result_replaces_pending_controller_label(self):
        for passed, reason in ((True, 'Preflight passed'), (False, 'Restore readback mismatch')):
            with self.subTest(passed=passed), tempfile.TemporaryDirectory() as root:
                window = MainWindow(SessionWorker(Path(root) / 'sessions.sqlite3'))
                self.assertIn('pending', window.decision_panel.explanation.text())
                window.handle_update({'kind': 'ack', 'ok': passed, 'op': 'priority_preflight',
                                      'priority_supported': passed, 'priority_reason': reason})
                self.assertNotIn('pending', window.decision_panel.explanation.text())
                self.assertIn(reason, window.decision_panel.explanation.text())
                if not passed:
                    self.assertIn('Auto disabled', window.decision_panel.explanation.text())
                    self.assertFalse(window.auto_button.isEnabled())
                window.close()

    def test_two_missing_foreground_windows_disable_auto(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(SessionWorker(Path(root) / 'sessions.sqlite3'))
            foreground, background = list(window.workloads.values())
            window.active = window.preflight_complete = window.priority_supported = True
            foreground.update(status='running', stale=False, identity=1, original_nice=0)
            background.update(status='running', stale=False, identity=2, original_nice=0)
            with patch('adaptive_os.desktop.time.monotonic_ns', return_value=0):
                window.set_auto(True)
            foreground['stale'] = True
            with patch('adaptive_os.desktop.time.monotonic_ns', return_value=5_000_000_000):
                window.refresh_staleness()
                window.refresh_staleness()
                self.assertEqual(window.rule_state.invalid_windows, 1)
            self.assertTrue(window.auto_active)
            with patch('adaptive_os.desktop.time.monotonic_ns', return_value=10_000_000_000):
                window.refresh_staleness()
            self.assertFalse(window.auto_active)
            window.active = False
            window.close()

    def test_controller_audit_events_use_existing_recording_stream(self):
        from adaptive_os.session_store import SessionStore
        import uuid
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3')
            worker.store = SessionStore(worker.database)
            worker.session_id = str(uuid.uuid4())
            worker.store.begin(worker.session_id, {})
            worker.active = True
            worker.handle({'op': 'audit', 'message': {'kind': 'cpu_decision', 'action': 'hold'}})
            row = worker.store.db.execute('select kind,payload from messages').fetchone()
            self.assertEqual(row['kind'], 'cpu_decision')
            self.assertEqual(__import__('json').loads(row['payload'])['action'], 'hold')
            worker.store.close()

    def test_experiment_retains_background_intervals_across_warmup_and_drain(self):
        # Losing either boundary interval drops coverage below the unchanged 95% gate.
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3')
            window = MainWindow(worker)
            window.timer.stop()
            window.active = True
            foreground, background = list(window.workloads)
            for row in window.workloads.values():
                row['status'] = 'running'
            trial = {'block': 0, 'arm': 'default', 'profile': 'light',
                     'measurement_seconds': 30, 'warmup_seconds': 5}
            window.workloads[foreground]['raw'] = {
                'start_ns': 10_000_000_000, 'mono_ns': 15_000_000_000}
            window.experiment = {'kind': 'calibration', 'phase': 'warmup', 'warmup_end': 5,
                'index': 0, 'trials': [trial], 'samples': [], 'raw_ticks': [],
                'results': [], 'cancelled': False, 'interventions': 0,
                'intervention_targets': [], 'intervention_importances': [],
                'active_trial_ids': [foreground, background]}

            def sample(left, right, units, cpu):
                records = window.experiment.setdefault('raw_records', [])
                previous = records[-1]['data'] if records else None
                if previous is None or previous['mono_ns'] != left * 1_000_000_000:
                    # A new counter instance rather than inventing work across a gap.
                    sequence, cumulative, instance = 0, 0, left * 1_000_000_000
                    window.handle_update({'kind': 'raw', 'workload_id': background, 'data': {
                        'sequence': sequence, 'start_ns': instance, 'mono_ns': instance,
                        'units': cumulative, 'ticks': []}})
                else:
                    sequence, cumulative, instance = previous['sequence'], previous['units'], previous['start_ns']
                window.handle_update({'kind': 'raw', 'workload_id': background, 'data': {
                    'sequence': sequence + 1, 'start_ns': instance,
                    'mono_ns': right * 1_000_000_000, 'units': cumulative + units, 'ticks': []}})
                window.sample({'kind': 'sample', 'workload_id': background,
                    'mono_ns': right * 1_000_000_000, 'cpu_pct': cpu,
                    'app_units_delta': units, 'app_interval_start_ns': left * 1_000_000_000,
                    'app_interval_end_ns': right * 1_000_000_000})

            try:
                sample(10, 12, 20000, 100)  # Wholly outside measurement.
                sample(14, 17, 300, 20)     # Two seconds retained before the UI phase switches.
                with patch('adaptive_os.desktop.time.monotonic', return_value=5):
                    window.tick_experiment()
                self.assertEqual(window.experiment['measurement_start_ns'], 15_000_000_000)
                sample(17, 43, 5200, 40)
                window.experiment['raw_ticks'] = [
                    {'scheduled_ns': n, 'deadline_ns': n + 20_000_000,
                     'completion_ns': n + 10_000_000, 'skipped': False}
                    for n in range(15_000_000_000, 45_000_000_000, 20_000_000)]
                with patch('adaptive_os.desktop.time.monotonic', return_value=35):
                    window.tick_experiment()
                sample(43, 46, 900, 90)     # Two seconds delivered during final drain.
                sample(46, 48, 20000, 100)  # Drain-only work must not inflate throughput.
                with patch('adaptive_os.desktop.time.monotonic', return_value=43):
                    window.tick_experiment()
                result = window.experiment['results'][0]
                self.assertTrue(result['valid'], result['exclusion_reasons'])
                self.assertEqual(result['miss_rate'], 0)
                # 200 + 5200 + 600 units over exactly 30 seconds.
                self.assertEqual(result['background_throughput'], 200)
                self.assertEqual(result['per_task_throughput'], {background: 200})
                self.assertEqual(result['cpu_allocation_pct'], {background: 30})
            finally:
                window.active = False
                window.experiment = None
                window.close()

    def test_experiment_retains_boundary_ticks_across_warmup_and_restart(self):
        for packet_sizes in ((2,), (3,), (1, 2)):
            with self.subTest(packet_sizes=packet_sizes), tempfile.TemporaryDirectory() as root:
                worker = SessionWorker(Path(root) / 'sessions.sqlite3')
                window = MainWindow(worker)
                window.timer.stop()
                window.active = True
                foreground, background = list(window.workloads)
                window.workloads[foreground]['status'] = 'running'
                trial = {'block': 0, 'arm': 'default', 'profile': 'light',
                         'measurement_seconds': 180, 'warmup_seconds': 5}
                window.experiment = {'kind': 'responsiveness', 'phase': 'warmup', 'warmup_end': 5,
                    'index': 0, 'trials': [trial, dict(trial)], 'samples': [], 'raw_ticks': [],
                    'results': [], 'cancelled': False, 'interventions': 0,
                    'intervention_targets': [], 'intervention_importances': [],
                    'active_trial_ids': [foreground]}

                def tick(scheduled):
                    return {'scheduled_ns': scheduled, 'wakeup_ns': scheduled,
                            'completion_ns': scheduled + 10_000_000,
                            'deadline_ns': scheduled + 20_000_000, 'skipped': False}

                sequence = 0
                def deliver(workload_id, ticks, mono_ns):
                    nonlocal sequence
                    window.handle_update({'kind': 'raw', 'workload_id': workload_id,
                        'data': {'sequence': sequence, 'units': sequence * 100,
                                 'start_ns': 10_000_000_000, 'mono_ns': mono_ns, 'ticks': ticks}})
                    sequence += 1

                try:
                    deliver(foreground, [tick(13_980_000_000)], 14_000_000_000)
                    with patch('adaptive_os.desktop.time.monotonic', return_value=5):
                        window.tick_experiment()
                    self.assertEqual(window.experiment['phase'], 'warmup')
                    self.assertEqual(window.experiment['alignment_start_ns'], 15_000_000_000)
                    ticks = [tick(n) for n in range(15_000_000_000, 195_000_000_000, 20_000_000)]
                    offset = 0
                    for size in packet_sizes:
                        packet = ticks[offset:offset + size]
                        deliver(foreground, packet, packet[-1]['deadline_ns'])
                        offset += size
                    deliver(background, [tick(15_500_000_000)], 15_520_000_000)
                    with patch('adaptive_os.desktop.time.monotonic', return_value=5.2):
                        window.tick_experiment()
                    self.assertEqual(window.experiment['raw_ticks'], ticks[:offset])
                    self.assertEqual(window.experiment['measurement_start_ns'], 15_000_000_000)
                    self.assertEqual(window.experiment['measurement_end_ns'], 195_000_000_000)
                    for left in range(offset, 9000, 200):
                        packet = ticks[left:left + 200]
                        deliver(foreground, packet, packet[-1]['deadline_ns'])
                    with patch('adaptive_os.desktop.time.monotonic', return_value=185.2):
                        window.tick_experiment()
                    self.assertEqual(window.experiment['phase'], 'drain')
                    deliver(foreground, [tick(195_000_000_000)], 195_020_000_000)
                    self.assertEqual(window.experiment['raw_ticks'], ticks)
                    with patch('adaptive_os.desktop.time.monotonic', return_value=193.3):
                        window.tick_experiment()
                    result = window.experiment['results'][0]
                    self.assertTrue(result['valid'], result['exclusion_reasons'])
                    self.assertEqual(result['miss_rate'], 0)
                    self.assertEqual(result['p95_lateness_ms'], 0)
                    boundaries = [item['message'] for item in worker.requests.queue
                                  if item.get('op') == 'audit'
                                  and item['message'].get('boundary') in ('measurement_start', 'measurement_end')]
                    self.assertEqual(len(boundaries), 2)
                    for boundary in boundaries:
                        self.assertEqual(boundary['measurement_start_ns'], 15_000_000_000)
                        self.assertEqual(boundary['measurement_end_ns'], 195_000_000_000)
                    request_id = next(iter(window.pending))
                    window.handle_update({'kind': 'ack', 'op': 'stop', 'ok': True,
                        'request_id': request_id, 'workload_id': foreground, 'status': 'stopped'})
                    self.assertEqual(window.experiment['index'], 1)
                    self.assertEqual(window.experiment['raw_ticks'], [])
                    self.assertIsNone(window.experiment['alignment_start_ns'])
                    self.assertNotIn('raw', window.workloads[foreground])
                finally:
                    window.active = False
                    window.experiment = None
                    window.close()

    def test_chart_bounds_and_stale_samples_render_a_gap(self):
        chart = TimeSeriesChart('CPU', '%')
        for n in range(301):
            chart.append('workload', n, float(n))
        self.assertEqual(len(chart.series['workload']), 300)
        chart.append('workload', 302, None)
        self.assertIsNone(chart.series['workload'][-1][1])
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(SessionWorker(Path(root) / 'sessions.sqlite3'))
            workload_id = next(iter(window.workloads))
            window.workloads[workload_id]['status'] = 'running'
            window.handle_update({'kind': 'sample', 'workload_id': workload_id, 'mono_ns': 1_000_000_000,
                                  'host_receipt_ns': time.monotonic_ns() - 4_000_000_000,
                                  'cpu_pct': 90, 'rss_mib': 10, 'nice': 0, 'app_rate': 1, 'app_unit': 'hashes/s'})
            window.refresh_staleness()
            self.assertTrue(window.workloads[workload_id]['stale'])
            self.assertIsNone(window.cpu_chart.series[workload_id][-1][1])
            window.close()

    def test_history_works_with_missing_docker(self):
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3', runtime_factory=lambda: (_ for _ in ()).throw(OSError('no Docker')))
            worker.start()
            worker.submit({'op': 'check'})
            events = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not any(e['kind'] == 'error' for e in events):
                try:
                    events.append(worker.updates.get(timeout=.1))
                except queue.Empty:
                    pass
            worker.submit({'op': 'quit'})
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(any(e['kind'] == 'history' for e in events))
            self.assertTrue(any('no Docker' in e.get('message', '') for e in events))

    def test_unknown_ack_cannot_change_effective_priority(self):
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(SessionWorker(Path(root) / 'sessions.sqlite3'))
            key = next(iter(window.workloads))
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority',
                                  'request_id': 'not-pending', 'workload_id': key, 'nice': 19})
            self.assertEqual(window.workloads[key]['nice'], 0)
            window.close()

    def test_mismatched_automatic_ack_restores_and_stops_session(self):
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(SessionWorker(Path(root) / 'sessions.sqlite3'))
            key = list(window.workloads)[1]
            row = window.workloads[key]
            window.active = window.auto_active = True
            window.priority_supported = window.preflight_complete = True
            row.update(status='running', identity=123, original_nice=0, role='Background')
            request_id = window.request_priority(key, 10)
            window.pending[request_id].update(automatic='lower', decision_time=10)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority',
                                  'request_id': request_id, 'workload_id': list(window.workloads)[0],
                                  'identity': 123, 'nice': 19})
            self.assertFalse(window.auto_active)
            restoration = list(window.pending.values())[-1]
            self.assertTrue(restoration['restoration'])
            self.assertEqual(restoration['nice'], 0)
            self.assertEqual(window.deferred_stop, 'session')
            window.active = False
            window.close()

    def test_session_boundary_clears_auto_state(self):
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(SessionWorker(Path(root) / 'sessions.sqlite3'))
            window.auto_active = True
            window.deferred_stop = 'session'
            window.handle_update({'kind': 'stopped', 'clean': False})
            self.assertFalse(window.auto_active)
            self.assertIsNone(window.deferred_stop)
            window.close()

    def test_database_failure_stops_and_cleans_owned_sandbox(self):
        class Transport:
            cleaned = False
            def command(self, op):
                pass
            def poll_message(self, timeout=0):
                return ({'kind': 'closed', 'sequence': 1, 'mono_ns': 1}, 1)
            def cleanup_session(self, session_id):
                self.cleaned = True
            def close_transport(self):
                pass
        class FullDisk:
            def record(self, *args):
                raise OSError('disk full')
            def finish(self, *args):
                raise OSError('disk full')
            def sessions(self):
                return []
        worker = SessionWorker('/unused')
        worker.runtime, worker.store = Transport(), FullDisk()
        worker.active, worker.session_id = True, 'test'
        worker.fail(OSError('disk full'))
        self.assertTrue(worker.runtime.cleaned)
        self.assertFalse(worker.active)
        self.assertIn('disk full', worker.error)

    def test_closed_envelope_is_recorded_before_transport_disconnect(self):
        from adaptive_os.session_store import SessionStore
        import uuid
        class Disconnected:
            def command(self, op):
                raise OSError('not connected')
            def poll_message(self, timeout=0):
                return ({'kind': 'closed', 'sequence': 1, 'mono_ns': 1}, 1)
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3')
            worker.store = SessionStore(worker.database)
            worker.session_id = str(uuid.uuid4())
            worker.store.begin(worker.session_id, {})
            worker.runtime = Disconnected()
            worker.clock_anchor = (0, 0)
            with self.assertRaises(RuntimeError):
                worker.pump()
            self.assertEqual(worker.store.db.execute('select kind from messages').fetchone()[0], 'closed')
            worker.store.close()

    def test_disconnected_shutdown_preserves_tail_and_reports_missing_close(self):
        from adaptive_os.session_store import SessionStore
        import uuid
        class Disconnected:
            diagnostics = ['attach exited']
            process = None
            cleaned = False
            def __init__(self, closed, connected=False):
                self.connected = connected
                self.items = [({'kind': 'event', 'sequence': 1, 'mono_ns': 1,
                                'message': 'heartbeat lease expired'}, 1)]
                if closed:
                    self.items.append(({'kind': 'closed', 'sequence': 2, 'mono_ns': 2}, 2))
            def command(self, op):
                if not self.connected:
                    raise OSError('not connected')
            def poll_message(self, timeout=0):
                return self.items.pop(0) if self.items else None
            def cleanup_session(self, session_id):
                self.cleaned = True
            def close_transport(self):
                pass
        for closed, connected in ((True, False), (False, False), (False, True)):
            with self.subTest(closed=closed, connected=connected), tempfile.TemporaryDirectory() as root:
                worker = SessionWorker(Path(root) / 'sessions.sqlite3')
                worker.store = SessionStore(worker.database)
                worker.session_id = str(uuid.uuid4())
                worker.store.begin(worker.session_id, {})
                worker.runtime, worker.active, worker.clock_anchor = Disconnected(closed, connected), True, (0, 0)
                with patch('adaptive_os.desktop_worker.time.monotonic', side_effect=range(20)):
                    with self.assertRaisesRegex(RuntimeError, 'closed acknowledgement' if connected else 'not connected'):
                        worker.stop_session('completed')
                messages = [dict(row) for row in worker.store.db.execute('select kind,payload from messages')]
                self.assertEqual(messages[0]['kind'], 'event')
                self.assertEqual(any(m['kind'] == 'closed' for m in messages), closed)
                failures = [m['payload'] for m in messages if m['kind'] == 'failure']
                self.assertTrue(any('attach exited' in m for m in failures))
                if not connected:
                    self.assertTrue(any('not connected' in m for m in failures))
                if not closed:
                    self.assertTrue(any('closed acknowledgement' in m for m in failures))
                self.assertTrue(worker.runtime.cleaned)
                self.assertEqual(worker.store.session(worker.session_id)['status'], 'failed')
                stopped = [m for m in list(worker.updates.queue) if m['kind'] == 'stopped']
                self.assertFalse(stopped[0]['clean'])
                worker.store.close()

    def test_history_pages_bound_rows_and_charts_after_workload_churn(self):
        from adaptive_os.session_store import SessionStore
        from adaptive_os.desktop import workload
        import uuid
        with tempfile.TemporaryDirectory() as root:
            worker = SessionWorker(Path(root) / 'sessions.sqlite3')
            store = SessionStore(worker.database)
            session_id = str(uuid.uuid4())
            store.begin(session_id, {})
            for index in range(24):
                row = workload('cpu', f'CPU {index}')
                store.configure(session_id, row)
                store.record(session_id, {'kind': 'sample', 'workload_id': row['id'],
                             'sequence': index, 'mono_ns': index * 1_000_000_000,
                             'cpu_pct': 50, 'nice': 0}, index)
            window = MainWindow(worker)
            window.load_recording(store.load(session_id))
            self.assertEqual(window.table.rowCount(), 8)
            self.assertEqual(len(window.cpu_chart.series), 8)
            self.assertTrue(window.workload_next.isEnabled())
            window.workload_next.click()
            request = worker.requests.get_nowait()
            self.assertEqual(request['workload_offset'], 8)
            window.load_recording(store.load(session_id, workload_offset=8))
            self.assertEqual([w['name'] for w in window.workloads.values()], [f'CPU {i}' for i in range(8, 16)])
            self.assertEqual(len(window.cpu_chart.series), 8)
            window.load_recording(store.load(session_id, workload_offset=16))
            self.assertFalse(window.workload_next.isEnabled())
            self.assertTrue(window.workload_previous.isEnabled())
            export_path = Path(root) / 'all.json'
            store.export(session_id, export_path, 'json')
            import json
            exported = json.loads(export_path.read_text())
            self.assertEqual(len(exported['workloads']), 24)
            self.assertEqual(len(exported['messages']), 24)
            window.close()
            store.close()


if __name__ == '__main__':
    unittest.main()
