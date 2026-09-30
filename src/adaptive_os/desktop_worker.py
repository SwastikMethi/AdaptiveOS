"""Single background owner for Docker transport and desktop SQLite recordings."""

from pathlib import Path
import queue
import threading
import time
import uuid

from .sandbox.protocol import VERSION, validate_command
from .sandbox_runtime import SandboxRuntime
from .session_store import SessionStore


class SessionWorker(threading.Thread):
    def __init__(self, database, runtime_factory=SandboxRuntime):
        super().__init__(name='sandbox-session', daemon=False)
        self.database = Path(database)
        self.runtime_factory = runtime_factory
        self.requests = queue.Queue(maxsize=128)
        self.updates = queue.Queue(maxsize=512)
        self.runtime = None
        self.store = None
        self.session_id = None
        self.active = False
        self.pending = {}
        self.quitting = False
        self.error = None
        self.last_heartbeat = self.last_flush = 0
        self.clock_anchor = None

    def submit(self, request):
        self.requests.put_nowait(request)

    def emit(self, kind, **fields):
        self.updates.put_nowait({'kind': kind, **fields})

    def get_runtime(self):
        if self.runtime is None:
            self.runtime = self.runtime_factory()
        return self.runtime

    def refresh_history(self):
        self.emit('history', sessions=self.store.sessions())

    def run(self):
        try:
            self.store = SessionStore(self.database)
            self.refresh_history()
            while not self.quitting:
                try:
                    request = self.requests.get(timeout=.03)
                except queue.Empty:
                    request = None
                try:
                    if request:
                        self.handle(request)
                    if self.active:
                        self.pump()
                except Exception as exc:
                    self.fail(exc)
        except Exception as exc:
            self.error = f'Recording worker failed: {exc}. Check the data folder permissions and available disk space.'
            try:
                self.emit('error', message=self.error)
            except queue.Full:
                pass
        finally:
            try:
                if self.active:
                    self.stop_session('interrupted')
            except Exception as exc:
                self.error = f'Shutdown incomplete: {exc}. Recover this exact session on the next launch.'
            if self.runtime:
                self.runtime.close_transport()
            if self.store:
                try:
                    self.store.close()
                except Exception as exc:
                    self.error = f'Recording flush failed: {exc}. Keep the database for recovery.'

    def fail(self, exc):
        text = str(exc)
        if self.active:
            try:
                self.store.record(self.session_id, {'kind': 'failure', 'message': text,
                    'diagnostics': list(getattr(self.runtime, 'diagnostics', [])),
                    'transport_exit': self.runtime.process.poll() if getattr(self.runtime, 'process', None) else None},
                    time.monotonic_ns())
                self.store.flush()
            except Exception:
                pass  # The original recording failure is retained in the visible error.
            try:
                self.stop_session('failed')
            except Exception as cleanup:
                text += f'; cleanup/recording: {cleanup}. Recover session {self.session_id} on next launch.'
        self.error = text
        try:
            self.emit('error', message=text)
        except queue.Full:
            self.quitting = True

    def handle(self, request):
        op = request['op']
        if op in ('check', 'prepare', 'history', 'load', 'export', 'recover') and self.active:
            raise ValueError('Stop the sandbox before history, exports, recovery, or environment checks')
        if op == 'check':
            runtime = self.get_runtime()
            environment = runtime.readiness()
            self.emit('docker', ready=True, environment=environment)
            self.emit('leftovers', sessions=runtime.leftovers())
        elif op == 'prepare':
            image = self.get_runtime().prepare(lambda line: self.emit('build', message=line))
            self.emit('prepared', image_id=image)
        elif op == 'start_session':
            if self.active:
                raise ValueError('A sandbox is already active')
            runtime = self.get_runtime()
            runtime.cancel.clear()
            runtime.prepare(lambda line: self.emit('build', message=line))
            self.session_id = str(uuid.uuid4())
            self.store.begin(self.session_id, {'workloads': request['workloads']})
            try:
                runtime.start(self.session_id)
                self.active = True
                self.pending.clear()
                ready = runtime.ready
                self.clock_anchor = (ready['mono_ns'], ready['receipt_ns'])
                self.store.record(self.session_id, ready, ready['receipt_ns'])
                for workload in request['workloads']:
                    self.store.configure(self.session_id, workload)
                self.store.flush()
                self.emit('ready', **{k: v for k, v in ready.items() if k != 'kind'}, session_id=self.session_id)
                self.last_heartbeat = 0
            except Exception:
                self.store.finish(self.session_id, 'failed')
                raise
        elif op == 'control':
            if not self.active:
                raise ValueError('Start a sandbox first')
            command = validate_command(request['command'])
            if request.get('workload'):
                self.store.configure(self.session_id, request['workload'])
            self.store.record(self.session_id, {'kind': 'control', 'command': command}, time.monotonic_ns())
            # Persist intent before its external side effect.
            self.store.flush()
            self.runtime.send(command)
            self.pending[command['request_id']] = time.monotonic()
        elif op == 'stop_session':
            self.stop_session('completed')
        elif op == 'history':
            self.refresh_history()
        elif op == 'load':
            self.emit('loaded', recording=self.store.load(request['session_id']))
        elif op == 'export':
            self.store.export(request['session_id'], request['path'], request['format'])
            self.emit('exported', path=str(request['path']))
        elif op == 'recover':
            self.get_runtime().cleanup_session(request['session_id'])
            self.emit('event', message=f'Removed owned resources for {request["session_id"]}')
            self.emit('leftovers', sessions=self.runtime.leftovers())
        elif op == 'quit':
            self.quitting = True
        else:
            raise ValueError('Unsupported desktop action')

    def receive(self, item):
        message, receipt_ns = item
        self.store.record(self.session_id, message, receipt_ns)
        if message['kind'] == 'ack':
            self.pending.pop(message['request_id'], None)
        if message['kind'] != 'raw':
            anchor_mono, anchor_receipt = self.clock_anchor
            # Compare elapsed durations, never absolute host and VM clocks.
            lag = (receipt_ns - anchor_receipt) - (message['mono_ns'] - anchor_mono)
            stale = lag > 3_000_000_000 or time.monotonic_ns() - receipt_ns > 3_000_000_000
            self.emit(message['kind'], **{k: v for k, v in message.items() if k != 'kind'},
                      host_receipt_ns=receipt_ns, stale=stale)
        return message['kind']

    def pump(self):
        now = time.monotonic()
        for _ in range(200):
            item = self.runtime.poll_message()
            if not item:
                break
            if self.receive(item) == 'closed':
                raise RuntimeError('Sandbox closed unexpectedly; the heartbeat lease or process failed')
        if now - self.last_heartbeat >= 1:
            self.runtime.command('heartbeat')
            self.last_heartbeat = now
        if self.runtime.process.poll() is not None:
            raise RuntimeError('Docker sandbox disconnected: ' + '\n'.join(self.runtime.diagnostics))
        if any(now - started > 5 for started in self.pending.values()):
            raise RuntimeError('Sandbox did not acknowledge a control within 5 seconds')
        if now - self.last_flush >= 1:
            self.store.flush()
            self.last_flush = now

    def stop_session(self, status):
        if not self.active:
            return
        errors = []
        closed = False
        try:
            self.runtime.command('shutdown')
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                item = self.runtime.poll_message(.1)
                if item and self.receive(item) == 'closed':
                    closed = True
                    break
        except Exception as exc:
            errors.append(str(exc))
        finally:
            try:
                self.runtime.cleanup_session(self.session_id)
            except Exception as exc:
                errors.append(str(exc))
            self.runtime.close_transport()
            self.active = False
            self.pending.clear()
            try:
                self.store.finish(self.session_id, status if closed and not errors else 'failed')
            except Exception as exc:
                errors.append(f'Recording write failed: {exc}')
            self.emit('stopped', session_id=self.session_id, clean=not errors)
            self.refresh_history()
        if errors:
            raise RuntimeError('; '.join(errors))
