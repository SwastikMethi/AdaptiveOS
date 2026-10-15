"""Owned loopback TCP relay. Bytes are scheduled continuously, outside telemetry."""
import json
import socket
import struct
import threading
import time

REQUEST = struct.Struct('!IQ')
RESPONSE = struct.Struct('!QIQ')
MAX_RECORD = 256 * 1024
BLOCK = bytes(1024)


def receive_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        part = stream.recv(count - len(data))
        if not part:
            raise OSError('Relay disconnected')
        data.extend(part)
    return bytes(data)


def connect_flow(port, workload_id, identity):
    stream = socket.create_connection(('127.0.0.1', port), timeout=3)
    stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    hello = json.dumps({'workload_id': workload_id, 'identity': identity}).encode()
    stream.sendall(struct.pack('!I', len(hello)) + hello)
    return stream


def request_record(stream, sequence, size):
    stream.sendall(REQUEST.pack(size, sequence))


def receive_record(stream, sequence, size):
    received_sequence, received_size, queue_ns = RESPONSE.unpack(receive_exact(stream, RESPONSE.size))
    if received_sequence != sequence or received_size != size:
        raise ValueError('TCP application record identity/size mismatch')
    remaining = size
    while remaining:
        part = stream.recv(min(remaining, 65536))
        if not part:
            raise OSError('Truncated TCP application record')
        remaining -= len(part)
    return queue_ns / 1e6


