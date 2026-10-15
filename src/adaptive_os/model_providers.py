"""Provider adapters loaded exclusively in the inference helper process."""
import json
import math
import ssl
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import urllib.error
import urllib.request

from .decision_controller import ModelProposal, MAX_INPUT_BYTES, actions_for

JEV_MODEL = 'jev-1.13.0'
QUESTIONS = {'action': {'type': 'choice',
    'instructions': 'Choose the next CPU priority action from synthetic workload measurements. '
                    'Protect foreground deadlines while preserving background progress and importance. '
                    'Use the trend and available actions. Missing measurements are unknown. '
                    'Code selects the task and enforces safety. Decide timing from the evidence.',
    'criteria': {'hold': 'Keep current priorities; insufficient evidence or no useful change.',
                 'lower': 'Reduce one eligible background priority to help foreground deadlines.',
                 'restore': 'Restore one changed background when its cost is no longer justified.'}}}


def questions_for(resource='cpu'):
    if resource == 'cpu':
        return QUESTIONS
    if resource not in ('memory', 'network'):
        raise ValueError('Unknown resource')
    metric = 'cache misses and request latency' if resource == 'memory' else 'deadline misses and completion latency'
    return {'action': {'type': 'choice',
        'instructions': f'Choose the next {resource} allocation action from synthetic workload measurements. '
            f'Protect foreground {metric} while preserving background progress and importance. '
            'Use trends and available actions. Missing measurements are unknown. '
            'Code selects the donor and enforces bounded transfers. CPU priorities stay fixed.',
        'criteria': {'hold': 'Keep current allocations; insufficient evidence or no useful change.',
                     'favor': 'Transfer one bounded step from an eligible background to the foreground.',
                     'restore': 'Reverse one previous transfer when its cost is no longer justified.'}}}


