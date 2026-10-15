"""Unprivileged Unix-socket client. No secrets or caller-selected commands."""

import json
import socket
import struct
import time
import uuid

from .protocol import MAX_RESPONSE, PROTOCOL, SOCKET, encode, validate


def request(op, *, socket_path=SOCKET, **fields):
    for attempt in range(3):
        try:
            return _request_once(op, socket_path=socket_path, **fields)
        except RuntimeError as exc:
            # These authenticated rejections precede writes; lost acknowledgements are never replayed.
            if str(exc) not in ('Resource writer busy; retry with a fresh request',
                                'Resource writer busy; failed profile evidence pending') or attempt == 2:
                raise
        time.sleep(.1)


def _request_once(op, *, socket_path, **fields):
    message = validate({'protocol': PROTOCOL, 'request_id': str(uuid.uuid4()), 'op': op, **fields})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30 if op == 'launch' else 10)
        connection.connect(str(socket_path))
        uid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid != 0:
            raise ValueError('Guest service socket peer is not root')
        connection.sendall(encode(message))
        with connection.makefile('rb') as stream:
            line = stream.readline(MAX_RESPONSE + 1)
        if len(line) > MAX_RESPONSE or not line.endswith(b'\n'):
            raise ValueError('Invalid guest response frame')
        reply = json.loads(line)
        if (not isinstance(reply, dict) or reply.get('protocol') != PROTOCOL
                or reply.get('request_id') != message['request_id'] or type(reply.get('ok')) is not bool):
            raise ValueError('Guest acknowledgement does not match request')
        if not reply['ok']:
            raise RuntimeError(reply.get('error', 'Guest service rejected request'))
        if not isinstance(reply.get('result'), dict):
            raise ValueError('Invalid guest result')
        return reply['result']
