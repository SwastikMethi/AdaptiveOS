"""Decision activity must describe control evidence, never infer an applied action."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import queue
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from adaptive_os.cpu_rule import CpuWindow, ManagedTask
from adaptive_os.decision_controller import PolicyController
from adaptive_os.session_store import SessionStore
from tests.test_decision_controller import Helper, proposal


class DecisionAuditTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.now = 100.
        self.helper = Helper()
        self.tasks = [ManagedTask('b', 'Low', 70, 0, 0, 42)]
        self.policy = PolicyController(self.helper, clock=lambda: self.now,
                                       audit=lambda **e: self.events.append(e))
        self.policy.activate('session', 'trial', ('f', 7), self.tasks, 'jev')

    def observe(self, end):
        return self.policy.observe(CpuWindow(end-5, end, .3, 70, True), self.tasks, ('f', 7))

    def test_dispatch_is_distinct_from_context_and_failed_call_has_rule_source(self):
        self.observe(5)
        sent = [e for e in self.events if e['kind'] == 'model_dispatched']
        self.assertEqual(len(sent), 1, 'Context preparation must not be labelled as a dispatched call')
        self.assertEqual(sent[0]['host_monotonic_ns'], 100_000_000_000)
        self.now = 101.
        self.policy.poll(self.tasks, ('f', 7))
        result = [e for e in self.events if e['kind'] == 'decision_result'][-1]
        self.assertEqual(result['source'], 'rule')
        self.assertEqual(result['cycle_id'], sent[0]['cycle_id'])
        self.helper.ready = False
        self.now = 105.
        self.observe(10)
        self.assertEqual(len([e for e in self.events if e['kind'] == 'model_dispatched']), 1)
        self.assertEqual(self.policy.effective, 'rule')

    def test_ack_keeps_original_cycle_when_a_new_window_arrives(self):
        self.observe(5)
        self.helper.replies.append({'request_id': self.helper.sent[-1][0], 'proposal': proposal()})
        self.policy.poll(self.tasks, ('f', 7))
        self.observe(10)
        self.helper.replies.append({'request_id': self.helper.sent[-1][0], 'proposal': proposal()})
        decision = self.policy.poll(self.tasks, ('f', 7))
        self.policy.command_sent('command', decision)
        command = self.events[-1]
        self.observe(15)
        self.assertTrue(self.policy.acknowledge('command', 'b', 42, 10, 15.1))
        ack = self.events[-1]
        self.assertIsNotNone(command.get('cycle_id'))
        self.assertEqual(ack['cycle_id'], command['cycle_id'])
        self.assertEqual(ack['source'], 'jev')

    def test_stop_rejection_keeps_the_inflight_cycle(self):
        self.observe(5)
        cycle = [e for e in self.events if e['kind'] == 'decision_context'][-1]['cycle_id']
        self.policy.stop()
        cancelled = [e for e in self.events if e.get('reason') == 'cancelled'][-1]
        self.assertEqual(cancelled['cycle_id'], cycle)


class DecisionHistoryTest(unittest.TestCase):
    def test_pages_keep_complete_cycles_and_ignore_raw_log_limit(self):
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(Path(root) / 'sessions.db')
            self.addCleanup(store.close)
            session = str(uuid.uuid4())
            store.begin(session, {})
            for i in range(205):
                for kind in ('rule_reference', 'decision_context', 'model_dispatched', 'model_proposal',
                             'decision_result', 'decision_command', 'decision_acknowledgement'):
                    store.record(session, {'kind': kind, 'cycle_id': f'1:{i}', 'requested': 'jev',
                        'effective': 'jev', 'decision_window': f'{i*5}:{i*5+5}'}, i)
            self.assertTrue(hasattr(store, 'decision_page'), 'Full decision history requires its own page query')
            page = store.decision_page(session)
            self.assertEqual(len(page['cycles']), 100)
            self.assertEqual(page['cycles'][0]['cycle_id'], '1:204')
            self.assertEqual(len(page['cycles'][-1]['events']), 7)
            older = store.decision_page(session, before=page['oldest'])
            last = store.decision_page(session, before=older['oldest'])
            self.assertEqual(len(last['cycles']), 5)
            self.assertFalse(last['has_older'])
            newer = store.decision_page(session, after=last['newest'])
            self.assertEqual([c['cycle_id'] for c in newer['cycles']],
                             [c['cycle_id'] for c in older['cycles']])
            # A late acknowledgement updates its original cycle, not its chronological position.
            store.record(session, {'kind': 'decision_acknowledgement', 'cycle_id': '1:0'}, 999)
            self.assertEqual(store.decision_page(session)['cycles'][0]['cycle_id'], '1:204')

    def test_legacy_cycles_are_available_with_missing_fields_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(Path(root) / 'sessions.db')
            self.addCleanup(store.close)
            session = str(uuid.uuid4())
            store.begin(session, {})
            for generation in (1, 2):
                store.record(session, {'kind': 'policy_started', 'generation': generation}, generation)
                store.record(session, {'kind': 'rule_reference', 'decision_window': '0:5',
                                      'requested': 'rule'}, generation)
            self.assertTrue(hasattr(store, 'decision_page'))
            page = store.decision_page(session)
            self.assertEqual(len(page['cycles']), 2)
            self.assertNotEqual(page['cycles'][0]['cycle_id'], page['cycles'][1]['cycle_id'])
            self.assertNotIn('host_monotonic_ns', page['cycles'][0]['events'][0])


class DecisionPresentationTest(unittest.TestCase):
    def api(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('adaptive_os.decision_activity'),
                             'A decision view must separate proposals from confirmed actions')
        from adaptive_os.decision_activity import DecisionEntry, policy_status
        return DecisionEntry, policy_status

    def test_failed_dispatch_never_reports_model_response_latency(self):
        Entry, _ = self.api()
        entry = Entry('cycle')
        for event in [
            {'kind': 'rule_reference', 'requested': 'jev', 'effective': 'jev'},
            {'kind': 'decision_context', 'request_id': 'call'},
            {'kind': 'model_rejection', 'request_id': 'call', 'reason': 'helper_unavailable', 'latency_ms': .1},
            {'kind': 'policy_fallback', 'latched': True, 'reason': 'helper_unavailable'},
            {'kind': 'decision_result', 'source': 'rule', 'decision': {'action': 'hold'}, 'result': {'action': 'hold'}}
        ]:
            entry.add(event)
        columns = entry.columns(0, {})
        self.assertIn('Rule', columns[1])
        self.assertEqual(columns[-1], '—')
        self.assertIn('not sent', entry.details(0, {}).lower())
        self.assertIn('Failure detection', entry.details(0, {}))

    def test_only_matched_ack_marks_change_confirmed_and_late_reply_cannot_undo_it(self):
        Entry, _ = self.api()
        entry = Entry('cycle')
        events = [
            {'kind': 'rule_reference', 'requested': 'jev', 'effective': 'jev'},
            {'kind': 'model_dispatched', 'request_id': 'call'},
            {'kind': 'model_proposal', 'request_id': 'call', 'latency_ms': 403,
             'proposal': proposal(), 'guarded_decision': {'action': 'lower', 'target': 'b', 'nice': 10}},
            {'kind': 'decision_command', 'request_id': 'cmd', 'model_request_id': 'call', 'source': 'jev',
             'decision': {'action': 'lower', 'target': 'b', 'nice': 10}},
        ]
        for event in events:
            entry.add(event)
            entry.add(event)
        self.assertEqual(len(entry.events), 4)
        self.assertIn('Awaiting', entry.columns(0, {'b': 'CPU hashing'})[3])
        entry.add({'kind': 'decision_acknowledgement', 'request_id': 'wrong', 'decision': {'action': 'lower'}})
        self.assertNotIn('confirmed', entry.columns(0, {})[3])
        entry.add({'kind': 'decision_acknowledgement', 'request_id': 'cmd', 'acknowledged_at': 12.,
                   'source': 'jev', 'decision': {'action': 'lower', 'target': 'b', 'nice': 10}})
        entry.add({'kind': 'model_rejection', 'reason': 'late_response', 'latency_ms': 1400})
        self.assertIn('confirmed', entry.columns(0, {})[3])
        self.assertEqual(entry.acknowledged_at, 12.)
        self.assertEqual(entry.columns(0, {})[-1], '403 ms')

    def test_shadow_and_guard_blocked_calls_keep_the_proposal_visible(self):
        Entry, _ = self.api()
        for shadow, reason in [(True, 'guarded lower'), (False, 'cooldown')]:
            entry = Entry('cycle')
            entry.add({'kind': 'rule_reference', 'requested': 'laya', 'effective': 'laya', 'shadow': shadow})
            entry.add({'kind': 'model_proposal', 'proposal': proposal(), 'latency_ms': 400,
                       'guarded_decision': {'action': 'lower' if shadow else 'hold', 'reason': reason}})
            columns = entry.columns(0, {})
            self.assertIn('Lower', columns[2])
            self.assertIn('Shadow' if shadow else 'cooldown', columns[3])
            self.assertIsNone(entry.acknowledged_at)

    def test_countdown_is_separate_from_cooldown_and_pauses_in_fallback_or_history(self):
        _, status = self.api()
        p = PolicyController(Helper(), clock=lambda: 100.)
        p.activate('s', 't', ('f', 7), [ManagedTask('b', 'Low', 70, 0, 0, 42)], 'jev')
        p.window = CpuWindow(5, 10, .3, 70, True)
        p.state = replace(p.state, last_change=9, valid_seconds=10)
        kwargs = dict(now=112., selected='jev', running=True, history=False,
                      anchor=(0, 100_000_000_000), telemetry_receipt=112_000_000_000)
        view = status(p, **kwargs)
        self.assertIn('3.0', view['next_call'])
        self.assertIn('27', view['cooldown'])
        p.effective, p.fallback_reason = 'rule', 'helper_unavailable'
        view = status(p, **kwargs)
        self.assertIn('Paused', view['next_call'])
        self.assertIn('Rule', view['active'])
        self.assertIn('3.0', view['next_check'])
        kwargs['history'] = True
        self.assertNotIn('3.0', status(p, **kwargs)['next_check'])
        kwargs['history'], kwargs['now'] = False, 116.
        self.assertIn('telemetry', status(p, **kwargs)['next_check'].lower())

    def test_rule_details_show_observed_inputs_without_claiming_a_model_call(self):
        Entry, _ = self.api()
        events = []
        p = PolicyController(audit=lambda **e: events.append(e))
        tasks = [ManagedTask('b', 'Low', 70, 0, 0, 42)]
        p.activate('s', 't', ('f', 7), tasks, 'rule')
        p.observe(CpuWindow(0, 5, .4, 70, True), tasks, ('f', 7), foreground_p95_ms=3, work={'b': 1234})
        entry = Entry('rule')
        for event in events:
            entry.add(event)
        detail = entry.details(0, {'b': 'Hasher'})
        self.assertIn('40.0%', detail)
        self.assertIn('Hasher', detail)
        self.assertIn('1234.0/s', detail)
        self.assertIn('No model call', detail)

    def test_legacy_rule_decision_is_finished_not_a_pending_model_request(self):
        Entry, _ = self.api()
        entry = Entry('legacy')
        entry.add({'kind': 'cpu_decision', 'window_start_ns': 0, 'window_end_ns': 5_000_000_000,
                   'miss_rate': .4, 'background_cpu': 70, 'valid': True,
                   'action': 'hold', 'reason': 'initial observation'})
        cols = entry.columns(0, {})
        self.assertEqual(cols[1:3], ['Rule', 'Hold'])
        self.assertIn('No priority change', cols[3])


class DecisionDesktopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from adaptive_os.desktop import MainWindow
        from adaptive_os.desktop_worker import SessionWorker
        self.folder = tempfile.TemporaryDirectory()
        self.worker = SessionWorker(Path(self.folder.name) / 'session.db')
        self.window = MainWindow(self.worker)
        self.window.timer.stop()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.window.active = False
        self.window.close()

    def test_decisions_have_their_own_panel_and_manual_change_waits_for_verified_ack(self):
        w = self.window
        self.assertTrue(hasattr(w, 'decision_panel'), 'Decision controls must leave the workload sidebar')
        self.assertTrue(w.decision_panel.isAncestorOf(w.policy_choice))
        self.assertFalse(w.events.isVisible())
        w.active, w.session_id, w.origin_ns = True, 's', 0
        key = list(w.workloads)[1]
        w.workloads[key].update(status='running', identity=42)
        request_id = w.request_priority(key, 10)
        w.refresh_decision_activity()
        entry = next(iter(w.decision_panel.live.values()))
        self.assertIn('Awaiting', entry.columns(0, {})[3])
        w.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority', 'request_id': request_id,
                         'workload_id': key, 'identity': 42, 'nice': 10, 'mono_ns': 10_000_000_000})
        self.assertIn('confirmed', entry.columns(0, {})[3])
        self.assertEqual(len(w.cpu_chart.markers), 1)
        w.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority', 'request_id': request_id,
                         'workload_id': key, 'identity': 42, 'nice': 10, 'mono_ns': 10_000_000_000})
        self.assertEqual(len(w.cpu_chart.markers), 1)

    def test_failed_readback_is_not_plotted_and_new_session_clears_decisions(self):
        w = self.window
        self.assertTrue(hasattr(w, 'decision_panel'))
        w.active, w.session_id = True, 's'
        key = list(w.workloads)[1]
        w.workloads[key].update(status='running', identity=42)
        request_id = w.request_priority(key, 10)
        w.handle_update({'kind': 'ack', 'ok': True, 'op': 'priority', 'request_id': request_id,
                        'workload_id': key, 'identity': 99, 'nice': 10, 'mono_ns': 10_000_000_000})
        entry = next(iter(w.decision_panel.live.values()))
        self.assertIn('failed', entry.columns(0, {})[3])
        self.assertEqual(w.cpu_chart.markers, {})
        w.active = False
        w.new_playground()
        self.assertFalse(w.decision_panel.live)

    def test_browsing_stays_stable_and_late_events_do_not_replace_latest_confirmed_change(self):
        panel = self.window.decision_panel
        panel.origin_ns = 0
        for cycle, at in [('first', 10), ('second', 20)]:
            panel.ingest({'kind': 'priority_requested', 'cycle_id': cycle, 'source': 'manual',
                          'request_id': cycle, 'workload_id': 'b', 'before_nice': 0, 'nice': 10})
            panel.ingest({'kind': 'priority_outcome', 'cycle_id': cycle, 'request_id': cycle,
                          'confirmed': True, 'acknowledged_at': at})
        panel.render()
        panel.timeline.topLevelItem(1).setExpanded(True)
        keys = list(panel.displayed)
        panel.ingest({'kind': 'rule_reference', 'cycle_id': 'third', 'requested': 'rule'})
        self.assertEqual(list(panel.displayed), keys)
        panel.ingest({'kind': 'model_rejection', 'cycle_id': 'first', 'reason': 'late_response'})
        self.assertIn('00:20', panel.latest_change)

    def test_small_timeline_does_not_offer_an_empty_older_page(self):
        panel = self.window.decision_panel
        panel.ingest({'kind': 'rule_reference', 'cycle_id': '1', 'requested': 'rule'})
        panel.render()
        self.assertFalse(panel.older.isEnabled())

    def test_recorded_page_summary_and_accessibility_include_confirmed_change(self):
        panel = self.window.decision_panel
        panel.origin_ns = 0
        events = [
            {'kind': 'priority_requested', 'cycle_id': 'c', 'source': 'manual',
             'request_id': 'cmd', 'workload_id': 'b', 'before_nice': 0, 'nice': 10},
            {'kind': 'priority_outcome', 'cycle_id': 'c', 'request_id': 'cmd',
             'confirmed': True, 'acknowledged_at': 10}]
        panel.set_page({'cycles': [{'cycle_id': 'c', 'events': events}], 'workload_names': {'b': 'Hasher'}})
        self.assertIn('Hasher: nice 0 → 10 confirmed', panel.latest_change)
        self.window.history_mode = True
        self.window.refresh_decision_activity()
        for label in panel.status_labels.values():
            self.assertIn(label.text(), label.accessibleName())

    def test_minimum_size_keeps_chart_timeline_and_footer_from_overlapping(self):
        from PySide6.QtCore import QPoint
        w = self.window
        w.decision_panel.latest_change = '[UI fixture] Background 5: nice 10 → 0 confirmed · 00:45'
        w.decision_panel.update_status({'active': 'Rule fallback', 'next_call': 'Jev: Paused',
            'next_check': 'Rule check: Expected in ~4.5 s', 'cooldown': '20 s cooldown remaining',
            'explanation': 'Jev: Local model helper is not ready. Model calls paused until explicitly re-enabled.'})
        w.show()
        w.resize(1000, 740)
        self.app.processEvents()
        cpu_bottom = w.cpu_chart.mapTo(w, QPoint(0, w.cpu_chart.height())).y()
        panel_top = w.decision_panel.mapTo(w, QPoint(0, 0)).y()
        tree_bottom = w.decision_panel.timeline.mapTo(w, QPoint(0, w.decision_panel.timeline.height())).y()
        footer_top = w.decision_panel.older.mapTo(w, QPoint(0, 0)).y()
        self.assertLessEqual(cpu_bottom, panel_top)
        self.assertLessEqual(tree_bottom, footer_top)
        self.assertTrue(w.cpu_chart.parentWidget().rect().contains(w.cpu_chart.geometry()),
                        f'Chart clipped: {w.cpu_chart.geometry()} in {w.cpu_chart.parentWidget().rect()}; '
                        f'tabs {w.metric_tabs.size()}, hint {w.metric_tabs.minimumSizeHint()}, '
                        f'bar {w.metric_tabs.tabBar().sizeHint()}, window {w.size()}')

    def test_full_audit_queue_still_cancels_inference_without_recursive_stop(self):
        w = self.window
        w.active = w.auto_active = True
        helper = Helper()
        w.policy.helper = helper
        tasks = [ManagedTask('b', 'Low', 70, 0, 0, 42)]
        w.policy.activate('s', 't', ('f', 7), tasks, 'jev')
        w.policy.observe(CpuWindow(0, 5, .3, 70, True), tasks, ('f', 7))
        with patch.object(self.worker, 'submit', side_effect=queue.Full):
            try:
                w.set_auto(False)
            except RecursionError:
                self.fail('Audit failure recursively re-entered active policy shutdown')
        self.assertFalse(w.policy.active)
        self.assertFalse(w.auto_active)
        self.assertIsNone(w.policy.outstanding)
        self.assertEqual(helper.cancelled, 1)

    def test_older_page_keeps_ack_received_while_worker_was_reading(self):
        panel = self.window.decision_panel
        requested = {'kind': 'priority_requested', 'cycle_id': 'c', 'source': 'manual',
                     'request_id': 'cmd', 'workload_id': 'b', 'before_nice': 0, 'nice': 10}
        page = {'cycles': [{'cycle_id': 'c', 'events': [requested]}]}
        panel.ingest(requested)
        panel.ingest({'kind': 'priority_outcome', 'cycle_id': 'c', 'request_id': 'cmd',
                      'confirmed': True, 'acknowledged_at': 10})
        panel.set_page(page, live=False)
        self.assertIn('confirmed', panel.displayed['c'].columns(0, {})[3])

    def test_late_latest_page_does_not_unfreeze_a_row_opened_after_the_request(self):
        w = self.window
        w.session_id = 's'
        panel = w.decision_panel
        event = {'kind': 'rule_reference', 'cycle_id': 'c', 'requested': 'rule', 'decision': {'action': 'hold'}}
        panel.ingest(event)
        panel.render()
        panel.jump_latest()
        token = w.decision_page_token[0]
        panel.timeline.topLevelItem(0).setExpanded(True)
        self.assertFalse(panel.following)
        w.handle_update({'kind': 'decision_history', 'session_id': 's', 'token': token,
                         'page': {'cycles': [{'cycle_id': 'new', 'events': [event | {'cycle_id': 'new'}]},
                                             {'cycle_id': 'c', 'events': [event]}]}})
        self.assertFalse(panel.following)
        self.assertEqual(list(panel.displayed), ['c'])
        self.assertTrue(panel.timeline.topLevelItem(0).isExpanded())
