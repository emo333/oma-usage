from contextlib import closing
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from oma_usage.store import Store, load_config, readonly
from oma_usage.accounting import FocusAccounting, ServiceAccounting


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {k: self.temp.name+'/'+k for k in ('XDG_DATA_HOME','XDG_CONFIG_HOME','XDG_RUNTIME_DIR')})
        self.env.start()
        self.store = Store()
        self.now = datetime(2026, 1, 1, 23, 59, 55, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def test_focus_transition_midnight_and_gaps(self):
        focus = FocusAccounting(self.store)
        def send(t, app, idle=False, unlocked=True, boot=None):
            focus.snapshot('one', t, app, idle, self.now+timedelta(seconds=t), t, t if boot is None else boot, unlocked)
        send(0, 'A')
        send(5, 'A')
        send(10, 'B')
        send(15, 'B')
        send(20, 'B', idle=True)
        send(25, 'B', unlocked=False)
        send(30, 'B')
        send(35, 'B', boot=50)
        focus.reset()
        send(40, 'B')
        totals = {r['key']: (r['seconds'], r['count']) for r in self.store.conn.execute('SELECT key,sum(seconds) seconds,sum(count) count FROM entities JOIN daily_usage ON id=entity_id GROUP BY id')}
        self.assertEqual(totals, {'app-id:A': (10,1), 'app-id:B': (5,1)})
        rows = list(self.store.conn.execute('SELECT day,seconds FROM daily_usage JOIN entities ON id=entity_id WHERE key=? ORDER BY day', ('app-id:A',)))
        self.assertEqual([tuple(r) for r in rows], [('2026-01-01',5),('2026-01-02',5)])

    def test_unknown_lock_state_cannot_create_review_coverage(self):
        focus = FocusAccounting(self.store)
        for t in (0,5,10):
            focus.snapshot('one',t,'A',False,self.now+timedelta(seconds=t),t,t,None)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM coverage_intervals').fetchone()[0],0)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM daily_usage').fetchone()[0],0)
        focus.reset()
        for t in (20,25):
            focus.snapshot('one',t,'A',True,self.now+timedelta(seconds=t),t,t,True)
        self.assertEqual(self.store.conn.execute('SELECT sum(covered_seconds) FROM source_days').fetchone()[0],5)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM daily_usage').fetchone()[0],0)

    def test_services_baseline_exited_and_scope(self):
        user = ServiceAccounting(self.store,'service_user')
        system = ServiceAccounting(self.store,'service_system')
        for source,key in ((user,'user:sample.service'),(system,'system:sample.service')):
            source.update({key:'running'},self.now,0,0)
            source.update({key:'exited'},self.now+timedelta(seconds=5),5,5)
            source.update({key:'running'},self.now+timedelta(seconds=10),10,10)
            source.update({key:'running'},self.now+timedelta(seconds=15),15,15)
        totals = list(self.store.conn.execute('SELECT key,sum(seconds),sum(count) FROM entities JOIN daily_usage ON id=entity_id GROUP BY id ORDER BY key'))
        self.assertEqual([tuple(r) for r in totals],[('system:sample.service',10,1),('user:sample.service',10,1)])

    def test_private_readonly_single_writer_and_clear(self):
        self.assertFalse(load_config()['collection_enabled'])
        with self.assertRaises(RuntimeError):
            Store()
        entity = self.store.entity('command','git')
        self.store.metadata('history_import_complete','yes')
        self.store.observe(entity,self.now,self.now)
        self.store.add_usage(entity,'bash_live',self.now.date().isoformat(),count=1)
        self.store.conn.commit()
        with closing(readonly()) as conn:
            with self.assertRaises(Exception):
                conn.execute('DELETE FROM entities')
        self.store.clear_history()
        self.assertEqual(self.store.metadata('history_import_complete'),'yes')
        self.assertIsNone(self.store.conn.execute('SELECT first_observed_at FROM entities').fetchone()[0])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM daily_usage').fetchone()[0],0)

    def test_retention_clips_intervals_without_losing_first_last(self):
        entity = self.store.entity('command','git')
        self.store.observe(entity,self.now,self.now)
        self.store.coverage('app_focus',self.now,self.now+timedelta(days=2))
        self.store.add_usage(entity,'bash_import',self.now.date().isoformat(),count=2)
        self.store.prune(self.now+timedelta(days=365),365)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM daily_usage').fetchone()[0],0)
        self.assertEqual(self.store.conn.execute('SELECT start_at FROM coverage_intervals').fetchone()[0],'2026-01-02T00:00:00+00:00')
        self.assertIsNotNone(self.store.conn.execute('SELECT last_observed_at FROM entities').fetchone()[0])

    def test_inventory_updates_removals_and_ambiguous_focus(self):
        from oma_usage.inventory import apply_inventory, resolve_app
        package = dict(version='1',installed_size_bytes=10,install_update_at=None,
                       required_by=[],optional_for=[],explicit_install=True)
        snapshot = dict(packages={'example':package},apps=[
            dict(key='one.desktop',label='One',owner='example',category='Development',
                 wmclass='shared',executable='one'),
            dict(key='two.desktop',label='Two',owner='example',category='Utility',
                 wmclass='shared',executable='two')],
            commands=[],services_user=[],services_system=[],ownership_complete=True,
            commands_complete=True,errors={})
        apply_inventory(self.store,snapshot,self.now)
        self.assertEqual(self.store.conn.execute('SELECT key FROM entities WHERE id=?',
                         (resolve_app(self.store,snapshot,'one'),)).fetchone()[0],'one.desktop')
        self.assertEqual(self.store.conn.execute('SELECT key FROM entities WHERE id=?',
                         (resolve_app(self.store,snapshot,'shared'),)).fetchone()[0],'app-id:shared')
        package['version'] = '2'
        apply_inventory(self.store,snapshot,self.now+timedelta(days=1))
        self.assertEqual(self.store.conn.execute("SELECT present_since FROM entities WHERE kind='package'").fetchone()[0],self.now.isoformat())
        snapshot['packages'] = None
        apply_inventory(self.store,snapshot,self.now+timedelta(days=2))
        self.assertEqual(self.store.conn.execute("SELECT installed FROM entities WHERE kind='package'").fetchone()[0],1)
        snapshot['packages'] = {}
        apply_inventory(self.store,snapshot,self.now+timedelta(days=3))
        snapshot['packages'] = {'example':package}
        apply_inventory(self.store,snapshot,self.now+timedelta(days=4))
        self.assertEqual(self.store.conn.execute("SELECT present_since FROM entities WHERE kind='package'").fetchone()[0],(self.now+timedelta(days=4)).isoformat())

    def test_incomplete_command_inventory_preserves_presence(self):
        from oma_usage.inventory import apply_inventory
        snapshot=dict(packages=None,apps=None,services_user=None,services_system=None,
                      commands=[dict(key='example-tool',label='example-tool',owner=None)],
                      ownership_complete=False,commands_complete=True,errors={})
        apply_inventory(self.store,snapshot,self.now)
        snapshot.update(commands=[],commands_complete=False)
        apply_inventory(self.store,snapshot,self.now+timedelta(days=1))
        row=self.store.conn.execute("SELECT installed,present_since FROM entities WHERE key='example-tool'").fetchone()
        self.assertEqual(tuple(row),(1,self.now.isoformat()))
        snapshot['commands_complete']=True
        apply_inventory(self.store,snapshot,self.now+timedelta(days=2))
        self.assertEqual(self.store.conn.execute("SELECT installed FROM entities WHERE key='example-tool'").fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
