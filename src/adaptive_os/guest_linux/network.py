"""Owned shared HTB topology. Every class uses the same 20-Mbit parent per direction."""

import json
import re

from .resources import NETWORK_LIMITS, PARENT_BPS


def parse_classes(text):
    if text.lstrip().startswith('['):
        return json.loads(text)
    rows = []
    # Ubuntu Noble's tc 6.1 ignores -j for classes; our integer-Kbit rates print exactly.
    for block in re.split(r'(?=^class )', text, flags=re.MULTILINE):
        if not block.strip():
            continue
        header = re.match(r'class (\w+) ([0-9a-f:]+) (root|parent ([0-9a-f:]+)) ', block)
        rates = re.search(r' rate (\d+)([KMG]?)bit ceil (\d+)([KMG]?)bit ', block)
        stats = re.search(r' Sent (\d+) bytes \d+ pkt \(dropped (\d+),', block)
        if not header or header[1] != 'htb' or not rates or not stats:
            raise ValueError('Unrecognized kernel HTB class read-back')
        units = {'': 1, 'K': 1000, 'M': 1000 ** 2, 'G': 1000 ** 3}
        rows.append({'kind': header[1], 'handle': header[2], 'parent': header[4] or 'root',
                     'options': {'rate': int(rates[1]) * units[rates[2]] // 8,
                                 'ceil': int(rates[3]) * units[rates[4]] // 8},
                     'bytes': int(stats[1]), 'drops': int(stats[2])})
    return rows


def tc_prefix(namespace):
    return (['ip', 'netns', 'exec', namespace] if namespace else []) + ['tc']


def class_id(task):
    return f'1:{0x100 + task["slot"]:x}'


def validate_settings(tasks, settings):
    if (set(settings) != {'shares', *NETWORK_LIMITS} or any(settings[k] != v for k, v in NETWORK_LIMITS.items())
            or set(settings['shares']) != {t['id'] for t in tasks}
            or any(type(v) is not int or not 500 <= v <= 8000 for v in settings['shares'].values())
            or sum(settings['shares'].values()) != 10_000):
        raise ValueError('Invalid shared network bounds or task set')


def shaping_commands(namespace, device, direction, tasks, settings, *, initialize=True, before=None):
    prefix, commands = tc_prefix(namespace), []
    if initialize:
        commands.extend([
            ['qdisc', 'add', 'dev', device, 'root', 'handle', '1:', 'htb', 'default', class_id(tasks[0]).split(':')[1]],
            ['class', 'add', 'dev', device, 'parent', '1:', 'classid', '1:1', 'htb',
             'rate', '20000000bit', 'ceil', '20000000bit', 'burst', '32k', 'cburst', '32k']])
    ordered = sorted(tasks, key=lambda t: (settings['shares'][t['id']] - (before or settings)['shares'][t['id']], t['slot']))
    for task in ordered:
        key, slot = class_id(task), task['slot']
        commands.append(['class', 'add' if initialize else 'change', 'dev', device, 'parent', '1:1', 'classid', key,
                         'htb', 'rate', str(PARENT_BPS * settings['shares'][task['id']] // 10_000) + 'bit',
                         'ceil', '20000000bit', 'burst', '32k', 'cburst', '32k'])
        if initialize:
            commands.extend([
                ['qdisc', 'add', 'dev', device, 'parent', key, 'handle', f'{0x100 + slot:x}:', 'pfifo', 'limit', '256'],
                ['filter', 'add', 'dev', device, 'parent', '1:', 'protocol', 'ip', 'prio', str(100 + slot),
                 'flower', 'src_ip' if direction == 'upload' else 'dst_ip', f'10.203.{slot}.2/32', 'classid', key]])
    return [prefix + command for command in commands]


def read_shaper(qdiscs, classes, filters, tasks, direction):
    roots = [row for row in qdiscs if row.get('root')]
    if len(roots) == 1 and roots[0]['kind'] == 'noqueue' and len(qdiscs) == 1:
        return None, {}
    if len(roots) != 1 or roots[0]['kind'] != 'htb' or roots[0]['handle'] != '1:':
        raise ValueError('Foreign shared-device qdisc; refusing adoption')
    by_class = {row['handle']: row for row in classes}
    if set(by_class) != {'1:1', *(class_id(t) for t in tasks)}:
        raise ValueError('Shared HTB class tree differs')
    parent = by_class['1:1']
    if any(parent['options'].get(key) != PARENT_BPS // 8 for key in ('rate', 'ceil')):
        raise ValueError('Shared HTB parent is not 20 Mbit/s')
    shares, telemetry = {}, {}
    leaves = {row.get('parent'): row for row in qdiscs if not row.get('root')}
    concrete_filters = [row for row in filters if row.get('options', {}).get('classid')]
    if len(concrete_filters) != len(tasks) or len(leaves) != len(tasks):
        raise ValueError('Shared classifiers or leaf queues differ')
    for task in tasks:
        key, address = class_id(task), f'10.203.{task["slot"]}.2'
        row, leaf = by_class[key], leaves.get(key, {})
        if (row.get('parent') != '1:1' or row['kind'] != 'htb' or row['options'].get('ceil') != PARENT_BPS // 8
                or leaf.get('kind') != 'pfifo' or leaf.get('options', {}).get('limit') != 256):
            raise ValueError('Task class/ceiling/queue differs')
        matching = [f for f in concrete_filters if f['options']['classid'] == key and f.get('protocol') == 'ip'
                    and f.get('kind') == 'flower' and
                    f['options'].get('keys', {}).get('src_ip' if direction == 'upload' else 'dst_ip') in (address, address + '/32')]
        if len(matching) != 1:
            raise ValueError('Task original-IP classifier differs')
        rate = row['options']['rate']
        if type(rate) is not int or rate * 8 * 10_000 % PARENT_BPS:
            raise ValueError('Unexpected task guaranteed rate')
        shares[task['id']] = rate * 8 * 10_000 // PARENT_BPS
        telemetry[task['id']] = {direction + '_bytes': row.get('bytes', 0),
                                  direction + '_drops': row.get('drops', 0),
                                  direction + '_backlog': leaf.get('backlog', 0)}
    settings = {**NETWORK_LIMITS, 'shares': shares}
    validate_settings(tasks, settings)
    return settings, telemetry
