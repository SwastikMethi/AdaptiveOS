import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from tests.test_guest_linux import FakeHost


class GuestClientTest(unittest.TestCase):
    def test_only_explicit_busy_replies_are_retried_with_fresh_ids_and_a_bound(self):
        import io
        import struct
        from unittest.mock import MagicMock
        from adaptive_os.guest_linux.client import request
        busy = 'Resource writer busy; retry with a fresh request'
        for errors, expected, succeeds in (([busy, busy, None], 3, True),
                                           ([busy] * 3, 3, False),
                                           (['Duplicate request'], 1, False),
                                           ([TimeoutError('acknowledgement lost')], 1, False)):
            sent = []
            def connect(*args):
                connection = MagicMock()
                connection.__enter__.return_value = connection
                connection.getsockopt.return_value = struct.pack('3i', 42, 0, 0)
                def send(payload):
                    message = json.loads(payload)
                    error = errors[len(sent)]
                    sent.append(message)
                    if isinstance(error, Exception):
                        raise error
                    reply = {'protocol': 'guest-linux/1', 'request_id': message['request_id'],
                             'ok': error is None, 'error': error, 'result': {'accepted': True}}
                    connection.makefile.return_value = io.BytesIO((json.dumps(reply) + '\n').encode())
                connection.sendall.side_effect = send
                return connection
            with self.subTest(errors=errors), patch('adaptive_os.guest_linux.client.socket.socket', side_effect=connect), \
                 patch('adaptive_os.guest_linux.client.socket.SO_PEERCRED', 17, create=True), \
                 patch('time.sleep'):
                if succeeds:
                    self.assertEqual(request('launch', app='geany'), {'accepted': True})
                else:
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        request('launch', app='geany')
            self.assertEqual(len(sent), expected)
            self.assertEqual(len({m['request_id'] for m in sent}), expected)


class GuestSocketTest(unittest.TestCase):
    def setUp(self):
        from adaptive_os.guest_linux.core import Engine, Journal
        from adaptive_os.guest_linux.service import GuestServer
        self.tmp = tempfile.TemporaryDirectory(prefix='aos-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 's'
        self.journal = Journal(Path(self.tmp.name) / 'state.sqlite')
        self.addCleanup(self.journal.close)
        self.host = FakeHost()
        self.engine = Engine(self.journal, self.host)
        # macOS cannot expose Linux SO_PEERCRED. Only that OS boundary is replaced.
        self.peer = patch('adaptive_os.guest_linux.service.peer_uid', return_value=os.getuid()).start()
        self.addCleanup(patch.stopall)
        self.server = GuestServer(self.path, self.engine, os.getuid())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, op, **fields):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(str(self.path))
            conn.sendall((json.dumps({'protocol': 'guest-linux/1', 'request_id': str(uuid.uuid4()),
                                     'op': op, **fields}) + '\n').encode())
            return json.loads(conn.makefile('rb').readline())

    def test_untrusted_uid_cannot_launch_or_read_tasks(self):
        self.peer.return_value = os.getuid() + 1
        # Authentication rejects the connection before reading any request bytes.
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(str(self.path))
            response = json.loads(conn.makefile('rb').readline())
        self.assertFalse(response['ok'])
        self.assertIn('Unauthorized', response['error'])
        self.assertEqual(self.host.live, {})

    def test_shutdown_fences_already_connected_mutations(self):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(str(self.path))
            self.server.stopping.set()
            conn.sendall((json.dumps({'protocol': 'guest-linux/1', 'request_id': str(uuid.uuid4()),
                                     'op': 'launch', 'app': 'firefox'}) + '\n').encode())
            reply = json.loads(conn.makefile('rb').readline())
        self.assertFalse(reply['ok'])
        self.assertEqual(self.host.live, {})

    def test_heartbeat_responds_while_resource_writer_is_busy(self):
        task = self.request('launch', app='firefox')['result']['task']
        run = self.request('begin')['result']['run_id']
        self.request('probe', run_id=run, task_id=task['id'], identity=task['identity'], expected_weight=100)
        with self.server.writer:
            before = time.monotonic()
            reply = self.request('heartbeat', run_id=run)
            self.assertTrue(reply['ok'])
            self.assertLess(time.monotonic() - before, 1)
            before = time.monotonic()
            busy = self.request('restore', run_id=run)
            self.assertFalse(busy['ok'])
            self.assertLess(time.monotonic() - before, 2)
            self.assertEqual(self.host.weights[task['id']], 90)
        self.request('end', run_id=run)
        self.assertEqual(self.host.weights[task['id']], 100)

    def test_snapshot_waits_for_busy_writer_and_returns_committed_state(self):
        task = self.request('launch', app='firefox')['result']['task']
        run = self.request('begin')['result']['run_id']
        entered, release = threading.Event(), threading.Event()
        errors = []
        def slow_writer():
            try:
                with self.server.writer:
                    entered.set()
                    release.wait(3)
                    self.engine.handle({'protocol': 'guest-linux/1', 'request_id': str(uuid.uuid4()),
                        'op': 'probe', 'run_id': run, 'task_id': task['id'],
                        'identity': task['identity'], 'expected_weight': 100})
            except Exception as exc:
                errors.append(exc)
        writer = threading.Thread(target=slow_writer)
        writer.start()
        timer = threading.Timer(.6, release.set)
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertTrue(self.request('heartbeat', run_id=run)['ok'])
            self.assertLess(time.monotonic() - started, .5)
            timer.start()
            snapshot = self.request('snapshot')
            self.assertTrue(snapshot['ok'], snapshot)
            self.assertEqual(snapshot['result']['tasks'][0]['cpu_weight'], 90)
            self.assertEqual(snapshot['result']['tasks'][0]['identity'], task['identity'])
        finally:
            release.set()
            timer.cancel()
            writer.join(3)
            self.request('end', run_id=run)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.host.weights[task['id']], 100)

    def test_blocked_telemetry_does_not_block_cpu_restoration(self):
        task = self.request('launch', app='firefox')['result']['task']
        run = self.request('begin')['result']['run_id']
        self.request('probe', run_id=run, task_id=task['id'], identity=task['identity'], expected_weight=100)
        entered, release = threading.Event(), threading.Event()
        inspect = self.host.inspect
        def blocked(task):
            entered.set()
            release.wait(2)
            return inspect(task)
        with patch.object(self.host, 'inspect', side_effect=blocked):
            snapshot = threading.Thread(target=lambda: self.request('snapshot'))
            snapshot.start()
            try:
                self.assertTrue(entered.wait(1))
                response = self.request('end', run_id=run)
                self.assertTrue(response['ok'], response)
                self.assertEqual(self.host.weights[task['id']], 100)
            finally:
                release.set()
                snapshot.join(3)

    def test_resource_ack_is_bound_to_protocol_and_request(self):
        response = self.request('launch', app='geany')
        self.assertTrue(response['ok'])
        self.assertEqual(response['protocol'], 'guest-linux/1')
        uuid.UUID(response['request_id'])
        self.assertEqual(response['result']['task']['app'], 'geany')

    def test_oversize_and_invalid_json_are_rejected_without_mutation(self):
        for payload in (b'x\n', b'x' * 65537 + b'\n', b'[]\n'):
            with self.subTest(length=len(payload)), socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(3)
                conn.connect(str(self.path))
                conn.sendall(payload)
                result = json.loads(conn.makefile('rb').readline())
                self.assertFalse(result['ok'])
        self.assertEqual(self.host.live, {})
