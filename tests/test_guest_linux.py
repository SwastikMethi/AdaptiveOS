import csv
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import sqlite3


class GuestCoreTest(unittest.TestCase):
    def setUp(self):
        from adaptive_os.guest_linux.core import Engine, Journal
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.host = FakeHost()
        self.now = 20.
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)

    def command(self, op, **fields):
        return self.engine.handle({'protocol': 'guest-linux/1', 'request_id': str(uuid.uuid4()),
                                   'op': op, **fields})

    def launch(self):
        return self.command('launch', app='firefox')['task']

    def begin(self):
        return self.command('begin')['run_id']

    def probe(self, task, run_id, **fields):
        return self.command('probe', run_id=run_id, task_id=task['id'],
                            identity=task['identity'], expected_weight=100, **fields)

    def test_probe_reads_kernel_and_restores_captured_setting_without_stopping_app(self):
        task = self.launch()
        run = self.begin()
        ack = self.probe(task, run)
        self.assertEqual(ack['weight'], 90)
        self.assertEqual(self.host.weights[task['id']], 90)
        self.command('end', run_id=run)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.assertIn(task['id'], self.host.live)

    def test_disable_restore_keeps_run_and_allows_another_probe(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.command('restore', run_id=run)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.probe(task, run)
        self.assertEqual(self.host.weights[task['id']], 90)

    def test_controller_lease_expiry_restores_and_requires_new_run(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.now += 9.9
        self.engine.tick()
        self.assertEqual(self.host.weights[task['id']], 90)
        self.now += .2
        self.engine.tick()
        self.assertEqual(self.host.weights[task['id']], 100)
        with self.assertRaisesRegex(ValueError, 'run'):
            self.probe(task, run)

    def test_restart_recovers_write_that_happened_before_ack(self):
        from adaptive_os.guest_linux.core import Engine, Journal
        task = self.launch()
        run = self.begin()
        self.host.crash_after_write = True
        with self.assertRaises(KeyboardInterrupt):
            self.probe(task, run)
        self.assertEqual(self.host.weights[task['id']], 90)
        self.journal.close()
        self.journal = Journal(self.path)
        recovered = Engine(self.journal, self.host, clock=lambda: self.now)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.assertIsNone(recovered.state['run'])
        self.assertEqual(recovered.state['tasks'][0]['identity'], task['identity'])
        self.assertTrue(any(row['kind'] == 'restoration' for row in self.journal.events()))

    def test_failed_readback_rolls_back_and_does_not_ack_success(self):
        task = self.launch()
        run = self.begin()
        self.host.ignore_next_write = True
        with self.assertRaisesRegex(RuntimeError, 'read.back'):
            self.probe(task, run)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.assertTrue(any(row['kind'] == 'restoration' for row in self.journal.events()))

    def test_identity_reuse_is_never_written_during_restore(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.host.live[task['id']] = 'replaced-invocation'
        writes = len(self.host.writes)
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.command('end', run_id=run)
        self.assertEqual(len(self.host.writes), writes)
        self.assertIsNotNone(self.engine.state['run'])

    def test_duplicate_and_stale_requests_cannot_reapply(self):
        task = self.launch()
        run = self.begin()
        request = {'protocol': 'guest-linux/1', 'request_id': str(uuid.uuid4()), 'op': 'probe',
                   'run_id': run, 'task_id': task['id'], 'identity': task['identity'], 'expected_weight': 100}
        self.engine.handle(request)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.engine.handle(request)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.probe(task, run)

    def test_heartbeat_is_independent_and_cannot_resurrect_expired_run(self):
        self.launch()
        run = self.begin()
        self.now += 9
        self.engine.heartbeat(run)
        self.now += 9
        self.engine.tick()
        self.assertIsNotNone(self.engine.state['run'])
        self.now += 11
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.engine.heartbeat(run)
        self.engine.tick()
        self.assertIsNone(self.engine.state['run'])

    def test_failed_restoration_is_retained_and_retried(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.host.fail_restore = True
        self.now += 11
        with self.assertRaises(RuntimeError):
            self.engine.tick()
        self.assertIsNotNone(self.engine.state['run'])
        self.host.fail_restore = False
        self.engine.tick()
        self.assertIsNone(self.engine.state['run'])
        self.assertEqual(self.host.weights[task['id']], 100)

    def test_exited_app_is_cleaned_only_after_restoration(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        del self.host.live[task['id']]
        self.engine.tick()
        self.assertEqual(self.host.cleaned, [task['id']])
        self.assertEqual(self.engine.state['tasks'], [])

    def test_unverified_or_nonbaseline_tasks_cannot_start_probe_run(self):
        task = self.launch()
        self.host.weights[task['id']] = 75
        with self.assertRaisesRegex(ValueError, '100'):
            self.begin()
        self.assertEqual(self.host.weights[task['id']], 75)

    def test_snapshot_remains_available_when_restoration_fails(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.host.fail_restore = True
        self.now += 11
        snapshot = self.command('snapshot')
        self.assertIsNotNone(snapshot['recovery_error'])
        self.assertEqual(snapshot['tasks'][0]['cpu_weight'], 90)

    def test_full_journal_cannot_block_recovery_from_durable_baseline(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.now += 11
        with patch.object(self.journal, 'event', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'journal'):
                self.engine.tick()
            self.assertEqual(self.host.weights[task['id']], 100)
            self.assertIsNotNone(self.engine.state['run'])
        self.engine.tick()
        self.assertIsNone(self.engine.state['run'])

    def test_journal_failure_after_kernel_write_rolls_back_without_success_ack(self):
        task = self.launch()
        run = self.begin()
        event = self.journal.event
        def fail_ack(kind, **fields):
            if kind == 'weight_ack':
                raise sqlite3.OperationalError('disk full')
            return event(kind, **fields)
        with patch.object(self.journal, 'event', side_effect=fail_ack):
            with self.assertRaises(sqlite3.OperationalError):
                self.probe(task, run)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.assertIsNone(self.engine.state['run'])
        self.begin()

    def test_telemetry_failure_cannot_block_controller_loss_restore(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        self.now += 11
        with patch.object(self.host, 'inspect', side_effect=OSError('namespace disappeared')):
            self.engine.tick()
        self.assertEqual(self.host.weights[task['id']], 100)

    def test_restart_restores_when_journal_cannot_record_recovery(self):
        from adaptive_os.guest_linux.core import Engine
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        with patch.object(self.journal, 'event', side_effect=sqlite3.OperationalError('disk full')):
            restarted = Engine(self.journal, self.host, clock=lambda: self.now)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.assertIsNotNone(restarted.recovery_error)
        self.assertIsNotNone(restarted.state['run'])

    def test_failed_final_state_commit_retains_baseline_for_retry(self):
        task = self.launch()
        run = self.begin()
        self.probe(task, run)
        with patch.object(self.journal, 'save', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'journal'):
                self.command('end', run_id=run)
        self.assertEqual(self.host.weights[task['id']], 100)
        self.assertEqual(self.engine.state['run']['id'], run)
        self.engine.tick()
        self.assertIsNone(self.engine.state['run'])


class GuestProtocolTest(unittest.TestCase):
    def test_protocol_has_no_arbitrary_commands_paths_memory_or_network_writes(self):
        from adaptive_os.guest_linux.protocol import validate
        base = {'protocol': 'guest-linux/1', 'request_id': str(uuid.uuid4())}
        for fields in ({'op': 'shell', 'command': 'id'}, {'op': 'memory'},
                       {'op': 'launch', 'app': '/bin/sh'}, {'op': 'launch', 'app': 'firefox', 'argv': []},
                       {'op': 'probe', 'run_id': str(uuid.uuid4()), 'task_id': '../../x',
                        'identity': '1', 'expected_weight': True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate({**base, **fields})
        with self.assertRaises(ValueError):
            validate({**base, 'protocol': 5, 'op': 'snapshot'})

    def test_records_preserve_backend_fields_in_playback_and_both_exports(self):
        from adaptive_os.session_store import SessionStore
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = SessionStore(root / 'recordings.sqlite3')
            self.addCleanup(store.close)
            run = str(uuid.uuid4())
            store.begin(run, {'backend': 'guest-linux', 'protocol': 'guest-linux/1'})
            payload = {'kind': 'guest_snapshot', 'backend': 'guest-linux',
                       'preflight': {'baseline': {'host': {'baseline_id': 'utm-plasma-baseline', 'utm_version': '4.7.5'},
                                                 'hypervisor': 'qemu', 'session_type': 'x11',
                                                 'packages': {'plasma-workspace': '5.27.11'}}}, 'tasks': [{
                'id': str(uuid.uuid4()), 'identity': 'abcdef', 'cpu_weight': 90,
                'cpus_effective': '1-3', 'memory_low': 0, 'memory_high': 'max',
                'memory_max': 'max', 'parent_memory_max': 4294967296,
                'network': {'shaped': False, 'upload_device': 'g-up', 'download_device': 'h-down'}}]}
            store.record(run, payload, 100)
            store.finish(run, 'completed')
            self.assertEqual(store.load(run)['events'], [payload])
            store.export(run, root / 'run.json', 'json')
            store.export(run, root / 'run.csv', 'csv')
            exported = json.loads((root / 'run.json').read_text())['messages'][0]
            with (root / 'run.csv').open() as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(json.loads(row['payload']), payload)
            self.assertEqual({k: v for k, v in exported.items() if k != 'host_receipt_ns'}, payload)


class FakeHost:
    def __init__(self):
        self.live, self.weights, self.writes, self.cleaned = {}, {}, [], []
        self.crash_after_write = self.ignore_next_write = self.fail_restore = False

    def launch(self, task):
        self.live[task['id']] = uuid.uuid4().hex
        self.weights[task['id']] = 100
        return self.inspect(task)

    def inspect(self, task):
        return self.identity(task)

    def identity(self, task):
        if task['id'] not in self.live:
            return None
        return {**task, 'identity': self.live[task['id']], 'cpu_weight': self.weights[task['id']]}

    def set_weight(self, task, weight):
        if self.fail_restore and weight == 100:
            raise RuntimeError('kernel restore unavailable')
        self.writes.append((task['id'], weight))
        if self.ignore_next_write:
            self.ignore_next_write = False
            return
        self.weights[task['id']] = weight
        if self.crash_after_write:
            self.crash_after_write = False
            raise KeyboardInterrupt('power loss after write')

    def restore_weight(self, task, weight):
        self.set_weight(task, weight)

    def cleanup(self, task):
        self.cleaned.append(task['id'])

    def preflight(self):
        return {'passed': True, 'backend': 'guest-linux', 'shaped': False}
