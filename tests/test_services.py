import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from oma_usage.services import MANAGER, MANAGER_PATH, UNIT, _BusMonitor


class Variant:
    def __init__(self, *args):
        self.value = args[-1]

    def unpack(self):
        return self.value


class Store:
    def __init__(self):
        self.conn = self
        self.seconds = {}
        self.counts = {}
        self.states = {}

    def entity(self, _kind, key):
        return key

    def execute(self, _statement, parameters):
        entity, active, sub = parameters
        self.states[entity] = (active, sub)

    def status(self, *_args):
        pass

    def coverage(self, *_args):
        pass

    def usage(self, entity, _source, start, end):
        self.seconds[entity] = self.seconds.get(entity, 0) + (end - start).total_seconds()

    def observe(self, *_args):
        pass

    def add_usage(self, entity, _source, _day, count):
        self.counts[entity] = self.counts.get(entity, 0) + count


class Connection:
    def __init__(self):
        self.calls = []

    def call(self, *args):
        self.calls.append(args)

    def call_finish(self, result):
        return result

    def signal_unsubscribe(self, _subscription):
        pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.t = 0
        self.store = Store()
        glib = SimpleNamespace(Variant=Variant, VariantType=SimpleNamespace(new=lambda value: value),
                               Error=Exception)
        gio = SimpleNamespace(DBusCallFlags=SimpleNamespace(NONE=0))
        self.owner = SimpleNamespace(store=self.store, enabled=lambda: True, closed=False,
                                     Gio=gio, GLib=glib)
        self.bus = _BusMonitor(self.owner, 'user', None)
        self.connection = Connection()
        connection = patch.object(self.bus, 'connection', self.connection)
        connection.start()
        self.addCleanup(connection.stop)
        self.bus.last_sync = 0
        self.clock = patch('oma_usage.services._clock', side_effect=self.point)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def point(self):
        return self.now + timedelta(seconds=self.t), self.t, self.t

    def tick(self, t):
        self.t = t
        self.bus.tick(*self.point())

    def row(self, name='sample.service', path='/unit/sample', active='inactive', sub='dead'):
        return (name, '', 'loaded', active, sub, '', path, 0, '', '/')

    def listed(self, *rows):
        self.bus._listed((rows,), None)

    def signal(self, callback, path, payload):
        callback(None, None, path, None, None, Variant(payload))

    def new(self, name='sample.service', path='/unit/sample'):
        self.signal(self.bus._unit_new, MANAGER_PATH, (name, path))

    def remove(self, name='sample.service', path='/unit/sample'):
        self.signal(self.bus._unit_removed, MANAGER_PATH, (name, path))

    def properties(self, changed, invalidated=(), path='/unit/sample'):
        self.signal(self.bus._properties, path, (UNIT, changed, invalidated))

    def test_known_transitions_and_invalidated_state(self):
        self.listed(self.row())
        self.tick(0)
        self.t = 2
        self.properties({'ActiveState': 'active', 'SubState': 'running'})
        self.t = 4
        self.properties({}, ['SubState'])
        self.t = 7
        self.properties({'SubState': 'running'})
        self.t = 9
        self.properties({'ActiveState': 'inactive', 'SubState': 'dead'})
        self.t = 10
        self.properties({'ActiveState': 'active', 'SubState': 'running'})
        self.tick(15)
        self.assertEqual(self.store.seconds, {'user:sample.service': 9})
        self.assertEqual(self.store.counts, {'user:sample.service': 2})
        self.assertEqual(self.connection.calls, [])

    def test_snapshot_race_and_noisy_unit_lifecycle_never_query_units(self):
        self.listed(self.row(active='active', sub='running'))
        self.tick(0)
        self.bus._resync()
        call = self.connection.calls[-1]
        self.assertEqual(call[1:4], (MANAGER_PATH, MANAGER, 'ListUnitsByPatterns'))
        self.t = 1
        self.remove()
        for index in range(100):
            path = '/unit/transient' + str(index)
            self.new('transient.service', path)
            self.properties({'ActiveState': 'active', 'SubState': 'running'}, path=path)
            self.remove('transient.service', path)
        # A stale snapshot must not resurrect the removed unit.
        call[-1](self.connection, Variant(((self.row(active='active', sub='running'),),)))
        self.tick(5)
        self.assertNotIn('/unit/sample', self.bus.states)
        self.assertEqual(len(self.connection.calls), 1)
        self.assertEqual(self.store.seconds.get('user:sample.service'), 1)
        self.assertNotIn('user:sample.service', self.store.counts)
        self.assertNotIn('user:transient.service', self.store.counts)

    def test_partial_new_and_snapshot_recovery_are_baselines(self):
        self.listed()
        self.tick(0)
        self.t = 1
        self.new()
        self.properties({'SubState': 'running'})
        self.tick(5)
        self.assertEqual(self.store.seconds, {})
        self.t = 6
        self.properties({'ActiveState': 'active'})
        self.tick(11)
        self.assertEqual(self.store.seconds, {'user:sample.service': 5})
        self.assertEqual(self.store.counts, {})
        self.t = 12
        self.properties({'SubState': 'INVALID'})
        self.tick(16)
        self.assertEqual(self.store.seconds['user:sample.service'], 6)
        self.assertEqual(self.store.states['user:sample.service'], ('unknown', 'unknown'))
        self.bus._resync()
        call = self.connection.calls[-1]
        self.t = 17
        call[-1](self.connection, Variant(((self.row(active='active', sub='running'),),)))
        self.tick(22)
        self.assertEqual(self.store.seconds['user:sample.service'], 11)
        self.assertEqual(self.store.counts, {})

    def test_generation_guard_and_both_scopes(self):
        self.bus._resync()
        call = self.connection.calls[-1]
        self.bus._detach()
        call[-1](None, Variant(((self.row(),),)))
        self.assertFalse(self.bus.ready)
        system = _BusMonitor(self.owner, 'system', None)
        connection = patch.object(system, 'connection', Connection())
        connection.start()
        self.addCleanup(connection.stop)
        system.last_sync = 0
        system._listed(((self.row(),),), None)
        system.tick(*self.point())
        self.t = 1
        self.signal(system._properties, '/unit/sample',
                    (UNIT, {'ActiveState': 'active', 'SubState': 'running'}, ()))
        self.t = 6
        system.tick(*self.point())
        self.assertEqual(self.store.counts, {'system:sample.service': 1})
        self.assertEqual(self.store.seconds, {'system:sample.service': 5})

    def test_names_and_states_are_sanitized(self):
        self.listed(self.row(name='bad\n.service'), self.row(name='okay.service', path='/unit/okay', sub='BAD'))
        self.tick(0)
        self.assertNotIn('/unit/sample', self.bus.states)
        self.assertEqual(self.bus.states['/unit/okay'], ('okay.service', 'inactive', None))
        self.t = 1
        self.properties({'SubState': 'running'}, path='/unit/okay')
        self.tick(6)
        self.assertEqual(self.store.counts, {})
        self.assertEqual(self.store.seconds, {'user:okay.service': 5})
