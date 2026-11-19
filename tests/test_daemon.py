"""Collector lock results must not starve or outlive their freshness window."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from oma_usage.daemon import Daemon
from oma_usage.store import utcnow


class LockDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {key: temporary.name + '/' + key for key in
                                      ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR')})
        env.start()
        self.addCleanup(env.stop)
        self.collector = Daemon()
        self.addCleanup(self.collector.close)
        self.collector.config['collection_enabled'] = True

    def test_lock_reply_is_processed_under_continuous_default_priority_work(self):
        daemon = self.collector
        glib = daemon.GLib
        daemon.lock_pending = True

        def busy():
            if not daemon.lock_pending:
                daemon.loop.quit()
            return True

        def stop():
            daemon.loop.quit()
            return False

        # Emulate a continuously readable source such as a busy D-Bus connection.
        competing = glib.idle_add(busy, priority=glib.PRIORITY_DEFAULT)
        deadline = glib.timeout_add(1500, stop)
        try:
            daemon._submit('lock', lambda: True)
            daemon.loop.run()
            unlocked = daemon.lock_unlocked
        finally:
            for source in (competing, deadline):
                if glib.MainContext.default().find_source_by_id(source):
                    glib.source_remove(source)
            # Drain any starved completion before closing the test's collector.
            while glib.MainContext.default().pending():
                glib.MainContext.default().iteration(False)
        self.assertIs(unlocked, True)

    def test_delayed_unlocked_reply_does_not_restore_focus_coverage(self):
        daemon = self.collector
        daemon._worker_done('lock', True, None, time.monotonic() - 8)
        daemon._status(utcnow())
        state = daemon.store.conn.execute(
            "SELECT state, error_code FROM collector_status WHERE source='app_focus'").fetchone()
        self.assertEqual(tuple(state), ('unavailable', 'lock_state_unknown'))
