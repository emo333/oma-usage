"""Directed, sourced comparisons. No network or browser work happens here."""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
import unicodedata
from urllib.parse import urlencode


@lru_cache(maxsize=1)
def catalog() -> list[dict]:
    return json.loads(files('oma_usage').joinpath('data/alternatives.json').read_text(encoding='utf-8'))


def _matching(identity, report):
    packages = {p['id'] for p in report['package_review']['inventory'] if p['key']==identity['package']}
    rows = [r for r in report['commands']['rows'] if r['key'] in identity['commands']]
    rows += [r for r in report['apps']['rows'] if r['owner_package_id'] in packages]
    if identity.get('include_owned_services'):
        rows += [r for r in report['services']['rows'] if r['owner_package_id'] in packages]
    return rows


def _positive(row):
    return row['count'] > 0 if row['kind']=='command' else row['seconds'] > 0


def _presence(identity, report):
    if any(p['key']==identity['package'] and p['installed'] for p in report['package_review']['inventory']):
        return 'Already installed'
    if any(r['installed'] and r['key'] in identity['commands'] for r in report['commands']['rows']):
        return 'Already installed'
    inventory = report['inventory']
    if inventory['ownership_complete'] and inventory['commands_complete'] and not inventory['errors'].get('packages'):
        return 'Not installed'
    return 'Installation status unknown'


def _usage(rows):
    if any(_positive(r) for r in rows):
        return 'Observed'
    if any(r.get('undated_count',0) for r in rows):
        return 'Undated history only'
    if any(r['usage_state']=='Not observed' for r in rows):
        return 'Not observed'
    return 'Insufficient coverage'


def build_recommendations(report: dict, *, observed_only=True) -> list[dict]:
    result = []
    for comparison in catalog():
        source_rows = _matching(comparison['source'],report)
        if observed_only and not any(_positive(row) for row in source_rows):
            continue
        target_rows = _matching(comparison['target'],report)
        installation = _presence(comparison['target'],report)
        usage = _usage(target_rows)
        order = (1 if any(_positive(r) for r in target_rows) else 0) if installation=='Already installed' else 2 if installation=='Installation status unknown' else 3
        result.append({**comparison,'installation':installation,'usage_state':usage,
                       'undated_count':sum(r.get('undated_count',0) for r in target_rows),
                       'source_observed':any(_positive(r) for r in source_rows),'order':order})
    return sorted(result,key=lambda r:(r['use_case'],r['order'],r['id']))


def comparisons_for(entity: dict, report: dict) -> list[dict]:
    package = entity['key'] if entity['kind']=='package' else next((p['key'] for p in report['package_review']['inventory'] if p['id']==entity.get('owner_package_id')),None)
    return [r for r in build_recommendations(report,observed_only=False)
            if (entity['kind']=='command' and entity['key'] in r['source']['commands'])
            or (entity['kind']!='command' and package==r['source']['package'])]


def command_use_cases(command_rows) -> list[dict]:
    # A name can belong to multiple explicit families. No inference from arguments.
    names = {}
    for comparison in catalog():
        for side in ('source','target'):
            for name in comparison[side]['commands']:
                names.setdefault(comparison['use_case'],set()).add(name)
    return [dict(use_case=use_case,count=sum(r['count'] for r in command_rows if r['key'] in commands))
            for use_case,commands in sorted(names.items())]


def research_query(tool: str, use_case: str | None = None) -> dict:
    if (not isinstance(tool,str) or not tool.strip() or len(tool.encode('utf-8')) > 255
        or any(unicodedata.category(c).startswith('C') for c in tool)):
        raise ValueError('Select a canonical tool name without control characters')
    known = {row['use_case'] for row in catalog()}
    if use_case is not None and use_case not in known:
        raise ValueError('Unknown catalog use case')
    query = ' '.join([tool.strip()]+([use_case] if use_case else [])+['alternatives','Linux'])
    return dict(query=query,url='https://duckduckgo.com/?'+urlencode({'q':query}),destination='https://duckduckgo.com/')
