import copy
import unittest

from adaptive_os.guest_linux.core import Engine
from tests.test_guest_linux_memory import GuestResourceTestCase, MemoryHost


class NetworkHost(MemoryHost):
    def __init__(self):
        super().__init__()
        self.shape = {'upload': None, 'download': None}
        self.network_writes = []
        self.fail_download = False
        self.net_bytes = 1000

    def network_configuration(self, tasks):
        return copy.deepcopy(self.shape)

    def set_network(self, tasks, settings, *, restore=False):
        for direction in ('upload', 'download'):
            self.network_writes.append((direction, copy.deepcopy(settings[direction]), restore))
            self.shape[direction] = copy.deepcopy(settings[direction])
            if not restore and self.crash_after_write:
                self.crash_after_write = False
                raise KeyboardInterrupt()
            if direction == 'upload' and self.fail_download and not restore:
                raise RuntimeError('partial download update')

    def network_telemetry(self, tasks):
        return {'settings': self.network_configuration(tasks), 'tasks': {
            t['id']: {'upload_bytes': self.net_bytes, 'download_bytes': self.net_bytes,
                      'upload_drops': 0, 'download_drops': 0} for t in tasks}}


class GuestNetworkTest(GuestResourceTestCase):
    # Reuse only the fixture/API helpers; memory behavior is covered in its own suite.
    def setUp(self):
        super().setUp()
        self.host = NetworkHost()
        for task in self.engine.state['tasks']:
            self.host.launch(task)
            self.host.live[task['id']] = task['identity']
        self.engine.host = self.host

    def begin(self, profile_id=None, scored=False):
        self.run = self.command('begin_control', resource='network', generation=self.engine.generation,
                                foreground_id=self.tasks[0]['id'], profile_id=profile_id, scored=scored,
                                tasks=[{'task_id': t['id'], 'identity': t['identity'], 'importance': 'Normal'}
                                       for t in self.tasks])['run_id']
        return self.command('snapshot')

    def test_equal_shares_five_point_transfer_and_two_distinct_restoration_boundaries(self):
        self.begin()
        fg, bg = (t['id'] for t in self.tasks)
        self.assertEqual(self.host.shape['upload']['shares'], {fg: 5000, bg: 5000})
        self.action(self.observe(10), 'resource_decide', 'favor')
        self.assertEqual(self.host.shape['upload']['shares'], {fg: 5500, bg: 4500})
        self.assertEqual(self.host.shape['download']['shares'], {fg: 5500, bg: 4500})
        self.command('restore', run_id=self.run)
        self.assertEqual(self.host.shape['upload']['shares'], {fg: 5000, bg: 5000})
        self.command('end', run_id=self.run)
        self.assertEqual(self.host.shape, {'upload': None, 'download': None})
        self.assertEqual(len(self.host.live), 2)

    def test_partial_direction_update_restores_both_shapers(self):
        self.begin()
        self.host.fail_download = True
        with self.assertRaisesRegex(RuntimeError, 'partial download'):
            self.action(self.observe(10), 'resource_decide', 'favor')
        self.assertEqual(self.host.shape, {'upload': None, 'download': None})
        self.assertIsNone(self.engine.state['run'])

    def test_service_crash_restores_unshaped_path(self):
        self.begin()
        self.host.crash_after_write = True
        with self.assertRaises(KeyboardInterrupt):
            self.action(self.observe(10), 'resource_decide', 'favor')
        self.engine = Engine(self.journal, self.host, clock=lambda: self.now)
        self.assertEqual(self.host.shape, {'upload': None, 'download': None})
        self.assertIsNone(self.engine.state['run'])

    def test_task_exit_restores_transit_before_cleaning_its_namespace(self):
        self.begin()
        del self.host.live[self.tasks[0]['id']]
        self.engine.tick()
        self.assertEqual(self.host.shape, {'upload': None, 'download': None})
        self.assertIsNone(self.engine.state['run'])

    def test_foreground_maximum_and_donor_floor(self):
        self.begin()
        for _ in range(8):
            self.action(self.observe(30), 'resource_decide', 'favor')
        self.assertEqual(self.host.shape['upload']['shares'][self.tasks[0]['id']], 8000)
        from adaptive_os.guest_linux.resources import favor_network
        shares = {'front': 8000, 'small': 500, 'large': 1500}
        self.assertEqual(favor_network(shares, 'front', {key: 'Normal' for key in shares}), shares)
        shares = {'front': 5000, 'small': 500, 'large': 4500}
        changed = favor_network(shares, 'front', {'front': 'Normal', 'small': 'Low', 'large': 'High'})
        self.assertEqual(changed, {'front': 5500, 'small': 500, 'large': 4000})

    def test_drift_after_observation_prevents_network_writes(self):
        self.begin()
        snapshot = self.observe(10)
        self.host.shape['download'] = None
        count = len(self.host.network_writes)
        with self.assertRaisesRegex(ValueError, 'Network settings'):
            self.action(snapshot, 'resource_decide', 'favor')
        self.assertEqual(len(self.host.network_writes), count)


