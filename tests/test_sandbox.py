"""Protocol boundaries, process identity, and scoped Docker ownership."""

import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from adaptive_os.sandbox.protocol import VERSION, validate_command, validate_config
from adaptive_os.sandbox.agent import read_process, cpu_percent, latency_window
from adaptive_os.sandbox import presets
from adaptive_os.sandbox import agent
from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.sandbox_runtime import SandboxRuntime, LABELS
from adaptive_os.docker_runtime import DockerError


class SandboxTest(unittest.TestCase):
    def stream_supervisor(self):
        supervisor = agent.Supervisor.__new__(agent.Supervisor)
        supervisor.selector = selectors.DefaultSelector()
        self.addCleanup(supervisor.selector.close)
        supervisor.buffers, supervisor.children = {}, {}
        supervisor.stopping = False
        supervisor.last_heartbeat = 0
        return supervisor

    def registered_pipe(self, supervisor, workload_id, diagnostic=False):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'rb')
        writer = os.fdopen(write_fd, 'wb', buffering=0)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        supervisor.register(reader, workload_id, diagnostic)
        return reader, writer

    def test_stopped_stream_events_are_ignored_after_workload_restart(self):
        supervisor = self.stream_supervisor()
        stdout, stdout_writer = self.registered_pipe(supervisor, 'work')
        stderr, stderr_writer = self.registered_pipe(supervisor, 'work', True)
        process = SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0, poll=lambda: 0)
        supervisor.children['work'] = agent.Child(process, 'cpu', {}, 1, 0)
        stdout_writer.write(b'{}\n')
        stderr_writer.write(b'diagnostic\n')
        ready = supervisor.selector.select(0)
        self.assertEqual({key.fileobj for key, _ in ready}, {stdout, stderr})
        supervisor.stop_child('work')
        self.assertTrue(stdout.closed and stderr.closed)

        replacement, replacement_writer = self.registered_pipe(supervisor, 'work')
        supervisor.children['work'] = agent.Child(
            SimpleNamespace(stdout=replacement, stderr=None, returncode=0, poll=lambda: 0),
            'cpu', {}, 2, 0)
        for key, _ in ready:
            supervisor.read_stream(key)
        progress = {'sequence': 0, 'units': 10, 'mono_ns': 20, 'start_ns': 0, 'ticks': []}
        replacement_writer.write((json.dumps(progress) + '\n').encode())
        with patch.object(supervisor, 'emit'):
            supervisor.read_stream(supervisor.selector.get_key(replacement))
        self.assertEqual(supervisor.children['work'].progress, progress)

        control, control_writer = self.registered_pipe(supervisor, None)
        control_writer.write((json.dumps({'v': VERSION, 'request_id': str(uuid.uuid4()),
                                         'op': 'heartbeat'}) + '\n').encode())
        with patch.object(agent.time, 'monotonic', return_value=123):
            supervisor.read_stream(supervisor.selector.get_key(control))
        self.assertEqual(supervisor.last_heartbeat, 123)
        self.assertFalse(supervisor.stopping)

    def test_repeated_eof_event_is_ignored_without_changing_shutdown_semantics(self):
        for workload_id in (None, 'work'):
            with self.subTest(workload_id=workload_id):
                supervisor = self.stream_supervisor()
                reader, writer = self.registered_pipe(supervisor, workload_id)
                key = supervisor.selector.get_key(reader)
                writer.close()
                supervisor.read_stream(key)
                supervisor.read_stream(key)
                self.assertNotIn(reader, supervisor.buffers)
                self.assertEqual(supervisor.stopping, workload_id is None)

    def test_registered_stream_read_errors_are_not_suppressed(self):
        supervisor = self.stream_supervisor()
        reader, _ = self.registered_pipe(supervisor, 'work')
        key = supervisor.selector.get_key(reader)
        with patch.object(agent.os, 'read', side_effect=OSError('read failed')):
            with self.assertRaisesRegex(OSError, 'read failed'):
                supervisor.read_stream(key)

    def test_priority_preflight_exercises_both_reversible_sequences(self):
        class Store:
            records = []
            def record(self, *args):
                self.records.append(args[1])
            def flush(self):
                pass
        class Runtime:
            ready = {'image_id': 'sha256:test', 'resource_limits': {'nice': [20, 20]}}
            sent = []
            def send(self, command):
                self.sent.append(command)
        worker = SessionWorker('/unused')
        worker.store, worker.runtime, worker.session_id = Store(), Runtime(), str(uuid.uuid4())
        worker.begin_preflight()
        identities = iter((1000, 1001))
        while worker.preflight:
            command = worker.preflight['expected']
            message = {'request_id': command['request_id'], 'op': command['op'], 'ok': True}
            if command['op'] == 'probe_start':
                message.update(workload_id=command['workload_id'], identity=next(identities), nice=command['nice'])
            elif command['op'] == 'priority':
                message.update(workload_id=command['workload_id'], identity=command['identity'], nice=command['nice'])
            elif command['op'] == 'probe_stop':
                message.update(workload_id=command['workload_id'], identity=command['identity'], status='stopped')
            worker.pending.pop(command['request_id'])
            worker.advance_preflight(message)
        result = list(worker.updates.queue)[-1]['preflight']
        self.assertTrue(result['passed'])
        self.assertEqual([(step['from'], step['to']) for step in result['transitions']],
                         [(0, 10), (10, 0), (10, 19), (19, 10)])
        self.assertEqual([sequence['original_nice'] for sequence in result['sequences']], [0, 10])

    def test_priority_change_rejects_stale_process_identity(self):
        class Process:
            pid = 100
            def poll(self):
                return None
        supervisor = agent.Supervisor.__new__(agent.Supervisor)
        supervisor.children = {'work': agent.Child(Process(), 'cpu', {}, 111, 0)}
        with patch.object(agent, 'read_process', return_value={'identity': 111, 'nice': 0}), \
                patch.object(agent.os, 'setpriority') as change:
            with self.assertRaisesRegex(ValueError, 'identity'):
                supervisor.set_priority('work', 222, 10)
        change.assert_not_called()

    def test_failed_preflight_aborts_probes_and_keeps_monitoring_available(self):
        class Store:
            def record(self, *args):
                pass
            def flush(self):
                pass
        class Runtime:
            ready = {'image_id': 'sha256:test', 'resource_limits': {}}
            def send(self, command):
                pass
        worker = SessionWorker('/unused')
        worker.store, worker.runtime, worker.session_id = Store(), Runtime(), str(uuid.uuid4())
        worker.begin_preflight()
        failed = worker.preflight['expected']
        worker.pending.pop(failed['request_id'])
        worker.advance_preflight({'request_id': failed['request_id'], 'op': failed['op'],
                                  'ok': False, 'error': 'permission denied'})
        abort = worker.preflight['expected']
        self.assertEqual(abort['op'], 'preflight_abort')
        worker.pending.pop(abort['request_id'])
        worker.advance_preflight({'request_id': abort['request_id'], 'op': 'preflight_abort', 'ok': True})
        result = list(worker.updates.queue)[-1]
        self.assertFalse(result['priority_supported'])
        self.assertIn('permission denied', result['priority_reason'])

    def test_terminal_stream_error_follows_already_queued_evidence(self):
        runtime = SandboxRuntime(docker='docker')
        envelope = ({'kind': 'closed', 'sequence': 3, 'mono_ns': 100}, 200)
        runtime.messages.put(envelope)
        runtime.stream_error = 'invalid trailing output'
        self.assertEqual(runtime.poll_message(), envelope)
        with self.assertRaisesRegex(DockerError, 'invalid trailing output'):
            runtime.poll_message()

    def test_full_output_pipe_does_not_block_the_heartbeat_loop(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(writer, False)
            while True:
                try:
                    os.write(writer, bytes(4096))
                except BlockingIOError:
                    break
            self.assertTrue(hasattr(agent, 'flush_output'), 'Supervisor output must be nonblocking')
            buffer = bytearray(b'telemetry\n')
            agent.flush_output(buffer, writer)
            self.assertEqual(buffer, bytearray(b'telemetry\n'))
            os.read(reader, 4096)
            agent.flush_output(buffer, writer)
            self.assertEqual(buffer, bytearray())
        finally:
            os.close(reader)
            os.close(writer)

    def test_incomplete_latency_window_stays_unknown(self):
        tick = {'scheduled_ns': 5_000_000_000, 'wakeup_ns': 5_000_000_000,
                'completion_ns': 5_001_000_000, 'deadline_ns': 5_020_000_000, 'skipped': False}
        value = latency_window([tick], 0, 10_000_000_000)
        self.assertIsNone(value['miss_rate'])
        self.assertEqual((value['window_start_ns'], value['window_end_ns']),
                         (5_000_000_000, 10_000_000_000))
        missing = latency_window([], 0, 10_000_000_000)
        self.assertIsNone(missing['miss_rate'])
        self.assertEqual(missing['window_end_ns'], 10_000_000_000)

    def test_file_io_step_reports_progress_before_full_scratch_pass(self):
        with tempfile.TemporaryFile() as scratch:
            self.assertTrue(hasattr(presets, 'io_step'), 'I/O must yield telemetry between bounded chunks')
            state = (True, 0)
            units = 0
            for _ in range(128):
                amount, state = presets.io_step(scratch, bytes(1024 * 1024), state)
                self.assertEqual(amount, 1024 * 1024)
                units += amount
            self.assertEqual(units, 128 * 1024 * 1024)
            self.assertEqual(scratch.seek(0, 2), 64 * 1024 * 1024)
            self.assertEqual(state, (True, 0))

    def test_protocol_rejects_untrusted_commands_and_unbounded_resources(self):
        for preset, config in [('shell', {}), ('memory', {'mib': 129}),
                               ('memory', {'mib': True}), ('periodic', {'budget_ms': float('nan')}),
                               ('periodic', {'budget_ms': 20}), ('cpu', {'command': 'sh'})]:
            with self.subTest(preset=preset, config=config), self.assertRaises(ValueError):
                validate_config(preset, config)
        self.assertEqual(validate_config('memory', {}), {'mib': 64})
        self.assertEqual(validate_config('periodic', {}), {'budget_ms': 4.0})
        base = {'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': 'priority',
                'workload_id': str(uuid.uuid4()), 'identity': 123, 'nice': 10}
        self.assertEqual(validate_command(base)['nice'], 10)
        for change in ({'nice': -1}, {'nice': True}, {'identity': True}, {'identity': 0},
                       {'pid': 1}, {'v': True},
                       {'workload_id': '../../other'}, {'op': 'exec'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_command({**base, **change})
        probe = {'v': VERSION, 'request_id': str(uuid.uuid4()), 'op': 'probe_start',
                 'workload_id': str(uuid.uuid4()), 'nice': 0}
        self.assertEqual(validate_command(probe), probe)

    def test_proc_fields_and_pid_reuse_do_not_fabricate_cpu(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root) / '123'
            proc.mkdir()
            fields = ['S'] + ['0'] * 49
            fields[11], fields[12], fields[16], fields[19] = '100', '50', '10', '987'
            (proc / 'stat').write_text('123 (name with ) spaces) ' + ' '.join(fields))
            (proc / 'status').write_text('State:\tS (sleeping)\nVmRSS:\t2048 kB\n')
            (proc / 'io').write_text('read_bytes: 4096\nwrite_bytes: 8192\n')
            current = read_process(123, Path(root), ticks_per_second=100)
            self.assertEqual(current['identity'], 987)
            self.assertEqual(current['cpu_ns'], 1_500_000_000)
            self.assertEqual(current['rss_mib'], 2)
            self.assertEqual(current['read_bytes'], 4096)
            self.assertEqual(current['nice'], 10)
            previous = {**current, 'mono_ns': 1_000_000_000, 'cpu_ns': 0}
            current['mono_ns'] = 2_500_000_000
            self.assertEqual(cpu_percent(previous, current), 100)
            self.assertIsNone(cpu_percent(previous, {**current, 'identity': 988}))
            (proc / 'io').unlink()
            self.assertIsNone(read_process(123, Path(root))['read_bytes'])
            (proc / 'stat').unlink()
            self.assertIsNone(read_process(123, Path(root)))

    def test_latency_uses_five_second_window_after_warmup(self):
        ticks = [{'scheduled_ns': n * 20_000_000, 'deadline_ns': n * 20_000_000 + 20_000_000,
                  'wakeup_ns': n * 20_000_000 + 2_000_000,
                  'completion_ns': n * 20_000_000 + 21_000_000, 'skipped': False} for n in range(250, 500)]
        self.assertIsNone(latency_window(ticks, 0, 4_000_000_000)['miss_rate'])
        value = latency_window(ticks, 0, 10_000_000_000)
        self.assertEqual(value['miss_rate'], 1)
        self.assertEqual(value['p95_lateness_ms'], 1)
        self.assertEqual(value['p95_wakeup_ms'], 2)
        self.assertEqual((value['window_start_ns'], value['window_end_ns']),
                         (5_000_000_000, 10_000_000_000))

    def test_cleanup_refuses_foreign_labels(self):
        run_id = str(uuid.uuid4())
        container_id = 'a' * 64
        removed = []
        def executor(command, **kwargs):
            args = command[1:]
            if args[0] == 'ps':
                output = container_id
            elif args[0] == 'inspect':
                output = json.dumps([{'Id': container_id, 'Config': {'Labels': {**LABELS, 'session_id': str(uuid.uuid4())}}}])
            elif args[:2] == ['volume', 'ls']:
                output = ''
            else:
                removed.append(args)
                output = ''
            return subprocess.CompletedProcess(command, 0, output, '')
        runtime = SandboxRuntime(executor=executor, docker='docker')
        with self.assertRaises(DockerError):
            runtime.cleanup_session(run_id)
        self.assertEqual(removed, [])

    def test_create_contains_no_host_mount_or_privilege_and_bounded_limits(self):
        run_id = str(uuid.uuid4())
        runtime = SandboxRuntime(docker='docker')
        command = runtime.create_args(run_id, 'sha256:' + 'a' * 64, priority=True)
        for flag, value in [('--network', 'none'), ('--memory', '1g'), ('--memory-swap', '1g'),
                            ('--pids-limit', '64'), ('--user', '65534:65534'), ('--cap-drop', 'ALL'),
                            ('--ulimit', 'nice=20:20')]:
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn('--read-only', command)
        self.assertNotIn('--privileged', command)
        self.assertNotIn('-t', command)
        mount = command[command.index('--mount') + 1]
        self.assertTrue(mount.startswith('type=volume,source=adaptive-os-'))
        self.assertIn('target=/scratch', mount)


if __name__ == '__main__':
    unittest.main()
