import json
from pathlib import Path
import sys
import tempfile
import time
import unittest


class HelperProcessTest(unittest.TestCase):
    def helper(self, body):
        from adaptive_os.model_runtime import HelperProcess
        program = ('import sys,json,time\njson.loads(sys.stdin.readline())\n'
                   'print(json.dumps({"kind":"ready","cold_start_ms":3}),flush=True)\n' + body)
        return HelperProcess([sys.executable, '-u', '-c', program], {'provider': 'test'})

    def ready(self, helper):
        until = time.monotonic() + 3
        while not helper.ready and time.monotonic() < until:
            helper.poll()
            time.sleep(.005)
        self.assertTrue(helper.ready)

    def test_hung_helper_has_bounded_queue_and_cleanup(self):
        helper = self.helper('time.sleep(100)\n')
        try:
            self.ready(helper)
            helper.submit('a', {'metric': 1})
            with self.assertRaises(RuntimeError):
                helper.submit('b', {'metric': 2})
            start = time.monotonic()
            self.assertIsNone(helper.poll())
            helper.cancel()
            self.assertLess(time.monotonic() - start, .3)
        finally:
            helper.close()

    def test_controller_recovers_same_process_after_discarding_late_pipe_reply(self):
        from adaptive_os.cpu_rule import CpuWindow, ManagedTask
        from adaptive_os.decision_controller import PolicyController
        from tests.test_decision_controller import proposal
        body = ('for index,line in enumerate(sys.stdin):\n'
                ' r=json.loads(line)\n'
                ' if index == 0: time.sleep(.1)\n'
                f' print(json.dumps({{"request_id":r["request_id"],"proposal":{proposal()!r}}}),flush=True)\n')
        helper = self.helper(body)
        now = [0.0]
        tasks = [ManagedTask('b', 'Low', 50, 0, 0, 42)]
        controller = PolicyController(helper, clock=lambda: now[0])
        try:
            self.ready(helper)
            pid = helper.process.pid
            controller.activate('s', 't', ('f', 7), tasks, 'laya')
            controller.observe(CpuWindow(0, 5, .01, 50, True), tasks, ('f', 7))
            now[0] = 1
            self.assertEqual(controller.poll(tasks, ('f', 7)).action, 'hold')
            self.assertEqual(helper.process.pid, pid)
            until = time.monotonic() + 2
            while controller.expired and time.monotonic() < until:
                self.assertIsNone(controller.poll(tasks, ('f', 7)))
                time.sleep(.005)
            self.assertIsNone(controller.expired)
            self.assertFalse(helper.busy)
            now[0] = 5
            controller.observe(CpuWindow(5, 10, .01, 50, True), tasks, ('f', 7))
            decision = None
            until = time.monotonic() + 2
            while decision is None and time.monotonic() < until:
                decision = controller.poll(tasks, ('f', 7))
                time.sleep(.005)
            self.assertEqual(decision.action, 'lower')
            self.assertEqual(helper.process.pid, pid)
            self.assertEqual(controller.failures, 0)
        finally:
            helper.close()

    def test_helper_disables_transitive_torch_compiler_decorators(self):
        import os
        from unittest.mock import patch
        from adaptive_os.model_runtime import HelperProcess
        program = ('import os,sys,json\njson.loads(sys.stdin.readline())\n'
                   'print(json.dumps({"kind":"ready","dynamo":os.environ.get("TORCHDYNAMO_DISABLE")}),flush=True)\n'
                   'sys.stdin.readline()\n')
        with patch.dict(os.environ, {'TORCHDYNAMO_DISABLE': '0'}):
            helper = HelperProcess([sys.executable, '-u', '-c', program], {'provider': 'test'})
        try:
            self.ready(helper)
            self.assertEqual(helper.metadata['dynamo'], '1')
        finally:
            helper.close()

    def test_startup_exit_fails_without_waiting_for_preload_timeout(self):
        from adaptive_os.model_runtime import HelperProcess
        helper = HelperProcess([sys.executable, '-c', 'raise SystemExit(4)'], {'provider': 'test'})
        try:
            until = time.monotonic() + 2
            while helper.process.poll() is None and time.monotonic() < until:
                time.sleep(.005)
            helper.poll()
            self.assertEqual(helper.metadata.get('kind'), 'startup_error')
        finally:
            helper.close()

    def test_response_roundtrip_and_oversized_output_rejected(self):
        helper = self.helper('r=json.loads(sys.stdin.readline())\n'
                             'print(json.dumps({"request_id":r["request_id"],"proposal":{"action":"hold"}}),flush=True)\n')
        try:
            self.ready(helper)
            helper.submit('one', {'metric': 1})
            response = None
            until = time.monotonic() + 2
            while response is None and time.monotonic() < until:
                response = helper.poll()
                time.sleep(.005)
            self.assertEqual(response['request_id'], 'one')
        finally:
            helper.close()
        helper = self.helper('sys.stdin.readline()\nprint("x"*100000,flush=True)\n')
        try:
            self.ready(helper)
            helper.submit('one', {})
            until = time.monotonic() + 2
            with self.assertRaises(ValueError):
                while time.monotonic() < until:
                    helper.poll()
                    time.sleep(.005)
        finally:
            helper.close()

    def test_missing_checkpoint_disables_only_laya(self):
        from adaptive_os.model_runtime import verify_checkpoint
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                verify_checkpoint(Path(folder))

    def test_checkpoint_copy_preserves_bundle_and_rejects_corruption(self):
        from adaptive_os.model_runtime import prepare_checkpoint
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundled = root / 'bundled'
            bundled.mkdir()
            (bundled / 'weights').write_bytes(b'original')
            manifest = {'revision': 'a' * 40, 'files': {'weights': hashlib.sha256(b'original').hexdigest()}}
            (bundled / 'manifest.json').write_text(json.dumps(manifest))
            copied = prepare_checkpoint(bundled, root / 'support')
            (copied / 'weights').write_bytes(b'changed')
            self.assertEqual((bundled / 'weights').read_bytes(), b'original')
            refreshed = prepare_checkpoint(bundled, root / 'support')
            self.assertEqual((refreshed / 'weights').read_bytes(), b'original')
            (bundled / 'weights').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):
                prepare_checkpoint(bundled, root / 'support')

    def test_checkpoint_manifest_cannot_escape_or_follow_symlinks(self):
        from adaptive_os.model_runtime import verify_checkpoint
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'manifest.json').write_text(json.dumps({'revision': 'a' * 40, 'files': {'../outside': 'b'*64}}))
            with self.assertRaises(ValueError):
                verify_checkpoint(root)


