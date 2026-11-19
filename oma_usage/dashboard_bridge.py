"""One-request, one-reply bridge for the native dashboard; never emits tracebacks."""
from __future__ import annotations

import json
import sys

from . import client, dashboard_actions


def dispatch(request):
    if not isinstance(request, dict) or not isinstance(request.get('op'), str):
        raise ValueError('invalid request')
    if request['op'] == 'page' and request.get('page') != 'settings':
        from . import dashboard_reports
        return dashboard_reports.page(request)
    result = dashboard_actions.handle(request)
    if result is not None:
        return result
    from . import dashboard_reports
    return dashboard_reports.detail(request)


def main():
    try:
        raw = sys.stdin.buffer.read(client.MAX_FRAME + 1)
        if len(raw) > client.MAX_FRAME:
            raise ValueError('request too large')
        response = dispatch(json.loads(raw))
        if not isinstance(response, dict):
            raise ValueError('invalid response')
        encoded = json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    except client.ClientError as exc:
        message = ('The collector did not acknowledge the request. Check the current state before retrying; it may have completed late.'
                   if exc.code == 'collector_timeout' else 'The collector could not complete the request. Refresh to check its current state before retrying.')
        encoded = json.dumps({'notice': message, 'tone': 'warn'})
    except Exception:
        encoded = json.dumps({'title': 'Request unavailable', 'tone': 'warn', 'sections': [{'title': 'Unable to complete request', 'subtitle': 'No details are available. Refresh and check the current state before retrying.'}]})
    sys.stdout.write(encoded + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
