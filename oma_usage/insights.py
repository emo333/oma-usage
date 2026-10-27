"""Pure read-only reports. Counts, foreground estimates, and runtime stay separate."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from .store import DEFAULTS, SOURCES, split_days, stamp

UTC = timezone.utc
METRICS = {'app':'seconds','command':'count','service':'seconds'}
DEFINITIONS = {
    'app': 'Estimated non-idle foreground attention, sampled every five seconds. Not background runtime or website activity.',
    'command': 'History-visible submitted syntactic mentions, not successful executions or loop iterations. Undated imports are separate.',
    'service': 'Observed running time and observed starts, not user attention. Active/exited oneshots do not accrue running time.',
}
BOUNDARIES = ('History omissions, aliases/functions, remote activity, scripts, Flatpaks, AppImages, and language-managed software are visible only through discovered launcher or command identities. Inventory is not usage. Browser focus reveals no websites. Service transitions faster than observation can be missed.')


def _time(value):
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _source(entity):
    if entity['kind'] == 'app':
        return 'app_focus'
    if entity['kind'] == 'command':
        return 'bash_live'
    return 'service_user' if entity['key'].startswith('user:') else 'service_system'


def _union(intervals):
    merged = []
    for left,right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0],max(right,merged[-1][1]))
        else:
            merged.append((left,right))
    return merged


def _coverage(intervals, source, start, end):
    return [(max(start,a),min(end,b)) for a,b in intervals.get(source,[]) if b >= start and a <= end]


def _qualifying(intervals, source, start, end):
    pieces = _coverage(intervals,source,start,end)
    if source == 'bash_live':
        return len({a.date() for a,b in pieces if a == b})
    totals = defaultdict(float)
    for a,b in _union(pieces):
        for left,right in split_days(a,b):
            totals[left.date()] += (right-left).total_seconds()
    return sum(seconds >= 1800 for seconds in totals.values())


def _has_coverage(intervals, entity, start, end):
    presence = _time(entity['present_since'])
    if presence is None:
        return False
    source = _source(entity)
    return any((a == b if source == 'bash_live' else b > a)
               for a,b in _coverage(intervals,source,max(start,presence),end))


def build_report(conn: sqlite3.Connection, *, days: int, now: datetime) -> dict:
    if days not in (7,30,90,365) or now.tzinfo is None:
        raise ValueError('A supported period and timezone-aware current time are required')
    now = now.astimezone(UTC)
    start = datetime.combine(now.date()-timedelta(days=days-1),datetime.min.time(),UTC)
    reader = conn.cursor()
    reader.row_factory = sqlite3.Row
    metadata = dict(reader.execute('SELECT key,value FROM metadata'))
    settings = {**DEFAULTS,**json.loads(metadata.get('settings','{}'))}
    entities = {row['id']:dict(row) for row in reader.execute('SELECT * FROM entities')}
    intervals = defaultdict(list)
    for row in reader.execute('SELECT source,start_at,end_at FROM coverage_intervals'):
        intervals[row['source']].append((_time(row['start_at']),_time(row['end_at'])))
    usage = defaultdict(lambda: dict(count=0,seconds=0.0))
    retained = defaultdict(lambda: dict(count=0,seconds=0.0))
    for row in reader.execute('SELECT entity_id,day,count,seconds FROM daily_usage'):
        target = retained[row['entity_id']]
        target['count'] += row['count']
        target['seconds'] += row['seconds']
        if start.date().isoformat() <= row['day'] <= now.date().isoformat():
            target = usage[row['entity_id']]
            target['count'] += row['count']
            target['seconds'] += row['seconds']
    undated = dict(reader.execute('SELECT entity_id,count FROM undated_history'))
    package_details = {r['entity_id']:dict(r) for r in reader.execute('SELECT * FROM package_details')}
    service_details = {r['entity_id']:dict(r) for r in reader.execute('SELECT * FROM service_details')}
    sections = {}
    categories = defaultdict(float)
    for kind,name in (('app','apps'),('command','commands'),('service','services')):
        rows = []
        for entity_id,entity in entities.items():
            if entity['kind'] != kind:
                continue
            row = {**entity,**usage[entity_id], 'undated_count':undated.get(entity_id,0),
                   'retained_count':retained[entity_id]['count'], 'retained_seconds':retained[entity_id]['seconds'],
                   'source':_source(entity),'definition':DEFINITIONS[kind]}
            row.update(service_details.get(entity_id,{}))
            row['template'] = kind == 'service' and entity['key'].endswith('@.service')
            metric = row[METRICS[kind]]
            row['usage_state'] = ('Template (not ranked)' if row['template'] else 'Observed' if metric > 0 else 'Not observed' if _has_coverage(intervals,entity,start,now) else 'Insufficient coverage')
            rows.append(row)
            if kind == 'app':
                categories[row['category']] += row['seconds']
        positive = [r for r in rows if r[METRICS[kind]] > 0 and not r['template']]
        sections[name] = dict(rows=sorted(rows,key=lambda r:r['key']),
            most=sorted(positive,key=lambda r:(-r[METRICS[kind]],r['key'])),
            least=sorted(positive,key=lambda r:(r[METRICS[kind]],r['key'])),
            zero=sorted((r for r in rows if r[METRICS[kind]] == 0 and r['installed'] and not r['template']),key=lambda r:r['key']),
            metric=METRICS[kind],definition=DEFINITIONS[kind])
    stale_start = now-timedelta(days=settings['stale_days'])
    children = defaultdict(list)
    for entity in entities.values():
        if entity['owner_package_id'] is not None:
            children[entity['owner_package_id']].append(entity)
    review = []
    package_rows = []
    ownership_complete = metadata.get('inventory_ownership_complete') == 'true'
    for entity_id,package in entities.items():
        if package['kind'] != 'package':
            continue
        mapped = children[entity_id]
        current = [e for e in mapped if e['installed'] and not (e['kind']=='service' and e['key'].endswith('@.service'))]
        row = {**package,**package_details.get(entity_id,{}),'entry_points':current}
        for field in ('required_by','optional_for'):
            row[field] = json.loads(row.get(field+'_json') or '[]')
        row['enabled_services'] = [e['key'] for e in current if e['kind']=='service' and service_details.get(e['id'],{}).get('unit_file_state') in ('enabled','enabled-runtime','linked','linked-runtime')]
        row['running_services'] = [e['key'] for e in mapped if e['kind']=='service' and service_details.get(e['id'],{}).get('sub_state') == 'running']
        row['undated_count'] = sum(undated.get(e['id'],0) for e in mapped)
        last = max((e['last_observed_at'] for e in mapped if e['last_observed_at']),default=None)
        last_at = _time(last)
        row['last_observed_at'] = last
        row['retained_command_mentions'] = sum(retained[e['id']]['count'] for e in mapped if e['kind']=='command')
        row['retained_focus_sessions'] = sum(retained[e['id']]['count'] for e in mapped if e['kind']=='app')
        row['retained_focus_seconds'] = sum(retained[e['id']]['seconds'] for e in mapped if e['kind']=='app')
        row['retained_service_seconds'] = sum(retained[e['id']]['seconds'] for e in mapped if e['kind']=='service')
        row['retained_service_starts'] = sum(retained[e['id']]['count'] for e in mapped if e['kind']=='service')
        row['few_recorded_uses'] = any(1 <= row[field] <= 5 for field in ('retained_command_mentions','retained_focus_sessions'))
        sources = sorted({_source(e) for e in current})
        presence = _time(package['present_since'])
        row['coverage_days'] = {source:_qualifying(intervals,source,max(stale_start,presence or now),now) for source in sources}
        if not package['installed']:
            reason = 'Not currently installed'
        elif not current:
            reason = 'No tracked entry point'
        elif not package['explicit_install']:
            reason = 'Dependency-installed package'
        elif not ownership_complete:
            reason = 'Ownership inventory incomplete'
        elif row['running_services'] or (last_at is not None and last_at >= stale_start):
            reason = 'Observed recently'
        elif presence is None or presence > stale_start:
            reason = 'Recently inventoried'
        elif any(count < 30 for count in row['coverage_days'].values()):
            reason = 'Insufficient coverage for review'
        else:
            reason = 'Not observed recently'
        row['review_state'] = reason
        package_rows.append(row)
        if reason == 'Not observed recently':
            review.append(row)
    coverage = []
    statuses = {r['source']:dict(r) for r in reader.execute('SELECT * FROM collector_status')}
    for source in SOURCES:
        pieces = _coverage(intervals,source,start,now)
        merged = _union(pieces)
        gaps = []
        if source not in ('bash_live','bash_import'):
            cursor = start
            for a,b in merged:
                if a > cursor:
                    gaps.append(dict(start_at=stamp(cursor),end_at=stamp(a)))
                cursor = max(cursor,b)
            if cursor < now:
                gaps.append(dict(start_at=stamp(cursor),end_at=stamp(now)))
        coverage.append(dict(source=source,**{k:v for k,v in statuses.get(source,dict(state='unavailable',last_success_at=None,error_code='not_started')).items() if k!='source'},
            covered_seconds=sum((b-a).total_seconds() for a,b in merged),
            observations=reader.execute('SELECT coalesce(sum(observations),0) FROM source_days WHERE source=? AND day>=? AND day<=?',(source,start.date().isoformat(),now.date().isoformat())).fetchone()[0],
            first_coverage_at=metadata.get('coverage_started:'+source) or (stamp(min(a for a,b in intervals[source])) if intervals[source] else None),
            qualifying_days=_qualifying(intervals,source,start,now),gaps=gaps,
            definition='Prompt point observations, not continuous coverage' if source=='bash_live' else 'Imported evidence, not collector availability' if source=='bash_import' else 'Collector availability, independent of software usage'))
    from .recommendations import command_use_cases
    return dict(period=dict(days=days,start_at=stamp(start),end_at=stamp(now)),settings=settings,**sections,
                package_review=dict(candidates=sorted(review,key=lambda r:r['key']),inventory=sorted(package_rows,key=lambda r:r['key'])),
                categories=[dict(category=category,seconds=seconds) for category,seconds in sorted(categories.items()) if seconds > 0],
                command_use_cases=command_use_cases(sections['commands']['rows']),
                source_coverage=coverage,history=dict(state='Imported' if metadata.get('history_import_complete') else 'Not imported',completed_at=metadata.get('history_import_complete'),undated_count=sum(undated.values())),
                inventory=dict(at=metadata.get('inventory_at'),ownership_complete=ownership_complete,
                               commands_complete=metadata.get('inventory_commands_complete')=='true',
                               errors=json.loads(metadata.get('inventory_errors','{}'))),boundaries=BOUNDARIES)
