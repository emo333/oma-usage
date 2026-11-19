"""Consent boundaries and settings for the native dashboard bridge."""
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from oma_usage import client, dashboard_actions as actions, dashboard_bridge, recommendations, store


class DashboardActionTests(unittest.TestCase):
    def test_settings_are_not_applied_until_explicit_action(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(store, 'paths', return_value={'data': Path(tmp)}), \
             patch.object(store, 'load_config', return_value=store.DEFAULTS.copy()), \
             patch.object(client, 'observer_settings', return_value=store.DEFAULTS.copy()), \
             patch.object(actions.setup, 'status', return_value={'stages': {}}), \
             patch.object(client, 'request', return_value={'ok': True}) as send:
            page = actions.settings_page({'filters': {'retention_days': '90'}})
            send.assert_not_called()
            options = page['sections'][1]['options']
            self.assertEqual(options[0]['selected'], '365')
            actions.handle({'op': 'apply_settings', 'filters': {'retention_days': '90', 'idle_seconds': '120'}})
            self.assertEqual(send.call_args.args[0], {'op': 'control', 'action': 'set_settings', 'settings': {'retention_days': 90, 'stale_days': 90, 'idle_seconds': 120}})

    def test_history_source_prefers_saved_unless_reset_or_selected(self):
        saved = {'history_file': '/saved/history'}
        with patch.object(actions.setup, 'status', return_value=saved):
            self.assertEqual(actions.selected_history({}), '/saved/history')
            self.assertIsNone(actions.selected_history({'filters': {'history_file': ''}}))
            self.assertEqual(actions.selected_history({'filters': {'history_file': '/chosen'}}), '/chosen')

    def test_import_failure_reports_safe_actionable_reason(self):
        notice = actions.result_notice('History', {'ok': False, 'error': 'history_unreadable'})
        self.assertIn('could not be read', notice['notice'])
        notice = actions.result_notice('History', {'ok': False, 'error': 'private/path'})
        self.assertNotIn('private/path', notice['notice'])

    def test_setup_preview_binds_exact_source_and_paths(self):
        preview: dict[str, Any] = {'paths': ['/private/config'], 'scopes': ['local'], 'history_file': '/private/history', 'privacy': 'local', 'bar_placement': 'right'}
        with patch.object(actions.setup, 'preview', return_value=preview) as read, \
             patch.object(actions.setup, 'enable_tracking', return_value={'ok': True}) as enable:
            view: Any = actions.handle({'op': 'setup_confirm', 'filters': {'history_file': '/private/history'}})
            enable.assert_not_called()
            request = view['buttons'][0]['request']
            actions.handle(request)
            enable.assert_called_once_with(history_file='/private/history')
            preview['paths'] = ['/different']
            actions.handle(request)
            self.assertEqual(enable.call_count, 1)
            self.assertGreaterEqual(read.call_count, 3)

    def test_research_discloses_exact_query_before_open(self):
        query = recommendations.research_query('example')
        view: Any = actions.handle({'op': 'research_preview', 'tool': 'example'})
        self.assertIn(query['url'], view['subtitle'])
        request = view['buttons'][0]['request']
        self.assertEqual(actions.handle(request), {'url': query['url']})
        request['url'] = 'https://example.com/'
        with self.assertRaises(ValueError):
            actions.handle(request)

    def test_unknown_link_rejected(self):
        with self.assertRaises(ValueError):
            actions.handle({'op': 'link_preview', 'url': 'https://example.com/private'})

    def test_clear_needs_separate_affirmative_action(self):
        with patch.object(client, 'request', return_value={'ok': True}) as send:
            preview: Any = actions.handle({'op': 'clear_preview'})
            send.assert_not_called()
            actions.handle(preview['buttons'][0]['request'])
            self.assertEqual(send.call_args.args[0]['action'], 'clear_history')

    def test_removal_requires_matching_preview_and_closes_on_success(self):
        preview: dict[str, Any] = {'ok': True, 'paths': ['/backend'], 'service_links': [], 'bash_startup': None,
                   'service': 'Stop owned service', 'scopes': ['Remove backend'], 'retained': ['/data'],
                   'next_step': 'Remove plugin separately.'}
        with patch.object(actions.uninstall, 'preview', return_value=preview), \
             patch.object(actions.uninstall, 'remove_backend', return_value={
                 'ok': True, 'next_step': 'Remove plugin separately.'}) as remove:
            view: Any = actions.handle({'op': 'remove_preview'})
            remove.assert_not_called()
            request = view['buttons'][0]['request']
            preview['paths'] = ['/different']
            actions.handle(request)
            remove.assert_not_called()
            preview['paths'] = ['/backend']
            result: Any = actions.handle(request)
            self.assertTrue(result['close'])
            remove.assert_called_once()

    def test_bridge_routes_pages_and_details(self):
        with patch('oma_usage.dashboard_reports.page', return_value={'title': 'page'}) as page, \
             patch('oma_usage.dashboard_reports.detail', return_value={'title': 'detail'}) as detail:
            self.assertEqual(dashboard_bridge.dispatch({'op': 'page', 'page': 'apps'}), {'title': 'page'})
            self.assertEqual(dashboard_bridge.dispatch({'op': 'report_detail'}), {'title': 'detail'})
            page.assert_called_once()
            detail.assert_called_once()
