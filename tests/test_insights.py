from contextlib import closing
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from oma_usage.insights import build_report
from oma_usage.store import Store


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ,{'XDG_DATA_HOME':self.temp.name})
        self.env.start()
        self.store = Store()
        self.now = datetime(2026,10,24,12,tzinfo=timezone.utc)
        self.store.metadata('inventory_ownership_complete','true')

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def entity(self,kind,key,present=120,owner=None):
        entity = self.store.entity(kind,key)
        self.store.conn.execute('UPDATE entities SET installed=1,explicit_install=1,present_since=?,first_inventory_at=?,owner_package_id=? WHERE id=?',((self.now-timedelta(days=present)).isoformat(),(self.now-timedelta(days=present)).isoformat(),owner,entity))
        return entity

    def report(self,days=30):
        return build_report(self.store.conn,days=days,now=self.now)

    def coverage(self,source,days=30):
        for offset in range(60,60+days):
            start = self.now-timedelta(days=offset)
            self.store.coverage(source,start,start if source=='bash_live' else start+timedelta(minutes=30))

    def test_metrics_rank_independently_and_zero_needs_overlap(self):
        for key,amount in [('a',600),('b',60),('c',0)]:
            entity = self.entity('app',key)
            self.store.add_usage(entity,'app_focus',self.now.date().isoformat(),seconds=amount,count=1 if amount else 0)
        for key,amount in [('x',10),('y',2),('z',0)]:
            entity = self.entity('command',key)
            self.store.add_usage(entity,'bash_live',self.now.date().isoformat(),count=amount)
        start=self.now-timedelta(hours=1)
        self.store.coverage('app_focus',start,start+timedelta(minutes=30))
        self.store.coverage('bash_live',start,start)
        self.entity('app','new',present=0)
        r=self.report()
        self.assertEqual([x['key'] for x in r['apps']['most']],['a','b'])
        self.assertEqual([x['key'] for x in r['apps']['least']],['b','a'])
        self.assertEqual([x['key'] for x in r['commands']['most']],['x','y'])
        self.assertEqual([x['key'] for x in r['commands']['least']],['y','x'])
        self.assertEqual({x['key']:x['usage_state'] for x in r['apps']['zero']},{'c':'Not observed','new':'Insufficient coverage'})
        self.assertEqual(r['categories'],[{'category':'Other','seconds':660}])

    def test_bash_coverage_is_points_not_all_day(self):
        old=self.entity('command','old')
        self.store.conn.execute('INSERT INTO undated_history VALUES(?,9)',(old,))
        point=self.now-timedelta(hours=2)
        self.store.coverage('bash_live',point,point)
        self.entity('command','new',present=0)
        r=self.report()
        self.assertEqual(r['commands']['most'],[])
        self.assertEqual({x['key']:x['usage_state'] for x in r['commands']['zero']},{'new':'Insufficient coverage','old':'Not observed'})
        self.assertEqual(r['history']['undated_count'],9)
        self.assertIsNone(next(x for x in r['commands']['rows'] if x['key']=='old')['last_observed_at'])

    def candidate(self):
        package=self.entity('package','old-package')
        app=self.entity('app','old.desktop',owner=package)
        observed=self.now-timedelta(days=100)
        self.store.observe(app,observed,observed)
        self.store.add_usage(app,'app_focus',observed.date().isoformat(),count=3,seconds=120)
        self.coverage('app_focus')
        return package,app

    def test_review_requires_old_presence_and_qualifying_days(self):
        package,app=self.candidate()
        candidate=self.report()['package_review']['candidates'][0]
        self.assertEqual(candidate['key'],'old-package')
        self.assertEqual(candidate['coverage_days'],{'app_focus':30})
        self.assertTrue(candidate['few_recorded_uses'])
        self.assertEqual(candidate['retained_focus_sessions'],3)
        self.store.conn.execute("DELETE FROM coverage_intervals WHERE start_at=(SELECT min(start_at) FROM coverage_intervals)")
        self.assertEqual(self.report()['package_review']['candidates'],[])
        self.assertEqual(self.report()['package_review']['inventory'][0]['review_state'],'Insufficient coverage for review')

    def test_recent_app_command_running_service_and_boundary_block_review(self):
        package,app=self.candidate()
        boundary=self.now-timedelta(days=90)
        self.store.observe(app,boundary,boundary)
        self.assertEqual(self.report()['package_review']['candidates'],[])
        self.store.conn.execute('UPDATE entities SET last_observed_at=? WHERE id=?',((self.now-timedelta(days=100)).isoformat(),app))
        command=self.entity('command','tool',owner=package)
        self.coverage('bash_live')
        self.store.observe(command,self.now,self.now)
        self.assertEqual(self.report()['package_review']['candidates'],[])
        self.store.conn.execute('UPDATE entities SET first_observed_at=NULL,last_observed_at=NULL WHERE id=?',(command,))
        self.assertEqual(len(self.report()['package_review']['candidates']),1)
        service=self.entity('service','system:tool.service',owner=package)
        self.store.conn.execute('INSERT INTO service_details VALUES(?,?,?,?)',(service,'enabled','active','running'))
        self.assertEqual(self.report()['package_review']['candidates'],[])

    def test_dependency_ownership_recent_presence_and_unmapped_packages(self):
        package,app=self.candidate()
        self.store.conn.execute('UPDATE entities SET explicit_install=0 WHERE id=?',(package,))
        self.assertEqual(self.report()['package_review']['candidates'],[])
        self.store.conn.execute('UPDATE entities SET explicit_install=1 WHERE id=?',(package,))
        self.store.metadata('inventory_ownership_complete','false')
        self.assertEqual(self.report()['package_review']['candidates'],[])
        self.store.metadata('inventory_ownership_complete','true')
        self.store.conn.execute('UPDATE entities SET present_since=? WHERE id=?',((self.now-timedelta(days=89)).isoformat(),package))
        self.assertEqual(self.report()['package_review']['candidates'],[])
        self.entity('package','no-entry')
        self.assertEqual(next(r for r in self.report()['package_review']['inventory'] if r['key']=='no-entry')['review_state'],'No tracked entry point')

    def test_expired_coverage_cannot_qualify_180_day_review(self):
        self.candidate()
        self.store.metadata('settings',json.dumps({'stale_days':180,'retention_days':90}))
        self.store.prune(self.now,90)
        self.assertEqual(self.report()['package_review']['candidates'],[])

    def test_ordinary_connection_and_disconnected_gaps(self):
        self.entity('app','zero')
        self.store.coverage('app_focus',self.now-timedelta(hours=2),self.now-timedelta(hours=1))
        self.store.coverage('app_focus',self.now-timedelta(minutes=30),self.now)
        self.store.conn.commit()
        with closing(sqlite3.connect(self.temp.name+'/oma-usage/usage.sqlite3')) as conn:
            r=build_report(conn,days=7,now=self.now)
            self.assertIsNone(conn.row_factory)
        coverage=next(x for x in r['source_coverage'] if x['source']=='app_focus')
        self.assertEqual(coverage['covered_seconds'],5400)
        self.assertEqual(len(coverage['gaps']),2)

    def test_service_templates_remain_inventory_not_zero_rankings(self):
        self.entity('service','system:example@.service')
        report=self.report()
        self.assertEqual(report['services']['rows'][0]['usage_state'],'Template (not ranked)')
        self.assertEqual(report['services']['zero'],[])


if __name__=='__main__':
    unittest.main()
