import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from adaptive_os.guest_linux.core import Engine, Journal
from adaptive_os.guest_linux.protocol import PROTOCOL
from tests.test_guest_linux_cpu import CpuHost

MIB = 1024 ** 2
GIB = 1024 ** 3


class MemoryHost(CpuHost):
    def __init__(self):
        super().__init__()
        self.memory = {None: {'low': 0, 'min': 0, 'high': 'max', 'max': 4 * GIB}}
        self.current, self.events = {}, {}
        self.pressure = 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
        self.bad_hierarchy = False
        self.memory_writes = []
        self.fail_memory_write = None

    def launch(self, task):
        self.memory[task['id']] = {'low': 0, 'min': 0, 'high': 'max', 'max': 'max'}
        self.current[task['id']] = 128 * MIB
        self.events[task['id']] = {'high': 0, 'oom': 0, 'oom_kill': 0, 'max': 0}
        return super().launch(task)

    def preflight(self):
        return {'passed': True, 'baseline': {'fingerprint': 'test-vm'}}

    def memory_settings(self, task=None):
        return dict(self.memory[task['id'] if task else None])

    def memory_configuration(self, tasks):
        if self.bad_hierarchy:
            raise ValueError('Memory ancestor hierarchy differs')
        return {'parent': self.memory_settings(),
                'tasks': {t['id']: self.memory_settings(t) for t in tasks}}

    def memory_parent(self):
        return {**self.memory_settings(), 'current': sum(self.current.values()),
                'pressure': self.pressure, 'events': {'high': 0, 'oom': 0, 'oom_kill': 0, 'max': 0}}

    def set_memory(self, task, settings, *, restore=False):
        key = task['id'] if task else None
        self.memory_writes.append((key, dict(settings), restore))
        self.memory[key].update(settings)
        if not restore and self.fail_memory_write == len(self.memory_writes):
            raise RuntimeError('partial memory operation')
        if not restore and self.crash_after_write:
            self.crash_after_write = False
            raise KeyboardInterrupt()

    def inspect(self, task):
        row = super().inspect(task)
        if row:
            row.update({f'memory_{k}': v for k, v in self.memory[task['id']].items()})
            row.update(memory_current=self.current[task['id']], memory_events=dict(self.events[task['id']]),
                       memory_pressure=self.pressure, memory_stat={'workingset_refault_file': 0, 'pgscan': 0})
        return row


class GuestResourceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / 'journal.db')
        self.addCleanup(self.journal.close)
        self.host, self.now = MemoryHost(), 20.
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)
        self.tasks = [self.command('launch', app=a)['task'] for a in ('firefox', 'geany')]
        self.run = None

    def message(self, op, **fields):
        return {'protocol': PROTOCOL, 'request_id': str(uuid.uuid4()), 'op': op, **fields}

    def command(self, op, **fields):
        return self.engine.handle(self.message(op, **fields))

    def begin(self, profile_id=None, scored=False):
        self.run = self.command('begin_control', resource='memory', generation=self.engine.generation,
                                foreground_id=self.tasks[0]['id'], profile_id=profile_id, scored=scored,
                                tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'}
                                       for t in self.tasks])['run_id']
        return self.command('snapshot')

    def observe(self, seconds=30):
        self.command('snapshot')
        for _ in range(seconds):
            self.now += 1
            self.engine.heartbeat(self.run)
            self.host.usage += 1000
            snapshot = self.command('snapshot')
        return snapshot

    def action(self, snapshot, op='memory_profile', action='establish'):
        return self.command(op, run_id=self.run, generation=snapshot['generation'],
                            observation_id=snapshot['resource_control']['observation_id'], action=action)


