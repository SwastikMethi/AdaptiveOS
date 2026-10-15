import copy
import tempfile
import unittest
from pathlib import Path


def snapshot():
    return {'backend': 'guest-linux', 'protocol': 'guest-linux/1', 'generation': 'generation', 'run_id': 'run', 'service_fingerprint': 'a' * 64,
            'observed_at_ns': 100_000_000_000, 'preflight': {'passed': True, 'baseline': {'fingerprint': 'utm-plasma'}},
            'recovery_error': None, 'resource_control': None,
            'cpu_control': {'foreground_id': 'private-foreground', 'importance': {'private-foreground': 'High', 'private-background': 'Low'},
                            'expected': {'private-foreground': 100, 'private-background': 100}, 'ready': True,
                            'observation_id': 'observation', 'valid_seconds': 10, 'cooldown_seconds': 0},
            'tasks': [{'id': key, 'identity': 'private-boot-unit', 'app': app, 'profile': '/private/profile',
                       'cpu_weight': 100, 'cpu_usage_usec': 1000, 'cpu_pressure': 'some avg10=15.0 total=100\n',
                       'cpus_effective': '1-3', 'memory_current': 128 * 1024**2, 'memory_low': 0, 'memory_high': 'max'}
                      for key, app in [('private-foreground', 'firefox'), ('private-background', 'geany')]]}


