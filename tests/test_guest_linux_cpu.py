import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from types import SimpleNamespace

from adaptive_os.guest_linux.core import Engine, Journal
from adaptive_os.guest_linux.protocol import PROTOCOL, validate
from tests.test_guest_linux import FakeHost


class CpuHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.cpus = '1-3'
        self.usage = 1000

    def identity(self, task):
        row = super().identity(task)
        return {**row, 'cpus_effective': self.cpus} if row else None

    def inspect(self, task):
        row = self.identity(task)
        return {**row, 'cpu_usage_usec': self.usage} if row else None


class GuestCpuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.host = CpuHost()
        self.now = 20.
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)
        self.tasks = [self.command('launch', app=app)['task'] for app in ('firefox', 'geany', 'geany')]
        self.run = None

    def message(self, op, **fields):
        return {'protocol': PROTOCOL, 'request_id': str(uuid.uuid4()), 'op': op, **fields}

    def command(self, op, **fields):
        return self.engine.handle(self.message(op, **fields))

    def begin(self, importance=('High', 'Low', 'Normal')):
        result = self.command('begin_cpu', generation=self.engine.generation, foreground_id=self.tasks[0]['id'],
                              tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': rank}
                                     for t, rank in zip(self.tasks, importance)])
        self.run = result['run_id']
        return self.command('snapshot')

    def observe(self, seconds=10):
        for _ in range(seconds):
            self.now += 1
            self.engine.heartbeat(self.run)
            self.host.usage += 1000
            snapshot = self.command('snapshot')
        return snapshot

    def decision(self, snapshot, action='favor'):
        return self.message('cpu_decide', run_id=self.run, generation=snapshot['generation'],
                            observation_id=snapshot['cpu_control']['observation_id'], action=action)

    def test_favor_lowers_only_eligible_background_and_restores_the_entire_run(self):
        self.begin()
        first = self.engine.handle(self.decision(self.observe()))
        self.assertEqual((first['task_id'], first['weight']), (self.tasks[1]['id'], 50))
        second = self.engine.handle(self.decision(self.observe(30)))
        self.assertEqual((second['task_id'], second['weight']), (self.tasks[1]['id'], 25))
        third = self.engine.handle(self.decision(self.observe(30)))
        self.assertEqual((third['task_id'], third['weight']), (self.tasks[2]['id'], 50))
        self.assertEqual(self.host.weights[self.tasks[0]['id']], 100)
        self.command('restore', run_id=self.run)
        self.assertEqual(list(self.host.weights.values()), [100, 100, 100])
        self.assertEqual(self.engine.state['run']['id'], self.run)
        self.assertEqual(len(self.host.live), 3)

    def test_equal_importance_uses_highest_eligible_weight_then_stable_id(self):
        self.begin(('Normal', 'Normal', 'Normal'))
        first = self.engine.handle(self.decision(self.observe()))
        ids = sorted(t['id'] for t in self.tasks[1:])
        self.assertEqual(first['task_id'], ids[0])
        second = self.engine.handle(self.decision(self.observe(30)))
        self.assertEqual((second['task_id'], second['weight']), (ids[1], 50))
        third = self.engine.handle(self.decision(self.observe(30)))
        self.assertEqual((third['task_id'], third['weight']), (ids[0], 25))

    def test_ten_valid_seconds_and_thirty_second_cooldown_are_enforced_by_service(self):
        self.begin()
        snapshot = self.observe(9)
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.engine.handle(self.decision(snapshot))
        self.engine.handle(self.decision(self.observe(1)))
        with self.assertRaisesRegex(ValueError, 'cooldown'):
            self.engine.handle(self.decision(self.observe(29)))
        self.engine.handle(self.decision(self.observe(1)))
        self.assertEqual(self.host.weights[self.tasks[1]['id']], 25)

    def test_hold_writes_nothing_and_decisions_are_five_seconds_apart(self):
        self.begin()
        ack = self.engine.handle(self.decision(self.observe(), action='hold'))
        self.assertEqual(ack['action'], 'hold')
        self.assertEqual(self.host.writes, [])
        with self.assertRaisesRegex(ValueError, 'five seconds'):
            self.engine.handle(self.decision(self.observe(4)))
        self.engine.handle(self.decision(self.observe(1)))
        self.assertEqual(self.host.weights[self.tasks[1]['id']], 50)

    def test_duplicate_consumed_old_and_restarted_observations_are_rejected(self):
        self.begin()
        message = self.decision(self.observe())
        self.engine.handle(message)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.engine.handle(message)
        message['request_id'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.engine.handle(message)
        fresh = self.decision(self.observe(30))
        fresh['generation'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'generation'):
            self.engine.handle(fresh)
        stale = self.decision(self.command('snapshot'))
        self.now += 3.1
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.engine.handle(stale)
        self.assertEqual(self.host.weights[self.tasks[1]['id']], 50)

    def test_missing_samples_restart_observation_window(self):
        self.begin()
        self.observe(9)
        self.now += 4
        snapshot = self.command('snapshot')
        self.assertEqual(snapshot['cpu_control']['valid_seconds'], 0)
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.engine.handle(self.decision(self.observe(9)))
        self.engine.handle(self.decision(self.observe(1)))

    def test_placement_drift_after_snapshot_prevents_mutation(self):
        self.begin()
        snapshot = self.observe()
        self.host.cpus = '0-3'
        with self.assertRaisesRegex(ValueError, 'placement'):
            self.engine.handle(self.decision(snapshot))
        self.assertEqual(self.host.writes, [])

    def test_background_weight_drift_after_snapshot_prevents_mutation(self):
        self.begin()
        snapshot = self.observe()
        self.host.weights[self.tasks[2]['id']] = 50
        with self.assertRaisesRegex(ValueError, 'weight'):
            self.engine.handle(self.decision(snapshot))
        self.assertEqual(self.host.writes, [])

    def test_invalid_telemetry_never_accumulates_valid_observation_seconds(self):
        self.begin()
        self.observe(9)
        with patch.object(self.host, 'inspect', side_effect=OSError('counter unavailable')):
            snapshot = self.command('snapshot')
        self.assertFalse(snapshot['cpu_control']['ready'])
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.engine.handle(self.decision(self.observe(10)))
        self.engine.handle(self.decision(self.observe(1)))

    def test_begin_rejects_stale_identity_missing_tasks_and_forged_importance(self):
        fields = {'generation': self.engine.generation, 'foreground_id': self.tasks[0]['id'],
                  'tasks': [{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'} for t in self.tasks]}
        for change in ('identity', 'missing', 'importance', 'duplicate', 'generation'):
            with self.subTest(change=change):
                bad = json.loads(json.dumps(fields))
                if change == 'identity':
                    bad['tasks'][0]['identity'] = 'stale'
                elif change == 'missing':
                    bad['tasks'].pop()
                elif change == 'importance':
                    bad['tasks'][0]['importance'] = 'Critical'
                elif change == 'duplicate':
                    bad['tasks'][1] = bad['tasks'][0]
                else:
                    bad['generation'] = str(uuid.uuid4())
                with self.assertRaises(ValueError):
                    self.command('begin_cpu', **bad)
        self.assertIsNone(self.engine.state['run'])
        self.begin()

    def test_cpu_run_cannot_use_legacy_probe_or_change_roles_mid_run(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, 'probe'):
            self.command('probe', run_id=self.run, task_id=self.tasks[0]['id'],
                         identity=self.tasks[0]['identity'], expected_weight=100)
        with self.assertRaisesRegex(ValueError, 'active run'):
            self.begin()
        self.assertEqual(self.host.writes, [])

    def test_partial_operation_and_ack_journal_failure_restore_all_weights(self):
        self.begin()
        self.engine.handle(self.decision(self.observe()))
        ready = self.observe(30)
        event = self.journal.event
        def fail_ack(kind, **fields):
            if kind == 'weight_ack':
                raise sqlite3.OperationalError('disk full')
            return event(kind, **fields)
        with patch.object(self.journal, 'event', side_effect=fail_ack):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine.handle(self.decision(ready))
        self.assertEqual(list(self.host.weights.values()), [100, 100, 100])
        self.assertIsNone(self.engine.state['run'])

    def test_service_restart_restores_a_torn_50_write_and_rejects_old_run(self):
        self.begin()
        message = self.decision(self.observe())
        self.host.crash_after_write = True
        with self.assertRaises(KeyboardInterrupt):
            self.engine.handle(message)
        self.assertEqual(self.host.weights[self.tasks[1]['id']], 50)
        self.journal.close()
        self.journal = Journal(self.path)
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)
        self.assertEqual(list(self.host.weights.values()), [100, 100, 100])
        self.assertIsNone(self.engine.state['run'])
        message['request_id'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'run'):
            self.engine.handle(message)
        snapshot = self.begin()
        self.assertEqual(snapshot['cpu_control']['valid_seconds'], 0)

    def test_controller_loss_restores_25_and_keeps_application_identities(self):
        self.begin()
        self.engine.handle(self.decision(self.observe()))
        self.engine.handle(self.decision(self.observe(30)))
        identities = dict(self.host.live)
        self.now += 10.1
        self.engine.tick()
        self.assertEqual(list(self.host.weights.values()), [100, 100, 100])
        self.assertEqual(self.host.live, identities)
        self.assertIsNone(self.engine.state['run'])

    def test_task_exit_ends_cpu_run_and_restores_remaining_applications(self):
        self.begin()
        self.engine.handle(self.decision(self.observe()))
        del self.host.live[self.tasks[0]['id']]
        self.engine.tick()
        self.assertIsNone(self.engine.state['run'])
        self.assertEqual(self.host.weights[self.tasks[1]['id']], 100)

    def test_protocol_rejects_arbitrary_weights_and_nonfinite_or_untyped_fields(self):
        self.begin()
        message = self.decision(self.observe())
        self.assertEqual(validate(message)['action'], 'favor')
        for field, value in [('action', 'set'), ('action', []), ('observation_id', 1),
                             ('generation', ''), ('weight', 10000), ('cpu', '0')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate({**message, field: value})


class GuestCpuHostTest(unittest.TestCase):
    def test_kernel_weight_allowlist_and_cpu_telemetry_use_the_owned_cgroup(self):
        from adaptive_os.guest_linux.linux import LinuxHost, names
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = Journal(root / 'state.sqlite3')
            self.addCleanup(journal.close)
            host = LinuxHost(journal, {'uid': 1000})
            task = {'id': str(uuid.uuid4()), 'slot': 1, 'app': 'geany', 'identity': 'boot:invocation'}
            unit, ns = names(task)
            group = root / 'adaptiveos.slice' / unit
            group.mkdir(parents=True)
            (group.parent / 'memory.max').write_text('4294967296')
            for name, value in {'cpu.weight': '100', 'cpuset.cpus.effective': '1-3',
                                'cpu.stat': 'usage_usec 123456\nuser_usec 120000\nsystem_usec 3456\n',
                                'cpu.pressure': 'some avg10=4.50 avg60=1.00 avg300=0.10 total=20000\n',
                                'memory.current': '1024', 'memory.low': '0', 'memory.min': '0', 'memory.high': 'max',
                                'memory.stat': 'workingset_refault_file 17\npgscan 40\n',
                                'memory.max': 'max', 'memory.events': 'oom 0\n', 'memory.pressure': '',
                                'cgroup.procs': ''}.items():
                (group / name).write_text(value)
            commands = []
            def kernel(command, **kwargs):
                if command[:2] == ['systemctl', 'show']:
                    self.assertEqual(command[2], unit)
                    return f'LoadState=loaded\nActiveState=active\nInvocationID=invocation\nControlGroup=/adaptiveos.slice/{unit}\nMainPID=123\n'
                self.assertEqual(command[:4], ['systemctl', 'set-property', '--runtime', unit])
                self.assertEqual(journal.events()[-1]['kind'], 'kernel_intent')
                commands.append(command)
                (group / 'cpu.weight').write_text(command[-1].split('=')[1])
                return ''
            read, stat = Path.read_text, os.stat
            host.run = kernel
            with patch('adaptive_os.guest_linux.linux.CGROUP', root), \
                    patch.object(host, 'task_dns', return_value={'nameservers': ['192.168.64.1'], 'private_directory': True}) as resolver, \
                    patch.object(Path, 'read_text', lambda p, *a, **kw: 'boot' if str(p) == '/proc/sys/kernel/random/boot_id' else read(p, *a, **kw)), \
                    patch('os.stat', lambda p, *a, **kw: SimpleNamespace(st_ino=42) if str(p) == '/run/netns/' + ns else stat(p, *a, **kw)):
                self.assertEqual(host.identity(task)['cpus_effective'], '1-3')
                report = host.inspect(task)
                resolver.assert_called_once_with(123)
                self.assertTrue(report['network']['dns']['private_directory'])
                self.assertEqual(report['cpu_usage_usec'], 123456)
                self.assertIn('total=20000', report['cpu_pressure'])
                for weight in (50, 25, 100, 90, 100):
                    host.set_weight(task, weight)
                    self.assertEqual(host.identity(task)['cpu_weight'], weight)
                for weight in (True, 0, 1, 24, 26, 75, 10000, '50'):
                    with self.subTest(weight=weight), self.assertRaises(ValueError):
                        host.set_weight(task, weight)
                self.assertEqual(len(commands), 5)
                self.assertEqual((group / 'cpuset.cpus.effective').read_text(), '1-3')


if __name__ == '__main__':
    unittest.main()
