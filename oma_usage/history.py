"""Bash syntactic mentions only; never execute or persist command source text."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re
import unicodedata

from tree_sitter import Language, Parser
import tree_sitter_bash

_LANGUAGE = Language(tree_sitter_bash.language())
_MARKER = re.compile(r'^#([+-]?[0-9]+)\s*$')
_MAX_COUNT = 2**31 - 1


_ANSI_ESCAPES = {'a': '\a', 'b': '\b', 'e': '\x1b', 'E': '\x1b',
                 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v',
                 '\\': '\\', "'": "'", '"': '"', '?': '?'}


def _ansi(raw: str, start: int) -> tuple[str, int] | None:
    result = []
    i = start
    while i < len(raw):
        c = raw[i]
        if c == "'":
            return ''.join(result), i + 1
        if c != '\\':
            result.append(c)
            i += 1
            continue
        i += 1
        if i == len(raw):
            return None
        c = raw[i]
        if c in _ANSI_ESCAPES:
            result.append(_ANSI_ESCAPES[c])
            i += 1
        elif c in '01234567xuU':
            octal = c in '01234567'
            width = 3 if octal else {'x': 2, 'u': 4, 'U': 8}[c]
            begin = i if octal else i + 1
            end = begin
            digits = '01234567' if octal else '0123456789abcdefABCDEF'
            while end < min(len(raw), begin + width) and raw[end] in digits:
                end += 1
            if end == begin:
                return None
            try:
                result.append(chr(int(raw[begin:end], 8 if octal else 16)))
            except ValueError:
                return None
            i = end
        elif c == 'c' and i + 1 < len(raw):
            control = raw[i + 1]
            if not control.isascii():
                return None
            result.append(chr(ord(control.upper()) ^ 64))
            i += 2
        else:
            # Bash preserves the backslash for an unrecognized ANSI escape.
            result.extend(('\\', c))
            i += 1
    return None


def _literal(raw: str) -> str | None:
    """Decode a static shell word, without expansion, globbing, or evaluation."""
    result = []
    i = 0
    quote = None
    while i < len(raw):
        c = raw[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                result.append(c)
        elif c == '\\':
            i += 1
            if i == len(raw):
                return None
            escaped = raw[i]
            if escaped == '\n':
                pass
            elif quote == '"' and escaped not in '$`"\\':
                result.extend(('\\', escaped))
            else:
                result.append(escaped)
        elif quote == '"':
            if c == '"':
                quote = None
            elif c in '$`':
                return None
            else:
                result.append(c)
        elif c in "'\"":
            quote = c
        elif c == '$' and raw[i:i + 2] == "$'":
            decoded = _ansi(raw, i + 2)
            if decoded is None:
                return None
            part, i = decoded
            result.append(part)
            continue
        elif c in '$`*?[]{}~' or c.isspace():
            return None
        else:
            result.append(c)
        i += 1
    return None if quote else ''.join(result)


def _identity(value: str | None) -> str | None:
    if value is None or value.endswith('/'):
        return None
    name = value.rsplit('/', 1)[-1]
    if not name or name in ('.', '..') or any(unicodedata.category(c).startswith('C') for c in name):
        return None
    try:
        return name if len(name.encode('utf-8')) <= 255 else None
    except UnicodeError:
        return None


def _mentions(root, source: bytes) -> list[str]:
    names = []

    def text(node):
        return source[node.start_byte:node.end_byte].decode('utf-8')

    def command(node):
        head = node.child_by_field_name('name')
        if head is None:
            return
        value = _literal(text(head))
        name = _identity(value)
        if name is not None:
            names.append(name)
        # A path named sudo is not the known bare wrapper. Arguments are only
        # interpreted for these explicit wrappers, never for eval/bash/ssh.
        args = list(node.children_by_field_name('argument'))
        cursor = 0
        while value in ('sudo', 'command', 'builtin', 'nohup'):
            wrapper = value
            while cursor < len(args):
                option = _literal(text(args[cursor]))
                if option is None:
                    return
                if option == '--':
                    cursor += 1
                    break
                if not option.startswith('-'):
                    break
                if wrapper != 'sudo':
                    return
                if option in ('-n', '-E', '-H', '-S'):
                    cursor += 1
                elif option in ('-u', '--user'):
                    cursor += 1
                    if cursor == len(args) or _literal(text(args[cursor])) in (None, ''):
                        return
                    cursor += 1
                else:
                    return
            if cursor == len(args):
                return
            value = _literal(text(args[cursor]))
            cursor += 1
            name = _identity(value)
            if name is None:
                return
            names.append(name)

    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ('function_definition', 'heredoc_body', 'comment'):
            continue
        if node.type == 'command':
            command(node)
        # Substitutions in arguments/assignments/redirection operands really
        # contain command syntax; ordinary data arguments never do.
        stack.extend(reversed(node.named_children))
    return names


def extract_commands(text: str) -> list[str]:
    """Return static syntactic command mentions, including repeated mentions.

    Incomplete/erroneous input is rejected as a whole. Locale-translated
    command names are unsupported rather than expanded.
    """
    source = text.encode('utf-8')
    root = Parser(_LANGUAGE).parse(source).root_node
    return [] if root.has_error else _mentions(root, source)


def parse_history(text: str, *, now: datetime) -> dict:
    """Aggregate history records into sanitized dated/undated protocol rows.

    A timestamp marker applies to the entire following multiline record, not
    just its first physical line. Only top-level syntax comments (or markers
    after an unmatched closing delimiter) can be record boundaries:
    marker-shaped literal/heredoc data must never be reparsed as commands.
    Ambiguous erroneous syntax stays in the skipped record. Untimestamped
    records accumulate until their syntax is complete.
    No import-time timestamp is assigned to undated evidence.
    """
    if now.tzinfo is None:
        raise ValueError('timezone_required')
    now = now.astimezone(timezone.utc)
    parser = Parser(_LANGUAGE)
    # Parse with all following context before deciding where a record ends.
    # Parsing a physical prefix alone cannot distinguish an unfinished literal
    # from a malformed record followed by a real history timestamp.
    source = text.encode('utf-8')
    tree = parser.parse(source)
    marker_offsets = {
        node.start_byte for node in tree.root_node.named_children
        if node.type == 'comment'
        and _MARKER.fullmatch(source[node.start_byte:node.end_byte].decode('utf-8'))
    }
    for node in tree.root_node.named_children:
        if node.type != 'ERROR':
            continue
        # Bash's grammar can absorb a following comment into an unmatched
        # closing-delimiter error, exposing it as "#" and number tokens.
        # Recover only this unambiguous case, never an error containing an
        # opening delimiter, quote, heredoc, or any other unknown context.
        preceding = []
        for child in node.children:
            if (child.type in ('#', 'comment') and preceding
                    and all(token.type in (')', '}') for token in preceding)):
                marker_offsets.add(child.start_byte)
            preceding.append(child)
    aggregates = {}
    skipped = 0
    pending = []
    timestamped = False
    stamp = None

    def consume(lines, at):
        nonlocal skipped
        if not lines:
            return
        source = ''.join(lines).encode('utf-8')
        root = parser.parse(source).root_node
        if root.has_error:
            skipped += 1
            return
        day = at.date().isoformat() if at is not None else None
        iso = at.isoformat(timespec='microseconds') if at is not None else None
        for name, count in Counter(_mentions(root, source)).items():
            key = (name, day)
            row = aggregates.setdefault(key, {'name': name, 'day': day, 'count': 0, 'first_at': iso, 'last_at': iso})
            row['count'] = min(_MAX_COUNT, row['count'] + count)
            if iso is not None:
                row['first_at'] = min(row['first_at'], iso)
                row['last_at'] = max(row['last_at'], iso)

    offset = 0
    for line in text.splitlines(keepends=True):
        marker = (_MARKER.fullmatch(line.rstrip('\r\n'))
                  if offset in marker_offsets else None)
        offset += len(line.encode('utf-8'))
        if marker is not None:
            consume(pending, stamp if timestamped else None)
            pending = []
            timestamped = True
            try:
                stamp = datetime.fromtimestamp(int(marker[1]), timezone.utc)
                if stamp > now:
                    stamp = None
            except (ValueError, OverflowError, OSError):
                stamp = None
        else:
            pending.append(line)
            if not timestamped:
                source = ''.join(pending).encode('utf-8')
                root = parser.parse(source).root_node
                physical = line.rstrip('\r\n')
                trailing_slashes = len(physical) - len(physical.rstrip('\\'))
                continued = (line.endswith('\n') and trailing_slashes % 2 == 1
                             and not (root.named_children and root.named_children[-1].type == 'comment'))
                if not root.has_error and not continued:
                    consume(pending, None)
                    pending = []
    consume(pending, stamp if timestamped else None)
    return {'rows': sorted(aggregates.values(), key=lambda row: (row['name'], row['day'] or '')), 'skipped': skipped}
