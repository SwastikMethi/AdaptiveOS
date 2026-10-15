"""Single background owner for Docker transport and desktop SQLite recordings."""

from pathlib import Path
import queue
import threading
import time
import uuid

from .macos_activity import SessionActivity
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
        self.pending_timeouts = {}
        self.quitting = False
        self.error = None
        self.last_heartbeat = self.last_flush = 0
        self.last_pump = self.last_health = 0
        self.flush_duration_ms = 0
        self.clock_anchor = None
        self.closed = False
        self.preflight = None
        self.workload_presets = {}
        self.activity = SessionActivity()

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

    def send_control(self, command, workload=None):
        command = validate_command(command)
        if workload:
            self.store.configure(self.session_id, workload)
        self.store.record(self.session_id, {'kind': 'control', 'command': command}, time.monotonic_ns())
        self.store.flush()
        self.runtime.send(command)
        self.pending[command['request_id']] = time.monotonic()
        self.pending_timeouts[command['request_id']] = 30 if command['op'] == 'start' and command.get('preset') == 'cache' else 5

    def preflight_send(self, stage, op, **fields):
        command = {'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': op, **fields}
        self.preflight.update(stage=stage, expected=command)
        self.send_control(command)

    def begin_preflight(self):
        self.preflight = {'sequences': [], 'transitions': []}
        self.preflight_send('start0', 'probe_start', workload_id=str(uuid.uuid4()), nice=0)

    def finish_preflight(self, passed, error=None):
        evidence = {**self.preflight, 'passed': passed,
                    'session_id': self.session_id, 'image_id': self.runtime.ready.get('image_id'),
                    'resource_limits': self.runtime.ready.get('resource_limits')}
        evidence.pop('expected', None)
        if error:
            evidence['error'] = error
        message = {'kind': 'priority_preflight', 'passed': passed, 'evidence': evidence}
        self.store.record(self.session_id, message, time.monotonic_ns())
        self.store.flush()
        self.emit('ack', op='priority_preflight', ok=passed, priority_supported=passed,
                  priority_reason=('Reversible priority preflight passed through acknowledged control path'
                                   if passed else f'Reversible priority preflight failed: {error}'),
                  preflight=evidence, session_id=self.session_id)
        self.preflight = None

    def abort_preflight(self, error):
        self.preflight['error'] = error
        self.preflight_send('abort', 'preflight_abort')

    def advance_preflight(self, message):
        if not self.preflight:
            return
        expected, stage = self.preflight['expected'], self.preflight['stage']
        if message.get('request_id') != expected['request_id']:
            return
        if not message.get('ok') or message.get('op') != expected['op']:
            error = message.get('error', 'preflight acknowledgement did not match request')
            if stage == 'abort':
                self.finish_preflight(False, error)
                raise RuntimeError('Preflight probe cleanup failed: ' + error)
            self.abort_preflight(error)
            return
        if 'workload_id' in expected and message.get('workload_id') != expected['workload_id']:
            error = 'preflight workload acknowledgement did not match'
            self.abort_preflight(error)
            return
        if stage in ('start0', 'start10'):
            if message.get('nice') != expected['nice'] or type(message.get('identity')) is not int:
                error = 'preflight snapshot readback did not match'
                self.abort_preflight(error)
                return
            self.preflight['sequences'].append({'workload_id': expected['workload_id'],
                'identity': message['identity'], 'original_nice': message['nice']})
            target = 10 if stage == 'start0' else 19
            self.preflight_send('p0_10' if stage == 'start0' else 'p10_19', 'priority',
                workload_id=expected['workload_id'], identity=message['identity'], nice=target)
        elif stage in ('p0_10', 'p0_0', 'p10_19', 'p10_10'):
            if message.get('nice') != expected['nice'] or message.get('identity') != expected['identity']:
                error = 'preflight transition readback did not match'
                self.abort_preflight(error)
                return
            sequence = self.preflight['sequences'][-1]
            previous = sequence['original_nice'] if not sequence.get('steps') else sequence['steps'][-1]['to']
            step = {'from': previous, 'to': message['nice'], 'actual': message['nice'],
                    'identity': message['identity'], 'request_id': expected['request_id']}
            sequence.setdefault('steps', []).append(step)
            self.preflight['transitions'].append(step)
            if stage == 'p0_10':
                self.preflight_send('p0_0', 'priority', workload_id=expected['workload_id'],
                                    identity=expected['identity'], nice=0)
            elif stage == 'p10_19':
                self.preflight_send('p10_10', 'priority', workload_id=expected['workload_id'],
                                    identity=expected['identity'], nice=10)
            else:
                self.preflight_send('stop0' if stage == 'p0_0' else 'stop10', 'probe_stop',
                                    workload_id=expected['workload_id'], identity=expected['identity'])
        elif stage == 'stop0':
            self.preflight_send('start10', 'probe_start', workload_id=str(uuid.uuid4()), nice=10)
        elif stage == 'stop10':
            self.preflight_send('complete', 'preflight_complete')
        elif stage == 'complete':
            self.finish_preflight(True)
        elif stage == 'abort':
            self.finish_preflight(False, self.preflight.get('error', 'preflight failed'))

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
            finally:
                self.activity.end()
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
            selected_resource = request.get('resource', 'cpu')
            self.store.begin(self.session_id, {'workloads': request['workloads'], 'resource': selected_resource,
                                               'link_mbps': request.get('link_mbps', 20)})
            self.workload_presets = {row['id']: row['preset'] for row in request['workloads']}
            try:
                self.activity.begin()
                self.store.record(self.session_id, {'kind': 'host_activity', 'state': 'started',
                    'app_nap_prevented': self.activity.active, 'idle_system_sleep_allowed': True}, time.monotonic_ns())
                runtime.start(self.session_id)
                self.active = True
                self.closed = False
                self.pending.clear()
                ready = runtime.ready
                self.clock_anchor = (ready['mono_ns'], ready['receipt_ns'])
                self.store.record(self.session_id, ready, ready['receipt_ns'])
                for workload in request['workloads']:
                    self.store.configure(self.session_id, workload)
                self.store.flush()
                self.emit('ready', **{k: v for k, v in ready.items() if k != 'kind'}, session_id=self.session_id)
                if selected_resource == 'cpu':
                    self.begin_preflight()
                else:
                    self.send_control({'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': 'configure',
                                       'resource': selected_resource, 'link_mbps': request.get('link_mbps', 20)})
                self.last_heartbeat = 0
            except Exception:
                try:
                    self.store.finish(self.session_id, 'failed')
                finally:
                    if not self.active:
                        self.activity.end()
                raise
        elif op == 'control':
            if not self.active:
                raise ValueError('Start a sandbox first')
            self.send_control(request['command'], request.get('workload'))
        elif op == 'audit':
            message = request.get('message')
            if (not isinstance(message, dict) or message.get('kind') not in
                    {'cpu_policy', 'cpu_decision', 'cpu_restoration', 'trial_boundary',
                     'policy_started', 'provider_configuration', 'rule_reference', 'decision_context', 'model_proposal',
                     'model_rejection', 'policy_fallback', 'decision_command', 'decision_acknowledgement',
                     'policy_stopped', 'model_dispatched', 'decision_result',
                     'priority_requested', 'priority_dispatched', 'priority_outcome', 'resource_effect'}):
                raise ValueError('Unsupported audit event')
            self.store.record(self.session_id, message, time.monotonic_ns())
        elif op == 'stop_session':
            self.stop_session('completed')
        elif op == 'history':
            self.refresh_history()
        elif op == 'load':
            self.emit('loaded', recording=self.store.load(request['session_id'], request.get('workload_offset', 0)))
        elif op == 'decision_history':
            try:
                page = self.store.decision_page(request['session_id'], before=request.get('before'),
                    after=request.get('after'), before_cycle=request.get('before_cycle'),
                    after_cycle=request.get('after_cycle'))
            except ValueError:
                self.emit('decision_history', session_id=request['session_id'], token=request['token'],
                          error='History position unavailable. Use Jump to latest and try again.')
            else:
                self.emit('decision_history', session_id=request['session_id'], token=request['token'], page=page)
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
        if message['kind'] == 'closed':
            self.closed = True
        if message['kind'] == 'ack':
            self.pending.pop(message['request_id'], None)
            self.pending_timeouts.pop(message['request_id'], None)
            self.advance_preflight(message)
        if message['kind'] != 'raw':
            anchor_mono, anchor_receipt = self.clock_anchor
            # Compare elapsed durations, never absolute host and VM clocks.
            lag = (receipt_ns - anchor_receipt) - (message['mono_ns'] - anchor_mono)
            stale = lag > 3_000_000_000 or time.monotonic_ns() - receipt_ns > 3_000_000_000
            self.emit(message['kind'], **{k: v for k, v in message.items() if k != 'kind'},
                      host_receipt_ns=receipt_ns, stale=stale, session_id=self.session_id)
        else:
            self.emit('raw', **{k: v for k, v in message.items() if k != 'kind'},
                      host_receipt_ns=receipt_ns, session_id=self.session_id)
        return message['kind']

    def pump(self):
        now = time.monotonic()
        loop_delay_ms = max(0, (now - self.last_pump) * 1000) if self.last_pump else 0
        self.last_pump = now
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
        expired = [request_id for request_id, started in self.pending.items()
                   if now - started > self.pending_timeouts.get(request_id, 5)]
        if expired:
            if self.preflight and self.preflight['stage'] != 'abort' \
                    and self.preflight['expected']['request_id'] in expired:
                self.pending.pop(self.preflight['expected']['request_id'], None)
                self.abort_preflight('preflight acknowledgement timed out')
            else:
                raise RuntimeError('Sandbox did not acknowledge a control within 5 seconds')
        if now - self.last_flush >= 1:
            started = time.monotonic()
            self.store.flush()
            self.flush_duration_ms = (time.monotonic() - started) * 1000
            self.last_flush = now
        if now - self.last_health >= 5:
            self.store.record(self.session_id, {'kind': 'host_health', 'heartbeat_age_ms':
                max(0, (now - self.last_heartbeat) * 1000), 'worker_loop_delay_ms': loop_delay_ms,
                'flush_duration_ms': self.flush_duration_ms, 'pending_controls': len(self.pending),
                'request_queue': self.requests.qsize(), 'update_queue': self.updates.qsize()},
                time.monotonic_ns())
            self.last_health = now

    def stop_session(self, status):
        if not self.active:
            return
        errors = []
        try:
            try:
                self.runtime.command('shutdown')
            except Exception as exc:
                errors.append(str(exc))
            # An exited attach process may still have valid evidence queued by its
            # reader. A failed send must not skip that evidence (especially closed).
            deadline = time.monotonic() + 4
            while not self.closed and time.monotonic() < deadline:
                item = self.runtime.poll_message(.1)
                if item:
                    self.receive(item)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if not self.closed:
                errors.append('Sandbox closed acknowledgement was not received')
            try:
                self.runtime.cleanup_session(self.session_id)
            except Exception as exc:
                errors.append(str(exc))
            transport_exit = self.runtime.process.poll() if getattr(self.runtime, 'process', None) else None
            try:
                self.runtime.close_transport()
            except Exception as exc:
                errors.append(str(exc))
            try:
                self.activity.end()
            except Exception as exc:
                errors.append(f'App Nap activity release failed: {exc}')
            self.active = False
            self.pending.clear()
            self.pending_timeouts.clear()
            try:
                self.store.record(self.session_id, {'kind': 'host_activity', 'state': 'ended',
                    'app_nap_prevented': self.activity.active, 'idle_system_sleep_allowed': True}, time.monotonic_ns())
                if errors:
                    self.store.record(self.session_id, {'kind': 'failure', 'message': '; '.join(errors),
                        'diagnostics': list(getattr(self.runtime, 'diagnostics', [])),
                        'transport_exit': transport_exit}, time.monotonic_ns())
                self.store.finish(self.session_id, status if not errors else 'failed')
            except Exception as exc:
                errors.append(f'Recording write failed: {exc}')
            self.emit('stopped', session_id=self.session_id, clean=not errors)
            self.refresh_history()
        if errors:
            raise RuntimeError('; '.join(errors))
