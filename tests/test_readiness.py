import unittest


class ReadinessTest(unittest.TestCase):
    def test_frozen_contexts_cover_seven_tasks_and_are_reproducible(self):
        from adaptive_os.model_readiness import frozen_contexts
        first = frozen_contexts()
        self.assertEqual(first, frozen_contexts())
        self.assertGreaterEqual(len(first), 100)
        self.assertEqual({len(item['backgrounds']) for item in first}, {1, 3, 7})
        self.assertTrue(any(item['window']['miss_rate'] is None for item in first))
        self.assertEqual({len(item['trend']) for item in first}, {0, 1, 2, 3})
        self.assertTrue(any(row['cpu_pct'] is None for item in first for row in item['backgrounds']))

    def test_full_readiness_uses_real_five_second_dispatch_cadence(self):
        from tests.test_decision_controller import Helper, proposal
        from adaptive_os.model_readiness import ReadinessRun
        now = [0.0]
        helper = Helper()
        helper.metadata = {}
        run = ReadinessRun(helper, 'laya', 'mps', clock=lambda: now[0])
        run.poll()
        helper.replies.append({'request_id': helper.sent[-1][0], 'proposal': proposal()})
        now[0] = .2
        run.poll()
        self.assertEqual(len(run.rows), 1)
        self.assertEqual(len(helper.sent), 1)
        now[0] = 4.999
        run.poll()
        self.assertEqual(len(helper.sent), 1)
        now[0] = 5
        run.poll()
        self.assertEqual(len(helper.sent), 2)

    def test_gate_needs_100_and_95_percent_in_time_and_full_audit(self):
        from adaptive_os.model_readiness import readiness_report
        rows = [{'valid': True, 'latency_ms': 100, 'audited': True, 'unauthorized_actions': 0}
                for _ in range(100)]
        self.assertFalse(readiness_report(rows[:99], 'fingerprint')['ready'])
        for row in rows[:5]:
            row['latency_ms'] = 1000
        self.assertTrue(readiness_report(rows, 'fingerprint')['ready'])
        rows[5]['valid'] = False
        self.assertFalse(readiness_report(rows, 'fingerprint')['ready'])
        rows[5]['valid'] = True
        rows[10]['audited'] = False
        self.assertFalse(readiness_report(rows, 'fingerprint')['ready'])
        rows[10]['audited'] = True
        rows[10]['unauthorized_actions'] = 1
        self.assertFalse(readiness_report(rows, 'fingerprint')['ready'])

    def test_device_or_provider_change_invalidates_evidence(self):
        from adaptive_os.model_readiness import configuration_fingerprint, readiness_matches
        a = configuration_fingerprint('laya', 'mps')
        b = configuration_fingerprint('laya', 'cpu')
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, configuration_fingerprint('jev', 'hosted'))
        self.assertFalse(readiness_matches({'ready': True, 'fingerprint': a}, b))

    def test_readiness_deadline_includes_response_validation(self):
        from unittest.mock import patch
        from tests.test_decision_controller import Helper, proposal
        from adaptive_os.model_readiness import ReadinessRun, ModelProposal
        now = [0.0]
        helper = Helper()
        helper.metadata = {}
        run = ReadinessRun(helper, 'jev', 'hosted', connection_only=True, clock=lambda: now[0])
        run.poll()
        helper.replies.append({'request_id': helper.sent[-1][0], 'proposal': proposal()})
        original = ModelProposal.parse
        def slow_parse(value):
            parsed = original(value)
            now[0] = 1.0
            return parsed
        with patch.object(ModelProposal, 'parse', side_effect=slow_parse):
            run.poll()
        self.assertFalse(run.rows[0]['valid'])
        self.assertEqual(run.rows[0]['error'], 'deadline')

    def test_restart_preserves_initial_cold_start_and_records_each_startup(self):
        from tests.test_decision_controller import Helper, proposal
        from adaptive_os.model_readiness import ReadinessRun

        class RestartingHelper(Helper):
            metadata = {'host_cold_start_ms': 2500}

            def cancel(self):
                super().cancel()
                self.ready = False

            def start(self):
                self.ready = True
                self.metadata = {'host_cold_start_ms': 800}

        now = [0.0]
        helper = RestartingHelper()
        run = ReadinessRun(helper, 'laya', 'mps', clock=lambda: now[0])
        run.contexts = run.contexts[:2]
        run.poll()
        now[0] = 1.0
        run.poll()
        now[0] = 5.0
        run.poll()
        helper.replies.append({'request_id': helper.sent[-1][0], 'proposal': proposal()})
        now[0] = 5.2
        run.poll()
        self.assertTrue(run.done)
        self.assertEqual(run.report['cold_start_ms'], 2500)
        self.assertEqual(run.report['helper_startups'], [
            {'before_context': 0, 'cold_start_ms': 2500},
            {'before_context': 1, 'cold_start_ms': 800}])
        self.assertEqual(run.report['cold_start_latency_ms'], {'p50': 800, 'p95': 2500, 'p99': 2500})

    def test_timeout_reports_deadline_detection_separately_from_cleanup(self):
        from tests.test_decision_controller import Helper
        from adaptive_os.model_readiness import ReadinessRun
        now = [0.0]

        class SlowCleanupHelper(Helper):
            metadata = {'host_cold_start_ms': 1200}

            def cancel(self):
                super().cancel()
                now[0] += .2

        helper = SlowCleanupHelper()
        run = ReadinessRun(helper, 'laya', 'mps', connection_only=True, clock=lambda: now[0])
        run.poll()
        now[0] = 1.0
        run.poll()
        self.assertEqual(helper.cancelled, 1)
        self.assertFalse(run.rows[0]['valid'])
        self.assertEqual(run.rows[0]['latency_ms'], 1000)
        self.assertAlmostEqual(run.rows[0]['cleanup_ms'], 200)
        self.assertEqual(run.report['warm_latency_ms']['p99'], 1000)
