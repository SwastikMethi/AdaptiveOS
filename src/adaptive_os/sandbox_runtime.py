"""Desktop-only Docker lifecycle. Never invokes legacy benchmark/probe containers."""

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

from .docker_runtime import DockerError, DockerRuntime, validate_container_id
from .sandbox.protocol import VERSION, encode, identifier, validate_command

LABELS = {'project': 'adaptive-os-desktop', 'architecture': 'single-sandbox-v2'}
CONTEXT_FILES = ('Dockerfile', '.dockerignore', '__init__.py', 'protocol.py', 'agent.py', 'presets.py', 'resources.py', 'network.py')


def docker_executable():
    candidates = [shutil.which('docker'), '/usr/local/bin/docker', '/opt/homebrew/bin/docker',
                  '/Applications/Docker.app/Contents/Resources/bin/docker']
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise DockerError('Docker CLI not found. Install and open Docker Desktop, then retry.')


def build_context():
    # The compiled app carries these sources as data, outside its Python modules.
    bundled = Path(sys.executable).parent.parent / 'Resources' / 'sandbox'
    path = bundled if (bundled / 'Dockerfile').is_file() else Path(__file__).parent / 'sandbox'
    if not all((path / name).is_file() for name in CONTEXT_FILES):
        raise DockerError('Bundled sandbox sources are missing. Rebuild or reinstall the desktop app.')
    return path


