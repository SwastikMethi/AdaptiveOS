import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import queue
import tempfile
import time
import unittest

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


if __name__ == '__main__':
    unittest.main()
