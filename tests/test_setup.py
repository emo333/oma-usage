"""Tracking setup outcomes with isolated state and controlled platform boundaries."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oma_usage import cli, installer, setup, store


class PluginReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.dict(os.environ, {
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / 'config'),
            'XDG_DATA_HOME': str(self.home / 'data'),
            'XDG_RUNTIME_DIR': str(self.home / 'runtime')}))
        self.responses: list[str | Exception] = ['ok']
        self.enabled = False
        self.placed = False
        self.elapsed = 0.0
        self.requests = 0
        patches.enter_context(patch.object(setup, 'preflight'))
        patches.enter_context(patch.object(setup, '_installed'))
        patches.enter_context(patch.object(setup, '_hook', return_value='pending_new_shell'))
        patches.enter_context(patch.object(cli, 'import_history', return_value={'ok': True, 'status': 'imported'}))
        patches.enter_context(patch.object(setup, 'run_command', side_effect=self.command))
        patches.enter_context(patch.object(setup.client, 'observer_settings', side_effect=lambda **_: self.settings()))
        patches.enter_context(patch.object(setup.client, 'request', side_effect=self.control))
        patches.enter_context(patch.object(setup, '_sources', side_effect=self.sources))
        patches.enter_context(patch.object(setup, 'time', SimpleNamespace(
            monotonic=lambda: self.elapsed, sleep=self.advance)))

    def settings(self):
        return dict(store.DEFAULTS, collection_enabled=self.enabled)

    def advance(self, seconds):
        self.elapsed += seconds
        self.assertLessEqual(self.elapsed, 5.4, 'plugin readiness wait exceeded its bound')

    def command(self, args, **_kwargs):
        if args[:3] == ['omarchy-shell', 'shell', 'putBarWidget']:
            self.requests += 1
            self.assertLess(self.requests, 100, 'plugin readiness wait never completed')
            response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            if isinstance(response, Exception):
                raise response
            if response == 'ok':
                self.placed = True
            return response
        if args == ['omarchy-shell', 'shell', 'rescanPlugins'] or args[0] == 'systemctl':
            return ''
        self.fail('Unexpected platform command: ' + repr(args))

    def control(self, message, **_kwargs):
        action = message['action']
        self.assertIn(action, ('pause', 'resume'))
        if action == 'resume':
            self.assertTrue(self.placed, 'collection resumed before plugin readiness')
        self.enabled = action == 'resume'
        store.save_config(self.settings())
        return {'ok': True}

    def sources(self):
        return {source: {'state': 'collecting' if self.enabled else 'paused'}
                for source in ('app_focus', 'service_user', 'service_system')}

    def run_setup(self):
        result = setup.enable_tracking(history_file=self.home / 'synthetic-history')
        saved = json.loads((store.paths()['config'] / 'setup-state.json').read_text())
        return result, saved

    def assert_paused_failure(self, result, saved):
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'plugin_enable_failed')
        self.assertEqual(result['stages']['plugin'], 'failed')
        self.assertFalse(saved['complete'])
        self.assertFalse(self.enabled)
        self.assertFalse(store.load_config()['collection_enabled'])

    def test_pending_scan_finishes_setup_before_collection_resumes(self):
        self.responses = ['not ready', 'not ready', 'ok']
        result, saved = self.run_setup()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['stages']['plugin'], 'enabled')
        self.assertEqual(result['stages']['collectors'], 'collecting')
        self.assertTrue(saved['complete'])
        self.assertTrue(store.load_config()['collection_enabled'])
        self.assertGreater(self.elapsed, 0)

    def test_scan_that_never_finishes_fails_with_collection_paused(self):
        self.responses = ['not ready']
        result, saved = self.run_setup()
        self.assert_paused_failure(result, saved)
        self.assertGreaterEqual(self.elapsed, 5)

    def test_nontransient_and_ambiguous_failures_do_not_wait_or_resume(self):
        for response in ('unknown', 'invalid placement', installer.InstallError('IPC timed out')):
            with self.subTest(response=str(response)):
                self.responses = [response]
                result, saved = self.run_setup()
                self.assert_paused_failure(result, saved)
                self.assertEqual(self.elapsed, 0)
