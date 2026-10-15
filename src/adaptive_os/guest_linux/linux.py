"""Ubuntu/systemd adapter. Names and commands come only from the root registry."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
from stat import S_ISREG
import subprocess
import tempfile
import uuid

from .protocol import identifier
from .resources import MEMORY_PARENT, SAFETY, POOL, memory_values
from .network import parse_classes, read_shaper, shaping_commands, tc_prefix, validate_settings

STATE = Path('/var/lib/adaptive-os')
CGROUP = Path('/sys/fs/cgroup')
SUBNET = ipaddress.ip_network('10.203.0.0/16')
DNS_LINK_TARGETS = {prefix + name for prefix in ('../run/systemd/resolve/', '/run/systemd/resolve/')
                    for name in ('stub-resolv.conf', 'resolv.conf')}


def names(task):
    key = identifier(task['id'])
    if type(task['slot']) is not int or not 1 <= task['slot'] <= 8:
        raise ValueError('Invalid task slot')
    return f'adaptiveos-task-{key}.service', f'aos-t-{key}'


def dns_config(source):
    addresses = []
    for line in source.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == 'nameserver':
            address = ipaddress.ip_address(parts[1])
            if address.version == 4 and not (address.is_loopback or address.is_unspecified or address.is_multicast):
                addresses.append(str(address))
    if not addresses:
        raise ValueError('No reachable IPv4 DNS resolver; the loopback stub cannot serve task namespaces')
    return ''.join(f'nameserver {address}\n' for address in dict.fromkeys(addresses))


def task_command(task, config, state):
    unit, namespace = names(task)
    profile = state / 'apps' / task['id']
    if task['app'] == 'firefox':
        app = ['/usr/bin/firefox', '--no-remote', '--new-instance', '--profile', str(profile), 'about:blank']
    elif task['app'] == 'geany':
        app = ['/usr/bin/geany', '--new-instance', '--config', str(profile)]
    else:
        raise ValueError('Unregistered application')
    properties = [f'User={config["uid"]}', f'Group={config["gid"]}', 'Slice=adaptiveos.slice',
                  'ExitType=cgroup', 'KillMode=control-group', 'CPUWeight=100', 'AllowedCPUs=1-3',
                  'CPUAccounting=yes', 'MemoryAccounting=yes', 'ProtectControlGroups=yes',
                  'NoNewPrivileges=yes', 'RestrictSUIDSGID=yes',
                  f'NetworkNamespacePath=/run/netns/{namespace}',
                  'TemporaryFileSystem=/run/systemd/resolve',
                  f'BindReadOnlyPaths={state}/resolv.conf:/etc/resolv.conf',
                  f'WorkingDirectory={profile}',
                  f'InaccessiblePaths=-/run/user/{config["uid"]}/systemd -/run/user/{config["uid"]}/bus']
    environment = {'HOME': config['home'], 'DISPLAY': config['display'],
                   'XAUTHORITY': config.get('xauthority', config['home'] + '/.Xauthority'),
                   'XDG_RUNTIME_DIR': f'/run/user/{config["uid"]}', 'XDG_SESSION_TYPE': 'x11',
                   'MOZ_ENABLE_WAYLAND': '0'}
    return ['systemd-run', '--quiet', '--collect', '--service-type=exec', f'--unit={unit}',
            *['--property=' + p for p in properties],
            *[f'--setenv={k}={v}' for k, v in environment.items()],
            '--', '/usr/bin/dbus-run-session', '--', *app]


def create_profile(state, task_id, uid, gid):
    apps = state / 'apps'
    apps.mkdir(mode=0o711, exist_ok=True)
    os.chmod(apps, 0o711)  # The service's 0077 umask must not block access to user-owned profiles.
    profile = apps / identifier(task_id)
    profile.mkdir(mode=0o700)
    os.chown(profile, uid, gid)
    return profile


def gateway_commands(gateway, uplink, table):
    # NAT is deliberately confined to the root namespace after the shared transit.
    return [
        ['ip', 'netns', 'add', gateway],
        ['ip', 'link', 'add', 'h-down', 'type', 'veth', 'peer', 'name', 'g-up'],
        ['ip', 'link', 'set', 'g-up', 'netns', gateway],
        ['ip', 'addr', 'add', '10.203.0.1/30', 'dev', 'h-down'],
        ['ip', 'link', 'set', 'h-down', 'up'],
        ['ip', '-n', gateway, 'addr', 'add', '10.203.0.2/30', 'dev', 'g-up'],
        ['ip', '-n', gateway, 'link', 'set', 'g-up', 'up'],
        ['ip', '-n', gateway, 'link', 'set', 'lo', 'up'],
        ['ip', '-n', gateway, 'route', 'add', 'default', 'via', '10.203.0.1'],
        ['ip', 'route', 'add', str(SUBNET), 'via', '10.203.0.2', 'dev', 'h-down'],
        ['ip', 'netns', 'exec', gateway, 'sysctl', '-qw', 'net.ipv4.ip_forward=1',
         'net.ipv6.conf.all.disable_ipv6=1', 'net.ipv6.conf.default.disable_ipv6=1'],
        ['sysctl', '-qw', 'net.ipv4.ip_forward=1'],
    ]


def task_network_commands(gateway, namespace, slot):
    return [
        ['ip', 'netns', 'add', namespace],
        ['ip', '-n', gateway, 'link', 'add', f'g{slot}', 'type', 'veth', 'peer', 'name', f't{slot}'],
        ['ip', '-n', gateway, 'link', 'set', f't{slot}', 'netns', namespace],
        ['ip', '-n', gateway, 'addr', 'add', f'10.203.{slot}.1/30', 'dev', f'g{slot}'],
        ['ip', '-n', gateway, 'link', 'set', f'g{slot}', 'up'],
        ['ip', 'netns', 'exec', namespace, 'sysctl', '-qw', 'net.ipv6.conf.all.disable_ipv6=1',
         'net.ipv6.conf.default.disable_ipv6=1'],
        ['ip', '-n', namespace, 'addr', 'add', f'10.203.{slot}.2/30', 'dev', f't{slot}'],
        ['ip', '-n', namespace, 'link', 'set', f't{slot}', 'up'],
        ['ip', '-n', namespace, 'link', 'set', 'lo', 'up'],
        ['ip', '-n', namespace, 'route', 'add', 'default', 'via', f'10.203.{slot}.1'],
    ]


class LinuxHost:
    def __init__(self, journal, config, state=STATE):
        self.journal, self.config, self.state = journal, config, Path(state)
        self.network = journal.metadata('network')

    def run(self, command, *, input=None):
        result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=8,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise RuntimeError(f'{command[0]} failed: {result.stderr.strip()[:500]}')
        return result.stdout

    def mutate(self, command, *, input=None):
        intent = self.journal.event('kernel_intent', command=command, input=input)
        result = self.run(command, input=input)
        self.journal.event('kernel_applied', intent=intent)
        return result

    def display_access(self):
        authority = Path(self.config.get('xauthority', self.config['home'] + '/.Xauthority'))
        try:
            info = authority.lstat()
            if not S_ISREG(info.st_mode) or info.st_uid != self.config['uid'] or info.st_mode & 0o077:
                raise ValueError('Xauthority must be a private regular file owned by the desktop user')
            self.run(['setpriv', '--reuid', str(self.config['uid']), '--regid', str(self.config['gid']),
                      '--init-groups', '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
                      'DISPLAY=' + self.config['display'], 'XAUTHORITY=' + str(authority), '/usr/bin/xdpyinfo'])
            return {'passed': True}
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            return {'passed': False, 'error': f'{exc}; log into Plasma (X11) and reconfigure the guest display with the installer'}

    def desktop_session(self):
        try:
            sessions = self.run(['loginctl', 'show-user', str(self.config['uid']), '--property=Sessions', '--value']).split()
            for session in sessions:
                try:
                    output = self.run(['loginctl', 'show-session', session,
                                       *['--property=' + key for key in ('User', 'Type', 'Remote', 'Active', 'Display', 'Desktop')]])
                except RuntimeError as exc:
                    # SSH sessions can end between the two loginctl queries.
                    if str(exc) == f"loginctl failed: Failed to get path for session '{session}': No session '{session}' known":
                        continue
                    raise
                data = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
                if all(data.get(key) == value for key, value in {
                        'User': str(self.config['uid']), 'Type': 'x11', 'Remote': 'no',
                        'Active': 'yes', 'Display': self.config['display']}.items()) and 'KDE' in data.get('Desktop', '').split(':'):
                    return {'passed': True, 'id': session, **data}
            return {'passed': False, 'error': 'No active local Plasma X11 login matches the configured user and display'}
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            return {'passed': False, 'error': str(exc)}

    def baseline_report(self):
        packages = self.run(['dpkg-query', '-W', '-f=${Package}\t${Version}\n',
                             'plasma-workspace', 'sddm', 'spice-vdagent', 'qemu-guest-agent', 'firefox', 'geany'])
        baseline = {'host': load_baseline(), 'hypervisor': self.run(['systemd-detect-virt', '--vm']).strip(),
                    'kernel': platform.release(), 'architecture': platform.machine(), 'session_type': 'x11',
                    'packages': dict(line.split('\t', 1) for line in packages.splitlines() if '\t' in line)}
        baseline['fingerprint'] = hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest()
        return baseline

    def preflight(self):
        checks = {}
        os_info = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        checks['ubuntu_24_04'] = os_info.get('ID') == 'ubuntu' and os_info.get('VERSION_ID', '').strip('"') == '24.04'
        checks['arm64'] = platform.machine() == 'aarch64'
        checks['four_cpus'] = Path('/sys/devices/system/cpu/online').read_text().strip() == '0-3'
        memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                          if line.startswith('MemTotal:'))) * 1024
        checks['eight_gib_ram'] = 7.5 * 1024**3 <= memory <= 8 * 1024**3
        disk = os.statvfs(self.state)
        checks['sixty_four_gib_disk'] = disk.f_blocks * disk.f_frsize >= 60 * 1024**3
        checks['cgroup_v2'] = {'cpu', 'memory', 'cpuset'} <= set((CGROUP / 'cgroup.controllers').read_text().split())
        resolver = Path('/etc/resolv.conf')
        checks['resolver_layout'] = resolver.is_symlink() and str(resolver.readlink()) in DNS_LINK_TARGETS
        checks['x11'] = Path('/tmp/.X11-unix/X' + self.config['display'][1:]).exists()
        display = self.display_access()
        checks['xauthority'] = display['passed']
        session = self.desktop_session()
        checks['local_x11_session'] = session['passed']
        runtime = Path(f'/run/user/{self.config["uid"]}')
        checks['user_runtime'] = (runtime.is_dir() and not runtime.is_symlink()
                                  and runtime.stat().st_uid == self.config['uid']
                                  and runtime.stat().st_mode & 0o777 == 0o700)
        try:
            baseline = self.baseline_report()
            checks['utm_baseline'] = baseline['hypervisor'] == 'qemu'
        except (OSError, RuntimeError, ValueError) as exc:
            baseline = {'error': str(exc)}
            checks['utm_baseline'] = False
        for app in ('firefox', 'geany'):
            path = Path('/usr/bin') / app
            checks[app + '_native'] = (path.is_file() and '/snap/' not in str(path.resolve())
                                      and path.stat().st_uid == 0 and not path.stat().st_mode & 0o022
                                      and b'snap' not in path.read_bytes()[:4096])
        checks['systemd_255'] = int(self.run(['systemctl', '--version']).split()[1]) >= 255
        parent = CGROUP / 'adaptiveos.slice'
        parent_values = {}
        if parent.exists():
            parent_values = {key: (parent / key).read_text().strip() for key in
                             ('memory.low', 'memory.min', 'memory.high', 'memory.max', 'cpuset.cpus.effective')}
            checks['parent_baseline'] = (parent_values['memory.low'] in ('0', str(POOL)) and
                all(parent_values[key] == value for key, value in {'memory.min': '0', 'memory.high': 'max',
                    'memory.max': str(SAFETY), 'cpuset.cpus.effective': '1-3'}.items()))
        return {'passed': all(checks.values()), 'checks': checks, 'parent': parent_values, 'display': display,
                'session': session, 'baseline': baseline,
                'backend': 'guest-linux', 'shaped': False, 'vm_verified': False,
                'memory_current_is_working_set': False}

    def ensure_network(self):
        if self.network:
            if not self.network['ready']:
                raise RuntimeError('Interrupted network setup requires reconciliation before launch')
            return
        links = {row['ifname'] for row in json.loads(self.run(['ip', '-j', 'link', 'show']))}
        if links & {'h-down', 'g-up'}:
            raise RuntimeError('Shared transit name is already in use; refusing to adopt it')
        routes = json.loads(self.run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
        for row in routes:
            if row.get('dst', 'default') != 'default' and ipaddress.ip_network(row['dst'], strict=False).overlaps(SUBNET):
                raise RuntimeError('Task subnet conflicts with an existing guest route')
        defaults = json.loads(self.run(['ip', '-j', '-4', 'route', 'show', 'default']))
        if len(defaults) != 1 or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', defaults[0].get('dev', '')):
            raise RuntimeError('Exactly one guest IPv4 uplink is required')
        token = uuid.uuid4().hex[:12]
        self.network = {'gateway': 'aos-gw-' + token, 'table': 'aos_' + token, 'uplink': defaults[0]['dev'],
                        'forward_before': Path('/proc/sys/net/ipv4/ip_forward').read_text().strip(),
                        'ready': False, 'tasks': {}}
        self.journal.save_metadata('network', self.network)
        for command in gateway_commands(self.network['gateway'], self.network['uplink'], self.network['table']):
            self.mutate(command)
        table, uplink = self.network['table'], self.network['uplink']
        self.mutate(['nft', '-f', '-'], input=(
            f'table ip {table} {{\n'
            ' chain nat { type nat hook postrouting priority srcnat; policy accept;\n'
            f'  ip saddr {SUBNET} oifname "{uplink}" masquerade\n }}\n'
            ' chain forwarding { type filter hook forward priority filter; policy accept;\n'
            f'  iifname "h-down" oifname "{uplink}" ip saddr {SUBNET} accept\n'
            f'  iifname "{uplink}" oifname "h-down" ip daddr {SUBNET} ct state established,related accept\n'
            '  iifname "h-down" drop\n  oifname "h-down" drop\n }\n}\n'))
        self.network['ready'] = True
        self.journal.save_metadata('network', self.network)

    def launch(self, task):
        if not self.preflight()['passed']:
            raise RuntimeError('Guest prerequisites failed; inspect snapshot preflight checks')
        self.mutate(['systemctl', 'start', 'adaptiveos.slice'])
        if not self.preflight()['passed']:
            raise RuntimeError('Root slice safety cap or placement does not match the baseline')
        source = Path('/run/systemd/resolve/resolv.conf')
        if not source.exists():
            source = Path('/etc/resolv.conf')
        (self.state / 'resolv.conf').write_text(dns_config(source.read_text()))
        os.chmod(self.state / 'resolv.conf', 0o644)
        self.ensure_network()
        unit, namespace = names(task)
        if (Path('/run/netns') / namespace).exists():
            raise RuntimeError('Task namespace already exists; refusing adoption')
        self.network['tasks'][task['id']] = task['slot']
        self.journal.save_metadata('network', self.network)
        for command in task_network_commands(self.network['gateway'], namespace, task['slot']):
            self.mutate(command)
        create_profile(self.state, task['id'], self.config['uid'], self.config['gid'])
        self.mutate(task_command(task, self.config, self.state))
        return self.inspect(task)

    def identity(self, task):
        unit, namespace = names(task)
        data = dict(line.split('=', 1) for line in self.run(['systemctl', 'show', unit,
                    '--property=LoadState,ActiveState,InvocationID,ControlGroup,MainPID']).splitlines() if '=' in line)
        if data.get('LoadState') == 'not-found' or data.get('ActiveState') in ('inactive', 'failed'):
            return None
        expected = '/adaptiveos.slice/' + unit
        if data.get('ControlGroup') != expected or not data.get('InvocationID'):
            raise RuntimeError('Task is outside the direct root slice or lacks an invocation identity')
        return {**task, 'unit': unit, 'identity': Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + data['InvocationID'],
                'cgroup': expected, 'main_pid': int(data['MainPID']),
                'cpus_effective': (CGROUP / expected.lstrip('/') / 'cpuset.cpus.effective').read_text().strip(),
                'cpu_weight': int((CGROUP / expected.lstrip('/') / 'cpu.weight').read_text())}

    def task_dns(self, pid):
        proc = Path('/proc') / str(pid)
        target = str((proc / 'root/etc/resolv.conf').readlink())
        if target not in DNS_LINK_TARGETS:
            raise RuntimeError('Task DNS symlink leaves its private resolver directory')
        # Resolved replaces files atomically; a private parent keeps those replacements out.
        mounts = [line.split(' - ', 1) for line in (proc / 'mountinfo').read_text().splitlines()]
        private = any(len(row) == 2 and len(row[0].split()) >= 5
                      and row[0].split()[4] == '/run/systemd/resolve' and row[1].split()[0] == 'tmpfs'
                      for row in mounts)
        actual = (proc / 'root/run/systemd/resolve' / target.rsplit('/', 1)[1]).read_text()
        expected = dns_config((self.state / 'resolv.conf').read_text())
        if not private or actual != expected:
            raise RuntimeError('Task DNS override is missing or changed')
        return {'nameservers': [line.split()[1] for line in expected.splitlines()], 'private_directory': True}

    def inspect(self, task):
        base = self.identity(task)
        if base is None:
            return None
        dns = self.task_dns(base['main_pid'])
        _, namespace = names(task)
        expected = base['cgroup']
        group = CGROUP / expected.lstrip('/')
        values = {key: (group / key).read_text().strip() for key in
                  ('cpuset.cpus.effective', 'memory.current', 'memory.low', 'memory.min', 'memory.high', 'memory.max')}
        cpu = dict(line.split() for line in (group / 'cpu.stat').read_text().splitlines())
        processes, isolated = [], []
        managed_net = os.stat('/run/netns/' + namespace).st_ino
        pids = {int(pid) for file in group.rglob('cgroup.procs') for pid in file.read_text().split()}
        for pid in sorted(pids):
            proc = Path('/proc') / str(pid)
            try:
                net = (proc / 'ns/net').stat().st_ino
                cgroup = (proc / 'cgroup').read_text().strip().removeprefix('0::')
                affinity = next(line.split(':', 1)[1].strip() for line in (proc / 'status').read_text().splitlines()
                                if line.startswith('Cpus_allowed_list:'))
                start_ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                process = {'pid': pid, 'start_ticks': start_ticks, 'cgroup': cgroup,
                           'network_namespace': net, 'cpus_allowed': affinity}
                if net != managed_net:
                    routes = (proc / 'net/route').read_text().splitlines()[1:]
                    process['isolated_no_ipv4_route'] = not routes
                    isolated.append(process)
                processes.append(process)
            except FileNotFoundError:
                continue
        return {**base,
                'cpu_usage_usec': int(cpu['usage_usec']), 'cpu_pressure': (group / 'cpu.pressure').read_text(),
                'cpus_effective': values['cpuset.cpus.effective'], 'memory_current': int(values['memory.current']),
                'memory_low': int(values['memory.low']), 'memory_min': int(values['memory.min']),
                'memory_high': values['memory.high'], 'memory_max': values['memory.max'],
                'parent_memory_max': int((CGROUP / 'adaptiveos.slice/memory.max').read_text()),
                'memory_events': (group / 'memory.events').read_text(), 'memory_pressure': (group / 'memory.pressure').read_text(),
                'memory_stat': dict((key, int(value)) for key, value in
                                    (line.split() for line in (group / 'memory.stat').read_text().splitlines())),
                'processes': processes, 'isolated_namespaces': isolated,
                'network': {'namespace': namespace, 'address': f'10.203.{task["slot"]}.2', 'shaped': False, 'dns': dns,
                            'upload_device': 'g-up', 'download_device': 'h-down',
                            'gateway': (self.network or {}).get('gateway'), 'uplink': (self.network or {}).get('uplink')},
                'profile': str(self.state / 'apps' / task['id'])}

    def memory_settings(self, task=None):
        observed = self.identity(task) if task else None
        if task and (not observed or observed['identity'] != task['identity']):
            raise RuntimeError('Memory application identity changed')
        group = CGROUP / (observed['cgroup'].lstrip('/') if task else 'adaptiveos.slice')
        return memory_values({'memory_' + key: (group / ('memory.' + key)).read_text().strip()
                              for key in ('low', 'min', 'high', 'max')})

    def memory_configuration(self, tasks):
        parent = self.memory_settings()
        if parent['min'] != 0 or parent['high'] != 'max' or parent['max'] != SAFETY or parent['low'] not in (0, POOL):
            raise ValueError('Memory ancestor safety/protection configuration differs')
        rows, ancestors = {}, {}
        for task in tasks:
            observed = self.identity(task)
            if not observed or Path(observed['cgroup']).parent != Path('/adaptiveos.slice'):
                raise ValueError('Memory ancestor hierarchy must be the direct root slice')
            rows[task['id']] = self.memory_settings(task)
            ancestors[task['id']] = ['/', '/adaptiveos.slice', observed['cgroup']]
            if rows[task['id']]['min'] != 0 or rows[task['id']]['max'] != 'max':
                raise ValueError('Unexpected child hard memory boundary')
        return {'parent': parent, 'tasks': rows, 'ancestors': ancestors}

    def memory_parent(self):
        group = CGROUP / 'adaptiveos.slice'
        return {**self.memory_settings(), 'current': int((group / 'memory.current').read_text()),
                'pressure': (group / 'memory.pressure').read_text(),
                'host_pressure': Path('/proc/pressure/memory').read_text(),
                'events': (group / 'memory.events').read_text()}

    def set_memory(self, task, settings, *, restore=False):
        if set(settings) != {'low', 'min', 'high', 'max'}:
            raise ValueError('Incomplete memory configuration')
        if (type(settings['low']) is not int or not 0 <= settings['low'] <= POOL or settings['min'] != 0
                or settings['max'] != ('max' if task else SAFETY)
                or settings['high'] != 'max' and
                (type(settings['high']) is not int or not settings['low'] <= settings['high'] <= SAFETY)):
            raise ValueError('Memory settings exceed managed bounds')
        if not task and settings not in (MEMORY_PARENT, {**MEMORY_PARENT, 'low': 0}):
            raise ValueError('Parent memory safety cap is fixed')
        self.memory_settings(task)  # Authenticate the live invocation immediately before writing.
        command = ['systemctl', 'set-property', '--runtime', names(task)[0] if task else 'adaptiveos.slice',
                   *[f'Memory{key.title()}={"infinity" if value == "max" else value}' for key, value in settings.items()]]
        (self.run if restore else self.mutate)(command)
        if self.memory_settings(task) != settings:
            raise RuntimeError('Memory kernel read-back mismatch')

    def network_devices(self):
        if not self.network or not self.network['ready']:
            raise ValueError('Owned routed gateway is not ready')
        return [('upload', self.network['gateway'], 'g-up'), ('download', None, 'h-down')]

    def network_telemetry(self, tasks):
        result = {'settings': {}, 'tasks': {t['id']: {} for t in tasks}, 'raw': {}}
        for direction, namespace, device in self.network_devices():
            prefix = tc_prefix(namespace) + ['-j', '-s', '-d']
            qdiscs = json.loads(self.run(prefix + ['qdisc', 'show', 'dev', device]))
            classes = parse_classes(self.run(prefix + ['class', 'show', 'dev', device]))
            filters = json.loads(self.run(prefix + ['filter', 'show', 'dev', device, 'parent', '1:']))
            setting, telemetry = read_shaper(qdiscs, classes, filters, tasks, direction)
            result['settings'][direction] = setting
            result['raw'][direction] = {'qdisc': qdiscs, 'class': classes, 'filter': filters}
            for key, values in telemetry.items():
                result['tasks'][key].update(values)
        return result

    def network_configuration(self, tasks):
        return self.network_telemetry(tasks)['settings']

    def set_network(self, tasks, settings, *, restore=False):
        if set(settings) != {'upload', 'download'} or not (0 if restore else 2) <= len(tasks) <= 8:
            raise ValueError('Invalid directional network configuration')
        for row in settings.values():
            if row is not None:
                validate_settings(tasks, row)
        for task in tasks:
            observed = self.identity(task)
            if restore and observed is None:
                continue
            if not observed or observed['identity'] != task['identity']:
                raise RuntimeError('Network application identity changed')
        write = self.run if restore else self.mutate
        errors = []
        for direction, namespace, device in self.network_devices():
            try:
                prefix = tc_prefix(namespace)
                qdiscs = json.loads(self.run(prefix + ['-j', 'qdisc', 'show', 'dev', device]))
                roots = [row for row in qdiscs if row.get('root')]
                if len(roots) != 1 or roots[0]['kind'] not in ('noqueue', 'htb'):
                    raise ValueError('Foreign shared-device qdisc; refusing replacement')
                shaped = roots[0]['kind'] == 'htb'
                if shaped and roots[0]['handle'] != '1:':
                    raise ValueError('Foreign HTB handle; refusing replacement')
                desired = settings[direction]
                if restore:
                    # A torn create/update can have an incomplete tree; durable run ownership authorizes deleting it.
                    if shaped:
                        write(prefix + ['qdisc', 'del', 'dev', device, 'root', 'handle', '1:'])
                    if desired is not None:
                        for command in shaping_commands(namespace, device, direction, tasks, desired):
                            write(command)
                elif desired is None:
                    raise ValueError('Only restoration can remove shaping')
                else:
                    before = self.network_configuration(tasks)[direction] if shaped else None
                    for command in shaping_commands(namespace, device, direction, tasks, desired,
                                                    initialize=not shaped, before=before):
                        write(command)
            except Exception as exc:
                if not restore:
                    raise
                errors.append(direction + ': ' + str(exc))
        if errors:
            raise RuntimeError('Network restoration pending: ' + '; '.join(errors))
        if self.network_configuration(tasks) != settings:
            raise RuntimeError('Network kernel read-back mismatch')

    def set_weight(self, task, weight):
        if type(weight) is not int or weight not in (25, 50, 90, 100):
            raise ValueError('Only bounded CPU weights and the feasibility probe are enabled')
        observed = self.identity(task)
        if not observed or observed['identity'] != task['identity']:
            raise RuntimeError('Application identity changed before write')
        self.mutate(['systemctl', 'set-property', '--runtime', observed['unit'], f'CPUWeight={weight}'])

    def restore_weight(self, task, weight):
        if type(weight) is not int or weight != 100:
            raise ValueError('Only the captured weight-100 baseline can be restored')
        observed = self.identity(task)
        if not observed or observed['identity'] != task['identity']:
            raise RuntimeError('Application identity changed before restoration')
        # The committed run baseline authorizes rollback even when new journal writes fail.
        self.run(['systemctl', 'set-property', '--runtime', observed['unit'], f'CPUWeight={weight}'])

    def cleanup(self, task):
        if self.identity(task) is not None:
            raise RuntimeError('Refusing network cleanup while application processes remain')
        if not self.network:
            return
        unit, namespace = names(task)
        if task['id'] in self.network['tasks']:
            if (Path('/run/netns') / namespace).exists():
                self.mutate(['ip', 'netns', 'delete', namespace])
            gateway = self.network['gateway']
            if (Path('/run/netns') / gateway).exists():
                links = {row['ifname'] for row in json.loads(self.run(['ip', '-j', '-n', gateway, 'link', 'show']))}
                for interface in (f'g{task["slot"]}', f't{task["slot"]}'):
                    if interface in links:
                        self.mutate(['ip', '-n', gateway, 'link', 'delete', interface])
                        break
            del self.network['tasks'][task['id']]
            self.journal.save_metadata('network', self.network)
        if self.network['tasks']:
            return
        self.journal.event('network_cleanup_intent', network=self.network)
        tables = json.loads(self.run(['nft', '-j', 'list', 'tables']))['nftables']
        if any(row.get('table', {}).get('name') == self.network['table'] for row in tables):
            self.mutate(['nft', 'delete', 'table', 'ip', self.network['table']])
        if (Path('/run/netns') / self.network['gateway']).exists():
            self.mutate(['ip', 'netns', 'delete', self.network['gateway']])
        links = {row['ifname'] for row in json.loads(self.run(['ip', '-j', 'link', 'show']))}
        for interface in ('h-down', 'g-up'):
            if interface in links:
                self.mutate(['ip', 'link', 'delete', interface])
                break
        if self.network['forward_before'] == '0':
            self.mutate(['sysctl', '-qw', 'net.ipv4.ip_forward=0'])
        self.network = None
        self.journal.save_metadata('network', None)


def load_config(path=Path('/etc/adaptive-os/guest.json')):
    stat = path.lstat()
    if not S_ISREG(stat.st_mode) or stat.st_uid != 0 or stat.st_mode & 0o022:
        raise ValueError('Guest configuration must be root-owned and not group/world writable')
    config = json.loads(path.read_text())
    if (not isinstance(config, dict) or not {'uid', 'display'} <= set(config)
            or set(config) - {'uid', 'display', 'xauthority'}
            or type(config['uid']) is not int or config['uid'] < 1000):
        raise ValueError('Configure one ordinary desktop user and display')
    if not isinstance(config['display'], str) or not re.fullmatch(r':[0-9]{1,2}', config['display']):
        raise ValueError('Only a local X11 display is supported')
    user = pwd.getpwuid(config['uid'])
    if not re.fullmatch(r'/[a-zA-Z0-9_./-]+', user.pw_dir):
        raise ValueError('Unsupported home path in systemd properties')
    authority = config.get('xauthority', user.pw_dir + '/.Xauthority')
    if (not isinstance(authority, str) or not re.fullmatch(r'/[a-zA-Z0-9_./-]+', authority)
            or '..' in Path(authority).parts):
        raise ValueError('Xauthority must be an absolute local path without traversal or systemd specifiers')
    return {**config, 'gid': user.pw_gid, 'home': user.pw_dir, 'xauthority': authority}


def load_baseline(path=Path('/etc/adaptive-os/baseline.json')):
    info = path.lstat()
    if not S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 65536:
        raise ValueError('Baseline manifest must be a small root-owned file without group/world write access')
    baseline = json.loads(path.read_text())
    if (not isinstance(baseline, dict) or not isinstance(baseline.get('utm_version'), str)
            or not 1 <= len(baseline['utm_version']) <= 100
            or baseline.get('desktop_gate_passed') is not True
            or baseline.get('recovery_copy_verified') is not True):
        raise ValueError('Record the UTM version and pass desktop/recovery-copy verification before installing the controller')
    identifier(baseline.get('baseline_id'))
    return baseline


def configure_display(uid, display, authority, path=Path('/etc/adaptive-os/guest.json')):
    if path.exists() and load_config(path)['uid'] != uid:
        raise ValueError('Display reconfiguration cannot change the registered desktop user')
    config = {'uid': uid, 'display': display.removesuffix('.0'), 'xauthority': authority}
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        candidate = Path(stream.name)
        try:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
            load_config(candidate)
            os.replace(candidate, path)
        finally:
            candidate.unlink(missing_ok=True)