class GuestNetworkCommandsTest(unittest.TestCase):
    def test_failed_upload_restore_still_attempts_download(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        host = object.__new__(LinuxHost)
        host.network_devices = lambda: [('upload', 'gateway', 'g-up'), ('download', None, 'h-down')]
        commands = []
        def execute(command):
            commands.append(command)
            if 'show' in command:
                return '[{"root": true, "kind": "htb", "handle": "1:"}]'
            if 'g-up' in command:
                raise RuntimeError('upload unavailable')
            return ''
        host.run = execute
        host.network_configuration = Mock(return_value={'upload': None, 'download': None})
        with self.assertRaisesRegex(RuntimeError, 'upload unavailable'):
            host.set_network([], {'upload': None, 'download': None}, restore=True)
        self.assertTrue(any('del' in c and 'h-down' in c for c in commands))

    def test_ubuntu_24_tc_61_text_class_output_preserves_exact_rates(self):
        from adaptive_os.guest_linux.network import parse_classes
        text = ('class htb 1:101 parent 1:1 leaf 101: prio 0 quantum 125000 rate 6666Kbit ceil 20Mbit linklayer ethernet\n'
                ' Sent 987654 bytes 50 pkt (dropped 3, overlimits 0 requeues 0)\n'
                ' backlog 0b 0p requeues 0\n')
        row = parse_classes(text)[0]
        self.assertEqual(row['options'], {'rate': 833250, 'ceil': 2500000})
        self.assertEqual((row['handle'], row['parent'], row['bytes'], row['drops']), ('1:101', '1:1', 987654, 3))
        with self.assertRaises(ValueError):
            parse_classes(text.replace('6666Kbit', 'unknown'))

    def test_common_parent_child_ceil_queue_and_original_ip_classifiers(self):
        from adaptive_os.guest_linux.network import shaping_commands
        tasks = [{'id': 'one', 'slot': 1}, {'id': 'two', 'slot': 2}]
        settings = {'shares': {'one': 5000, 'two': 5000}}
        upload = shaping_commands('aos-gw-test', 'g-up', 'upload', tasks, settings)
        download = shaping_commands(None, 'h-down', 'download', tasks, settings)
        self.assertTrue(all(c[:4] == ['ip', 'netns', 'exec', 'aos-gw-test'] for c in upload))
        self.assertTrue(all(c[0] == 'tc' for c in download))
        for direction, commands in [('src_ip', upload), ('dst_ip', download)]:
            parent = [c for c in commands if '1:1' in c and 'root' not in c and c[c.index('classid')+1] == '1:1']
            self.assertEqual(len(parent), 1)
            self.assertEqual(parent[0][parent[0].index('rate')+1], '20000000bit')
            self.assertEqual(parent[0][parent[0].index('ceil')+1], '20000000bit')
            leaves = [c for c in commands if 'pfifo' in c]
            self.assertEqual(len(leaves), 2)
            self.assertTrue(all(c[-2:] == ['limit', '256'] for c in leaves))
            filters = [c for c in commands if 'flower' in c]
            self.assertEqual([c[c.index(direction)+1] for c in filters], ['10.203.1.2/32', '10.203.2.2/32'])


if __name__ == '__main__':
    unittest.main()
