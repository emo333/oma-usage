import unittest
from unittest.mock import patch

from oma_usage import dashboard_reports as reports


def command(index):
    return {'kind': 'command', 'key': f'cmd-{index:03}', 'label': f'cmd-{index:03}',
            'usage_state': 'Observed', 'count': index + 1, 'seconds': 0,
            'undated_count': 0, 'installed': 1, 'owner_package_id': None}


def snapshot():
    rows = [command(i) for i in range(95)]
    empty = {'rows': [], 'most': [], 'least': [], 'zero': [], 'metric': 'seconds', 'definition': 'No evidence'}
    report = {'apps': empty, 'services': empty,
              'commands': {'rows': rows, 'most': list(reversed(rows)), 'least': rows, 'zero': [],
                           'metric': 'count', 'definition': 'Mentions'},
              'package_review': {'inventory': [], 'candidates': []},
              'source_coverage': [], 'inventory': {'errors': {}, 'at': None,
                                                    'ownership_complete': False, 'commands_complete': False},
              'period': {'start_at': 'start', 'end_at': 'end'}, 'categories': [],
              'command_use_cases': [], 'boundaries': 'Local only'}
    return {'report': report, 'errors': [], 'observed_comparisons': [], 'all_comparisons': []}


class DashboardReportTests(unittest.TestCase):
    def test_pagination_search_and_stable_detail_identity(self):
        with patch.object(reports, 'load_snapshot', return_value=snapshot()):
            ranked = reports.page({'page': 'commands'})['sections'][-1]
            self.assertEqual(ranked['items'][0]['label'], 'cmd-094')
            self.assertEqual(len(ranked['options']), 1)
            page = reports.page({'page': 'commands', 'offset': 40, 'filters': {'mode': 'all'}})
            section = page['sections'][-1]
            self.assertEqual(len(section['items']), 40)
            self.assertEqual(section['items'][0]['action']['request'],
                             {'op': 'entity_detail', 'kind': 'command', 'key': 'cmd-040'})
            self.assertEqual(section['buttons'][-1]['request']['offset'], 80)
            filtered = reports.page({'page': 'commands', 'filters': {'search': 'CMD-09'}})['sections'][-1]
            self.assertEqual(len(filtered['items']), 5)
            self.assertEqual(filtered['buttons'][0]['request']['key'], 'search')
            detail = reports.detail({'op': 'entity_detail', 'kind': 'command', 'key': 'cmd-040'})
            self.assertIn('cmd-040', detail['title'])

    def test_overview_highlights_are_positive_and_separate(self):
        with patch.object(reports, 'load_snapshot', return_value=snapshot()):
            overview = reports.page({'page': 'overview'})
        self.assertEqual(len(overview['highlights']), 1)
        self.assertEqual(overview['highlights'][0]['label'], 'Dated command mentions')
        self.assertEqual(overview['highlights'][0]['value'], '4,560 mentions')

    def test_first_run_links_to_settings(self):
        data = snapshot()
        data['report'] = None
        with patch.object(reports, 'load_snapshot', return_value=data):
            overview = reports.page({'page': 'overview'})
        self.assertNotIn('highlights', overview)
        self.assertEqual(overview['sections'][0]['buttons'][0]['request'], {'op': 'page', 'page': 'settings'})

    def test_healthy_coverage_has_no_false_error(self):
        data = snapshot()
        data['report']['inventory']['errors'] = {'commands': None}
        data['report']['source_coverage'] = [{
            'source': 'focus', 'state': 'ok', 'error_code': None, 'first_coverage_at': None,
            'last_success_at': None, 'covered_seconds': 120, 'observations': 1,
            'qualifying_days': 0, 'gaps': [], 'definition': 'Availability'}]
        with patch.object(reports, 'load_snapshot', return_value=data):
            overview = reports.page({'page': 'overview'})
            detail = reports.detail({'op': 'coverage_detail', 'source': 'focus'})
        self.assertNotIn('Inventory commands', overview['status'])
        self.assertNotIn('Error code', [item['label'] for item in detail['sections'][0]['items']])
        self.assertIn('(120 s)', next(item['value'] for item in detail['sections'][0]['items'] if item['label'] == 'Availability'))

    def test_catalog_comparison_has_only_explicit_preview_actions(self):
        data = snapshot()
        data['all_comparisons'] = [{**reports.recommendations.catalog()[0],
                                    'installation': 'Installation status unknown',
                                    'usage_state': 'Insufficient coverage',
                                    'undated_count': None, 'source_observed': None}]
        row = data['all_comparisons'][0]
        with patch.object(reports, 'load_snapshot', return_value=data):
            detail = reports.detail({'op': 'comparison_detail', 'id': row['id']})
        requests = [button['request'] for button in detail['sections'][0]['buttons']]
        self.assertEqual(requests[0], {'op': 'link_preview', 'url': row['source_url']})
        self.assertEqual(requests[1]['op'], 'research_preview')
        self.assertEqual(requests[1]['tool'], row['source']['package'])


if __name__ == '__main__':
    unittest.main()
