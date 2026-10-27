"""Private GLib collector runtime: the main-loop thread owns every database write."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import os
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import time
import unicodedata

import sys

from .accounting import FocusAccounting
from .store import (Store, load_config, save_config, socket_path, private_dir,
                    validate_settings, utcnow)

MAX_FRAME = 1024 * 1024
UTC = timezone.utc


class ProtocolError(Exception):
    def __init__(self, code='invalid_message'):
        self.code = code


def _fields(obj, required):
    if type(obj) is not dict or set(obj) != set(required):
        raise ProtocolError()


def _identity(value, *, empty=False, command=False):
    if (type(value) is not str or (not value and not empty)
            or len(value.encode('utf-8')) > 255
            or any(unicodedata.category(c).startswith('C') for c in value)
            or (command and ('/' in value or value in ('.', '..')))):
        raise ProtocolError('invalid_identity')
    return value


def _sequence(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ProtocolError('invalid_sequence')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError()
        result[key] = value
    return result


def _timestamp(value):
    if type(value) is not str or len(value) > 40:
        raise ProtocolError('invalid_history_row')
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() != timedelta(0):
            raise ValueError()
        return result.astimezone(UTC)
    except ValueError:
        raise ProtocolError('invalid_history_row') from None


def _lock_query():
    """The installed omarchy-shell help defines target/method positional IPC."""
    try:
        result = subprocess.run(['omarchy-shell', 'lock', 'isLocked'],
                                capture_output=True, timeout=2, check=False)
        if result.returncode == 0 and result.stdout.strip() in (b'true', b'false'):
            return result.stdout.strip() == b'false'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


@dataclass(eq=False)
class Peer:
    sock: socket.socket
    read_watch: int = 0
    write_watch: int = 0
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    observer: bool = False
    observer_id: str | None = None
    import_id: str | None = None
    import_noop: bool = False
    touched: float = field(default_factory=time.monotonic)


class Daemon:
    def __init__(self):
        # Import only GLib: no GTK or display initialization in collectors.
        from gi.repository import GLib
        self.GLib = GLib
        self.config = load_config()
        self.path = socket_path()
        self.store = Store()
        self.focus = FocusAccounting(self.store)
        self.loop = GLib.MainLoop()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='oma-usage')
        self.peers = set()
        self.watches = []
        self.listener = None
        self.socket_identity = None
        self.import_peer = None
        self.pending_inventory = None
        self.inventory = None
        self.monitor = None
        self.lock_unlocked = None
        self.lock_checked = 0.0
        self.lock_pending = False
        self.inventory_pending = False
        self.last_inventory = -900.0
        self.last_lock = -5.0
        self.last_prune = utcnow().date()
        self.closed = False
        self.fatal_error = None
        self.old_signals = {}

    @property
    def enabled(self):
        return self.config['collection_enabled'] and self.import_peer is None

    def _settings(self):
        return dict(op='settings', collection_enabled=self.enabled,
                    idle_seconds=self.config['idle_seconds'])

    def _broadcast(self):
        for peer in tuple(self.peers):
            if peer.observer:
                self._send(peer, self._settings())

    def _reset(self):
        self.focus.reset()
        if self.monitor:
            self.monitor.reset()

    def _status(self, now):
        if self.import_peer:
            return
        if not self.enabled:
            for source in ('app_focus', 'bash_live', 'service_user', 'service_system'):
                self.store.status(source, 'paused', now)
        elif self.lock_unlocked is None or time.monotonic() - self.lock_checked > 7:
            self.store.status('app_focus', 'unavailable', now, 'lock_state_unknown')
        elif self.lock_unlocked is False:
            self.store.status('app_focus', 'paused', now)
        elif (not any(p.observer_id for p in self.peers) or self.focus.previous is None
              or time.monotonic() - self.focus.previous.mono > 15):
            self.store.status('app_focus', 'unavailable', now, 'observer_unavailable')
        else:
            self.store.status('app_focus', 'collecting', self.focus.previous.now)


    def run(self):
        try:
            private_dir(self.path.parent)
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError('Collector socket path is not an owned socket; inspect it before starting')
                # The exclusive Store writer lock proves there is no live writer.
                self.path.unlink()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.setblocking(False)
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            self.socket_identity = self.path.stat().st_ino
            self.listener.listen(16)
            self.watches.append(self.GLib.io_add_watch(
                self.listener.fileno(), self.GLib.IO_IN, self._accept))
            from . import inventory
            from .services import ServiceMonitor
            self.inventory = inventory
            self.monitor = ServiceMonitor(self.store, lambda: self.enabled)
            self.monitor.start()
            self.store.metadata('settings', json.dumps(self.config))
            self.store.prune(utcnow(), self.config['retention_days'])
            self.store.status('bash_live', 'starting' if self.enabled else 'paused', utcnow())
            self._status(utcnow())
            self.store.conn.commit()
            self.watches.append(self.GLib.timeout_add_seconds(1, self._tick))
            for sig in (signal.SIGTERM, signal.SIGINT):
                self.old_signals[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: self.loop.quit())
            self._tick()
            self.loop.run()
            if self.fatal_error:
                raise RuntimeError(self.fatal_error)
        finally:
            self.close()
        return 0

    def _accept(self, _fd, _condition):
        for _ in range(16):
            try:
                conn, _ = self.listener.accept()
            except BlockingIOError:
                break
            conn.setblocking(False)
            credentials = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            if struct.unpack('3i', credentials)[1] != os.getuid() or len(self.peers) >= 64:
                conn.close()
                continue
            peer = Peer(conn)
            self.peers.add(peer)
            peer.read_watch = self.GLib.io_add_watch(
                conn.fileno(), self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR, self._read, peer)
        return True

    def _send(self, peer, message):
        if peer not in self.peers:
            return
        encoded = json.dumps(message, separators=(',', ':')).encode() + b'\n'
        if len(peer.outgoing) + len(encoded) > MAX_FRAME:
            self._drop(peer)
            return
        peer.outgoing.extend(encoded)
        if not peer.write_watch:
            peer.write_watch = self.GLib.io_add_watch(
                peer.sock.fileno(), self.GLib.IO_OUT | self.GLib.IO_ERR | self.GLib.IO_HUP,
                self._write, peer)

    def _write(self, _fd, condition, peer):
        if condition & (self.GLib.IO_ERR | self.GLib.IO_HUP):
            self._drop(peer)
            return False
        try:
            sent = peer.sock.send(peer.outgoing)
            del peer.outgoing[:sent]
        except BlockingIOError:
            return True
        except OSError:
            self._drop(peer)
            return False
        if peer.outgoing:
            return True
        peer.write_watch = 0
        return False

    def _read(self, _fd, condition, peer):
        try:
            data = peer.sock.recv(65536)
            if not data:
                self._drop(peer)
                return False
            peer.touched = time.monotonic()
            peer.incoming.extend(data)
            # Process a bounded number of bytes per main-loop dispatch.
            while b'\n' in peer.incoming:
                end = peer.incoming.index(b'\n')
                if end > MAX_FRAME:
                    self._drop(peer)
                    return False
                frame = bytes(peer.incoming[:end])
                del peer.incoming[:end + 1]
                try:
                    message = json.loads(frame, object_pairs_hook=_pairs,
                                         parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError()))
                    reply = self._dispatch(peer, message)
                    self._send(peer, reply)
                except (ProtocolError, UnicodeError, ValueError, RecursionError) as exc:
                    if peer is self.import_peer:
                        self._abort_import()
                    elif self.import_peer is None:
                        self.store.conn.rollback()
                        if not isinstance(exc, ProtocolError):
                            self._reset()
                    self._send(peer, dict(ok=False, error=exc.code if isinstance(exc, ProtocolError) else 'invalid_message'))
                except (sqlite3.Error, OSError):
                    if peer is self.import_peer:
                        self._abort_import()
                    else:
                        self.store.conn.rollback()
                        self._reset()
                    self._send(peer, dict(ok=False, error='storage_error'))
                except Exception:
                    self.fatal_error = 'Collector integration failed; restart after checking local dependencies'
                    self.loop.quit()
                    return False
                if peer not in self.peers:
                    return False
            if len(peer.incoming) > MAX_FRAME:
                self._drop(peer)
                return False
        except BlockingIOError:
            pass
        except OSError:
            self._drop(peer)
            return False
        if condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
            self._drop(peer)
            return False
        return True

    def _dedup(self, source, identity, seq):
        key = f'sequence:{source}:{identity}'
        previous = self.store.metadata(key)
        if previous is not None and seq <= int(previous):
            raise ProtocolError('duplicate_sequence')
        self.store.metadata(key, seq)

    def _dispatch(self, peer, message):
        if type(message) is not dict or type(message.get('op')) is not str:
            raise ProtocolError()
        op = message['op']
        if op == 'hello':
            _fields(message, ('op', 'role'))
            if message['role'] != 'observer' or peer.observer:
                raise ProtocolError()
            peer.observer = True
            return self._settings()
        if op == 'history_import':
            return self._history(peer, message)
        if self.import_peer:
            raise ProtocolError('import_busy')
        now = utcnow()
        if op == 'control':
            result = self._control(message, now)
        elif op == 'focus':
            _fields(message, ('op', 'observer_id', 'seq', 'app_id', 'idle'))
            identity = _identity(message['observer_id'])
            app = _identity(message['app_id'], empty=True)
            if '/' in app:
                raise ProtocolError('invalid_identity')
            seq = _sequence(message['seq'])
            if not peer.observer or type(message['idle']) is not bool:
                raise ProtocolError()
            if peer.observer_id is not None and peer.observer_id != identity:
                raise ProtocolError('invalid_identity')
            if any(p is not peer and p.observer_id is not None for p in self.peers):
                raise ProtocolError('observer_busy')
            if not self.enabled:
                raise ProtocolError('collection_paused')
            self._dedup('app_focus', identity, seq)
            peer.observer_id = identity
            unlocked = self.lock_unlocked if time.monotonic() - self.lock_checked <= 7 else None
            self.focus.snapshot(identity, seq, app, message['idle'], now, time.monotonic(),
                                time.clock_gettime(time.CLOCK_BOOTTIME), unlocked)
            self._status(now)
            result = dict(ok=True)
        elif op == 'bash':
            _fields(message, ('op', 'shell_id', 'seq', 'names'))
            identity = _identity(message['shell_id'])
            seq = _sequence(message['seq'])
            names = message['names']
            if type(names) is not list or len(names) > 8192:
                raise ProtocolError()
            for name in names:
                _identity(name, command=True)
            if not self.enabled:
                raise ProtocolError('collection_paused')
            self._dedup('bash_live', identity, seq)
            for name in names:
                entity = self.store.entity('command', name)
                self.store.observe(entity, now, now)
                self.store.add_usage(entity, 'bash_live', now.date().isoformat(), count=1)
            self.store.coverage('bash_live', now, now)
            self.store.status('bash_live', 'collecting', now)
            result = dict(ok=True)
        else:
            raise ProtocolError('unknown_operation')
        self.store.conn.commit()
        return result

    def _control(self, message, now):
        action = message.get('action')
        _fields(message, ('op', 'action', 'settings') if action == 'set_settings' else ('op', 'action'))
        updated = self.config.copy()
        if action in ('pause', 'resume'):
            updated['collection_enabled'] = action == 'resume'
        elif action == 'set_settings':
            settings = message['settings']
            if type(settings) is not dict or 'collection_enabled' in settings:
                raise ProtocolError('invalid_settings')
            try:
                validate_settings(settings, partial=True)
            except ValueError:
                raise ProtocolError('invalid_settings') from None
            updated.update(settings)
        elif action == 'clear_history':
            self._reset()
            self.store.clear_history()
            self.store.conn.commit()
            self._status(now)
            return dict(ok=True)
        else:
            raise ProtocolError('invalid_action')
        save_config(updated)
        self.config = updated
        self.store.metadata('settings', json.dumps(self.config))
        self._reset()
        self.store.prune(now, updated['retention_days'])
        self._status(now)
        self._broadcast()
        return dict(ok=True)

    def _history(self, peer, message):
        phase = message.get('phase')
        _fields(message, ('op', 'phase', 'import_id', 'rows') if phase == 'rows' else ('op', 'phase', 'import_id'))
        import_id = _identity(message['import_id'])
        if phase == 'begin':
            if self.import_peer:
                raise ProtocolError('import_busy')
            self.store.conn.commit()
            self._reset()
            self.import_peer = peer
            peer.import_id = import_id
            peer.import_noop = self.store.metadata('history_import_complete') is not None
            self.store.conn.execute('BEGIN IMMEDIATE')
            self._broadcast()
            return dict(ok=True, already_imported=peer.import_noop)
        if self.import_peer is not peer or peer.import_id != import_id:
            raise ProtocolError('invalid_import')
        if phase == 'rows':
            rows = message['rows']
            if type(rows) is not list or len(rows) > 8192:
                raise ProtocolError('invalid_history_row')
            now = utcnow()
            cutoff = now.date() - timedelta(days=self.config['retention_days'] - 1)
            parsed = []
            for row in rows:
                _fields(row, ('name', 'day', 'count', 'first_at', 'last_at'))
                name = _identity(row['name'], command=True)
                count = row['count']
                if type(count) is not int or not 1 <= count <= 2**31 - 1:
                    raise ProtocolError('invalid_history_row')
                if row['day'] is None:
                    if row['first_at'] is not None or row['last_at'] is not None:
                        raise ProtocolError('invalid_history_row')
                    parsed.append((name, count, None, None, None))
                else:
                    try:
                        if type(row['day']) is not str:
                            raise ValueError()
                        day = date.fromisoformat(row['day'])
                        if day.isoformat() != row['day']:
                            raise ValueError()
                    except ValueError:
                        raise ProtocolError('invalid_history_row') from None
                    first, last = _timestamp(row['first_at']), _timestamp(row['last_at'])
                    if first > last or last > now or first.date() != day or last.date() != day:
                        raise ProtocolError('invalid_history_row')
                    parsed.append((name, count, day, first, last))
            if not peer.import_noop:
                for name, count, day, first, last in parsed:
                    entity = self.store.entity('command', name)
                    if day is None:
                        self.store.conn.execute('INSERT INTO undated_history VALUES(?,?) ON CONFLICT(entity_id) DO UPDATE SET count=count+excluded.count', (entity, count))
                    else:
                        self.store.observe(entity, first, last)
                        if day >= cutoff:
                            self.store.add_usage(entity, 'bash_import', day.isoformat(), count=count)
            return dict(ok=True)
        if phase == 'commit':
            noop = peer.import_noop
            if not noop:
                self.store.metadata('history_import_complete', utcnow().isoformat())
            self.store.status('bash_import', 'collecting', utcnow())
            self.store.conn.commit()
            self._finish_import()
            return dict(ok=True, already_imported=noop)
        raise ProtocolError('invalid_import')

    def _finish_import(self):
        peer = self.import_peer
        self.import_peer = None
        if peer:
            peer.import_id = None
            peer.import_noop = False
        self._reset()
        self._broadcast()

    def _abort_import(self):
        self.store.conn.rollback()
        self._finish_import()

    def _drop(self, peer):
        if peer not in self.peers:
            return
        self.peers.remove(peer)
        for watch in (peer.read_watch, peer.write_watch):
            if watch:
                self.GLib.source_remove(watch)
        peer.read_watch = peer.write_watch = 0
        peer.sock.close()
        if peer is self.import_peer:
            self._abort_import()
        if peer.observer_id is not None:
            self.focus.reset()

    def _submit(self, kind, work):
        future = self.executor.submit(work)
        def finished(done):
            try:
                value, error = done.result(), None
            except Exception:
                value, error = None, f'{kind}_unavailable'
            if not self.closed:
                self.GLib.idle_add(self._worker_done, kind, value, error)
        future.add_done_callback(finished)

    def _worker_done(self, kind, value, error):
        if self.closed:
            return False
        if kind == 'lock':
            self.lock_pending = False
            self.lock_unlocked = value if error is None else None
            self.lock_checked = time.monotonic()
            if self.lock_unlocked is not True:
                self.focus.reset()
        else:
            self.inventory_pending = False
            self.pending_inventory = (value, error)
        return False

    def _tick(self):
        try:
            now, mono = utcnow(), time.monotonic()
            if mono - self.last_lock >= 5 and not self.lock_pending:
                self.last_lock, self.lock_pending = mono, True
                self._submit('lock', _lock_query)
            if self.inventory and mono - self.last_inventory >= 900 and not self.inventory_pending:
                self.last_inventory, self.inventory_pending = mono, True
                self._submit('inventory', self.inventory.collect_inventory)
            if self.import_peer:
                if mono - self.import_peer.touched > 30:
                    self._drop(self.import_peer)
                return True
            if self.pending_inventory is not None:
                snapshot, error = self.pending_inventory
                self.pending_inventory = None
                if not error:
                    self.inventory.apply_inventory(self.store, snapshot, now)
                    if snapshot['apps'] is not None:
                        self.focus.resolve = lambda app_id, snapshot=snapshot: self.inventory.resolve_app(self.store, snapshot, app_id)
                    self.store.metadata('inventory_error', '')
                else:
                    self.store.metadata('inventory_error', error)
            if self.monitor:
                self.monitor.tick(now, mono, time.clock_gettime(time.CLOCK_BOOTTIME))
            self._status(now)
            if now.date() != self.last_prune:
                self.store.prune(now, self.config['retention_days'])
                self.last_prune = now.date()
            self.store.conn.commit()
        except Exception:
            # Fail closed and preserve the database; never print captured source data.
            self.fatal_error = 'Collector storage or integration failed; inspect local permissions and restart the collector'
            self.loop.quit()
            return False
        return True

    def close(self):
        if self.closed:
            return
        self.closed = True
        for sig, handler in self.old_signals.items():
            signal.signal(sig, handler)
        for peer in tuple(self.peers):
            self._drop(peer)
        if self.monitor:
            self.monitor.close()
        for watch in self.watches:
            if self.GLib.MainContext.default().find_source_by_id(watch):
                self.GLib.source_remove(watch)
        self.watches.clear()
        if self.listener:
            self.listener.close()
        if self.socket_identity is not None:
            try:
                if self.path.lstat().st_ino == self.socket_identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        self.executor.shutdown(wait=True, cancel_futures=True)
        try:
            if self.fatal_error:
                self.store.conn.rollback()
            else:
                self.store.conn.commit()
        finally:
            self.store.close()


def main():
    """Collector CLI entry, with actionable errors and no captured-source traceback."""
    try:
        return Daemon().run()
    except RuntimeError as exc:
        print(f'oma-usage: {exc}', file=sys.stderr)
    except sqlite3.Error:
        print('oma-usage: Database is unreadable, locked, corrupt, or unwritable; preserve usage.sqlite3 and check its ownership and schema before restarting', file=sys.stderr)
    except OSError:
        print('oma-usage: Cannot access the private collector files or socket; check XDG directories, ownership, permissions, and available disk space', file=sys.stderr)
    except ImportError:
        print('oma-usage: Collector requires system PyGObject and GLib; install native dependencies with omarchy pkg add python-gobject gtk4 libadwaita', file=sys.stderr)
    return 1
