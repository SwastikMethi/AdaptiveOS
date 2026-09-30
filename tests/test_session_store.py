import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from adaptive_os.session_store import SessionStore


class SessionStoreTest(unittest.TestCase):
    def test_recordings_are_batched_and_exports_preserve_raw_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.sqlite3'
            session_id, workload_id = str(uuid.uuid4()), str(uuid.uuid4())
            store = SessionStore(path)
            store.begin(session_id, {'image_id': 'sha256:test'})
            store.configure(session_id, {'id': workload_id, 'name': 'Periodic task', 'preset': 'periodic', 'config': {}})
            for index in range(3):
                store.record(session_id, {'v': 2, 'sequence': index, 'kind': 'sample',
                             'workload_id': workload_id, 'mono_ns': (index + 1) * 1_000_000_000,
                             'cpu_pct': None if index == 0 else 50, 'rss_mib': 12}, index + 100)
            other = sqlite3.connect(path)
            self.assertEqual(other.execute('select count(*) from messages').fetchone()[0], 0)
            store.flush()
            self.assertEqual(other.execute('select count(*) from messages').fetchone()[0], 3)
            other.close()
            store.record(session_id, {'kind': 'raw', 'sequence': 3, 'mono_ns': 4_000_000_000,
                         'workload_id': workload_id, 'data': {'ticks': [{'skipped': True}]}}, 104)
            store.finish(session_id, 'completed')
            loaded = store.load(session_id)
            self.assertEqual(loaded['session']['status'], 'completed')
            self.assertEqual(len(loaded['samples']), 3)
            self.assertIsNone(loaded['samples'][0]['cpu_pct'])
            store.export(session_id, Path(directory) / 'recording.json', 'json')
            exported = json.loads((Path(directory) / 'recording.json').read_text())
            self.assertEqual(len(exported['messages']), 4)
            self.assertTrue(exported['messages'][3]['data']['ticks'][0]['skipped'])
            store.export(session_id, Path(directory) / 'recording.csv', 'csv')
            with (Path(directory) / 'recording.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]['cpu_pct'], '')
            self.assertEqual(json.loads(rows[-1]['payload'])['kind'], 'raw')
            store.close()

    def test_interrupted_sessions_and_history_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.sqlite3'
            session_id, workload_id = str(uuid.uuid4()), str(uuid.uuid4())
            store = SessionStore(path)
            store.begin(session_id, {})
            for index in range(1000):
                store.record(session_id, {'kind': 'sample', 'sequence': index,
                             'workload_id': workload_id, 'mono_ns': index * 1_000_000_000,
                             'cpu_pct': 10}, index)
            store.close()
            store = SessionStore(path)
            self.assertEqual(store.sessions()[0]['status'], 'interrupted')
            history = store.load(session_id)
            self.assertLessEqual(len(history['samples']), 300)
            self.assertEqual(history['samples'][-1]['sequence'], 999)
            with self.assertRaises(ValueError):
                store.record(session_id, {'kind': 'sample', 'cpu_pct': float('nan')}, 0)
            with self.assertRaises(sqlite3.IntegrityError):
                store.record(str(uuid.uuid4()), {'kind': 'event'}, 0)
            store.close()


if __name__ == '__main__':
    unittest.main()
