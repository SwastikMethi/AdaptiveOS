import unittest

from adaptive_os.cpu_rule import CpuWindow, ManagedTask, RuleState, acknowledge, evaluate_rule


def task(name, importance, cpu, original=0, current=None, order=None):
    return ManagedTask(name, importance, cpu, original, original if current is None else current,
                       identity=1, intervention_order=order)


class CpuRuleTest(unittest.TestCase):
    def test_lowers_after_two_distinct_overload_windows_in_importance_order(self):
        state = RuleState()
        tasks = [task('high', 'High', 90), task('low-b', 'Low', 50), task('low-a', 'Low', 50)]
        decision, state = evaluate_rule(CpuWindow(0, 5, .03, 25, True), tasks, state)
        self.assertEqual(decision.action, 'hold')
        decision, state = evaluate_rule(CpuWindow(5, 10, .03, 25, True), tasks, state)
        self.assertEqual((decision.action, decision.target, decision.nice), ('lower', 'low-a', 10))
        duplicate, duplicate_state = evaluate_rule(CpuWindow(5, 10, .03, 25, True), tasks, state)
        self.assertEqual(duplicate.action, 'hold')
        self.assertEqual(duplicate_state, state)
        old, old_state = evaluate_rule(CpuWindow(0, 5, .03, 25, True), tasks, state)
        self.assertEqual(old.reason, 'duplicate or old window')
        self.assertEqual(old_state, state)

    def test_strict_thresholds_bounds_and_control_limit(self):
        state = RuleState(valid_seconds=10)
        tasks = [task('minimal', 'Low', 90, 19)]
        for start in (0, 5):
            decision, state = evaluate_rule(CpuWindow(start, start + 5, .02, 20, True), tasks, state)
            self.assertEqual(decision.action, 'hold')
        for start in (10, 15):
            decision, state = evaluate_rule(CpuWindow(start, start + 5, .03, 20, True), tasks, state)
        self.assertEqual((decision.action, decision.reason), ('hold', 'control limit reached'))

    def test_cooldown_starts_only_after_acknowledged_change(self):
        state = RuleState(valid_seconds=10, overload_windows=2)
        tasks = [task('low', 'Low', 50)]
        decision, state = evaluate_rule(CpuWindow(10, 15, .03, 50, True), tasks, state)
        self.assertEqual(decision.action, 'lower')
        state = acknowledge(state, 20)
        decision, state = evaluate_rule(CpuWindow(20, 25, .03, 50, True), tasks, state)
        self.assertEqual(decision.reason, 'cooldown')
        decision, _ = evaluate_rule(CpuWindow(45, 50, .03, 50, True), tasks, state)
        self.assertEqual(decision.action, 'lower')

    def test_restores_high_importance_then_reverse_intervention_order(self):
        tasks = [task('low', 'Low', 50, current=10, order=3),
                 task('high-old', 'High', 20, current=10, order=1),
                 task('high-new', 'High', 10, current=10, order=2)]
        state = RuleState(valid_seconds=10)
        for start in (0, 5, 10):
            decision, state = evaluate_rule(CpuWindow(start, start + 5, .004, 10, True), tasks, state)
        self.assertEqual((decision.action, decision.target, decision.nice), ('restore', 'high-new', 0))

    def test_two_invalid_windows_disable_and_request_full_restore(self):
        tasks = [task('low', 'Low', 50, current=10, order=1)]
        decision, state = evaluate_rule(CpuWindow(0, 5, None, None, False), tasks, RuleState())
        self.assertEqual(decision.action, 'hold')
        decision, state = evaluate_rule(CpuWindow(5, 10, None, None, False), tasks, state)
        self.assertEqual(decision.action, 'restore_all')
        self.assertTrue(state.disabled)


if __name__ == '__main__':
    unittest.main()
