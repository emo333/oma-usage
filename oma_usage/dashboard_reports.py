"""Read-only, plain-text dashboard reports for the native frontend."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from . import client, insights, inventory, recommendations, store

PERIODS = (7, 30, 90, 365)
COLLECTOR_TIMEOUT = 2.0
PAGE_SIZE = 40
SOURCE_LABELS = {'app_focus': 'Application focus', 'bash_live': 'Tracked terminals',
                 'bash_import': 'Bash history import', 'service_user': 'User services',
                 'service_system': 'System services'}


def error_code(error):
    """Do not expose subprocess output, parser input, or exception text."""
    return str(getattr(error, 'code', None) or type(error).__name__)


def load_snapshot(days=30):
    """Read local evidence without creating state or initiating collection."""
    result: dict[str, Any] = {'report': None, 'errors': [],
              'observed_comparisons': [], 'all_comparisons': [
                  {**row, 'installation': 'Installation status unknown',
                   'usage_state': 'Insufficient coverage', 'undated_count': None,
                   'source_observed': None} for row in recommendations.catalog()]}
    database = store.paths()['data'] / 'usage.sqlite3'
    if database.exists():
        try:
            conn = store.readonly()
            try:
                result['report'] = insights.build_report(conn, days=days, now=datetime.now(timezone.utc))
            finally:
                conn.close()
            result['observed_comparisons'] = recommendations.build_recommendations(result['report'])
            result['all_comparisons'] = recommendations.build_recommendations(result['report'], observed_only=False)
        except Exception as exc:
            result['errors'].append('Database unavailable: ' + error_code(exc) + '. Check schema, permissions, and writer availability; the database has not been reset.')
    try:
        result['enabled'] = client.observer_settings(timeout=COLLECTOR_TIMEOUT)['collection_enabled']
    except client.ClientError as exc:
        if exc.code == 'collector_timeout':
            result['errors'].append('Collector did not reply in time. Refresh to check its live state.')
        else:
            result['errors'].append('Cannot reach the collector. Check collection in Settings.')
    return result


def text(value):
    if value is None:
        return 'Unknown / no dated evidence'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def duration(value):
    seconds = float(value or 0)
    if seconds < 60:
        return f'{seconds:,.1f} s'
    if seconds < 3600:
        return f'{seconds / 60:,.1f} min'
    return f'{seconds / 3600:,.1f} h'


def action(label, op, **kwargs):
    return {'label': label, 'request': {'op': op, **kwargs}}


def option(label, key, values):
    return {'label': label, 'key': key, 'values': [{'label': title, 'value': value} for title, value in values]}


def _days(req):
    days = req.get('days', 30)
    return days if days in PERIODS else 30


def _filters(req):
    value = req.get('filters')
    return value if isinstance(value, dict) else {}


def _offset(req):
    value = req.get('offset', 0)
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _paginate(rows, req, page_name):
    offset = _offset(req)
    if rows:
        offset = min(offset, (len(rows) - 1) // PAGE_SIZE * PAGE_SIZE)
    else:
        offset = 0
    buttons = []
    if offset:
        buttons.append(action('Previous page', 'page', page=page_name, offset=max(0, offset - PAGE_SIZE)))
    if offset + PAGE_SIZE < len(rows):
        buttons.append(action('Next page', 'page', page=page_name, offset=offset + PAGE_SIZE))
    return rows[offset:offset + PAGE_SIZE], buttons, f'{offset + 1 if rows else 0}–{min(offset + PAGE_SIZE, len(rows))} of {len(rows):,}'

def _search(req, page_name, rows, fields):
    term = _filters(req).get('search', '')
    term = term if isinstance(term, str) else ''
    needle = term.casefold()
    if needle:
        rows = [row for row in rows if any(needle in str(field(row) or '').casefold() for field in fields)]
    label = 'Search: ' + term if term else 'Search…'
    return rows, action(label, 'input', key='search', next={'op': 'page', 'page': page_name})


def _highlights(report):
    highlights = []
    for name, label, field in [('apps', 'Focused time', 'seconds'),
                               ('commands', 'Dated command mentions', 'count'),
                               ('services', 'Observed service runtime', 'seconds')]:
        total = sum(row[field] for row in report[name]['most'])
        if total > 0:
            highlights.append({'label': label, 'value': f'{total:,} mentions' if field == 'count' else duration(total),
                               'note': {'apps': 'Active foreground windows', 'commands': 'Timestamped mentions only',
                                        'services': 'Summed across services'}[name],
                               'action': action('View evidence', 'page', page=name)})
    return highlights


def _entity_action(row):
    return action('View evidence', 'entity_detail', kind=row['kind'], key=row['key'])


def _status(snapshot):
    errors = list(snapshot['errors'])
    report = snapshot['report']
    if report:
        unavailable = [s for s in report['source_coverage'] if s['state'] in ('error', 'unavailable') or s.get('error_code')]
        if any(s.get('error_code') == 'lock_state_unknown' for s in unavailable):
            errors.append('App focus is paused until the collector can confirm the screen is unlocked.')
        elif unavailable and not snapshot['errors']:
            errors.append('Some sources are not reporting. Open Source health on Overview for details.')
        errors.extend(f'Inventory {source}: {text(code)}' for source, code in report['inventory']['errors'].items() if code)
    if errors:
        return {'status': '\n'.join(errors), 'tone': 'warn'}
    if snapshot.get('enabled') is False:
        return {'status': 'Collection paused. Previously recorded activity is still available.', 'tone': 'warn'}
    return {'status': 'Collection enabled. Reports stay on this device.', 'tone': 'ok'}


def page(req):
    name = req.get('page', 'overview')
    if name not in ('overview', 'apps', 'commands', 'services', 'review', 'alternatives'):
        raise ValueError('Unknown report page')
    snapshot = load_snapshot(_days(req))
    report = snapshot['report']
    titles = {'overview': 'Overview', 'apps': 'Applications', 'commands': 'Commands',
              'services': 'Services', 'review': 'Package review', 'alternatives': 'Alternatives'}
    subtitles = {'overview': 'Your activity at a glance.', 'apps': 'Which apps had your attention?',
                 'commands': 'Which commands appeared in your Bash history?',
                 'services': 'Which services were observed running?',
                 'review': 'Which installed packages have enough evidence to review?',
                 'alternatives': 'Compare tools for the same task, using the offline catalog.'}
    result: dict[str, Any] = {'title': titles[name], 'subtitle': subtitles[name], **_status(snapshot), 'sections': []}
    if name == 'overview':
        result['sections'] = _overview(report)
        if report:
            period = report['period']
            result['subtitle'] = f"Activity from {period['start_at'][:10]} through {period['end_at'][:10]} (UTC)."
            result['highlights'] = _highlights(report)
    elif name in ('apps', 'commands', 'services'):
        result['sections'] = _metrics(name, report, req)
    elif name == 'review':
        result['sections'] = _review(report, req)
    else:
        result['sections'] = _alternatives(snapshot, req)
    return result


def _overview(report):
    if not report or not (report['package_review']['inventory'] or any(report[n]['rows'] for n in ('apps', 'commands', 'services'))):
        opening = 'The usage database has no inventoried identities yet.' if report else 'No readable usage database.'
        return [{'title': 'Getting started', 'help': 'Opening the dashboard does not start collection or import history.',
                 'subtitle': opening + ' Review local setup in Settings before enabling tracking. Existing database errors are never reset.',
                 'buttons': [action('Open Settings', 'page', page='settings')]}]
    sections: list[dict[str, Any]] = []
    for name, title in [('apps', 'Leading applications'), ('commands', 'Leading commands'), ('services', 'Service activity')]:
        rows = report[name]['most'][:5]
        sections.append({'title': title, 'help': report[name]['definition'],
                         'items': [{'label': r['label'], 'value': f"{r['count']:,} mentions" if name == 'commands' else duration(r['seconds']),
                                    'action': _entity_action(r)} for r in rows],
                         'subtitle': '' if rows else 'No positive dated evidence in this period.',
                         'buttons': [action('View all', 'page', page=name)]})
    sections.append({'title': 'Application categories', 'help': 'Estimated focused time, not background runtime.',
                     'items': [{'label': text(r['category']), 'value': duration(r['seconds'])} for r in report['categories']],
                     'subtitle': '' if report['categories'] else 'No category focus evidence.'})
    sections.append({'title': 'Curated command use cases', 'help': 'Dated mentions of catalog command identities; arguments are not inferred.',
                     'items': [{'label': r['use_case'], 'value': f"{r['count']:,} mentions"} for r in report['command_use_cases']]})
    sections.append({'title': 'Package review', 'subtitle': f"{len(report['package_review']['candidates']):,} candidates · not a removal recommendation",
                     'buttons': [action('Review packages', 'page', page='review')]})
    sections.append({'title': 'Source health and coverage', 'help': 'Availability is not usage; gaps are limited to the selected window.',
                     'items': [{'label': SOURCE_LABELS.get(s['source'], s['source']), 'value': s['state'],
                                'tone': 'ok' if s['state'] == 'collecting' and not s.get('error_code') else 'warn',
                                'note': f"{duration(s['covered_seconds'])} · {s['observations']:,} observations · {s['qualifying_days']} qualifying days · {len(s['gaps'])} gaps · last success: {text(s['last_success_at'])}" + (f" · error: {s['error_code']}" if s.get('error_code') else ''),
                                'action': action('View coverage', 'coverage_detail', source=s['source'])} for s in report['source_coverage']]})
    sections.append({'title': 'Inventory and limitations', 'subtitle': f"Inventory refreshed: {text(report['inventory']['at'])}\n{report['boundaries']}",
                     'help': 'Inventory is not usage. Missing data does not imply a tool was never used.'})
    return sections


def _metrics(name, report, req):
    kind = {'apps': 'app', 'commands': 'command', 'services': 'service'}[name]
    sections: list[dict[str, Any]] = [{'title': 'About this evidence', 'subtitle': insights.DEFINITIONS[kind],
                 'help': 'Rankings use positive dated evidence in the selected period; zero is not proof of non-use.'}]
    if name == 'commands':
        sections[0]['subtitle'] += ' Live counts come only from tracked Bash terminals when Bash returns to its prompt.'
        sections[0]['buttons'] = [action('Open tracked terminal', 'tracked_terminal')]
    filters = _filters(req)
    mode = filters.get('mode', 'most')
    sort = filters.get('sort', 'name')
    data = report[name] if report else {'rows': [], 'most': [], 'least': [], 'zero': [], 'metric': insights.METRICS[kind]}
    rows = list(data.get({'all': 'rows', 'most': 'most', 'least': 'least', 'unobserved': 'zero'}.get(mode, 'rows'), []))
    rows, search = _search(req, name, rows, [lambda r: r['label'], lambda r: r['key'], lambda r: r['usage_state']])
    if mode not in ('most', 'least'):
        if sort == 'most':
            rows.sort(key=lambda r: (-r[data['metric']], r['key']))
        elif sort == 'least':
            rows.sort(key=lambda r: (r[data['metric']], r['key']))
        elif sort == 'state':
            rows.sort(key=lambda r: (r['usage_state'], r['key']))
        else:
            rows.sort(key=lambda r: (str(r['label']).casefold(), r['key']))
    rows, buttons, summary = _paginate(rows, req, name)
    items = []
    for row in rows:
        metric = f"{row['count']:,} dated mentions" if kind == 'command' else duration(row['seconds'])
        evidence = [row['key'], row['usage_state']]
        if kind != 'command':
            evidence.append(f"{row['count']:,} " + ('focus sessions' if kind == 'app' else 'observed starts'))
        elif row['undated_count']:
            evidence.append(f"{row['undated_count']:,} undated mentions")
        if not row['installed']:
            evidence.append('Not currently installed')
        items.append({'label': text(row['label']), 'value': metric, 'note': ' · '.join(evidence),
                      'action': _entity_action(row)})
    if not items:
        summary += ' · No matching evidence. Try another filter or view all inventory.'
    options = [option('Show', 'mode', [('Most observed', 'most'), ('Least observed', 'least'), ('All inventory', 'all'), ('Installed without positive evidence', 'unobserved')])]
    if mode not in ('most', 'least'):
        options.append(option('Sort', 'sort', [('Name', 'name'), ('Most evidence', 'most'), ('Least evidence', 'least'), ('Evidence state', 'state')]))
    sections.append({'title': 'Evidence', 'subtitle': summary, 'items': items, 'buttons': [search] + buttons,
                     'options': options})
    return sections


def _review(report, req):
    filters = _filters(req)
    mode = filters.get('mode', 'candidates')
    data = report['package_review'] if report else {'candidates': [], 'inventory': []}
    rows = list(data['inventory' if mode == 'inventory' else 'candidates'])
    rows, search = _search(req, 'review', rows, [lambda r: r['key'], lambda r: r['review_state']])
    sort = filters.get('sort', 'name')
    if sort == 'state':
        rows.sort(key=lambda r: (r['review_state'], r['key']))
    elif sort == 'size':
        rows.sort(key=lambda r: (-(r.get('installed_size_bytes') or 0), r['key']))
    elif sort == 'last':
        rows.sort(key=lambda r: (r.get('last_observed_at') or '', r['key']))
    else:
        rows.sort(key=lambda r: r['key'])
    rows, buttons, summary = _paginate(rows, req, 'review')
    if not rows:
        summary += ' · No matching packages. Try another filter or view all inventory.'
    return [{'title': 'Package-level review', 'help': 'Every mapped source needs 30 qualifying days. Inventory is not usage.',
             'subtitle': 'Evidence for review only; not a removal recommendation. No uninstall or service-disabling actions.'},
            {'title': 'Packages', 'subtitle': summary, 'items': [
                {'label': r['key'], 'value': r['review_state'],
                 'note': f"Installed: {text(bool(r['installed']))} · explicit: {text(bool(r['explicit_install']))} · last observed: {text(r.get('last_observed_at'))} · installed bytes: {text(r.get('installed_size_bytes'))}",
                 'action': _entity_action(r)} for r in rows], 'buttons': [search] + buttons,
             'options': [option('Show', 'mode', [('Review candidates', 'candidates'), ('All package inventory', 'inventory')]),
                         option('Sort', 'sort', [('Name', 'name'), ('Review evidence', 'state'), ('Installed size', 'size'), ('Last observation', 'last')])]}]


def _alternatives(snapshot, req):
    filters = _filters(req)
    rows = snapshot['all_comparisons' if filters.get('mode') == 'all' else 'observed_comparisons']
    cases = sorted({r['use_case'] for r in recommendations.catalog()})
    use_case = filters.get('use_case', '')
    rows = [r for r in rows if not use_case or r['use_case'] == use_case]
    rows, search = _search(req, 'alternatives', rows, [lambda r: r['source']['package'], lambda r: r['target']['package'], lambda r: r['use_case']])
    sort = filters.get('sort', 'recommended')
    if sort == 'source':
        rows.sort(key=lambda r: (r['source']['package'], r['id']))
    elif sort == 'target':
        rows.sort(key=lambda r: (r['target']['package'], r['id']))
    rows, buttons, summary = _paginate(rows, req, 'alternatives')
    if not rows:
        summary += ' · No matching comparisons. Try all curated use cases or another filter.'
    return [{'title': 'Curated comparisons', 'help': 'Directed, locally reviewed comparisons, not universal rankings. No online requests are made here.',
             'subtitle': summary, 'items': [{'label': r['source']['package'] + ' → ' + r['target']['package'],
                'value': r['use_case'], 'note': f"{r['installation']} · {r['usage_state']} · undated mentions: {text(r['undated_count'])}",
                'action': action('View comparison', 'comparison_detail', id=r['id'])} for r in rows],
             'buttons': [search] + buttons + [action('Research a tool…', 'input', key='tool', next={'op': 'research_preview'})],
             'options': [option('Show', 'mode', [('Observed sources in this period', 'observed'), ('All curated use cases', 'all')]),
                         option('Use case', 'use_case', [('All use cases', '')] + [(c, c) for c in cases]),
                         option('Sort', 'sort', [('Curated order', 'recommended'), ('Source', 'source'), ('Alternative', 'target')])]}]


def detail(req):
    op = req.get('op')
    if op not in ('entity_detail', 'coverage_detail', 'comparison_detail'):
        raise ValueError('Unknown report detail')
    snapshot = load_snapshot(_days(req))
    report = snapshot['report']
    if op == 'comparison_detail':
        row = next((r for r in snapshot['all_comparisons'] if r['id'] == req.get('id')), None)
        if row is None:
            return {'title': 'Comparison unavailable', 'sections': [{'title': 'Not found', 'subtitle': 'This comparison is no longer in the catalog.'}]}
        fields = [('Use case', row['use_case']), ('Source tool', row['source']['package']), ('Alternative', row['target']['package']),
                  ('Installation', row['installation']), ('Actual target evidence', row['usage_state']), ('Undated target mentions', row['undated_count']),
                  ('Source observed in selected period', row['source_observed']), ('Advantage', row['advantage']), ('Tradeoff', row['tradeoff']),
                  ('Primary source link', row['source_url']), ('Locally reviewed on', row['reviewed_at'])]
        return {'title': 'Curated comparison', 'sections': [{'title': 'Comparison and evidence',
                'items': [{'label': k, 'value': text(v)} for k, v in fields],
                'buttons': [action('Open primary source link…', 'link_preview', url=row['source_url']),
                            action('Research newer alternatives…', 'research_preview', tool=row['source']['package'], use_case=row['use_case'])]}]}
    if not report:
        return {'title': 'Evidence unavailable', **_status(snapshot), 'sections': [{'title': 'No readable report', 'subtitle': 'Refresh to retry; no evidence was reset.'}]}
    if op == 'coverage_detail':
        row = next((r for r in report['source_coverage'] if r['source'] == req.get('source')), None)
        if row is None:
            raise ValueError('Unknown coverage source')
        fields = [('State', row['state']), ('Reliable observation began', row['first_coverage_at']),
                  ('Last success', row['last_success_at']),
                  ('Availability', f"{duration(row['covered_seconds'])} ({text(row['covered_seconds'])} s)"),
                  ('Observations', row['observations']), ('Qualifying days', row['qualifying_days'])]
        if row.get('error_code'):
            fields.insert(1, ('Error code', row['error_code']))
        return {'title': 'Source coverage · ' + row['source'], 'sections': [
            {'title': 'Coverage', 'help': row['definition'], 'items': [{'label': k, 'value': text(v)} for k, v in fields]},
            {'title': 'Gaps in selected window', 'help': 'Gaps do not establish whether software was used.',
             'subtitle': 'No gaps reported.' if not row['gaps'] else '',
             'items': [{'label': g['start_at'], 'value': g['end_at']} for g in row['gaps']]}]}
    kind, key = req.get('kind'), req.get('key')
    names = {'app': 'apps', 'command': 'commands', 'service': 'services', 'package': 'package_review'}
    if kind not in names or not isinstance(key, str):
        raise ValueError('Unknown entity identity')
    rows = report['package_review']['inventory'] if kind == 'package' else report[names[kind]]['rows']
    entity = next((r for r in rows if r['key'] == key), None)
    if entity is None:
        return {'title': 'Evidence unavailable', 'sections': [{'title': 'Identity not found', 'subtitle': 'This identity is no longer in the current report.'}]}
    return _entity_detail(entity, report)


def _entity_detail(entity, report):
    fields = [('key', 'Stable identity'), ('kind', 'Kind'), ('installed', 'Currently installed'),
              ('explicit_install', 'Explicitly installed'), ('review_state', 'Review evidence'), ('usage_state', 'Selected-period evidence'),
              ('first_observed_at', 'First dated observation'), ('last_observed_at', 'Last dated observation'),
              ('present_since', 'Continuously present since'), ('first_inventory_at', 'First inventoried'),
              ('count', 'Dated mentions / focus sessions / starts'), ('seconds', 'Selected-period focused / running seconds'),
              ('undated_count', 'Undated imported mentions'), ('retained_count', 'Retained dated count (not lifetime)'),
              ('retained_seconds', 'Retained seconds (not lifetime)'), ('retained_command_mentions', 'Retained command mentions'),
              ('retained_focus_sessions', 'Retained focus sessions'), ('retained_focus_seconds', 'Retained focused seconds'),
              ('retained_service_seconds', 'Retained service seconds'), ('retained_service_starts', 'Retained service starts'),
              ('coverage_days', 'Qualifying days by mapped source'), ('enabled_services', 'Enabled services'),
              ('running_services', 'Currently running services'), ('installed_size_bytes', 'Installed size in bytes'),
              ('install_update_at', 'Package install/update date (not original installation)'), ('required_by', 'Required by'),
              ('optional_for', 'Optional consumers'), ('unit_file_state', 'Unit file state'), ('active_state', 'Active state'),
              ('sub_state', 'Service substate'), ('template', 'Uninstantiated template'), ('category', 'Broad launcher category'), ('version', 'Package version')]
    items = [{'label': label, 'value': text(entity[key])} for key, label in fields if key in entity]
    if entity.get('entry_points') is not None:
        items.append({'label': 'Tracked entry points', 'value': text([e['key'] for e in entity['entry_points']])})
    if entity.get('few_recorded_uses') or (entity['kind'] in ('app', 'command') and 1 <= entity.get('retained_count', 0) <= 5):
        items.append({'label': 'Few recorded uses', 'value': 'Retained mentions or focus sessions, not lifetime counts.'})
    sections = [{'title': 'Recorded evidence', 'help': entity.get('definition') or 'Package evidence is not a lifetime total. Relationships are warnings, not permission to delete.', 'items': items}]
    package = entity if entity['kind'] == 'package' else next((p for p in report['package_review']['inventory'] if p['id'] == entity.get('owner_package_id')), None)
    if package:
        try:
            current = inventory.package_details(package['key'])
            sections.append({'title': 'Current pacman metadata', 'help': 'Read-only current metadata; stored relationships may be stale.',
                             'items': [{'label': str(k), 'value': text(v)} for k, v in current.items()]})
        except Exception as exc:
            sections.append({'title': 'Current pacman metadata', 'subtitle': 'Unavailable: ' + error_code(exc) + '. Stored relationships may be stale.'})
    else:
        sections.append({'title': 'Package ownership', 'subtitle': 'Unknown / not mapped.'})
    comparisons = recommendations.comparisons_for(entity, report)
    buttons = [action(c['use_case'] + ' → ' + c['target']['package'], 'comparison_detail', id=c['id']) for c in comparisons]
    if comparisons:
        buttons.append(action('Research newer alternatives…', 'research_preview', tool=comparisons[0]['source']['package'], use_case=comparisons[0]['use_case']))
    else:
        buttons.append(action('Research newer alternatives…', 'input', key='tool', next={'op': 'research_preview'}))
    sections.append({'title': 'Curated source comparisons', 'subtitle': '' if comparisons else 'No curated comparison yet.', 'buttons': buttons})
    return {'title': text(entity['label']) + ' · evidence', 'sections': sections}