class ProviderError(Exception):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _retry_after(header):
    try:
        seconds = float(header)
        return max(1, seconds) if math.isfinite(seconds) else 60
    except (ValueError, TypeError):
        try:
            return max(1, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60


def jev_http(payload, key):
    import certifi
    request = urllib.request.Request('https://api.typesafe.ai/v1/systemone',
        data=json.dumps(payload, allow_nan=False).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        # No SDK retries or redirects. Host deadline also covers DNS/TLS/read/parse.
        tls = urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
        with urllib.request.build_opener(_NoRedirect(), tls).open(request, timeout=.9) as response:
            content = response.read(32769)
            if len(content) > 32768:
                raise ProviderError('unavailable')
            return json.loads(content)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderError('authentication') from None
        if exc.code in (429, 529):
            raise ProviderError('rate_limit', _retry_after(exc.headers.get('Retry-After'))) from None
        raise ProviderError('unavailable') from None
    except (OSError, ValueError):
        raise ProviderError('unavailable') from None


def _choice(result, model, elapsed, resource='cpu', criteria=None):
    try:
        answer = result['answers']['action']
        if answer['type'] != 'choice':
            raise ValueError('wrong answer type')
        probabilities = answer['probabilities']
        if criteria is not None:
            if (not isinstance(probabilities, dict) or set(probabilities) != set(criteria)
                    or answer['choice'] not in criteria or not set(criteria).issubset(actions_for(resource))):
                raise ValueError('answer does not match offered actions')
            # Keep the recording vocabulary complete without accepting unoffered model choices.
            probabilities = {action: probabilities.get(action, 0.) for action in actions_for(resource)}
        proposal = {'action': answer['choice'], 'probabilities': probabilities,
                    'confidence': answer['confidence'], 'model': model,
                    'inference_ms': elapsed, 'input_tokens': result['usage']['input_tokens']}
        ModelProposal.parse(proposal, resource=resource)
        return proposal
    except (KeyError, TypeError, ValueError):
        raise ProviderError('invalid_proposal') from None


class JevProvider:
    def __init__(self, key, *, transport=jev_http, clock=time.monotonic):
        if not key or not isinstance(key, str) or len(key) > 4096:
            raise ProviderError('authentication')
        self._key, self.transport, self.clock = key, transport, clock
        self.backoff_until = 0
        self.disabled = False

    def questions(self, context):
        return questions_for(context.get('resource', 'cpu'))

    def proposal_resource(self, context):
        return context.get('resource', 'cpu')

    def predict(self, context):
        if self.disabled:
            raise ProviderError('authentication')
        if self.clock() < self.backoff_until:
            raise ProviderError('rate_limit', self.backoff_until - self.clock())
        if len(json.dumps(context, allow_nan=False).encode()) > MAX_INPUT_BYTES:
            raise ProviderError('input_too_large')
        started = self.clock()
        questions = self.questions(context)
        try:
            result = self.transport({'model': JEV_MODEL, 'state': context,
                                     'questions': questions}, self._key)
        except ProviderError as exc:
            if exc.code == 'authentication':
                self.disabled, self._key = True, ''
            if exc.code == 'rate_limit':
                self.backoff_until = self.clock() + exc.retry_after
            raise
        if result.get('model') != JEV_MODEL:
            raise ProviderError('model_version')
        return _choice(result, result['model'], (self.clock() - started) * 1000,
                       self.proposal_resource(context), questions['action']['criteria'])


class LayaProvider:
    def __init__(self, checkpoint, support, device='mps'):
        from .model_runtime import prepare_checkpoint, verify_checkpoint, LAYA_REVISION
        if device not in ('mps', 'cpu'):
            raise ProviderError('unavailable')
        manifest = verify_checkpoint(checkpoint)
        if manifest['revision'] != LAYA_REVISION:
            raise ProviderError('model_version')
        working = prepare_checkpoint(checkpoint, support)
        import torch
        from laya import Agent
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        if device == 'mps' and not torch.backends.mps.is_available():
            raise ProviderError('unavailable')
        self.agent = Agent(str(working), device=device, compile=False, fast=False,
                           expected_sha256=manifest['files'])
        if self.agent.device.type != device:
            raise ProviderError('unavailable')
        # Laya 0.3.27 retries GPU memory errors on CPU in Agent._infer. Trials
        # require one fixed runtime, so use its eager forward without retries.
        # Pin full precision too; the one-question MPS path already uses FP32.
        self.agent.model.float()
        self.agent.amp_enabled = False
        self.agent.dtype = torch.float32

        def infer_fixed(batch):
            if self.agent.device.type != device:
                raise ProviderError('unavailable')
            with torch.inference_mode():
                return self.agent.model(*(batch[name].to(self.agent.device) for name in
                    ('input_ids', 'attention_mask', 'marker_pos', 'marker_mask', 'qtype')))

        self.agent._infer = infer_fixed
        self.device = device
        self.model = 'laya-0.3.27@' + manifest['revision']
        # Readiness is for the same explicit token budget and device as trials.
        self.max_len, self.head_max_len = 2048, 512

    def predict(self, context):
        if len(json.dumps(context, allow_nan=False).encode()) > MAX_INPUT_BYTES:
            raise ProviderError('input_too_large')
        started = time.monotonic()
        # Check the exact release's tokenizer output before doing inference.
        questions = questions_for(context.get('resource', 'cpu'))
        internal = {key: self.agent._to_internal(value) for key, value in questions.items()}
        encoded = self.agent._encode_state(context, list(questions), internal,
                                           self.max_len, self.head_max_len)
        if any(item['state_stats']['state_tokens_dropped'] for item in encoded):
            raise ProviderError('input_too_large')
        result = self.agent.system_one(context, questions, max_len=self.max_len, head_max_len=self.head_max_len)
        if result.get('usage', {}).get('truncated'):
            raise ProviderError('input_too_large')
        return _choice(result, self.model, (time.monotonic() - started) * 1000, context.get('resource', 'cpu'))
