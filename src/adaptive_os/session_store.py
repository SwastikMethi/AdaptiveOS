"""Versioned desktop recordings. One connection, owned by the session worker."""

import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile

from .sandbox.protocol import MAX_WORKLOADS, identifier, validate_config


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_text(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


class SessionStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 2):
            self.db.close()
            raise ValueError('Unsupported recording database version; keep this file and use a compatible app')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, metadata TEXT NOT NULL CHECK(json_valid(metadata)));
            CREATE TABLE IF NOT EXISTS workloads (
                session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
                name TEXT NOT NULL, preset TEXT NOT NULL, config TEXT NOT NULL CHECK(json_valid(config)),
                PRIMARY KEY(session_id,id));
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                sequence INTEGER, kind TEXT NOT NULL, workload_id TEXT, mono_ns INTEGER,
                receipt_ns INTEGER NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)),
                UNIQUE(session_id,sequence));
            CREATE INDEX IF NOT EXISTS session_messages ON messages(session_id,kind,workload_id,mono_ns);
            PRAGMA user_version=2;
        ''')
        self.db.execute("UPDATE sessions SET status='interrupted',ended_at=? WHERE status='active'", (utc_now(),))
        self.flush()

    def begin(self, session_id, metadata):
        self.db.execute('INSERT INTO sessions VALUES (?,?,NULL,?,?)',
                        (identifier(session_id), utc_now(), 'active', json_text(metadata)))
        self.flush()

    def configure(self, session_id, workload):
        name = workload['name'].strip()
        if not name or len(name) > 80:
            raise ValueError('Workload names must contain 1–80 characters')
        config = validate_config(workload['preset'], workload['config'])
        self.db.execute('INSERT OR REPLACE INTO workloads VALUES (?,?,?,?,?)',
                        (identifier(session_id), identifier(workload['id']), name,
                         workload['preset'], json_text(config)))

    def record(self, session_id, message, receipt_ns):
        payload = json_text(message)
        self.db.execute('INSERT INTO messages(session_id,sequence,kind,workload_id,mono_ns,receipt_ns,payload) '
                        'VALUES (?,?,?,?,?,?,?)', (identifier(session_id), message.get('sequence'), message['kind'],
                        message.get('workload_id'), message.get('mono_ns'), receipt_ns, payload))

    def flush(self):
        self.db.commit()

    def finish(self, session_id, status):
        if status not in ('completed', 'failed', 'interrupted'):
            raise ValueError('Invalid terminal session status')
        self.db.execute('UPDATE sessions SET status=?,ended_at=? WHERE id=?', (status, utc_now(), session_id))
        self.flush()

    def sessions(self):
        return [dict(row) for row in self.db.execute(
            'SELECT id,started_at,ended_at,status FROM sessions ORDER BY started_at DESC LIMIT 200')]

    def session(self, session_id):
        row = self.db.execute('SELECT * FROM sessions WHERE id=?', (identifier(session_id),)).fetchone()
        if row is None:
            raise ValueError('Recording not found')
        return {**dict(row), 'metadata': json.loads(row['metadata'])}

    def workloads(self, session_id, limit=-1, offset=0):
        return [{**dict(row), 'config': json.loads(row['config'])} for row in self.db.execute(
            'SELECT * FROM workloads WHERE session_id=? ORDER BY rowid LIMIT ? OFFSET ?', (session_id, limit, offset))]

    def load(self, session_id, workload_offset=0):
        session = self.session(session_id)
        if type(workload_offset) is not int or workload_offset < 0:
            raise ValueError('Invalid workload page')
        total = self.db.execute('SELECT count(*) FROM workloads WHERE session_id=?', (session_id,)).fetchone()[0]
        workload_offset = min(workload_offset, max(0, (total - 1) // MAX_WORKLOADS * MAX_WORKLOADS))
        # Select at most 300 evenly spaced observed points per workload, including
        # the final sample. Raw/full-resolution data remain in messages/exports.
        samples = [json.loads(row[0]) for row in self.db.execute('''
            WITH page AS (SELECT id FROM workloads WHERE session_id=? ORDER BY rowid LIMIT ? OFFSET ?),
            ranked AS (
                SELECT payload,mono_ns,
                  row_number() OVER (PARTITION BY workload_id ORDER BY mono_ns DESC) AS n,
                  count(*) OVER (PARTITION BY workload_id) AS total
                FROM messages WHERE session_id=? AND kind='sample' AND workload_id IN (SELECT id FROM page))
            SELECT payload FROM ranked WHERE (n-1) % max(1, (total+299)/300)=0 ORDER BY mono_ns
        ''', (session_id, MAX_WORKLOADS, workload_offset, session_id))]
        events = [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM (SELECT id,payload FROM messages WHERE session_id=? "
            "AND kind NOT IN ('sample','raw') ORDER BY id DESC LIMIT 500) ORDER BY id", (session_id,))]
        return {'session': session, 'workloads': self.workloads(session_id, MAX_WORKLOADS, workload_offset),
                'workload_offset': workload_offset, 'workload_total': total, 'samples': samples, 'events': events}

    def export(self, session_id, path, format):
        if format not in ('json', 'csv'):
            raise ValueError('Export format must be json or csv')
        self.flush()
        session = self.session(session_id)
        path = Path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent,
                                             prefix='.adaptive-os-export-', delete=False) as stream:
                temporary = Path(stream.name)
                rows = self.db.execute('SELECT payload,receipt_ns FROM messages WHERE session_id=? ORDER BY id', (session_id,))
                if format == 'json':
                    stream.write('{"schema_version":2,"session":' + json_text(session) + ',"workloads":' +
                                 json_text(self.workloads(session_id)) + ',"messages":[')
                    separator = ''
                    for row in rows:
                        value = {**json.loads(row['payload']), 'host_receipt_ns': row['receipt_ns']}
                        stream.write(separator + json_text(value))
                        separator = ','
                    stream.write(']}\n')
                else:
                    fields = ['kind', 'sequence', 'workload_id', 'mono_ns', 'cpu_pct', 'rss_mib',
                              'app_rate', 'app_unit', 'nice', 'read_bytes', 'write_bytes',
                              'miss_rate', 'p95_lateness_ms', 'p95_wakeup_ms', 'warmup']
                    writer = csv.writer(stream)
                    writer.writerow(['session_id', 'host_receipt_ns', *fields, 'payload'])
                    for row in rows:
                        message = json.loads(row['payload'])
                        writer.writerow([session_id, row['receipt_ns'], *[message.get(k) for k in fields], row['payload']])
                stream.flush()
            temporary.replace(path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def close(self):
        try:
            self.flush()
        finally:
            self.db.close()
