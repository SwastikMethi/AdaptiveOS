import unittest


class GuestExperimentTest(unittest.TestCase):
    def test_endpoint_keeps_connection_during_paced_background_idle(self):
        import hashlib
        import http.client
        import queue
        import threading
        import time
        from unittest.mock import patch
        from adaptive_os.guest_linux import fixtures
        created = queue.Queue()
        server_type = fixtures.ThreadingHTTPServer
        def create(address, handler):
            server = server_type(address, handler)
            created.put(server)
            return server
        with patch.object(fixtures, 'ThreadingHTTPServer', side_effect=create), patch.object(fixtures, 'emit'):
            thread = threading.Thread(target=fixtures.endpoint, args=('127.0.0.1', 0), daemon=True)
            thread.start()
            server = created.get(timeout=5)
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                connection.request('GET', '/?bytes=65536')
                self.assertEqual(connection.getresponse().read(), fixtures.BLOCK[:65536])
                time.sleep(10.2)
                connection.request('POST', '/', fixtures.BLOCK[:65536])
                self.assertEqual(connection.getresponse().read(), hashlib.sha256(fixtures.BLOCK[:65536]).hexdigest().encode())
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_rejected_snapshot_is_recorded_without_becoming_latest(self):
        import copy
        from unittest.mock import Mock
        from adaptive_os.guest_linux.experiments import GuestRun
        from tests.test_guest_linux_policy import snapshot
        source = snapshot()
        for failure in ('preflight', 'inspection', 'settings'):
            changed = copy.deepcopy(source)
            if failure == 'preflight':
                changed['preflight'].update(passed=False, display={'error': 'display probe timed out'})
            elif failure == 'inspection':
                changed['tasks'][0]['inspection_error'] = 'process exited during inspection'
            else:
                changed['tasks'][0]['cpu_weight'] = 90
            run = object.__new__(GuestRun)
            run.before, run.latest, run.run_id = source, source, source['run_id']
            run.resource, run.fixtures, run.audit = 'cpu', [], Mock()
            run.transport = Mock(return_value=changed)
            with self.subTest(failure=failure):
                with self.assertRaises(RuntimeError):
                    run.sample()
                run.audit.assert_called_once_with(kind='guest_snapshot', **changed)
                self.assertIs(run.latest, source)

    def test_memory_background_batches_bytes_without_sampling_away_outcomes(self):
        from adaptive_os.guest_linux.fixtures import workload
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        import os
        original_stat = os.stat
        for role, chunk in (('foreground', 65536), ('background', 1024**2)):
            events = []
            with tempfile.TemporaryDirectory() as folder:
                args = SimpleNamespace(resource='memory', role=role, profile='contention',
                    directory=folder, task=role, memory_mib=1, seconds=.04, direction='both',
                    identity='test', endpoint='http://192.168.64.1:8766')
                with patch('adaptive_os.guest_linux.fixtures.emit', side_effect=lambda **row: events.append(row)), \
                     patch('adaptive_os.guest_linux.fixtures.signal.signal'), \
                     patch('adaptive_os.guest_linux.fixtures.os.posix_fadvise', create=True), \
                     patch('adaptive_os.guest_linux.fixtures.os.POSIX_FADV_DONTNEED', 4, create=True), \
                     patch('adaptive_os.guest_linux.fixtures.Path.read_text', return_value='/owned'), \
                     patch('adaptive_os.guest_linux.fixtures.os.stat', side_effect=lambda path, *a, **kw:
                           SimpleNamespace(st_ino=1) if str(path) == '/proc/self/ns/net' else original_stat(path, *a, **kw)), \
                     patch('adaptive_os.guest_linux.fixtures.os.sched_getaffinity', return_value={1, 2, 3}, create=True):
                    workload(args, {})
            rows = [r for e in events for r in e.get('rows', [])]
            self.assertTrue(rows)
            self.assertTrue(all(r['work'] == chunk for r in rows), (role, rows[:2]))

    def test_memory_profiles_representative_activity_without_external_pressure(self):
        from adaptive_os.guest_linux.experiments import calibrate
        from tests.test_guest_linux_policy import snapshot
        from unittest.mock import Mock, patch
        import tempfile
        from pathlib import Path
        source = snapshot()
        run = Mock(error=None, foreground=source['tasks'][0]['id'])
        run.fixtures = [Mock(task=source['tasks'][0], rows=[{'latency_ms': .1}])]
        run.sample.return_value = {**source, 'resource_control': {'observation_id': 'sample'}}
        run.command.return_value = {'profile': {'suitable': True, 'id': 'profile', 'highs': {}}}
        with tempfile.TemporaryDirectory() as folder, patch('adaptive_os.guest_linux.experiments.request', return_value=source), \
             patch('adaptive_os.guest_linux.experiments.GuestRun', return_value=run), \
             patch('adaptive_os.guest_linux.experiments.contention_pilot', return_value={'passed': True}):
            config, _ = calibrate('memory', Path(folder), 'http://endpoint')
        self.assertTrue(config['passed'])
        self.assertEqual(run.start_fixtures.call_args.args[0], 'contention')
        run.start_pressure.assert_not_called()

    def test_memory_contention_verifier_uses_the_contention_workload(self):
        from adaptive_os.guest_linux.resource_verify import verify
        from unittest.mock import Mock, patch
        import tempfile
        from pathlib import Path
        run = Mock(error=None, before={})
        run.start_fixtures.side_effect = RuntimeError('stop after workload selection')
        with tempfile.TemporaryDirectory() as folder, patch('adaptive_os.guest_linux.resource_verify.GuestRun', return_value=run):
            verify('memory', 'contention', Path(folder), {})
        run.start_fixtures.assert_called_once_with('contention', {})

    def test_contention_calibration_requires_measured_pressure_or_saturation(self):
        from adaptive_os.guest_linux.experiments import contention_checks
        first = {'observed_at_ns': 0, 'tasks': [{'id': 'one', 'cpu_usage_usec': 0, 'memory_stat': {},
                  'memory_pressure': 'some avg10=0.0 total=0', 'network': {'upload_bytes': 0, 'download_bytes': 0}}]}
        import copy
        last = copy.deepcopy(first)
        last['observed_at_ns'] = 30_000_000_000
        for resource in ('cpu', 'memory', 'network'):
            self.assertFalse(all(contention_checks(resource, first, last).values()))
        last['tasks'][0].update(cpu_usage_usec=90_000_000, memory_stat={'pgscan': 100},
            memory_pressure='some avg10=2.0 total=10\nfull avg10=0.2 total=1')
        last['tasks'][0]['network'] = {'upload_bytes': 75_000_000, 'download_bytes': 75_000_000}
        for resource in ('cpu', 'memory', 'network'):
            self.assertTrue(all(contention_checks(resource, first, last).values()))

    def test_sampling_accepts_kernel_string_limits_during_legitimate_cooldown(self):
        from adaptive_os.guest_linux.experiments import GuestRun
        from tests.test_guest_linux_policy import snapshot
        from unittest.mock import Mock
        source = snapshot()
        source['cpu_control'] = None
        source['resource_control'] = {'resource': 'memory', 'ready': False, 'scoring_valid': True,
            'expected': {'parent': {'low': 3}, 'tasks': {t['id']: {'low': 0, 'min': 0, 'high': 300, 'max': 'max'} for t in source['tasks']}}}
        source['memory_parent'] = {'low': 3}
        for task in source['tasks']:
            task.update(memory_low=0, memory_min=0, memory_high='300', memory_max='max')
        run = object.__new__(GuestRun)
        run.before, run.run_id, run.fixtures, run.audit = source, source['run_id'], [], Mock()
        run.resource, run.transport = 'memory', Mock(return_value=source)
        self.assertIs(run.sample(), source)

    def test_sampling_rejects_actual_settings_drift_even_during_cooldown(self):
        from adaptive_os.guest_linux.experiments import GuestRun
        from tests.test_guest_linux_policy import snapshot
        from unittest.mock import Mock
        import copy
        source = snapshot()
        run = object.__new__(GuestRun)
        run.before, run.run_id, run.fixtures, run.audit = source, source['run_id'], [], Mock()
        for resource in ('cpu', 'memory', 'network'):
            changed = copy.deepcopy(source)
            run.resource = resource
            if resource == 'cpu':
                changed['tasks'][0]['cpu_weight'] = 90
            else:
                changed['cpu_control'] = None
                changed['resource_control'] = {'resource': resource, 'ready': False, 'scoring_valid': True,
                    'expected': {'upload': {'shares': {'front': 5000}}, 'download': {'shares': {'front': 5000}}}}
                if resource == 'network':
                    changed['network_parent'] = {'settings': {'upload': None, 'download': None}}
                else:
                    changed['resource_control']['expected'] = {'parent': {'low': 3}, 'tasks': {t['id']: {'high': 300} for t in source['tasks']}}
                    changed['memory_parent'] = {'low': 0}
            run.transport = Mock(return_value=changed)
            with self.subTest(resource=resource), self.assertRaisesRegex(RuntimeError, 'settings'):
                run.sample()

    def test_calibration_cleanup_failure_cannot_pass(self):
        from adaptive_os.guest_linux.experiments import calibrate
        from tests.test_guest_linux_policy import snapshot
        from unittest.mock import Mock, patch
        import tempfile
        from pathlib import Path
        source = snapshot()
        run = Mock(error=None)
        run.foreground = source['tasks'][0]['id']
        run.fixtures = [Mock(task=source['tasks'][0], rows=[{'latency_ms': 40}])]
        run.sample.return_value = source
        run.close.side_effect = lambda: setattr(run, 'error', 'restoration failed')
        with tempfile.TemporaryDirectory() as folder, patch('adaptive_os.guest_linux.experiments.request', return_value=source), \
             patch('adaptive_os.guest_linux.experiments.GuestRun', return_value=run):
            report, _ = calibrate('cpu', Path(folder), 'http://endpoint')
            self.assertFalse(report['passed'])
            self.assertIn('restoration', report['attempts'][-1]['error'])

    def test_shadow_cleanup_failure_is_saved_as_failed_evidence(self):
        from adaptive_os.guest_linux.experiments import shadow_check
        from tests.test_guest_linux_policy import snapshot
        from unittest.mock import Mock, patch
        import tempfile
        from pathlib import Path
        run = Mock(error=None, before=snapshot())
        run.policy.shadow_rows = [{}] * 36
        run.close.side_effect = lambda: setattr(run, 'error', 'restoration failed')
        with tempfile.TemporaryDirectory() as folder, patch('adaptive_os.guest_linux.experiments.GuestRun', return_value=run), \
             patch('adaptive_os.guest_linux.experiments.validate_report', return_value=True):
            report, path = shadow_check({'resource': 'cpu'}, Path(folder), '')
            self.assertFalse(report['passed'])
            self.assertIn('restoration', path.read_text())

    def test_restored_download_uses_absolute_path_inside_task_mount_namespace(self):
        from adaptive_os.guest_linux.resource_verify import public_network
        from adaptive_os.guest_linux.fixtures import BLOCK
        from unittest.mock import Mock, patch
        from pathlib import Path
        source = {'tasks': [{'id': 'one', 'main_pid': 123, 'network': {'namespace': 'task-one'}}]}
        def command(args, **kwargs):
            target = args[args.index('-o') + 1]
            self.assertTrue(Path(target).is_absolute())
        with patch('subprocess.check_output', side_effect=['93.184.215.14 example.com', '']), \
             patch('subprocess.run', side_effect=command), \
             patch.object(Path, 'read_bytes', return_value=BLOCK), \
             patch.object(Path, 'stat', return_value=Mock(st_size=len(BLOCK))):
            rows = public_network(source, Path('relative/evidence'), 'http://192.168.64.1:8766')
            self.assertEqual(rows[0]['bytes'], len(BLOCK))

    def test_repeated_calibration_attempts_save_independent_json_configuration(self):
        from adaptive_os.guest_linux.experiments import calibrate
        from tests.test_guest_linux_policy import snapshot
        from unittest.mock import Mock, patch
        import json
        import tempfile
        from pathlib import Path
        source = snapshot()
        runs = []
        for latency in (20, 40):
            run = Mock(error=None)
            run.foreground = source['tasks'][0]['id']
            fixture = Mock(task=source['tasks'][0], rows=[{'latency_ms': latency}])
            run.fixtures, run.sample.return_value = [fixture], source
            runs.append(run)
        with tempfile.TemporaryDirectory() as folder, \
             patch('adaptive_os.guest_linux.experiments.request', return_value=source), \
             patch('adaptive_os.guest_linux.experiments.GuestRun', side_effect=runs), \
             patch('adaptive_os.guest_linux.experiments.contention_pilot', return_value={'passed': True}):
            report, path = calibrate('cpu', Path(folder), 'http://endpoint')
            self.assertTrue(report['passed'])
            saved = json.loads(path.read_text())
            self.assertEqual(len(saved['attempts']), 2)
            self.assertEqual(saved['attempts'][0]['configuration']['iterations'], 50000)
            self.assertNotIn('attempts', saved['attempts'][1]['configuration'])

    def test_cleanup_failure_still_ends_run_and_exports_failed_evidence(self):
        from adaptive_os.guest_linux.experiments import GuestRun
        from unittest.mock import Mock
        from pathlib import Path
        run = object.__new__(GuestRun)
        run.policy = Mock()
        run.pressure_process = Mock()
        run.pressure_process.stdin.close.side_effect = OSError('pressure pipe failed')
        run.run_id, run.session, run.error = 'owned-run', 'session', None
        run.stop, run.pulse, run.store, run.command = Mock(), None, Mock(), Mock()
        run.fixtures, run.directory = [Mock()], Path('/tmp/evidence')
        run.close()
        run.command.assert_called_once_with('end')
        run.fixtures[0].close.assert_called_once()
        run.store.finish.assert_called_once_with('session', 'failed')
        self.assertEqual(run.store.export.call_count, 2)

    def test_packet_proof_correlates_original_and_mapped_tuples_in_both_directions(self):
        from adaptive_os.guest_linux.resource_verify import packet_checks
        out = 'IP 10.203.1.2.40000 > 192.168.64.1.8766: Flags [P.], length 120\n'
        back = 'IP 192.168.64.1.8766 > 10.203.1.2.40000: Flags [P.], length 120\n'
        wire = 'IP 192.168.64.2.50000 > 192.168.64.1.8766: Flags [P.], length 120\n'
        reply = 'IP 192.168.64.1.8766 > 192.168.64.2.50000: Flags [P.], length 120\n'
        mapping = 'tcp src=10.203.1.2 dst=192.168.64.1 sport=40000 dport=8766 src=192.168.64.1 dst=192.168.64.2 sport=8766 dport=50000'
        captures = {'task': out + back, 'gateway': out + back, 'transit': out + back, 'uplink': wire + reply}
        self.assertTrue(all(packet_checks(captures, mapping, '10.203.1.2').values()))
        self.assertFalse(all(packet_checks({**captures, 'transit': wire + reply}, mapping, '10.203.1.2').values()))
        self.assertFalse(all(packet_checks(captures, mapping.replace('dport=50000', 'dport=51000'), '10.203.1.2').values()))

    def test_fixture_setup_refuses_ordinary_users_before_resolving_tasks(self):
        from adaptive_os.guest_linux.fixtures import managed_fixture
        from unittest.mock import patch
        with patch('os.geteuid', return_value=1000), self.assertRaises(PermissionError):
            managed_fixture(None)

    def test_randomized_paired_schedule_has_full_duration_and_all_arms_in_each_block(self):
        from adaptive_os.guest_linux.experiments import schedule
        for model, count, arms in [(False, 30, {'default', 'manual', 'rule'}),
                                   (True, 40, {'default', 'manual', 'rule', 'jev'})]:
            rows = schedule(281, model=model)
            self.assertEqual(len(rows), count)
            self.assertEqual(rows, schedule(281, model=model))
            for profile in ('light', 'contention'):
                for block in range(5):
                    group = [r for r in rows if r['profile'] == profile and r['block'] == block]
                    self.assertEqual({r['arm'] for r in group}, arms)
                    self.assertTrue(all(r['warmup_seconds'] == 30 and r['measurement_seconds'] == 180 for r in group))
            self.assertNotEqual([r['arm'] for r in rows], [r['arm'] for r in schedule(282, model=model)])

    def test_network_acceptance_requires_each_direction_parent_share_and_borrower(self):
        from adaptive_os.guest_linux.experiments import network_checks
        rows = []
        for direction in ('upload', 'download'):
            rows.extend([{'direction': direction, 'seconds': 30, 'parent_mbps': 19.8,
                          'shares': {'a': .5, 'b': .5}, 'expected_shares': {'a': .5, 'b': .5}, 'borrower': False},
                         {'direction': direction, 'seconds': 30, 'parent_mbps': 19,
                          'shares': {'a': 1.}, 'expected_shares': {'a': 1.}, 'borrower': True}])
        self.assertTrue(all(network_checks(rows).values()))
        broken = [*rows[:2], {**rows[2], 'parent_mbps': 12}, rows[3]]
        self.assertFalse(all(network_checks(broken).values()))
        self.assertFalse(all(network_checks(rows[:2]).values()))

    def test_every_background_loss_and_failed_trials_are_retained(self):
        from adaptive_os.guest_linux.experiments import comparison_report
        trials = []
        for block in range(5):
            for profile in ('light', 'contention'):
                for arm, latency, one, two in [('default', 100, 100, 100), ('manual', 75, 80, 80),
                                               ('rule', 70, 80, 80), ('jev', 60, 90, 50)]:
                    trials.append({'profile': profile, 'block': block, 'arm': arm, 'valid': True,
                                   'foreground_p95_ms': latency, 'background_throughput': {'one': one, 'two': two}})
        report = comparison_report(trials)
        self.assertFalse(report['ai_useful'])
        self.assertEqual(report['profiles']['contention']['jev']['worst_background_loss'], .5)
        trials.append({'profile': 'contention', 'block': 5, 'arm': 'jev', 'valid': False, 'error': 'OOM'})
        report = comparison_report(trials)
        self.assertEqual(report['invalid_trials'][-1]['error'], 'OOM')


if __name__ == '__main__':
    unittest.main()
