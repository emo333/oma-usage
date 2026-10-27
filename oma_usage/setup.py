"""Consent-only setup operations; invoke on a worker, never the GTK main thread.

preview/status are read-only. Only enable_tracking starts services, imports the
selected history snapshot, changes Bash startup, or enables the Omarchy plugin.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import time

from . import client, store
from .installer import (OWNER, InstallError, assert_owned, atomic_write, locations,
                        preflight, read_manifest, run_command, lifecycle_lock)

STAGES = ('preflight', 'service', 'history', 'hook', 'plugin', 'collectors')
START = b'# >>> oma-usage >>>'
END = b'# <<< oma-usage <<<'


class SetupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _state_path() -> Path:
    return store.paths()['config'] / 'setup-state.json'


def _history_file(selected=None) -> str:
    return str(Path(selected if selected is not None else os.environ.get('HISTFILE') or Path.home() / '.bash_history').expanduser().absolute())


def _source() -> str:
    return 'source ' + shlex.quote(str(locations()['hook']))


def preview(history_file=None) -> dict:
    """Describe exact requested changes without opening the history file."""
    target = locations()
    managed = [target['manifest'], target['venv'], target['launcher'], target['desktop'],
               target['unit'],
               target['unit'].parent / 'graphical-session.target.wants/oma-usage.service',
               Path.home() / '.config/omarchy/shell.json',
               target['hook'], store.paths()['config'] / 'config.json', _state_path(),
               store.paths()['config'] / 'setup.lock', Path.home() / '.bashrc',
               store.paths()['data'] / 'usage.sqlite3',
               store.paths()['data'] / 'usage.sqlite3-wal',
               store.paths()['data'] / 'usage.sqlite3-shm',
               store.paths()['data'] / 'writer.lock']
    if 'runtime' in store.paths():
        managed.append(store.paths()['runtime'] / 'collector.sock')
    return {'paths': [str(path) for path in managed], 'history_file': _history_file(history_file),
            'scopes': ['Estimated foreground app attention (app IDs only; idle and lock excluded)',
                       'One-time Bash history command-name mentions, then history-visible prompt submissions',
                       'Observed user and system service running time and starts',
                       'Installed packages, launchers, executable names, and service inventory',
                       'Enable the user service and add Usage to the bar only if absent; preserve existing placement; Omarchy manages shell.json'],
            'privacy': 'Local-only metadata. No titles, arguments, working directories, document names, websites, or raw history are stored. Browser research requires separate confirmation. Opening this preview starts no collection.',
            'bar_placement': 'preserve existing; right when absent'}


def _read_state() -> dict | None:
    path = _state_path()
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise SetupError('setup_state_invalid')
    try:
        state = json.loads(path.read_text())
        if (state['owner'] != OWNER or type(state['complete']) is not bool
                or type(state['resume_on_success']) is not bool
                or not isinstance(state['stages'], dict)
                or set(state['stages']) != set(STAGES)
                or any(not isinstance(value, str) for value in state['stages'].values())
                or not isinstance(state['history_file'], str)):
            raise ValueError
        return state
    except (OSError, ValueError, KeyError, TypeError):
        raise SetupError('setup_state_invalid') from None


def _save(state: dict) -> None:
    atomic_write(_state_path(), json.dumps(state, sort_keys=True).encode())


def _sources() -> dict:
    conn = store.readonly()
    try:
        return {row['source']: {'state': row['state'], 'error_code': row['error_code']}
                for row in conn.execute('SELECT source,state,error_code FROM collector_status')}
    finally:
        conn.close()


def _result(state, *, error=None, sources=None) -> dict:
    result = {'ok': state['complete'] and error is None, 'stages': state['stages'].copy(),
              'history_file': state['history_file']}
    if error or state.get('error'):
        result['error'] = error or state['error']
        result['ok'] = False
    if state.get('manual_source'):
        result['manual_source'] = state['manual_source']
    if sources is not None:
        result['sources'] = sources
    return result


def status() -> dict:
    """Read saved stage outcomes and current source states; no collector startup."""
    try:
        state = _read_state()
        if state is None:
            return {'ok': False, 'stages': dict.fromkeys(STAGES, 'not_started')}
        try:
            sources = _sources()
        except (OSError, RuntimeError, sqlite3.Error):
            sources = {}
        result = _result(state, sources=sources)
        if state['complete']:
            try:
                settings = client.observer_settings(timeout=0.2)
            except client.ClientError:
                result.update(ok=False, error='collector_unavailable')
            else:
                result['collection_enabled'] = settings['collection_enabled']
                if settings['collection_enabled'] and any(sources.get(source, {}).get('state') != 'collecting'
                        for source in ('app_focus', 'service_user', 'service_system')):
                    result.update(ok=False, error='collectors_unavailable')
        return result
    except SetupError as exc:
        return {'ok': False, 'stages': dict.fromkeys(STAGES, 'unknown'), 'error': exc.code}


def _control(action) -> None:
    client.request({'op': 'control', 'action': action}, timeout=2)


def _wait_socket(seconds=10) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return client.observer_settings(timeout=0.2)
        except client.ClientError:
            if time.monotonic() >= deadline:
                raise SetupError('collector_unavailable') from None
            time.sleep(0.2)


def _paused_config(config: dict) -> None:
    """Do not race a live writer whose socket happens to be unavailable."""
    folder = store.paths()['data']
    store.private_dir(folder)
    fd = os.open(folder / 'writer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('collector_unavailable') from None
        store.save_config(dict(config, collection_enabled=False))


def _installed() -> None:
    target = locations()
    manifest = read_manifest()
    expected = [target['launcher'], target['desktop'], target['unit'], target['hook']]
    for path in expected:
        if not path.is_file() or str(path) not in manifest['files']:
            raise SetupError('installation_missing')
        assert_owned(path, manifest)
    venv = target['venv']
    marker = venv / '.oma-usage-owned'
    if (not manifest['venv'] or venv.is_symlink() or marker.is_symlink()
            or not marker.is_file() or marker.read_text() != OWNER
            or not (venv / 'bin/python').is_file()):
        raise SetupError('installation_missing')


def _hook() -> str:
    """Append one exact block; preserve every pre-existing unrelated byte."""
    path = Path.home() / '.bashrc'
    block = START + b'\n' + _source().encode() + b'\n' + END + b'\n'
    try:
        if path.is_symlink():
            raise SetupError('bash_startup_custom')
        if path.exists():
            mode = path.stat().st_mode
            if not stat.S_ISREG(mode):
                raise SetupError('bash_startup_custom')
            original = path.read_bytes()
            if START in original or END in original:
                if original.count(START) != 1 or original.count(END) != 1:
                    raise SetupError('bash_block_conflict')
                offset = original.index(START)
                if (offset and original[offset - 1:offset] != b'\n') or original[offset:offset + len(block)] != block:
                    raise SetupError('bash_block_conflict')
                return 'pending_new_shell'
            if not mode & 0o222:
                raise SetupError('bash_startup_readonly')
            permissions = stat.S_IMODE(mode)
        else:
            original = b''
            permissions = 0o600
        parent_mode = path.parent.stat().st_mode
        if not parent_mode & 0o222:
            raise SetupError('bash_startup_readonly')
        separator = b'\n' if original and not original.endswith(b'\n') else b''
        atomic_write(path, original + separator + block, permissions)
        return 'pending_new_shell'
    except OSError:
        raise SetupError('bash_startup_unwritable') from None


def _wait_collectors(seconds=8) -> tuple[bool, dict]:
    deadline = time.monotonic() + seconds
    sources = {}
    while True:
        try:
            sources = _sources()
        except (OSError, RuntimeError, sqlite3.Error):
            pass
        # Bash is installed, not active in already-open shells. No fabricated
        # prompt heartbeat is required to prove setup or imported history.
        if all(sources.get(source, {}).get('state') == 'collecting'
               for source in ('app_focus', 'service_user', 'service_system')):
            return True, sources
        if time.monotonic() >= deadline:
            return False, sources
        time.sleep(0.25)


def enable_tracking(history_file=None) -> dict:
    """Perform explicitly consented setup, persisting every actual stage outcome.

    Failures before hook/plugin readiness leave collection paused. Independent
    source failures keep authorized working sources running and setup incomplete.
    Completed setup preserves the effective pause setting when retried.
    """
    lock = None
    state = None
    stage = 'preflight'
    paused = False
    try:
        # Validate before any configuration write; malformed config is not reset.
        config = store.load_config()
        previous = _read_state()
        lock = lifecycle_lock()
        try:
            lock.__enter__()
        except InstallError as exc:
            lock = None
            if str(exc).startswith('setup_busy'):
                raise SetupError('setup_busy') from None
            raise
        # Re-read after serialization so another consent worker cannot race us.
        previous = _read_state()
        state = previous or {'owner': OWNER, 'complete': False, 'resume_on_success': True,
                             'history_file': _history_file(history_file),
                             'stages': dict.fromkeys(STAGES, 'not_started')}
        if history_file is not None:
            state['history_file'] = _history_file(history_file)
        state.pop('error', None)
        state.pop('manual_source', None)
        _save(state)
        try:
            preflight()
            _installed()
        except (InstallError, OSError):
            raise SetupError('preflight_failed') from None
        state['stages']['preflight'] = 'ready'
        _save(state)
        stage = 'service'
        try:
            effective = client.observer_settings(timeout=0.2)
        except client.ClientError:
            # No writer is running. Consent permits only a paused config write.
            # Existing user settings remain intact and strictly validated.
            if state['complete']:
                state['resume_on_success'] = config['collection_enabled']
            _paused_config(config)
        else:
            if state['complete']:
                state['resume_on_success'] = effective['collection_enabled']
            _control('pause')
            paused = True
        _save(state)
        run_command(['systemctl', '--user', 'daemon-reload'])
        run_command(['systemctl', '--user', 'enable', '--now', 'oma-usage.service'])
        _wait_socket()
        _control('pause')
        paused = True
        state['stages']['service'] = 'ready'
        _save(state)
        stage = 'history'
        from .cli import import_history
        imported = import_history(state['history_file'])
        if imported['ok']:
            state['stages']['history'] = imported['status']
        else:
            state['stages']['history'] = 'not_imported'
            state['error'] = imported['error']
        _save(state)
        stage = 'hook'
        try:
            state['stages']['hook'] = _hook()
        except SetupError:
            state['manual_source'] = _source()
            raise
        _save(state)
        stage = 'plugin'
        run_command(['omarchy-shell', 'shell', 'rescanPlugins'])
        reply = run_command(['omarchy-shell', 'shell', 'putBarWidget', 'oma.usage',
                             '{"section":"right"}'])
        if reply != 'ok':
            raise SetupError('plugin_enable_failed')
        state['stages']['plugin'] = 'enabled'
        _save(state)
        stage = 'collectors'
        if state['resume_on_success']:
            _control('resume')
            paused = False
            ready, sources = _wait_collectors()
            if not ready:
                # Preserve functioning authorized sources while reporting the
                # unavailable source precisely; setup is still incomplete.
                state['sources'] = sources
                raise SetupError('collectors_unavailable')
            state['stages']['collectors'] = 'collecting'
        else:
            # A completed installation intentionally paused by the user stays so.
            sources = _sources()
            state['stages']['collectors'] = 'paused'
        state['sources'] = sources
        if not imported['ok']:
            stage = 'history'
            raise SetupError(imported['error'])
        state['complete'] = True
        state.pop('error', None)
        _save(state)
        return _result(state, sources=sources)
    except (SetupError, client.ClientError, InstallError, OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        code = exc.code if isinstance(exc, (SetupError, client.ClientError)) else {
            'preflight': 'preflight_failed', 'service': 'service_start_failed',
            'history': 'history_import_failed', 'hook': 'bash_startup_unwritable',
            'plugin': 'plugin_enable_failed', 'collectors': 'collectors_unavailable'}[stage]
        if paused:
            try:
                _control('pause')
            except client.ClientError:
                pass
        if state is not None and code != 'setup_busy':
            state['complete'] = False
            state['stages'][stage] = 'failed' if stage != 'history' else 'not_imported'
            state['error'] = code
            try:
                _save(state)
            except (OSError, InstallError):
                pass
            return _result(state, error=code, sources=state.get('sources'))
        return {'ok': False, 'stages': dict.fromkeys(STAGES, 'not_started'), 'error': code}
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)
