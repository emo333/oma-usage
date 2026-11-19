"""Display-independent dashboard controls and explicit-consent workflows."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from . import client, recommendations, setup, store, uninstall

TIMEOUT = 2.0

ERRORS = {
    'collector_timeout': 'The collector did not acknowledge the request. It may have completed late; refresh and check its state before retrying.',
    'collector_unavailable': 'The collector is unavailable. Check that the user service is running.',
    'collector_disconnected': 'The collector disconnected before acknowledging the request. Check its state before retrying.',
    'collector_rejected': 'The collector rejected the request. Refresh and check its current state.',
    'history_unreadable': 'The selected history file could not be read. Check the selected source and its permissions.',
    'history_parse_failed': 'The history file could not be parsed. No raw history was displayed.',
    'history_parser_unavailable': 'The history parser is unavailable in the installed runtime.',
    'setup_state_invalid': 'The saved setup state is invalid; preserve and repair it before retrying.',
    'setup_busy': 'Another setup or removal operation is in progress.',
    'preflight_failed': 'Setup preflight failed. Check the local backend installation and permissions.',
    'installation_missing': 'The local backend installation is incomplete.',
    'service_start_failed': 'The user service could not be started or paused.',
    'history_import_failed': 'The one-time history import failed.',
    'plugin_enable_failed': 'The Usage bar widget could not be enabled.',
    'collectors_unavailable': 'One or more requested collectors are unavailable.',
    'lock_state_unknown': 'The session lock state is unknown, so foreground attention is not being recorded.',
    'observer_unavailable': 'The foreground observer is unavailable.',
    'collector_peer_rejected': 'The collector socket peer could not be verified.',
    'invalid_response': 'The collector returned an invalid response; check its state before retrying.',
    'request_too_large': 'The request exceeded the collector protocol limit.',
}


def error_message(code):
    return ERRORS.get(code, 'The operation could not be completed. Check the current state before retrying.')


def removal_error(value):
    error = value.get('error')
    if not isinstance(error, str):
        return 'Check ownership and service configuration before retrying.'
    reasons = (
        ('writer.lock', 'A collector still owns the writer lock; stop it before retrying.'),
        ('Bash block', 'The managed Bash startup block is linked, edited, or ambiguous; inspect it before retrying.'),
        ('systemd', 'Systemd service ownership or drop-ins could not be verified.'),
        ('service', 'Service ownership or enablement could not be verified.'),
        ('runtime', 'The runtime ownership could not be verified.'),
        ('manifest', 'The ownership manifest could not be verified.'),
        ('not writable', 'A backend target parent is not writable.'),
        ('setup state', 'The saved setup state is foreign or malformed.'),
        ('target', 'A backend target or parent could not be verified.'),
    )
    return next((message for match, message in reasons if match in error), 'Check ownership and service configuration before retrying.')


def state_label(value):
    allowed = {'not_started', 'unknown', 'ready', 'failed', 'imported', 'already_imported',
               'not_imported', 'enabled', 'collecting', 'paused', 'starting', 'unavailable', 'error'}
    return value.replace('_', ' ') if isinstance(value, str) and value in allowed else 'unknown'


def action(label, op, *, tone=None, **fields):
    result = {'label': label, 'request': {'op': op, **fields}}
    if tone:
        result['tone'] = tone
    return result


def dialog(title, body, buttons=(), *, tone=None):
    result = {'title': title, 'subtitle': body, 'sections': [], 'buttons': list(buttons)}
    if tone:
        result['tone'] = tone
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def filters(req):
    value = req.get('filters', {})
    return value if isinstance(value, dict) else {}


def selected_history(req, saved=None):
    values = filters(req)
    if 'history_file' in values:
        value = values['history_file']
        return (value.strip() or None) if isinstance(value, str) else None
    if saved is None:
        saved = setup.status()
    value = saved.get('history_file')
    return value if isinstance(value, str) and value else None


def settings_page(req):
    sections = []
    try:
        config = store.load_config()
    except Exception:
        config = None
    try:
        live = client.observer_settings(timeout=TIMEOUT)
        state = 'Collection enabled' if live['collection_enabled'] else 'Collection paused / not enabled'
        collection_tone = 'ok' if live['collection_enabled'] else 'warn'
    except client.ClientError as exc:
        state = error_message(exc.code) + ' Pause state cannot be confirmed.'
        collection_tone = 'warn'
    sections.append({'title': 'Collection', 'subtitle': state, 'tone': collection_tone,
                     'help': 'Source retry refreshes live status. A disconnected observer or bus retries through its collector.',
                     'buttons': [action('Pause collection', 'control', control='pause'),
                                 action('Resume collection', 'control', control='resume'),
                                 action('Retry source status', 'page', page='settings')]})
    options = []
    for key, title in [('retention_days', 'Daily retention (days)'), ('stale_days', 'Stale review threshold (days)'), ('idle_seconds', 'Idle threshold (seconds)')]:
        option = {'key': key, 'label': title, 'values': [{'label': str(n), 'value': str(n)} for n in store.CHOICES[key]]}
        if config:
            option['selected'] = str(config[key])
        options.append(option)
    sections.append({'title': 'Settings', 'subtitle': ('Changes are not applied until you select Apply settings.' if config else 'Configuration unavailable; repair config.json. Settings were not reset.'),
                     'tone': 'ok' if config else 'warn',
                     'help': 'Daily retention controls how long daily aggregates are kept. The stale review threshold is the age of dated evidence used for review; it is not proof of non-use. Idle threshold excludes idle time from foreground attention. Ninety-day retention with a 180-day stale threshold may leave insufficient review coverage; first/last dated evidence survives aggregate expiry.',
                     'options': options if config else [], 'buttons': [action('Apply settings', 'apply_settings')] if config else []})
    history_status = 'No database evidence available; import status is not assumed.'
    if (store.paths()['data'] / 'usage.sqlite3').exists():
        try:
            conn = store.readonly()
            try:
                row = conn.execute("SELECT value FROM metadata WHERE key='history_import_complete'").fetchone()
                count = conn.execute('SELECT COALESCE(SUM(count), 0) FROM undated_history').fetchone()[0]
                history_status = f"History: {'Imported' if row and row[0] else 'Not imported'} · completed: {row[0] if row and row[0] else 'Unknown'} · undated mentions: {count:,}"
            finally:
                conn.close()
        except Exception:
            history_status = 'History status unavailable; database was not reset.'
    try:
        setup_state = setup.status()
        setup_error = setup_state.get('error')
        summary = error_message(setup_error) if setup_error else ('Setup complete.' if setup_state.get('ok') else 'Setup has not completed.')
        stage_items = [{'label': key.replace('_', ' ').capitalize(), 'value': state_label(setup_state.get('stages', {}).get(key))}
                       for key in setup.STAGES]
        source_items = []
        sources = setup_state.get('sources', {})
        for key in store.SOURCES:
            source = sources.get(key)
            if isinstance(source, dict):
                item = {'label': key.replace('_', ' ').capitalize(), 'value': state_label(source.get('state'))}
                if source.get('error_code'):
                    item['note'] = error_message(source['error_code'])
                source_items.append(item)
    except Exception:
        setup_state = {}
        summary = 'Setup status unavailable; no setup was started.'
        stage_items, source_items = [], []
    path = selected_history(req, setup_state)
    sections.append({'title': 'Bash history', 'subtitle': history_status,
                     'items': [{'label': 'Selected history source', 'value': path or 'Default history source'}],
                     'help': 'A completed one-time import is not replayed after clearing. Retry pauses collection transactionally and restores its previous state. Timestamped history can produce dated mentions; untimestamped mentions are separate and do not enter date rankings.',
                     'buttons': [action('Choose history file…', 'choose_file'),
                                 action('Use default history source', 'reset_history_file'),
                                 action('Retry one-time history import', 'import_history')]})
    sections.append({'title': 'Local tracking setup', 'subtitle': summary,
                     'tone': 'ok' if setup_state.get('ok') else 'warn', 'items': stage_items,
                     'help': 'Each stage reports its last saved setup outcome. Previewing setup makes no changes.',
                     'buttons': [action('Preview selected setup…', 'setup_preview'), action('Enable local tracking…', 'setup_confirm')]})
    if source_items:
        sections.append({'title': 'Source status', 'items': source_items,
                         'help': 'Last recorded status, not a guarantee that a source is currently reachable.'})
    sections.append({'title': 'Destructive actions', 'tone': 'danger',
                     'help': 'Clearing history and removing the backend are separate operations, each requiring explicit confirmation. Backend removal retains usage and settings.',
                     'buttons': [action('Clear usage history…', 'clear_preview', tone='danger'),
                                 action('Remove local backend…', 'remove_preview', tone='danger')]})
    return {'title': 'Settings', 'subtitle': 'Local collection and consent', 'sections': sections}


def setup_description(value):
    return ('Exact managed paths:\n' + '\n'.join(value['paths']) + '\n\nCollection scopes:\n' + '\n'.join(value['scopes'])
            + '\n\nBash history source: ' + value['history_file'] + '\n\nPrivacy limits: ' + value['privacy']
            + '\n\nBar placement: ' + value['bar_placement'])

def result_notice(name, value):
    if value.get('ok'):
        if value.get('status') == 'already_imported':
            return {'notice': 'History import was already completed; no history was reread.'}
        message = name + ' completed.'
        if value.get('status') == 'imported' and isinstance(value.get('skipped'), int):
            message += ' Skipped history entries: ' + str(value['skipped']) + '.'
        return {'notice': message}
    message = name + ' did not complete. ' + error_message(value.get('error'))
    stages = value.get('stages')
    if isinstance(stages, dict):
        message += ' Stages: ' + ', '.join(key + ': ' + state_label(stages.get(key)) for key in setup.STAGES) + '.'
    sources = value.get('sources')
    if isinstance(sources, dict):
        message += ' Sources: ' + ', '.join(key + ': ' + state_label(sources[key].get('state'))
                                          for key in store.SOURCES if isinstance(sources.get(key), dict)) + '.'
    return {'notice': message, 'tone': 'warn'}


def handle(req):
    op = req.get('op')
    if op == 'page' and req.get('page') == 'settings':
        return settings_page(req)
    if op == 'control':
        control = req.get('control')
        if control not in ('pause', 'resume'):
            raise ValueError('invalid control')
        return result_notice(control.capitalize(), client.request({'op': 'control', 'action': control}, timeout=TIMEOUT))
    if op == 'apply_settings':
        config = store.load_config()
        values = filters(req)
        settings = {}
        for key, choices in store.CHOICES.items():
            value = values.get(key, str(config[key]))
            if not isinstance(value, str) or value not in tuple(map(str, choices)):
                raise ValueError('invalid setting')
            settings[key] = int(value)
        return result_notice('Apply settings', client.request({'op': 'control', 'action': 'set_settings', 'settings': settings}, timeout=TIMEOUT))
    if op == 'clear_preview':
        return dialog('Clear all recorded usage history?', 'Deletes daily app, command, and service usage, undated imported counts, source coverage intervals and days, and first/last dated observation evidence. Inventory, collector status, settings, and any completed import marker are retained. If the one-time import was completed, it will not be replayed after clearing. The daemon pauses the writer during the transaction and resumes only if collection was enabled.', [action('Clear usage history', 'clear_confirm', tone='danger')], tone='danger')
    if op == 'clear_confirm':
        return result_notice('Clear usage history', client.request({'op': 'control', 'action': 'clear_history'}, timeout=TIMEOUT))
    if op == 'import_history':
        from .cli import import_history
        return result_notice('One-time history import', import_history(selected_history(req)))
    if op in ('setup_preview', 'setup_confirm'):
        preview = setup.preview(history_file=selected_history(req))
        buttons = []
        if op == 'setup_confirm':
            buttons = [action('Enable local tracking', 'setup_enable', history_file=preview['history_file'], preview_hash=fingerprint(preview))]
        body = setup_description(preview)
        if buttons:
            body += '\n\nThis starts the user service paused, imports history once, preserves existing widget placement or adds Usage to the right bar section, then resumes collection. No shell startup files are changed. Only proceed if you consent to these exact local changes.'
        return dialog('Enable local tracking?' if buttons else 'Local setup preview · no changes yet', body, buttons)
    if op == 'setup_enable':
        history = req.get('history_file')
        if not isinstance(history, str) or fingerprint(setup.preview(history_file=history)) != req.get('preview_hash'):
            return dialog('Setup preview changed', 'No setup was started. Preview the exact changes again.', tone='warn')
        return result_notice('Enable local tracking', setup.enable_tracking(history_file=history))
    if op == 'remove_preview':
        value = uninstall.preview()
        if not value.get('ok'):
            return dialog('Backend removal blocked', 'The backend could not be verified. No cleanup was performed. ' + removal_error(value), tone='warn')
        body = ('Remove these verified backend paths:\n' + '\n'.join(value['paths']) + '\n\nService links:\n' + '\n'.join(value['service_links']) + '\n\nLegacy Bash block: ' + (value['bash_startup'] or 'None') + '\n\n' + value['service'] + '\n\n' + '\n'.join(value['scopes']) + '\n\nRetained:\n' + '\n'.join(value['retained']) + '\n\nThe dashboard will close because its runtime is removed. ' + value['next_step'])
        return dialog('Remove local backend?', body, [action('Remove local backend', 'remove_confirm', preview_hash=fingerprint(value), tone='danger')], tone='danger')
    if op == 'remove_confirm':
        preview = uninstall.preview()
        if not preview.get('ok') or fingerprint(preview) != req.get('preview_hash'):
            return dialog('Removal preview changed', 'No cleanup was performed. Preview the exact changes again.', tone='warn')
        result = uninstall.remove_backend()
        if result.get('ok') or result.get('runtime_removed'):
            message = ('Usage and settings were retained; collection is off. ' + result['next_step']
                       if result.get('ok') else 'The runtime was removed, but cleanup needs attention. ' + removal_error(result))
            return {'title': 'Local backend removed' if result.get('ok') else 'Backend cleanup incomplete', 'sections': [{'title': 'Next step', 'subtitle': message}], 'close': True}
        return dialog('Backend removal incomplete', 'Cleanup did not complete; some stages may have changed. ' + removal_error(result), tone='warn')
    if op == 'tracked_terminal':
        from . import installer
        target = installer.locations()
        path = target['launcher']
        installer.assert_owned(path, installer.read_manifest())
        if not path.is_file():
            return dialog('Tracked terminal unavailable', 'Update the local backend to restore its launcher.', tone='warn')
        home = str(Path.home())
        subprocess.Popen(['xdg-terminal-exec', '--dir=' + home, '--', str(path), 'tracked-shell'], cwd=home, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {'notice': 'Tracked terminal launched. Command names are counted only when collection is enabled.'}
    if op == 'research_preview':
        tool = req.get('tool') or filters(req).get('tool')
        if not tool:
            return dialog('Select tool for research', 'Enter a canonical public tool name. Do not include private filenames, history, account names, or usage evidence.', [action('Enter tool name…', 'input', key='tool', next={'op': 'research_preview'})])
        query = recommendations.research_query(tool, req.get('use_case'))
        body = ('Exact query:\n' + query['query'] + '\n\nDestination:\n' + query['destination'] + '\n\nExact URL:\n' + query['url'] + '\n\nOnly this selected tool / use-case query is disclosed. No inventory, counts, timestamps, or history are included. Results are not fetched back by this application.')
        open_fields = {'url': query['url'], 'tool': tool}
        if req.get('use_case') is not None:
            open_fields['use_case'] = req['use_case']
        return dialog('Send this research query to your browser?', body, [action('Open browser', 'open_url', **open_fields)])
    if op == 'link_preview':
        url = req.get('url')
        if url not in {row['source_url'] for row in recommendations.catalog()}:
            raise ValueError('invalid destination')
        return dialog('Open external source?', 'This opens your browser. No usage evidence is sent by Oma Usage.\n\nDestination: ' + url, [action('Open source', 'open_url', url=url)])
    if op == 'open_url':
        url = req.get('url')
        allowed = {row['source_url'] for row in recommendations.catalog()}
        if req.get('tool'):
            allowed.add(recommendations.research_query(req['tool'], req.get('use_case'))['url'])
        if not isinstance(url, str) or url not in allowed:
            raise ValueError('invalid destination')
        return {'url': url}
    return None
