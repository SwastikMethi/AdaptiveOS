"""Privileged guest service. Install under root ownership before starting it."""

import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time

from .core import Engine, Journal
from .linux import LinuxHost, STATE, load_config
from .protocol import MAX_MESSAGE, MAX_RESPONSE, PROTOCOL, SOCKET, encode, validate


def peer_uid(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        request = {}
        try:
            if peer_uid(self.connection) != self.server.allowed_uid:
                raise ValueError('Unauthorized guest socket peer')
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                raise ValueError('Invalid guest frame size')
            request = validate(json.loads(line))
            if request['op'] == 'heartbeat':
                self.server.engine.heartbeat(request['run_id'])
                result = {'run_id': request['run_id']}
            else:
                # Reconciliation can outlast a scheduling tick; heartbeats bypass this bounded wait.
                if not self.server.writer.acquire(timeout=1):
                    raise ValueError('Resource writer busy; retry with a fresh request')
                try:
                    if self.server.stopping.is_set():
                        raise ValueError('Guest service is shutting down')
                    result = (self.server.engine.snapshot_seed() if request['op'] == 'snapshot'
                              else self.server.engine.handle(request))
                finally:
                    self.server.writer.release()
                if request['op'] == 'snapshot':
                    result = self.server.engine.snapshot(result)
                    if (result.get('resource_control') or {}).get('headroom_required'):
                        if not self.server.writer.acquire(timeout=1):
                            raise ValueError('Resource writer busy; failed profile evidence pending')
                        try:
                            self.server.engine.commit_observation_faults()
                        finally:
                            self.server.writer.release()
            response = {'ok': True, 'result': result}
        except Exception as exc:
            response = {'ok': False, 'error': str(exc)[:500]}
        response.update(protocol=PROTOCOL, request_id=request.get('request_id') if isinstance(request, dict) else None)
        try:
            self.wfile.write(encode(response, MAX_RESPONSE))
        except (OSError, ValueError):
            pass  # The durable journal, never successful socket delivery, owns recovery.


class GuestServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, engine, allowed_uid):
        self.engine, self.allowed_uid = engine, allowed_uid
        self.writer = threading.Lock()
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('The service must run as root inside the configured Ubuntu guest')
    os.umask(0o077)
    config = load_config()
    for directory in (STATE, Path(SOCKET).parent):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('Service directories must be root-owned and not writable by clients')
    with (STATE / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = Path(SOCKET)
        if path.exists():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != config['uid']:
                raise SystemExit('Refusing to replace an unowned guest socket')
            path.unlink()
        journal = Journal(STATE / 'journal.sqlite3')
        engine = Engine(journal, LinuxHost(journal, config))
        server = GuestServer(path, engine, config['uid'])
        os.chown(path, config['uid'], config['gid'])
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            while not stopped.wait(.5):
                if server.writer.acquire(blocking=False):
                    try:
                        engine.tick()
                    except Exception as exc:
                        print(f'Recovery pending: {exc}', file=sys.stderr, flush=True)
                    finally:
                        server.writer.release()
        finally:
            server.shutdown()
            with server.writer:
                engine.restore('service_shutdown', end=True)
            server.server_close()
            journal.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
