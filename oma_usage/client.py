"""Bounded local protocol requests; never include payloads in error messages."""
from __future__ import annotations

import json
import os
import socket
import struct
import time

MAX_FRAME = 1024 * 1024


class ClientError(Exception):
    """A fixed, nonidentifying protocol or availability error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def encode_frame(message: dict) -> bytes:
    try:
        frame = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    except (TypeError, ValueError, UnicodeError):
        raise ClientError('invalid_request') from None
    if len(frame) > MAX_FRAME:
        raise ClientError('request_too_large')
    return frame


class Connection:
    """One ordered request stream, optionally used for an import transaction.

    The daemon owns pausing and restoring collection for imports, including
    incomplete transactions whose client disconnects. Each exchange has a total
    deadline, rather than a fresh timeout for every received fragment.
    """

    def __init__(self, *, timeout: float = 5.0):
        from .store import socket_path

        self.timeout = timeout
        self.buffer = bytearray()
        self.settings = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.settimeout(timeout)
            self.socket.connect(str(socket_path()))
            credentials = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            if struct.unpack('3i', credentials)[1] != os.getuid():
                raise ClientError('collector_peer_rejected')
        except (OSError, RuntimeError):
            self.socket.close()
            raise ClientError('collector_unavailable') from None
        except BaseException:
            self.socket.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.socket.close()

    def _receive(self, deadline: float) -> dict:
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError('collector_timeout')
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(min(65536, MAX_FRAME + 1 - len(self.buffer)))
            if not chunk:
                raise ClientError('collector_disconnected')
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_FRAME:
                raise ClientError('invalid_response')
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        try:
            reply = json.loads(line)
        except (ValueError, UnicodeError):
            raise ClientError('invalid_response') from None
        if not isinstance(reply, dict):
            raise ClientError('invalid_response')
        return reply

    def exchange(self, message: dict) -> dict:
        frame = encode_frame(message)
        deadline = time.monotonic() + self.timeout
        try:
            self.socket.settimeout(self.timeout)
            self.socket.sendall(frame)
            while True:
                reply = self._receive(deadline)
                if reply.get('op') == 'settings':
                    if type(reply.get('collection_enabled')) is not bool or type(reply.get('idle_seconds')) is not int:
                        raise ClientError('invalid_response')
                    self.settings = reply
                    if message.get('op') == 'hello':
                        return reply
                    continue
                if type(reply.get('ok')) is not bool:
                    raise ClientError('invalid_response')
                # Never relay arbitrary daemon strings to terminals or the UI.
                if not reply['ok']:
                    raise ClientError('collector_rejected')
                return reply
        except TimeoutError:
            raise ClientError('collector_timeout') from None
        except OSError:
            raise ClientError('collector_unavailable') from None


def request(message: dict, *, timeout: float = 5.0) -> dict:
    """Send one control/Bash request and return its acknowledged result."""
    with Connection(timeout=timeout) as connection:
        return connection.exchange(message)


def observer_settings(*, timeout: float = 5.0) -> dict:
    """Obtain current collection settings without sending an app identity."""
    return request({'op': 'hello', 'role': 'observer'}, timeout=timeout)
