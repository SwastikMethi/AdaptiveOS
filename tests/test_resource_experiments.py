import unittest

from adaptive_os import experiments


class ResourceExperimentsTest(unittest.TestCase):
    def test_frozen_profile_sizes_are_part_of_resource_calibration_identity(self):
        from adaptive_os.resource_experiments import resource_calibration_key
        config = dict(resource='network', sandbox={'image_id': 'image'},
                      profiles={'light': {'response_kib': 16}, 'contention': {'response_kib': 64}})
        changed = dict(config, profiles={'light': {'response_kib': 32}, 'contention': {'response_kib': 64}})
        self.assertNotEqual(resource_calibration_key(config), resource_calibration_key(changed))

    def test_resource_comparison_has_five_paired_blocks_and_full_durations(self):
        plan = experiments.trial_plan('resource_comparison')
        self.assertEqual(len(plan), 40)
        for profile in ('light', 'contention'):
            for block in range(5):
                self.assertEqual({r['arm'] for r in plan if r['profile'] == profile and r['block'] == block},
                                 {'default', 'manual', 'rule', 'jev'})
        self.assertTrue(all(r['warmup_seconds'] == 30 and r['measurement_seconds'] == 180 for r in plan))
        self.assertEqual(plan, experiments.trial_plan('resource_comparison'))

    def test_resource_metrics_deduplicate_windows_and_reject_incomplete_evidence(self):
        from adaptive_os.resource_experiments import summarize_resource
        rows = []
        for seq, at, window in [(0, 0, None), (1, 5_000_000_000,
                dict(window_start_ns=0, window_end_ns=5_000_000_000, requests=100, misses=20,
                     failed_transfers=0, p95_latency_ms=4, latency_histogram=[0]*6+[100]+[0]*9)),
                (2, 6_000_000_000, None)]:
            for key in ('f', 'b'):
                rows.append(dict(workload_id=key, data=dict(sequence=seq, start_ns=0, mono_ns=at,
                    units=at//1_000_000, resource_window=window if key == 'f' else None)))
        result = summarize_resource(rows, [], 'f', {'b'}, 0, 5_000_000_000, 'memory')
        self.assertTrue(result['valid'], result['exclusion_reasons'])
        self.assertEqual(result['miss_rate'], .2)
        self.assertEqual(result['p95_lateness_ms'], 4)
        self.assertEqual(result['per_task_throughput'], {'b': 1000})
        missing = summarize_resource(rows, [], 'f', {'b'}, 0, 10_000_000_000, 'memory')
        self.assertFalse(missing['valid'])

    def test_four_arm_gate_keeps_fallback_trials_and_requires_rule_improvement(self):
        from adaptive_os.resource_experiments import resource_comparison
        trials = [dict(r, valid=True, miss_rate={'default': .5, 'manual': .4, 'rule': .3, 'jev': .2}[r['arm']],
                       background_throughput=80 if r['arm'] != 'default' else 100,
                       per_task_throughput={'b': 80 if r['arm'] != 'default' else 100},
                       p95_lateness_ms=50, controller_cpu_pct=1, decision_windows=36, fallback_windows=36)
                  for r in experiments.trial_plan('resource_comparison')]
        report = resource_comparison(trials, 'network', frozen=True)
        self.assertTrue(report['useful'])
        self.assertEqual(report['profiles']['contention']['arms']['jev']['fallback_fraction'], 1)
        for row in trials:
            if row['arm'] == 'jev':
                row['miss_rate'] = .3
        self.assertFalse(resource_comparison(trials, 'network', frozen=True)['useful'])
        self.assertFalse(resource_comparison(trials, 'network', frozen=False)['useful'])
