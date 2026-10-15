import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import chdir, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit
from adaptive_os.desktop import MainWindow, main, read_jev_env_file
from adaptive_os.desktop_worker import SessionWorker
from tests.test_decision_controller import Helper, proposal


class JevEnvFileTest(unittest.TestCase):
    def test_reads_only_the_key_without_execution_or_environment_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            marker = Path(folder) / 'must-not-exist'
            for assignment in ('JEV_API_KEY=fake-token # comment',
                               'export JEV_API_KEY = "fake-token"',
                               "JEV_API_KEY='fake-token'"):
                with self.subTest(assignment=assignment):
                    contents = f'# comment\nIGNORED=$(touch "{marker}")\n{assignment}\n'
                    path.write_text(contents)
                    environment = dict(os.environ)
                    self.assertEqual(read_jev_env_file(path), 'fake-token')
                    self.assertEqual(dict(os.environ), environment)
                    self.assertEqual(path.read_text(), contents)
                    self.assertFalse(marker.exists())
            path.write_text('JEV_API_KEY="${UNEXPANDED}-$(whoami)"\n')
            self.assertEqual(read_jev_env_file(path), '${UNEXPANDED}-$(whoami)')

    def test_invalid_keys_fail_without_echoing_secret_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            secret = 'private-test-token'
            cases = ('# no key', 'JEV_API_KEY=', 'JEV_API_KEY=""',
                     f'JEV_API_KEY="{secret}', f'JEV_API_KEY={secret} extra',
                     f'JEV_API_KEY={secret}\nJEV_API_KEY=duplicate',
                     'JEV_API_KEY=' + secret * 300, f'JEV_API_KEY={secret}\0')
            for contents in cases:
                with self.subTest(contents=contents[:40]):
                    path.write_text(contents)
                    with self.assertRaises(ValueError) as caught:
                        read_jev_env_file(path)
                    self.assertNotIn(secret, str(caught.exception))
            path.write_bytes(b'JEV_API_KEY=\xff')
            with self.assertRaisesRegex(ValueError, 'Cannot read'):
                read_jev_env_file(path)
            with self.assertRaisesRegex(ValueError, 'Cannot read'):
                read_jev_env_file(Path(folder) / 'missing.env')

    def test_cli_rejects_invalid_file_before_opening_a_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('JEV_API_KEY="private-test-token')
            errors = io.StringIO()
            with patch('adaptive_os.desktop.QApplication') as app, redirect_stderr(errors):
                with self.assertRaises(SystemExit) as caught:
                    main(['--jev-env-file', str(path)])
            self.assertEqual(caught.exception.code, 2)
            app.assert_not_called()
            self.assertIn('Invalid JEV_API_KEY quoting', errors.getvalue())
            self.assertNotIn('private-test-token', errors.getvalue())


class ModelDesktopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.worker = SessionWorker(Path(self.folder.name) / 'recording.sqlite3')
        self.window = MainWindow(self.worker)
        self.window.timer.stop()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.window.active = False
        self.window.experiment = None
        self.window.close()

    def running(self):
        self.window.active = self.window.preflight_complete = self.window.priority_supported = True
        self.window.session_id = 'session'
        for i, row in enumerate(self.window.workloads.values()):
            row.update(status='running', identity=i + 1, original_nice=0, nice=0)

    def test_modes_and_masked_memory_only_credentials(self):
        window = self.window
        modes = {window.policy_choice.itemData(i) for i in range(window.policy_choice.count())}
        self.assertEqual(modes, {'manual', 'rule', 'shadow:jev', 'shadow:laya', 'jev', 'laya'})
        self.assertEqual(window.jev_key.echoMode(), QLineEdit.EchoMode.Password)
        window.jev_key.setText('never-record-this')
        self.running()
        window.update_controls()
        self.assertFalse(window.jev_key.isEnabled())
        self.assertFalse(window.laya_device.isEnabled())

    def test_entrypoint_prefills_masked_key_only_with_explicit_env_flag(self):
        path = Path(self.folder.name) / '.env'
        path.write_text('JEV_API_KEY=private-test-token\n')
        for extra in ([], ['--jev-env-file', '.env']):
            with self.subTest(arguments=extra), chdir(self.folder.name), \
                    patch('adaptive_os.desktop.MainWindow', return_value=self.window), \
                    patch('adaptive_os.desktop.SessionWorker', return_value=self.worker), \
                    patch.object(self.worker, 'start'), patch.object(self.worker, 'submit'), \
                    patch.object(self.worker, 'is_alive', return_value=False), \
                    patch.object(self.window, 'show'), patch.object(self.app, 'exec', return_value=0):
                self.window.jev_key.clear()
                self.assertEqual(main(['--data-dir', self.folder.name, *extra]), 0)
                self.assertEqual(self.window.jev_key.text(), 'private-test-token' if extra else '')
                self.assertEqual(self.window.jev_key.echoMode(), QLineEdit.EchoMode.Password)
                self.assertIsNone(self.window.model_helper)
                self.assertFalse(self.window.auto_active)
                self.assertEqual(path.read_text(), 'JEV_API_KEY=private-test-token\n')

    def test_signal_shutdown_runs_on_timer_without_confirmation_and_only_once(self):
        import signal
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock
        window = self.window
        self.running()
        window.auto_active = True
        window.experiment = {'phase': 'running'}
        window.jev_key.setText('test-only-key')
        helper = SimpleNamespace(close=Mock())
        window.model_helper = helper
        self.worker.runtime = SimpleNamespace(cancel=threading.Event())
        with patch.object(self.worker, 'is_alive', return_value=True), \
                patch.object(self.worker, 'submit') as submit, \
                patch('adaptive_os.desktop.QMessageBox.question') as confirm:
            window.request_shutdown(signal.SIGTERM, None)
            self.assertFalse(window.closing)
            helper.close.assert_not_called()
            window.pump()
            window.request_shutdown(signal.SIGINT, None)
            window.pump()
            confirm.assert_not_called()
            helper.close.assert_called_once()
            self.assertEqual(sum(call.args == ({'op': 'quit'},) for call in submit.call_args_list), 1)
        self.assertTrue(window.closing)
        self.assertTrue(self.worker.runtime.cancel.is_set())
        self.assertFalse(window.auto_active)
        self.assertIsNone(window.experiment)
        self.assertEqual(window.jev_key.text(), '')

    def test_shutdown_retries_a_full_worker_queue(self):
        import queue
        window = self.window
        with patch.object(self.worker, 'is_alive', return_value=True), \
                patch.object(self.worker, 'submit', side_effect=[queue.Full, None]) as submit:
            window.request_shutdown()
            window.pump()
            self.assertTrue(window.closing)
            self.assertFalse(window.quit_submitted)
            window.pump()
            self.assertTrue(window.quit_submitted)
            self.assertEqual(submit.call_count, 2)

    def test_normal_window_close_still_requires_confirmation_for_active_session(self):
        from PySide6.QtGui import QCloseEvent
        from PySide6.QtWidgets import QMessageBox
        self.running()
        event = QCloseEvent()
        with patch.object(self.worker, 'is_alive', return_value=True), \
                patch('adaptive_os.desktop.QMessageBox.question', return_value=QMessageBox.StandardButton.Cancel):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window.closing)

    def test_new_workload_set_refreshes_selected_editor(self):
        window = self.window
        old_foreground = next(iter(window.workloads.values()))
        old_foreground.update(name='Recorded foreground', config={'budget_ms': 12})
        window.show_details()
        window.new_playground()
        new_foreground = next(iter(window.workloads.values()))
        self.assertEqual(window.edit_name.text(), new_foreground['name'])
        self.assertEqual(window.edit_budget.value(), new_foreground['config']['budget_ms'])
        window.experiment_kind.setCurrentIndex(window.experiment_kind.findData('heldout_calibration'))
        with patch.object(window, 'start_session'):
            window.start_experiment()
        self.assertEqual(window.edit_name.text(), 'Protected foreground')
        self.assertEqual(window.edit_budget.value(), 6)

    def test_demo_override_is_limited_to_jev_resource_playground(self):
        self.window.allow_unqualified_jev_demo = True
        for resource in ('cpu', 'memory', 'network'):
            self.window.resource = resource
            for provider in ('jev', 'laya'):
                with self.subTest(resource=resource, provider=provider):
                    self.assertEqual(self.window.live_model_allowed(provider),
                                     provider == 'jev' and resource != 'cpu')
                    self.assertFalse(self.window.model_is_ready(provider))
        self.window.experiment = {'phase': 'running'}
        self.assertFalse(self.window.live_model_allowed('jev'))
        self.window.experiment = None
        self.window.history_mode = True
        self.assertFalse(self.window.live_model_allowed('jev'))

    def test_demo_override_still_requires_loaded_matching_helper(self):
        self.running()
        self.window.allow_unqualified_jev_demo = True
        self.window.resource = 'memory'
        self.window.policy_choice.setCurrentIndex(self.window.policy_choice.findData('jev'))
        self.window.set_auto(True)
        self.assertFalse(self.window.auto_active)
        self.assertIn('Load and test', self.window.status.text())

    def test_live_demo_records_override_without_marking_readiness_passed(self):
        from unittest.mock import patch
        window = self.window
        window.resource_choice.setCurrentIndex(window.resource_choice.findData('memory'))
        self.running()
        window.resource_supported = True
        window.allow_unqualified_jev_demo = True
        for row in window.workloads.values():
            row['allocated'] = row['config']['mib']
        window.model_helper = Helper()
        window.loaded_provider = 'jev'
        window.loaded_fingerprint = window.provider_fingerprint('jev')
        window.policy_choice.setCurrentIndex(window.policy_choice.findData('jev'))
        with patch.object(window, 'audit') as audit:
            window.set_auto(True)
        self.assertTrue(window.auto_active)
        self.assertFalse(window.model_is_ready('jev'))
        event = next(call.kwargs for call in audit.call_args_list
                     if call.args == ('provider_configuration',))
        self.assertFalse(event['readiness_passed'])
        self.assertTrue(event['unqualified_demo'])
        self.assertEqual(event['deadline_ms'], 1000)
        window.set_auto(False)
        window.model_helper = None

    def test_live_models_require_readiness_even_with_preflight(self):
        self.running()
        self.window.policy_choice.setCurrentIndex(self.window.policy_choice.findData('jev'))
        self.window.set_auto(True)
        self.assertFalse(self.window.auto_active)
        self.assertIn('readiness', self.window.status.text().lower())

    def test_unloading_helper_immediately_clears_loaded_status(self):
        from types import SimpleNamespace
        self.window.model_helper = SimpleNamespace(close=lambda: None)
        self.window.loaded_provider = 'laya'
        self.window.provider_status.setText('Laya loaded')
        self.window.unload_model()
        self.assertIn('unloaded', self.window.provider_status.text().lower())
        self.assertIsNone(self.window.policy.helper)

    def test_resource_selection_manual_transfer_ack_restore_and_cpu_lock(self):
        window = self.window
        window.resource_choice.setCurrentIndex(window.resource_choice.findData('memory'))
        self.assertEqual(len(window.workloads), 4)
        self.assertEqual(sum(row['config']['mib'] for row in window.workloads.values()), 512)
        self.running()
        window.resource_supported = True
        window.priority_supported = False
        window.preflight_reason = 'Memory control ready; CPU priorities fixed.'
        window.refresh_decision_activity()
        self.assertNotIn('Auto disabled', window.decision_panel.explanation.text())
        for row in window.workloads.values():
            row.update(allocated=128, original_allocation=128)
        window.update_controls()
        self.assertFalse(window.resource_choice.isEnabled())
        self.assertFalse(window.priority.isEnabled())
        window.manual_allocation('favor')
        request = next(p for p in window.pending.values() if p['op'] == 'memory_budget')
        command = {k: request[k] for k in ('v', 'request_id', 'op', 'foreground_id', 'changes')}
        window.handle_update({**command, 'kind': 'ack', 'ok': True, 'mono_ns': 12_000_000_000,
                              'confirmed': {c['workload_id']: c['requested'] for c in command['changes']}})
        foreground = next(r for r in window.workloads.values() if r['role'] == 'Foreground')
        self.assertEqual(foreground['allocated'], 144)
        self.assertTrue(all(row['nice'] == 0 for row in window.workloads.values()))
        window.manual_allocation('restore')
        self.assertTrue(any(p['changes'][0]['requested'] == 128 for p in window.pending.values()))

    def test_resource_ack_mismatch_stops_automatic_control(self):
        window = self.window
        window.resource_choice.setCurrentIndex(window.resource_choice.findData('network'))
        self.running()
        window.resource_supported = True
        for row in window.workloads.values():
            row.update(allocated=2500, original_allocation=2500)
        window.manual_allocation('favor')
        request = next(p for p in window.pending.values() if p['op'] == 'network_share')
        window.handle_update({'kind': 'ack', 'request_id': request['request_id'], 'op': 'network_share',
                              'ok': True, 'changes': request['changes'], 'confirmed': {}, 'mono_ns': 10**9})
        self.assertFalse(window.resource_supported)
        self.assertTrue(any(r['op'] == 'stop_session' for r in list(self.worker.requests.queue)))

    def test_manual_allocation_finishes_and_restores_before_foreground_stop(self):
        window = self.window
        window.resource_choice.setCurrentIndex(window.resource_choice.findData('memory'))
        self.running()
        window.resource_supported = True
        for row in window.workloads.values():
            row.update(allocated=128, original_allocation=128)
        foreground = next(r for r in window.workloads.values() if r['role'] == 'Foreground')
        window.table.setCurrentCell(0, 0)
        window.manual_allocation('favor')
        request = next(iter(window.pending.values()))
        window.stop_selected()
        self.assertEqual(window.deferred_stop, foreground['id'])
        self.assertEqual([r['op'] for r in window.pending.values()], ['memory_budget'])
        def acknowledge(request, at):
            window.handle_allocation_ack({**request, 'kind': 'ack', 'ok': True, 'mono_ns': at,
                'confirmed': {c['workload_id']: c['requested'] for c in request['changes']}})
        acknowledge(request, 12 * 10**9)
        restore = next(iter(window.pending.values()))
        self.assertEqual(restore['allocation_action'], 'restore')
        self.assertFalse(any(r['op'] == 'stop' for r in window.pending.values()))
        acknowledge(restore, 13 * 10**9)
        self.assertEqual(foreground['allocated'], 128)
        self.assertTrue(any(r['op'] == 'stop' and r['workload_id'] == foreground['id'] for r in window.pending.values()))

    def test_invalid_typed_cache_capacity_leaves_config_and_shows_feedback(self):
        self.window.resource_choice.setCurrentIndex(self.window.resource_choice.findData('memory'))
        self.window.edit_memory.setValue(17)
        self.window.save_settings()
        self.assertEqual(next(iter(self.window.workloads.values()))['config']['mib'], 128)
        self.assertNotIn('Settings saved', self.window.status.text())

    def test_cpu_dialog_offers_only_cpu_mode_presets(self):
        from adaptive_os.desktop import WorkloadDialog
        dialog = WorkloadDialog(self.window)
        self.addCleanup(dialog.close)
        self.assertEqual({dialog.preset.itemData(i) for i in range(dialog.preset.count())},
                         {'periodic', 'cpu', 'memory', 'io'})

    def test_connection_test_retains_visible_full_readiness(self):
        from types import SimpleNamespace
        from adaptive_os.model_readiness import readiness_report
        window = self.window
        fingerprint = window.provider_fingerprint('jev')
        valid = {'valid': True, 'latency_ms': 100, 'audited': True, 'unauthorized_actions': 0}
        window.readiness[fingerprint] = readiness_report([valid] * 108, fingerprint)
        window.loaded_provider = 'jev'
        window.readiness_run = SimpleNamespace(rows=[valid], contexts=[{}], done=True,
            fingerprint=fingerprint, report=readiness_report([valid], fingerprint),
            poll=lambda: None, save=lambda path: Path('connection.json'))
        window.poll_model()
        self.assertIn('live readiness passed', window.provider_status.text())
        self.assertTrue(window.model_is_ready('jev'))

    def test_shadow_model_proposals_never_issue_priority_commands(self):
        window = self.window
        self.running()
        helper = Helper()
        window.model_helper = helper
        window.loaded_provider = 'jev'
        window.loaded_fingerprint = window.provider_fingerprint('jev')
        window.policy_choice.setCurrentIndex(window.policy_choice.findData('shadow:jev'))
        window.set_auto(True)
        foreground, background = window.workloads.values()
        for end in (5, 10):
            background['series'] = [(i, {'mono_ns': int(i * 1e9), 'cpu_pct': 70})
                                    for i in range(end - 4, end + 1)]
            foreground['sample'] = {'window_start_ns': (end - 5) * 10**9,
                                    'window_end_ns': end * 10**9, 'miss_rate': .3}
            window.evaluate_auto(foreground)
            helper.replies.append({'request_id': helper.sent[-1][0], 'proposal': proposal()})
            window.poll_model()
        self.assertFalse(any(item['op'] == 'priority' for item in window.pending.values()))
        self.assertEqual(background['nice'], 0)
        self.assertIn('Shadow', window.controller_status.text())
        window.model_helper = None

    def test_late_shadow_reply_is_drained_and_its_cost_is_recorded(self):
        from adaptive_os.cpu_rule import CpuWindow
        window = self.window
        self.running()
        foreground, background = window.workloads.values()
        background['sample'] = {'cpu_pct': 70}
        helper = Helper()
        window.model_helper = window.policy.helper = helper
        now = [0.0]
        window.policy.clock = lambda: now[0]
        identity = (foreground['id'], foreground['identity'])
        window.policy.activate('session', 'trial', identity, window.current_model_tasks(), 'laya', shadow=True)
        window.auto_active = True
        window.experiment = {'phase': 'measurement', 'inference_events': []}
        window.policy.observe(CpuWindow(0, 5, .01, 70, True), window.current_model_tasks(), identity)
        request_id = helper.sent[-1][0]
        now[0] = 1
        window.poll_model()
        helper.replies.append({'request_id': request_id, 'proposal': proposal(),
                               'helper_cpu_seconds': .8, 'helper_max_rss': 2000})
        now[0] = 1.4
        window.poll_model()
        self.assertIsNone(window.policy.expired)
        late = [row for row in window.experiment['inference_events'] if row.get('reason') == 'late_response']
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]['helper_cpu_seconds'], .8)
        self.assertFalse(any(item['op'] == 'priority' for item in window.pending.values()))
        window.model_helper = None

    def test_restoration_commands_are_serialized_until_acknowledged(self):
        from adaptive_os.desktop import workload
        self.running()
        first = list(self.window.workloads)[1]
        second = workload('cpu')
        second.update(status='running', identity=77, nice=10, original_nice=0)
        self.window.add_workload(second)
        self.window.workloads[first]['nice'] = 10
        self.window.set_auto(False)
        commands = []
        while not self.worker.requests.empty():
            request = self.worker.requests.get_nowait()
            if request['op'] == 'control':
                commands.append(request['command'])
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.window.handle_update({**command, 'kind': 'ack', 'ok': True, 'nice': 0})
        following = []
        while not self.worker.requests.empty():
            request = self.worker.requests.get_nowait()
            if request['op'] == 'control':
                following.append(request['command'])
        self.assertEqual(len(following), 1)
        self.assertNotEqual(following[0]['workload_id'], command['workload_id'])

    def test_shutdown_restores_high_importance_first(self):
        from adaptive_os.desktop import workload
        self.running()
        first = list(self.window.workloads)[1]
        self.window.workloads[first].update(nice=10, importance='Low', intervention_order=1)
        high = workload('cpu')
        high.update(status='running', identity=77, nice=10, original_nice=0, importance='High', intervention_order=2)
        self.window.add_workload(high)
        self.window.set_auto(False)
        first_request = next(item for item in self.window.pending.values() if not item.get('queued'))
        self.assertEqual(first_request['workload_id'], high['id'])

    def test_loaded_device_cannot_be_mislabeled_after_setting_changes(self):
        window = self.window
        window.model_helper = Helper()
        window.loaded_provider = 'laya'
        window.loaded_fingerprint = window.provider_fingerprint('laya')
        window.laya_device.setCurrentIndex(1)
        self.running()
        window.policy_choice.setCurrentIndex(window.policy_choice.findData('shadow:laya'))
        window.set_auto(True)
        self.assertFalse(window.auto_active)
        window.model_helper = None

    def test_automatic_ack_requires_the_controller_command_binding(self):
        self.running()
        window = self.window
        key = list(window.workloads)[1]
        request_id = window.request_priority(key, 10)
        window.pending[request_id].update(automatic='lower', decision_time=10)
        window.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority',
            'request_id': request_id, 'workload_id': key, 'identity': window.workloads[key]['identity'],
            'nice': 10, 'mono_ns': 11_000_000_000})
        self.assertIsNone(window.rule_state.last_change)
        self.assertIn('did not match', window.status.text())

    def test_failed_heldout_calibration_invalidates_previous_qualification(self):
        self.window.qualified_config = {'budget_ms': 6, 'backgrounds': 3}
        self.window.experiment = {'kind': 'heldout_calibration', 'seed': 1, 'cancelled': False,
                                 'results': [], 'config': {'backgrounds': 3}}
        self.window.finish_experiment()
        self.assertIsNone(self.window.qualified_config)

    def test_background_raw_records_reach_live_report_and_match_export(self):
        import json
        import uuid
        from adaptive_os.experiments import summarize_trial, recompute_recording
        from adaptive_os.session_store import SessionStore
        worker, window = self.worker, self.window
        key = list(window.workloads)[1]
        worker.store = SessionStore(worker.database)
        self.addCleanup(worker.store.close)
        worker.session_id = str(uuid.uuid4())
        worker.store.begin(worker.session_id, {})
        worker.store.configure(worker.session_id, window.workloads[key])
        worker.workload_presets = {key: 'cpu'}
        window.experiment = {'phase': 'measurement', 'raw_records': []}
        for seq, at, units in ((0, 100, 0), (1, 300, 20)):
            worker.receive(({'kind': 'raw', 'workload_id': key, 'sequence': seq + 1,
                'mono_ns': at, 'data': {'sequence': seq, 'start_ns': 10, 'mono_ns': at,
                                      'units': units, 'ticks': []}}, at))
        while not worker.updates.empty():
            window.handle_update(worker.updates.get_nowait())
        live = summarize_trial(None, [], {key}, 100, 300, raw_records=window.experiment['raw_records'])
        self.assertTrue(live['valid'], live['exclusion_reasons'])
        worker.store.record(worker.session_id, {'kind': 'trial_boundary', 'boundary': 'measurement_end',
            'trial_index': 0, 'result': live, 'measurement_start_ns': 100, 'measurement_end_ns': 300}, 301)
        target = Path(self.folder.name) / 'full.json'
        worker.store.export(worker.session_id, target, 'json')
        recomputed = recompute_recording(json.loads(target.read_text()))['results'][0]
        self.assertEqual(recomputed['per_task_throughput'], live['per_task_throughput'])
        self.assertEqual(recomputed['background_coverage_ns'], live['background_coverage_ns'])

    def test_ready_binds_calibration_before_preflight(self):
        window = self.window
        window.experiment = {'kind': 'heldout_calibration', 'phase': 'session', 'config': {}}
        ready = {'kind': 'ready', 'session_id': 'session', 'mono_ns': 0, 'priority_supported': False,
                 'image_id': 'sha256:abc', 'allowed_cpus': [0, 1], 'workload_cpu': 0,
                 'collector_cpu': 1, 'shared_collector': False}
        window.handle_update(ready)
        with patch.object(window, 'experiment_next_trial') as start:
            window.handle_update({'kind': 'ack', 'op': 'priority_preflight', 'ok': True,
                                  'session_id': 'session', 'priority_supported': True})
            start.assert_called_once()
        self.assertEqual(window.experiment['config']['sandbox']['image_id'], 'sha256:abc')
        self.assertEqual(window.experiment['config']['sandbox']['workload_cpu'], 0)

    def test_background_telemetry_loss_restores_without_double_counting(self):
        import time
        self.running()
        window = self.window
        window.set_auto(True)
        foreground, background = window.workloads.values()
        background.update(nice=10, stale=True, series=[])
        for end in (5, 5, 10):
            foreground.update(stale=False, receipt_ns=time.monotonic_ns(),
                              sample={'miss_rate': .3, 'window_start_ns': (end - 5) * 10**9,
                                      'window_end_ns': end * 10**9})
            window.evaluate_auto(foreground)
            if end == 5:
                self.assertEqual(window.rule_state.invalid_windows, 1)
        self.assertFalse(window.auto_active)
        self.assertEqual(window.rule_state.invalid_windows, 2)
        self.assertTrue(any(item.get('restoration') and item['nice'] == 0 for item in window.pending.values()))

    def test_experimental_policy_settings_cannot_change_mid_trial(self):
        self.running()
        window = self.window
        window.set_auto(True)
        window.experiment = {'phase': 'measurement', 'cancelled': False}
        window.update_controls()
        self.assertFalse(window.auto_button.isEnabled())
        self.assertFalse(window.policy_choice.isEnabled())
        window.auto_button.click()
        self.assertTrue(window.auto_active)
