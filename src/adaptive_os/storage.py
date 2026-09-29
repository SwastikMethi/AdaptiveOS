"""One SQLite audit database; each append commits so interrupted runs stay visible."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, policy TEXT NOT NULL, profile TEXT NOT NULL,
                block_index INTEGER NOT NULL, randomization_seed INTEGER NOT NULL,
                started_at_utc TEXT NOT NULL, ended_at_utc TEXT, valid INTEGER NOT NULL DEFAULT 0,
                invalid_reason TEXT, metadata_json TEXT NOT NULL, settings_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (
                run_id TEXT NOT NULL REFERENCES runs(run_id), monotonic_offset_ns INTEGER NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, monotonic_offset_ns, kind));
            CREATE TABLE IF NOT EXISTS decisions (
                run_id TEXT NOT NULL REFERENCES runs(run_id), window_index INTEGER NOT NULL,
                policy_version TEXT NOT NULL, state_json TEXT NOT NULL, proposed_action TEXT NOT NULL,
                applied_action TEXT NOT NULL, reason TEXT NOT NULL, confidence REAL, inference_duration_ms REAL,
                original_shares INTEGER NOT NULL, new_shares INTEGER NOT NULL, docker_result TEXT NOT NULL,
                PRIMARY KEY(run_id, window_index));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                monotonic_offset_ns INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def start_run(self, run_id, policy, profile, block_index, seed, metadata, settings):
        with self.connection:
            self.connection.execute("INSERT INTO runs(run_id,policy,profile,block_index,randomization_seed,"
                                    "started_at_utc,metadata_json,settings_json) VALUES(?,?,?,?,?,?,?,?)",
                                    (run_id, policy, profile, block_index, seed, utc_now(), encode(metadata), encode(settings)))

    def sample(self, run_id, offset_ns, kind, payload):
        with self.connection:
            self.connection.execute("INSERT INTO samples VALUES(?,?,?,?)", (run_id, offset_ns, kind, encode(payload)))

    def event(self, run_id, offset_ns, kind, payload):
        with self.connection:
            self.connection.execute("INSERT INTO events(run_id,monotonic_offset_ns,event_type,payload_json) VALUES(?,?,?,?)",
                                    (run_id, offset_ns, kind, encode(payload)))

    def decision(self, run_id, index, state, *, proposed_action, applied_action, reason,
                 policy_version="default-v1", confidence=None, inference_duration_ms=None,
                 original_shares=1024, new_shares=1024, docker_result="not_changed"):
        with self.connection:
            self.connection.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, index, policy_version, encode(state), proposed_action, applied_action, reason,
                 confidence, inference_duration_ms, original_shares, new_shares, docker_result))

    def finish_run(self, run_id, valid, reason, metadata):
        with self.connection:
            self.connection.execute("UPDATE runs SET ended_at_utc=?,valid=?,invalid_reason=?,metadata_json=? WHERE run_id=?",
                                    (utc_now(), int(valid), reason, encode(metadata), run_id))
