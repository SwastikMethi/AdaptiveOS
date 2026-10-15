import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from types import SimpleNamespace

from adaptive_os.guest_linux.core import Journal


class GuestCpuVerificationTest(unittest.TestCase):
    def test_distribution_requires_proportional_change_restoration_and_saturation(self):
        from adaptive_os.guest_linux.cpu_verify import distribution_checks
        phases = [{'foreground_share': share, 'expected_share': expected, 'cpu_seconds': 88., 'wall_seconds': 30.}
                  for share, expected in ((.50, .50), (.66, 2/3), (.80, .80), (.51, .50))]
        self.assertTrue(all(distribution_checks(phases).values()))
        unchanged = [{**phase, 'foreground_share': .5} for phase in phases]
        self.assertFalse(all(distribution_checks(unchanged).values()))
        light = [{**phase, 'cpu_seconds': 40.} for phase in phases]
        self.assertFalse(distribution_checks(light)['saturated'])
        short = [{**phase, 'wall_seconds': 5.} for phase in phases]
        self.assertFalse(distribution_checks(short)['steady_windows'])
        drifted = [*phases[:3], {**phases[3], 'foreground_share': .7}]
        self.assertFalse(distribution_checks(drifted)['restored_distribution'])

    def test_load_target_must_match_registered_invocation_and_direct_task_cgroup(self):
        from adaptive_os.guest_linux.cpu_verify import load_target
        task_id = str(uuid.uuid4())
        identity = str(uuid.uuid4()) + ':invocation'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = Journal(root / 'state.sqlite3')
            journal.save({'run': None, 'tasks': [{'id': task_id, 'identity': identity, 'app': 'geany', 'slot': 1}]})
            journal.close()
            config = root / 'guest.json'
            config.write_text(json.dumps({'uid': 1000}))
            unit = 'adaptiveos-task-' + task_id + '.service'
            properties = {'ControlGroup': '/adaptiveos.slice/' + unit, 'InvocationID': 'invocation', 'MainPID': '123'}
            with patch('adaptive_os.guest_linux.cpu_verify.subprocess.check_output',
                       side_effect=lambda *a, **kw: '\n'.join(f'{k}={v}' for k, v in properties.items())), \
                    patch('adaptive_os.guest_linux.cpu_verify.boot_id', return_value=identity.split(':')[0]), \
                    patch('adaptive_os.guest_linux.cpu_verify.pwd.getpwuid', return_value=SimpleNamespace(pw_gid=1000)):
                result = load_target(task_id, identity, root / 'state.sqlite3', config)
                self.assertEqual(result['cgroup'], '/sys/fs/cgroup/adaptiveos.slice/' + unit)
                self.assertEqual(result['namespace'], '/proc/123/ns/net')
                with self.assertRaisesRegex(ValueError, 'identity'):
                    load_target(task_id, 'stale', root / 'state.sqlite3', config)
                with self.assertRaises(ValueError):
                    load_target('../../system.slice', identity, root / 'state.sqlite3', config)
                properties['ControlGroup'] = '/system.slice/other.service'
                with self.assertRaisesRegex(ValueError, 'cgroup'):
                    load_target(task_id, identity, root / 'state.sqlite3', config)

    def test_cpu_load_requires_root_and_never_runs_with_user_supplied_process_ids(self):
        from adaptive_os.guest_linux.cpu_verify import run_load
        with patch('os.geteuid', return_value=1000), patch('os.fork', create=True) as fork:
            with self.assertRaisesRegex(PermissionError, 'root'):
                run_load(str(uuid.uuid4()), 'identity')
            fork.assert_not_called()


if __name__ == '__main__':
    unittest.main()
