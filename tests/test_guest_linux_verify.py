from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GuestVerificationTest(unittest.TestCase):
    def test_comparison_requires_same_live_identities_and_restored_cpu(self):
        from adaptive_os.guest_linux.verify import recovery_checks
        task = {'id': 'a', 'identity': 'boot:invocation', 'cpu_weight': 100,
                'cpus_effective': '1-3', 'network': {'namespace': 'aos-task-a'}, 'profile': '/apps/a'}
        before = {'tasks': [task]}
        checks = recovery_checks(before, {'tasks': [{**task, 'cpu_weight': 90}]})
        self.assertFalse(checks['weights_restored'])
        self.assertTrue(checks['application_identities'])
        changed = {'tasks': [{**task, 'identity': 'replacement'}]}
        self.assertFalse(recovery_checks(before, changed)['application_identities'])
        self.assertTrue(all(recovery_checks(before, before).values()))
        self.assertFalse(recovery_checks(before, {'tasks': []})['application_identities'])

    @unittest.skipIf(sys.platform == 'linux', 'Host rejection check')
    def test_installer_rejects_host_without_writing_guest_paths(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/install_guest_linux.sh'
        result = subprocess.run(['bash', str(script), 'guest'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Ubuntu 24.04 ARM64 guest', result.stderr)
