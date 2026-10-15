import unittest

from adaptive_os.experiments import (calibration_plan, comparison_report, qualifies_calibration,
                                     summarize_trial, trial_plan)


class ExperimentTest(unittest.TestCase):
    def test_responsiveness_plan_is_reproducible_complete_and_180_seconds(self):
        first = trial_plan('responsiveness', seed=42)
        self.assertEqual(first, trial_plan('responsiveness', seed=42))
        self.assertEqual(len(first), 30)
        for profile in ('light', 'contention'):
            for block in range(5):
                rows = [row for row in first if row['profile'] == profile and row['block'] == block]
                self.assertEqual({row['arm'] for row in rows}, {'default', 'manual10', 'rule'})
                self.assertEqual({row['measurement_seconds'] for row in rows}, {180})

    def test_calibration_keeps_all_budgets_repetitions_and_pairing(self):
        rows = calibration_plan()
        self.assertEqual(len(rows), 72)
        self.assertEqual({row['budget_ms'] for row in rows}, {2, 4, 6, 8, 10, 12, 14, 16})
        self.assertEqual({row['profile'] for row in rows}, {'solo', 'light', 'contention'})
        self.assertEqual({row['repetition'] for row in rows}, {0, 1, 2})

    def test_seven_background_coverage_is_separate_and_300_seconds(self):
        rows = trial_plan('coverage')
        self.assertEqual(rows, [{'block': 0, 'arm': 'rule', 'warmup_seconds': 5,
                                 'measurement_seconds': 300, 'control_coverage_only': True}])

    def test_report_applies_usefulness_gate_without_inventing_zero_baseline_gain(self):
        rows = [{'block': block, 'arm': arm, 'miss_rate': .10 if arm == 'default' else .07,
                 'p95_lateness_ms': 2 if arm == 'default' else 1.5,
                 'background_throughput': 100 if arm == 'default' else 70}
                for block in range(5) for arm in ('default', 'manual10', 'rule')]
        report = comparison_report(rows)
        self.assertTrue(report['useful'])
        self.assertEqual(len(report['paired_differences']), 5)
        self.assertFalse(comparison_report(rows[:2])['useful'])
        zero = comparison_report([
            {'block': 0, 'arm': 'default', 'miss_rate': 0, 'p95_lateness_ms': 0,
             'background_throughput': 100},
            {'block': 0, 'arm': 'rule', 'miss_rate': 0, 'p95_lateness_ms': 0,
             'background_throughput': 100},
            {'block': 0, 'arm': 'manual10', 'miss_rate': 0, 'p95_lateness_ms': 0,
             'background_throughput': 100},
        ])
        self.assertIsNone(zero['foreground_improvement_pct'])
        self.assertFalse(zero['useful'])

    def test_exact_tick_and_interval_weighted_summary(self):
        ticks = [{'scheduled_ns': n, 'deadline_ns': n + 20_000_000,
                  'completion_ns': n + 21_000_000, 'skipped': False}
                 for n in range(0, 100_000_000, 20_000_000)]
        samples = [{'workload_id': 'bg', 'app_units_delta': 10,
                    'app_interval_start_ns': 0, 'app_interval_end_ns': 40_000_000},
                   {'workload_id': 'bg', 'app_units_delta': 60,
                    'app_interval_start_ns': 40_000_000, 'app_interval_end_ns': 100_000_000}]
        result = summarize_trial(ticks, samples, {'bg'}, 0, 100_000_000)
        self.assertTrue(result['valid'])
        self.assertEqual(result['miss_rate'], 1)
        self.assertEqual(result['p95_lateness_ms'], 1)
        self.assertEqual(result['per_task_throughput']['bg'], 700)
        self.assertEqual(result['cpu_allocation_pct'], {})

    def test_calibration_thresholds_are_fixed(self):
        self.assertTrue(qualifies_calibration(.005, .02, .021, 0))
        self.assertTrue(qualifies_calibration(.005, .02, .02, 1.01))
        self.assertFalse(qualifies_calibration(.0051, .02, .03, 2))
        self.assertFalse(qualifies_calibration(.005, .021, .03, 2))
        self.assertFalse(qualifies_calibration(.005, .02, .02, 1))

    def test_cpu_allocation_excludes_boundary_samples_outside_measurement(self):
        samples = [
            {'workload_id': 'bg', 'mono_ns': 999, 'cpu_pct': 100},
            {'workload_id': 'bg', 'mono_ns': 1000, 'cpu_pct': 20},
            {'workload_id': 'bg', 'mono_ns': 1999, 'cpu_pct': 40},
            {'workload_id': 'bg', 'mono_ns': 2000, 'cpu_pct': 90},
            {'workload_id': 'bg', 'cpu_pct': 95},
            {'workload_id': 'bg', 'mono_ns': None, 'cpu_pct': 95},
            {'workload_id': 'outside', 'mono_ns': 2001, 'cpu_pct': 80},
            {'workload_id': 'unknown', 'mono_ns': 1500, 'cpu_pct': None},
        ]
        result = summarize_trial(None, samples, set(), 1000, 2000)
        self.assertEqual(result['cpu_allocation_pct'], {'bg': 30})

    def test_boundary_retention_does_not_fill_real_throughput_gaps(self):
        samples = [
            {'workload_id': 'bg', 'app_units_delta': 300,
             'app_interval_start_ns': 4_000_000_000, 'app_interval_end_ns': 7_000_000_000},
            {'workload_id': 'bg', 'app_units_delta': 2400,
             'app_interval_start_ns': 9_000_000_000, 'app_interval_end_ns': 33_000_000_000},
            {'workload_id': 'bg', 'app_units_delta': 300,
             'app_interval_start_ns': 33_000_000_000, 'app_interval_end_ns': 36_000_000_000},
        ]
        result = summarize_trial(None, samples, {'bg'}, 5_000_000_000, 35_000_000_000)
        self.assertFalse(result['valid'])  # 28/30 seconds: below the 95% coverage threshold.
        self.assertEqual(result['exclusion_reasons'], ['incomplete background throughput coverage: bg'])
        self.assertIsNone(result['background_throughput'])

    def test_complete_trial_requires_every_distinct_tick_inside_exact_bounds(self):
        ticks = [{'scheduled_ns': n, 'deadline_ns': n + 20_000_000,
                  'completion_ns': n + 10_000_000, 'skipped': False}
                 for n in range(5_000_000_000, 185_000_000_000, 20_000_000)]
        self.assertEqual(len(ticks), 9000)
        complete = summarize_trial(ticks + [ticks[0]], [], set(), 5_000_000_000, 185_000_000_000)
        self.assertTrue(complete['valid'])
        self.assertEqual(complete['miss_rate'], 0)
        for missing in (0, 4500, 8999):
            with self.subTest(missing=missing):
                result = summarize_trial(ticks[:missing] + ticks[missing + 1:], [], set(),
                                         5_000_000_000, 185_000_000_000)
                self.assertFalse(result['valid'])
                self.assertEqual(result['exclusion_reasons'], ['incomplete foreground tick coverage'])
                self.assertIsNone(result['miss_rate'])


if __name__ == '__main__':
    unittest.main()
