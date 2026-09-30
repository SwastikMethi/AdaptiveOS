"""Protocol boundaries, process identity, and scoped Docker ownership."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from adaptive_os.sandbox.protocol import validate_command, validate_config
from adaptive_os.sandbox.agent import read_process, cpu_percent, latency_window
from adaptive_os.sandbox import presets
from adaptive_os.sandbox_runtime import SandboxRuntime, LABELS
from adaptive_os.docker_runtime import DockerError


class SandboxTest(unittest.TestCase):
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
        base = {'v': 2, 'request_id': str(uuid.uuid4()), 'op': 'priority',
                'workload_id': str(uuid.uuid4()), 'nice': 10}
        self.assertEqual(validate_command(base)['nice'], 10)
        for change in ({'nice': -1}, {'nice': True}, {'pid': 1}, {'v': True},
                       {'workload_id': '../../other'}, {'op': 'exec'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_command({**base, **change})

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
        ticks = [{'scheduled_ns': n * 1_000_000_000, 'deadline_ns': n * 1_000_000_000 + 20_000_000,
                  'wakeup_ns': n * 1_000_000_000 + 2_000_000,
                  'completion_ns': n * 1_000_000_000 + 21_000_000, 'skipped': False} for n in range(5, 10)]
        self.assertIsNone(latency_window(ticks, 0, 4_000_000_000)['miss_rate'])
        value = latency_window(ticks, 0, 10_000_000_000)
        self.assertEqual(value['miss_rate'], 1)
        self.assertEqual(value['p95_lateness_ms'], 1)
        self.assertEqual(value['p95_wakeup_ms'], 2)

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
