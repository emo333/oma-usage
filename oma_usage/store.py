"""Private, single-writer evidence storage. All timestamps and days are UTC."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone, timedelta

UTC = timezone.utc
DEFAULTS = dict(collection_enabled=False, retention_days=365, stale_days=90, idle_seconds=60)
CHOICES = dict(retention_days=(90, 365), stale_days=(30, 90, 180), idle_seconds=(60, 120, 300))
SOURCES = ('app_focus', 'bash_live', 'bash_import', 'service_user', 'service_system')


def stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def utcnow() -> datetime:
    return datetime.now(UTC)


def paths() -> dict[str, Path]:
    home = Path.home()
    result = {'data': Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'oma-usage',
              'config': Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'oma-usage'}
    if os.environ.get('XDG_RUNTIME_DIR'):
        result['runtime'] = Path(os.environ['XDG_RUNTIME_DIR']) / 'oma-usage'
    return result


def socket_path() -> Path:
    if 'runtime' not in paths():
        raise RuntimeError('XDG_RUNTIME_DIR is required for the local collector socket')
    return paths()['runtime'] / 'collector.sock'


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError('Private directory must be owned by the current user')
    path.chmod(0o700)


def validate_settings(settings: dict, *, partial=False) -> dict:
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS):
        raise ValueError('invalid_settings')
    if not partial and set(settings) != set(DEFAULTS):
        raise ValueError('invalid_settings')
    for key, value in settings.items():
        if key == 'collection_enabled':
            if type(value) is not bool:
                raise ValueError('invalid_settings')
        elif type(value) is not int or value not in CHOICES[key]:
            raise ValueError('invalid_settings')
    return settings


def load_config() -> dict:
    path = paths()['config'] / 'config.json'
    if not path.exists():
        return DEFAULTS.copy()
    try:
        return validate_settings(json.loads(path.read_text()))
    except (ValueError, OSError) as exc:
        raise RuntimeError('Configuration is malformed or unreadable; repair config.json before starting') from exc


def save_config(settings: dict) -> None:
    validate_settings(settings)
    folder = paths()['config']
    private_dir(folder)
    target = folder / 'config.json'
    temporary = folder / 'config.json.new'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(settings, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    target.chmod(0o600)


SCHEMA = """
CREATE TABLE entities(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('app','command','service','package')),
 key TEXT NOT NULL, label TEXT NOT NULL, owner_package_id INTEGER REFERENCES entities(id),
 category TEXT NOT NULL DEFAULT 'Other', installed INTEGER NOT NULL DEFAULT 0,
 explicit_install INTEGER NOT NULL DEFAULT 0, first_inventory_at TEXT, present_since TEXT,
 first_observed_at TEXT, last_observed_at TEXT, UNIQUE(kind,key));
CREATE TABLE daily_usage(entity_id INTEGER REFERENCES entities(id),day TEXT,source TEXT,
 count INTEGER NOT NULL DEFAULT 0,seconds REAL NOT NULL DEFAULT 0,PRIMARY KEY(entity_id,day,source));
CREATE TABLE undated_history(entity_id INTEGER PRIMARY KEY REFERENCES entities(id),count INTEGER NOT NULL);
CREATE TABLE source_days(day TEXT,source TEXT,covered_seconds REAL NOT NULL DEFAULT 0,
 observations INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(day,source));
CREATE TABLE coverage_intervals(source TEXT,start_at TEXT,end_at TEXT);
CREATE INDEX coverage_source_time ON coverage_intervals(source,start_at,end_at);
CREATE TABLE collector_status(source TEXT PRIMARY KEY,state TEXT NOT NULL,last_success_at TEXT,error_code TEXT);
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE package_details(entity_id INTEGER PRIMARY KEY REFERENCES entities(id),version TEXT,
 installed_size_bytes INTEGER,install_update_at TEXT,required_by_json TEXT,optional_for_json TEXT);
