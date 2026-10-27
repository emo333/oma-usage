"""Explicit backend cleanup; Omarchy alone owns the plugin checkout lifecycle."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat

from . import installer, store

ASSETS = ('launcher', 'desktop', 'unit', 'hook')
NEXT_STEP = 'Then remove the native plugin separately: omarchy plugin remove oma.usage'


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _safe(path: Path, *, directory=False) -> None:
    installer._safe_parents(path)
    if _exists(path):
        info = path.lstat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if info.st_uid != os.getuid() or not expected(info.st_mode):
            raise installer.InstallError('Refusing foreign or unexpected target: ' + str(path))
    for parent in path.parents:
        if parent.exists() and parent.stat().st_uid not in (0, os.getuid()):
            raise installer.InstallError('Refusing foreign target parent: ' + str(parent))
        if parent.exists() and not parent.is_dir():
            raise installer.InstallError('Target parent is not a directory: ' + str(parent))


def _writable_parent(path: Path) -> None:
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if not parent.stat().st_mode & 0o222 or not os.access(parent, os.W_OK | os.X_OK):
        raise installer.InstallError('Target parent is not writable: ' + str(parent))


def _bash_edit(hook: Path):
    path = Path.home() / '.bashrc'
    start, end = b'# >>> oma-usage >>>', b'# <<< oma-usage <<<'
    if path.is_symlink():
        original = path.read_bytes() if path.exists() else b''
        if start in original or end in original:
            raise installer.InstallError('Remove the managed Bash block manually from the linked startup file before retrying: ' + str(path))
        return None
    _safe(path)
    if not path.exists():
        return None
    original = path.read_bytes()
    if start not in original and end not in original:
        return None
    block = start + b'\nsource ' + shlex.quote(str(hook)).encode() + b'\n' + end + b'\n'
    offset = original.find(start)
    if (original.count(start) != 1 or original.count(end) != 1
            or (offset and original[offset - 1:offset] != b'\n')
            or original[offset:offset + len(block)] != block):
        raise installer.InstallError('Edited or ambiguous managed Bash block in ' + str(path)
                                     + '; restore the exact source block or remove it manually, then retry.')
    return path, original[:offset] + original[offset + len(block):], stat.S_IMODE(path.stat().st_mode)


def _service(target, manifest):
    """Reject overrides/foreign enablement before invoking any systemd mutation."""
    unit = target['unit']
    links = []
    if unit.parent.exists():
        for folder in unit.parent.iterdir():
            if folder.name.endswith(('.wants', '.requires')) and folder.is_dir():
                link = folder / unit.name
                if _exists(link):
                    if (folder.is_symlink() or folder.stat().st_uid != os.getuid()
                            or not link.is_symlink() or link.lstat().st_uid != os.getuid()
                            or link.resolve() != unit.resolve()):
                        raise installer.InstallError('Refusing foreign service enablement: ' + str(link))
                    links.append(link)
    if not unit.exists() and str(unit) not in manifest['files'] and not links:
        return False, links
    reply = installer.run_command(['systemctl', '--user', 'show', unit.name,
                                  '--property=FragmentPath,DropInPaths,ActiveState,LoadState'])
    values = dict(line.split('=', 1) for line in reply.splitlines() if '=' in line)
    if not {'FragmentPath', 'DropInPaths', 'ActiveState', 'LoadState'} <= values.keys():
        raise installer.InstallError('Cannot establish systemd unit ownership; inspect systemctl --user show oma-usage.service.')
    fragment = values['FragmentPath']
    if values['DropInPaths'] or (fragment and Path(fragment).resolve() != unit.resolve()):
        raise installer.InstallError('Foreign unit or systemd drop-ins found; preserve them and resolve ownership before retrying.')
    if not unit.exists():
        if values['ActiveState'] not in ('inactive', 'failed') or fragment:
            raise installer.InstallError('The owned unit file is missing but systemd still has a loaded unit; restore the owned file before retrying.')
        return False, links
    if not fragment and values['ActiveState'] not in ('inactive', 'failed'):
        raise installer.InstallError('Cannot establish running service ownership; inspect systemctl --user show oma-usage.service.')
    return True, links


def _plan():
    target = installer.locations()
    manifest = installer.read_manifest()
    allowed = {str(target[name]) for name in ASSETS}
    unexpected = set(manifest['files']) - allowed
    if unexpected:
        raise installer.InstallError('Ownership manifest contains non-backend paths; no cleanup performed: '
                                     + ', '.join(sorted(unexpected)) + '. Repair the manifest without deleting those paths.')
    _safe(target['manifest'])
    files = []
    for name in ASSETS:
        path = target[name]
        _safe(path)
        installer.assert_owned(path, manifest)
        if path.exists():
            files.append(path)
    venv = target['venv']
    _safe(venv, directory=True)
    if venv.exists():
        marker = venv / '.oma-usage-owned'
        _safe(marker)
        if not manifest['venv'] or not marker.is_file() or marker.read_text() != installer.OWNER:
            raise installer.InstallError('Refusing unrelated runtime: ' + str(venv))
    config = store.paths()['config'] / 'config.json'
    state = store.paths()['config'] / 'setup-state.json'
    _safe(config)
    _safe(state)
    settings = store.load_config()
    if state.exists():
        from .setup import SetupError, _read_state
        try:
            _read_state()
        except SetupError:
            raise installer.InstallError('Refusing foreign or malformed setup state: ' + str(state)
                                         + '. Preserve and repair it before retrying cleanup.') from None
    bash = _bash_edit(target['hook'])
    service, links = _service(target, manifest)
    _safe(target['data'] / 'writer.lock')
    # Detect permission/shape problems before stopping a service or changing any
    # startup bytes. Missing parent directories may be created only after consent.
    modified = files + [p for p in (state, target['manifest'], venv) if p.exists()]
    if modified or bash or links or settings['collection_enabled']:
        modified.extend((config, target['data'] / 'writer.lock'))
    for path in modified + links:
        _writable_parent(path)
    if bash:
        _writable_parent(bash[0])
    paths = files + ([venv] if venv.exists() else [])
    paths += [p for p in (target['manifest'], state) if p.exists()]
    present = bool(paths or bash or links)
    summary = {'ok': True, 'backend_present': present,
               'paths': [str(path) for path in paths],
               'bash_startup': str(bash[0]) if bash else None,
               'service': 'Stop and disable owned oma-usage.service' if service else 'No owned loaded service to stop',
               'service_links': [str(path) for path in links],
               'collection_enabled': settings['collection_enabled'],
               'retained': [str(store.paths()['data'] / 'usage.sqlite3'), str(config), str(target['plugin'])],
               'scopes': ['Remove only verified backend files and the owned external runtime',
                          'Remove only the exact managed Bash source block; existing shells need restarting',
                          'Keep all usage, completed-history-import metadata, and settings; collection becomes disabled',
                          'Clear owned tracking setup state; do not disable or delete the Omarchy plugin'],
               'next_step': NEXT_STEP}
    return summary, target, files, bash, service, links, config, state, settings


def preview() -> dict:
    """Read-only validation and exact cleanup scope; no lock/file creation."""
    try:
        return _plan()[0]
    except (installer.InstallError, OSError, RuntimeError, ValueError) as exc:
        return {'ok': False, 'error': str(exc), 'next_step': NEXT_STEP}
    except Exception as exc:
        # Setup's structured validation error must remain actionable too.
        return {'ok': False, 'error': getattr(exc, 'code', type(exc).__name__), 'next_step': NEXT_STEP}


def remove_backend() -> dict:
    """Run only after explicit consent; prevalidate every target before stopping."""
    completed = []
    runtime_removed = False
    try:
        with installer.lifecycle_lock():
            summary, target, files, bash, service, links, config, state, settings = _plan()
            if not summary['backend_present'] and not settings['collection_enabled']:
                return dict(summary, details=['No installed backend assets found; nothing was removed.'])
            if service:
                installer.run_command(['systemctl', '--user', 'disable', '--now', 'oma-usage.service'])
                completed.append('Owned user service stopped and disabled')
            # A collector launched outside systemd must never outlive removal or
            # race the retained configuration. Do not contact an unproven socket.
            lock_path = target['data'] / 'writer.lock'
            if not target['data'].exists():
                target['data'].mkdir(mode=0o700, parents=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'r+') as writer:
                try:
                    fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise installer.InstallError('A collector still owns writer.lock; stop that collector and retry. Runtime and usage were retained.') from None
                if config.exists() or settings['collection_enabled']:
                    installer.atomic_write(config, json.dumps(dict(settings, collection_enabled=False), sort_keys=True).encode())
                    completed.append('Retained settings with collection disabled')
                if bash:
                    installer.atomic_write(*bash)
                    completed.append('Exact managed Bash source block removed')
                for link in links:
                    if _exists(link):
                        link.unlink()
                        completed.append('Removed owned service link: ' + str(link))
                for path in files:
                    path.unlink()
                    completed.append('Removed: ' + str(path))
                if service or links:
                    installer.run_command(['systemctl', '--user', 'daemon-reload'])
                if state.exists():
                    state.unlink()
                    completed.append('Cleared owned setup state')
                if target['venv'].exists():
                    shutil.rmtree(target['venv'])
                    runtime_removed = True
                    completed.append('Removed owned external runtime')
                if target['manifest'].exists():
                    target['manifest'].unlink()
                    completed.append('Cleared backend ownership manifest')
            return dict(summary, details=completed, collection_enabled=False, runtime_removed=runtime_removed)
    except Exception as exc:
        return {'ok': False, 'error': str(exc) or getattr(exc, 'code', type(exc).__name__),
                'details': completed, 'partial': bool(completed), 'runtime_removed': runtime_removed,
                'next_step': 'Resolve the reported ownership or service error and retry backend cleanup. ' + NEXT_STEP}