class GuestMemoryTest(GuestResourceTestCase):
    def test_throttling_invalidates_profile_across_gaps_restore_and_service_restart(self):
        self.begin()
        profile = self.action(self.observe())['profile']
        self.command('end', run_id=self.run)
        self.begin(profile['id'], scored=True)
        self.observe(10)
        self.host.events[self.tasks[0]['id']]['high'] += 1
        self.assertTrue(self.observe(1)['resource_control']['headroom_required'])
        self.assertFalse(self.journal.metadata('memory_profile:' + profile['id'])['valid'])
        self.now += 4
        self.engine.heartbeat(self.run)
        self.command('restore', run_id=self.run)
        snapshot = self.command('snapshot')
        self.assertTrue(snapshot['resource_control']['headroom_required'])
        self.assertFalse(snapshot['resource_control']['scoring_valid'])
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.begin(profile['id'], scored=True)

    def test_peak_profile_independent_highs_pool_and_restore_boundaries(self):
        original = copy.deepcopy(self.host.memory)
        self.begin()
        self.assertEqual(self.host.memory[None], {'low': 3 * GIB, 'min': 0, 'high': 'max', 'max': 4 * GIB})
        self.host.current[self.tasks[0]['id']] = 1400 * MIB
        self.observe(15)
        self.host.current[self.tasks[0]['id']] = 900 * MIB
        report = self.action(self.observe(15))
        self.assertTrue(report['profile']['suitable'])
        fg, bg = (self.host.memory[t['id']] for t in self.tasks)
        self.assertEqual(fg['high'], 1792 * MIB)
        self.assertEqual(bg['high'], 384 * MIB)
        self.assertEqual((fg['low'], bg['low']), (384 * MIB, 384 * MIB))
        self.action(self.observe(10), 'resource_decide', 'favor')
        self.assertEqual(fg['low'], 448 * MIB)  # Undistributed protection first.
        self.assertEqual(bg['high'], 384 * MIB)
        self.command('restore', run_id=self.run)
        self.assertEqual(fg['low'], 384 * MIB)
        self.assertEqual(fg['high'], 1792 * MIB)
        self.command('end', run_id=self.run)
        self.assertEqual(self.host.memory, original)
        self.assertEqual(len(self.host.live), 2)

    def test_pressure_or_throttling_cannot_establish_profile(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, '30 valid'):
            self.action(self.observe(29))
        self.host.events[self.tasks[0]['id']]['high'] += 1
        snapshot = self.observe(1)
        with self.assertRaisesRegex(ValueError, '30 valid'):
            self.action(snapshot)
        self.host.pressure = 'some avg10=2.00 total=20\nfull avg10=0.10 total=1\n'
        with self.assertRaisesRegex(ValueError, '30 valid'):
            self.action(self.observe(35))
        self.assertTrue(all(self.host.memory[t['id']]['high'] == 'max' for t in self.tasks))

    def test_headroom_that_does_not_fit_is_unsuitable_and_retains_max(self):
        self.begin()
        for task in self.tasks:
            self.host.current[task['id']] = 1800 * MIB
        profile = self.action(self.observe())['profile']
        self.assertFalse(profile['suitable'])
        self.assertIn('envelope', profile['reason'])
        self.assertTrue(all(self.host.memory[t['id']]['high'] == 'max' for t in self.tasks))
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.action(self.observe(10), 'resource_decide', 'favor')

    def test_growth_only_raises_high_and_invalidates_frozen_scoring(self):
        self.begin()
        profile = self.action(self.observe())['profile']
        self.command('end', run_id=self.run)
        self.begin(profile['id'], scored=True)
        self.host.current[self.tasks[0]['id']] = 300 * MIB
        snapshot = self.observe(10)
        self.assertTrue(snapshot['resource_control']['headroom_required'])
        self.assertFalse(snapshot['resource_control']['scoring_valid'])
        self.action(snapshot, action='grow')
        self.assertEqual(self.host.memory[self.tasks[0]['id']]['high'], 576 * MIB)
        self.assertEqual(self.host.memory[self.tasks[1]['id']]['high'], 384 * MIB)
        self.assertFalse(self.command('snapshot')['resource_control']['scoring_valid'])
        self.command('end', run_id=self.run)
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.begin(profile['id'], scored=True)

    def test_partial_operation_rolls_back_children_and_parent(self):
        self.host.fail_memory_write = 2
        original = copy.deepcopy(self.host.memory)
        with self.assertRaisesRegex(RuntimeError, 'partial'):
            self.begin()
        self.assertEqual(self.host.memory, original)
        self.assertIsNone(self.engine.state['run'])

    def test_restore_failure_does_not_skip_other_children_or_parent(self):
        self.begin()
        self.action(self.observe())
        original = self.host.set_memory
        def fail_one(task, settings, *, restore=False):
            if restore and task and task['id'] == self.tasks[0]['id']:
                raise RuntimeError('one child unavailable')
            return original(task, settings, restore=restore)
        with patch.object(self.host, 'set_memory', side_effect=fail_one):
            with self.assertRaisesRegex(RuntimeError, 'one child'):
                self.command('end', run_id=self.run)
        self.assertEqual(self.host.memory[self.tasks[1]['id']]['low'], 0)
        self.assertEqual(self.host.memory[None]['low'], 0)
        self.assertIsNotNone(self.engine.state['run'])
        self.engine.tick()
        self.assertIsNone(self.engine.state['run'])

    def test_interrupted_profile_recovers_parent_and_children_from_journal(self):
        original = copy.deepcopy(self.host.memory)
        self.begin()
        ready = self.observe()
        self.host.crash_after_write = True
        with self.assertRaises(KeyboardInterrupt):
            self.action(ready)
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)
        self.assertEqual(self.host.memory, original)
        self.assertIsNone(self.engine.state['run'])
        records = self.journal.db.execute("SELECT payload FROM metadata WHERE name LIKE 'memory_profile:%'").fetchall()
        import json
        self.assertTrue(records)
        self.assertTrue(all(not json.loads(row[0])['valid'] for row in records))

    def test_headroom_failure_stays_visible_until_an_upward_adjustment(self):
        self.begin()
        self.action(self.observe())
        self.observe(10)
        self.host.events[self.tasks[0]['id']]['high'] += 1
        self.assertTrue(self.observe(1)['resource_control']['headroom_required'])
        snapshot = self.observe(5)
        self.assertTrue(snapshot['resource_control']['headroom_required'])
        self.action(snapshot, action='grow')
        self.assertGreater(self.host.memory[self.tasks[0]['id']]['high'], 384 * MIB)

    def test_controller_loss_and_hierarchy_rejection(self):
        original = copy.deepcopy(self.host.memory)
        self.host.bad_hierarchy = True
        with self.assertRaisesRegex(ValueError, 'ancestor'):
            self.begin()
        self.assertEqual(self.host.memory_writes, [])
        self.host.bad_hierarchy = False
        self.begin()
        self.action(self.observe())
        self.now += 10.1
        self.engine.tick()
        self.assertEqual(self.host.memory, original)

    def test_no_arbitrary_limits_and_stale_observations(self):
        self.begin()
        snapshot = self.observe()
        fields = {'run_id': self.run, 'generation': snapshot['generation'],
                  'observation_id': snapshot['resource_control']['observation_id'], 'action': 'establish'}
        with self.assertRaises(ValueError):
            self.command('memory_profile', **fields, high=1)
        fields['generation'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'generation'):
            self.command('memory_profile', **fields)
        fields['generation'] = snapshot['generation']
        self.now += 3.1
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.command('memory_profile', **fields)

    def test_full_pool_transfers_only_protection_and_respects_donor_minimum(self):
        from adaptive_os.guest_linux.resources import favor_memory
        rows = {'front': {'low': 1536 * MIB, 'high': 2048 * MIB},
                'back': {'low': 1536 * MIB, 'high': 2048 * MIB}}
        result = favor_memory(rows, 'front', {'front': 'High', 'back': 'Low'})
        self.assertEqual((result['front']['low'], result['back']['low']), (1600 * MIB, 1472 * MIB))
        self.assertEqual([row['high'] for row in result.values()], [2048 * MIB, 2048 * MIB])
        self.assertEqual(sum(row['low'] for row in result.values()), 3 * GIB)