class GuestPolicyTest(unittest.TestCase):
    def test_expired_observation_holds_without_replay_and_accepts_the_next_observation(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        for error in ('Fresh resource observation required', 'Fresh observation with ten valid seconds required'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                now, calls, events = [10.], [], []
                policy = GuestPolicy(Path(folder), clock=lambda: now[0])
                policy.mode = 'rule'
                def apply(op, **fields):
                    calls.append((op, fields))
                    if len(calls) == 1:
                        raise RuntimeError(error)
                    return {'action': 'favor'}
                policy.tick(snapshot(), apply, lambda **e: events.append(e))
                self.assertEqual(len(calls), 1)
                self.assertEqual(policy.fallbacks, 1)
                self.assertTrue(any(e.get('fallback_reason') == 'stale_observation' for e in events))
                now[0] += .1
                policy.tick(snapshot(), apply, lambda **e: events.append(e))
                self.assertEqual(len(calls), 1)
                now[0] = 15.
                policy.tick(snapshot(), apply, lambda **e: events.append(e))
                self.assertEqual(len(calls), 1)
                fresh = snapshot()
                fresh['cpu_control']['observation_id'] = 'fresh-observation'
                now[0] = 16.
                policy.tick(fresh, apply, lambda **e: events.append(e))
                self.assertEqual([fields['observation_id'] for _, fields in calls], ['observation', 'fresh-observation'])
                self.assertEqual(policy.fallbacks, 1)

    def test_expired_model_application_is_audited_and_fallback_is_counted_once(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        class Helper:
            ready = True
            key = None
            def submit(self, key, payload): self.key = key
            def poll(self):
                return {'request_id': self.key, 'proposal': {'action': 'hold', 'probabilities': {'hold': 1, 'favor': 0, 'restore': 0},
                        'confidence': 1, 'model': 'jev-1.13.0', 'inference_ms': 100}} if self.key else None
            def close(self): self.ready = False
        for elapsed in (.2, 1.001):
            with self.subTest(elapsed=elapsed), tempfile.TemporaryDirectory() as folder:
                now, events, calls = [10.], [], []
                policy = GuestPolicy(Path(folder), clock=lambda: now[0], helper=Helper())
                policy.mode = 'shadow'
                def apply(op, **fields):
                    calls.append((op, fields))
                    raise RuntimeError('Fresh resource observation required')
                policy.tick(snapshot(), apply, lambda **e: events.append(e))
                now[0] += elapsed
                policy.tick(snapshot(), apply, lambda **e: events.append(e))
                self.assertIsNone(policy.pending)
                self.assertEqual(len(calls), 1)
                self.assertEqual(policy.fallbacks, 1)
                audit = next(e for e in events if e['kind'] == 'guest_model_audit')
                self.assertFalse(audit['kernel_guard_passed'])
                self.assertFalse(audit['valid'])
                self.assertEqual(audit['fallback_reason'], 'stale_observation' if elapsed < 1 else 'deadline')
                self.assertEqual(policy.shadow_rows, [audit])

    def test_other_writer_failures_are_not_suppressed_or_retried(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        from unittest.mock import Mock
        for error in (RuntimeError('Network settings changed after observation'),
                      RuntimeError('Stale service generation'), TimeoutError('Guest response timed out')):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as folder:
                policy = GuestPolicy(Path(folder), clock=lambda: 10.)
                policy.mode = 'rule'
                apply = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    policy.tick(snapshot(), apply, lambda **e: None)
                self.assertEqual(apply.call_count, 1)
                self.assertEqual(policy.fallbacks, 0)

    def test_deadline_includes_poll_and_proposal_validation(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        from unittest.mock import patch
        now, events = [10.], []
        class Helper:
            ready = True
            key = None
            def submit(self, key, payload): self.key = key
            def poll(self):
                return {'request_id': self.key, 'proposal': {'action': 'hold', 'probabilities': {'hold': 1, 'favor': 0, 'restore': 0},
                        'confidence': 1, 'model': 'jev-1.13.0', 'inference_ms': 100}} if self.key else None
            def close(self): self.ready = False
        with tempfile.TemporaryDirectory() as folder:
            policy = GuestPolicy(Path(folder), clock=lambda: now[0], helper=Helper())
            policy.mode = 'shadow'
            policy.tick(snapshot(), lambda *a, **k: {}, lambda **e: events.append(e))
            from adaptive_os.guest_linux.policy import ModelProposal
            parse = ModelProposal.parse
            def slow_parse(*args, **kwargs):
                result = parse(*args, **kwargs)
                now[0] += 1.001
                return result
            with patch('adaptive_os.guest_linux.policy.ModelProposal.parse', side_effect=slow_parse):
                policy.tick(snapshot(), lambda *a, **k: {}, lambda **e: events.append(e))
            audit = [e for e in events if e['kind'] == 'guest_model_audit'][-1]
            self.assertEqual(audit['fallback_reason'], 'deadline')
            self.assertGreaterEqual(audit['latency_ms'], 1000)

    def test_authentication_failure_is_latched_and_not_retried_each_cycle(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        class Helper:
            ready = True
            busy = False
            key = None
            def submit(self, key, payload): self.key = key
            def poll(self):
                return {'request_id': self.key, 'error': 'authentication'} if self.key else None
            def close(self): self.ready = False
        now, events = [10.], []
        with tempfile.TemporaryDirectory() as folder:
            policy = GuestPolicy(Path(folder), key='never-record-this-key', clock=lambda: now[0], helper=Helper())
            policy.mode = 'shadow'
            policy.tick(snapshot(), lambda *a, **k: {}, lambda **e: events.append(e))
            now[0] += .1
            policy.tick(snapshot(), lambda *a, **k: {}, lambda **e: events.append(e))
            self.assertTrue(policy.authentication_failed)
            now[0] += 5
            from unittest.mock import patch
            with patch('adaptive_os.guest_linux.policy.new_helper', side_effect=AssertionError('authentication retried')):
                policy.tick(snapshot(), lambda *a, **k: {}, lambda **e: events.append(e))
            self.assertNotIn('never-record-this-key', str(events))

    def test_guest_jev_cpu_action_and_prompt_use_weights_and_favor(self):
        from adaptive_os.guest_linux.policy import GuestJevProvider, public_context
        def transport(payload, key):
            self.assertEqual(key, 'test-key')
            self.assertIn('CPU weights', payload['questions']['action']['instructions'])
            return {'model': 'jev-1.13.0', 'usage': {'input_tokens': 20},
                    'answers': {'action': {'type': 'choice', 'choice': 'favor', 'confidence': .9,
                                         'probabilities': {'hold': .05, 'favor': .9, 'restore': .05}}}}
        result = GuestJevProvider('test-key', transport=transport).predict(public_context(snapshot()))
        self.assertEqual(result['action'], 'favor')

    def test_guest_choices_exclude_unavailable_actions_and_keep_recording_vocabulary(self):
        from adaptive_os.guest_linux.policy import GuestJevProvider, public_context
        from adaptive_os.model_providers import ProviderError
        context = public_context(snapshot())
        context['available_actions'] = ['hold', 'restore']
        answer = {'type': 'choice', 'choice': 'hold', 'confidence': .9,
                  'probabilities': {'hold': .9, 'restore': .1}}
        def transport(payload, key):
            self.assertEqual(set(payload['questions']['action']['criteria']), {'hold', 'restore'})
            return {'model': 'jev-1.13.0', 'usage': {'input_tokens': 20}, 'answers': {'action': answer}}
        provider = GuestJevProvider('test-key', transport=transport)
        for resource in ('cpu', 'memory', 'network'):
            with self.subTest(resource=resource):
                context['resource'] = resource
                result = provider.predict(context)
                self.assertEqual(result['probabilities'], {'hold': .9, 'favor': 0., 'restore': .1})
        for changes in ({'choice': 'favor'}, {'probabilities': {'hold': .9}},
                        {'probabilities': {'hold': .9, 'restore': .05, 'favor': .05}},
                        {'probabilities': {'hold': .9, 'restore': .9}},
                        {'probabilities': {'hold': float('nan'), 'restore': .1}},
                        {'confidence': 2}):
            with self.subTest(changes=changes):
                original = answer.copy()
                answer.update(changes)
                with self.assertRaises(ProviderError):
                    provider.predict(context)
                answer.clear()
                answer.update(original)

    def test_unavailable_model_action_still_falls_back_once_during_cooldown(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        class Helper:
            ready = True
            key = None
            def submit(self, key, context): self.key = key
            def poll(self):
                return {'request_id': self.key, 'proposal': {'action': 'favor',
                        'probabilities': {'hold': 0., 'favor': 1., 'restore': 0.},
                        'confidence': 1., 'model': 'jev-1.13.0', 'inference_ms': 100}} if self.key else None
            def close(self): self.ready = False
        now, calls, events = [10.], [], []
        source = snapshot()
        source['cpu_control'].update(ready=False, cooldown_seconds=25)
        with tempfile.TemporaryDirectory() as folder:
            policy = GuestPolicy(Path(folder), clock=lambda: now[0], helper=Helper())
            policy.mode = 'shadow'
            def apply(op, **fields):
                calls.append(fields['action'])
                return {'action': fields['action']}
            policy.tick(source, apply, lambda **event: events.append(event))
            now[0] += .1
            policy.tick(source, apply, lambda **event: events.append(event))
            self.assertEqual(calls, ['hold'])
            self.assertEqual(policy.fallbacks, 1)
            audit = next(e for e in events if e['kind'] == 'guest_model_audit')
            self.assertFalse(audit['valid'])
            self.assertEqual(audit['fallback_reason'], 'invalid_proposal')
            self.assertTrue(audit['kernel_guard_passed'])

    def test_public_context_is_anonymous_and_uses_guest_semantics(self):
        import json
        from adaptive_os.guest_linux.policy import public_context, rule_action
        context = public_context(snapshot())
        self.assertEqual(context['resource'], 'cpu')
        self.assertEqual(rule_action(context), 'favor')
        self.assertNotIn('private', json.dumps(context))
        self.assertIn('favor', context['available_actions'])
        bad = snapshot()
        bad['cpu_control']['ready'] = False
        self.assertNotIn('favor', public_context(bad)['available_actions'])

    def test_jev_deadline_falls_back_once_and_rejects_late_response(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        class Helper:
            ready = True
            busy = False
            def submit(self, key, payload): self.busy = True
            def poll(self): return None
            def cancel(self): self.ready = self.busy = False
            def close(self): self.cancel()
        now, actions, events = [10.], [], []
        with tempfile.TemporaryDirectory() as folder:
            policy = GuestPolicy(Path(folder), clock=lambda: now[0], helper=Helper())
            policy.mode = 'shadow'
            policy.tick(snapshot(), lambda op, **fields: actions.append(op), lambda **event: events.append(event))
            self.assertIsNotNone(policy.pending)
            policy.tick(snapshot(), lambda op, **fields: actions.append(op), lambda **event: events.append(event))
            self.assertEqual(actions, [])
            now[0] += 1.001
            policy.tick(snapshot(), lambda op, **fields: actions.append(op), lambda **event: events.append(event))
            self.assertEqual(actions, ['resource_decide'])
            self.assertIsNone(policy.pending)
            self.assertTrue(any(e.get('fallback_reason') == 'deadline' for e in events))

    def test_default_manual_and_unqualified_jev_never_call_resource_writer(self):
        from adaptive_os.guest_linux.policy import GuestPolicy
        with tempfile.TemporaryDirectory() as folder:
            policy = GuestPolicy(Path(folder))
            actions = []
            policy.tick(snapshot(), lambda *a, **k: actions.append(a), lambda **e: None)
            self.assertEqual(actions, [])
            with self.assertRaisesRegex(ValueError, 'readiness'):
                policy.set_mode('jev', snapshot())


class GuestReadinessTest(unittest.TestCase):
    def test_qualification_binds_installed_service_and_controller_dependencies(self):
        from adaptive_os.guest_linux.readiness import fingerprint
        from unittest.mock import patch
        source = snapshot()
        before = fingerprint(source, 'cpu')
        self.assertNotEqual(before, fingerprint({**source, 'service_fingerprint': 'b' * 64}, 'cpu'))
        original = Path.read_bytes
        for name in ('desktop.py', 'protocol.py', 'service.py', 'decision_controller.py'):
            def changed(path):
                return original(path) + b'\n# changed\n' if path.name == name else original(path)
            with self.subTest(name=name), patch.object(Path, 'read_bytes', changed):
                self.assertNotEqual(before, fingerprint(source, 'cpu'))

    def test_runner_dispatches_exactly_108_paced_requests_and_keeps_audits(self):
        from adaptive_os.guest_linux.readiness import GuestReadiness
        class Helper:
            ready = True
            metadata = {}
            pending = None
            def poll(self):
                key, self.pending = self.pending, None
                return {'request_id': key, 'proposal': {'action': 'hold', 'probabilities': {'hold': 1, 'favor': 0, 'restore': 0},
                        'confidence': 1, 'model': 'jev-1.13.0', 'inference_ms': 100}} if key else None
            def submit(self, key, context):
                if self.pending: raise AssertionError('Two outstanding requests')
                self.pending = key
            def close(self): self.ready = False
        now = [10.]
        run = GuestReadiness(Helper(), snapshot(), 'network', clock=lambda: now[0])
        for index in range(108):
            now[0] = 10 + index * 5
            run.poll()
            self.assertIsNotNone(run.pending)
            now[0] += .1
            run.poll()
        self.assertTrue(run.done)
        self.assertTrue(run.report['passed'])
        self.assertEqual(len(run.report['rows']), 108)

    def test_108_context_vm_binding_pacing_audits_and_shadow_are_mandatory(self):
        from adaptive_os.guest_linux.readiness import corpus, fingerprint, validate_report
        source = snapshot()
        contexts = corpus('cpu')
        self.assertEqual(len(contexts), 108)
        proposal = {'action': 'hold', 'probabilities': {'hold': 1, 'favor': 0, 'restore': 0},
                    'confidence': 1, 'model': 'jev-1.13.0', 'inference_ms': 100}
        report = {'kind': 'guest_readiness', 'backend': 'guest-linux', 'fingerprint': fingerprint(source, 'cpu'),
                  'resource': 'cpu', 'completed_at_ns': 1000, 'rows': [
                      {'index': i, 'context': context, 'proposal': proposal, 'latency_ms': 100,
                       'dispatch_seconds': i * 5, 'audited': True, 'unauthorized_actions': 0,
                       'request_id': str(i), 'response_request_id': str(i), 'valid': True}
                      for i, context in enumerate(contexts)]}
        self.assertTrue(validate_report(report, source, 'cpu', now_ns=1001))
        for failure in ({'passed': False}, {'error': 'restoration failed'}):
            self.assertFalse(validate_report({**report, **failure}, source, 'cpu', now_ns=1001))
        for field, value in [('kind', 'readiness'), ('fingerprint', 'docker-fingerprint'), ('rows', report['rows'][:107])]:
            self.assertFalse(validate_report({**report, field: value}, source, 'cpu', now_ns=1001))
        for field, value in [('dispatch_seconds', 0), ('audited', False), ('unauthorized_actions', 1), ('response_request_id', 'stale')]:
            bad = copy.deepcopy(report)
            bad['rows'][40][field] = value
            self.assertFalse(validate_report(bad, source, 'cpu', now_ns=1001))
        bad = copy.deepcopy(report)
        bad['rows'][40]['dispatch_seconds'] = float('nan')
        self.assertFalse(validate_report(bad, source, 'cpu', now_ns=1001))
        bad = copy.deepcopy(report)
        for row in bad['rows'][:6]: row.update(valid=False, latency_ms=1001)
        self.assertFalse(validate_report(bad, source, 'cpu', now_ns=1001))
        self.assertFalse(validate_report(report, source, 'memory', now_ns=1001))


if __name__ == '__main__':
    unittest.main()
