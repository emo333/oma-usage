"""Filesystem-only installation safety boundaries; never touches host configuration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oma_usage import installer


class InstallationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'plugins/oma.usage'
        self.project.mkdir(parents=True)
        (self.project / 'manifest.json').write_text(json.dumps({'id': 'oma.usage', 'schemaVersion': 1}))
        for name in ('pyproject.toml', 'bootstrap.py', 'Service.qml', 'BarWidget.qml'):
            (self.project / name).write_text('initial payload')
        for name in ('systemd/oma-usage.service.in', 'desktop/org.omarchy.OmaUsage.desktop.in',
                     'bash/oma-usage.bash', 'bash/terminal.bash'):
            path = self.project / 'integrations' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('@LAUNCHER@')
        (self.project / 'oma_usage').mkdir()
        (self.project / 'oma_usage/__main__.py').write_text('print("initial")')
        (self.project / 'dashboard/src').mkdir(parents=True)
        (self.project / 'dashboard/Cargo.toml').write_text('manifest')
        (self.project / 'dashboard/Cargo.lock').write_text('lock')
        (self.project / 'dashboard/src/main.rs').write_text('source')
        self.target = dict(data=self.root / 'data', config=self.root / 'config',
                           venv=self.root / 'data/venv', manifest=self.root / 'data/installed-files.json',
                           launcher=self.root / 'bin/oma-usage', desktop=self.root / 'apps/oma.desktop',
                           unit=self.root / 'systemd/oma-usage.service', plugin=self.project,
                           hook=self.root / 'data/oma-usage.bash', terminal=self.root / 'data/terminal.bash')
        mock_locations = patch.object(installer, 'locations', return_value=self.target)
        mock_locations.start()
        self.addCleanup(mock_locations.stop)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_missing_backend_check_leaves_checkout_and_filesystem_unchanged(self):
        before = self.snapshot()
        self.assertFalse(installer.runtime_current(self.project))
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.target['config'].exists())
        self.assertFalse(self.target['data'].exists())

    def test_development_checkout_is_rejected_before_install_mutation(self):
        development = self.root / 'development'
        development.mkdir()
        (development / 'manifest.json').write_bytes((self.project / 'manifest.json').read_bytes())
        before = self.snapshot()
        with self.assertRaises(installer.InstallError):
            installer.install(development)
        self.assertEqual(before, self.snapshot())

    def test_same_version_payload_change_requires_runtime_refresh(self):
        assets = installer._assets(self.project)
        for path, content in assets.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.target['venv'] / 'bin').mkdir(parents=True)
        (self.target['venv'] / 'bin/python').touch()
        dashboard = self.target['venv'] / 'bin/oma-usage-dashboard'
        dashboard.write_bytes(b'dashboard')
        dashboard.chmod(0o700)
        (self.target['venv'] / '.oma-usage-owned').write_text(installer.OWNER)
        self.target['manifest'].write_text(json.dumps({
            'owner': installer.OWNER, 'venv': True,
            'files': {str(path): installer._digest(content) for path, content in assets.items()},
            'dashboard_digest': installer._digest(b'dashboard'),
            'source_digest': installer._source_digest(self.project)}))
        self.assertTrue(installer.runtime_current(self.project))
        (self.project / 'oma_usage/__main__.py').write_text('print("updated")')
        self.assertFalse(installer.runtime_current(self.project))

    def test_rust_source_and_lockfile_are_payload_inputs(self):
        initial = installer._source_digest(self.project)
        source = self.project / 'dashboard/src/main.rs'
        source.write_text('changed source')
        changed = installer._source_digest(self.project)
        self.assertNotEqual(initial, changed)
        (self.project / 'dashboard/Cargo.lock').write_text('changed lock')
        self.assertNotEqual(changed, installer._source_digest(self.project))
        target = self.project / 'dashboard/target'
        target.mkdir()
        (target / 'generated').write_text('ignored')
        self.assertNotIn(target / 'generated', installer._payload(self.project))

    def test_modified_dashboard_is_not_replaced_or_removed(self):
        venv = self.target['venv']
        (venv / 'bin').mkdir(parents=True)
        (venv / '.oma-usage-owned').write_text(installer.OWNER)
        binary = venv / 'bin/oma-usage-dashboard'
        binary.write_bytes(b'foreign modification')
        manifest = {'owner': installer.OWNER, 'venv': True, 'files': {},
                    'dashboard_digest': installer._digest(b'original')}
        with self.assertRaises(installer.InstallError):
            installer._owned_runtime(manifest)
        self.assertEqual(binary.read_bytes(), b'foreign modification')

    def test_unowned_backend_file_is_preserved_and_reported(self):
        launcher = self.target['launcher']
        launcher.parent.mkdir()
        launcher.write_text('my custom launcher')
        before = self.snapshot()
        with self.assertRaisesRegex(installer.InstallError, 'foreign or modified'):
            installer.runtime_current(self.project)
        self.assertEqual(before, self.snapshot())

    def test_concurrent_lifecycle_operation_is_rejected(self):
        with installer.lifecycle_lock():
            with self.assertRaisesRegex(installer.InstallError, 'setup_busy'):
                with installer.lifecycle_lock():
                    self.fail('second lifecycle lock was acquired')

    def test_plugin_link_cannot_turn_development_tree_into_runtime_source(self):
        development = self.root / 'development'
        self.project.rename(development)
        self.project.symlink_to(development, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaises(installer.InstallError):
            installer.runtime_current(development)
        self.assertEqual(before, self.snapshot())

    def test_owned_unit_with_foreign_drop_in_is_not_selected_for_update(self):
        unit = self.target['unit']
        unit.parent.mkdir()
        unit.write_bytes(b'owned unit')
        manifest = {'files': {str(unit): installer._digest(b'owned unit')}}
        reply = 'FragmentPath=' + str(unit) + '\nActiveState=active\nDropInPaths=/custom/override.conf'
        with patch.object(installer, 'run_command', return_value=reply):
            with self.assertRaises(installer.InstallError):
                installer._active_owned_collector(manifest)