class GuestMemoryHostTest(unittest.TestCase):
    def test_owned_direct_hierarchy_kernel_readback_and_limit_validation(self):
        from adaptive_os.guest_linux.linux import LinuxHost, names
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = Journal(root / 'journal.db')
            self.addCleanup(journal.close)
            host = LinuxHost(journal, {'uid': 1000})
            task = {'id': str(uuid.uuid4()), 'identity': 'boot:unit', 'slot': 1, 'app': 'geany'}
            unit = names(task)[0]
            group = root / 'adaptiveos.slice' / unit
            group.mkdir(parents=True)
            for directory in (group.parent, group):
                for key, value in {'low': 0, 'min': 0, 'high': 'max', 'max': 4 * GIB if directory == group.parent else 'max'}.items():
                    (directory / ('memory.' + key)).write_text(str(value))
            identity = {**task, 'cgroup': '/adaptiveos.slice/' + unit, 'unit': unit}
            def kernel(command, **kw):
                destination = group if command[3] == unit else group.parent
                for setting in command[4:]:
                    key, value = setting.split('=')
                    (destination / ('memory.' + key.removeprefix('Memory').lower())).write_text('max' if value == 'infinity' else value)
                return ''
            with patch('adaptive_os.guest_linux.linux.CGROUP', root), patch.object(host, 'identity', return_value=identity), \
                    patch.object(host, 'run', side_effect=kernel):
                before = host.memory_configuration([task])
                self.assertEqual(before['ancestors'][task['id']], ['/', '/adaptiveos.slice', identity['cgroup']])
                settings = {'low': 256 * MIB, 'min': 0, 'high': 768 * MIB, 'max': 'max'}
                host.set_memory(task, settings)
                self.assertEqual(host.memory_settings(task), settings)
                with self.assertRaises(ValueError):
                    host.set_memory(task, {**settings, 'max': GIB})
                host.set_memory(task, before['tasks'][task['id']], restore=True)
                self.assertEqual(host.memory_configuration([task]), before)
                identity['cgroup'] = '/user.slice/adaptiveos.slice/' + unit
                with self.assertRaisesRegex(ValueError, 'ancestor'):
                    host.memory_configuration([task])
if __name__ == '__main__':
    unittest.main()
