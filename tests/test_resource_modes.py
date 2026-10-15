"""Resource boundaries and real workload behavior, independent of Docker."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from adaptive_os.sandbox.protocol import VERSION, validate_command, validate_config
from adaptive_os import model_readiness as readiness


def transfer(resource='memory', previous=128, requested=112):
    foreground, donor = str(uuid.uuid4()), str(uuid.uuid4())
    return dict(v=VERSION, request_id=str(uuid.uuid4()), op=resource + '_budget' if resource == 'memory' else 'network_share',
        foreground_id=foreground, changes=[
            dict(workload_id=donor, identity=12, previous=previous, requested=requested),
            dict(workload_id=foreground, identity=11, previous=previous, requested=2 * previous - requested)])


class ResourceContractTest(unittest.TestCase):
    def test_network_transfer_cannot_relabel_background_as_foreground(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from adaptive_os.sandbox.agent import Supervisor
        command = transfer('network', 2500, 2000)
        supervisor = Supervisor.__new__(Supervisor)
        supervisor.resource, supervisor.transaction = 'network', None
        supervisor.seen_allocations = {}
        supervisor.children = {c['workload_id']: SimpleNamespace(identity=c['identity'], preset='network',
            allocated=c['previous'], start_command=None, config={'foreground': False}) for c in command['changes']}
        supervisor.owned_child = supervisor.children.__getitem__
        supervisor.relay, supervisor.emit = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, 'foreground'):
            supervisor.transfer_allocation(command)
        supervisor.relay.update_shares.assert_not_called()

    def test_transfer_is_closed_identity_bound_and_conserves_pool(self):
        for resource, before, after in [('memory', 128, 112), ('network', 2500, 2000)]:
            command = transfer(resource, before, after)
            self.assertEqual(validate_command(command), command)
            for field, bad in [('identity', True), ('requested', before - 1), ('workload_id', 'host-pid')]:
                value = json.loads(json.dumps(command))
                value['changes'][0][field] = bad
                with self.assertRaises(ValueError):
                    validate_command(value)
            value = json.loads(json.dumps(command))
            value['changes'][1]['requested'] += 16
            with self.assertRaises(ValueError):
                validate_command(value)

    def test_cache_config_enforces_chunk_pool_and_seed_bounds(self):
        self.assertEqual(validate_config('cache', {})['mib'], 128)
        self.assertEqual(validate_config('network', {})['share'], 2500)
        for mib in (0, 15, 17, 528, True):
            with self.assertRaises(ValueError):
                validate_config('cache', {'mib': mib})

    def test_resource_fingerprint_cannot_reuse_cpu_or_other_runtime_readiness(self):
        cpu = readiness.configuration_fingerprint('laya', 'mps')
        memory = readiness.configuration_fingerprint('laya', 'mps', resource='memory')
        network = readiness.configuration_fingerprint('laya', 'mps', resource='network')
        self.assertEqual(len({cpu, memory, network}), 3)
        self.assertNotEqual(memory, readiness.configuration_fingerprint('laya', 'cpu', resource='memory'))
        for resource in ('memory', 'network'):
            corpus = readiness.frozen_contexts(resource)
            self.assertEqual(len(corpus), 108)
            self.assertEqual({len(c['backgrounds']) for c in corpus}, {1, 3, 7})
            self.assertTrue(all(c['resource'] == resource for c in corpus))
            self.assertTrue(all(set(c['available_actions']) <= {'hold', 'favor', 'restore'} for c in corpus))

    def test_new_full_failure_overrides_old_pass_but_connection_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for index, (kind, ready, count) in enumerate([('full', True, 108), ('full', False, 12), ('connection', True, 1)]):
                report = dict(fingerprint='same', ready=ready, contexts=count, validation_kind=kind, completed_at_ns=index)
                (path / f'readiness-{index}.json').write_text(json.dumps(report))
            self.assertFalse(readiness.latest_reports(path)['same']['ready'])

    def test_touching_an_old_pass_cannot_supersede_completed_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            older, newer = directory / 'readiness-pass.json', directory / 'readiness-fail.json'
            older.write_text(json.dumps(dict(fingerprint='same', ready=True, contexts=108,
                validation_kind='full', completed_at_ns=100)))
            newer.write_text(json.dumps(dict(fingerprint='same', ready=False, contexts=108,
                validation_kind='full', completed_at_ns=200)))
            os.utime(older, ns=(10**18, 10**18))
            os.utime(newer, ns=(1000, 1000))
            self.assertFalse(readiness.latest_reports(directory)['same']['ready'])
            older.write_text(json.dumps(dict(fingerprint='same', ready=True, contexts=108)))
            self.assertFalse(readiness.latest_reports(directory)['same']['ready'])


class CacheTest(unittest.TestCase):
    def test_resize_releases_real_mappings_and_changes_seeded_cache_misses(self):
        from adaptive_os.sandbox.resources import Cache
        cache = Cache(16, seed=123, working_set_mib=32, refill_ms=.1)
        self.addCleanup(cache.close)
        self.assertEqual(sum(len(chunk) for chunk in cache.chunks), 16 * 1048576)
        for key in range(512):
            cache.request(key)
        before = cache.misses
        for key in range(512):
            cache.request(key)
        self.assertEqual(cache.misses - before, 512)
        cache.resize(32)
        for key in range(512):
            cache.request(key)
        before = cache.misses
        for key in range(512):
            cache.request(key)
        self.assertEqual(cache.misses, before)
        removed = cache.chunks[-1]
        cache.resize(16)
        self.assertTrue(removed.closed)
        self.assertEqual(cache.allocated_mib, 16)
        self.assertLessEqual(len(cache.entries), 256)

    def test_allocation_failure_keeps_old_capacity(self):
        from unittest.mock import patch
        from adaptive_os.sandbox.resources import Cache
        cache = Cache(16, seed=1, working_set_mib=32, refill_ms=.1)
        self.addCleanup(cache.close)
        with patch('adaptive_os.sandbox.resources.mmap.mmap', side_effect=OSError('out of memory')):
            with self.assertRaises(OSError):
                cache.resize(32)
        self.assertEqual(cache.allocated_mib, 16)
        cache.request(0)

    def test_memory_transaction_shrinks_before_growing_and_rolls_back_failure(self):
        from adaptive_os.sandbox.resources import MemoryTransaction
        command = transfer()
        tx = MemoryTransaction(command)
        donor, foreground = command['changes']
        self.assertEqual(tx.next_change(), donor)
        with self.assertRaises(ValueError):
            tx.acknowledge(foreground['workload_id'], foreground['identity'], 144, True)
        tx.acknowledge(donor['workload_id'], donor['identity'], 112, True)
        self.assertEqual(tx.next_change(), foreground)
        tx.acknowledge(foreground['workload_id'], foreground['identity'], 128, False)
        rollback = tx.next_change()
        self.assertEqual((rollback['workload_id'], rollback['previous'], rollback['requested']),
                         (donor['workload_id'], 112, 128))
        tx.acknowledge(donor['workload_id'], donor['identity'], 128, True)
        self.assertTrue(tx.done)
        self.assertFalse(tx.ok)
        self.assertTrue(tx.rolled_back)


class ResourceRuleTest(unittest.TestCase):
    def test_shadow_timeline_distinguishes_guarded_proposal_from_actual_result(self):
        from adaptive_os.decision_activity import DecisionEntry
        entry = DecisionEntry('shadow')
        entry.add(dict(kind='decision_result', decision={'action': 'favor'}, result={'action': 'hold'}))
        self.assertIn('Controller result: {"action": "hold"}', entry.details(None, {}))

    def test_switching_policy_requires_restored_resource_allocations(self):
        from adaptive_os.resource_control import ResourceTask
        from adaptive_os.decision_controller import PolicyController
        controller = PolicyController(resource='memory')
        fg = ResourceTask('f', 'High', 1, 128, 128)
        controller.activate('s', 't', ('f', 1), [ResourceTask('b', 'Low', 2, 128, 128)], foreground_task=fg)
        with self.assertRaisesRegex(ValueError, 'restore'):
            controller.activate('s', 't', ('f', 1), [ResourceTask('b', 'Low', 2, 128, 112)],
                                foreground_task=ResourceTask('f', 'High', 1, 128, 144))

    def test_resource_rule_requires_two_high_six_low_and_ack_cooldown(self):
        from adaptive_os.resource_control import ResourceTask, allocation_decision, evaluate_resource_rule
        from adaptive_os.cpu_rule import CpuWindow, RuleState, acknowledge
        tasks = [ResourceTask('a', 'Low', 1, 128, 128), ResourceTask('b', 'Normal', 2, 128, 128)]
        foreground = ResourceTask('f', 'High', 3, 128, 128)
        state = RuleState()
        for end in (5, 10):
            decision, state = evaluate_resource_rule(CpuWindow(end - 5, end, .03, None, True), tasks, state, foreground, 'memory')
        self.assertEqual(decision.action, 'favor')
        self.assertEqual(decision.target, 'a')
        self.assertEqual([c['requested'] for c in decision.changes], [112, 144])
        state = acknowledge(state, 10.1)
        tasks[0] = ResourceTask('a', 'Low', 1, 128, 112, 1)
        foreground = ResourceTask('f', 'High', 3, 128, 144)
        for end in (15, 20, 25, 30, 35, 40):
            decision, state = evaluate_resource_rule(CpuWindow(end - 5, end, 0, None, True), tasks, state, foreground, 'memory')
            self.assertEqual(decision.action, 'hold')
        decision, state = evaluate_resource_rule(CpuWindow(40, 45, 0, None, True), tasks, state, foreground, 'memory')
        self.assertEqual(decision.action, 'restore')
        self.assertEqual([c['requested'] for c in decision.changes], [128, 128])
        tasks += [ResourceTask('c', 'Low', 4, 128, 160)]
        self.assertEqual(allocation_decision('favor', tasks, foreground, 'memory').target, 'c')

    def test_shared_model_deadline_shadow_ack_and_identity_guards(self):
        from adaptive_os.resource_control import ResourceTask
        from adaptive_os.cpu_rule import CpuWindow
        from adaptive_os.decision_controller import PolicyController
        from tests.test_decision_controller import Helper
        for resource, amount in [('memory', 128), ('network', 2500)]:
            now = [0.]
            helper = Helper()
            task, fg = ResourceTask('b', 'Low', 2, amount, amount), ResourceTask('f', 'High', 1, amount, amount)
            controller = PolicyController(helper, clock=lambda: now[0], resource=resource)
            controller.activate('s', 't', ('f', 1), [task], 'jev', foreground_task=fg, shadow=True)
            for end in (5, 10):
                controller.observe(CpuWindow(end - 5, end, .3, None, True), [task], ('f', 1))
                helper.replies.append({'request_id': helper.sent[-1][0], 'proposal':
                    dict(action='favor', probabilities=dict(hold=.1, favor=.8, restore=.1),
                         model='test', confidence=.8, inference_ms=10)})
                self.assertEqual(controller.poll([task], ('f', 1)).action, 'hold')
            self.assertEqual(helper.sent[-1][1]['resource'], resource)
            controller.shadow = False
            decision = controller.guarded('favor', [task], ('f', 1))
            controller.command_sent('r', decision)
            self.assertFalse(controller.acknowledge_allocation('wrong', list(decision.changes), 10.2))
            self.assertTrue(controller.acknowledge_allocation('r', list(decision.changes), 10.2))
            self.assertEqual(controller.state.last_change, 10.2)
            self.assertEqual(controller.guarded('favor', [task], ('f', 2)).action, 'restore_all')
            controller.observe(CpuWindow(10, 15, .3, None, True), [task], ('f', 1))
            now[0] = 1.
            self.assertEqual(controller.poll([task], ('f', 1)).action, 'hold')
            self.assertEqual(controller.fallback_reason, 'deadline')

    def test_invalid_allocation_is_rejected_even_during_initial_observation(self):
        from adaptive_os.resource_control import ResourceTask
        from adaptive_os.cpu_rule import CpuWindow
        from adaptive_os.decision_controller import PolicyController
        controller = PolicyController(resource='memory')
        task = ResourceTask('b', 'Low', 2, 128, 0)
        controller.activate('s', 't', ('f', 1), [task], foreground_task=ResourceTask('f', 'High', 1, 128, 128))
        result = controller.observe(CpuWindow(0, 5, .1, None, True), [task], ('f', 1))
        self.assertEqual(result.action, 'restore_all')


class RelayTest(unittest.TestCase):
    def test_real_tcp_shares_change_atomically_with_bounded_backpressure(self):
        import socket
        import threading
        import time
        from adaptive_os.sandbox.network import Relay, connect_flow, receive_record, request_record
        relay = Relay(20)
        self.addCleanup(relay.close)
        relay.add_flow('a', 11, 5000)
        relay.add_flow('b', 12, 5000)
        clients = [connect_flow(relay.port, key, identity) for key, identity in [('a', 11), ('b', 12)]]
        stopping = threading.Event()
        def consume(client):
            sequence = 0
            while not stopping.is_set():
                try:
                    request_record(client, sequence, 64 * 1024)
                    receive_record(client, sequence, 64 * 1024)
                    sequence += 1
                except (OSError, ValueError):
                    return
        threads = [threading.Thread(target=consume, args=(c,)) for c in clients]
        for thread in threads:
            thread.start()
        try:
            time.sleep(.8)
            before = relay.snapshot()
            with self.assertRaises(ValueError):
                relay.update_shares([dict(workload_id='a', identity=999, previous=5000, requested=8000),
                                     dict(workload_id='b', identity=12, previous=5000, requested=2000)])
            self.assertEqual(relay.snapshot()['a']['share'], 5000)
            relay.update_shares([dict(workload_id='a', identity=11, previous=5000, requested=8000),
                                 dict(workload_id='b', identity=12, previous=5000, requested=2000)])
            measured_start = time.monotonic()
            time.sleep(.8)
            after = relay.snapshot()
            a = after['a']['bytes'] - before['a']['bytes']
            b = after['b']['bytes'] - before['b']['bytes']
            self.assertGreater(a, b * 2)
            self.assertLessEqual(a + b, 20e6 / 8 * (time.monotonic() - measured_start + .05) + relay.burst)
            self.assertTrue(all(row['queued_bytes'] <= 256 * 1024 + 16 for row in after.values()))
        finally:
            stopping.set()
            relay.close()
            for client in clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
            for thread in threads:
                thread.join(2)
            self.assertFalse(any(thread.is_alive() for thread in threads))


if __name__ == '__main__':
    unittest.main()
