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
            start_id = window.control('start', workload_id)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'start', 'workload_id': workload_id,
                                  'nice': 0, 'request_id': start_id, 'status': 'running'})
            request_id = window.request_priority(workload_id, 10)
            self.assertEqual(row['nice'], 0)
            self.assertIn(request_id, window.pending)
            window.handle_update({'kind': 'ack', 'ok': False, 'op': 'priority', 'request_id': request_id,
                                  'error': 'not supported'})
            self.assertEqual(row['nice'], 0)
            self.assertNotIn(request_id, window.pending)
            request_id = window.request_priority(workload_id, 19)
            window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority', 'request_id': request_id,
                                  'workload_id': workload_id, 'nice': 19})
            self.assertEqual(row['nice'], 19)
            window.active = False
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
