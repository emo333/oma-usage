import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from oma_usage.store import Store
from oma_usage.insights import build_report
from oma_usage.recommendations import build_recommendations, catalog, comparisons_for, research_query


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'XDG_DATA_HOME':self.temp.name})
        self.env.start()
        self.store=Store()
        self.now=datetime(2026,10,24,12,tzinfo=timezone.utc)
        self.store.metadata('inventory_ownership_complete','true')
        self.store.metadata('inventory_commands_complete','true')
        self.store.metadata('inventory_errors','{}')
        self.grep=self.command('grep','grep',count=4)
        self.rg=self.command('rg','ripgrep')
        self.store.coverage('bash_live',self.now,self.now)

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def command(self,name,package,count=0):
        owner=self.store.entity('package',package)
        entity=self.store.entity('command',name)
        for item in (owner,entity):
            self.store.conn.execute('UPDATE entities SET installed=1,present_since=? WHERE id=?',((self.now-timedelta(days=10)).isoformat(),item))
        self.store.conn.execute('UPDATE entities SET owner_package_id=? WHERE id=?',(owner,entity))
        if count:
            self.store.add_usage(entity,'bash_live',self.now.date().isoformat(),count=count)
            self.store.observe(entity,self.now,self.now)
        return entity

    def report(self):
        return build_report(self.store.conn,days=30,now=self.now)

    def test_installed_unobserved_target_is_comparison_not_install_advice(self):
        rows=build_recommendations(self.report())
        self.assertEqual([r['id'] for r in rows],['grep-ripgrep'])
        self.assertEqual(rows[0]['installation'],'Already installed')
        self.assertEqual(rows[0]['usage_state'],'Not observed')
        self.assertEqual(rows[0]['source_url'],'https://github.com/BurntSushi/ripgrep')
        self.assertNotIn('filename-search',[r['use_case'] for r in rows])

    def test_used_targets_remain_and_removal_refreshes_badge(self):
        self.store.add_usage(self.rg,'bash_live',self.now.date().isoformat(),count=2)
        first=build_recommendations(self.report())[0]
        self.assertEqual((first['installation'],first['usage_state']),('Already installed','Observed'))
        self.store.conn.execute("UPDATE entities SET installed=0 WHERE key IN ('rg','ripgrep')")
        removed=build_recommendations(self.report())[0]
        self.assertEqual((removed['installation'],removed['usage_state']),('Not installed','Observed'))
        self.assertEqual(self.store.conn.execute('SELECT count FROM daily_usage WHERE entity_id=?',(self.rg,)).fetchone()[0],2)

    def test_absence_without_complete_discovery_is_unknown(self):
        self.store.conn.execute("UPDATE entities SET installed=0 WHERE key IN ('rg','ripgrep')")
        self.store.metadata('inventory_commands_complete','false')
        self.assertEqual(build_recommendations(self.report())[0]['installation'],'Installation status unknown')

    def test_installed_unobserved_precedes_observed_within_use_case(self):
        extra=copy.deepcopy(catalog()[0])
        extra['id']='second-target'
        extra['target']={'package':'synthetic-target','commands':['synthetic-target']}
        self.command('synthetic-target','synthetic-target',count=1)
        with patch('oma_usage.recommendations.catalog',return_value=[catalog()[0],extra]):
            rows=build_recommendations(self.report())
        self.assertEqual([r['usage_state'] for r in rows],['Not observed','Observed'])

    def test_undated_target_evidence_and_details_without_dated_source(self):
        self.store.conn.execute('INSERT INTO undated_history VALUES(?,7)',(self.rg,))
        self.assertEqual(build_recommendations(self.report())[0]['usage_state'],'Undated history only')
        self.store.conn.execute('DELETE FROM daily_usage')
        report=self.report()
        self.assertEqual(build_recommendations(report),[])
        source=next(r for r in report['commands']['rows'] if r['key']=='grep')
        self.assertEqual([r['id'] for r in comparisons_for(source,report)],['grep-ripgrep'])
        self.assertEqual(comparisons_for({'kind':'command','key':'unsupported','owner_package_id':None},report),[])

    def test_queries_disclose_only_selected_tool_and_catalog_use_case(self):
        result=research_query('grep','text-search')
        self.assertEqual(parse_qs(urlsplit(result['url']).query),{'q':['grep text-search alternatives Linux']})
        self.assertEqual(result['query'],'grep text-search alternatives Linux')
        self.assertEqual(research_query('tool')['query'],'tool alternatives Linux')
        with self.assertRaises(ValueError):
            research_query('grep\nPRIVATE_SENTINEL')
        with self.assertRaises(ValueError):
            research_query('grep','made-up-use-case')

    def test_docker_owned_service_is_source_evidence(self):
        owner=self.store.entity('package','docker')
        service=self.store.entity('service','system:docker.service')
        self.store.conn.execute('UPDATE entities SET installed=1 WHERE id=?',(owner,))
        self.store.conn.execute('UPDATE entities SET installed=1,owner_package_id=? WHERE id=?',(owner,service))
        self.store.add_usage(service,'service_system',self.now.date().isoformat(),seconds=60)
        self.assertIn('docker-podman',[r['id'] for r in build_recommendations(self.report())])


if __name__=='__main__':
    unittest.main()
