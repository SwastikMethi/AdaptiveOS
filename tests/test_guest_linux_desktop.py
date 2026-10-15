import json
import csv
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.test_guest_linux import FakeHost

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class GuestEntryTest(unittest.TestCase):
    def test_guest_rejects_wayland_missing_display_and_headless_qt_before_startup(self):
        from adaptive_os.guest_linux.desktop import main
        for environment in ({'XDG_SESSION_TYPE': 'wayland', 'DISPLAY': ':0', 'XDG_CURRENT_DESKTOP': 'KDE'},
                            {'XDG_SESSION_TYPE': 'x11', 'XDG_CURRENT_DESKTOP': 'KDE'},
                            {'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':0', 'XDG_CURRENT_DESKTOP': 'KDE', 'QT_QPA_PLATFORM': 'offscreen'}):
            with self.subTest(environment=environment), patch.dict('os.environ', environment, clear=True), \
                    patch('sys.platform', 'linux'), patch('os.geteuid', return_value=1000), \
                    patch('os.sched_setaffinity', create=True) as affinity, patch('builtins.print'), \
                    patch('adaptive_os.guest_linux.desktop.QApplication') as qt:
                qt.instance.side_effect = AssertionError('Invalid session reached Qt startup')
                self.assertEqual(main(), 2)
                affinity.assert_not_called()

    def test_backend_choice_defaults_to_docker_and_forwards_guest(self):
        from adaptive_os.cli import build_parser, main
        self.assertEqual(build_parser().parse_args(['desktop']).backend, 'docker')
        with patch('adaptive_os.desktop.main', return_value=0) as desktop:
            self.assertEqual(main(['desktop', '--backend', 'guest-linux', '--jev-env-file', 'private.env']), 0)
            self.assertIn('--backend', desktop.call_args.args[0])
            self.assertIn('guest-linux', desktop.call_args.args[0])
            self.assertIn('private.env', desktop.call_args.args[0])

    @unittest.skipIf(sys.platform == 'linux', 'Checks the macOS host rejection')
    def test_guest_cannot_fall_back_to_docker_or_use_experimental_override(self):
        for extra in ([], ['--allow-unqualified-jev-demo']):
            result = subprocess.run([sys.executable, '-m', 'adaptive_os.desktop', '--backend', 'guest-linux', *extra],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('guest', result.stderr.lower())
            self.assertNotIn('unrecognized arguments', result.stderr)


class GuestWorkerTest(unittest.TestCase):
    def test_desktop_jev_applies_model_decisions_without_qualification_reports(self):
        from adaptive_os.guest_linux.desktop import GuestWorker
        from tests.test_guest_linux_policy import snapshot

        class Helper:
            ready = True
            request_id = None
            def submit(self, request_id, context): self.request_id = request_id
            def poll(self):
                return {'request_id': self.request_id, 'proposal': {
                    'action': 'favor', 'probabilities': {'hold': 0., 'favor': 1., 'restore': 0.},
                    'confidence': 1., 'model': 'jev-1.13.0', 'inference_ms': 100}} if self.request_id else None
            def close(self): self.ready = False

        for resource in ('cpu', 'memory', 'network'):
            with self.subTest(resource=resource), tempfile.TemporaryDirectory() as folder:
                source = snapshot()
                control = source.pop('cpu_control')
                control.update(resource=resource, scored=False)
                if resource == 'network':
                    control['expected'] = {d: {'shares': {t['id']: 5000 for t in source['tasks']}}
                                           for d in ('upload', 'download')}
                source['resource_control'] = control
                for task in source['tasks']:
                    task['cpu_pressure'] = 'some avg10=0.0 total=100\n'
                worker = GuestWorker(Path(folder) / 'recording.sqlite3', jev_key='test-only-key')
                now, events, actions = [10.], [], []
                worker.policy.clock = lambda: now[0]
                self.addCleanup(worker.policy.close)
                with patch('adaptive_os.guest_linux.policy.new_helper', return_value=Helper()):
                    try:
                        worker.policy.set_mode('jev', source, resource)
                    except ValueError as exc:
                        self.fail(f'Interactive Jev was blocked by qualification: {exc}')
                def apply(op, **fields):
                    actions.append((op, fields))
                    return {'action': fields['action']}
                worker.policy.tick(source, apply, lambda **event: events.append(event))
                now[0] += .1
                worker.policy.tick(source, apply, lambda **event: events.append(event))
                self.assertEqual([fields['action'] for _, fields in actions], ['favor'])
                audit = next(e for e in events if e['kind'] == 'guest_model_audit')
                self.assertEqual(audit['mode'], 'jev')
                self.assertTrue(audit['valid'])
                self.assertTrue(audit['kernel_guard_passed'])
                self.assertEqual(worker.policy.fallbacks, 0)
                self.assertFalse((Path(folder) / 'readiness').exists())

    def test_desktop_jev_still_requires_credentials_and_scored_run_qualification(self):
        from adaptive_os.guest_linux.desktop import GuestWorker
        from tests.test_guest_linux_policy import snapshot
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recording.sqlite3')
            with self.assertRaisesRegex(ValueError, 'key'):
                worker.policy.set_mode('jev', snapshot())
            source = snapshot()
            source['cpu_control']['scored'] = True
            worker.policy.key = 'test-only-key'
            with self.assertRaisesRegex(ValueError, 'readiness'):
                worker.policy.set_mode('jev', source)
            self.assertEqual(worker.policy.mode, 'manual')
            worker.policy.mode = 'jev'
            events = []
            worker.policy.tick(source, lambda op, **fields: {'action': fields.get('action')},
                               lambda **event: events.append(event))
            self.assertTrue(any(e.get('fallback_reason') == 'qualification_expired' for e in events))
            self.assertFalse(any(e['kind'] == 'guest_model_request' for e in events))

    def test_memory_worker_profiles_and_exports_distinct_limits_and_parent_restoration(self):
        from adaptive_os.guest_linux.core import Engine, Journal
        from adaptive_os.guest_linux.desktop import GuestWorker
        from adaptive_os.guest_linux.protocol import PROTOCOL
        from adaptive_os.session_store import SessionStore
        from tests.test_guest_linux_memory import MemoryHost
        import uuid
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = Journal(root / 'journal.sqlite3')
            self.addCleanup(journal.close)
            now, host = [20.], MemoryHost()
            engine = Engine(journal, host, clock=lambda: now[0])
            def transport(op, **fields):
                return engine.handle({'protocol': PROTOCOL, 'request_id': str(uuid.uuid4()), 'op': op, **fields})
            tasks = [transport('launch', app=app)['task'] for app in ('firefox', 'geany')]
            worker = GuestWorker(root / 'recording.sqlite3', transport=transport)
            worker.start()
            self.addCleanup(lambda: (worker.stop.set(), worker.join(5)))
            def await_op(op):
                while True:
                    event = worker.updates.get(timeout=5)
                    if event['op'] == 'error': self.fail(event['error'])
                    if event['op'] == op: return event['result']
            current = await_op('snapshot')
            worker.submit('begin_control', resource='memory', generation=current['generation'],
                          foreground_id=tasks[0]['id'], profile_id=None, scored=False,
                          tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'} for t in tasks])
            run = await_op('begin_control')['run_id']
            worker.submit('snapshot')
            await_op('snapshot')
            for _ in range(30):
                now[0] += 1
                engine.heartbeat(run)
                worker.submit('snapshot')
                current = await_op('snapshot')
            worker.submit('memory_profile', generation=current['generation'],
                          observation_id=current['resource_control']['observation_id'], action='establish')
            self.assertTrue(await_op('memory_profile')['profile']['suitable'])
            worker.submit('end')
            await_op('end')
            worker.stop.set()
            worker.join(5)
            self.assertIsNone(worker.error)
            self.assertEqual(host.memory[None]['low'], 0)
            store = SessionStore(root / 'recording.sqlite3')
            self.addCleanup(store.close)
            store.export(worker.session_id, root / 'guest.json', 'json')
            store.export(worker.session_id, root / 'guest.csv', 'csv')
            messages = json.loads((root / 'guest.json').read_text())['messages']
            with (root / 'guest.csv').open() as stream:
                exported = [json.loads(row['payload']) for row in csv.DictReader(stream)]
            self.assertEqual(exported, [{k: v for k, v in m.items() if k != 'host_receipt_ns'} for m in messages])
            self.assertTrue(any(m.get('resource_control', {}).get('resource') == 'memory' for m in messages if m.get('resource_control')))

    def test_cpu_run_shutdown_and_exports_retain_roles_observations_and_acknowledgements(self):
        from adaptive_os.guest_linux.core import Engine, Journal
        from adaptive_os.guest_linux.desktop import GuestWorker
        from adaptive_os.guest_linux.protocol import PROTOCOL
        from adaptive_os.session_store import SessionStore
        from tests.test_guest_linux_cpu import CpuHost
        import uuid
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = Journal(root / 'journal.sqlite3')
            self.addCleanup(journal.close)
            host, now = CpuHost(), [20.]
            engine = Engine(journal, host, clock=lambda: now[0])
            def transport(op, **fields):
                return engine.handle({'protocol': PROTOCOL, 'request_id': str(uuid.uuid4()), 'op': op, **fields})
            tasks = [transport('launch', app=app)['task'] for app in ('firefox', 'geany')]
            worker = GuestWorker(root / 'recordings.sqlite3', transport=transport)
            worker.start()
            self.addCleanup(lambda: (worker.stop.set(), worker.join(5)))
            def await_op(op):
                while True:
                    event = worker.updates.get(timeout=5)
                    if event['op'] == 'error':
                        self.fail(event['error'])
                    if event['op'] == op:
                        return event['result']
            snapshot = await_op('snapshot')
            worker.submit('begin_cpu', generation=snapshot['generation'], foreground_id=tasks[0]['id'],
                          tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'} for t in tasks])
            run = await_op('begin_cpu')['run_id']
            self.assertEqual(worker.run_id, run)
            worker.submit('snapshot')
            await_op('snapshot')
            for _ in range(10):
                now[0] += 1
                engine.heartbeat(run)
                worker.submit('snapshot')
                snapshot = await_op('snapshot')
            worker.submit('cpu_decide', generation=snapshot['generation'],
                          observation_id=snapshot['cpu_control']['observation_id'], action='favor')
            self.assertEqual(await_op('cpu_decide')['weight'], 50)
            worker.stop.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(worker.error)
            self.assertEqual(list(host.weights.values()), [100, 100])
            store = SessionStore(root / 'recordings.sqlite3')
            self.addCleanup(store.close)
            data = store.load(worker.session_id)
            self.assertEqual(data['session']['status'], 'completed')
            self.assertEqual(data['session']['metadata']['milestone'], 5)
            self.assertTrue(any(e.get('cpu_control', {}).get('foreground_id') == tasks[0]['id']
                                for e in data['events'] if e.get('cpu_control')))
            self.assertTrue(any(e.get('op') == 'cpu_decide' and e.get('weight') == 50 for e in data['events']))
            self.assertTrue(any(e.get('op') == 'end' and e.get('restoration') for e in data['events']))
            store.export(worker.session_id, root / 'recording.json', 'json')
            store.export(worker.session_id, root / 'recording.csv', 'csv')
            exported = json.loads((root / 'recording.json').read_text())['messages']
            with (root / 'recording.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([json.loads(row['payload']) for row in rows],
                             [{k: v for k, v in row.items() if k != 'host_receipt_ns'} for row in exported])

    def test_controller_shutdown_restores_apps_and_records_acknowledgements(self):
        from adaptive_os.guest_linux.core import Engine, Journal
        from adaptive_os.guest_linux.desktop import GuestWorker
        from adaptive_os.guest_linux.protocol import PROTOCOL
        from adaptive_os.session_store import SessionStore
        import uuid
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            journal = Journal(directory / 'journal.sqlite3')
            self.addCleanup(journal.close)
            host = FakeHost()
            engine = Engine(journal, host)
            def transport(op, **fields):
                return engine.handle({'protocol': PROTOCOL, 'request_id': str(uuid.uuid4()), 'op': op, **fields})
            worker = GuestWorker(directory / 'recordings.sqlite3', transport=transport)
            worker.start()
            self.addCleanup(lambda: (worker.stop.set(), worker.join(timeout=5)))
            def await_op(op):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    event = worker.updates.get(timeout=5)
                    if event['op'] == 'error':
                        self.fail(event['error'])
                    if event['op'] == op:
                        return event['result']
                self.fail('No acknowledgement: ' + op)
            await_op('snapshot')
            worker.submit('launch', app='firefox')
            task = await_op('launch')['task']
            worker.submit('begin')
            await_op('begin')
            worker.submit('probe', task_id=task['id'], identity=task['identity'], expected_weight=100)
            await_op('probe')
            self.assertEqual(host.weights[task['id']], 90)
            worker.stop.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(host.weights[task['id']], 100)
            self.assertIn(task['id'], host.live)
            store = SessionStore(directory / 'recordings.sqlite3')
            self.addCleanup(store.close)
            recording = store.load(worker.session_id)
            self.assertEqual(recording['session']['metadata']['backend'], 'guest-linux')
            self.assertEqual(recording['session']['status'], 'completed')
            self.assertTrue(any(e['kind'] == 'guest_ack' and e['op'] == 'end' for e in recording['events']))


class GuestCpuWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_rejected_policy_returns_to_active_mode_and_error_survives_snapshots(self):
        from adaptive_os.guest_linux.desktop import GuestWindow, GuestWorker
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recording.sqlite3')
            window = GuestWindow(worker)
            self.addCleanup(window.close)
            self.addCleanup(window.timer.stop)
            source = {'tasks': [], 'preflight': {'passed': True}, 'recovery_error': None,
                      'controller_policy': {'requested': 'manual'}, 'capabilities': []}
            worker.updates.put({'op': 'snapshot', 'result': source})
            window.poll()
            window.policy.setCurrentIndex(window.policy.findData('jev'))
            worker.updates.put({'op': 'error', 'failed_op': 'policy', 'policy_mode': 'manual',
                                'error': 'A Jev key is required; it is never recorded'})
            worker.updates.put({'op': 'snapshot', 'result': source})
            window.poll()
            self.assertEqual(window.policy.currentData(), 'manual')
            self.assertIn('A Jev key is required', window.status.text())
            worker.updates.put({'op': 'snapshot', 'result': source})
            window.poll()
            self.assertIn('A Jev key is required', window.status.text())
            worker.updates.put({'op': 'policy', 'result': {'mode': 'rule'}})
            worker.updates.put({'op': 'snapshot', 'result': {
                **source, 'controller_policy': {'requested': 'rule'}}})
            window.poll()
            self.assertNotIn('A Jev key is required', window.status.text())
            self.assertEqual(window.policy.currentData(), 'rule')
            self.assertIn('Rule', window.active_policy.text())

    def test_memory_presentation_keeps_pressure_and_limits_readable(self):
        from adaptive_os.guest_linux.desktop import GuestWindow, GuestWorker
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recording.sqlite3')
            window = GuestWindow(worker)
            self.addCleanup(window.close)
            self.addCleanup(window.timer.stop)
            window.resource.setCurrentText('memory')
            task = {'id': 'firefox', 'identity': 'instance', 'app': 'firefox',
                    'memory_current': 800 * 1024**2, 'memory_low': 1024 * 1024**2,
                    'memory_high': 1280 * 1024**2,
                    'memory_pressure': 'some avg10=1.25 avg60=0.42 avg300=0.10 total=123456789\n'}
            source = {'tasks': [task], 'generation': 'generation', 'run_id': None,
                      'preflight': {'passed': True}, 'recovery_error': None,
                      'capabilities': ['memory_control']}
            worker.updates.put({'op': 'snapshot', 'result': source})
            window.poll()
            self.assertEqual([window.table.item(0, i).text() for i in (4, 5, 6, 9)],
                             ['800.0 MiB', '1024 MiB', '1280 MiB', '1.25%'])
            for column in (5, 6, 9, 10, 11):
                self.assertIn('\n', window.table.horizontalHeaderItem(column).text())
            window.resize(1040, 950)
            window.show()
            self.app.processEvents()
            self.assertLessEqual(window.minimumSizeHint().width(), 1040)

    def test_network_presentation_rates_and_activity_are_bound_to_run_and_task_identity(self):
        from adaptive_os.guest_linux.desktop import GuestWindow, GuestWorker
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recording.sqlite3')
            window = GuestWindow(worker)
            self.addCleanup(window.close)
            self.addCleanup(window.timer.stop)
            worker.run_id = 'run'
            task = {'id': 'firefox', 'identity': 'instance-1', 'app': 'firefox', 'cpu_weight': 100,
                    'network': {'upload_bytes': 1000, 'download_bytes': 1000}}
            control = {'resource': 'network', 'foreground_id': 'firefox', 'importance': {'firefox': 'Normal'},
                       'expected': {d: {'shares': {'firefox': 5500}} for d in ('upload', 'download')}}
            snapshot = {'generation': 'generation', 'run_id': 'run', 'observed_at_ns': 1_000_000_000,
                        'tasks': [task], 'resource_control': control, 'preflight': {'passed': True},
                        'recovery_error': None, 'capabilities': ['cpu_weight_control']}
            activity = {'generation': 'generation', 'run_id': 'run', 'identities': {'firefox': 'instance-1'},
                        'source': 'Jev', 'at_ns': time.time_ns(),
                        'event': {'proposal': {'action': 'favor'}, 'action': 'favor', 'kernel_guard_passed': True,
                                  'acknowledgement': {'action': 'favor'}}}
            worker.updates.put({'op': 'snapshot', 'result': snapshot})
            window.poll()
            self.assertEqual(window.table.item(0, 13).text(), '—')
            current = {**snapshot, 'observed_at_ns': 2_000_000_000,
                       'tasks': [{**task, 'network': {'upload_bytes': 126000, 'download_bytes': 1251000}}],
                       'controller_policy': {'activity': activity, 'requested': 'manual'}}
            worker.updates.put({'op': 'snapshot', 'result': current})
            window.poll()
            self.assertEqual(window.table.item(0, 12).text(), '1.00')
            self.assertEqual(window.table.item(0, 13).text(), '10.00')
            self.assertEqual(window.table.item(0, 8).text(), '55.00%')
            self.assertIn('Jev · Favor', window.decision.text())
            self.assertIn('Linux acknowledged', window.decision_detail.text())
            self.assertTrue(window.table.isColumnHidden(5))
            self.assertTrue(window.setup.isHidden())
            window.policy.setCurrentIndex(window.policy.findData('jev'))
            worker.updates.put({'op': 'snapshot', 'result': current})
            window.poll()
            self.assertEqual(window.policy.currentData(), 'jev')
            window.resize(1040, 950)
            window.show()
            self.app.processEvents()
            self.assertLessEqual(window.minimumSizeHint().width(), 1040)
            for change in ({'run_id': 'different'}, {'generation': 'different'},
                           {'tasks': [{**task, 'identity': 'instance-2'}]}):
                worker.updates.put({'op': 'snapshot', 'result': {**current, **change}})
                window.poll()
                self.assertNotIn('Jev', window.decision.text())
                self.assertEqual(window.table.item(0, 13).text(), '—')

    def test_decision_display_distinguishes_fallback_bounds_rejection_and_manual_restore(self):
        from adaptive_os.guest_linux.desktop import GuestWindow, GuestWorker
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recording.sqlite3')
            window = GuestWindow(worker)
            self.addCleanup(window.close)
            self.addCleanup(window.timer.stop)
            worker.store = Mock()
            worker.run_id = 'run'
            worker.latest = {'generation': 'generation', 'run_id': 'run', 'tasks': []}
            event = {'kind': 'guest_model_audit', 'mode': 'jev', 'valid': True,
                     'proposal': {'action': 'favor'}, 'action': 'favor', 'kernel_guard_passed': True,
                     'acknowledgement': {'action': 'hold'}}
            worker.audit(**event)
            snapshot = {**worker.latest, 'controller_policy': {'activity': worker.activity}}
            window.render_activity(snapshot)
            self.assertIn('Jev · Favor', window.decision.text())
            self.assertIn('Applied: Hold', window.decision_detail.text())
            worker.audit(**{**event, 'valid': False, 'fallback_reason': 'deadline'})
            window.render_activity({**snapshot, 'controller_policy': {'activity': worker.activity}})
            self.assertIn('Rule fallback', window.decision.text())
            self.assertIn('deadline', window.decision_detail.text())
            worker.audit(**{**event, 'valid': False, 'fallback_reason': 'stale_observation',
                            'kernel_guard_passed': False, 'acknowledgement': None})
            window.render_activity({**snapshot, 'controller_policy': {'activity': worker.activity}})
            self.assertIn('Not applied', window.decision_detail.text())
            worker.run_id = None
            worker.show_activity({'action': 'end', 'acknowledgement': {'restoration': []}}, 'Manual')
            window.render_activity({**snapshot, 'run_id': None, 'controller_policy': {'activity': worker.activity}})
            self.assertIn('Manual · End run', window.decision.text())
            self.assertIn('Applied: Restore', window.decision_detail.text())

    def test_foreground_and_importance_stay_bound_to_task_ids_after_rows_reorder(self):
        from adaptive_os.guest_linux.desktop import GuestWindow, GuestWorker
        import uuid
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recordings.sqlite3')
            window = GuestWindow(worker)
            self.addCleanup(window.close)
            self.addCleanup(window.timer.stop)
            tasks = [{'id': str(uuid.uuid4()), 'identity': str(uuid.uuid4()), 'app': name, 'cpu_weight': 100,
                      'cpus_effective': '1-3'} for name in ('firefox', 'geany')]
            snapshot = {'tasks': tasks, 'generation': str(uuid.uuid4()), 'preflight': {'passed': True},
                        'recovery_error': None, 'run_id': None, 'cpu_control': None,
                        'capabilities': ['launch', 'cpu_weight_control']}
            worker.updates.put({'op': 'snapshot', 'result': snapshot})
            window.poll()
            window.foreground.setCurrentIndex(1)
            window.table.cellWidget(0, 1).setCurrentText('Low')
            worker.updates.put({'op': 'snapshot', 'result': {**snapshot, 'tasks': list(reversed(tasks))}})
            window.poll()
            self.assertEqual(window.foreground.currentData(), tasks[1]['id'])
            window.send('begin_cpu')
            op, fields = worker.requests.get_nowait()
            self.assertEqual(op, 'begin_cpu')
            self.assertEqual(fields['foreground_id'], tasks[1]['id'])
            config = {t['task_id']: t for t in fields['tasks']}
            self.assertEqual(config[tasks[0]['id']]['importance'], 'Low')
            self.assertEqual(config[tasks[1]['id']]['identity'], tasks[1]['identity'])

    def test_favor_is_disabled_until_service_readiness_and_sends_observation_binding(self):
        from adaptive_os.guest_linux.desktop import GuestWindow, GuestWorker
        import uuid
        with tempfile.TemporaryDirectory() as folder:
            worker = GuestWorker(Path(folder) / 'recordings.sqlite3')
            window = GuestWindow(worker)
            self.addCleanup(window.close)
            self.addCleanup(window.timer.stop)
            worker.run_id = str(uuid.uuid4())
            control = {'foreground_id': 'foreground', 'importance': {}, 'observation_id': str(uuid.uuid4()),
                       'ready': False, 'valid_seconds': 9, 'cooldown_seconds': 0, 'decision_wait_seconds': 0}
            snapshot = {'tasks': [], 'generation': str(uuid.uuid4()), 'preflight': {'passed': True},
                        'recovery_error': None, 'run_id': worker.run_id, 'cpu_control': control,
                        'capabilities': ['cpu_weight_control']}
            worker.updates.put({'op': 'snapshot', 'result': snapshot})
            window.poll()
            self.assertFalse(window.buttons['Favor foreground'][0].isEnabled())
            self.assertFalse(window.foreground.isEnabled())
            control = {**control, 'ready': True, 'valid_seconds': 10}
            worker.updates.put({'op': 'snapshot', 'result': {**snapshot, 'cpu_control': control}})
            window.poll()
            self.assertTrue(window.buttons['Favor foreground'][0].isEnabled())
            window.send('cpu_decide')
            op, fields = worker.requests.get_nowait()
            self.assertEqual(op, 'cpu_decide')
            self.assertEqual(fields, {'generation': snapshot['generation'],
                                     'observation_id': control['observation_id'], 'action': 'favor'})
