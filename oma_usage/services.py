"""Read-only systemd monitors. D-Bus callbacks cache; only tick writes evidence.

Signals are registered before Subscribe/ListUnitsByPatterns. A reconciliation is
an observation baseline, never evidence of starts missed while disconnected.
"""
from __future__ import annotations

from collections import deque
import time
import unicodedata

from .accounting import ServiceAccounting
from .store import utcnow

DESTINATION = 'org.freedesktop.systemd1'
MANAGER_PATH = '/org/freedesktop/systemd1'
MANAGER = 'org.freedesktop.systemd1.Manager'
UNIT = 'org.freedesktop.systemd1.Unit'
PROPERTIES = 'org.freedesktop.DBus.Properties'
CALL_TIMEOUT_MS = 5000
RETRY_SECONDS = 15
RESYNC_SECONDS = 60
MAX_PENDING = 8192


def _unit_name(value):
    return (isinstance(value, str) and value.endswith('.service')
            and not value.endswith('@.service') and '/' not in value
            and len(value.encode('utf-8')) <= 255
            and not any(unicodedata.category(c).startswith('C') for c in value))


def _state(value):
    # Persist systemd state tokens, not arbitrary D-Bus payloads.
    return (isinstance(value, str) and 0 < len(value) <= 64
            and all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in value))


def _clock():
    return utcnow(), time.monotonic(), time.clock_gettime(time.CLOCK_BOOTTIME)


