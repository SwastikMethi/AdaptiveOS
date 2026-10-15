import math
import unittest
from dataclasses import replace

from adaptive_os.cpu_rule import CpuWindow, ManagedTask


class Helper:
    ready = True
    def __init__(self):
        self.sent, self.replies = [], []
        self.cancelled = 0

    def submit(self, request_id, payload):
        self.sent.append((request_id, payload))

    def poll(self):
        return self.replies.pop(0) if self.replies else None

    def cancel(self):
        self.cancelled += 1

    def close(self):
        self.cancel()


def proposal(action='lower', **fields):
    return {'action': action, 'probabilities': {'hold': .1, 'lower': .8, 'restore': .1},
            'confidence': .7, 'model': 'test-v1', 'inference_ms': 10, **fields}


class ControllerTest(unittest.TestCase):
    def setUp(self):
        from adaptive_os.decision_controller import PolicyController
        self.time = 0.0
        self.helper = Helper()
        self.tasks = [ManagedTask('b', 'Low', 50, 0, 0, 42)]
        self.controller = PolicyController(self.helper, clock=lambda: self.time)
        self.controller.activate('s', 't', ('f', 7), self.tasks, 'jev', shadow=False)

    def observe(self, end=10, miss=.01, valid=True):
        return self.controller.observe(CpuWindow(end - 5, end, miss, 50, valid), self.tasks,
                                       ('f', 7), foreground_p95_ms=0, work={'b': 100})

    def reply(self, action='lower', **fields):
        self.helper.replies.append({'request_id': self.helper.sent[-1][0],
                                    'proposal': proposal(action, **fields)})
        return self.controller.poll(self.tasks, ('f', 7))

    def prime(self):
        self.observe(5)
        self.reply('hold', probabilities={'hold': .8, 'lower': .1, 'restore': .1})

    def test_models_choose_timing_independently_but_need_ten_seconds(self):
        self.observe(5)
        self.assertEqual(self.reply().action, 'hold')
        self.observe(10)
        result = self.reply()
        self.assertEqual((result.action, result.target, result.nice), ('lower', 'b', 10))

    def test_deadline_is_exclusive_and_includes_validation(self):
        self.prime()
        self.observe()
        self.time = 1.0
        self.assertEqual(self.reply().action, 'hold')
        self.assertEqual(self.controller.failures, 1)

    def test_isolated_timeout_discards_late_reply_and_recovers_next_window(self):
        events = []
        self.controller.audit = lambda **event: events.append(event)
        self.prime()
        self.observe(10)
        expired = self.helper.sent[-1][0]
        self.time = 1
        self.assertEqual(self.controller.poll(self.tasks, ('f', 7)).action, 'hold')
        self.assertEqual(self.helper.cancelled, 0)
        self.time = 1.4
        self.helper.replies.append({'request_id': expired, 'proposal': proposal(),
                                    'helper_cpu_seconds': .9, 'helper_max_rss': 100})
        self.assertIsNone(self.controller.poll(self.tasks, ('f', 7)))
        self.assertEqual(self.controller.failures, 1)
        self.assertTrue(any(e.get('reason') == 'late_response' and e['request_id'] == expired for e in events))
        self.time = 5
        self.observe(15)
        self.assertEqual(self.reply().action, 'lower')
        self.assertEqual(self.controller.failures, 0)
        self.assertEqual(self.controller.effective, 'jev')
        self.assertEqual(self.controller.state.valid_seconds, 15)

    def test_transient_provider_unavailable_keeps_helper_for_next_window(self):
        self.prime()
        self.observe(10)
        self.helper.replies.append({'request_id': self.helper.sent[-1][0], 'error': 'unavailable'})
        self.assertEqual(self.controller.poll(self.tasks, ('f', 7)).action, 'hold')
        self.assertEqual(self.helper.cancelled, 0)
        self.assertEqual(self.controller.failures, 1)
        self.time = 5
        self.observe(15)
        self.assertEqual(self.reply().action, 'lower')
        self.assertEqual(self.controller.effective, 'jev')
        self.assertEqual(self.controller.failures, 0)

    def test_two_provider_unavailable_responses_still_latch_rule(self):
        events = []
        self.controller.audit = lambda **event: events.append(event)
        self.prime()
        for index, end in enumerate((10, 15)):
            self.time = index * 5
            self.observe(end)
            self.helper.replies.append({'request_id': self.helper.sent[-1][0], 'error': 'unavailable'})
            self.controller.poll(self.tasks, ('f', 7))
        self.assertEqual(self.helper.cancelled, 1)
        self.assertEqual(self.controller.effective, 'rule')
        self.assertEqual(self.controller.failures, 2)
        self.assertEqual([event['reason'] for event in events if event['kind'] == 'policy_fallback'],
                         ['unavailable', 'unavailable'])

    def test_hung_request_never_queues_a_second_window_and_latches_rule(self):
        self.prime()
        self.observe(10)
        sent = len(self.helper.sent)
        self.time = 1
        self.controller.poll(self.tasks, ('f', 7))
        self.time = 5
        decision = self.observe(15)
        self.assertEqual(decision.action, 'hold')
        self.assertEqual(len(self.helper.sent), sent)
        self.assertEqual(self.controller.effective, 'rule')
        self.assertEqual(self.controller.failures, 2)
        self.assertEqual(self.helper.cancelled, 1)

    def test_late_model_reply_cannot_undo_acknowledged_rule_fallback_state(self):
        self.observe(5, miss=.3)
        self.reply('hold', probabilities={'hold': .8, 'lower': .1, 'restore': .1})
        self.observe(10, miss=.3)
        expired = self.helper.sent[-1][0]
        self.time = 1
        decision = self.controller.poll(self.tasks, ('f', 7))
        self.assertEqual(decision.action, 'lower')
        self.controller.command_sent('fallback-ack', decision)
        self.assertTrue(self.controller.acknowledge('fallback-ack', 'b', 42, 10, 11))
        self.tasks = [replace(self.tasks[0], current_nice=10, intervention_order=1)]
        self.helper.replies.append({'request_id': expired, 'proposal': proposal()})
        self.time = 1.5
        self.assertIsNone(self.controller.poll(self.tasks, ('f', 7)))
        self.time = 5
        self.observe(15, miss=.3)
        self.assertEqual(self.reply('restore', probabilities={'hold': .1, 'lower': .1, 'restore': .8}).action, 'hold')
        self.assertEqual(self.controller.state.last_change, 11)
        self.assertEqual(self.controller.captures['b'].original_nice, 0)
        self.assertEqual(self.controller.state.valid_seconds, 15)

    def test_stop_terminates_expired_inference_and_rejects_its_late_reply(self):
        self.observe(5)
        expired = self.helper.sent[-1][0]
        self.time = 1
        self.controller.poll(self.tasks, ('f', 7))
        self.assertEqual(self.helper.cancelled, 0)
        self.controller.stop()
        self.assertEqual(self.helper.cancelled, 1)
        self.helper.replies.append({'request_id': expired, 'proposal': proposal()})
        self.assertIsNone(self.controller.poll(self.tasks, ('f', 7)))

    def test_timeout_during_validation_does_not_wait_for_an_already_consumed_reply(self):
        self.prime()
        self.observe(10)
        original = self.controller.guarded
        def slow_guard(*args, **kwargs):
            result = original(*args, **kwargs)
            self.time = 1
            return result
        self.controller.guarded = slow_guard
        self.reply()
        self.controller.guarded = original
        self.time = 5
        self.assertIsNone(self.observe(15))
        self.assertEqual(self.reply().action, 'lower')
        self.assertEqual(self.controller.failures, 0)

    def test_duplicate_windows_do_not_dispatch_or_double_count(self):
        self.observe(5)
        self.observe(5)
        self.assertEqual(len(self.helper.sent), 1)
        self.assertEqual(self.controller.state.valid_seconds, 5)

    def test_newer_window_rejects_old_response_without_queueing(self):
        self.observe(5)
        old = self.helper.sent[-1][0]
        self.observe(10)
        self.helper.replies.append({'request_id': old, 'proposal': proposal()})
        self.assertIsNone(self.controller.poll(self.tasks, ('f', 7)))
        self.assertEqual(self.controller.state.valid_seconds, 10)
        self.assertGreaterEqual(self.helper.cancelled, 1)

    def test_stop_or_mode_change_invalidates_inference(self):
        self.prime()
        self.observe()
        old = self.helper.sent[-1][0]
        self.controller.stop()
        self.helper.replies.append({'request_id': old, 'proposal': proposal()})
        self.assertIsNone(self.controller.poll(self.tasks, ('f', 7)))

    def test_process_exit_replacement_and_changed_importance_reject_reply(self):
        self.prime()
        for tasks in ([], [replace(self.tasks[0], identity=99)],
                      [replace(self.tasks[0], importance='High')]):
            with self.subTest(tasks=tasks):
                self.observe(10 + len(self.helper.sent) * 5)
                self.helper.replies.append({'request_id': self.helper.sent[-1][0], 'proposal': proposal()})
                result = self.controller.poll(tasks, ('f', 7))
                self.assertNotIn(result.action, ('lower', 'restore'))

    def test_two_failures_latch_rule_preserving_captures_and_cooldown(self):
        self.prime()
        self.observe(10)
        result = self.reply()
        self.controller.command_sent('ack1', result)
        self.assertTrue(self.controller.acknowledge('ack1', 'b', 42, 10, 11))
        self.tasks = [replace(self.tasks[0], current_nice=10, intervention_order=1)]
        for end in (15, 20):
            decision = self.observe(end, miss=.2)
            if decision is None:
                self.time += 1
                decision = self.controller.poll(self.tasks, ('f', 7))
            self.assertEqual(decision.action, 'hold')
        self.assertEqual(self.controller.effective, 'rule')
        self.assertEqual(self.controller.state.last_change, 11)
        self.assertEqual(self.controller.captures['b'].original_nice, 0)
        self.assertEqual(self.controller.state.valid_seconds, 20)

    def test_shadow_never_changes_even_when_model_or_fallback_would(self):
        self.controller.shadow = True
        self.prime()
        self.observe(10, miss=.3)
        self.assertEqual(self.reply().action, 'hold')
        for end in (15, 20, 25):
            self.observe(end, miss=.3)
            self.time += 1
            decision = self.controller.poll(self.tasks, ('f', 7))
            if decision:
                self.assertEqual(decision.action, 'hold')

    def test_only_verified_ack_starts_cooldown_and_blocks_overlap(self):
        self.prime()
        self.observe()
        result = self.reply()
        self.controller.command_sent('r', result)
        self.observe(15)
        self.assertEqual(self.reply().action, 'hold')
        self.assertFalse(self.controller.acknowledge('r', 'b', 99, 10, 16))
        self.assertIsNone(self.controller.state.last_change)

    def test_missing_cpu_is_unknown_and_never_lowered(self):
        self.prime()
        self.tasks = [replace(self.tasks[0], cpu_pct=None)]
        self.observe()
        self.assertNotEqual(self.reply().action, 'lower')
        self.assertIsNone(self.helper.sent[-1][1]['backgrounds'][0]['cpu_pct'])

    def test_seven_backgrounds_and_trend_are_complete_and_anonymous(self):
        self.tasks = [ManagedTask(f'secret-id-{i}', 'Normal', 7, 0, 0, i + 1) for i in range(7)]
        self.controller.activate('private-session', 'private-trial', ('private-foreground', 7),
                                 self.tasks, 'jev', shadow=True)
        self.observe(5)
        payload = self.helper.sent[-1][1]
        self.assertEqual(len(payload['backgrounds']), 7)
        self.assertNotIn('secret', str(payload))
        self.assertNotIn('private', str(payload))
        self.assertEqual({row['label'] for row in payload['backgrounds']}, {f'b{i}' for i in range(1, 8)})

    def test_malformed_nonfinite_or_oversized_proposals_fail_closed(self):
        from adaptive_os.decision_controller import ModelProposal
        for fields in ({'action': 'kill'}, {'confidence': math.nan},
                       {'probabilities': {'hold': .1, 'lower': math.inf, 'restore': .1}},
                       {'probabilities': {'hold': .1, 'lower': .2, 'restore': .1}},
                       {'model': 'x' * 10000}, {'inference_ms': -1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ModelProposal.parse(proposal(**fields))

    def test_four_decimal_provider_probabilities_allow_rounding_error(self):
        from adaptive_os.decision_controller import ModelProposal
        parsed = ModelProposal.parse(proposal(probabilities={'hold': .1004, 'lower': .7506, 'restore': .1491}))
        self.assertEqual(parsed.action, 'lower')

    def test_nonfinite_context_cannot_trigger_rule_fallback_change(self):
        self.observe(5, miss=.3)
        self.reply('hold', probabilities={'hold': .8, 'lower': .1, 'restore': .1})
        self.tasks = [replace(self.tasks[0], cpu_pct=math.inf)]
        result = self.observe(10, miss=.3)
        self.assertNotEqual(result.action, 'lower')

    def test_state_is_not_recaptured_on_redundant_activation(self):
        self.prime()
        self.observe()
        result = self.reply()
        self.controller.command_sent('r', result)
        self.controller.acknowledge('r', 'b', 42, 10, 11)
        with self.assertRaises(ValueError):
            self.controller.activate('s', 't', ('f', 7), [replace(self.tasks[0], current_nice=10)], 'jev')

    def test_latest_measurements_are_revalidated_before_applying(self):
        self.prime()
        self.observe()
        self.tasks = [replace(self.tasks[0], cpu_pct=math.inf)]
        self.assertNotEqual(self.reply().action, 'lower')

    def test_duplicate_current_processes_cannot_inflate_cpu_guard(self):
        self.prime()
        self.tasks = [replace(self.tasks[0], cpu_pct=11)]
        self.observe()
        self.tasks *= 2
        self.assertEqual(self.reply().action, 'restore_all')

    def test_command_and_ack_link_to_the_exact_model_proposal(self):
        events = []
        self.controller.audit = lambda **event: events.append(event)
        self.prime()
        self.observe()
        model_id = self.helper.sent[-1][0]
        decision = self.reply()
        self.controller.command_sent('control1', decision)
        self.controller.acknowledge('control1', 'b', 42, 10, 11)
        for event in events[-2:]:
            self.assertEqual(event['model_request_id'], model_id)
            self.assertEqual(event['request_id'], 'control1')

    def test_deadline_includes_current_eligibility_validation(self):
        self.prime()
        self.observe()
        original = self.controller.guarded
        def slow_guard(*args, **kwargs):
            result = original(*args, **kwargs)
            self.time = 1.0
            return result
        self.controller.guarded = slow_guard
        self.assertEqual(self.reply().action, 'hold')
        self.assertEqual(self.controller.fallback_reason, 'deadline')

    def test_ack_cannot_move_cooldown_back_before_command_window(self):
        self.prime()
        self.observe()
        self.controller.command_sent('r', self.reply())
        self.assertFalse(self.controller.acknowledge('r', 'b', 42, 10, 9))
        self.assertIsNone(self.controller.state.last_change)
