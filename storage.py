"""Small persistent SQLite store for the ICAI alert dashboard.

Only completed successful snapshots mark a batch as observed. New-batch alerts
are queued in the database before delivery and retried after network failures.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.setup()

    def db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    def setup(self):
        with self.db() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS batches (
                    key TEXT PRIMARY KEY,
                    course TEXT NOT NULL,
                    region TEXT NOT NULL,
                    pou TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_scan_id INTEGER NOT NULL,
                    is_baseline INTEGER NOT NULL DEFAULT 0,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_last_seen ON batches(last_scan_id);
            ''')

    def get_setting(self, key: str) -> str | None:
        with self.db() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row['value'] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.db() as conn:
            conn.execute('''INSERT INTO settings(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, value))

    def event(self, kind: str, title: str, details: str = ''):
        with self.db() as conn:
            conn.execute('INSERT INTO events(at,kind,title,details) VALUES(?,?,?,?)',
                         (now(),kind,title,details[:500]))

    def scan_start(self) -> int:
        with self.db() as conn:
            cur = conn.execute('INSERT INTO checks(started_at,status) VALUES(?,?)', (now(), 'running'))
            return cur.lastrowid

    def scan_failed(self, scan_id: int, error: str):
        with self.db() as conn:
            conn.execute('UPDATE checks SET finished_at=?, status=?, message=? WHERE id=?',
                         (now(), 'error', error[:1200], scan_id))
            conn.execute('INSERT INTO events(at,kind,title,details) VALUES(?,?,?,?)',
                         (now(), 'error', 'ICAI check failed', error[:500]))

    def scan_success(self, scan_id: int, batches: list[dict[str, Any]], key_fn) -> int:
        """Persist a full snapshot in one transaction; new rows become notification queue."""
        timestamp = now()
        seen_in_this_scan: set[str] = set()
        with self.db() as conn:
            is_first = conn.execute('SELECT COUNT(*) FROM checks WHERE status=?', ('ok',)).fetchone()[0] == 0
            new_count = 0
            for b in batches:
                key = key_fn(b['course'], b)
                if key in seen_in_this_scan:
                    continue
                seen_in_this_scan.add(key)
                record = (b['course'], b['region'], b['pou'],
                          json.dumps(b['fields'], ensure_ascii=False),
                          json.dumps(b['links'], ensure_ascii=False),timestamp,scan_id,key)
                existing = conn.execute('SELECT 1 FROM batches WHERE key=?', (key,)).fetchone()
                if existing:
                    conn.execute('''UPDATE batches SET course=?, region=?, pou=?,
                        fields_json=?, links_json=?, last_seen=?, last_scan_id=? WHERE key=?''', record)
                else:
                    conn.execute('''INSERT INTO batches (
                        key,course,region,pou,fields_json,links_json,first_seen,last_seen,
                        last_scan_id,is_baseline,notified) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (key,b['course'],b['region'],b['pou'],
                         json.dumps(b['fields'], ensure_ascii=False),
                         json.dumps(b['links'], ensure_ascii=False),timestamp,timestamp,scan_id,
                         int(is_first),int(is_first)))
                    if not is_first:
                        new_count += 1
                        conn.execute('INSERT INTO events(at,kind,title,details) VALUES(?,?,?,?)',
                                     (timestamp,'new',f"New batch: {b['course']}",key))
            conn.execute('''UPDATE checks SET finished_at=?, status='ok', total=?, new_count=?,
                message=? WHERE id=?''',
                (timestamp, len(seen_in_this_scan),new_count,
                 ('First successful scan — existing batches saved as baseline' if is_first else 'Scan complete'),scan_id))
            if is_first:
                conn.execute('INSERT INTO events(at,kind,title,details) VALUES(?,?,?,?)',
                             (timestamp,'success','Baseline created',f'{len(seen_in_this_scan)} existing batches'))
            return new_count

    def pending_alerts(self, limit: int = 30) -> list[dict]:
        with self.db() as conn:
            rows = conn.execute('''SELECT * FROM batches WHERE notified=0 AND is_baseline=0
                                ORDER BY first_seen ASC LIMIT ?''', (limit,)).fetchall()
            return [self._batch(row) for row in rows]

    def mark_notified(self, key: str):
        with self.db() as conn:
            conn.execute('UPDATE batches SET notified=1 WHERE key=?', (key,))
            conn.execute('INSERT INTO events(at,kind,title,details) VALUES(?,?,?,?)',
                         (now(),'sent','Telegram alert sent',key))

    @staticmethod
    def _batch(row):
        out = dict(row)
        out['fields'] = json.loads(out.pop('fields_json'))
        out['links'] = json.loads(out.pop('links_json'))
        out['is_baseline'] = bool(out['is_baseline'])
        out['notified'] = bool(out['notified'])
        return out

    def snapshot(self) -> dict:
        with self.db() as conn:
            most_recent = conn.execute('SELECT * FROM checks ORDER BY id DESC LIMIT 1').fetchone()
            last_good = conn.execute('SELECT id,finished_at FROM checks WHERE status=? ORDER BY id DESC LIMIT 1', ('ok',)).fetchone()
            counts = conn.execute('''SELECT COUNT(*) AS total,
                                    SUM(CASE WHEN is_baseline=0 THEN 1 ELSE 0 END) AS new_total,
                                    SUM(CASE WHEN notified=0 AND is_baseline=0 THEN 1 ELSE 0 END) AS pending
                                    FROM batches''').fetchone()
            rows = conn.execute('SELECT * FROM batches ORDER BY first_seen DESC,course LIMIT 250').fetchall()
            events = conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT 35').fetchall()
            return {
                'last_check': dict(most_recent) if most_recent else None,
                'last_success': dict(last_good) if last_good else None,
                'stats': {'known':counts['total'] or 0,'new':counts['new_total'] or 0,
                          'pending':counts['pending'] or 0},
                'batches':[{**self._batch(row), 'currently_listed': bool(last_good and row['last_scan_id']==last_good['id'])} for row in rows],
                'events':[dict(row) for row in events],
            }
