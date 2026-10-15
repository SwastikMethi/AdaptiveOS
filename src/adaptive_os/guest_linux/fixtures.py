"""Bounded administrator test fixtures; workloads run as the ordinary task user."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import queue
import random
import select
import signal
import socket
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

BLOCK = hashlib.sha256(b'adaptive-os-guest-fixed-benchmark-block-v1').digest() * 32768


def emit(**row):
    print(json.dumps(row, allow_nan=False), flush=True)


def endpoint(host, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                size = int(parse_qs(urlsplit(self.path).query).get('bytes', ['65536'])[0])
                if not 1 <= size <= 4 * 1024**2:
                    raise ValueError()
                self.send_response(200)
                self.send_header('Content-Length', str(size))
                self.end_headers()
                while size:
                    chunk = BLOCK[:min(size, len(BLOCK))]
                    self.wfile.write(chunk)
                    size -= len(chunk)
            except (ValueError, OSError):
                self.close_connection = True

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 1 <= size <= 4 * 1024**2:
                    raise ValueError()
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError()
                digest = hashlib.sha256(data).hexdigest().encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(digest)))
                self.end_headers()
                self.wfile.write(digest)
            except (ValueError, OSError):
                self.close_connection = True

        def setup(self):
            super().setup()
            # Light-profile pacing can exceed 10 seconds; retain idle sockets for a bounded fixture.
            self.connection.settimeout(900)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    emit(kind='endpoint_ready', host=host, port=port, payload_sha256=hashlib.sha256(BLOCK[:65536]).hexdigest())
    server.serve_forever()


def workload(args, target):
    stop, results = threading.Event(), queue.SimpleQueue()
    owner = os.getppid()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    rng = random.Random(8128)
    file = None
    if args.resource == 'memory':
        directory = Path(args.directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ('memory-' + args.task + '.bin')
        size = args.memory_mib * 1024**2
        if path.is_symlink():
            raise ValueError('Fixture file must not be a symlink')
        if not path.exists() or path.stat().st_size != size:
            with path.open('wb') as stream:
                for _ in range(args.memory_mib):
                    stream.write(BLOCK)
                stream.flush()
                os.fsync(stream.fileno())
        file = os.open(path, os.O_RDONLY)
        os.posix_fadvise(file, 0, size, os.POSIX_FADV_DONTNEED)
        for offset in range(0, size, len(BLOCK)):
            if os.pread(file, len(BLOCK), offset) != BLOCK:
                raise ValueError('Memory fixture checksum mismatch')
    url = urlsplit(args.endpoint)
    if args.resource == 'network':
        if url.scheme != 'http' or not ipaddress.ip_address(url.hostname).is_private:
            raise ValueError('Use the private Mac test endpoint')
    until = time.monotonic() + args.seconds

    def operation(direction='download'):
        stream = http.client.HTTPConnection(url.hostname, url.port, timeout=10) if args.resource == 'network' else None
        data = BLOCK if args.resource == 'memory' and args.role == 'background' else BLOCK[:65536]
        due = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=3) if args.resource == 'cpu' and args.role == 'foreground' else None
        try:
            while not stop.is_set() and time.monotonic() < until:
                started = time.monotonic()
                if args.resource == 'cpu':
                    def compute(_=0):
                        return hashlib.pbkdf2_hmac('sha256', b'guest-trial', b'fixed-input', args.iterations)
                    if pool:
                        list(pool.map(compute, range(3)))
                    else:
                        compute()
                    count = 1
                elif args.resource == 'memory':
                    offset = rng.randrange(args.memory_mib * 1024**2 // len(data)) * len(data)
                    if os.pread(file, len(data), offset) != data:
                        raise ValueError('Memory fixture read checksum mismatch')
                    count = len(data)
                elif direction == 'upload':
                    stream.request('POST', '/', data)
                    response = stream.getresponse()
                    if response.status != 200 or response.read() != hashlib.sha256(data).hexdigest().encode():
                        raise ValueError('Upload checksum mismatch')
                    count = len(data)
                else:
                    stream.request('GET', '/?bytes=65536')
                    response = stream.getresponse()
                    if response.status != 200 or response.read() != data:
                        raise ValueError('Download checksum mismatch')
                    count = len(data)
                finished = time.monotonic()
                latency = (finished - due if args.role == 'foreground' and args.resource == 'cpu' else finished - started) * 1000
                results.put({'at': finished, 'latency_ms': latency, 'work': count, 'direction': direction})
                interval = (.100 if args.resource == 'cpu' else .005 if args.resource == 'memory' else .080) if args.role == 'foreground' else (
                    max(.001, (finished - started) * 10) if args.profile == 'light' else 0.)
                due = max(due + interval, finished) if args.role != 'foreground' or args.resource != 'cpu' else due + interval
                stop.wait(max(0, due - time.monotonic()))
        except Exception as exc:
            results.put({'error': str(exc), 'at': time.monotonic()})
            stop.set()
        finally:
            if stream:
                stream.close()
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)

    count = 3 if args.resource == 'cpu' and args.role == 'background' else 2 if args.resource == 'network' and args.direction == 'both' else 1
    threads = [threading.Thread(target=operation, args=(('upload' if index == 0 else 'download')
                if args.direction == 'both' else args.direction,), daemon=True) for index in range(count)]
    emit(kind='fixture_ready', task_id=args.task, identity=args.identity, resource=args.resource,
         role=args.role, pid=os.getpid(), cgroup=Path('/proc/self/cgroup').read_text().strip(),
         network_namespace=os.stat('/proc/self/ns/net').st_ino, cpus=sorted(os.sched_getaffinity(0)))
    for thread in threads:
        thread.start()
    try:
        while time.monotonic() < until and not stop.wait(1):
            if os.getppid() != owner:
                break
            batch = []
            while not results.empty():
                batch.append(results.get())
            emit(kind='fixture_metrics', task_id=args.task, rows=batch, time=time.monotonic())
    finally:
        stop.set()
        for thread in threads:
            thread.join(12)
        remaining = []
        while not results.empty():
            remaining.append(results.get())
        emit(kind='fixture_metrics', task_id=args.task, rows=remaining, time=time.monotonic())
        if file is not None:
            os.posix_fadvise(file, 0, args.memory_mib * 1024**2, os.POSIX_FADV_DONTNEED)
            os.close(file)


def managed_fixture(args):
    if os.geteuid() != 0:
        raise PermissionError('Only bounded setup requires root')
    # The existing verifier resolves only registered task/invocation identities.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cpu_verify import load_target
    target = load_target(args.task, args.identity)
    namespace = os.open(target['namespace'], os.O_RDONLY)
    child = os.fork()
    if child == 0:
        try:
            Path(target['cgroup'], 'cgroup.procs').write_text(str(os.getpid()))
            os.setns(namespace, 0)
            os.close(namespace)
            os.setgroups([])
            os.setgid(target['gid'])
            os.setuid(target['uid'])
            os.sched_setaffinity(0, {1, 2, 3})
            workload(args, target)
            os._exit(0)
        except BaseException as exc:
            emit(kind='fixture_error', error=str(exc))
            os._exit(1)
    os.close(namespace)
    os.sched_setaffinity(0, {0})
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    try:
        select.select([sys.stdin], [], [], args.seconds + 120)
    finally:
        try:
            os.kill(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)


def pressure_load(mib, seconds):
    import mmap
    if os.geteuid() == 0 or not 128 <= mib <= 5120:
        raise ValueError('Pressure fixture requires an ordinary user and a bounded footprint')
    memory = mmap.mmap(-1, mib * 1024**2)
    try:
        for offset in range(0, mib * 1024**2, 4096):
            memory[offset] = 1
        emit(kind='pressure_ready', mib=mib, pid=os.getpid())
        select.select([sys.stdin], [], [], seconds)
    finally:
        memory.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--pressure-mib', type=int)
    parser.add_argument('--host', default='192.168.64.1')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--task')
    parser.add_argument('--identity')
    parser.add_argument('--resource', choices=('cpu', 'memory', 'network'), default='cpu')
    parser.add_argument('--role', choices=('foreground', 'background'), default='foreground')
    parser.add_argument('--profile', choices=('light', 'contention'), default='contention')
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--iterations', type=int, default=100000)
    parser.add_argument('--memory-mib', type=int, default=1024)
    parser.add_argument('--directory', default='/tmp/adaptive-os-fixtures')
    parser.add_argument('--endpoint', default='http://192.168.64.1:8766')
    parser.add_argument('--direction', choices=('upload', 'download', 'both'), default='both')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 900 or not 1000 <= args.iterations <= 2_000_000 or not 64 <= args.memory_mib <= 2304:
        parser.error('Fixture bounds exceeded')
    if args.pressure_mib:
        pressure_load(args.pressure_mib, args.seconds)
    elif args.serve:
        endpoint(args.host, args.port)
    else:
        managed_fixture(args)


if __name__ == '__main__':
    main()