class SandboxRuntime(DockerRuntime):
    def __init__(self, executor=subprocess.run, docker=None):
        self.docker = docker or docker_executable()
        def execute(command, **kwargs):
            return executor([self.docker, *command[1:]], **kwargs)
        super().__init__(executor=execute)
        self.commands = deque(maxlen=100)
        self.messages = queue.Queue(maxsize=4096)
        self.stream_error = None
        self.process = None
        self.session_id = None
        self.container_id = None
        self.image_id = None
        self.ready = None
        self.readers = []
        self.diagnostics = deque(maxlen=30)
        self.cancel = threading.Event()

    def readiness(self):
        # Match Docker's precedence: DOCKER_CONTEXT overrides DOCKER_HOST.
        if os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'):
            endpoint = os.environ['DOCKER_HOST']
        else:
            context = os.environ.get('DOCKER_CONTEXT') or self._run(['context', 'show'])
            endpoint = self._run(['context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}'])
        if not endpoint.startswith('unix://'):
            raise DockerError('Only a local Docker Unix socket is supported. Select Docker Desktop locally.')
        version = json.loads(self._run(['version', '--format', '{{json .Server}}']))
        if version.get('Os') != 'linux':
            raise DockerError('Docker must be running Linux containers.')
        return {'docker_endpoint': endpoint, 'docker_version': version}

    def image_tag(self):
        context = build_context()
        digest = hashlib.sha256()
        for name in CONTEXT_FILES:
            digest.update(name.encode())
            digest.update((context / name).read_bytes())
        return 'adaptive-os-sandbox:' + digest.hexdigest()[:20]

    def prepare(self, progress=lambda text: None):
        self.readiness()
        tag = self.image_tag()
        try:
            self.image_id = self._run(['image', 'inspect', '--format', '{{.Id}}', tag])
            return self.image_id
        except DockerError:
            pass
        process = subprocess.Popen([self.docker, 'build', '--progress', 'plain', '-t', tag, str(build_context())],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue(maxsize=256)
        def read_build():
            for line in process.stdout:
                try:
                    lines.put_nowait(line.rstrip()[:2000])
                except queue.Full:
                    pass  # Build logs only; telemetry is never dropped silently.
        reader = threading.Thread(target=read_build, daemon=True)
        reader.start()
        deadline = time.monotonic() + 600
        try:
            while process.poll() is None or not lines.empty():
                if self.cancel.is_set() or time.monotonic() > deadline:
                    raise DockerError('Image preparation cancelled or timed out. Retry when Docker is ready.')
                try:
                    progress(lines.get(timeout=.1))
                except queue.Empty:
                    pass
            if process.returncode:
                raise DockerError('Sandbox image build failed. See the build log; check Docker and internet access.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            reader.join(timeout=2)
            process.stdout.close()
        self.image_id = self._run(['image', 'inspect', '--format', '{{.Id}}', tag])
        return self.image_id

    @staticmethod
    def volume_name(session_id):
        return 'adaptive-os-' + identifier(session_id)

    @staticmethod
    def labels(session_id):
        return {**LABELS, 'session_id': identifier(session_id)}

    def create_args(self, session_id, image, priority):
        args = ['create', '-i', '--init', '--network', 'none', '--read-only', '--user', '65534:65534',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '1g',
                '--memory-swap', '1g', '--pids-limit', '64', '--log-driver', 'local',
                '--log-opt', 'max-size=10m', '--log-opt', 'max-file=2',
                '--mount', f'type=volume,source={self.volume_name(session_id)},target=/scratch']
        for key, value in self.labels(session_id).items():
            args += ['--label', f'{key}={value}']
        if priority:
            args += ['--ulimit', 'nice=20:20']
        return [*args, image]

    def inspect_container(self, container_id, session_id):
        validate_container_id(container_id)
        data = json.loads(self._run(['inspect', container_id]))[0]
        labels = data['Config'].get('Labels') or {}
        if data['Id'] != container_id or any(labels.get(k) != v for k, v in self.labels(session_id).items()):
            raise DockerError('Refusing cleanup/control: container ownership labels do not match')
        return data

    def _read_output(self, stream, diagnostic=False):
        last_sequence = -1
        last_mono = 0
        try:
            while line := stream.readline(262145):
                if len(line) > 262144 or not line.endswith('\n'):
                    raise ValueError('Sandbox protocol line exceeded its bound or was truncated')
                if diagnostic:
                    self.diagnostics.append(line.strip()[:2000])
                    continue
                message = json.loads(line)
                if (not isinstance(message, dict) or type(message.get('v')) is not int or message['v'] != VERSION
                        or type(message.get('sequence')) is not int or message['sequence'] != last_sequence + 1
                        or type(message.get('mono_ns')) is not int or message['mono_ns'] < last_mono
                        or message.get('kind') not in ('ready', 'ack', 'sample', 'raw', 'event', 'exit', 'closed')):
                    raise ValueError('Invalid or out-of-order sandbox message')
                last_sequence, last_mono = message['sequence'], message['mono_ns']
                self.messages.put_nowait((message, time.monotonic_ns()))
        except (ValueError, OSError, queue.Full) as exc:
            self.stream_error = f'Sandbox telemetry interrupted: {exc}'

    def start(self, session_id):
        identifier(session_id)
        if self.process and self.process.poll() is None:
            raise DockerError('This session already has an active sandbox')
        environment = self.readiness()
        if not self.image_id:
            raise DockerError('Prepare the sandbox image first')
        self.session_id = session_id
        for priority in (True, False):
            volume = self.volume_name(session_id)
            args = ['volume', 'create']
            for key, value in self.labels(session_id).items():
                args += ['--label', f'{key}={value}']
            self._run([*args, volume])
            # A same-name preexisting volume is not ours merely because create returned it.
            self.inspect_volume(volume, session_id)
            try:
                self.container_id = self._run(self.create_args(session_id, self.image_id, priority), timeout=30)
                self.inspect_container(self.container_id, session_id)
                self.messages = queue.Queue(maxsize=4096)
                self.stream_error = None
                self.diagnostics.clear()
                self.process = subprocess.Popen([self.docker, 'start', '-ai', self.container_id],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                self.readers = [threading.Thread(target=self._read_output, args=(stream, diagnostic), daemon=True)
                                for stream, diagnostic in ((self.process.stdout, False), (self.process.stderr, True))]
                for reader in self.readers:
                    reader.start()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not self.cancel.is_set():
                    item = self.poll_message(.1)
                    if item:
                        message, receipt_ns = item
                        if message['kind'] != 'ready':
                            raise DockerError('Sandbox did not send its ready handshake')
                        self.ready = {**message, 'environment': environment, 'image_id': self.image_id,
                                      'container_id': self.container_id, 'volume': volume,
                                      'receipt_ns': receipt_ns}
                        return self.process
                    if self.process.poll() is not None:
                        raise DockerError('Sandbox startup failed: ' + '\n'.join(self.diagnostics))
                raise DockerError('Sandbox startup timed out or was cancelled')
            except (DockerError, OSError, ValueError) as exc:
                self.cleanup_session(session_id)
                self.close_transport()
                # Only unsupported ulimit startup gets a monitoring-only retry.
                if not priority or 'rlimit' not in str(exc).lower() and 'ulimit' not in str(exc).lower():
                    raise
        raise DockerError('Sandbox could not start')

    def send(self, command):
        command = validate_command(command)
        if not self.process or self.process.poll() is not None:
            raise DockerError('Sandbox is not connected')
        try:
            self.process.stdin.write(encode(command))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise DockerError('Sandbox connection was lost') from exc

    def command(self, op, **fields):
        command = {'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': op, **fields}
        self.send(command)
        return command

    def poll_message(self, timeout=0):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            if self.stream_error:
                raise DockerError(self.stream_error)
            return None

    def inspect_volume(self, name, session_id):
        if name != self.volume_name(session_id):
            raise DockerError('Refusing an unexpected volume name')
        data = json.loads(self._run(['volume', 'inspect', name]))[0]
        labels = data.get('Labels') or {}
        if data['Name'] != name or any(labels.get(k) != v for k, v in self.labels(session_id).items()):
            raise DockerError('Refusing cleanup: volume ownership labels do not match')
        return data

    def leftovers(self):
        filters = [part for key, value in LABELS.items() for part in ('--filter', f'label={key}={value}')]
        sessions = {}
        for kind, args in (('container', ['ps', '-aq', '--no-trunc']), ('volume', ['volume', 'ls', '-q'])):
            for target in self._run([*args, *filters]).splitlines():
                data = json.loads(self._run(['inspect', target] if kind == 'container' else ['volume', 'inspect', target]))[0]
                labels = (data.get('Config', {}).get('Labels') if kind == 'container' else data.get('Labels')) or {}
                if any(labels.get(k) != v for k, v in LABELS.items()):
                    continue
                session_id = identifier(labels.get('session_id'))
                item = sessions.setdefault(session_id, {'session_id': session_id, 'containers': [], 'volumes': []})
                item['containers' if kind == 'container' else 'volumes'].append(target)
        return list(sessions.values())

    def cleanup_session(self, session_id):
        filters = [part for key, value in self.labels(session_id).items()
                   for part in ('--filter', f'label={key}={value}')]
        errors = []
        for container_id in self._run(['ps', '-aq', '--no-trunc', *filters]).splitlines():
            try:
                self.inspect_container(container_id, session_id)
                self._run(['rm', '-f', container_id])
            except (DockerError, ValueError, KeyError) as exc:
                errors.append(str(exc))
        for name in self._run(['volume', 'ls', '-q', *filters]).splitlines():
            try:
                self.inspect_volume(name, session_id)
                self._run(['volume', 'rm', name])
            except (DockerError, ValueError, KeyError) as exc:
                errors.append(str(exc))
        if errors:
            raise DockerError('; '.join(errors))

    def close_transport(self):
        process = self.process
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for reader in self.readers:
                reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.process = None
