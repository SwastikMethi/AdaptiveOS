import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class GuestHostTest(unittest.TestCase):
    def test_profiles_are_traversable_with_the_root_services_restrictive_umask(self):
        from adaptive_os.guest_linux.linux import create_profile
        import os
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            task_id = 'a5762b2b-2785-4d9b-9120-fd8ed38bf62f'
            old = os.umask(0o077)
            try:
                profile = create_profile(state, task_id, os.getuid(), os.getgid())
            finally:
                os.umask(old)
            self.assertEqual((state / 'apps').stat().st_mode & 0o777, 0o711)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)

    def test_task_unit_owns_descendants_and_does_not_depend_on_controller(self):
        from adaptive_os.guest_linux.linux import task_command
        task = {'id': 'a5762b2b-2785-4d9b-9120-fd8ed38bf62f', 'app': 'firefox', 'slot': 1}
        config = {'uid': 1000, 'gid': 1000, 'home': '/home/guest', 'display': ':0'}
        command = task_command(task, config, Path('/var/lib/adaptive-os'))
        properties = {arg.removeprefix('--property=') for arg in command if arg.startswith('--property=')}
        self.assertIn('User=1000', properties)
        self.assertIn('Slice=adaptiveos.slice', properties)
        self.assertIn('ExitType=cgroup', properties)
        self.assertIn('AllowedCPUs=1-3', properties)
        self.assertIn('CPUWeight=100', properties)
        self.assertIn('ProtectControlGroups=yes', properties)
        self.assertIn('NoNewPrivileges=yes', properties)
        self.assertIn('TemporaryFileSystem=/run/systemd/resolve', properties)
        self.assertFalse(any(p.startswith(('PartOf=', 'BindsTo=', 'RuntimeDirectory=')) for p in properties))
        self.assertIn('/usr/bin/dbus-run-session', command)
        self.assertIn('--no-remote', command)
        self.assertIn('--new-instance', command)
        self.assertNotIn('sudo', command)
        task['app'] = 'geany'
        geany = task_command(task, config, Path('/var/lib/adaptive-os'))
        self.assertIn('--new-instance', geany)
        self.assertIn('--config', geany)
        self.assertNotEqual(geany[-1], command[-1])

    def test_task_identity_cannot_escape_generated_paths(self):
        from adaptive_os.guest_linux.linux import task_command
        for identity in ('../../system.slice', '--help', 'a' * 32):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                task_command({'id': identity, 'slot': 1, 'app': 'firefox'},
                             {'uid': 1000, 'gid': 1000, 'home': '/home/guest', 'display': ':0'}, Path('/var/lib/adaptive-os'))

    def test_dns_rejects_stub_and_ipv6_bypass_and_requires_real_resolver(self):
        from adaptive_os.guest_linux.linux import dns_config
        source = 'nameserver 127.0.0.53\nnameserver ::1\nnameserver 192.168.64.1\nsearch local\n'
        self.assertEqual(dns_config(source), 'nameserver 192.168.64.1\n')
        with self.assertRaisesRegex(ValueError, 'DNS'):
            dns_config('nameserver 127.0.0.53\n')

    def test_task_dns_requires_private_directory_and_the_managed_resolver(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            expected = 'nameserver 192.168.64.1\n'
            (state / 'resolv.conf').write_text(expected)
            proc = state / 'proc/42'
            (proc / 'root/etc').mkdir(parents=True)
            resolved = proc / 'root/run/systemd/resolve'
            resolved.mkdir(parents=True)
            link = proc / 'root/etc/resolv.conf'
            link.symlink_to('../run/systemd/resolve/stub-resolv.conf')
            target = resolved / 'stub-resolv.conf'
            target.write_text(expected)
            mount = '1 0 0:1 / /run/systemd/resolve rw - tmpfs tmpfs rw\n'
            (proc / 'mountinfo').write_text(mount)
            host = LinuxHost(Mock(metadata=Mock(return_value=None)), {'uid': os.getuid()}, state=state)
            with patch('adaptive_os.guest_linux.linux.Path', return_value=state / 'proc'):
                self.assertEqual(host.task_dns(42), {'nameservers': ['192.168.64.1'], 'private_directory': True})
                for contents, mounts in ((expected, ''), ('nameserver 127.0.0.53\n', mount),
                                         ('nameserver 192.0.2.1\n', mount)):
                    target.write_text(contents)
                    (proc / 'mountinfo').write_text(mounts)
                    with self.subTest(contents=contents, mounts=mounts), self.assertRaisesRegex(RuntimeError, 'DNS'):
                        host.task_dns(42)
                target.write_text(expected)
                (proc / 'mountinfo').write_text(mount)
                link.unlink()
                link.symlink_to('/etc/another-resolver')
                with self.assertRaisesRegex(RuntimeError, 'DNS'):
                    host.task_dns(42)

    def test_mac_preflight_reports_real_storage_and_never_claims_vm_verified(self):
        from adaptive_os.guest_linux.preflight import host_report
        with patch('adaptive_os.guest_linux.preflight.shutil.disk_usage') as usage:
            usage.return_value.free = 6 * 1024**3
            report = host_report(Path('.'))
        self.assertFalse(report['passed'])
        self.assertFalse(report['vm_verified'])
        self.assertEqual(report['storage']['required_bytes'], 180 * 1024**3)
        self.assertEqual(report['storage']['free_bytes'], 6 * 1024**3)

    def test_utm_preflight_reserves_space_for_a_full_recovery_copy(self):
        from adaptive_os.guest_linux.preflight import host_report
        from types import SimpleNamespace
        with patch('adaptive_os.guest_linux.preflight.platform.machine', return_value='arm64'), \
                patch('adaptive_os.guest_linux.preflight.platform.system', return_value='Darwin'), \
                patch.object(Path, 'is_dir', side_effect=lambda: True), \
                patch('adaptive_os.guest_linux.preflight.shutil.disk_usage', return_value=SimpleNamespace(free=179 * 1024**3)):
            low = host_report(Path('.'))
            self.assertFalse(low['passed'])
            with patch('adaptive_os.guest_linux.preflight.shutil.disk_usage', return_value=SimpleNamespace(free=227 * 1024**3)):
                enough = host_report(Path('.'))
            self.assertTrue(enough['passed'])
            self.assertIn('UTM.app', enough['utm'])
            self.assertEqual(enough['vm']['desktop'], 'KDE Plasma/X11')
            self.assertFalse(enough['vm_verified'])

    def test_task_launch_uses_the_configured_sddm_authority(self):
        from adaptive_os.guest_linux.linux import task_command
        config = {'uid': 1000, 'gid': 1000, 'home': '/home/guest', 'display': ':1',
                  'xauthority': '/tmp/xauth_ABC123'}
        command = task_command({'id': 'a5762b2b-2785-4d9b-9120-fd8ed38bf62f', 'app': 'firefox', 'slot': 1},
                               config, Path('/var/lib/adaptive-os'))
        self.assertIn('--setenv=DISPLAY=:1', command)
        self.assertIn('--setenv=XAUTHORITY=/tmp/xauth_ABC123', command)
        self.assertIn('--setenv=XDG_SESSION_TYPE=x11', command)

    def test_xauthority_requires_private_owned_regular_file_and_valid_cookie(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            authority = Path(folder) / 'xauth_ABC123'
            authority.write_bytes(b'test cookie')
            authority.chmod(0o600)
            host = LinuxHost(Mock(metadata=Mock(return_value=None)), {
                'uid': os.getuid(), 'gid': os.getgid(), 'home': folder,
                'display': ':1', 'xauthority': str(authority)})
            def run(command, **kwargs):
                self.assertEqual(command[:6], ['setpriv', '--reuid', str(os.getuid()), '--regid', str(os.getgid()), '--init-groups'])
                self.assertIn('DISPLAY=:1', command)
                self.assertIn('XAUTHORITY=' + str(authority), command)
                self.assertEqual(command[-1], '/usr/bin/xdpyinfo')
                return 'name of display: :1'
            host.run = run
            self.assertTrue(host.display_access()['passed'])
            authority.chmod(0o644)
            self.assertFalse(host.display_access()['passed'])
            authority.chmod(0o600)
            host.config['uid'] += 1
            self.assertFalse(host.display_access()['passed'])
            host.config['uid'] = os.getuid()
            authority.rename(authority.with_suffix('.real'))
            authority.symlink_to(authority.with_suffix('.real'))
            self.assertFalse(host.display_access()['passed'])
            authority.unlink()
            self.assertFalse(host.display_access()['passed'])
            authority.write_bytes(b'expired cookie')
            authority.chmod(0o600)
            host.run = Mock(side_effect=RuntimeError('unable to open display'))
            failure = host.display_access()
            self.assertFalse(failure['passed'])
            self.assertIn('reconfigure', failure['error'].lower())
            import subprocess
            host.run = Mock(side_effect=subprocess.TimeoutExpired('xdpyinfo', 8))
            self.assertFalse(host.display_access()['passed'])

    def test_config_accepts_explicit_authority_and_legacy_default_but_not_unsafe_paths(self):
        from adaptive_os.guest_linux.linux import load_config
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'guest.json'
            config = {'uid': 1000, 'display': ':0'}
            path.write_text(json.dumps(config))
            info = SimpleNamespace(st_uid=0, st_mode=0o100600)
            user = SimpleNamespace(pw_dir='/home/guest', pw_gid=1000)
            with patch.object(Path, 'lstat', return_value=info), patch('adaptive_os.guest_linux.linux.pwd.getpwuid', return_value=user):
                self.assertEqual(load_config(path)['xauthority'], '/home/guest/.Xauthority')
                config['xauthority'] = '/tmp/xauth_ABC123'
                path.write_text(json.dumps(config))
                self.assertEqual(load_config(path)['xauthority'], '/tmp/xauth_ABC123')
                for unsafe in ('relative', '/tmp/../etc/shadow', '/tmp/%u', '/tmp/auth\nHOME=/root', 1):
                    path.write_text(json.dumps({**config, 'xauthority': unsafe}))
                    with self.subTest(path=unsafe), self.assertRaises(ValueError):
                        load_config(path)

    def test_session_check_does_not_mistake_xwayland_or_another_user_for_plasma_x11(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        host = LinuxHost(Mock(metadata=Mock(return_value=None)), {'uid': 1000, 'display': ':0'})
        values = {'User': '1000', 'Type': 'x11', 'Remote': 'no', 'Active': 'yes', 'Display': ':0', 'Desktop': 'KDE'}
        def run(command, **kwargs):
            if command[1] == 'show-user':
                return '2 3\n'
            # loginctl accepts repeated properties, not systemctl's comma list.
            return '\n'.join(f'{key}={value}' for key, value in values.items()
                             if '--property=' + key in command)
        host.run = run
        self.assertTrue(host.desktop_session()['passed'])
        for field, bad in (('Type', 'wayland'), ('User', '1001'), ('Remote', 'yes'), ('Active', 'no'), ('Display', ':1'), ('Desktop', 'xfce')):
            with self.subTest(field=field):
                previous, values[field] = values[field], bad
                self.assertFalse(host.desktop_session()['passed'])
                values[field] = previous

    def test_session_check_skips_only_a_vanished_session_and_still_requires_plasma(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        import subprocess
        host = LinuxHost(Mock(metadata=Mock(return_value=None)), {'uid': 1000, 'display': ':0'})
        missing = RuntimeError("loginctl failed: Failed to get path for session '604': No session '604' known")
        desktop = 'User=1000\nType=x11\nRemote=no\nActive=yes\nDisplay=:0\nDesktop=KDE\n'
        host.run = Mock(side_effect=['604 3\n', missing, desktop])
        result = host.desktop_session()
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['id'], '3')
        host.run = Mock(side_effect=['604\n', missing])
        self.assertFalse(host.desktop_session()['passed'])
        for error in (RuntimeError('loginctl failed: Access denied'),
                      RuntimeError("loginctl failed: Failed to get path for session '999': No session '999' known"),
                      subprocess.TimeoutExpired('loginctl', 8)):
            with self.subTest(error=str(error)):
                host.run = Mock(side_effect=['604 3\n', error, desktop])
                self.assertFalse(host.desktop_session()['passed'])
                self.assertEqual(host.run.call_count, 2)

    def test_baseline_fingerprint_changes_when_guest_packages_change(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        host = LinuxHost(Mock(metadata=Mock(return_value=None)), {'uid': 1000})
        packages = ['plasma-workspace\t5.27.11\nspice-vdagent\t0.22.1\n']
        host.run = lambda command, **kwargs: 'qemu\n' if command[0] == 'systemd-detect-virt' else packages[0]
        manifest = {'baseline_id': 'c5b4d7bd-65b8-481f-9a09-4c9c09b54abc', 'utm_version': '4.7.5',
                    'desktop_gate_passed': True, 'recovery_copy_verified': True}
        with patch('adaptive_os.guest_linux.linux.load_baseline', return_value=manifest), \
                patch('adaptive_os.guest_linux.linux.platform.release', return_value='6.8-test'):
            before = host.baseline_report()
            self.assertEqual(before['host'], manifest)
            self.assertEqual(before['hypervisor'], 'qemu')
            self.assertEqual(before['fingerprint'], host.baseline_report()['fingerprint'])
            packages[0] = packages[0].replace('5.27.11', '5.27.12')
            self.assertNotEqual(before['fingerprint'], host.baseline_report()['fingerprint'])

    def test_baseline_manifest_cannot_claim_readiness_without_desktop_and_cold_copy(self):
        from adaptive_os.guest_linux.linux import load_baseline
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'baseline.json'
            info = SimpleNamespace(st_uid=0, st_mode=0o100644, st_size=200)
            manifest = {'baseline_id': 'c5b4d7bd-65b8-481f-9a09-4c9c09b54abc', 'utm_version': '4.7.5',
                        'desktop_gate_passed': True, 'recovery_copy_verified': True}
            with patch.object(Path, 'lstat', return_value=info):
                path.write_text(json.dumps(manifest))
                self.assertEqual(load_baseline(path), manifest)
                for field in ('desktop_gate_passed', 'recovery_copy_verified'):
                    path.write_text(json.dumps({**manifest, field: False}))
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        load_baseline(path)
                path.write_text(json.dumps(manifest))
                info.st_mode = 0o100666
                with self.assertRaises(ValueError):
                    load_baseline(path)

    def test_display_reconfiguration_is_atomic_and_cannot_change_the_registered_user(self):
        from adaptive_os.guest_linux.linux import configure_display
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'guest.json'
            original = {'uid': 1000, 'display': ':0'}
            path.write_text(json.dumps(original))
            info = SimpleNamespace(st_uid=0, st_mode=0o100600)
            user = SimpleNamespace(pw_dir='/home/guest', pw_gid=1000)
            with patch.object(Path, 'lstat', return_value=info), patch('adaptive_os.guest_linux.linux.pwd.getpwuid', return_value=user):
                for uid, display, authority in ((1000, 'remote:0', '/tmp/xauth_OK'), (1001, ':1', '/tmp/xauth_OK'),
                                                 (1000, ':1', '/tmp/../unsafe')):
                    with self.subTest(uid=uid, display=display), self.assertRaises(ValueError):
                        configure_display(uid, display, authority, path)
                    self.assertEqual(json.loads(path.read_text()), original)
                configure_display(1000, ':1.0', '/tmp/xauth_NEW', path)
                self.assertEqual(json.loads(path.read_text()), {'uid': 1000, 'display': ':1', 'xauthority': '/tmp/xauth_NEW'})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_host_preflight_creates_nested_evidence_directory(self):
        from adaptive_os.guest_linux.preflight import main
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'missing' / 'evidence.json'
            with patch('sys.argv', ['preflight', '--output', str(output)]), patch('builtins.print'):
                main()
            self.assertFalse(json.loads(output.read_text())['vm_verified'])

    def test_restoration_uses_identity_only_and_does_not_require_new_journal_writes(self):
        from adaptive_os.guest_linux.linux import LinuxHost
        from unittest.mock import Mock
        host = LinuxHost(Mock(metadata=Mock(return_value=None)), {'uid': 1000})
        task = {'id': 'a5762b2b-2785-4d9b-9120-fd8ed38bf62f', 'slot': 1, 'identity': 'valid'}
        host.inspect = Mock(side_effect=OSError('telemetry unavailable'))
        host.identity = Mock(return_value={**task, 'unit': 'owned.service', 'cpu_weight': 90})
        host.journal.event.side_effect = OSError('disk full')
        with patch.object(host, 'run', return_value='') as kernel:
            host.restore_weight(task, 100)
            self.assertEqual(kernel.call_args.args[0], ['systemctl', 'set-property', '--runtime', 'owned.service', 'CPUWeight=100'])

    def test_unshaped_topology_has_shared_transit_and_root_only_nat(self):
        from adaptive_os.guest_linux.linux import gateway_commands, task_network_commands
        commands = gateway_commands('aos-gw-test', 'enp0s1', 'aos_test')
        self.assertIn(['ip', 'link', 'add', 'h-down', 'type', 'veth', 'peer', 'name', 'g-up'], commands)
        self.assertIn(['ip', '-n', 'aos-gw-test', 'route', 'add', 'default', 'via', '10.203.0.1'], commands)
        self.assertIn(['ip', 'route', 'add', '10.203.0.0/16', 'via', '10.203.0.2', 'dev', 'h-down'], commands)
        self.assertFalse(any('tc' in command for command in commands))
        tasks = task_network_commands('aos-gw-test', 'aos-task-test', 2)
        self.assertIn(['ip', '-n', 'aos-task-test', 'route', 'add', 'default', 'via', '10.203.2.1'], tasks)
        self.assertIn(['ip', '-n', 'aos-task-test', 'addr', 'add', '10.203.2.2/30', 'dev', 't2'], tasks)
        self.assertFalse(any('masquerade' in ' '.join(c) for c in tasks))