class ProviderTest(unittest.TestCase):
    def laya_dependencies(self, forced_device=None):
        from contextlib import nullcontext
        from types import SimpleNamespace
        class Model:
            def float(self): return self
            def __call__(self, *args): raise RuntimeError('GPU out of memory')
        class Agent:
            def __init__(self, path, device, **kwargs):
                self.device = SimpleNamespace(type=forced_device or device)
                self.model, self.cpu_retries = Model(), 0
            def _infer(self, batch):
                try:
                    return self.model()
                except RuntimeError:
                    self.cpu_retries += 1
                    return 'cpu retry'
        torch = SimpleNamespace(set_num_threads=lambda n: None, set_num_interop_threads=lambda n: None,
            backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
            inference_mode=nullcontext, float32='float32')
        return {'torch': torch, 'laya': SimpleNamespace(Agent=Agent)}

    def test_laya_never_retries_gpu_errors_on_cpu(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from adaptive_os.model_providers import LayaProvider
        from adaptive_os.model_runtime import LAYA_REVISION
        with patch.dict(sys.modules, self.laya_dependencies()), \
                patch('adaptive_os.model_runtime.verify_checkpoint', return_value={'revision': LAYA_REVISION, 'files': {}}), \
                patch('adaptive_os.model_runtime.prepare_checkpoint', return_value='/test'):
            provider = LayaProvider('/test', '/working', 'mps')
            tensor = SimpleNamespace(to=lambda device: 'tensor')
            batch = {name: tensor for name in ('input_ids', 'attention_mask', 'marker_pos', 'marker_mask', 'qtype')}
            with self.assertRaises(RuntimeError):
                provider.agent._infer(batch)
            self.assertEqual(provider.agent.cpu_retries, 0)

    def test_laya_rejects_loader_device_fallback_before_readiness(self):
        from unittest.mock import patch
        from adaptive_os.model_providers import LayaProvider, ProviderError
        from adaptive_os.model_runtime import LAYA_REVISION
        with patch.dict(sys.modules, self.laya_dependencies('cpu')), \
                patch('adaptive_os.model_runtime.verify_checkpoint', return_value={'revision': LAYA_REVISION, 'files': {}}), \
                patch('adaptive_os.model_runtime.prepare_checkpoint', return_value='/test'):
            with self.assertRaises(ProviderError):
                LayaProvider('/test', '/working', 'mps')

    def test_jev_typed_request_and_response(self):
        from adaptive_os.model_providers import JevProvider
        captured = []
        def transport(payload, key):
            captured.append((payload, key))
            return {'model': 'jev-1.13.0', 'answers': {'action': {
                'type': 'choice', 'choice': 'hold', 'probabilities': {'hold': .8, 'lower': .1, 'restore': .1},
                'confidence': .7}}, 'usage': {'input_tokens': 250}}
        provider = JevProvider('memory-only-secret', transport=transport)
        result = provider.predict({'window': {'miss_rate': .01}})
        self.assertEqual(result['action'], 'hold')
        self.assertEqual(captured[0][0]['model'], 'jev-1.13.0')
        self.assertEqual(captured[0][0]['questions']['action']['type'], 'choice')
        self.assertNotIn('memory-only-secret', json.dumps(result))
        self.assertNotIn('memory-only-secret', json.dumps(captured[0][0]))

    def test_jev_has_no_retries_and_respects_backoff(self):
        from adaptive_os.model_providers import JevProvider, ProviderError
        attempts = []
        def transport(payload, key):
            attempts.append(1)
            raise ProviderError('rate_limit', retry_after=30)
        provider = JevProvider('secret', transport=transport, clock=lambda: 1)
        for _ in range(2):
            with self.assertRaises(ProviderError):
                provider.predict({})
        self.assertEqual(len(attempts), 1)

    def test_credentials_required_and_authentication_failure_disables(self):
        from adaptive_os.model_providers import JevProvider, ProviderError
        with self.assertRaises(ProviderError):
            JevProvider('')
        attempts = []
        def transport(payload, key):
            attempts.append(1)
            raise ProviderError('authentication')
        provider = JevProvider('secret', transport=transport)
        for _ in range(2):
            with self.assertRaises(ProviderError):
                provider.predict({})
        self.assertEqual(len(attempts), 1)
