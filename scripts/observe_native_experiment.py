"""Observe a UI-started experiment, keep the host awake, and preserve evidence.

Only read-only SQLite access is used against the running app. Full exports use
the production exporter on a disposable database snapshot after observation.
This script never selects policies, starts workloads, or supplies credentials.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import uuid

from adaptive_os.experiments import recompute_recording
from adaptive_os.session_store import SessionStore


def capture(db, session, directory):
    with tempfile.TemporaryDirectory(prefix='recording-snapshot-', dir=directory) as temporary:
        snapshot = Path(temporary) / 'sessions.sqlite3'
        copy = sqlite3.connect(snapshot)
        db.backup(copy)
        copy.close()
        store = SessionStore(snapshot)
        try:
            for extension in ('json', 'csv'):
                store.export(session, directory / f'recording.{extension}', extension)
        finally:
            store.db.close()
    recording = json.loads((directory / 'recording.json').read_text())
    recomputed = recompute_recording(recording)
    (directory / 'recomputed-v2.json').write_text(json.dumps(recomputed, indent=2, allow_nan=False))
    return recording, recomputed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--trials', type=int, required=True)
    parser.add_argument('--timeout', type=int, default=10800)
    args = parser.parse_args()
    directory = Path('output/native-experiments') / uuid.uuid4().hex
    directory.mkdir(parents=True)
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    assert db.execute('SELECT status FROM sessions WHERE id=?', (args.session,)).fetchone(), 'Unknown session'
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-dimsu', '-w', str(os.getpid())])
    started = time.monotonic()
    next_report = started
    report = {'session_id': args.session, 'expected_trials': args.trials,
              'awake_assertion': 'caffeinate -dimsu', 'lease_seconds': 10}
    print(directory, flush=True)
    try:
        while True:
            status = db.execute('SELECT status FROM sessions WHERE id=?', (args.session,)).fetchone()[0]
            if time.monotonic() >= next_report or status != 'active':
                boundaries = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload FROM messages WHERE session_id=? AND kind='trial_boundary' ORDER BY id",
                    (args.session,))]
                ended = [row for row in boundaries if row['boundary'] == 'measurement_end']
                fallbacks = db.execute("SELECT count(*) FROM messages WHERE session_id=? AND kind='policy_fallback'",
                                      (args.session,)).fetchone()[0]
                print(json.dumps({'elapsed_s': round(time.monotonic()-started, 1), 'status': status,
                                  'completed_trials': len(ended), 'fallbacks': fallbacks,
                                  'latest_trial_index': boundaries[-1]['trial_index'] if boundaries else None,
                                  'latest_boundary': boundaries[-1]['boundary'] if boundaries else None}), flush=True)
                next_report += 30
            if status != 'active':
                break
            assert time.monotonic()-started < args.timeout, 'Observation deadline exceeded'
            time.sleep(2)
        recording, recomputed = capture(db, args.session, directory)
        results = recomputed['results']
        assert status == 'completed', f'Session ended with {status}'
        assert len(results) == args.trials, f'Expected {args.trials} trials, received {len(results)}'
        assert [row['trial_index'] for row in results] == list(range(args.trials))
        assert all(row['valid'] for row in results), 'One or more trials have invalid measurements'
        originals = [row['result'] for row in recording['messages']
                     if row['kind'] == 'trial_boundary' and row['boundary'] == 'measurement_end']
        metric_keys = ('valid', 'exclusion_reasons', 'miss_rate', 'p95_lateness_ms',
                       'background_throughput', 'per_task_throughput', 'background_coverage_ns')
        assert all(all(original[key] == recomputed_row[key] for key in metric_keys)
                   for original, recomputed_row in zip(originals, results)), 'Raw recomputation differs from live results'
        heartbeat = [row['heartbeat_age_ms'] for row in recording['messages'] if row['kind'] == 'host_health']
        assert heartbeat and max(heartbeat) < 10000, 'Heartbeat lease exceeded'
        report.update(passed=True, completed_trials=len(results), raw_recomputation_parity=True,
                      max_heartbeat_age_ms=max(heartbeat), arms=dict(Counter(row['arm'] for row in results)),
                      profiles=dict(Counter(row['profile'] for row in results)),
                      fallback_windows=sum(row.get('fallback_windows', 0) for row in results),
                      interventions=sum(row.get('interventions', 0) for row in results))
    except Exception as exc:
        report.update(passed=False, error=str(exc))
        raise
    finally:
        report['elapsed_s'] = time.monotonic()-started
        (directory / 'observation.json').write_text(json.dumps(report, indent=2, allow_nan=False))
        awake.terminate()
        awake.wait(timeout=5)
        db.close()
        print(json.dumps(report), flush=True)
        print(directory / 'observation.json', flush=True)


if __name__ == '__main__':
    main()