class _BusMonitor:
    def __init__(self, owner, scope, bus_type):
        self.owner = owner
        self.scope = scope
        self.bus_type = bus_type
        self.source = 'service_' + scope
        self.accounting = ServiceAccounting(owner.store, self.source)
        self.connection = None
        self.closed_handler = None
        self.subscriptions = []
        self.cancellable = None
        self.generation = 0
        self.connecting = False
        self.ready = False
        self.error = None
        self.retry_at = 0.0
        self.last_sync = float('-inf')
        self.last_flush = float('-inf')
        self.listing = False
        self.states = {}  # object path -> (unit name, ActiveState, SubState)
        self.overlay = {}
        self.pending_properties = {}
        self.revisions = {}
        self.pending_names = {}
        self.applied_paths = {}
        self.events = deque()
        self.was_enabled = False

    def _valid(self, generation):
        return not self.owner.closed and generation == self.generation

    def _enqueue(self, kind, payload):
        if not self.owner.enabled():
            # No private transaction can be touched by asynchronous callbacks.
            self.events.clear()
            return
        if len(self.events) >= MAX_PENDING:
            self._fail('service_event_overflow')
            return
        self.events.append((kind, payload, _clock()))

    def connect(self):
        if self.connecting or self.connection is not None or self.owner.closed:
            return
        Gio = self.owner.Gio
        self.connecting = True
        self.error = None
        self.cancellable = Gio.Cancellable()
        generation = self.generation

        def connected(_source, result):
            try:
                connection = Gio.bus_get_finish(result)
            except self.owner.GLib.Error as exc:
                if self._valid(generation):
                    self._fail(self._error_code(exc, 'service_bus_unavailable'))
                return
            if not self._valid(generation):
                return
            self.connecting = False
            self.connection = connection
            self.closed_handler = connection.connect('closed', self._disconnected)
            self._subscribe_signals()
            self._call(MANAGER_PATH, MANAGER, 'Subscribe', None, '()', self._subscribed)

        Gio.bus_get(self.bus_type, self.cancellable, connected)

    def _error_code(self, error, default):
        remote = self.owner.Gio.DBusError.get_remote_error(error)
        if remote in ('org.freedesktop.DBus.Error.AccessDenied',
                      'org.freedesktop.DBus.Error.AuthFailed',
                      'org.freedesktop.systemd1.AccessDenied'):
            return 'service_access_denied'
        return default

    def _call(self, path, interface, method, parameters, reply_type, callback):
        if self.connection is None:
            return
        generation = self.generation
        Gio, GLib = self.owner.Gio, self.owner.GLib

        def finished(connection, result):
            try:
                value = connection.call_finish(result).unpack()
                error = None
            except GLib.Error as exc:
                value, error = None, exc
            if self._valid(generation):
                callback(value, error)

        self.connection.call(DESTINATION, path, interface, method, parameters,
                             GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE,
                             CALL_TIMEOUT_MS, self.cancellable, finished)

    def _subscribe_signals(self):
        Gio = self.owner.Gio
        connection = self.connection
        for interface, member, path, arg0, callback, sender in (
            (MANAGER, 'UnitNew', MANAGER_PATH, None, self._unit_new, DESTINATION),
            (MANAGER, 'UnitRemoved', MANAGER_PATH, None, self._unit_removed, DESTINATION),
            (PROPERTIES, 'PropertiesChanged', None, UNIT, self._properties, DESTINATION),
            ('org.freedesktop.DBus', 'NameOwnerChanged', '/org/freedesktop/DBus',
             DESTINATION, self._owner_changed, 'org.freedesktop.DBus'),
        ):
            self.subscriptions.append(connection.signal_subscribe(
                sender, interface, member, path, arg0, Gio.DBusSignalFlags.NONE,
                callback))

    def _subscribed(self, _value, error):
        # Inventory may share Gio's cached bus connection and subscription.
        if error and self.owner.Gio.DBusError.get_remote_error(error) != 'org.freedesktop.systemd1.AlreadySubscribed':
            self._fail(self._error_code(error, 'service_subscribe_failed'))
            return
        self._resync()

    def _resync(self):
        if self.listing or self.connection is None:
            return
        self.listing = True
        self.overlay = {}
        self.pending_properties = {}
        self.last_sync = time.monotonic()
        parameters = self.owner.GLib.Variant('(asas)', ([], ['*.service']))
        self._call(MANAGER_PATH, MANAGER, 'ListUnitsByPatterns', parameters,
                   '(a(ssssssouso))', self._listed)

    def _listed(self, value, error):
        self.listing = False
        if error:
            self._fail(self._error_code(error, 'service_snapshot_failed'))
            return
        states = {}
        for row in value[0]:
            name, _description, _load, active, sub, _following, path, *_job = row
            if _unit_name(name) and _state(active) and _state(sub):
                states[path] = (name, active, sub)
        states.update(self.overlay)
        for path, (changed, invalidated) in self.pending_properties.items():
            row = states.get(path)
            if row is not None:
                active = changed.get('ActiveState', row[1])
                sub = changed.get('SubState', row[2])
                if _state(active) and _state(sub):
                    states[path] = (row[0], active, sub)
        self.states = {path: row for path, row in states.items() if row is not None}
        self.overlay = {}
        self.ready = True
        self.error = None
        self._enqueue('baseline', self.states.copy())
        for path, (_changed, invalidated) in self.pending_properties.items():
            if invalidated and path in self.states:
                self._fetch(path, self.states[path][0])
        self.pending_properties = {}

    def _change(self, path, row):
        if self.listing:
            self.overlay[path] = row
        if row is None:
            self.states.pop(path, None)
        else:
            self.states[path] = row
        if self.ready:
            self._enqueue('change', (path, row))

    def _unit_new(self, _connection, _sender, _path, _interface, _signal, parameters):
        name, path = parameters.unpack()
        if _unit_name(name):
            self.revisions[path] = self.revisions.get(path, 0) + 1
            self.pending_properties.pop(path, None)
            self.pending_names[path] = name
            self._fetch(path, name)

    def _unit_removed(self, _connection, _sender, _path, _interface, _signal, parameters):
        name, path = parameters.unpack()
        if _unit_name(name):
            self.revisions[path] = self.revisions.get(path, 0) + 1
            self.pending_properties.pop(path, None)
            self.pending_names.pop(path, None)
            if path in self.states:
                self._change(path, (name, 'inactive', 'dead'))
            self._change(path, None)

    def _fetch(self, path, name):
        # The callback only applies if no newer signal/removal superseded it.
        revision = self.revisions.get(path, 0)
        def fetched(value, error):
            if self.revisions.get(path, 0) != revision:
                return
            if error:
                # Vanishing units during an asynchronous GetAll are normal.
                remote = self.owner.Gio.DBusError.get_remote_error(error)
                if remote in ('org.freedesktop.DBus.Error.UnknownObject',
                              'org.freedesktop.systemd1.NoSuchUnit'):
                    return
                self._fail(self._error_code(error, 'service_properties_failed'))
                return
            properties = value[0]
            active, sub = properties.get('ActiveState'), properties.get('SubState')
            if _state(active) and _state(sub):
                self.pending_names.pop(path, None)
                self._change(path, (name, active, sub))
        self._call(path, PROPERTIES, 'GetAll', self.owner.GLib.Variant('(s)', (UNIT,)),
                   '(a{sv})', fetched)

    def _properties(self, _connection, _sender, path, _interface, _signal, parameters):
        interface, changed, invalidated = parameters.unpack()
        if interface != UNIT:
            return
        changed = {key: value for key, value in changed.items()
                   if key in ('ActiveState', 'SubState') and _state(value)}
        invalidated = [key for key in invalidated if key in ('ActiveState', 'SubState')]
        if not changed and not invalidated:
            return
        self.revisions[path] = self.revisions.get(path, 0) + 1
        if self.listing:
            pending, pending_invalidated = self.pending_properties.get(path, ({}, []))
            pending.update(changed)
            self.pending_properties[path] = (pending, list(set(pending_invalidated + invalidated)))
        old = self.states.get(path)
        if old is None:
            if path in self.pending_names:
                self._fetch(path, self.pending_names[path])
            return
        if 'ActiveState' in invalidated or 'SubState' in invalidated:
            self._fetch(path, old[0])
            return
        active, sub = changed.get('ActiveState', old[1]), changed.get('SubState', old[2])
        if _state(active) and _state(sub) and (active, sub) != old[1:]:
            self._change(path, (old[0], active, sub))

    def _owner_changed(self, _connection, _sender, _path, _interface, _signal, parameters):
        _name, old, new = parameters.unpack()
        if old != new:
            self._fail('service_manager_restarted')

    def _disconnected(self, _connection, _remote, _error):
        self._fail('service_bus_disconnected')

    def _detach(self):
        self.generation += 1
        if self.cancellable:
            self.cancellable.cancel()
        if self.connection:
            for subscription in self.subscriptions:
                self.connection.signal_unsubscribe(subscription)
            if self.closed_handler:
                self.connection.disconnect(self.closed_handler)
        # Gio.bus_get connections may also be used by inventory. Never close them.
        self.connection = None
        self.closed_handler = None
        self.subscriptions.clear()
        self.connecting = self.ready = self.listing = False
        self.states.clear()
        self.overlay.clear()
        self.pending_names.clear()
        self.pending_properties.clear()
        self.revisions.clear()
        self.applied_paths.clear()
        self.events.clear()
        self.accounting.reset()

    def _fail(self, code):
        self._detach()
        self.error = code
        self.retry_at = time.monotonic() + RETRY_SECONDS

    def reset(self):
        self.accounting.reset()
        self.events.clear()
        self.was_enabled = False
        self.last_flush = float('-inf')

    def _details(self, rows):
        for name, active, sub in rows:
            entity = self.owner.store.entity('service', self.scope + ':' + name)
            self.owner.store.conn.execute(
                'INSERT INTO service_details(entity_id,unit_file_state,active_state,sub_state) '
                'VALUES(?,\'unknown\',?,?) ON CONFLICT(entity_id) DO UPDATE SET '
                'active_state=excluded.active_state,sub_state=excluded.sub_state',
                (entity, active, sub))

    def _account(self, rows, clock):
        self.accounting.update({self.scope + ':' + row[0]: row[2]
                                for row in rows.values()}, *clock)

    def tick(self, now, mono, boot):
        if not self.owner.enabled():
            self.reset()
            return
        if not self.was_enabled:
            self.reset()
            self.was_enabled = True
            # Never replay queued transitions or time gathered while paused.
            if self.ready:
                self._account(self.states, (now, mono, boot))
                self._details(self.states.values())
                self.applied_paths = {p: self.scope + ':' + r[0] for p, r in self.states.items()}
                self.last_flush = mono
        if self.connection is None and not self.connecting and mono >= self.retry_at:
            self.connect()
        if self.ready:
            while self.events:
                kind, payload, clock = self.events.popleft()
                if kind == 'baseline':
                    # Close observed time before replacing state; reconciliation
                    # cannot establish that an unseen transition was a start.
                    if self.accounting.previous is not None:
                        self.accounting.update(self.accounting.states, *clock)
                    self.accounting.reset()
                    self._account(payload, clock)
                    self._details(payload.values())
                else:
                    path, row = payload
                    states = self.accounting.states.copy()
                    if row is not None:
                        key = self.scope + ':' + row[0]
                        states[key] = row[2]
                        self._details((row,))
                    else:
                        # Object paths are not stored. Decode removal identity
                        # from the event's transient path-to-name map below.
                        key = self.applied_paths.get(path)
                        if key is not None:
                            states.pop(key, None)
                    self.accounting.update(states, *clock)
                if kind == 'baseline':
                    self.applied_paths = {p: self.scope + ':' + r[0] for p, r in payload.items()}
                elif payload[1] is not None:
                    self.applied_paths[payload[0]] = self.scope + ':' + payload[1][0]
                else:
                    self.applied_paths.pop(payload[0], None)
                self.last_flush = clock[1]
            if mono - self.last_flush >= 5:
                self.accounting.update(self.accounting.states, now, mono, boot)
                self._details(self.states.values())
                self.last_flush = mono
            if mono - self.last_sync >= RESYNC_SECONDS:
                self._resync()
        state = 'collecting' if self.ready else ('unavailable' if self.error else 'starting')
        self.owner.store.status(self.source, state, now, self.error)


class ServiceMonitor:
    """Main-loop-only monitor; daemon owns commits, pauses and store lifetime.

    Call tick every second, reset at pause/import boundaries, and close before
    closing the store. Neither start, reset, close nor any D-Bus callback writes
    SQLite. tick is also a no-op writer while enabled_callable returns false.
    """
    def __init__(self, store, enabled_callable):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.store = store
        self.enabled = enabled_callable
        self.closed = False
        self.started = False
        self.buses = [_BusMonitor(self, 'user', Gio.BusType.SESSION),
                      _BusMonitor(self, 'system', Gio.BusType.SYSTEM)]
        for bus in self.buses:
            bus.applied_paths = {}

    def start(self):
        if not self.closed and not self.started:
            self.started = True
            for bus in self.buses:
                bus.connect()

    def tick(self, now, mono, boot):
        if self.closed or not self.started:
            return
        for bus in self.buses:
            bus.tick(now, mono, boot)

    def reset(self):
        for bus in self.buses:
            bus.reset()

    def close(self):
        if not self.closed:
            self.closed = True
            for bus in self.buses:
                bus._detach()
