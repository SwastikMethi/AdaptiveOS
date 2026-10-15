"""Read-only evidence capture while the bundled app is operated through native UI.

The operator establishes visible/minimized state and starts the policy. This
monitor does not operate the app, mutate recordings, or alter its ten-second lease.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--phase', choices=('visible', 'minimized'), required=True)
    parser.add_argument('--backgrounds', type=int, required=True)
    parser.add_argument('--seconds', type=int, default=1800)
    args = parser.parse_args()
    directory = Path('output/native-soak') / uuid.uuid4().hex
    directory.mkdir(parents=True)
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    current = db.execute("SELECT payload FROM messages WHERE session_id=? AND kind='decision_context' "
                         'ORDER BY id DESC LIMIT 1', (args.session,)).fetchone()
    assert current, 'Start Laya and wait for its first decision before observing'
    context = json.loads(current[0])
    assert context['requested'] == context['effective'] == 'laya' and not context['shadow']
    backgrounds = {pair[0] for pair in context['binding']['processes']}
    foreground = context['binding']['foreground'][0]
    assert len(backgrounds) == args.backgrounds
    selected = backgrounds | {foreground}
    last_id = db.execute('SELECT max(id) FROM messages WHERE session_id=?', (args.session,)).fetchone()[0]
    started = time.monotonic()
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(__import__('os').getpid())])
    events, resources = [], []
    samples = defaultdict(list)
    errors = []
    next_report = started
    report = {'session_id': args.session, 'operator_window_state': args.phase,
              'pid': args.pid, 'backgrounds': args.backgrounds, 'requested_seconds': args.seconds,
              'awake_assertion': 'caffeinate -dimsu', 'lease_seconds': 10}
    try:
        while time.monotonic() - started < args.seconds:
            state = db.execute('SELECT status FROM sessions WHERE id=?', (args.session,)).fetchone()
            if not state or state[0] != 'active':
                raise AssertionError('Session stopped during observation')
            for row_id, receipt, payload in db.execute(
                    'SELECT id,receipt_ns,payload FROM messages WHERE session_id=? AND id>? ORDER BY id',
                    (args.session, last_id)).fetchall():
                last_id = row_id
                item = json.loads(payload)
                kind = item['kind']
                if kind == 'sample' and item.get('workload_id') in selected:
                    samples[item['workload_id']].append(item['mono_ns'])
                elif kind != 'raw':
                    events.append({**item, 'host_receipt_ns': receipt})
                if kind == 'policy_fallback' and item['latched']:
                    errors.append('model latched to Rule: ' + item.get('reason', 'unknown'))
                if kind == 'decision_context' and item['effective'] != 'laya':
                    errors.append('Laya ceased controlling')
                if kind == 'control' and item['command'].get('op') == 'priority':
                    if item['command'].get('workload_id') not in backgrounds:
                        errors.append('priority change outside selected backgrounds')
                if kind == 'host_health' and item.get('heartbeat_age_ms', 0) >= 10000:
                    errors.append('heartbeat lease deadline exceeded')
            if time.monotonic() >= next_report:
                process = subprocess.run(['/bin/ps', '-p', str(args.pid), '-o', 'pid=,pcpu=,rss=,time='],
                                         capture_output=True, text=True, check=True)
                row = {'elapsed_s': round(time.monotonic() - started, 2), 'app_resource_sample': process.stdout.strip(),
                       'sample_counts': {key: len(value) for key, value in samples.items()},
                       'model_responses': sum(item['kind'] == 'model_proposal' for item in events),
                       'errors': list(dict.fromkeys(errors))}
                resources.append(row)
                print(json.dumps(row), flush=True)
                next_report += 30
            if errors:
                raise AssertionError('; '.join(dict.fromkeys(errors)))
            time.sleep(1)
        assert selected == set(samples), 'A selected workload had no samples'
        spans = {key: (value[-1] - value[0]) / 1e9 for key, value in samples.items()}
        gaps = {key: max((b - a) / 1e9 for a, b in zip(value, value[1:])) for key, value in samples.items()}
        assert min(spans.values()) >= args.seconds - 10, spans
        assert max(gaps.values()) < 5, gaps
        proposals = [item for item in events if item['kind'] == 'model_proposal']
        contexts = [item for item in events if item['kind'] == 'decision_context']
        assert len(contexts) >= args.seconds / 5 - 3, len(contexts)
        assert len(proposals) / len(contexts) >= .95, 'Fewer than 95% of requests met the deadline'
        assert all(item['latency_ms'] < 1000 for item in proposals)
        latency = sorted(item['latency_ms'] for item in proposals)
        report.update(passed=True, sample_span_s=spans, largest_sample_gap_s=gaps,
            proposals=len(proposals), actions=dict(Counter(item['proposal']['action'] for item in proposals)),
            contexts=len(contexts), valid_fraction=len(proposals) / len(contexts),
            fallback_reasons=[item['reason'] for item in events if item['kind'] == 'policy_fallback'],
            model_identities=sorted({item['proposal']['model'] for item in proposals}),
            verified_interventions=sum(item['kind'] == 'decision_acknowledgement' for item in events),
            maximum_latency_ms=max(latency), p95_latency_ms=latency[int(.95 * (len(latency) - 1))])
    except Exception as exc:
        report.update(passed=False, error=str(exc))
        raise
    finally:
        report.update(elapsed_s=time.monotonic() - started, resources=resources)
        (directory / 'report.json').write_text(json.dumps(report, indent=2))
        (directory / 'events.json').write_text(json.dumps(events, separators=(',', ':')))
        awake.terminate()
        awake.wait(timeout=5)
        db.close()
        print(directory / 'report.json', flush=True)


if __name__ == '__main__':
    main()
