"""Fault an exact app-owned Jev helper and observe the existing native controller.

The operator starts Auto: Jev through the UI. No key is read, no network settings
are changed, and this script never sends sandbox controls or changes app state.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import uuid


def owned_helper(app_pid):
    result = subprocess.run(['/bin/ps', '-Ao', 'pid=,ppid=,comm='], capture_output=True, text=True, check=True)
    matches = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[1] == str(app_pid) and fields[2].endswith('/Contents/Helpers/model_helper/model_helper'):
            matches.append(int(fields[0]))
    assert len(matches) == 1, 'Expected one exact app-owned helper'
    return matches[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--app-pid', type=int, required=True)
    parser.add_argument('--fault', choices=('timeout', 'late', 'disconnect'), required=True)
    args = parser.parse_args()
    directory = Path('output/native-jev-failures') / uuid.uuid4().hex
    directory.mkdir(parents=True)
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    context = db.execute("SELECT payload FROM messages WHERE session_id=? AND kind='decision_context' ORDER BY id DESC LIMIT 1",
                         (args.session,)).fetchone()
    assert context, 'Start Auto: Jev and wait for a model decision first'
    context = json.loads(context[0])
    assert context['requested'] == context['effective'] == 'jev' and not context['shadow']
    helper = owned_helper(args.app_pid)
    foreground = context['binding']['foreground'][0]
    backgrounds = {pair[0] for pair in context['binding']['processes']}
    selected = backgrounds | {foreground}
    last_id = db.execute('SELECT max(id) FROM messages WHERE session_id=?', (args.session,)).fetchone()[0]
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    started = time.monotonic()
    events = []
    report = {'session_id': args.session, 'fault': args.fault, 'helper_pid': helper,
              'mechanism': ('SIGSTOP owned helper; resume after deadline' if args.fault == 'late' else
                            'SIGSTOP owned helper' if args.fault == 'timeout' else
                            'SIGTERM owned helper (pipe disconnection)'),
              'lease_seconds': 10, 'app_pid': args.app_pid}
    try:
        os.kill(helper, signal.SIGTERM if args.fault == 'disconnect' else signal.SIGSTOP)
        resumed = False
        while time.monotonic() - started < 20:
            for row_id, receipt, payload in db.execute('SELECT id,receipt_ns,payload FROM messages WHERE session_id=? AND id>? ORDER BY id',
                                                     (args.session, last_id)).fetchall():
                last_id = row_id
                events.append({**json.loads(payload), 'host_receipt_ns': receipt})
            latched = [item for item in events if item['kind'] == 'policy_fallback' and item['latched']]
            if args.fault == 'late' and not resumed and any(
                    item['kind'] == 'policy_fallback' and item['reason'] == 'deadline' for item in events):
                assert owned_helper(args.app_pid) == helper, 'Expired helper was replaced'
                os.kill(helper, signal.SIGCONT)
                resumed = True
            recovered = resumed and any(item['kind'] == 'model_proposal' for item in events)
            if (latched or recovered) and time.monotonic() - started >= 12:
                break
            time.sleep(.1)
        assert db.execute('SELECT status FROM sessions WHERE id=?', (args.session,)).fetchone()[0] == 'active'
        failures = [item for item in events if item['kind'] == 'policy_fallback']
        if args.fault == 'late':
            assert len(failures) == 1 and failures[0]['failures'] == 1, failures
            assert not failures[0]['latched'] and failures[0]['effective'] == 'jev', failures
            rejected = [item for item in events if item['kind'] == 'model_rejection'
                        and item['reason'] == 'late_response']
            assert len(rejected) == 1, rejected
            accepted = [item for item in events if item['kind'] == 'model_proposal']
            assert accepted and all(item['latency_ms'] < 1000 for item in accepted)
            assert all(item['request_id'] != rejected[0]['request_id'] for item in accepted)
            assert owned_helper(args.app_pid) == helper, 'Recovery must reuse the same helper'
            report.update(late_reply_rejected=True, recovered_proposals=len(accepted),
                          recovered_with_same_helper=True)
        else:
            assert len(failures) == 2 and [item['failures'] for item in failures] == [1, 2], failures
            assert failures[-1]['latched'] and failures[-1]['effective'] == 'rule'
        if args.fault in ('timeout', 'late'):
            assert failures[0]['reason'] == 'deadline', failures
        else:
            assert failures[0]['reason'] in ('helper_failed', 'helper_unavailable'), failures
        samples = {key: [item['mono_ns'] for item in events if item['kind'] == 'sample' and item.get('workload_id') == key]
                   for key in selected}
        assert all(len(values) >= 8 for values in samples.values())
        max_gap = max((b-a)/1e9 for values in samples.values() for a,b in zip(values, values[1:]))
        assert max_gap < 5
        health = [item for item in events if item['kind'] == 'host_health']
        assert health and max(item['heartbeat_age_ms'] for item in health) < 10000
        commands = [item['command'] for item in events if item['kind'] == 'control' and item['command']['op'] == 'priority']
        assert all(item['workload_id'] in backgrounds for item in commands)
        report.update(passed=True, fallback_reasons=[item['reason'] for item in failures],
                      largest_sample_gap_s=max_gap, max_heartbeat_age_ms=max(item['heartbeat_age_ms'] for item in health),
                      guarded_rule_commands=len(commands))
    except Exception as exc:
        report.update(passed=False, error=str(exc))
        raise
    finally:
        if args.fault in ('timeout', 'late'):
            try:
                if owned_helper(args.app_pid) == helper:
                    os.kill(helper, signal.SIGCONT)
            except (AssertionError, ProcessLookupError):
                pass
        report['elapsed_s'] = time.monotonic() - started
        (directory/'report.json').write_text(json.dumps(report, indent=2))
        (directory/'events.json').write_text(json.dumps(events, separators=(',', ':')))
        awake.terminate()
        awake.wait(timeout=5)
        db.close()
        print(json.dumps(report), flush=True)
        print(directory/'report.json', flush=True)


if __name__ == '__main__':
    main()
