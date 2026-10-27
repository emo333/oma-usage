"""Lazy dashboard/collector entry points with no GTK imports in collectors."""
from __future__ import annotations


def _row_batches(rows, import_id):
    import json
    from .client import ClientError, MAX_FRAME, encode_frame

    envelope = {'op': 'history_import', 'phase': 'rows', 'import_id': import_id, 'rows': []}
    overhead = len(encode_frame(envelope))
    batch = []
    size = overhead
    for row in rows:
        try:
            row_size = len(json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
        except (ValueError, TypeError, UnicodeError):
            raise ClientError('history_parse_failed') from None
        if overhead + row_size > MAX_FRAME:
            raise ClientError('request_too_large')
        extra = row_size + bool(batch)
        if size + extra > MAX_FRAME or len(batch) >= 8192:
            yield dict(envelope, rows=batch)
            batch = []
            size = overhead
            extra = row_size
        batch.append(row)
        size += extra
    if batch:
        yield dict(envelope, rows=batch)


def import_history(path=None) -> dict:
    """Import one in-memory snapshot; callers must run this off the UI thread.

    Only sanitized parser rows cross the socket. The begin operation pauses live
    collection without changing consent settings; the daemon restores the exact
    previous state on commit or disconnect, including every failure below.
    """
    import os
    from pathlib import Path
    import secrets
    from .client import ClientError, Connection
    from .store import utcnow

    import_id = secrets.token_hex(16)
    try:
        with Connection(timeout=15.0) as connection:
            common = {'op': 'history_import', 'import_id': import_id}
            begin = connection.exchange(dict(common, phase='begin'))
            if begin.get('already_imported') is True:
                connection.exchange(dict(common, phase='commit'))
                return {'ok': True, 'status': 'already_imported', 'skipped': 0}
            selected = Path(path) if path is not None else Path(os.environ.get('HISTFILE') or Path.home() / '.bash_history')
            try:
                # No history text, path, or parser exception is ever reported.
                with selected.open('r', encoding='utf-8', errors='replace') as handle:
                    text = handle.read()
            except (OSError, ValueError):
                return {'ok': False, 'error': 'history_unreadable'}
            try:
                from .history import parse_history
                parsed = parse_history(text, now=utcnow())
                rows = parsed['rows']
                skipped = parsed['skipped']
                if not isinstance(rows, list) or type(skipped) is not int or skipped < 0:
                    return {'ok': False, 'error': 'history_parse_failed'}
            except ImportError:
                return {'ok': False, 'error': 'history_parser_unavailable'}
            except Exception:
                return {'ok': False, 'error': 'history_parse_failed'}
            finally:
                del text
            for frame in _row_batches(rows, import_id):
                connection.exchange(frame)
            result = connection.exchange(dict(common, phase='commit'))
            return {'ok': True, 'status': 'already_imported' if result.get('already_imported') is True else 'imported', 'skipped': skipped}
    except ClientError as exc:
        return {'ok': False, 'error': exc.code}


def _bash_event(argv) -> int:
    """Fail quietly, including parser startup, blocked stdin, and socket waits."""
    import os
    import signal

    # The hook's helper is disposable: a hard process deadline also covers native
    # parser work and never emits a traceback or leaves a shell waiting on I/O.
    signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
    signal.setitimer(signal.ITIMER_REAL, 0.250)
    try:
        import argparse
        import sys
        from .client import observer_settings, request

        class QuietParser(argparse.ArgumentParser):
            def error(self, message):
                raise ValueError('invalid_arguments')

        parser = QuietParser(add_help=False)
        parser.add_argument('--session', required=True)
        parser.add_argument('--sequence', required=True, type=int)
        parser.add_argument('--check', action='store_true')
        args = parser.parse_args(argv)
        if not args.session or len(args.session.encode('utf-8')) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in args.session) or args.sequence < 0:
            return 0
        if args.check:
            if observer_settings(timeout=0.100).get('collection_enabled') is True:
                print('collecting')
            return 0
        # Never permit an unbounded private record to be retained or parsed.
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return 0
        text = raw.decode('utf-8', errors='strict')
        if text:
            from .history import extract_commands
            names = extract_commands(text)
        else:
            names = []
        request({'op': 'bash', 'shell_id': args.session, 'seq': args.sequence, 'names': names}, timeout=0.200)
    except BaseException:
        # No source text, names, errors, or diagnostics go to the shell.
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return 0


def main(argv=None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'bash-event':
        return _bash_event(argv[1:])

    import argparse
    parser = argparse.ArgumentParser(prog='oma-usage', description='Local software usage insights')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('daemon', help='Run the private local collector')
    history = commands.add_parser('import-history', help='Import one Bash history snapshot')
    history.add_argument('--file', dest='path')
    commands.add_parser('bash-event', help='Internal bounded Bash prompt helper')
    uninstall = commands.add_parser('uninstall', help='Preview ownership-safe local backend cleanup')
    uninstall.add_argument('--yes', action='store_true', help='Approve backend cleanup; retain usage, settings, and native plugin')
    args = parser.parse_args(argv)
    if args.command == 'uninstall':
        import json
        from .uninstall import preview, remove_backend
        result = remove_backend() if args.yes else preview()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not args.yes:
            print('Preview only; no changes made. Approve with: oma-usage uninstall --yes')
        return 0 if result.get('ok') else 1
    if args.command == 'import-history':
        result = import_history(args.path)
        if result['ok']:
            print(result['status'])
            print('skipped=' + str(result['skipped']))
            return 0
        print(result['error'], file=sys.stderr)
        if result['error'] == 'collector_unavailable':
            print('Start the local oma-usage collector before importing history.', file=sys.stderr)
        return 1
    try:
        if args.command == 'daemon':
            from .daemon import main as run
        else:
            from .ui import main as run
        result = run()
        return result if type(result) is int else 0
    except ImportError:
        print('component_unavailable', file=sys.stderr)
        return 1