class Relay:
    def __init__(self, mbps=20):
        self.rate = mbps * 1_000_000 / 8
        self.burst = max(1024, self.rate / 100)  # At most 10 ms of idle credit.
        self.tokens = 0.
        self.last = time.monotonic()
        self.virtual_time = 0.
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.flows, self.connections = {}, {}
        self.error = None
        self.listener = socket.socket()
        try:
            self.listener.bind(('127.0.0.1', 0))
        except OSError:
            self.listener.close()
            raise
        self.port = self.listener.getsockname()[1]
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.thread = threading.Thread(target=self.run, name='bandwidth-relay', daemon=True)
        self.thread.start()

    def add_flow(self, key, identity, share):
        with self.lock:
            if key in self.flows or len(self.flows) >= 8 or sum(f['share'] for f in self.flows.values()) + share > 10000:
                raise ValueError('Network share pool exceeded or duplicate flow')
            if type(share) is not int or not 500 <= share <= 9500:
                raise ValueError('Invalid flow share')
            self.flows[key] = dict(identity=identity, share=share, socket=None, bytes=0, remaining=0,
                                   finish=self.virtual_time, pending=False, output=b'', records=0, queue_ns=0)

    def remove_flow(self, key):
        with self.lock:
            flow = self.flows.pop(key, None)
            if flow and flow['socket']:
                self.disconnect(flow['socket'])

    def update_shares(self, changes):
        with self.lock:
            if len(changes) != 2 or len({c['workload_id'] for c in changes}) != 2:
                raise ValueError('Invalid network transfer')
            if sum(c['previous'] for c in changes) != sum(c['requested'] for c in changes):
                raise ValueError('Network transfer must conserve shares')
            for c in changes:
                flow = self.flows.get(c['workload_id'])
                if (not flow or flow['identity'] != c['identity'] or flow['share'] != c['previous']
                        or type(c['requested']) is not int or not 500 <= c['requested'] <= 9500):
                    raise ValueError('Stale flow identity/allocation or invalid share')
            for c in changes:
                self.flows[c['workload_id']].update(share=c['requested'], finish=self.virtual_time)

    def snapshot(self):
        with self.lock:
            return {key: {'share': f['share'], 'bytes': f['bytes'], 'records': f['records'],
                          'queue_delay_ms': f['queue_ns'] / 1e6,
                          'queued_bytes': f['remaining'] + len(f['output'])}
                    for key, f in self.flows.items()}

    def disconnect(self, stream):
        connection = self.connections.pop(stream, None)
        if connection and connection['key'] in self.flows:
            self.flows[connection['key']].update(socket=None, pending=False, remaining=0, output=b'')
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        stream.close()

    def read_connections(self, now):
        try:
            stream, _ = self.listener.accept()
            if len(self.connections) >= 8:
                stream.close()
            else:
                stream.setblocking(False)
                stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
                self.connections[stream] = dict(key=None, buffer=b'', started=now)
        except BlockingIOError:
            pass
        for stream, connection in list(self.connections.items()):
            key = connection['key']
            flow = self.flows.get(key)
            if flow and flow['pending']:
                continue  # One record per flow; TCP applies backpressure to pipelining.
            try:
                limit = REQUEST.size if key else 260
                if len(connection['buffer']) < limit:
                    try:
                        part = stream.recv(limit - len(connection['buffer']))
                    except BlockingIOError:
                        part = None
                    if part == b'':
                        raise OSError('EOF')
                    connection['buffer'] += part or b''
                data = connection['buffer']
                if not key:
                    if now - connection['started'] > 3:
                        raise ValueError('Handshake expired')
                    if len(data) < 4:
                        continue
                    size = struct.unpack('!I', data[:4])[0]
                    if not 1 <= size <= 256:
                        raise ValueError('Handshake bound exceeded')
                    if len(data) < size + 4:
                        continue
                    hello = json.loads(data[4:4 + size])
                    flow = self.flows.get(hello['workload_id'])
                    if not flow or flow['identity'] != hello['identity'] or flow['socket']:
                        raise ValueError('Unowned or duplicate flow')
                    key = connection['key'] = hello['workload_id']
                    flow['socket'] = stream
                    data = connection['buffer'] = data[4 + size:]
                if len(data) >= REQUEST.size:
                    size, sequence = REQUEST.unpack(data[:REQUEST.size])
                    if not 1 <= size <= MAX_RECORD or sequence != flow['records']:
                        raise ValueError('Invalid record size or sequence')
                    connection['buffer'] = data[REQUEST.size:]
                    flow.update(pending=True, remaining=size, size=size, sequence=sequence,
                                queued_at=time.monotonic_ns(), header=False,
                                finish=max(flow['finish'], self.virtual_time))
            except (OSError, ValueError, KeyError, TypeError):
                self.disconnect(stream)

    def schedule(self):
        blocked = set()
        while self.tokens >= 1:
            active = [(key, flow) for key, flow in self.flows.items() if flow['pending'] and key not in blocked]
            if not active:
                return
            key, flow = min(active, key=lambda item: (item[1]['finish'], item[0]))
            if not flow['output']:
                if not flow['header']:
                    flow['queue_ns'] = time.monotonic_ns() - flow['queued_at']
                    flow['output'] = RESPONSE.pack(flow['sequence'], flow['size'], flow['queue_ns'])
                    flow['header'] = True
                else:
                    amount = min(1024, flow['remaining'])
                    flow['output'] = BLOCK[:amount]
                    flow['remaining'] -= amount
            try:
                count = flow['socket'].send(flow['output'][:max(1, int(self.tokens))])
                if count <= 0:
                    raise OSError('Relay send failed')
                self.tokens -= count
                self.virtual_time = flow['finish']
                flow['finish'] += count / flow['share']
                flow['bytes'] += count
                flow['output'] = flow['output'][count:]
                if not flow['remaining'] and not flow['output']:
                    flow['pending'] = False
                    flow['records'] += 1
            except BlockingIOError:
                blocked.add(key)
            except OSError:
                self.disconnect(flow['socket'])

    def run(self):
        try:
            while not self.stopping.wait(.001):
                with self.lock:
                    now = time.monotonic()
                    self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
                    self.last = now
                    self.read_connections(now)
                    self.schedule()
        except Exception as exc:
            self.error = str(exc)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)
        with self.lock:
            for stream in list(self.connections):
                self.disconnect(stream)
            self.listener.close()
