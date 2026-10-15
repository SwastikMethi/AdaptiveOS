import copy
import unittest

from adaptive_os.experiments import summarize_trial


def packet(sequence, at, units, instance=10):
    return {'kind': 'raw', 'workload_id': 'bg',
            'data': {'sequence': sequence, 'start_ns': instance,
                     'mono_ns': at, 'units': units, 'ticks': []}}


class RawReportingTest(unittest.TestCase):
    def summary(self, records, samples=()):
        return summarize_trial(None, samples, {'bg'}, 100, 300, raw_records=records)

    def test_raw_counters_fill_sampled_gaps_and_clip_both_boundaries(self):
        records = [packet(0, 50, 0), packet(1, 150, 10),
                   packet(2, 250, 40), packet(3, 350, 50)]
        result = self.summary(records + [copy.deepcopy(records[1])])
        self.assertTrue(result['valid'])
        self.assertEqual(result['per_task_throughput'], {'bg': 200_000_000})
        self.assertEqual(result['background_coverage_ns'], {'bg': 200})
        self.assertEqual(result['report_version'], 2)
        self.assertEqual(result['throughput_source'], 'raw-cumulative-v1')

    def test_conflicting_duplicate_and_counter_reset_are_rejected(self):
        for bad in (packet(1, 200, 11), packet(2, 300, 9), packet(2, 150, 20)):
            with self.subTest(bad=bad):
                result = self.summary([packet(0, 100, 0), packet(1, 200, 10), bad])
                self.assertFalse(result['valid'])
                self.assertIsNone(result['background_throughput'])
                self.assertTrue(result['exclusion_reasons'])

    def test_missing_sequence_cannot_invent_coverage(self):
        result = self.summary([packet(0, 100, 0), packet(2, 300, 20)])
        self.assertFalse(result['valid'])
        self.assertEqual(result['background_coverage_ns'], {'bg': 0})

    def test_process_instances_are_never_bridged(self):
        result = self.summary([packet(0, 100, 0), packet(1, 200, 10),
                               packet(0, 250, 0, 250), packet(1, 300, 5, 250)])
        self.assertFalse(result['valid'])
        self.assertEqual(result['background_coverage_ns'], {'bg': 150})

    def test_overlapping_process_intervals_are_rejected(self):
        result = self.summary([packet(0, 100, 0), packet(1, 300, 20),
                               packet(0, 200, 0, 190), packet(1, 300, 10, 190)])
        self.assertFalse(result['valid'])

    def test_legacy_samples_deduplicate_without_filling_gaps(self):
        sample = {'workload_id': 'bg', 'app_units_delta': 10,
                  'app_interval_start_ns': 100, 'app_interval_end_ns': 200}
        result = summarize_trial(None, [sample, sample], {'bg'}, 100, 300)
        self.assertFalse(result['valid'])

    def test_raw_empty_never_falls_back_to_sampled_measurements(self):
        sample = {'workload_id': 'bg', 'app_units_delta': 10,
                  'app_interval_start_ns': 100, 'app_interval_end_ns': 300}
        self.assertFalse(self.summary([], [sample])['valid'])

    def test_conflicting_foreground_tick_is_rejected(self):
        tick = {'scheduled_ns': 0, 'completion_ns': 1,
                'deadline_ns': 20_000_000, 'skipped': False}
        result = summarize_trial([tick, {**tick, 'completion_ns': 20_000_001}],
                                 [], set(), 0, 20_000_000, raw_records=[])
        self.assertFalse(result['valid'])

    def test_recomputation_preserves_original_export(self):
        from adaptive_os.experiments import recompute_recording
        original = {'valid': False, 'per_task_throughput': {'bg': 0}, 'block': 0, 'arm': 'default'}
        recording = {'messages': [packet(0, 100, 0), packet(1, 300, 20),
            {'kind': 'trial_boundary', 'boundary': 'measurement_end', 'trial_index': 0,
             'result': original, 'measurement_start_ns': 100, 'measurement_end_ns': 300}]}
        before = copy.deepcopy(recording)
        result = recompute_recording(recording)
        self.assertEqual(recording, before)
        self.assertEqual(result['results'][0]['per_task_throughput'], {'bg': 100_000_000})
        self.assertEqual(result['report_version'], 2)
