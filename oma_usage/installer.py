"""Consent-only preparation of an external runtime for Omarchy-managed checkouts."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import stat
import sys

OWNER = 'org.omarchy.OmaUsage:1'
NATIVE_PREREQUISITE = 'omarchy pkg add python-gobject gtk4 libadwaita'


class InstallError(Exception):
    pass


def locations() -> dict[str, Path]:
    home = Path.home()
    data = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')).absolute() / 'oma-usage'
    config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')).absolute()
    return dict(data=data, config=config / 'oma-usage', venv=data / 'venv', manifest=data / 'installed-files.json',
                launcher=home / '.local/bin/oma-usage',
                desktop=Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')).absolute() / 'applications/org.omarchy.OmaUsage.desktop',
                unit=config / 'systemd/user/oma-usage.service',
                plugin=home / '.config/omarchy/plugins/oma.usage',
                hook=data / 'oma-usage.bash')


def run_command(args: list[str], *, timeout=15) -> str:
    """Bounded external boundary; errors never echo subprocess output."""
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError('Command unavailable or timed out: ' + Path(args[0]).name) from None
    if result.returncode:
        raise InstallError('Command failed: ' + Path(args[0]).name
                           + ' (exit ' + str(result.returncode) + '). Check the required packages and user session.')
    return result.stdout.decode('utf-8', errors='replace').strip()


def preflight() -> None:
    if os.getuid() == 0:
        raise InstallError('Do not run installation or setup as root.')
    if sys.version_info < (3, 12):
        raise InstallError('Python 3.12 or newer is required.')
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        gi.require_version('GioUnix', '2.0')
        from gi.repository import Gtk, Adw, GioUnix  # noqa: F401
    except (ImportError, ValueError):
        raise InstallError('Native dependencies missing. Run: ' + NATIVE_PREREQUISITE) from None
    for binary in ('pacman', 'systemctl', 'omarchy', 'omarchy-shell', 'omarchy-plugin-enable'):
        if not shutil.which(binary):
            raise InstallError('Required command missing: ' + binary)
    run_command(['pacman', '-Q'], timeout=15)
    run_command(['systemctl', '--user', 'show-environment'])
    # Query only. These invoke neither enablement nor a shell configuration write.
    help_text = run_command(['omarchy', 'plugin', 'enable', '--help'])
    if 'plugin enable' not in help_text:
        raise InstallError('incompatible_omarchy_plugin_contract')
    enable = Path(shutil.which('omarchy-plugin-enable')).read_text()
    if '--section)' not in enable or 'enablePlugin' not in enable:
        raise InstallError('incompatible_omarchy_plugin_contract')
    root = Path(os.environ.get('OMARCHY_PATH', '/usr/share/omarchy'))
    try:
        registry = (root / 'shell/services/PluginRegistry.qml').read_text()
        shell = (root / 'shell/shell.qml').read_text()
    except OSError:
        raise InstallError('incompatible_omarchy_shell_contract') from None
    if (not all(token in registry for token in ('bar-widget', 'barWidget', 'service', 'schemaVersion'))
            or not all(token in shell for token in ('rescanPlugins', 'function putBarWidget'))):
        raise InstallError('incompatible_omarchy_shell_contract')
    # Requiring a live shell makes an unavailable integration an explicit error.
    reply = run_command(['omarchy-shell', 'shell', 'listPlugins'])
    try:
        plugins = json.loads(reply)
    except ValueError:
        raise InstallError('incompatible_omarchy_shell_contract') from None
    if not isinstance(plugins, (list, dict)):
        raise InstallError('incompatible_omarchy_shell_contract')


def _safe_parents(path: Path) -> None:
    if path.is_symlink():
        raise InstallError('Refusing symbolic-link install destination: ' + str(path))
    for parent in path.parents:
        if parent.is_symlink():
            try:
                owned_directory = (parent.lstat().st_uid == os.getuid()
                                   and parent.is_dir() and parent.stat().st_uid == os.getuid())
            except OSError:
                owned_directory = False
            if not owned_directory:
                raise InstallError('Refusing unowned symbolic-link install parent: ' + str(parent))


def atomic_write(path: Path, content: bytes, mode=0o600) -> None:
    _safe_parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.oma-usage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_manifest() -> dict:
    path = locations()['manifest']
    _safe_parents(path)
    if not path.exists():
        return {'owner': OWNER, 'files': {}, 'venv': False}
    try:
        if not path.is_file() or path.stat().st_uid != os.getuid():
            raise ValueError
        value = json.loads(path.read_text())
        if value['owner'] != OWNER or not isinstance(value['files'], dict) or type(value['venv']) is not bool:
            raise ValueError
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value['files'].items()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError('Foreign or malformed installation ownership manifest.') from None
    return value


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def assert_owned(path: Path, manifest: dict) -> None:
    _safe_parents(path)
    if path.exists():
        expected = manifest['files'].get(str(path))
        if (not path.is_file() or path.stat().st_uid != os.getuid()
                or expected is None or _digest(path.read_bytes()) != expected):
            raise InstallError('Refusing foreign or modified file: ' + str(path))


def _systemd_quote(path: Path) -> str:
    # Unit expansion and C-style quoting are separate from shell quoting.
    value = str(path).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
    return '"' + value + '"'


def _desktop_quote(path: Path) -> str:
    value = str(path).replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        value = value.replace(character, '\\' + character)
    # Desktop entry string escaping precedes Exec argument parsing.
    return ('"' + value + '"').replace('\\', '\\\\')


@contextmanager
def lifecycle_lock():
    """Serialize all consented backend lifecycle operations."""
    folder = locations()['config']
    _safe_parents(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not folder.is_dir() or folder.stat().st_uid != os.getuid():
        raise InstallError('Refusing unowned setup directory: ' + str(folder))
    folder.chmod(0o700)
    path = folder / 'setup.lock'
    _safe_parents(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise InstallError('Refusing unowned setup lock.')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('setup_busy: Another setup or removal is running.') from None
        yield


def _project(project: Path) -> Path:
    if locations()['plugin'].is_symlink():
        raise InstallError('The installed plugin must be a checkout, not a link to the development tree.')
    project = project.resolve(strict=True)
    if project != locations()['plugin'].resolve(strict=True):
        raise InstallError('Install through Omarchy first. The development checkout is not a runtime source.')
    try:
        value = json.loads((project / 'manifest.json').read_text())
        if value['id'] != 'oma.usage' or value['schemaVersion'] != 1:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError('The installed Omarchy plugin manifest is invalid.') from None
    return project


def _payload(project: Path) -> list[Path]:
    # These are the complete wheel/build inputs; ignore Git state and generated files.
    files = [project / name for name in
             ('pyproject.toml', 'manifest.json', 'bootstrap.py', 'Service.qml', 'BarWidget.qml')]
    for folder in ('oma_usage', 'integrations'):
        files.extend(path for path in (project / folder).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts
                     and path.suffix not in ('.pyc', '.pyo'))
    if any(path.is_symlink() for path in files):
        raise InstallError('Symbolic links are not supported in the plugin build payload.')
    return sorted(files)


def _source_digest(project: Path) -> str:
    digest = hashlib.sha256()
    for path in _payload(project):
        digest.update(str(path.relative_to(project)).encode() + b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def _assets(project: Path) -> dict[Path, bytes]:
    target = locations()
    launcher = target['launcher'].absolute()
    python = target['venv'] / 'bin/python'
    return {
        launcher: ('#!/bin/sh\n# Managed by oma-usage (org.omarchy.OmaUsage)\nexec ' + shlex.quote(str(python)) + ' -I -m oma_usage "$@"\n').encode(),
        target['unit']: (project / 'integrations/systemd/oma-usage.service.in').read_text().replace('@LAUNCHER@', _systemd_quote(launcher)).encode(),
        target['desktop']: (project / 'integrations/desktop/org.omarchy.OmaUsage.desktop.in').read_text().replace('@LAUNCHER@', _desktop_quote(launcher)).encode(),
        target['hook']: (project / 'integrations/bash/oma-usage.bash').read_bytes(),
    }


def _owned_runtime(manifest: dict) -> None:
    target = locations()
    _safe_parents(target['data'])
    if target['data'].exists() and (not target['data'].is_dir()
                                     or target['data'].stat().st_uid != os.getuid()):
        raise InstallError('Refusing unowned backend data directory.')
    allowed = {str(target[key]) for key in ('launcher', 'desktop', 'unit', 'hook')}
    if set(manifest['files']) - allowed:
        raise InstallError('Legacy or foreign ownership manifest. Remove the old installation explicitly before installing this backend.')
    for key in ('launcher', 'desktop', 'unit', 'hook'):
        assert_owned(target[key], manifest)
    venv = target['venv']
    _safe_parents(venv)
    marker = venv / '.oma-usage-owned'
    if venv.exists() and (not manifest['venv'] or not marker.is_file()
                          or marker.is_symlink() or marker.read_text() != OWNER
                          or venv.stat().st_uid != os.getuid()):
        raise InstallError('Refusing unrelated virtualenv: ' + str(venv))


def runtime_current(project: Path) -> bool:
    """Read-only freshness and ownership check, safe before setup consent."""
    project = _project(project)
    target = locations()
    manifest = read_manifest()
    _owned_runtime(manifest)
    assets = _assets(project)
    return (manifest.get('source_digest') == _source_digest(project)
            and manifest['venv'] and (target['venv'] / 'bin/python').is_file()
            and all(path.is_file() and manifest['files'].get(str(path)) == _digest(content)
                    for path, content in assets.items()))


def _active_owned_collector(manifest: dict) -> bool:
    unit = locations()['unit']
    if not unit.is_file() or str(unit) not in manifest['files']:
        return False
    assert_owned(unit, manifest)
    # Check what systemd actually loaded, not merely a same-named local file.
    reply = run_command(['systemctl', '--user', 'show', 'oma-usage.service',
                         '--property=FragmentPath,ActiveState,DropInPaths'])
    fields = dict(line.split('=', 1) for line in reply.splitlines() if '=' in line)
    if fields.get('DropInPaths'):
        raise InstallError('Refusing to update a collector with systemd drop-ins. Resolve those overrides first.')
    if fields.get('ActiveState') not in ('active', 'activating', 'reloading'):
        return False
    if Path(fields.get('FragmentPath', '')).absolute() != unit.absolute():
        raise InstallError('Refusing to restart a collector using an unowned unit.')
    return True


def _require_closed_dashboard() -> None:
    from gi.repository import Gio, GLib
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                              'org.freedesktop.DBus', 'NameHasOwner',
                              GLib.Variant('(s)', ('org.omarchy.OmaUsage',)),
                              GLib.VariantType.new('(b)'), Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        raise InstallError('Cannot check the running dashboard. Retry in your desktop session.') from None
    if reply.unpack()[0]:
        raise InstallError('Close the existing Oma Usage dashboard before updating its runtime, then retry.')


def install(project: Path, *, check_only=False) -> dict:
    """Install only external backend files after explicit file/network consent."""
    project = _project(project)
    preflight()
    target = locations()
    assets = _assets(project)
    for path in (*assets, target['venv']):
        if any(ord(character) < 32 or ord(character) == 127 for character in str(path)):
            raise InstallError('Control characters are not supported in installation paths.')
    manifest = read_manifest()
    _owned_runtime(manifest)
    if check_only:
        return {'ok': True, 'paths': [str(path) for path in assets]}
    _require_closed_dashboard()
    with lifecycle_lock():
        manifest = read_manifest()
        _owned_runtime(manifest)
        target['data'].mkdir(mode=0o700, parents=True, exist_ok=True)
        target['data'].chmod(0o700)
        source_digest = _source_digest(project)
        python = target['venv'] / 'bin/python'
        # Build from a disposable copy: setuptools must never touch the Git checkout.
        with tempfile.TemporaryDirectory(prefix='oma-usage-build-') as directory:
            temporary = Path(directory)
            source, wheels = temporary / 'source', temporary / 'wheels'
            source.mkdir()
            wheels.mkdir()
            for path in _payload(project):
                destination = source / path.relative_to(project)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            if _source_digest(source) != source_digest:
                raise InstallError('Plugin changed during setup. Retry after the plugin update finishes.')
            try:
                builder = temporary / 'builder'
                run_command(['/usr/bin/python3', '-I', '-m', 'venv', str(builder)], timeout=120)
                run_command([str(builder / 'bin/python'), '-I', '-m', 'pip', 'wheel',
                             '--wheel-dir', str(wheels), str(source)], timeout=300)
            except InstallError as exc:
                raise InstallError('Could not download/build the backend wheel. Check network access and '
                                   'your pip package index; install prerequisites with: '
                                   + NATIVE_PREREQUISITE + '. ' + str(exc)) from None
            project_wheels = list(wheels.glob('oma_usage-*.whl'))
            if len(project_wheels) != 1:
                raise InstallError('Expected exactly one oma-usage project wheel.')
            _require_closed_dashboard()
            active = _active_owned_collector(manifest)
            manifest.pop('source_digest', None)
            atomic_write(target['manifest'], json.dumps(manifest, sort_keys=True).encode())
            if active:
                run_command(['systemctl', '--user', 'stop', 'oma-usage.service'])
            # Failure after this point deliberately leaves a stopped collector and
            # stale fingerprint: retry is explicit, never a fake successful update.
            if not target['venv'].exists():
                target['venv'].mkdir(mode=0o700, parents=True)
                atomic_write(target['venv'] / '.oma-usage-owned', OWNER.encode())
                manifest['venv'] = True
                atomic_write(target['manifest'], json.dumps(manifest, sort_keys=True).encode())
            run_command(['/usr/bin/python3', '-I', '-m', 'venv', '--system-site-packages',
                         str(target['venv'])], timeout=120)
            run_command([str(python), '-I', '-m', 'pip', 'install', '--no-index',
                         '--find-links', str(wheels), '--force-reinstall',
                         str(project_wheels[0])], timeout=300)
        for path, content in assets.items():
            atomic_write(path, content, 0o700 if path == target['launcher'] else 0o600)
            manifest['files'][str(path)] = _digest(content)
            atomic_write(target['manifest'], json.dumps(manifest, sort_keys=True).encode())
        run_command(['systemctl', '--user', 'daemon-reload'])
        if active:
            run_command(['systemctl', '--user', 'start', 'oma-usage.service'])
        manifest['source_digest'] = source_digest
        atomic_write(target['manifest'], json.dumps(manifest, sort_keys=True).encode())
    return {'ok': True, 'paths': [str(path) for path in assets]}
