import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import desktop_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]
STUB = '''
import argparse, json, signal, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', type=Path)
parser.add_argument('--allow-unqualified-jev-demo', action='store_true')
args = parser.parse_args()
args.data_dir.mkdir(parents=True, exist_ok=True)
(args.data_dir / 'started.json').write_text(json.dumps({'experimental': args.allow_unqualified_jev_demo}))
def stop(*_):
    (args.data_dir / 'stopped').write_text('clean')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    time.sleep(.02)
'''


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='controller launcher with spaces ')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        for name in ('start.sh', 'stop.sh', 'scripts/desktop_launcher.py'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        (self.root / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        package = self.root / 'src' / 'adaptive_os'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'desktop.py').write_text(STUB)
        self.directory = self.root / 'output' / 'desktop'
        self.state = self.directory / 'launcher.json'
        self.addCleanup(self.stop_fixture)

    def call(self, script, *args):
        return subprocess.run([str(self.root / script), *args], cwd='/tmp', capture_output=True,
                              text=True, timeout=40)

    def stop_fixture(self):
        if self.state.exists():
            result = self.call('stop.sh')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_start_duplicate_stop_and_explicit_options_from_another_directory(self):
        result = self.call('start.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.state.read_text())
        self.assertFalse(json.loads((self.directory / 'started.json').read_text())['experimental'])
        duplicate = self.call('start.sh')
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn('Already running', duplicate.stdout)
        self.assertEqual(json.loads(self.state.read_text()), record)
        self.assertEqual(self.call('stop.sh').returncode, 0)
        self.assertEqual((self.directory / 'stopped').read_text(), 'clean')
        self.assertFalse(self.state.exists())
        self.assertIn('Already stopped', self.call('stop.sh').stdout)
        custom = self.root / 'custom recordings'
        result = self.call('start.sh', '--data-dir', str(custom), '--allow-unqualified-jev-demo')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads((custom / 'started.json').read_text())['experimental'])
        self.assertEqual(self.call('stop.sh').returncode, 0)
        self.assertTrue((custom / 'stopped').exists())
        self.assertGreaterEqual((self.directory / 'app.log').read_text().count('--- Launch'), 2)

    def test_stale_pid_cannot_stop_an_unrelated_process(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(other.wait)
        self.addCleanup(other.terminate)
        self.directory.mkdir(parents=True)
        self.state.write_text(json.dumps({'pid': other.pid, 'identity': 'previous process'}))
        result = self.call('stop.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(other.poll())
        self.assertFalse(self.state.exists())

    def test_real_desktop_loads_env_file_without_leaking_key_to_launcher_artifacts(self):
        package = self.root / 'src' / 'adaptive_os'
        shutil.rmtree(package)
        package.symlink_to(ROOT / 'src' / 'adaptive_os', target_is_directory=True)
        key = 'private-launcher-test-token'
        env_file = self.root / '.env'
        env_file.write_text(f'JEV_API_KEY={key}\n')
        with patch.dict(os.environ, {'QT_QPA_PLATFORM': 'offscreen'}):
            result = self.call('start.sh', '--jev-env-file', '.env')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state.read_text()
        self.assertIn('--jev-env-file .env', state)
        self.assertEqual(self.call('stop.sh').returncode, 0)
        for output in (result.stdout, result.stderr, state, (self.directory / 'app.log').read_text()):
            self.assertNotIn(key, output)
        self.assertNotIn(key.encode(), (self.directory / 'sessions-v2.sqlite3').read_bytes())
        self.assertEqual(env_file.read_text(), f'JEV_API_KEY={key}\n')

    def test_launch_failure_does_not_leave_live_state(self):
        result = self.call('start.sh', '--invalid-option')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('See', result.stderr)
        self.assertFalse(self.state.exists())
        self.assertIn('unrecognized arguments', (self.directory / 'app.log').read_text())

    def test_missing_environment_explains_setup(self):
        (self.root / '.venv').unlink()
        result = self.call('start.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pip install', result.stderr)
        self.assertFalse(self.state.exists())

    def test_concurrent_operation_is_rejected_without_launching(self):
        self.directory.mkdir(parents=True)
        with (self.directory / 'launcher.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.call('start.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('in progress', result.stderr)
        self.assertFalse(self.state.exists())

    def test_reused_pid_identity_and_invalid_pid_are_rejected(self):
        self.directory.mkdir(parents=True)
        with patch.object(launcher, 'STATE', self.state), \
                patch.object(launcher, 'process_identity', return_value='new birth time'):
            self.state.write_text(json.dumps({'pid': 12345, 'identity': 'old birth time'}))
            self.assertIsNone(launcher.owned_pid())
            self.assertFalse(self.state.exists())
        for pid in (-1, 0, 1, '123', True):
            self.assertIsNone(launcher.process_identity(pid))

    def test_mac_framework_reexec_preserves_process_identity(self):
        from types import SimpleNamespace
        suffix = f' -m adaptive_os.desktop --data-dir {launcher.DIRECTORY}'
        results = [SimpleNamespace(returncode=0, stderr='',
                   stdout=f'S Fri Oct 9 11:30:00 2026 {os.getuid()} {program}{suffix}')
                   for program in (str(launcher.PYTHON), '/Library/Frameworks/Python.app/Contents/MacOS/Python')]
        with patch.object(launcher.subprocess, 'run', side_effect=results):
            self.assertEqual(launcher.process_identity(12345), launcher.process_identity(12345))

    def test_timeout_keeps_state_and_never_force_kills(self):
        self.directory.mkdir(parents=True)
        self.state.write_text('retained')
        with patch.object(launcher, 'STATE', self.state), \
                patch.object(launcher, 'owned_pid', return_value=12345), \
                patch.object(launcher.time, 'monotonic', side_effect=[0, 61]), \
                patch.object(launcher.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'not force-killed'):
                launcher.stop()
        kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertEqual(self.state.read_text(), 'retained')
        self.state.unlink()


class DesktopSignalTest(unittest.TestCase):
    def test_real_qt_entrypoint_handles_sigterm_and_sigint(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with (directory / 'app.log').open('w+') as log:
                    process = subprocess.Popen([sys.executable, '-m', 'adaptive_os.desktop', '--data-dir', folder],
                        cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'QT_QPA_PLATFORM': 'offscreen'},
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 15
                        while not (directory / 'sessions-v2.sqlite3').exists() and process.poll() is None:
                            if time.monotonic() >= deadline:
                                self.fail('Desktop did not initialize its recording worker')
                            time.sleep(.05)
                        self.assertIsNone(process.poll())
                        process.send_signal(sig)
                        code = process.wait(timeout=30)
                        log.seek(0)
                        self.assertEqual(code, 0, log.read())
                        self.assertFalse((directory / 'desktop.lock').exists())
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
