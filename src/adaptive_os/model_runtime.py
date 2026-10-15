"""Bounded private-pipe transport and immutable checkpoint handling."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

MAX_PACKET = 32768
LAYA_REVISION = '7b928d828b7b0e022f929d9bd2e44165aa270148'


def verify_checkpoint(directory):
    directory = Path(directory)
    try:
        manifest = json.loads((directory / 'manifest.json').read_text())
        if not re.fullmatch('[0-9a-f]{40}', manifest['revision']) or not manifest['files']:
            raise ValueError('invalid checkpoint manifest')
        for name, digest in manifest['files'].items():
            relative = Path(name)
            file = directory / relative
            if (relative.is_absolute() or '..' in relative.parts or file.is_symlink() or
                    not file.resolve().is_relative_to(directory.resolve()) or
                    not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('invalid checkpoint path')
            with file.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest:
                raise ValueError('checkpoint integrity check failed')
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('checkpoint missing or invalid') from exc


def prepare_checkpoint(bundled, support):
    """Always verify the signed source; loader mutations occur only in a copy."""
    manifest = verify_checkpoint(bundled)
    support = Path(support)
    support.mkdir(parents=True, exist_ok=True)
    destination = support / ('laya-' + manifest['revision'])
    try:
        if verify_checkpoint(destination) == manifest:
            return destination
    except ValueError:
        pass
    with tempfile.TemporaryDirectory(prefix='laya-copy-', dir=support) as temporary:
        copied = Path(temporary) / 'checkpoint'
        shutil.copytree(bundled, copied)
        verify_checkpoint(copied)
        if destination.is_symlink():
            raise ValueError('checkpoint working directory is a symlink')
        if destination.exists():
            shutil.rmtree(destination)
        copied.rename(destination)
    return destination


def runtime_paths():
    for executable in (Path(sys.executable), Path(sys.argv[0])):
        contents = executable.resolve().parent.parent
        helper = contents / 'Helpers' / 'model_helper' / 'model_helper'
        if helper.is_file():
            return [str(helper)], contents / 'Resources' / 'laya'
    root = Path(__file__).resolve().parents[2]
    python = root / 'build' / 'laya-venv' / 'bin' / 'python'
    return [str(python if python.exists() else sys.executable), str(root / 'model_helper.py')], root / 'build' / 'laya-checkpoint'


class HelperProcess:
    """One request, bounded pipes, nonblocking poll; termination never joins inference."""
    def __init__(self, command, config, *, diagnostic_stream=None):
        self.command = list(command)
        self._config = dict(config)
        self.process = None
        self.ready = False
        self.busy = False
        self.metadata = {}
        self._stderr = diagnostic_stream if config.get('provider') == 'laya' and diagnostic_stream is not None else subprocess.DEVNULL
        self._out = b''
        self._in = b''
        self.start()

    def start(self):
        self.cancel()
        self.started = time.monotonic()
        environment = os.environ.copy()
        # Neither credentials nor an inherited MPS fallback may reach a child via env.
        for name in ('TYPESAFE_API_KEY', 'JEV_API_KEY', 'PYTORCH_ENABLE_MPS_FALLBACK'):
            environment.pop(name, None)
        environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                           TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2',
                           TORCHDYNAMO_DISABLE='1')
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self._stderr, env=environment, close_fds=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        self._queue({'kind': 'initialize', **self._config})
        self._flush()

    def _queue(self, packet):
        value = json.dumps(packet, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if self._out or len(value) > MAX_PACKET:
            raise ValueError('helper input exceeds bound')
        self._out = value

    def _flush(self):
        if self._out and self.process:
            try:
                count = os.write(self.process.stdin.fileno(), self._out)
                self._out = self._out[count:]
            except BlockingIOError:
                pass

    def submit(self, request_id, payload):
        if not self.ready or self.busy or not self.process:
            raise RuntimeError('helper unavailable or already occupied')
        self._queue({'kind': 'predict', 'request_id': request_id, 'context': payload})
        self.busy = True
        self._flush()

    def poll(self):
        if not self.process:
            return None
        self._flush()
        try:
            chunk = os.read(self.process.stdout.fileno(), MAX_PACKET + 1)
        except BlockingIOError:
            chunk = None
        if chunk:
            self._in += chunk
        if len(self._in) > MAX_PACKET:
            self.cancel()
            raise ValueError('helper output exceeds bound')
        if b'\n' in self._in:
            line, self._in = self._in.split(b'\n', 1)
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('invalid helper packet')
            if value.get('kind') == 'ready':
                self.metadata = {**value, 'host_cold_start_ms': (time.monotonic() - self.started) * 1000}
                self.ready = True
                return None
            if value.get('kind') == 'startup_error':
                self.ready = False
                self.metadata = value
                return None
            self.busy = False
            return value
        if chunk == b'' and self.process.poll() is not None:
            self.ready = False
            if not self.metadata:
                self.metadata = {'kind': 'startup_error', 'error': 'unavailable'}
            if self.busy:
                raise RuntimeError('helper exited')
        return None

    def cancel(self):
        process, self.process = self.process, None
        self.ready = self.busy = False
        self._out = self._in = b''
        if process:
            if process.poll() is None:
                process.kill()
            for stream in (process.stdin, process.stdout):
                stream.close()
            try:
                process.wait(timeout=.05)
            except subprocess.TimeoutExpired:
                # Popen's reaper handles the killed child; never wait for inference.
                pass

    def close(self):
        self.cancel()
        self._config.clear()
