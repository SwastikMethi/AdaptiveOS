import unittest
from adaptive_os.experiments import trial_plan, comparison_report


class ModelExperimentTest(unittest.TestCase):
    def test_calibration_binding_requires_image_affinity_and_importance(self):
        from adaptive_os.experiments import calibration_key
        config = {'budget_ms': 6, 'backgrounds': 3, 'importance': 'Normal',
                  'sandbox': {'image_id': 'sha256:abc', 'workload_cpu': 0, 'collector_cpu': 1,
                              'allowed_cpus': [0, 1], 'shared_collector': False}}
        baseline = calibration_key(config)
        self.assertIsNotNone(baseline)
        self.assertIsNone(calibration_key({**config, 'sandbox': {}}))
        for change in ({'importance': 'High'}, {'backgrounds': 7},
                       {'sandbox': {**config['sandbox'], 'image_id': 'sha256:changed'}},
                       {'sandbox': {**config['sandbox'], 'workload_cpu': 1}}):
            self.assertNotEqual(baseline, calibration_key({**config, **change}))

    def test_five_arm_plan_has_five_paired_blocks_per_profile(self):
        plan = trial_plan('model_comparison', seed=42)
        self.assertEqual(len(plan), 50)
        self.assertEqual(plan, trial_plan('model_comparison', seed=42))
        for profile in ('light', 'contention'):
            for block in range(5):
                rows = [row for row in plan if row['profile'] == profile and row['block'] == block]
                self.assertEqual({row['arm'] for row in rows}, {'default', 'manual10', 'rule', 'jev', 'laya'})
                self.assertEqual({row['measurement_seconds'] for row in rows}, {180})

    def test_shadow_plans_apply_fixed_nice_zero(self):
        for provider in ('jev', 'laya'):
            rows = trial_plan('shadow_' + provider)
            self.assertTrue(all(row['arm'] == 'shadow:' + provider and row['fixed_nice'] == 0 for row in rows))

    def test_heldout_calibration_uses_only_six_ms(self):
        rows = trial_plan('heldout_calibration')
        self.assertEqual(len(rows), 9)
        self.assertEqual({row['budget_ms'] for row in rows}, {6})

    def test_model_usefulness_requires_improvement_over_rule_on_misses(self):
        rows = [{**row, 'valid': True, 'miss_rate': {'default': .1, 'manual10': .02, 'rule': .04,
                 'jev': .05, 'laya': .03}[row['arm']], 'p95_lateness_ms': 0,
                 'background_throughput': 80, 'per_task_throughput': {'b': 80},
                 'fallback_windows': 3 if row['arm'] in ('jev', 'laya') else 0, 'decision_windows': 36}
                for row in trial_plan('model_comparison')]
        report = comparison_report(rows)
        contention = report['profiles']['contention']
        self.assertFalse(contention['providers']['jev']['useful'])
        self.assertTrue(contention['providers']['laya']['useful'])
        self.assertIn('manual10', contention['arms'])
        self.assertEqual(contention['arms']['jev']['fallback_fraction'], 3 / 36)
        zero = [{**row, 'miss_rate': 0} for row in rows]
        self.assertFalse(comparison_report(zero)['profiles']['contention']['providers']['laya']['useful'])
        self.assertFalse(comparison_report(rows + [rows[0]])['profiles']['light']['paired_complete'])

    def test_inference_costs_and_each_background_loss_are_reported(self):
        rows = [{**row, 'valid': True, 'miss_rate': .1, 'p95_lateness_ms': 2,
                 'background_throughput': 100 if row['arm'] == 'default' else 80,
                 'per_task_throughput': {'b': 100 if row['arm'] == 'default' else 80},
                 'inference_overhead': {'requests': 36, 'helper_cpu_seconds': 2,
                    'helper_peak_rss_bytes': 1000, 'warm_latency_ms': {'p95': 200}}}
                for row in trial_plan('model_comparison')]
        profile = comparison_report(rows)['profiles']['contention']
        self.assertEqual(profile['arms']['laya']['per_task_loss_vs_default_pct']['b'], 20)
        self.assertEqual(len(profile['arms']['laya']['inference_overhead']), 5)

    def test_late_rejected_inference_still_counts_toward_resource_cost(self):
        from adaptive_os.experiments import inference_summary
        summary = inference_summary([
            {'kind': 'decision_context'},
            {'kind': 'policy_fallback', 'reason': 'deadline'},
            {'kind': 'model_rejection', 'reason': 'late_response', 'latency_ms': 1400,
             'helper_cpu_seconds': .8, 'helper_max_rss': 2000},
        ])
        self.assertEqual(summary['accepted_responses'], 0)
        self.assertEqual(summary['timeouts'], 1)
        self.assertEqual(summary['observed_helper_cpu_seconds'], .8)
        self.assertEqual(summary['helper_peak_rss_bytes'], 2000)