CREATE TABLE service_details(entity_id INTEGER PRIMARY KEY REFERENCES entities(id),unit_file_state TEXT,
 active_state TEXT,sub_state TEXT);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self):
        os.umask(0o077)
        folder = paths()['data']
        private_dir(folder)
        self.lock = open(folder / 'writer.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise RuntimeError('Another collector owns the database writer lock') from exc
        self._connected = False
        try:
            path = folder / 'usage.sqlite3'
            if path.is_symlink():
                raise RuntimeError('Database must not be a symbolic link')
            self.conn = sqlite3.connect(path, timeout=5)
            self._connected = True
            path.chmod(0o600)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA foreign_keys=ON')
            self.conn.execute('PRAGMA busy_timeout=5000')
            version = self.conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('Database schema is newer than this application; upgrade oma-usage')
            if version == 0:
                if self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise RuntimeError('Unrecognized database schema; preserve and inspect the database')
                self.conn.executescript(SCHEMA)
            if self.conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database is corrupt; preserve it and restore a known-good backup')
            self.conn.execute('PRAGMA journal_mode=WAL')
        except Exception:
            self.close()
            raise

    def close(self):
        if self._connected:
            self.conn.close()
            self._connected = False
        if not self.lock.closed:
            self.lock.close()

    def entity(self, kind: str, key: str, *, label=None) -> int:
        self.conn.execute('INSERT INTO entities(kind,key,label) VALUES(?,?,?) ON CONFLICT(kind,key) DO NOTHING',
                          (kind, key, label or key))
        return self.conn.execute('SELECT id FROM entities WHERE kind=? AND key=?', (kind, key)).fetchone()[0]

    def observe(self, entity_id: int, first: datetime, last: datetime):
        a, b = stamp(first), stamp(last)
        self.conn.execute('UPDATE entities SET first_observed_at=CASE WHEN first_observed_at IS NULL OR first_observed_at>? THEN ? ELSE first_observed_at END,last_observed_at=CASE WHEN last_observed_at IS NULL OR last_observed_at<? THEN ? ELSE last_observed_at END WHERE id=?', (a, a, b, b, entity_id))

    def add_usage(self, entity_id: int, source: str, day: str, *, count: int = 0, seconds: float = 0):
        self.conn.execute('INSERT INTO daily_usage VALUES(?,?,?,?,?) ON CONFLICT(entity_id,day,source) DO UPDATE SET count=count+excluded.count,seconds=seconds+excluded.seconds', (entity_id, day, source, count, seconds))

    def usage(self, entity_id: int, source: str, start: datetime, end: datetime, *, count=0):
        self.observe(entity_id, start, end)
        first = True
        for left, right in split_days(start, end):
            self.add_usage(entity_id, source, left.date().isoformat(), count=count if first else 0,
                           seconds=(right-left).total_seconds())
            first = False

    def coverage(self, source: str, start: datetime, end: datetime, *, observations=1):
        a, b = stamp(start), stamp(end)
        self.conn.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value>excluded.value', ('coverage_started:'+source,a))
        previous = self.conn.execute('SELECT rowid,end_at FROM coverage_intervals WHERE source=? ORDER BY rowid DESC LIMIT 1', (source,)).fetchone()
        if previous and previous['end_at'] == a and end > start:
            self.conn.execute('UPDATE coverage_intervals SET end_at=? WHERE rowid=?', (b, previous['rowid']))
        else:
            self.conn.execute('INSERT INTO coverage_intervals VALUES(?,?,?)', (source, a, b))
        first = True
        for left, right in split_days(start, end):
            self.conn.execute('INSERT INTO source_days VALUES(?,?,?,?) ON CONFLICT(day,source) DO UPDATE SET covered_seconds=covered_seconds+excluded.covered_seconds,observations=observations+excluded.observations', (left.date().isoformat(), source, (right-left).total_seconds(), observations if first else 0))
            first = False

    def status(self, source: str, state: str, now: datetime, error_code=None):
        self.conn.execute('INSERT INTO collector_status VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET state=excluded.state,last_success_at=CASE WHEN excluded.state="collecting" THEN excluded.last_success_at ELSE collector_status.last_success_at END,error_code=excluded.error_code', (source, state, stamp(now) if state == 'collecting' else None, error_code))

    def metadata(self, key: str, value=None):
        if value is None:
            row = self.conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
        self.conn.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def prune(self, now: datetime, retention_days: int):
        cutoff = (now.astimezone(UTC).date() - timedelta(days=retention_days-1)).isoformat()
        for table in ('daily_usage', 'source_days'):
            self.conn.execute(f'DELETE FROM {table} WHERE day<?', (cutoff,))
        boundary = cutoff + 'T00:00:00+00:00'
        self.conn.execute('DELETE FROM coverage_intervals WHERE end_at<?', (boundary,))
        self.conn.execute('UPDATE coverage_intervals SET start_at=? WHERE start_at<?', (boundary, boundary))

    def clear_history(self):
        for table in ('daily_usage', 'undated_history', 'source_days', 'coverage_intervals'):
            self.conn.execute(f'DELETE FROM {table}')
        self.conn.execute('UPDATE entities SET first_observed_at=NULL,last_observed_at=NULL')
        self.conn.execute("DELETE FROM metadata WHERE key LIKE 'coverage_started:%'")


def split_days(start: datetime, end: datetime):
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start == end:
        yield start, end
    while start < end:
        midnight = datetime.combine(start.date()+timedelta(days=1), datetime.min.time(), UTC)
        right = min(end, midnight)
        yield start, right
        start = right


def readonly() -> sqlite3.Connection:
    path = paths()['data'] / 'usage.sqlite3'
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    if conn.execute('PRAGMA user_version').fetchone()[0] != 1:
        conn.close()
        raise RuntimeError('Unsupported database schema')
    return conn
