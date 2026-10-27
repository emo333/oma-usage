import hashlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from oma_usage import cli, installer, store, uninstall


class BackendRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(os.environ, {'HOME': str(self.home),
                         'XDG_DATA_HOME': str(self.home / 'data'),
                         'XDG_CONFIG_HOME': str(self.home / 'config'),
                         'XDG_RUNTIME_DIR': str(self.home / 'run')})
        env.start()
        self.addCleanup(env.stop)
        self.target = installer.locations()
        self.commands = []
        commands = patch.object(installer, 'run_command', side_effect=self.command)
        commands.start()
        self.addCleanup(commands.stop)

    def command(self, args, **kwargs):
        self.commands.append(args)
        if 'show' in args:
            return ('FragmentPath=' + str(self.target['unit'])
                    + '\nDropInPaths=\nActiveState=inactive\nLoadState=loaded')
        return ''

    def install_owned(self):
        files = {}
        for name in uninstall.ASSETS:
            path = self.target[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            content = ('owned ' + name + '\n').encode()
            path.write_bytes(content)
            files[str(path)] = hashlib.sha256(content).hexdigest()
        self.target['venv'].mkdir()
        (self.target['venv'] / '.oma-usage-owned').write_text(installer.OWNER)
        self.target['manifest'].write_text(json.dumps({'owner': installer.OWNER, 'files': files, 'venv': True}))
        self.config = dict(store.DEFAULTS, collection_enabled=True, retention_days=90)
        store.save_config(self.config)
        self.database = self.target['data'] / 'usage.sqlite3'
        self.database.write_bytes(b'usage and completed import metadata retained')
        self.bash = self.home / '.bashrc'
        self.prefix, self.suffix = b'# unrelated\nexport A=1\n', b'\n# trailing\xff\n'
        self.block = (b'# >>> oma-usage >>>\nsource ' + shlex.quote(str(self.target['hook'])).encode()
                      + b'\n# <<< oma-usage <<<\n')
        self.bash.write_bytes(self.prefix + self.block + self.suffix)

    def test_preview_is_read_only_and_cleanup_retains_evidence_and_unrelated_bytes(self):
        self.install_owned()
        plugin = self.target['plugin']
        plugin.mkdir(parents=True)
        (plugin / 'keep').write_text('native Git checkout')
        before = {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()}
        self.assertTrue(uninstall.preview()['ok'])
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob('*') if path.is_file()})
        self.assertTrue(all('show' in args for args in self.commands))
        self.assertTrue(uninstall.remove_backend()['ok'])
        self.assertEqual(self.bash.read_bytes(), self.prefix + self.suffix)
        self.assertEqual(self.database.read_bytes(), b'usage and completed import metadata retained')
        self.assertEqual(store.load_config(), dict(self.config, collection_enabled=False))
        self.assertEqual((plugin / 'keep').read_text(), 'native Git checkout')
        self.assertFalse(self.target['venv'].exists())
        self.assertTrue(all(not self.target[key].exists() for key in uninstall.ASSETS))
        self.assertTrue(uninstall.remove_backend()['ok'])

    def test_modified_asset_blocks_every_destructive_step(self):
        self.install_owned()
        self.target['hook'].write_text('user modification')
        original = self.bash.read_bytes()
        result = uninstall.remove_backend()
        self.assertFalse(result['ok'])
        self.assertIn(str(self.target['hook']), result['error'])
        self.assertEqual(self.bash.read_bytes(), original)
        self.assertTrue(self.target['venv'].exists())
        self.assertEqual(store.load_config(), self.config)
        self.assertFalse(self.commands)

    def test_manifest_cannot_authorize_arbitrary_paths(self):
        self.install_owned()
        foreign = self.home / 'personal-file'
        foreign.write_bytes(b'preserve')
        manifest = json.loads(self.target['manifest'].read_text())
        manifest['files'][str(foreign)] = hashlib.sha256(b'preserve').hexdigest()
        self.target['manifest'].write_text(json.dumps(manifest))
        self.assertFalse(uninstall.remove_backend()['ok'])
        self.assertEqual(foreign.read_bytes(), b'preserve')
        self.assertTrue(self.target['launcher'].exists())
        self.assertFalse(self.commands)

    def test_edited_bash_block_prevents_service_or_file_changes(self):
        self.install_owned()
        original = self.bash.read_bytes().replace(b'\nsource ', b'\n# custom source ')
        self.bash.write_bytes(original)
        result = uninstall.remove_backend()
        self.assertFalse(result['ok'])
        self.assertIn('Bash block', result['error'])
        self.assertEqual(self.bash.read_bytes(), original)
        self.assertFalse(self.commands)
        self.assertEqual(store.load_config(), self.config)

    def test_foreign_systemd_override_is_not_disabled(self):
        self.install_owned()
        with patch.object(installer, 'run_command', return_value=(
                'FragmentPath=' + str(self.target['unit']) + '\nDropInPaths=/custom.conf\nActiveState=active\nLoadState=loaded')) as run:
            result = uninstall.remove_backend()
        self.assertFalse(result['ok'])
        self.assertIn('drop-ins', result['error'])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(self.target['launcher'].exists())

    def test_removal_does_not_require_plugin_checkout(self):
        self.install_owned()
        self.assertFalse(self.target['plugin'].exists())
        self.assertTrue(uninstall.remove_backend()['ok'])
        self.assertTrue(self.database.exists())

    def test_partial_backend_without_unit_is_removed_without_service_mutation(self):
        self.install_owned()
        self.target['unit'].unlink()
        with patch.object(installer, 'run_command', return_value='FragmentPath=\nDropInPaths=\nActiveState=inactive\nLoadState=not-found') as run:
            result = uninstall.remove_backend()
        self.assertTrue(result['ok'])
        self.assertTrue(all('show' in call.args[0] for call in run.call_args_list))
        self.assertTrue(self.database.exists())

    def test_live_non_systemd_collector_prevents_runtime_deletion(self):
        self.install_owned()
        lock_path = self.target['data'] / 'writer.lock'
        with lock_path.open('w') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = uninstall.remove_backend()
        self.assertFalse(result['ok'])
        self.assertIn('writer.lock', result['error'])
        self.assertTrue(self.target['venv'].exists())
        self.assertEqual(store.load_config(), self.config)
        self.assertEqual(self.bash.read_bytes(), self.prefix + self.block + self.suffix)

    def test_foreign_runtime_marker_preserves_all_backend_files(self):
        self.install_owned()
        (self.target['venv'] / '.oma-usage-owned').write_text('someone else')
        result = uninstall.remove_backend()
        self.assertFalse(result['ok'])
        self.assertIn(str(self.target['venv']), result['error'])
        self.assertTrue(self.target['launcher'].exists())
        self.assertFalse(self.commands)

    def test_empty_preview_and_cli_default_do_not_create_state(self):
        before = list(self.home.rglob('*'))
        self.assertFalse(uninstall.preview()['backend_present'])
        with patch('builtins.print'):
            self.assertEqual(cli.main(['uninstall']), 0)
        self.assertEqual(list(self.home.rglob('*')), before)
        self.assertFalse(self.commands)

    def test_unrelated_linked_bash_startup_is_preserved_during_removal(self):
        self.install_owned()
        shared = self.home / 'shared-bashrc'
        shared.write_bytes(self.prefix)
        self.bash.unlink()
        self.bash.symlink_to(shared)
        self.assertTrue(uninstall.remove_backend()['ok'])
        self.assertTrue(self.bash.is_symlink())
        self.assertEqual(shared.read_bytes(), self.prefix)
        self.assertFalse(self.target['venv'].exists())
